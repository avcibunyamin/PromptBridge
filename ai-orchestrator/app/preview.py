"""TB-13: local project preview detection and process management.

Preview commands execute user project code. Keep the boundary narrow:
argv only, loopback binding, explicit approval tied to a digest, and owned
process groups for shutdown/recovery.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import os
import tempfile
from pathlib import Path
import shutil
import socket
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from app.security import ALLOWED_ORIGINS
from app import store
from app.adapters import process as proc

MAX_LOG_CHARS = 12000
READY_TIMEOUT = 20.0
SETTLE = 1.0   # seconds a ready server must stay alive (F5-Kapanış)
POLL_INTERVAL = 0.2

# TB-13a: a dev server gets only these variables (plus LC_* and the suggestion's own HOST/PORT/...):
# no provider keys, tokens, agent sockets or Conductor settings from the user's or launchd's environment.
PREVIEW_ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TZ", "TERM")


class PreviewError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 422):
        super().__init__(detail)
        self.code = code
        self.status = status


def _now() -> str:
    return store._now()


def normalize_workspace(path: str) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise PreviewError("workspace_missing", "Proje klasörü bulunamadı")
    return root


def _sha_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _digest_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


# K-066: a preview whose origin the panel trusts (ALLOWED_ORIGINS: the panel and its Vite dev server) could send
# mutating requests that mutation_guard accepts; with allow-same-origin its iframe would act as that origin too.
RESERVED_PORTS = frozenset(urlsplit(o).port for o in ALLOWED_ORIGINS)


def _choose_port(default: int) -> int:
    if default not in RESERVED_PORTS and _port_available(default):
        return default
    while (port := free_port()) in RESERVED_PORTS:
        pass
    return port


def _which(name: str) -> str | None:
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def _package_json(root: Path, port: int | None) -> dict[str, Any] | None:
    pkg = root / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreviewError("invalid_package_json", f"package.json okunamadı: {exc}") from exc
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return None
    script = next((name for name in ("dev", "start") if isinstance(scripts.get(name), str)), None)
    if not script:
        return None
    npm = _which("npm")
    if not npm:
        raise PreviewError("missing_binary", "npm bulunamadı")
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    framework = "node"
    extra: list[str] = []
    if "vite" in deps or "vite" in str(scripts.get(script, "")):
        framework = "vite"
        port = port or _choose_port(5173)
        extra = ["--", "--host", "127.0.0.1", "--port", str(port)]
    elif "next" in deps or "next" in str(scripts.get(script, "")):
        framework = "next"
        port = port or _choose_port(3000)
        extra = ["--", "--hostname", "127.0.0.1", "--port", str(port)]
    elif "react-scripts" in deps:
        framework = "cra"
        port = port or _choose_port(3000)
    else:
        port = port or _choose_port(3000)
    run_scripts = [
        [name, scripts[name][:500] + "…" if len(scripts[name]) > 500 else scripts[name]]
        for name in (f"pre{script}", script, f"post{script}")
        if isinstance(scripts.get(name), str)
    ]
    return {
        "kind": framework,
        "label": f"npm run {script}",
        "argv": [npm, "run", script, *extra],
        "env": {"HOST": "127.0.0.1", "PORT": str(port), "BROWSER": "none"},
        "script_path": "package.json",
        "script_digest": _sha_file(pkg),
        "scripts": run_scripts,
    }


def _declares(source: str, flag: str) -> bool:
    """A real argparse `add_argument(...)` or click `option(...)` call names the flag (comments and strings do not)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in ("add_argument", "option")
               and any(isinstance(a, ast.Constant) and a.value == flag for a in node.args)
               for node in ast.walk(tree))


def _python_project(root: Path, port: int | None) -> dict[str, Any] | None:
    py = sys.executable
    port = port or _choose_port(8000)
    if (root / "manage.py").exists():
        return {
            "kind": "django",
            "label": "python manage.py runserver",
            "argv": [py, "manage.py", "runserver", f"127.0.0.1:{port}"],
            "env": {"HOST": "127.0.0.1", "PORT": str(port)},
            "script_path": "manage.py",
            "script_digest": _sha_file(root / "manage.py"),
        }
    candidates = [root / "main.py", root / "app.py"]
    app_file = next((p for p in candidates if p.exists()), None)
    if app_file and "uvicorn" in ((root / "requirements.txt").read_text(errors="ignore") if (root / "requirements.txt").exists() else ""):
        module = app_file.stem
        return {
            "kind": "fastapi",
            "label": f"python -m uvicorn {module}:app",
            "argv": [py, "-m", "uvicorn", f"{module}:app", "--host", "127.0.0.1", "--port", str(port)],
            "env": {"HOST": "127.0.0.1", "PORT": str(port)},
            "script_path": app_file.name,
            "script_digest": _sha_file(app_file),
        }
    if app_file:
        # K-065: many scripts take their port as a flag, not from PORT (ReelsVideo listens on 8080 otherwise and the
        # preview waits on the wrong port). ponytail: only argparse/click calls are matched (via ast); any other CLI
        # parser still needs PORT.
        source = app_file.read_text(errors="ignore")
        flags = (["--host", "127.0.0.1"] if _declares(source, "--host") else []) + (
            ["--port", str(port)] if _declares(source, "--port") else [])
        return {
            "kind": "python-http",
            "label": " ".join(["python", app_file.name, *flags]),
            "argv": [py, app_file.name, *flags],
            "env": {"HOST": "127.0.0.1", "PORT": str(port)},
            "script_path": app_file.name,
            "script_digest": _sha_file(app_file),
        }
    return None


def _static_site(root: Path, port: int | None) -> dict[str, Any] | None:
    """TB-13a: a plain HTML/CSS/JS folder. Python's file server only serves files (no project code runs);
    the digest does not cover index.html, so editing the page needs no new approval."""
    if not (root / "index.html").is_file():
        return None
    port = port or _choose_port(8080)
    return {
        "kind": "static",
        "label": "python -m http.server (statik site)",
        "argv": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(root)],
        "env": {"HOST": "127.0.0.1", "PORT": str(port)},
        "script_path": "index.html",
        "script_digest": None,
    }


def detect(path: str, *, port: int | None = None) -> dict[str, Any]:
    root = normalize_workspace(path)
    if port in RESERVED_PORTS:
        raise PreviewError("port_reserved", f"Port {port} PromptBridge paneline ayrılmış; başka bir port seçin", 409)
    suggestion = _package_json(root, port) or _python_project(root, port) or _static_site(root, port)
    if not suggestion:
        raise PreviewError("not_detected", "Desteklenen çalıştırma komutu algılanamadı", 404)
    chosen_port = int(suggestion["env"]["PORT"])
    url = f"http://127.0.0.1:{chosen_port}/"
    payload = {
        "workspace_path": str(root),
        "kind": suggestion["kind"],
        "argv": suggestion["argv"],
        "env": suggestion["env"],
        "script_path": suggestion["script_path"],
        "script_digest": suggestion["script_digest"],
    }
    command_digest = _digest_payload(payload)
    return {
        **suggestion,
        "workspace_path": str(root),
        "port": chosen_port,
        "url": url,
        "command_digest": command_digest,
        "requires_approval": True,
    }


def approved(conn, workspace_path: str, command_digest: str) -> bool:
    row = conn.execute("SELECT command_digest FROM preview_approvals WHERE workspace_path=?", (workspace_path,)).fetchone()
    return bool(row and row["command_digest"] == command_digest)


def approval(conn, workspace_path: str, command_digest: str | None = None) -> dict[str, Any] | None:
    if command_digest:
        row = conn.execute(
            "SELECT suggestion_json FROM preview_approvals WHERE workspace_path=? AND command_digest=?",
            (workspace_path, command_digest),
        ).fetchone()
    else:
        row = conn.execute("SELECT suggestion_json FROM preview_approvals WHERE workspace_path=?", (workspace_path,)).fetchone()
    if not row:
        return None
    return json.loads(row["suggestion_json"])


def approve(conn, suggestion: dict[str, Any], actor: str = "human") -> None:
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO preview_approvals(workspace_path,command_digest,suggestion_json,approved_at,approved_by) "
            "VALUES (?,?,?,?,?) ON CONFLICT(workspace_path) DO UPDATE SET "
            "command_digest=excluded.command_digest,suggestion_json=excluded.suggestion_json,"
            "approved_at=excluded.approved_at,approved_by=excluded.approved_by",
            (
                suggestion["workspace_path"],
                suggestion["command_digest"],
                json.dumps(suggestion, ensure_ascii=False, sort_keys=True),
                _now(),
                actor,
            ),
        )


def row_to_status(row) -> dict[str, Any] | None:
    if not row:
        return None
    data = dict(row)
    suggestion = json.loads(data["suggestion_json"]) if data.get("suggestion_json") else None
    return {**data, "suggestion": suggestion}


def status(conn, workspace_path: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM preview_servers WHERE workspace_path=?", (workspace_path,)).fetchone()
    return row_to_status(row)


def record_status(conn, workspace_path: str, **values: Any) -> None:
    current = status(conn, workspace_path)
    payload = {
        "status": "stopped",
        "command_digest": None,
        "suggestion_json": None,
        "pid": None,
        "pgid": None,
        "start_time": None,
        "boot_time": None,
        "port": None,
        "url": None,
        "log_tail": "",
        "error": None,
        "started_at": None,
        "updated_at": _now(),
    }
    if current:
        payload.update({k: current.get(k) for k in payload if k in current})
    payload.update(values)
    payload["updated_at"] = _now()
    with store.transaction(conn):
        conn.execute(
            "INSERT INTO preview_servers(workspace_path,status,command_digest,suggestion_json,pid,pgid,start_time,boot_time,port,url,log_tail,error,started_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(workspace_path) DO UPDATE SET "
            "status=excluded.status,command_digest=excluded.command_digest,suggestion_json=excluded.suggestion_json,"
            "pid=excluded.pid,pgid=excluded.pgid,start_time=excluded.start_time,boot_time=excluded.boot_time,"
            "port=excluded.port,url=excluded.url,log_tail=excluded.log_tail,error=excluded.error,"
            "started_at=excluded.started_at,updated_at=excluded.updated_at",
            (
                workspace_path,
                payload["status"],
                payload["command_digest"],
                payload["suggestion_json"],
                payload["pid"],
                payload["pgid"],
                payload["start_time"],
                payload["boot_time"],
                payload["port"],
                payload["url"],
                payload["log_tail"][-MAX_LOG_CHARS:],
                payload["error"],
                payload["started_at"],
                payload["updated_at"],
            ),
        )


# TB-22 (K-029, experimental, off by default): run the preview under macOS sandbox-exec. The tool is deprecated but
# shipped; a profile can only narrow what the process may do. Reads: the system, the workspace, TMPDIR and
# toolchains under HOME; writes: the workspace, TMPDIR and npm's cache; network: it may listen on localhost but
# connect nowhere. The usual secret files, `open`/`osascript`/the pasteboard and the keychain daemons stay out of reach.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SYSTEM_READ = ("/usr", "/bin", "/sbin", "/System", "/Library", "/opt/homebrew", "/usr/local", "/private/var/db/timezone",
               "/private/etc", "/dev", "/Applications/Xcode.app")
HOME_TOOLS = (".nvm", ".volta", ".bun", ".pyenv", ".rbenv", ".deno", ".cargo", ".rustup", "Library/Python",
              "Library/pnpm", ".local/share/pnpm")
HOME_WRITE = (".npm",)   # npm's cache and its per-run logs
HOME_SECRETS = (".ssh", ".netrc", ".npmrc", ".git-credentials", ".config/gh", ".aws", ".docker", ".kube", ".gnupg",
                "Library/Keychains")
# `open URL` reaches LaunchServices outside the sandbox (measured: denying its mach services did not stop it).
DENIED_TOOLS = ("/usr/bin/open", "/usr/bin/osascript", "/usr/bin/pbcopy", "/usr/bin/pbpaste")
# Gemini TB-22 review: the keychain, LaunchServices (`open https://…` would leave the sandbox) and the pasteboard.
DENIED_SERVICES = ("com.apple.SecurityServer", "com.apple.securityd", "com.apple.security.agent", "com.apple.lsd.open",
                   "com.apple.lsd.mapdb", "com.apple.lsd.modifydb", "com.apple.coreservices.launchservicesd",
                   "com.apple.pasteboard.1", "com.apple.pasteboard.pboard")


def _sb(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(workspace: str, home: str, tmp: str, conductor_root: str, home_write: bool = True) -> str:
    real = os.path.realpath
    home_paths = lambda names: [real(os.path.join(home, n)) for n in names if os.path.exists(os.path.join(home, n))]
    python = {real(sys.prefix), real(sys.base_prefix)}   # Python previews run on Conductor's own interpreter
    reads = [real(p) for p in SYSTEM_READ] + sorted(python) + [real(workspace), real(tmp)] + home_paths(HOME_TOOLS + HOME_WRITE)
    writes = [real(workspace), real(tmp)] + (home_paths(HOME_WRITE) if home_write else [])   # TB-27: tests get none
    secrets = [real(os.path.join(home, n)) for n in HOME_SECRETS] + [real(os.path.join(conductor_root, ".system"))]
    sub = lambda paths: " ".join(f"(subpath {_sb(p)})" for p in paths)
    return "\n".join([
        "(version 1)", "(deny default)",
        "(allow process-fork) (allow process-exec) (allow signal (target same-sandbox)) (allow sysctl-read)",
        "(allow mach-lookup) (allow ipc-posix-shm) (allow ipc-posix-sem) (allow file-read-metadata)",
        f'(allow file-read* (literal "/") {sub(reads)})',
        f'(allow file-write* {sub(writes)} (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper"))',
        '(allow network-bind (local ip "localhost:*")) (allow network-inbound (local ip "localhost:*"))',
        # localhost only (Turbopack talks to its own workers on random ports), never Conductor's API, its rehearsal
        # port or the mobile gateway (Gemini TB-22 P1). ponytail: other local services (a proxy, a database) stay reachable.
        # No outbound connection at all, localhost included (Gemini TB-22 P1). Measured on this macOS: a port-specific
        # deny under `localhost:*` does not match, so "localhost except Conductor's API" cannot be written; with
        # localhost open, project code could call Conductor's API (switch the sandbox off, approve, push). Cost:
        # Next.js's Turbopack dev server, which talks to its workers over localhost TCP, fails in this mode.
        "(deny process-exec " + " ".join(f"(literal {_sb(p)})" for p in DENIED_TOOLS) + ")",
        "(deny mach-lookup " + " ".join(f"(global-name {_sb(n)})" for n in DENIED_SERVICES) + ")",
        f"(deny file-read* file-write* {sub(secrets)})",
    ])


def sandboxed(argv: list[str], workspace: str, env: dict[str, str], home_write: bool = True) -> list[str]:
    """The sandbox-exec argv; sets env["TMPDIR"] to a fresh private folder (the shared TMPDIR holds other apps' files).
    ponytail: the folder is left for macOS's periodic /var/folders cleanup."""
    root = os.environ.get("AI_ORCHESTRATOR_ROOT") or str(Path(__file__).resolve().parents[1])
    env["TMPDIR"] = tempfile.mkdtemp(prefix="conductor-preview-")
    home = env.get("HOME") or str(Path.home())
    if home_write:
        os.makedirs(os.path.join(home, ".npm"), exist_ok=True)   # npm writes there; a missing folder would not be allowed
    profile = sandbox_profile(workspace, home, env["TMPDIR"], root, home_write)
    return [SANDBOX_EXEC, "-p", profile, *argv]


def sanitized_env(overrides: dict[str, str]) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k in PREVIEW_ENV_ALLOW or k.startswith("LC_")}
    base.update({k: str(v) for k, v in overrides.items()})
    return base


async def _read_stream(stream: asyncio.StreamReader | None, sink: list[str]) -> None:
    if not stream:
        return
    while True:
        chunk = await stream.read(2048)
        if not chunk:
            return
        sink.append(chunk.decode("utf-8", errors="replace"))
        joined = "".join(sink)
        if len(joined) > MAX_LOG_CHARS:
            sink[:] = [joined[-MAX_LOG_CHARS:]]


async def _ready(url: str, timeout: float = READY_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)

            async def probe() -> int:
                reader, writer = await asyncio.open_connection(parsed.hostname, port)
                try:
                    target = parsed.path or "/"
                    if parsed.query:
                        target += "?" + parsed.query
                    writer.write(f"GET {target} HTTP/1.1\r\nHost: {parsed.hostname}\r\nConnection: close\r\n\r\n".encode())
                    await writer.drain()
                    headers = await reader.readuntil(b"\r\n\r\n")
                    return int(headers.split(b"\r\n", 1)[0].split()[1])
                finally:
                    writer.close()

            status_code = await asyncio.wait_for(probe(), min(0.6, deadline - time.monotonic()))
            if 100 <= status_code < 500:
                return True
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            await asyncio.sleep(POLL_INTERVAL)
    return False


async def start(conn, suggestion: dict[str, Any], registry: dict[str, Any], *, sandbox: bool = False) -> dict[str, Any]:
    workspace_path = suggestion["workspace_path"]
    if not approved(conn, workspace_path, suggestion["command_digest"]):
        raise PreviewError("approval_required", "Bu çalıştırma komutu için açık onay gerekiyor", 409)
    existing = status(conn, workspace_path)
    if existing and existing["status"] in ("starting", "running"):
        raise PreviewError("already_running", "Bu proje için önizleme zaten çalışıyor", 409)
    # F5-Kapanış: readiness accepts any HTTP answer on the port, so a port another server already holds would make
    # someone else's site look like this preview. ponytail: checked once before spawn; a server that grabs the
    # port during start-up still races it.
    if not _port_available(int(suggestion["port"])):
        raise PreviewError("port_in_use", f"Port {suggestion['port']} başka bir süreç tarafından kullanılıyor", 409)
    env = sanitized_env(suggestion.get("env") or {})
    argv = sandboxed(suggestion["argv"], workspace_path, env) if sandbox else suggestion["argv"]   # TB-22
    process, owner, gate = await proc.spawn_owned(argv, cwd=workspace_path, env=env)
    log_parts: list[str] = []
    registry[workspace_path] = {"process": process, "owner": owner, "logs": log_parts}
    record_status(
        conn,
        workspace_path,
        status="starting",
        command_digest=suggestion["command_digest"],
        suggestion_json=json.dumps(suggestion, ensure_ascii=False, sort_keys=True),
        pid=owner["pid"],
        pgid=owner["pgid"],
        start_time=owner["start_time"],
        boot_time=owner["boot_time"],
        port=suggestion["port"],
        url=suggestion["url"],
        log_tail="",
        error=None,
        started_at=_now(),
    )
    proc.release_gate(gate)
    asyncio.create_task(_read_stream(process.stdout, log_parts))
    asyncio.create_task(_read_stream(process.stderr, log_parts))
    ready = await _ready(suggestion["url"])
    exited = False
    if ready:
        # F5-Kapanış (Codex review P2): an answer on the port proves nothing if our server lost the port to
        # another one and died; give it a moment to fail. ponytail: a server that stays alive on another port
        # (e.g. a dev server that falls back to the next free port) is not detected.
        try:
            await asyncio.wait_for(process.wait(), SETTLE)
            exited = True
        except asyncio.TimeoutError:
            pass
    if not ready or exited:
        try:
            if exited:
                await proc.reap_owned(owner)              # the leader is gone; reap what may be left of its group
            else:
                await proc.stop_process(process, proc.remember_group(owner))
        finally:
            record_status(conn, workspace_path, status="failed", log_tail="".join(log_parts),
                          error="Önizleme süreci erken çıktı" if exited else "Hazır olma süresi doldu")
            registry.pop(workspace_path, None)
        if exited:
            raise PreviewError("process_exited", "Önizleme süreci erken çıktı; port başka bir süreçte olabilir", 409)
        raise PreviewError("readiness_timeout", "Önizleme hazır olmadı", 504)
    owner = proc.remember_group(owner)
    registry[workspace_path]["owner"] = owner
    record_status(conn, workspace_path, status="running", log_tail="".join(log_parts), error=None)
    return status(conn, workspace_path) or {}


async def stop(conn, workspace_path: str, registry: dict[str, Any]) -> dict[str, Any]:
    root = str(normalize_workspace(workspace_path))
    entry = registry.pop(root, None)
    row = status(conn, root)
    if entry:
        await proc.stop_process(entry["process"], entry.get("owner"))
    elif row and row.get("pid"):
        owner = {k: row[k] for k in ("pid", "pgid", "start_time", "boot_time")}
        proof = await proc.reap_owned(owner)
        if not proof.get("ok"):
            raise PreviewError("cleanup_failed", "Önizleme süreci temizlenemedi", 409)
    record_status(conn, root, status="stopped", log_tail="".join(entry.get("logs", [])) if entry else row.get("log_tail", "") if row else "", error=None)
    return status(conn, root) or {}


def reconcile(conn) -> None:
    rows = conn.execute("SELECT * FROM preview_servers WHERE status IN ('starting','running')").fetchall()
    for row in rows:
        if not proc.group_exists(row["pgid"]):
            record_status(conn, row["workspace_path"], status="failed", error="Sunucu yeniden başlarken süreç bulunamadı")
