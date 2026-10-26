"""TB-29 (K-036): notifications on the paired phone through Web Push.

The phone's browser makes a push subscription; the Mac sends each notice *outwards* to that browser's push service
(Google FCM for Android Chrome), end-to-end encrypted (RFC 8291) with this Mac's VAPID key, so it arrives with
Tailscale off and Conductor still listens on loopback only. Only well-known push services are accepted as endpoints
(the phone cannot make the Mac call an arbitrary URL). Sending runs on a background thread with a timeout and never
touches the run that triggered it; a subscription the service reports gone (404/410) is dropped."""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

HOSTS = ('fcm.googleapis.com', 'updates.push.services.mozilla.com')
HOST_SUFFIXES = ('.push.apple.com', '.notify.windows.com')
TIMEOUT_S = 10
TTL_S = 6 * 3600
CLAIM = {'sub': 'mailto:conductor@localhost'}
# What a run's status means to someone away from the Mac. A run the user stopped is not worth a notice.
STATUS_TEXT = {
    'SUCCEEDED': 'Bitti', 'FAILED': 'Başarısız oldu', 'RECOVERY_REQUIRED': 'Kurtarma gerekiyor',
    'WAITING_CAPACITY': 'Kota doldu, bekliyor', 'WAITING_AUTH': 'Sağlayıcı oturumu gerekiyor',
    'WAITING_APPROVAL': 'Onayını bekliyor', 'WAITING_CONTEXT': 'İnceleme gerekiyor',
}


def valid_endpoint(url: str) -> bool:
    # Whitespace, control characters and backslashes are refused outright: URL parsers disagree on them.
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f or c == '\\' for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port   # raises ValueError for a non-numeric or out-of-range port
        if parts.geturl() != url:   # what is stored is exactly what gets called
            return False
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    return (parts.scheme == 'https' and not parts.username and not parts.password and port in (None, 443)
            and (host in HOSTS or any(host.endswith(s) for s in HOST_SUFFIXES)))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def key_path(db_path) -> Path:
    return Path(db_path).parent / 'push_vapid.pem'


_KEY_LOCK = threading.Lock()


def _vapid(db_path):
    """This Mac's VAPID key, made once (0600). Only its public half ever leaves the Mac."""
    from py_vapid import Vapid
    path = key_path(db_path)
    with _KEY_LOCK:
        if not path.exists():
            v = Vapid()
            v.generate_keys()
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.vapid-', suffix='.tmp')   # created 0600
            os.close(fd)
            v.save_key(tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
    return Vapid.from_file(str(path))


def public_key(db_path) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return _b64(_vapid(db_path).public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def subscribe(conn, device_id: str, endpoint: str, p256dh: str, auth: str) -> bool:
    """Write (or replace) the device's subscription, only while the device is not revoked; whether a row was
    written. The check sits in the same statement, so a revoke cannot slip in between check and write."""
    from app.store import transaction
    with transaction(conn):
        # The WHERE clause also keeps SQLite from reading ON CONFLICT as a join constraint (upsert parsing rule).
        cur = conn.execute("INSERT INTO push_subscriptions(device_id, endpoint, p256dh, auth, created_at) "
                           "SELECT ?,?,?,?,? WHERE EXISTS "
                           "(SELECT 1 FROM mobile_devices WHERE id=? AND revoked_at IS NULL) "
                           "ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint, p256dh=excluded.p256dh, "
                           "auth=excluded.auth, created_at=excluded.created_at",
                           (device_id, endpoint, p256dh, auth, datetime.now(timezone.utc).isoformat(), device_id))
        return cur.rowcount > 0


def unsubscribe(conn, device_id: str) -> None:
    from app.store import transaction
    with transaction(conn):
        conn.execute("DELETE FROM push_subscriptions WHERE device_id=?", (device_id,))


def subscribed(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT device_id FROM push_subscriptions")}


# ── sending ───────────────────────────────────────────────────────────────────

def _send(db_path, sub: dict, payload: dict) -> int | None:
    """One encrypted push; the HTTP status (None when it could not be sent). Patched in tests."""
    import requests
    from pywebpush import WebPushException, webpush
    session = requests.Session()
    session.max_redirects = 0   # a push service answers itself; a redirect is never followed (defence in depth)
    try:
        r = webpush({'endpoint': sub['endpoint'], 'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}},
                    data=json.dumps(payload, ensure_ascii=False), vapid_private_key=str(key_path(db_path)),
                    vapid_claims=dict(CLAIM), timeout=TIMEOUT_S, ttl=TTL_S, requests_session=session)
        return getattr(r, 'status_code', 201)
    except WebPushException as exc:
        return getattr(exc.response, 'status_code', None)
    except Exception:   # network, DNS, timeout: a lost notice never breaks anything else
        log.warning('push: send failed', exc_info=True)
        return None


def _deliver(db_path, payload: dict, device_id: str | None = None) -> None:
    from app import store
    conn = store.connect(db_path)   # K-006: this thread's own connection, the project's settings
    try:
        sql = ("SELECT s.* FROM push_subscriptions s JOIN mobile_devices d ON d.id=s.device_id "
               "WHERE d.revoked_at IS NULL" + (" AND s.device_id=?" if device_id else ""))
        subs = [dict(r) for r in conn.execute(sql, (device_id,) if device_id else ())]
        if not subs:
            return
        _vapid(db_path)
        for sub in subs:
            if not valid_endpoint(sub['endpoint']):
                continue
            status = _send(db_path, sub, payload)
            if status in (404, 410):   # the browser dropped this subscription
                with store.transaction(conn):
                    conn.execute("DELETE FROM push_subscriptions WHERE device_id=? AND endpoint=?",
                                 (sub['device_id'], sub['endpoint']))
    except sqlite3.Error:
        log.warning('push: database not ready', exc_info=True)
    finally:
        conn.close()


def notify(db_path, title: str, body: str, url: str = '/ui/', tag: str | None = None, *, device_id: str | None = None,
           wait: bool = False) -> threading.Thread:
    payload = {'title': title[:120], 'body': body[:240], 'url': url, 'tag': tag or url}
    t = threading.Thread(target=_deliver, args=(str(db_path), payload, device_id), name='conductor-push', daemon=True)
    t.start()
    if wait:
        t.join()
    return t


def _db_of(conn) -> str | None:
    row = conn.execute('PRAGMA database_list').fetchone()
    return row[2] if row and row[2] else None


def _anyone(conn) -> bool:
    try:
        return conn.execute('SELECT 1 FROM push_subscriptions LIMIT 1').fetchone() is not None
    except sqlite3.Error:   # a database from before schema 1.12 (or a bare test one)
        return False


COMMIT_WAIT_S = 5


def on_session_status(conn, session_id: str, status: str) -> None:
    """Called after every run status write (runner_store.update_session_status): one cheap query when nobody
    subscribed, otherwise a background thread. The write may sit inside a caller's still-open transaction, so the
    thread reads the status back on its own connection and sends only once it is really committed (a rolled-back
    write never sends; agy TB-29 P2)."""
    if status not in STATUS_TEXT or not _anyone(conn):
        return
    db = _db_of(conn)
    if db:
        threading.Thread(target=_session_notice, args=(db, session_id, status), name='conductor-push', daemon=True).start()


def _session_notice(db_path, session_id: str, status: str) -> None:
    from app import store
    conn = store.connect(db_path)
    try:
        deadline = time.monotonic() + COMMIT_WAIT_S
        while True:
            row = conn.execute("SELECT s.status, w.id, w.title FROM sessions s LEFT JOIN work_runs r ON r.session_id=s.id "
                               "LEFT JOIN works w ON w.id=r.work_id WHERE s.id=?", (session_id,)).fetchone()
            if row and row[0] == status:
                break
            if time.monotonic() > deadline:
                return
            time.sleep(0.2)
    except sqlite3.Error:
        return
    finally:
        conn.close()
    title, url = (row[2], f'/ui/#/works/{row[1]}') if row[1] else ('Yürütme', f'/ui/#/sessions/{session_id}')
    _deliver(db_path, {'title': title[:120], 'body': STATUS_TEXT[status], 'url': url, 'tag': f'run:{session_id}'})


def on_envelope(conn, envelope_id: str) -> None:
    if not _anyone(conn):
        return
    row = conn.execute("SELECT title, origin_ref FROM envelopes WHERE id=?", (envelope_id,)).fetchone()
    name = (row[0] or Path(row[1] or envelope_id).name) if row else envelope_id
    db = _db_of(conn)
    if db:
        notify(db, 'Dosya onayı bekliyor', name, '/ui/', tag=f'envelope:{envelope_id}')


# ── API (phone only: reached through the mobile gateway with the device's own token) ─────────────────────────────
from fastapi import APIRouter, Depends, HTTPException, Request   # noqa: E402
from pydantic import BaseModel, ConfigDict, Field   # noqa: E402

from app.security import mutation_guard   # noqa: E402

router = APIRouter(prefix='/api/push', dependencies=[Depends(mutation_guard)])
_last_test: dict[str, float] = {}
TEST_EVERY_S = 30


def _device(request: Request) -> str:
    device = getattr(request.state, 'conductor_mobile_device', None)
    if not device:   # the Mac panel has its own notices; a subscription belongs to a paired phone
        raise HTTPException(404, {'code': 'phone_only', 'detail': 'Bildirimler eşleşmiş telefon içindir'})
    return device


class Keys(BaseModel):
    model_config = ConfigDict(extra='forbid')
    p256dh: str = Field(min_length=20, max_length=200, pattern=r'^[A-Za-z0-9_-]+=*$')
    auth: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+=*$')


class Subscription(BaseModel):
    model_config = ConfigDict(extra='ignore')   # browsers add expirationTime
    endpoint: str = Field(min_length=10, max_length=1000)
    keys: Keys


def _with_conn(request, fn, *args):
    from app import store
    conn = store.connect(request.app.state.runner_db)
    try:
        return fn(conn, *args)
    finally:
        conn.close()


@router.get('/key')
def get_key(request: Request):
    _device(request)
    return {'key': public_key(request.app.state.runner_db)}


@router.get('/subscription')
def get_subscription(request: Request):
    device = _device(request)
    return {'subscribed': _with_conn(request, lambda c: device in subscribed(c))}


@router.put('/subscription')
def put_subscription(body: Subscription, request: Request):
    device = _device(request)
    if not valid_endpoint(body.endpoint):
        raise HTTPException(422, {'code': 'push_service', 'detail': 'Tanınmayan bildirim servisi'})
    _with_conn(request, subscribe, device, body.endpoint, body.keys.p256dh, body.keys.auth)
    return {'subscribed': True}


@router.delete('/subscription')
def delete_subscription(request: Request):
    device = _device(request)
    _with_conn(request, unsubscribe, device)
    return {'subscribed': False}


@router.post('/test')
def post_test(request: Request):
    import time
    device = _device(request)
    now = time.monotonic()
    if now - _last_test.get(device, -TEST_EVERY_S) < TEST_EVERY_S:
        raise HTTPException(429, {'code': 'rate_limited', 'detail': 'Deneme bildirimi 30 saniyede bir'})
    _last_test[device] = now
    notify(request.app.state.runner_db, 'PromptBridge', 'Bildirimler çalışıyor.', '/ui/', tag='test', device_id=device)
    return {'sent': True}
