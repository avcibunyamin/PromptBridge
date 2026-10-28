"""G6: FastAPI REST API for the Conductor approval panel.

Endpoints
---------
GET  /api/envelopes              List all or ?state= filtered
GET  /api/envelopes/{eid}        Envelope + versions + history
GET  /api/envelopes/{eid}/content  Raw approved/captured bytes
POST /api/envelopes/{eid}/approve  PENDING→APPROVED→DELIVERED
POST /api/envelopes/{eid}/reject   PENDING→REJECTED
POST /api/envelopes/{eid}/edit     Append an edited version
POST /api/reconcile                Reconcile DELIVERING records
GET  /api/events                   SSE live state stream
GET  /health                       Liveness probe
GET  /ui/{path}                    React static files (prod)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import store
from .adapters import process as proc
from .security import ALLOWED_ORIGINS, mutation_guard
from .errors import install_handlers
from .routes import inbox_for
from .delivery import DeliveryConflict, deliver, reconcile
from .store import DeliveredHashMismatch, InvalidTransition, VersionMismatch
from .transitions import EnvelopeNotFound, InvalidEvent, transition, Event

log = logging.getLogger(__name__)

# ── project paths (resolved from env or package location) ─────────────────────

def _root() -> Path:
    env = os.environ.get("AI_ORCHESTRATOR_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def _db_path() -> Path:
    root = _root()
    db_dir = root / ".system"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "state.db"


def _inbox_dir() -> Path:
    d = _root() / "agents" / "claude" / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git_commit() -> str:
    """F3-06: short HEAD of this checkout, `+dirty` if tracked code under ai-orchestrator/ changed.

    Read once per app; argv only (no shell), 2 s limit; any failure is 'unknown'."""
    code_dir = str(Path(__file__).resolve().parent.parent)
    try:
        head = subprocess.run(['git', '-C', code_dir, 'rev-parse', '--short', 'HEAD'],
                              capture_output=True, text=True, timeout=2)
        if head.returncode != 0 or not head.stdout.strip():
            return 'unknown'
        dirty = subprocess.run(['git', '-C', code_dir, 'status', '--porcelain', '--untracked-files=no', '--', '.'],
                               capture_output=True, text=True, timeout=2)
        return head.stdout.strip() + ('+dirty' if dirty.returncode != 0 or dirty.stdout.strip() else '')
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def _get_conn():
    """Open a per-request SQLite connection (thread-safe)."""
    conn = store.connect(_db_path())
    return conn


# ── SSE event bus ─────────────────────────────────────────────────────────────

class EventBus:
    """Broadcast envelope state changes to all SSE subscribers."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []
        self._loop = None

    def subscribe(self) -> asyncio.Queue:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def publish(self, data: dict) -> None:
        payload = json.dumps(data, default=str)
        def _put():
            for q in self._queues:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
        if self._loop:
            self._loop.call_soon_threadsafe(_put)


_bus = EventBus()


def notify_state_change(eid: str, state: str) -> None:
    _bus.publish({"eid": eid, "state": state})


def _emit(eid: str, state: str) -> None:
    """Fire-and-forget SSE event (safe to call from sync code)."""
    notify_state_change(eid, state)


# ── request models ─────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    actor: str = Field(default="human", min_length=1)
    version_id: str | None = None

class RejectRequest(BaseModel):
    actor: str = Field(default="human", min_length=1)
    reason: str = Field(default="")

# ── app factory ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_path = _db_path()
    log.info("Conductor API starting — db=%s", db_path)
    from .session_api import worker_lifespan
    from . import preview
    app.state.preview_registry = {}
    async with worker_lifespan(app, db_path):
        conn = store.connect(db_path)
        try:
            preview.reconcile(conn)
        finally:
            conn.close()
        try:
            yield
        finally:
            try:
                conn = store.connect(db_path)
            except Exception:
                log.exception("preview shutdown db connect failed; stopping processes directly")
                for workspace, entry in list(app.state.preview_registry.items()):
                    try:
                        await proc.stop_process(entry["process"], entry.get("owner"))
                    except Exception:
                        log.exception("preview shutdown direct stop failed for %s", workspace)
                app.state.preview_registry.clear()
            else:
                try:
                    for workspace in list(app.state.preview_registry):
                        try:
                            await preview.stop(conn, workspace, app.state.preview_registry)
                        except Exception:
                            log.exception("preview shutdown cleanup failed for %s", workspace)
                finally:
                    conn.close()
    log.info("Conductor API stopped")


_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "testclient"})


class _LoopbackOnlyMiddleware:
    """Pure ASGI middleware rejecting non-loopback HTTP/WebSocket clients."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if client is not None and (not client or client[0] not in _LOOPBACK_CLIENTS):
                resp = PlainTextResponse("loopback only", status_code=403)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Conductor API",
        version="0.6.0",
        description="Conductor Phase 1 approval panel backend",
        lifespan=_lifespan,
    )

    _static = static_dir or (Path(__file__).parent / "static")
    commit = _git_commit()

    from .session_api import router as session_router
    app.include_router(session_router)
    from .works_api import router as works_router
    app.include_router(works_router)
    from .preview_api import router as preview_router
    app.include_router(preview_router)
    from .mobile_api import router as mobile_router
    app.include_router(mobile_router)
    from .user_settings import router as settings_router   # TB-19
    app.include_router(settings_router)
    from .github import router as github_router   # TB-21
    app.include_router(github_router)
    from .isolation import router as isolation_router   # TB-26
    app.include_router(isolation_router)
    from .test_runner import router as tests_router   # TB-27
    app.include_router(tests_router)
    from .push import router as push_router   # TB-29
    app.include_router(push_router)
    from .folder_picker import router as folder_router   # K-041
    app.include_router(folder_router)
    from .accounts import router as accounts_router   # TB-39
    app.include_router(accounts_router)
    install_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── helpers ────────────────────────────────────────────────────────────────

    def _http_error(exc: Exception) -> HTTPException:
        if isinstance(exc, EnvelopeNotFound):
            return HTTPException(404, detail={"code": "not_found", "detail": str(exc)})
        if isinstance(exc, (InvalidTransition, InvalidEvent)):
            return HTTPException(409, detail={"code": "invalid_transition", "detail": str(exc)})
        if isinstance(exc, VersionMismatch):
            return HTTPException(409, detail={"code": "version_mismatch", "detail": str(exc)})
        if isinstance(exc, (DeliveryConflict, DeliveredHashMismatch)):
            return HTTPException(409, detail={"code": "delivery_conflict", "detail": str(exc)})
        if isinstance(exc, KeyError):
            return HTTPException(404, detail={"code": "not_found", "detail": str(exc)})
        if isinstance(exc, ValueError):
            return HTTPException(422, detail={"code": "validation_error", "detail": str(exc)})
        raise exc

    # ── routes ─────────────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        # F3-06: which code, schema and panel bundle this server is actually running/serving.
        try:
            conn = _get_conn()
            try:
                row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            finally:
                conn.close()
            schema = row[0] if row else None
        except Exception:
            schema = None
        try:
            found = re.search(r'/assets/(index-[A-Za-z0-9_-]+\.js)', (_static / 'index.html').read_text())
            bundle = found.group(1) if found else None
        except OSError:
            bundle = None
        return {"status": "ok", "db": str(_db_path()),
                "version": {"commit": commit, "schema": schema, "ui_bundle": bundle}}


    @app.get("/api/setup/checks")
    def setup_checks(request: Request):
        if getattr(request.state, "conductor_mobile_device", None):
            raise HTTPException(404, detail="not found")
        from . import doctor
        results = doctor.checks(_root(), server=False)
        return {"checks": results, "ok": not any(item['level'] == 'FAIL' for item in results)}


    @app.get("/api/envelopes")
    def list_envelopes(state: str | None = None):
        conn = _get_conn()
        try:
            if state:
                rows = store.list_by_state(conn, state.upper())
            else:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM envelopes ORDER BY created_at, id"
                )]
            return rows
        finally:
            conn.close()

    @app.get("/api/envelopes/{eid}")
    def get_envelope(eid: str):
        conn = _get_conn()
        try:
            env = store.get_envelope(conn, eid)
            if env is None:
                raise HTTPException(404, detail={"code": "not_found", "detail": eid})
            versions = [dict(r) for r in conn.execute(
                "SELECT version_id, kind, sha256, bytes, created_at, created_by "
                "FROM versions WHERE envelope_id=? ORDER BY rowid", (eid,)
            )]
            history = store.get_history(conn, eid)
            return {"envelope": env, "versions": versions, "history": history}
        finally:
            conn.close()

    @app.get("/api/envelopes/{eid}/content")
    def get_content(eid: str, version_id: str | None = Query(default=None)):
        conn = _get_conn()
        try:
            env = store.get_envelope(conn, eid)
            if env is None:
                raise HTTPException(404, detail={"code": "not_found", "detail": eid})
            vid = version_id or env.get("approved_version_id") or env.get("captured_version_id")
            if not vid:
                raise HTTPException(404, detail={"code": "no_content"})
            version = store.get_version(conn, vid)
            if version is None or version["envelope_id"] != eid:
                raise HTTPException(404, detail={"code": "version_not_found", "detail": vid})
            content = store.get_content(conn, vid)
            return PlainTextResponse(
                content.decode("utf-8", errors="replace"),
                media_type="text/plain; charset=utf-8",
            )
        finally:
            conn.close()

    @app.post("/api/envelopes/{eid}/approve", dependencies=[Depends(mutation_guard)])
    def approve_envelope(eid: str, body: ApproveRequest):
        conn = _get_conn()
        try:
            env = store.get_envelope(conn, eid)
            if env is None:
                raise HTTPException(404, detail={"code": "not_found", "detail": eid})
            vid = body.version_id or env.get("approved_version_id") or env.get("captured_version_id")
            try:
                inbox = inbox_for(_root(), env)
                transition(conn, eid, Event.HUMAN_APPROVE,
                           expected_state="PENDING_APPROVAL",
                           actor=body.actor,
                           payload={"version_id": vid})
                final = deliver(conn, eid, inbox_dir=inbox, actor=body.actor)
            except Exception as exc:
                raise _http_error(exc) from exc
            updated = store.get_envelope(conn, eid)
            _emit(eid, updated["state"])
            return {"envelope": updated, "delivered_path": str(final)}
        finally:
            conn.close()

    @app.post("/api/envelopes/{eid}/reject", dependencies=[Depends(mutation_guard)])
    def reject_envelope(eid: str, body: RejectRequest):
        conn = _get_conn()
        try:
            env = store.get_envelope(conn, eid)
            if env is None:
                raise HTTPException(404, detail={"code": "not_found", "detail": eid})
            payload = {}
            if body.reason:
                payload["reason"] = body.reason
            try:
                transition(conn, eid, Event.HUMAN_REJECT,
                           expected_state="PENDING_APPROVAL",
                           actor=body.actor,
                           payload=payload)
            except Exception as exc:
                raise _http_error(exc) from exc
            updated = store.get_envelope(conn, eid)
            _emit(eid, updated["state"])
            return {"envelope": updated}
        finally:
            conn.close()

    @app.post("/api/envelopes/{eid}/edit", dependencies=[Depends(mutation_guard)])
    async def edit_envelope(eid: str, request: Request):
        """Raw request body becomes the new edited version content."""
        conn = _get_conn()
        try:
            env = store.get_envelope(conn, eid)
            if env is None:
                raise HTTPException(404, detail={"code": "not_found", "detail": eid})
            if env["state"] != "PENDING_APPROVAL":
                raise HTTPException(409, detail={"code": "invalid_transition",
                                                  "detail": "editing requires PENDING_APPROVAL"})
            content = await request.body()
            if not content:
                raise HTTPException(422, detail={"code": "empty_body"})
            try:
                vid = store.add_edited_version(conn, eid, content, actor="human")
            except Exception as exc:
                raise _http_error(exc) from exc
            updated = store.get_envelope(conn, eid)
            _emit(eid, updated["state"])
            return {"version_id": vid, "envelope": updated}
        finally:
            conn.close()

    @app.post("/api/reconcile", dependencies=[Depends(mutation_guard)])
    def run_reconcile():
        conn = _get_conn()
        try:
            results = reconcile(conn, inbox_dir=_inbox_dir(), actor="system")
            for r in results:
                _emit(r["eid"], r.get("action", "unknown"))
            return {"results": results}
        finally:
            conn.close()

    @app.get("/api/events")
    async def sse_events(request: Request):
        """Server-Sent Events stream for live envelope state updates."""
        async def _generate():
            q = _bus.subscribe()
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"event: envelope_update\ndata: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                _bus.unsubscribe(q)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── static files (production build) ───────────────────────────────────────
    class _UIStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            resp = await super().get_response(path, scope)
            if resp.status_code in (200, 304):
                resp.headers["Cache-Control"] = "no-cache"
            return resp

    class _AssetsStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            resp = await super().get_response(path, scope)
            if resp.status_code == 200:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

    if _static.exists():
        import mimetypes
        mimetypes.add_type('application/manifest+json', '.webmanifest')   # TB-12: PWA manifest
        app.mount("/ui", _UIStaticFiles(directory=str(_static), html=True), name="ui")
        # Panel build referansları mutlak /assets/... (vite base='/'); rebuild
        # gerekmesin diye o yolu da servis et.
        _assets = _static / "assets"
        if _assets.exists():
            app.mount("/assets", _AssetsStaticFiles(directory=str(_assets)), name="assets")

    app.add_middleware(_LoopbackOnlyMiddleware)
    return app


# Module-level app instance (uvicorn uses this)
app = create_app()
