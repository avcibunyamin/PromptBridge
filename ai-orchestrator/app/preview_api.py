"""TB-13 preview API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import preview, store, user_settings
from app.security import mutation_guard

router = APIRouter(prefix="/api/previews")


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_path: str = Field(min_length=1)


class ApproveBody(WorkspaceBody):
    command_digest: str = Field(min_length=64, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    actor: str = Field(default="human", min_length=1, max_length=80)


class StartBody(WorkspaceBody):
    command_digest: str = Field(min_length=64, max_length=64)


def _db(request: Request):
    return store.connect(request.app.state.runner_db)


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, preview.PreviewError):
        return HTTPException(exc.status, detail={"code": exc.code, "detail": str(exc)})
    if isinstance(exc, (ValueError, OSError)):
        return HTTPException(422, detail={"code": "validation_error", "detail": str(exc)})
    raise exc


def _detect_for(path: str, *, port: int | None = None) -> dict:
    return preview.detect(path, port=port)


def _current_approved_suggestion(conn, workspace_path: str, command_digest: str) -> dict:
    approved = preview.approval(conn, workspace_path, command_digest)
    if not approved:
        raise preview.PreviewError("approval_required", "Bu çalıştırma komutu için açık onay gerekiyor", 409)
    suggestion = _detect_for(workspace_path, port=approved.get("port"))
    if suggestion["command_digest"] != command_digest:
        raise preview.PreviewError("digest_changed", "Çalıştırma komutu değişti; yeniden inceleyin", 409)
    return suggestion


@router.get("/detect")
def detect(workspace_path: str):
    try:
        return _detect_for(workspace_path)
    except Exception as exc:
        raise _fail(exc) from exc


@router.get("/status")
def status(workspace_path: str, request: Request):
    try:
        root = str(preview.normalize_workspace(workspace_path))
        conn = _db(request)
        try:
            row = preview.status(conn, root)
            suggestion = row.get("suggestion") if row and row.get("suggestion") else None
            if not suggestion:
                approved = preview.approval(conn, root)
                try:
                    suggestion = _detect_for(root, port=approved.get("port") if approved else None)
                except preview.PreviewError:
                    pass
            approved = bool(suggestion and preview.approved(conn, root, suggestion["command_digest"]))
            return {"workspace_path": root, "suggestion": suggestion, "approved": approved, "server": row}
        finally:
            conn.close()
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/approve", dependencies=[Depends(mutation_guard)])
def approve(body: ApproveBody, request: Request):
    try:
        suggestion = _detect_for(body.workspace_path, port=body.port)
        if suggestion["command_digest"] != body.command_digest:
            raise preview.PreviewError("digest_changed", "Çalıştırma komutu değişti; yeniden inceleyin", 409)
        conn = _db(request)
        try:
            preview.approve(conn, suggestion, actor=body.actor)
            return {"approved": True, "suggestion": suggestion}
        finally:
            conn.close()
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/start", dependencies=[Depends(mutation_guard)])
async def start(body: StartBody, request: Request):
    try:
        root = str(preview.normalize_workspace(body.workspace_path))
        conn = _db(request)
        try:
            suggestion = _current_approved_suggestion(conn, root, body.command_digest)
            server = await preview.start(conn, suggestion, request.app.state.preview_registry,
                                         sandbox=user_settings.preview_sandbox(request.app.state.runner_db))
            return {"server": server}
        finally:
            conn.close()
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/stop", dependencies=[Depends(mutation_guard)])
async def stop(body: WorkspaceBody, request: Request):
    try:
        conn = _db(request)
        try:
            server = await preview.stop(conn, body.workspace_path, request.app.state.preview_registry)
            return {"server": server}
        finally:
            conn.close()
    except Exception as exc:
        raise _fail(exc) from exc


@router.post("/restart", dependencies=[Depends(mutation_guard)])
async def restart(body: StartBody, request: Request):
    try:
        root = str(preview.normalize_workspace(body.workspace_path))
        conn = _db(request)
        try:
            suggestion = _current_approved_suggestion(conn, root, body.command_digest)
            current = preview.status(conn, root)
            if current and current["status"] in ("starting", "running"):
                await preview.stop(conn, body.workspace_path, request.app.state.preview_registry)
            server = await preview.start(conn, suggestion, request.app.state.preview_registry,
                                         sandbox=user_settings.preview_sandbox(request.app.state.runner_db))
            return {"server": server}
        finally:
            conn.close()
    except Exception as exc:
        raise _fail(exc) from exc
