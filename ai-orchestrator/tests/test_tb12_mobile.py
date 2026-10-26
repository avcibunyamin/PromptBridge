"""TB-12 (K-024): the mobile gateway (MobileGuard), pairing, devices, audit and schema 1.10.

The gateway is exercised as an ASGI wrapper around the real app (no Tailscale, no network)."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import mobile, store

HOST = 'mac.tail1234.ts.net'
LOGIN = 'operator@example.com'


class Schema(unittest.TestCase):
    def test_1_9_database_migrates_to_1_10_losslessly(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / 'state.db'
        conn = store.connect(db); self.addCleanup(conn.close); store.migrate(conn)
        with store.transaction(conn):                              # make it a 1.9 database with data
            for t in ('mobile_devices', 'pairing_codes', 'mobile_audit'):
                conn.execute(f'DROP TABLE {t}')
            conn.execute("UPDATE schema_meta SET value='1.9' WHERE key='schema_version'")
            conn.execute("INSERT INTO preview_approvals VALUES ('/w', 'd', '{}', 'now', 'human')")
        store.migrate(conn); store.migrate(conn)
        self.assertEqual(conn.execute("SELECT value FROM schema_meta").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT count(*) FROM preview_approvals").fetchone()[0], 1)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'mobile_devices', 'pairing_codes', 'mobile_audit'} <= tables)
        mobile.audit(conn, None, 'GET', '/x', 401, 'unauthenticated')
        for sql in ("UPDATE mobile_audit SET outcome='x'", "DELETE FROM mobile_audit"):
            with self.subTest(sql), self.assertRaises(sqlite3.DatabaseError):
                with store.transaction(conn):
                    conn.execute(sql)


class Config(unittest.TestCase):
    def test_gateway_off_unless_explicitly_enabled(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / 'state.db'
        self.assertIsNone(mobile.load_config(db))                   # no file: off
        mobile.save_config(db, False, '')
        self.assertIsNone(mobile.load_config(db))
        for host, port in (('example.com', 8770), ('mac.ts.net', 8765), ('mac.ts.net', 8766), ('MAC.ts.net!', 8770)):
            with self.subTest(host=host, port=port), self.assertRaises(ValueError):
                mobile.save_config(db, True, host, port)
        mobile.save_config(db, True, HOST)
        self.assertEqual(mobile.load_config(db), {'enabled': True, 'host': HOST, 'port': 8770})


class Gateway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); (root / '.system').mkdir()
        static = root / 'static'; (static / 'assets').mkdir(parents=True)
        (static / 'index.html').write_text('<html>panel</html>'); (static / 'assets' / 'index-x.js').write_text('js')
        (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        patcher = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root)}); patcher.start(); self.addCleanup(patcher.stop)
        from app.api import create_app
        self.db = str(root / '.system/state.db')
        self.app = create_app(static_dir=static)
        self.local = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.local.__enter__(); self.addCleanup(self.local.__exit__, None, None, None)
        self.app.state.runner_db = self.db
        self.gw = TestClient(mobile.MobileGuard(self.app, self.db, HOST), base_url=f'https://{HOST}')
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close)

    def pair(self, login=LOGIN):
        code = self.local.post('/api/mobile/pairing').json()['code']
        r = self.gw.post('/api/mobile/pair', json={'code': code, 'device_name': 'Pixel'},
                         headers={'Tailscale-User-Login': login} if login else {})
        return r

    def auth(self, token, login=LOGIN, **extra):
        return {'Authorization': f'Bearer {token}', 'Tailscale-User-Login': login, **extra}

    def test_host_and_allow_list(self):
        self.assertEqual(TestClient(mobile.MobileGuard(self.app, self.db, HOST), base_url='https://evil.ts.net').get('/ui/').status_code, 421)
        self.assertEqual(self.gw.get('/ui/').status_code, 200)                  # panel shell needs no token (pair screen)
        self.assertEqual(self.gw.get('/assets/index-x.js').status_code, 200)
        for method, path in (('GET', '/docs'),   # POST /api/works and the team preview: `command` (K-033, TB-24 tests) ('GET', '/api/mobile/devices'),
                             ('POST', '/api/mobile/pairing'), ('GET', '/api/mobile/status'),
                             ('POST', '/api/sessions/catalog/refresh'), ('POST', '/api/envelopes/e/edit'),
                             ('POST', '/api/previews/start'),
                             ('GET', '/api/previews/status')):                 # reads any path; no preview on a phone
            with self.subTest(path=path):
                self.assertEqual(self.gw.request(method, path).status_code, 404)

    def test_pairing_rules(self):
        self.assertEqual(self.pair(login=None).json()['code'], 'tailscale_identity_missing')
        code = self.local.post('/api/mobile/pairing').json()['code']
        wrong = '000000' if code != '000000' else '111111'
        before = [dict(r) for r in self.conn.execute('SELECT * FROM pairing_codes')]
        cross = self.gw.post('/api/mobile/pair', json={'code': wrong},
                             headers={'Tailscale-User-Login': LOGIN, 'sec-fetch-site': 'cross-site'})
        self.assertEqual((cross.status_code, cross.json()['code']), (403, 'untrusted_origin'))
        self.assertEqual([dict(r) for r in self.conn.execute('SELECT * FROM pairing_codes')], before)
        for _ in range(mobile.PAIR_MAX_ATTEMPTS):
            self.assertEqual(self.gw.post('/api/mobile/pair', json={'code': wrong}, headers={'Tailscale-User-Login': LOGIN}).status_code, 403)
        r = self.gw.post('/api/mobile/pair', json={'code': code}, headers={'Tailscale-User-Login': LOGIN})
        self.assertEqual(r.json()['code'], 'no_active_code')                   # burned after 5 wrong tries
        ok = self.pair()
        self.assertEqual(ok.status_code, 200)
        token = ok.json()['token']
        self.assertEqual(ok.json()['scopes'], ['view', 'approve'])
        stored = self.conn.execute('SELECT token_hash FROM mobile_devices').fetchall()
        self.assertNotIn(token, json.dumps([tuple(r) for r in stored]))         # only the hash is kept
        same_code = self.local.post('/api/mobile/pairing').json()['code']
        same = self.gw.post('/api/mobile/pair', json={'code': same_code, 'device_name': 'Pixel'},
                            headers={'Tailscale-User-Login': LOGIN, 'Origin': f'https://{HOST}', 'sec-fetch-site': 'same-origin'})
        self.assertEqual(same.status_code, 200)
        # behind `tailscale serve` the gateway sees the local http hop while the browser's origin is https
        hop = TestClient(mobile.MobileGuard(self.app, self.db, HOST), base_url=f'http://{HOST}:8770')
        hop_code = self.local.post('/api/mobile/pairing').json()['code']
        behind = hop.post('/api/mobile/pair', json={'code': hop_code, 'device_name': 'Pixel'},
                          headers={'Tailscale-User-Login': LOGIN, 'Origin': f'https://{HOST}', 'sec-fetch-site': 'same-origin'})
        self.assertEqual(behind.status_code, 200)
        evil = hop.post('/api/mobile/pair', json={'code': '000000'},
                        headers={'Tailscale-User-Login': LOGIN, 'Origin': 'https://evil.example'})
        self.assertEqual((evil.status_code, evil.json()['code']), (403, 'untrusted_origin'))
        self.assertEqual(self.local.post('/api/mobile/pair', json={'code': '123456'}).status_code, 404)   # not on 8765
        self.assertEqual(self.local.post('/api/mobile/pair', json={'code': '123456'},
                                         headers={'sec-fetch-site': 'cross-site'}).status_code, 404)

    def test_replayed_or_expired_code_is_refused(self):
        code = self.local.post('/api/mobile/pairing').json()['code']
        h = {'Tailscale-User-Login': LOGIN}
        self.assertEqual(self.gw.post('/api/mobile/pair', json={'code': code}, headers=h).status_code, 200)
        self.assertEqual(self.gw.post('/api/mobile/pair', json={'code': code}, headers=h).status_code, 403)
        code = self.local.post('/api/mobile/pairing').json()['code']
        with store.transaction(self.conn):
            self.conn.execute("UPDATE pairing_codes SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.gw.post('/api/mobile/pair', json={'code': code}, headers=h).json()['code'], 'no_active_code')

    def test_token_user_scope_and_trust_mark(self):
        token = self.pair().json()['token']
        self.assertEqual(self.gw.get('/api/works').status_code, 401)
        self.assertEqual(self.gw.get('/api/works', headers=self.auth('nope')).status_code, 401)
        self.assertEqual(self.gw.get('/api/works', headers=self.auth(token, Cookie='a=b')).status_code, 401)
        self.assertEqual(self.gw.get('/api/works', headers=self.auth(token, login='other@example.com')).status_code, 403)
        self.assertEqual(self.gw.get('/api/works', headers=self.auth(token)).status_code, 200)
        self.assertEqual(self.gw.get('/api/mobile/me', headers=self.auth(token)).json()['scopes'], ['view', 'approve'])
        self.assertEqual(self.local.get('/api/mobile/me').status_code, 404)    # how the panel knows it is local
        for query in ('check_auth=true', 'check%5Fauth=true', 'x=1&check_auth=', 'check_auth'):  # as FastAPI decodes it
            with self.subTest(query=query):
                self.assertEqual(self.gw.get(f'/api/sessions/providers?{query}', headers=self.auth(token)).status_code, 403)
        # approve passes mutation_guard through the server-side mark (404: no such envelope, not 403 origin)
        r = self.gw.post('/api/envelopes/missing/approve', json={}, headers=self.auth(token, Origin=f'https://{HOST}'))
        self.assertEqual(r.status_code, 404)
        # the same mutation on 8765 with the phone's origin is still refused, a header cannot fake the mark
        r = self.local.post('/api/envelopes/missing/approve', json={},
                            headers={'Origin': f'https://{HOST}', 'conductor_mobile_device': 'dev_x'})
        self.assertEqual(r.status_code, 403)
        with store.transaction(self.conn):
            self.conn.execute("UPDATE mobile_devices SET scopes='view'")
        self.assertEqual(self.gw.post('/api/envelopes/missing/approve', json={}, headers=self.auth(token)).status_code, 403)

    def test_rate_limit_revoke_and_audit(self):
        token = self.pair().json()['token']
        with patch.object(mobile, 'DEVICE_RATE', 3):
            codes = [self.gw.get('/api/works', headers=self.auth(token)).status_code for _ in range(4)]
            anonymous = [self.gw.get('/api/works', headers={'Cookie': 'a=b'} if i % 2 else {}).status_code for i in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        self.assertEqual(anonymous, [401, 401, 401, 429])                  # unknown callers cannot flood the audit
        audited = self.conn.execute("SELECT count(*) FROM mobile_audit WHERE device_id IS NULL").fetchone()[0]
        self.assertEqual(audited, 3)
        device = self.local.get('/api/mobile/status').json()['devices'][0]['id']
        self.assertEqual(self.local.delete(f'/api/mobile/devices/{device}').status_code, 200)
        self.assertEqual(self.gw.get('/api/works', headers=self.auth(token)).status_code, 401)
        outcomes = [a['outcome'] for a in self.local.get('/api/mobile/status').json()['audit']]
        self.assertIn('paired', outcomes); self.assertIn('rate_limited', outcomes); self.assertIn('unauthenticated', outcomes)
        cross = self.local.delete(f'/api/mobile/devices/{device}', headers={'Origin': 'https://evil.example'})
        self.assertEqual(cross.status_code, 403)                                # device admin keeps mutation_guard

    def raw(self, path, host=HOST):
        """The guard with a raw ASGI scope: the HTTP client would normalise `..` and `//` before sending.
        Only for requests the guard refuses itself (the app is never reached here)."""
        sent = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'method': 'GET', 'path': path, 'query_string': b'', 'headers': [(b'host', host.encode())]}
        asyncio.run(mobile.MobileGuard(self.app, self.db, HOST)(scope, receive, send))
        return sent[0]['status'], json.loads(sent[1]['body'])['code']

    def test_dot_segments_backslashes_and_empty_segments_are_404(self):
        for path in ('/ui/../api/mobile/devices', '/ui/..', '/ui/./index.html', '/assets/../index.html', '/ui/.',
                     '/ui//index.html', '//ui/', '/ui/x//', '/ui\\..\\api', '/assets/a\\b', '/api/works/../works'):
            with self.subTest(path=path):
                self.assertEqual(self.raw(path), (404, 'not_found'))
        self.assertEqual(self.raw('/ui/../x', host='evil.ts.net'), (421, 'wrong_host'))   # Host is still checked first
        self.assertEqual(self.gw.get('/ui/').status_code, 200)                  # the trailing '/' stays valid

    def test_refused_requests_count_against_the_device_rate(self):
        token = self.pair().json()['token']
        with patch.object(mobile, 'DEVICE_RATE', 3):
            codes = [self.gw.get('/api/works', headers=self.auth(token, login='other@example.com')).status_code
                     for _ in range(5)]
        self.assertEqual(codes, [403, 403, 403, 429, 429])
        mismatches = self.conn.execute(
            "SELECT count(*) FROM mobile_audit WHERE outcome='tailscale_user_mismatch'").fetchone()[0]
        self.assertEqual(mismatches, 3)
        fresh = self.pair().json()['token']                                     # a fresh device for the cookie case
        with patch.object(mobile, 'DEVICE_RATE', 3):
            cookie = [self.gw.get('/api/works', headers=self.auth(fresh, Cookie='a=b')).status_code
                      for _ in range(4)]
        self.assertEqual(cookie, [401, 401, 401, 429])                        # a valid token with a cookie counts too

    def test_revocation_between_token_check_and_watch_is_refused(self):
        token = self.pair().json()['token']
        device = self.conn.execute('SELECT id FROM mobile_devices').fetchone()[0]
        real_watch = mobile._watch

        def racing_watch(device_id):                     # revoke() lands after device_for_token(), before the watch
            c = store.connect(self.db)
            try:
                mobile.revoke(c, device_id)
            finally:
                c.close()
            return real_watch(device_id)
        with patch.object(mobile, '_watch', racing_watch):
            r = self.gw.get('/api/works', headers=self.auth(token))
        self.assertEqual((r.status_code, r.json()['code']), (401, 'device_revoked'))
        self.assertEqual(mobile._streams.get(device, set()), set())             # the watch was removed
        outcomes = [row[0] for row in self.conn.execute('SELECT outcome FROM mobile_audit WHERE device_id=?', (device,))]
        self.assertIn('device_revoked', outcomes)
        self.assertEqual(self.gw.get('/api/events', headers=self.auth(token)).status_code, 401)   # same token later

    def test_audit_row_is_written_when_the_app_raises(self):
        token = self.pair().json()['token']

        async def boom(scope, receive, send):
            raise RuntimeError('boom')
        gw = TestClient(mobile.MobileGuard(boom, self.db, HOST), base_url=f'https://{HOST}', raise_server_exceptions=False)
        self.assertEqual(gw.post('/api/envelopes/e1/approve', json={}, headers=self.auth(token)).status_code, 500)
        rows = [tuple(r) for r in self.conn.execute(
            "SELECT status, outcome FROM mobile_audit WHERE path='/api/envelopes/e1/approve'")]
        self.assertEqual(rows, [(500, 'forwarded')])

    def test_long_login_is_refused_and_pair_rate_is_per_user(self):
        r = self.pair(login='a' * 195 + '@x.com')                               # 201 characters
        self.assertEqual(r.json()['code'], 'tailscale_login_too_long')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM mobile_devices').fetchone()[0], 0)   # not cut and stored
        code = self.local.post('/api/mobile/pairing').json()['code']
        wrong = '000000' if code != '000000' else '111111'

        def attempt(login):
            return self.gw.post('/api/mobile/pair', json={'code': wrong}, headers={'Tailscale-User-Login': login}).status_code
        with patch.object(mobile, 'PAIR_RATE', 2):
            first = [attempt('a@example.com') for _ in range(3)]
            other = attempt('b@example.com')
        self.assertEqual(first, [403, 403, 429])
        self.assertEqual(other, 403)                                             # not locked out by a@example.com


class StreamRevocation(unittest.IsolatedAsyncioTestCase):
    async def test_revoking_a_device_ends_its_open_stream(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / 'state.db')
        conn = store.connect(db); self.addCleanup(conn.close); store.migrate(conn)
        code = mobile.create_pairing(conn)['code']
        token = mobile.pair(conn, code, 'Pixel', LOGIN)['token']
        device = mobile.devices(conn)[0]['id']
        seen = []

        async def stream_app(scope, receive, send):                    # like an SSE endpoint: runs until disconnect
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            while (await receive())['type'] != 'http.disconnect':
                pass
            seen.append('disconnected')

        guard = mobile.MobileGuard(stream_app, db, HOST)
        never = asyncio.Event()

        async def receive():
            await never.wait()

        async def send(message):
            pass

        scope = {'type': 'http', 'method': 'GET', 'path': '/api/events', 'query_string': b'',
                 'headers': [(b'host', HOST.encode()), (b'authorization', f'Bearer {token}'.encode()),
                             (b'tailscale-user-login', LOGIN.encode())]}
        task = asyncio.create_task(guard(scope, receive, send))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        mobile.revoke(conn, device)
        await asyncio.wait_for(task, 2)
        self.assertEqual(seen, ['disconnected'])


class StarletteStreams(unittest.IsolatedAsyncioTestCase):
    """The real SSE shape: StreamingResponse + `request.is_disconnected()` polling, which Starlette runs from an
    already cancelled scope. ASGI spec 2.3 (this uvicorn) also listens for the disconnect; 2.4 relies on the probe."""

    async def run_stream(self, spec, end):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / 'state.db')
        conn = store.connect(db); self.addCleanup(conn.close); store.migrate(conn)
        token = mobile.pair(conn, mobile.create_pairing(conn)['code'], 'Pixel', LOGIN)['token']
        device = mobile.devices(conn)[0]['id']
        from starlette.requests import Request
        from starlette.responses import StreamingResponse

        async def sse_app(scope, receive, send):
            request = Request(scope, receive)

            async def gen():
                while not await request.is_disconnected():
                    yield b'data: x\n\n'
                    await asyncio.sleep(0.005)
            await StreamingResponse(gen(), media_type='text/event-stream')(scope, receive, send)

        gone = asyncio.Event()

        async def receive():
            await gone.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            pass

        scope = {'type': 'http', 'method': 'GET', 'path': '/api/events', 'query_string': b'', 'asgi': {'spec_version': spec},
                 'headers': [(b'host', HOST.encode()), (b'authorization', f'Bearer {token}'.encode()),
                             (b'tailscale-user-login', LOGIN.encode())]}
        before = asyncio.all_tasks()
        task = asyncio.create_task(mobile.MobileGuard(sse_app, db, HOST)(scope, receive, send))
        await asyncio.sleep(0.3)                                       # ~60 disconnect probes
        self.assertFalse(task.done())
        self.assertLessEqual(len(asyncio.all_tasks() - before), 6)     # probes do not pile up tasks
        gone.set() if end == 'client' else mobile.revoke(conn, device)
        await asyncio.wait_for(task, 2)                                # the stream ends
        await asyncio.sleep(0.05)
        self.assertEqual(asyncio.all_tasks() - before, set())          # nothing left behind

    async def test_streams_end_and_leave_no_tasks(self):
        for spec, end in (('2.3', 'client'), ('2.4', 'client'), ('2.4', 'revoke'), ('2.3', 'revoke')):
            with self.subTest(spec=spec, end=end):
                await self.run_stream(spec, end)


if __name__ == '__main__':
    unittest.main()
