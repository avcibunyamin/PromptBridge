"""G5+G6 Demo CLI.

Usage:
    python -m app <command> [options]
    python -m app.cli <command> [options]

Environment:
    AI_ORCHESTRATOR_ROOT   project root (default: parent of this file's package)

Commands:
    serve               Start the watcher + API server (http://localhost:8765)
    watch               Start the file watcher only (blocks until Ctrl-C)
    list [--state S]    List envelopes (optionally filtered by state)
    show <eid>          Show full envelope + history as JSON
    approve <eid>       Approve and atomically deliver an envelope
    reject <eid>        Reject an envelope
    reconcile           Resolve DELIVERING envelopes after a restart
    capture <path>      Manually capture a file (bypass watcher)
    doctor              Check the installation (no provider calls)
"""

from __future__ import annotations

import argparse
import json
import logging
import asyncio
import os
import sys
from pathlib import Path

from . import store
from .delivery import deliver, reconcile
from .watcher import capture_file, run_watcher
from .routes import inbox_for

# ── project layout ─────────────────────────────────────────────────────────────

def _find_root() -> Path:
    env = os.environ.get("AI_ORCHESTRATOR_ROOT")
    if env:
        return Path(env).resolve()
    # Default: two levels up from this file (app/ → ai-orchestrator/)
    return Path(__file__).resolve().parent.parent


def _db_path(root: Path) -> Path:
    db_dir = root / ".system"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "state.db"


def _inbox_dir(root: Path) -> Path:
    d = root / "agents" / "claude" / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_db(root: Path):
    path = _db_path(root)
    conn = store.connect(path)
    store.migrate(conn)
    return conn


# ── formatting helpers ─────────────────────────────────────────────────────────

_STATE_COLORS = {
    "PENDING_APPROVAL": "\033[33m",  # yellow
    "APPROVED":         "\033[32m",  # green
    "DELIVERED":        "\033[34m",  # blue
    "REJECTED":         "\033[31m",  # red
    "FAILED":           "\033[31m",  # red
    "DELIVERING":       "\033[36m",  # cyan
    "CAPTURED":         "\033[35m",  # magenta
}
_RESET = "\033[0m"


def _color_state(state: str) -> str:
    if not sys.stdout.isatty():
        return state
    return _STATE_COLORS.get(state, "") + state + _RESET


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no envelopes)")
        return
    cols = ["id", "state", "title", "source_agent", "created_at"]
    widths = {c: max(len(c), max(len(str(r.get(c) or "")[:40]) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        cells = []
        for c in cols:
            val = str(r.get(c) or "")[:40]
            if c == "state":
                cells.append(_color_state(val).ljust(widths[c]))
            else:
                cells.append(val.ljust(widths[c]))
        print("  ".join(cells))


# ── command handlers ───────────────────────────────────────────────────────────

def cmd_serve(args, root: Path) -> int:
    """Start watcher (daemon thread) + FastAPI server (foreground)."""
    import threading
    import uvicorn
    from .api import create_app, notify_state_change

    db = _db_path(root)
    inbox = _inbox_dir(root)

    # Reconcile leftovers before any new traffic
    conn = _open_db(root)
    results = reconcile(conn, inbox_dir=inbox)
    conn.close()
    if results:
        print(f"[reconcile] Processed {len(results)} stuck envelope(s) on startup.")

    # Watcher runs in a daemon thread — dies when main process exits
    def _watcher():
        run_watcher(root, db,
                    observer_class=getattr(args, "observer_class", None),
                    require_ready=getattr(args, "require_ready", False),
                    bidirectional=getattr(args, 'bidirectional', False),
                    on_capture=notify_state_change)

    t = threading.Thread(target=_watcher, daemon=True, name="conductor-watcher")
    t.start()

    from . import quota
    quota.AUTO = True   # K-039: read an account's quota after its steps (only the real server; tests never do)

    print(f"[conductor] Watcher started (daemon)")

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8765)
    reload_ = getattr(args, "reload", False)

    # Set root env so api.py resolves the same paths
    os.environ["AI_ORCHESTRATOR_ROOT"] = str(root)
    # K-041: the model lists once at start (`agy models`, Codex model/list; no model call), so the pickers
    # are not empty after a restart. In the background: the panel is up at once; a failure only leaves the old fallback.
    def _catalog():
        from . import catalog
        from .provider_policy import load_config
        try:
            catalog.refresh(str(db), load_config(Path(db).parent / 'providers.yaml'))
        except Exception as exc:
            print(f"[conductor] Model list not refreshed at start: {exc}")
    threading.Thread(target=_catalog, daemon=True, name="conductor-catalog").start()
    static_dir = root / "app" / "static"
    app_instance = create_app(static_dir=static_dir if static_dir.exists() else None)

    print(f"[conductor] API → http://{host}:{port}  (Ctrl-C to stop)")
    if (root / "app" / "static").exists():
        print(f"[conductor] Panel → http://{host}:{port}/ui/")

    from .mobile import MobileGuard, load_config as mobile_config
    mobile = mobile_config(db)
    if not mobile or reload_:
        uvicorn.run(app_instance, host=host, port=port, reload=reload_,
                    log_level="info")
        return 0
    # TB-12 (K-024): a second loopback listener for the Tailscale-served gateway, same app behind MobileGuard.
    app_instance.state.mobile_gateway = {'host': mobile['host'], 'port': mobile['port']}
    main = uvicorn.Server(uvicorn.Config(app_instance, host=host, port=port, log_level="info"))
    gateway = uvicorn.Server(uvicorn.Config(MobileGuard(app_instance, db, mobile['host']), host='127.0.0.1',
                                            port=mobile['port'], log_level="info", lifespan='off'))
    print(f"[conductor] Mobil geçit → http://127.0.0.1:{mobile['port']} (tailscale serve: https://{mobile['host']})")

    async def _both():
        await asyncio.gather(main.serve(), gateway.serve())
    asyncio.run(_both())
    return 0


def cmd_watch(args, root: Path) -> int:
    conn = _open_db(root)
    # Reconcile any DELIVERING leftovers before starting
    results = reconcile(conn, inbox_dir=_inbox_dir(root))
    conn.close()
    if results:
        print(f"[reconcile] Processed {len(results)} stuck envelope(s) on startup.")
    run_watcher(root, _db_path(root),
                observer_class=getattr(args, "observer_class", None),
                require_ready=getattr(args, "require_ready", False),
                bidirectional=getattr(args, 'bidirectional', False))
    return 0

def cmd_list(args, root: Path) -> int:
    conn = _open_db(root)
    state_filter = getattr(args, "state", None)
    if state_filter:
        rows = store.list_by_state(conn, state_filter.upper())
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM envelopes ORDER BY created_at, id"
        )]
    conn.close()
    _print_table(rows)
    return 0


def cmd_show(args, root: Path) -> int:
    conn = _open_db(root)
    env = store.get_envelope(conn, args.eid)
    if env is None:
        print(f"Envelope not found: {args.eid}", file=sys.stderr)
        conn.close()
        return 1
    versions = [dict(r) for r in conn.execute(
        "SELECT version_id, kind, sha256, bytes, created_at, created_by "
        "FROM versions WHERE envelope_id=? ORDER BY rowid", (args.eid,)
    )]
    history = store.get_history(conn, args.eid)
    selected = env.get("approved_version_id") or env.get("captured_version_id")
    preview = store.get_content(conn, selected).decode("utf-8", errors="replace") if selected else ""
    conn.close()
    out = {"envelope": env, "versions": versions, "history": history,
           "preview_version_id": selected, "preview": preview}
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_approve(args, root: Path) -> int:
    conn = _open_db(root)
    env = store.get_envelope(conn, args.eid)
    if env is None:
        print(f"Envelope not found: {args.eid}", file=sys.stderr)
        conn.close()
        return 1

    state = env["state"]
    if state != "PENDING_APPROVAL":
        print(f"Cannot approve: envelope is in state {state!r} (expected PENDING_APPROVAL)",
              file=sys.stderr)
        conn.close()
        return 1

    from .transitions import transition, Event
    inbox = inbox_for(root, env)
    vid = env["approved_version_id"] or env["captured_version_id"]
    edit_path = getattr(args, "edit", None)
    if edit_path:
        vid = store.add_edited_version(conn, args.eid,
                                       Path(edit_path).read_bytes(), actor=args.actor,
                                       base_version_id=vid)

    # PENDING_APPROVAL → APPROVED
    transition(conn, args.eid, Event.HUMAN_APPROVE,
               expected_state="PENDING_APPROVAL",
               actor=args.actor,
               payload={"version_id": vid})

    # APPROVED → DELIVERING → DELIVERED
    try:
        final = deliver(conn, args.eid, inbox_dir=inbox, actor=args.actor)
        print(f"İletildi: {final}")
    except Exception as exc:
        print(f"✗ Delivery failed: {exc}", file=sys.stderr)
        conn.close()
        return 1

    conn.close()
    return 0


def cmd_reject(args, root: Path) -> int:
    from .transitions import transition, Event
    conn = _open_db(root)
    env = store.get_envelope(conn, args.eid)
    if env is None:
        print(f"Envelope not found: {args.eid}", file=sys.stderr)
        conn.close()
        return 1
    if env["state"] != "PENDING_APPROVAL":
        print(f"Cannot reject: state is {env['state']!r}", file=sys.stderr)
        conn.close()
        return 1

    payload = {}
    if getattr(args, "reason", None):
        payload["reason"] = args.reason

    transition(conn, args.eid, Event.HUMAN_REJECT,
               expected_state="PENDING_APPROVAL",
               actor=args.actor,
               payload=payload)
    print(f"✗ Rejected {args.eid}")
    conn.close()
    return 0


def cmd_reconcile(args, root: Path) -> int:
    conn = _open_db(root)
    results = reconcile(conn, inbox_dir=_inbox_dir(root))
    conn.close()
    if not results:
        print("(nothing to reconcile)")
        return 0
    for r in results:
        action = r.get("action", "?")
        eid = r.get("eid", "?")
        extra = r.get("sha256", r.get("error", ""))
        print(f"  {eid}  →  {action}" + (f"  ({extra})" if extra else ""))
    return 0


def cmd_capture(args, root: Path) -> int:
    conn = _open_db(root)
    path = Path(args.path).resolve()
    result = capture_file(path, conn, source_agent=args.source)
    conn.close()
    if result is None:
        print(f"Skipped (duplicate or empty): {path}", file=sys.stderr)
        return 1
    eid, vid = result
    print(f"✓ Captured → eid={eid}  vid={vid}")
    return 0


def cmd_doctor(args, root: Path) -> int:
    from . import doctor
    return doctor.run(root)


# ── argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Conductor Phase 1 CLI",
    )
    p.add_argument("--root", type=Path, default=None,
                   help="Project root (default: AI_ORCHESTRATOR_ROOT or auto-detect)")
    p.add_argument("--actor", default="human",
                   help="Actor name recorded in audit log (default: human)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging")

    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start watcher + API server")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    serve.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload (dev)")
    serve.add_argument("--require-ready", action="store_true",
                       help="Watcher: only capture files with .md.ready marker")
    serve.add_argument('--bidirectional', action='store_true',
                       help='Also watch Claude outbox and deliver approved replies to ChatGPT')

    watch = sub.add_parser("watch", help="Start the outbox file watcher")
    watch.add_argument("--require-ready", action="store_true",
                       help="Only capture files accompanied by <name>.md.ready")
    watch.add_argument('--bidirectional', action='store_true',
                       help='Watch ChatGPT and Claude outboxes')

    ls = sub.add_parser("list", help="List envelopes")
    ls.add_argument("--state", help="Filter by state (e.g. PENDING_APPROVAL)")

    show = sub.add_parser("show", help="Show envelope details as JSON")
    show.add_argument("eid", help="Envelope ID")

    approve = sub.add_parser("approve", help="Approve and deliver an envelope")
    approve.add_argument("eid", help="Envelope ID")
    approve.add_argument("--edit", type=Path,
                         help="Optional file whose bytes become the approved edited version")

    reject = sub.add_parser("reject", help="Reject an envelope")
    reject.add_argument("eid", help="Envelope ID")
    reject.add_argument("--reason", help="Rejection reason")

    sub.add_parser("reconcile", help="Resolve DELIVERING envelopes after restart")

    cap = sub.add_parser("capture", help="Manually capture a file")
    cap.add_argument("path", help="Path to the file to capture")
    cap.add_argument('--source', choices=('chatgpt', 'claude'), default='chatgpt')

    sub.add_parser("doctor", help="Check the installation (no provider calls)")

    return p


# ── entry point ────────────────────────────────────────────────────────────────

_COMMANDS = {
    "serve": cmd_serve,
    "watch": cmd_watch,
    "list": cmd_list,
    "show": cmd_show,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "reconcile": cmd_reconcile,
    "capture": cmd_capture,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    root = args.root.resolve() if args.root else _find_root()
    handler = _COMMANDS[args.command]
    return handler(args, root)


if __name__ == "__main__":
    sys.exit(main())
