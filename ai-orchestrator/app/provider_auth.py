"""F3-03 session check: `codex login status` / `claude auth status` / `agy models` (no model call, no quota).

Only GET /api/sessions/providers?check_auth=true calls `check` (an explicit user action). The output may contain account details (e-mail,
organisation): it is classified in memory and discarded — never stored, returned or logged.
"""
import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
import psutil
from app.adapters.process import group_members
from app.adapters.codex import codex_env
from app.provider_policy import kind, model_of

log = logging.getLogger(__name__)
TTL, TIMEOUT, LIMIT, VERIFY = 60.0, 5.0, 4096, 1.0
SLOW = {'agy': 15.0}                          # `agy models` starts a language server first (3-4 s seen)
ARGS = {'codex': ('login', 'status'), 'claude': ('auth', 'status'), 'agy': ('models',)}
_cache = {}                                   # identity -> (state, monotonic, wall-clock)
_locks = {}                                   # identity -> Lock; concurrent GETs share one check per identity
_locks_guard = threading.Lock()
_blocked = set()                              # identities whose probe group was not verified dead (until restart)


def classify(provider, code, out, model=None):
    """Verified shapes (codex-cli 0.155.0-alpha.9, Claude Code 2.1.284); anything else is unknown."""
    if provider == 'claude':
        try:
            data = json.loads(out)
        except ValueError:
            try:
                data, _ = json.JSONDecoder().raw_decode(out, out.index('{'))
            except ValueError:
                return 'unknown'
        value = data.get('loggedIn') if isinstance(data, dict) else None
        return {True: 'authenticated', False: 'unauthenticated'}.get(value, 'unknown') if isinstance(value, bool) else 'unknown'
    text = out.lower()
    if provider == 'agy':   # TB-07: `agy models` lists models (0) or asks to sign in (1); no model call
        if 'please sign in' in text:
            return 'unauthenticated'
        if code != 0 or 'gemini' not in text:
            return 'unknown'
        # agy updates itself and its model list changes (1.2.16 dropped claude-sonnet-4-6): a chain link
        # whose model is gone would fail as UNKNOWN without hand-off, so the check reports it.
        listed = {line.split('\t', 1)[0].strip() for line in out.splitlines() if '\t' in line}
        return 'model_unavailable' if model and model not in listed else 'authenticated'
    if 'not logged in' in text:
        return 'unauthenticated'
    return 'authenticated' if code == 0 and 'logged in' in text else 'unknown'


def _exited(pid):
    """The leader has exited but is not reaped yet (so its pid, and our group id, stay reserved)."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _read(proc, timeout=TIMEOUT):
    """(bounded output, leader exited in time). Stops as soon as the leader exits, even if a child
    still holds the pipe open; the leader is never reaped here."""
    deadline, buf, fd = time.monotonic() + timeout, b'', proc.stdout.fileno()
    while time.monotonic() < deadline:
        ready = select.select([fd], [], [], 0.05)[0]
        chunk = os.read(fd, 65536) if ready else b''
        buf += chunk[:LIMIT - len(buf)]
        if not chunk and _exited(proc.pid):
            return buf, True
        if ready and not chunk:
            time.sleep(0.05)                      # EOF or full buffer: wait for the exit without spinning
    return buf, False                             # timeout


def _group_gone(pgid):
    """Group death is verified from the process table, never inferred from the leader's exit."""
    deadline = time.monotonic() + VERIFY
    try:
        while group_members(pgid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True
    except Exception:
        return False


def _cleanup(proc):
    """(leader exit code or None, group verified dead). Always signals the whole group, even when the
    leader already exited: children may live on. The leader is still unreaped here, so proc.pid cannot
    name anyone else's group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass                                      # e.g. macOS EPERM when only the zombie leader is left; verified below
    try:
        code = proc.wait(timeout=VERIFY)
    except subprocess.TimeoutExpired:
        code = None
    proc.stdout.close()
    return code, _group_gone(proc.pid)


def _probe(provider, binary, home=None, model=None):   # provider = kind (`codex`), home = that account's
    """(state, group verified dead). Never raises. Fixed argv, no shell, own process group, bounded
    read, hard timeout. Only a failure to start counts as clean; any error after the start still
    runs the cleanup and keeps its verdict."""
    if provider == 'ollama':   # TB-14: Ollama's own API on loopback; no process, no sign-in
        from app import catalog
        found, error, _ = catalog._ollama()
        if error:
            return 'unknown', True
        hit = next((e for e in found if e['model'] == model), None)
        return ('model_unavailable' if hit is None or hit['status'] != 'available' else 'authenticated'), True
    try:
        # K-014: Codex is checked in Conductor's CODEX_HOME; without one the check fails closed.
        if provider == 'agy':
            from app.adapters.agy import prepare_home   # its own HOME and unlocked keychain (K-018/3)
            env = prepare_home(home)
        else:
            env = codex_env(home) if provider == 'codex' else None
        proc = subprocess.Popen([binary, *ARGS[provider]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env)
    except Exception:
        return 'unknown', True                    # nothing was started (e.g. the binary vanished)
    buf, exited, code, gone = b'', False, None, False
    try:
        buf, exited = _read(proc, SLOW.get(provider, TIMEOUT))
    except Exception:
        exited = False                            # e.g. psutil AccessDenied: the result is unknown, cleanup still runs
    try:
        code, gone = _cleanup(proc)
    except Exception:
        gone = False
    state = classify(provider, code, buf.decode('utf-8', 'replace'), model) if exited and code is not None else 'unknown'
    buf = b''                                     # the output is not kept
    return state, gone


def cached(provider):
    """The last result while it is fresh, without probing."""
    hit = _cache.get(provider)
    return (hit[0], hit[2]) if hit and time.monotonic() - hit[1] < TTL else ('unknown', None)


def health(provider):
    return 'probe_cleanup_failed' if provider in _blocked else 'ok'


def check(provider, binary, home=None):
    """(state, checked_at wall-clock). Cached for TTL seconds; one probe at a time per identity
    (TB-06: `codex@is` is checked in its own home and cached apart from `codex`).

    If a probe's process group could not be verified dead, that identity gets no new probe until
    the server restarts (probes must not pile up); `health` reports it."""
    with _locks_guard:
        lock = _locks.setdefault(provider, threading.Lock())
    with lock:
        if provider in _blocked:
            return 'unknown', None
        hit = _cache.get(provider)
        if hit and time.monotonic() - hit[1] < TTL:
            return hit[0], hit[2]
        try:
            state, gone = _probe(kind(provider), binary, home, model_of(provider))
        except Exception:                         # _probe does not raise; if it ever does, assume the worst
            state, gone = 'unknown', False
        if not gone:
            _blocked.add(provider)
            _cache.pop(provider, None)
            log.warning('session check for %s: process group not verified dead; further checks disabled', provider)
            return 'unknown', None
        _cache[provider] = (state, time.monotonic(), time.time())
        return state, _cache[provider][2]
