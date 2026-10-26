"""TB-12 (K-024): mobile access through a gateway that Tailscale serves over HTTPS.

The gateway is a second listener on 127.0.0.1 (default 8770) in the same process. It wraps the same FastAPI
app in MobileGuard, which admits only an allow-list of endpoints, only with a device token, only for the
Tailscale user that paired the device and only within the device's scopes. Conductor itself never listens
beyond loopback; `tailscale serve` (never Funnel) carries the gateway to the user's tailnet. The main API on
8765 is unchanged: none of these checks apply there and the server-side trust mark below is never set there.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from uuid import uuid4

from . import store

PAIR_TTL = timedelta(minutes=5)
PAIR_MAX_ATTEMPTS = 5
DEFAULT_SCOPES = ('view', 'approve')            # K-024/3, K-027/1: a new device may watch and approve envelopes
SCOPES = ('view', 'approve', 'interrupt', 'command')   # K-027/2, K-033/2: the extra ones only from the Mac panel, per device
DEVICE_RATE, PAIR_RATE = 120, 10                 # requests per minute
GATEWAY_MARK = 'conductor_mobile_gateway'        # set on every request that came through the gateway
DEVICE_MARK = 'conductor_mobile_device'          # set only after the device was authenticated
LOGIN_HEADER = b'tailscale-user-login'

# (method, path pattern, scope). Anything else is 404 on the gateway. `None` scope = no token needed.
ALLOW = [
    ('GET', r'/ui(/.*)?', None), ('GET', r'/assets/[^/]+', None),
    ('POST', r'/api/mobile/pair', None),
    ('GET', r'/health', 'view'), ('GET', r'/api/mobile/me', 'view'),
    ('GET', r'/api/works', 'view'), ('GET', r'/api/works/[^/]+', 'view'),
    ('GET', r'/api/works/[^/]+/messages', 'view'), ('GET', r'/api/works/[^/]+/events', 'view'),
    ('GET', r'/api/sessions', 'view'), ('GET', r'/api/sessions/catalog', 'view'), ('GET', r'/api/sessions/quota', 'view'),
    ('GET', r'/api/sessions/providers', 'view'),
    ('GET', r'/api/sessions/[^/]+', 'view'), ('GET', r'/api/sessions/[^/]+/events', 'view'),
    ('GET', r'/api/sessions/[^/]+/recovery', 'view'),
    ('GET', r'/api/envelopes', 'view'), ('GET', r'/api/envelopes/[^/]+', 'view'),
    ('GET', r'/api/envelopes/[^/]+/content', 'view'), ('GET', r'/api/events', 'view'),
    ('POST', r'/api/envelopes/[^/]+/approve', 'approve'), ('POST', r'/api/envelopes/[^/]+/reject', 'approve'),
    # K-027: stop a run, cancel a quota wait, take a queued message back — nothing that starts work or spends quota
    ('POST', r'/api/sessions/[^/]+/cancel', 'interrupt'), ('DELETE', r'/api/sessions/[^/]+/wait', 'interrupt'),
    ('POST', r'/api/works/[^/]+/messages/[^/]+/cancel', 'interrupt'),
    # K-033 / TB-37: give work from the phone — a new work in a folder a work already uses, rename a work, a message,
    # the team preview, and theme / pinned_works / default team in Settings (other fields are refused with 403)
    ('POST', r'/api/works', 'command'), ('PATCH', r'/api/works/[^/]+', 'command'),
    ('POST', r'/api/works/[^/]+/messages', 'command'),
    ('GET', r'/api/sessions/providers/preview', 'command'),
    ('GET', r'/api/settings', 'command'), ('PUT', r'/api/settings', 'command'),
    # K-036: the phone's own push subscription and a rate-limited test notice
    ('GET', r'/api/push/key', 'view'), ('GET', r'/api/push/subscription', 'view'),
    ('PUT', r'/api/push/subscription', 'view'), ('DELETE', r'/api/push/subscription', 'view'),
    ('POST', r'/api/push/test', 'view'),
]
_ALLOW = [(m, re.compile(p), s) for m, p, s in ALLOW]
_NOT_ON_GATEWAY_QUERY = {'/api/sessions/providers': ('check_auth',)}   # would start provider processes


def _now():
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── configuration ─────────────────────────────────────────────────────────────

def config_path(db_path) -> Path:
    return Path(db_path).parent / 'mobile.json'


def load_config(db_path) -> dict | None:
    """`{"enabled": true, "host": "<mac>.<tailnet>.ts.net", "port": 8770}` or None (gateway off)."""
    try:
        return valid_config(json.loads(config_path(db_path).read_text()))
    except (OSError, ValueError, AttributeError):
        return None


def valid_config(data: dict) -> dict | None:
    host, port = data.get('host'), data.get('port', 8770)
    if (data.get('enabled') is not True or not isinstance(host, str) or not host.endswith('.ts.net')
            or not re.fullmatch(r'[a-z0-9.-]{1,253}', host) or not isinstance(port, int) or isinstance(port, bool)
            or not 1024 < port < 65536 or port in (8765, 8766)):
        return None
    return {'enabled': True, 'host': host, 'port': port}


def save_config(db_path, enabled: bool, host: str, port: int = 8770) -> dict:
    data = {'enabled': bool(enabled), 'host': (host or '').strip().lower(), 'port': port}
    if enabled and valid_config(data) is None:
        raise ValueError("host must be the Mac's MagicDNS name (...ts.net); port must be free and not 8765/8766")
    config_path(db_path).write_text(json.dumps(data, indent=1))
    return data


# ── pairing, devices, audit ───────────────────────────────────────────────────

def create_pairing(conn) -> dict:
    """A new 6-digit code (5 minutes, single use). Earlier unused codes stop working."""
    code = f'{secrets.randbelow(10 ** 6):06d}'
    now = _now()
    with store.transaction(conn):
        conn.execute("UPDATE pairing_codes SET used_at=? WHERE used_at IS NULL", (now.isoformat(),))
        conn.execute("INSERT INTO pairing_codes(id, code_hash, created_at, expires_at) VALUES (?,?,?,?)",
                     (uuid4().hex, _hash(code), now.isoformat(), (now + PAIR_TTL).isoformat()))
    return {'code': code, 'expires_at': (now + PAIR_TTL).isoformat()}


class PairingError(Exception):
    pass


def pair(conn, code: str, name: str, tailscale_login: str | None) -> dict:
    """Exchange a valid code for a device token (returned once; only its hash is stored)."""
    if not tailscale_login:
        raise PairingError('tailscale_identity_missing')
    if len(tailscale_login) > 200:                              # refused, never cut: a cut login could match another
        raise PairingError('tailscale_login_too_long')
    now = _now()
    refused = result = None
    # One write transaction (BEGIN IMMEDIATE): a code is checked and consumed atomically, so two phones can never
    # both use it. A refusal is raised only after commit, so a wrong try is counted (not rolled back).
    with store.transaction(conn):
        row = conn.execute("SELECT * FROM pairing_codes WHERE used_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None or datetime.fromisoformat(row['expires_at']) < now:
            refused = 'no_active_code'
        elif not (isinstance(code, str) and secrets.compare_digest(_hash(code), row['code_hash'])):
            attempts = row['attempts'] + 1
            conn.execute("UPDATE pairing_codes SET attempts=?, used_at=? WHERE id=?",
                         (attempts, now.isoformat() if attempts >= PAIR_MAX_ATTEMPTS else None, row['id']))
            refused = 'wrong_code'
        else:
            conn.execute("UPDATE pairing_codes SET used_at=? WHERE id=?", (now.isoformat(), row['id']))
            token = secrets.token_urlsafe(32)                   # 256 bits
            device = {'id': 'dev_' + uuid4().hex[:12], 'name': (name or 'Telefon').strip()[:60] or 'Telefon',
                      'scopes': ' '.join(DEFAULT_SCOPES), 'tailscale_login': tailscale_login}
            conn.execute("INSERT INTO mobile_devices(id, name, token_hash, scopes, tailscale_login, created_at) "
                         "VALUES (?,?,?,?,?,?)", (device['id'], device['name'], _hash(token), device['scopes'],
                                                   device['tailscale_login'], now.isoformat()))
            result = {'token': token, 'device_id': device['id'], 'scopes': list(DEFAULT_SCOPES)}
    if refused:
        raise PairingError(refused)
    return result


def device_for_token(conn, token: str):
    row = conn.execute("SELECT * FROM mobile_devices WHERE token_hash=? AND revoked_at IS NULL", (_hash(token),)).fetchone()
    return dict(row) if row else None


def devices(conn) -> list[dict]:
    pushed = {r[0] for r in conn.execute("SELECT device_id FROM push_subscriptions")}   # TB-29
    return [{**{k: r[k] for k in ('id', 'name', 'scopes', 'tailscale_login', 'created_at', 'last_seen_at', 'revoked_at')},
             'push': r['id'] in pushed}
            for r in conn.execute("SELECT * FROM mobile_devices ORDER BY created_at DESC")]


def revoke(conn, device_id: str) -> bool:
    with store.transaction(conn):
        changed = conn.execute("UPDATE mobile_devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                               (_now().isoformat(), device_id)).rowcount
        conn.execute("DELETE FROM push_subscriptions WHERE device_id=?", (device_id,))   # TB-29
    if changed:
        _signal_revoked(device_id)                               # open streams of this device end now
    return bool(changed)


def set_scopes(conn, device_id: str, scopes: list[str]) -> list[str] | None:
    """K-027/4 (Mac only): `view` always, the rest from SCOPES; recorded in the audit log."""
    wanted = [s for s in SCOPES if s in set(scopes) | {'view'}]
    if set(scopes) - set(SCOPES):
        raise ValueError('unknown scope')
    with store.transaction(conn):
        if not conn.execute("UPDATE mobile_devices SET scopes=? WHERE id=? AND revoked_at IS NULL",
                            (' '.join(wanted), device_id)).rowcount:
            return None
        conn.execute("INSERT INTO mobile_audit(at, device_id, method, path, status, outcome) VALUES (?,?,?,?,?,?)",
                     (_now().isoformat(), device_id, 'PUT', f'/api/mobile/devices/{device_id}/scopes', 200,
                      'scopes:' + ' '.join(wanted)))
    return wanted


# TB-24 follow-up (Argon): work given from a phone shows up on the Mac at once, so a lost phone is noticed.
# Fixed text only: nothing the phone sent reaches osascript. Fire and forget; a failure never touches the request.
NOTICE = {'work': 'Telefondan yeni çalışma başlatıldı', 'message': 'Telefondan yeni mesaj gönderildi'}


def notify_mac(kind: str) -> None:
    if sys.platform != 'darwin':
        return
    try:
        proc = subprocess.Popen(['/usr/bin/osascript', '-e', f'display notification "{NOTICE[kind]}" with title "PromptBridge"'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return
    threading.Thread(target=proc.wait, daemon=True).start()   # reaped off the request path: no zombie


def audit(conn, device_id, method, path, status, outcome):
    with store.transaction(conn):
        conn.execute("INSERT INTO mobile_audit(at, device_id, method, path, status, outcome) VALUES (?,?,?,?,?,?)",
                     (_now().isoformat(), device_id, method, path[:300], status, outcome))


def recent_audit(conn, limit=50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM mobile_audit ORDER BY id DESC LIMIT ?", (limit,))]


# Open gateway requests per device: (event, the loop that request runs on). revoke() may run in a worker
# thread, so it reaches each request's loop with call_soon_threadsafe.
_streams: dict[str, set] = defaultdict(set)


def _watch(device_id: str):
    entry = (asyncio.Event(), asyncio.get_running_loop())
    _streams[device_id].add(entry)
    return entry


def _signal_revoked(device_id: str):
    for entry in list(_streams.get(device_id, ())):
        event, loop = entry
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:                                     # that request's loop is gone
            _streams[device_id].discard(entry)


# ── the guard ─────────────────────────────────────────────────────────────────

def _route(method: str, path: str):
    for m, pattern, scope in _ALLOW:
        if m == method and pattern.fullmatch(path):
            return True, scope
    return False, None


class _Limiter:
    def __init__(self):
        self.hits = defaultdict(deque)

    def allow(self, key, per_minute) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            return False
        q.append(now)
        return True


class MobileGuard:
    """ASGI wrapper for the gateway listener (see module docstring). Order: Host → path shape → allow-list →
    (pairing) → no cookies → token → rate → Tailscale user → scope → forbidden query → trust mark → revocation
    re-check → audit (also when the app raises)."""

    def __init__(self, app, db_path, host: str):
        self.app, self.db_path, self.host = app, str(db_path), host
        self.limits = _Limiter()

    async def _reject(self, send, status, code, conn=None, device=None, method='', path=''):
        if conn is not None:
            audit(conn, device, method, path, status, code)
        body = json.dumps({'detail': code, 'code': code}).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)          # the main listener owns the lifespan
        if scope['type'] != 'http':
            return await self._reject(send, 404, 'not_found')    # no websockets on the gateway
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        method, path = scope['method'], scope['path']
        if headers.get(b'host', b'').decode('latin-1').split(':')[0].lower() != self.host:
            return await self._reject(send, 421, 'wrong_host')
        segments = path.split('/')                               # '/ui/' → ['', 'ui', '']: the trailing '/' stays valid
        if '\\' in path or any(s in ('.', '..') for s in segments) or '' in segments[1:-1]:
            return await self._reject(send, 404, 'not_found')    # no dot segments, backslashes or '//' on the gateway
        allowed, needed = _route(method, path)
        if not allowed:
            return await self._reject(send, 404, 'not_found')
        state = scope.setdefault('state', {})
        state[GATEWAY_MARK] = True
        login = headers.get(LOGIN_HEADER, b'').decode('utf-8', 'replace') or None
        state['conductor_tailscale_login'] = login
        if needed is None:                                       # static panel files and the pairing call
            # per Tailscale user: one tailnet member cannot lock out another member's pairing
            if path == '/api/mobile/pair' and not self.limits.allow('pair:' + (login or '-'), PAIR_RATE):
                return await self._reject(send, 429, 'rate_limited')
            return await self.app(scope, receive, send)
        conn = store.connect(self.db_path)
        try:
            auth = headers.get(b'authorization', b'').decode('latin-1')
            device = device_for_token(conn, auth[7:]) if auth.startswith('Bearer ') and len(auth) > 7 else None
            if device is not None and not self.limits.allow(device['id'], DEVICE_RATE):   # before every refusal
                return await self._reject(send, 429, 'rate_limited', conn, device['id'], method, path)
            if b'cookie' in headers or device is None:
                if device is None and not self.limits.allow('anonymous', DEVICE_RATE):   # bounds audit rows from unknown callers
                    return await self._reject(send, 429, 'rate_limited')
                code = 'cookies_not_accepted' if b'cookie' in headers else 'unauthenticated'
                return await self._reject(send, 401, code, conn, None, method, path)
            if login != device['tailscale_login']:
                return await self._reject(send, 403, 'tailscale_user_mismatch', conn, device['id'], method, path)
            if needed not in device['scopes'].split():
                return await self._reject(send, 403, 'scope_denied', conn, device['id'], method, path)
            query = parse_qsl(scope.get('query_string', b'').decode('latin-1'), keep_blank_values=True)
            if {k for k, _ in query} & set(_NOT_ON_GATEWAY_QUERY.get(path, ())):
                return await self._reject(send, 403, 'query_not_allowed', conn, device['id'], method, path)
            with store.transaction(conn):
                conn.execute("UPDATE mobile_devices SET last_seen_at=? WHERE id=?", (_now().isoformat(), device['id']))
            state[DEVICE_MARK] = device['id']                    # trusted by mutation_guard; never set on 8765
            state['conductor_mobile_scopes'] = device['scopes'].split()
            status = {}
            watch = _watch(device['id'])
            # revocation race: a revoke() that committed after device_for_token() signalled before this request was
            # watched, so it is read again now that the watch is registered (a later revoke sets the event)
            row = conn.execute("SELECT revoked_at FROM mobile_devices WHERE id=?", (device['id'],)).fetchone()
            if row is None or row['revoked_at'] is not None:
                _streams[device['id']].discard(watch)
                return await self._reject(send, 401, 'device_revoked', conn, device['id'], method, path)
            revoked = watch[0]
            inflight = None                                      # the one receive() in progress for this request

            async def guarded_receive():
                # A revocation ends an open stream: the app sees the client disconnect. Starlette's
                # is_disconnected() calls this from an already cancelled scope, so the in-flight receive() is kept
                # for the next call (no task leaks, no message is lost); only this call's waiter is cancelled.
                nonlocal inflight
                if revoked.is_set():
                    return {'type': 'http.disconnect'}
                getter = inflight = inflight or asyncio.ensure_future(receive())
                if not getter.done():
                    waiter = asyncio.ensure_future(revoked.wait())
                    try:
                        await asyncio.wait({waiter, getter}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        waiter.cancel()
                    if not getter.done():                        # revoked while waiting
                        return {'type': 'http.disconnect'}
                if inflight is getter:
                    inflight = None
                return getter.result()

            async def recording_send(message):
                if message['type'] == 'http.response.start':
                    status['code'] = message['status']
                await send(message)

            try:
                await self.app(scope, guarded_receive, recording_send)
            finally:
                _streams[device['id']].discard(watch)
                if inflight is not None:
                    inflight.cancel()
                if method != 'GET':                              # written even when the app raised (no status: 500)
                    audit(conn, device['id'], method, path, status.get('code', 500), 'forwarded')
            if method == 'POST' and 200 <= status.get('code', 0) < 300 and needed == 'command':
                kind = 'work' if path == '/api/works' else 'message' if path.endswith('/messages') else None
                if kind:
                    notify_mac(kind)
        finally:
            conn.close()
