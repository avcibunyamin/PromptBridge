"""TB-37: work soft-delete (schema 1.13, revision CAS, active run guard) and pinned_works settings."""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from starlette.testclient import TestClient

from app import mobile, runner_store as repo, store, user_settings, works_store as ws
from app.api import create_app
from app.provider_policy import DEFAULT, digest, normalize


class TB37DeleteAndPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.system').mkdir()
        self.db = self.root / '.system/state.db'
        (self.root / '.system/providers.yaml').write_text(
            'roles:\n  author: [codex]\n  reviewer: [claude]\nmax_context_continuations: 2\n'
        )
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        scan = patch('app.work_dispatcher.SCAN_SECONDS', 3600)
        scan.start()
        self.addCleanup(scan.stop)
        self.dirs = []

    def folder(self):
        d = self.root / f'ws{len(self.dirs)}'
        d.mkdir()
        self.dirs.append(d)
        return str(d)

    def client(self):
        app = create_app()
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)

        class _NeverRunAdapter:
            async def run(self, *args, **kwargs):
                raise AssertionError('real provider adapter must never run in unit tests')
                yield  # pragma: no cover

        app.state.runner.adapter_factory = lambda _provider: _NeverRunAdapter()
        return c

    def test_1_12_database_migrates_to_1_13_idempotently(self):
        db_112 = self.root / 'legacy_1_12.db'
        conn = store.connect(db_112)
        self.addCleanup(conn.close)
        schema_112 = (Path(store.__file__).with_name('schema.sql').read_text()
                      .replace('  deleted_at TEXT,\n', ''))
        conn.executescript(schema_112)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO schema_meta VALUES ('schema_version', '1.12')")
        ws_dir = self.folder()
        conn.execute(
            "INSERT INTO works(id,workspace_path,title,create_hash,created_at,updated_at) "
            "VALUES ('w_112',?,'legacy','h','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (ws_dir,),
        )
        cols_before = {r[1] for r in conn.execute('PRAGMA table_info(works)')}
        self.assertNotIn('deleted_at', cols_before)

        store.migrate(conn)
        ver1 = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver1, '1.13')
        self.assertEqual(store.SCHEMA_VERSION, '1.13')
        self.assertIn('1.12', store.SUPPORTED_VERSIONS)
        self.assertIn('1.13', store.SUPPORTED_VERSIONS)
        cols_after = {r[1] for r in conn.execute('PRAGMA table_info(works)')}
        self.assertIn('deleted_at', cols_after)
        self.assertIsNone(ws.get_work(conn, 'w_112')['deleted_at'])

        # Second migrate call must be idempotent
        store.migrate(conn)
        ver2 = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(ver2, '1.13')
        self.assertEqual(ws.get_work(conn, 'w_112')['title'], 'legacy')

    def test_deleted_work_excluded_from_list_counts_get_and_dispatch_while_rows_stay(self):
        c = self.client()
        w1 = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'one', 'workspace_path': self.folder()}).json()['id']
        w2 = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'two', 'workspace_path': self.folder()}).json()['id']
        w3 = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'three', 'workspace_path': self.folder()}).json()['id']
        self.assertEqual(c.patch(f'/api/works/{w2}', json={'expected_revision': 0, 'archived': True}).status_code, 200)

        conn = store.connect(self.db)
        self.addCleanup(conn.close)
        policy = normalize(DEFAULT)
        pol_digest = digest(policy)
        msg, _ = ws.add_message(conn, w1, str(uuid4()), 'keep my row', pol_digest, policy, pol_digest)
        events_before = conn.execute('SELECT count(*) FROM work_events WHERE work_id=?', (w1,)).fetchone()[0]
        self.assertGreaterEqual(events_before, 2)
        self.assertIn(w1, ws.works_with_pending(conn))

        before_list = c.get('/api/works').json()
        self.assertEqual(before_list['counts'], {'active': 2, 'archived': 1})
        self.assertEqual(before_list['total'], 2)

        # Delete active work w1 via JSON body
        del_res = c.request('DELETE', f'/api/works/{w1}', json={'expected_revision': 0})
        self.assertEqual(del_res.status_code, 200, del_res.text)
        self.assertEqual(del_res.json()['revision'], 1)
        self.assertTrue(del_res.json()['deleted'])

        # GET and mutations treat deleted work as 404
        self.assertEqual(c.get(f'/api/works/{w1}').status_code, 404)
        self.assertEqual(c.get(f'/api/works/{w1}/messages').status_code, 404)
        self.assertEqual(c.patch(f'/api/works/{w1}', json={'expected_revision': 1, 'title': 'x'}).status_code, 404)
        self.assertEqual(c.delete(f'/api/works/{w1}?expected_revision=1').status_code, 404)

        # Active list and counts exclude w1
        after_list = c.get('/api/works').json()
        self.assertEqual([it['id'] for it in after_list['items']], [w3])
        self.assertEqual(after_list['total'], 1)
        self.assertEqual(after_list['counts'], {'active': 1, 'archived': 1})

        # Delete archived work w2 via query parameter expected_revision
        del_arch = c.delete(f'/api/works/{w2}?expected_revision=1')
        self.assertEqual(del_arch.status_code, 200, del_arch.text)
        arch_list = c.get('/api/works?archived=true').json()
        self.assertEqual(arch_list['items'], [])
        self.assertEqual(arch_list['total'], 0)
        self.assertEqual(arch_list['counts'], {'active': 1, 'archived': 0})

        # Rows in works, work_messages and work_events are still in the database
        raw_work = conn.execute('SELECT * FROM works WHERE id=?', (w1,)).fetchone()
        self.assertIsNotNone(raw_work)
        self.assertIsNotNone(raw_work['deleted_at'])
        self.assertEqual(raw_work['revision'], 1)
        msg_rows = conn.execute('SELECT * FROM work_messages WHERE work_id=?', (w1,)).fetchall()
        self.assertEqual(len(msg_rows), 1)
        self.assertEqual(msg_rows[0]['id'], msg['id'])
        event_rows = conn.execute('SELECT * FROM work_events WHERE work_id=?', (w1,)).fetchall()
        self.assertGreaterEqual(len(event_rows), events_before)

        # Pending message dispatch skips the deleted work
        self.assertNotIn(w1, ws.works_with_pending(conn))
        self.assertEqual(ws.dispatch_next(conn, w1), (None, 'work_deleted'))

    def test_delete_work_active_run_and_stale_revision_return_409(self):
        c = self.client()
        wid = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'active', 'workspace_path': self.folder()}).json()['id']

        # Stale revision -> 409 revision_conflict
        stale = c.delete(f'/api/works/{wid}?expected_revision=99')
        self.assertEqual((stale.status_code, stale.json()['code']), (409, 'revision_conflict'))

        # Missing expected_revision -> 422
        self.assertEqual(c.delete(f'/api/works/{wid}').status_code, 422)

        # Cross-site origin rejected by mutation_guard -> 403
        evil = c.delete(f'/api/works/{wid}?expected_revision=0', headers={'origin': 'https://evil.invalid'})
        self.assertEqual(evil.status_code, 403)

        # Link a session to the work and check every active/unresolved status
        conn = store.connect(self.db)
        self.addCleanup(conn.close)
        policy = normalize(DEFAULT)
        pol_digest = digest(policy)
        msg, _ = ws.add_message(conn, wid, str(uuid4()), 'run', pol_digest, policy, pol_digest)
        sid = 'sess_' + uuid4().hex
        with store.transaction(conn):
            repo.create_session(conn, sid, self.folder(), 'auto', policy=policy)
            conn.execute("UPDATE work_messages SET status='LINKED' WHERE id=?", (msg['id'],))
            conn.execute('INSERT INTO work_runs VALUES (?,?,?,?,?)', (msg['id'], sid, wid, 'QUEUED', store._now()))

        blocked_statuses = (
            'QUEUED', 'RUNNING', 'PAUSED', 'RECOVERY_REQUIRED',
            'WAITING_CAPACITY', 'WAITING_AUTH', 'WAITING_APPROVAL', 'WAITING_CONTEXT', 'WAITING_HUMAN',
        )
        for status in blocked_statuses:
            with self.subTest(status=status):
                with store.transaction(conn):
                    conn.execute('UPDATE sessions SET status=? WHERE id=?', (status, sid))
                res = c.delete(f'/api/works/{wid}?expected_revision=0')
                self.assertEqual((res.status_code, res.json()['code']), (409, 'work_active'))

        # Once the latest execution is terminal, delete succeeds
        with store.transaction(conn):
            conn.execute("UPDATE sessions SET status='SUCCEEDED' WHERE id=?", (sid,))
        ok_res = c.delete(f'/api/works/{wid}?expected_revision=0')
        self.assertEqual(ok_res.status_code, 200, ok_res.text)

        # Mobile ALLOW list must not allow DELETE /api/works/{wid}
        self.assertFalse(any(m == 'DELETE' and pat.fullmatch(f'/api/works/{wid}') for m, pat, _ in mobile._ALLOW))

    def test_pinned_works_validation_and_phone_fields(self):
        c = self.client()
        get_res = c.get('/api/settings')
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.json()['settings']['pinned_works'], [])
        self.assertIn('pinned_works', user_settings.PHONE_FIELDS)

        # Valid 20 items
        twenty = [f'work_{i:02d}' for i in range(20)]
        put_20 = c.put('/api/settings', json={'pinned_works': twenty})
        self.assertEqual(put_20.status_code, 200, put_20.text)
        self.assertEqual(put_20.json()['settings']['pinned_works'], twenty)

        # 21 items rejected (422)
        twenty_one = [f'work_{i:02d}' for i in range(21)]
        self.assertEqual(c.put('/api/settings', json={'pinned_works': twenty_one}).status_code, 422)

        # Duplicate items rejected (422)
        self.assertEqual(c.put('/api/settings', json={'pinned_works': ['w1', 'w1']}).status_code, 422)

        # Item > 64 chars or empty rejected (422)
        self.assertEqual(c.put('/api/settings', json={'pinned_works': ['x' * 65]}).status_code, 422)
        self.assertEqual(c.put('/api/settings', json={'pinned_works': ['']}).status_code, 422)

        # Saved pinned_works untouched after rejected updates
        self.assertEqual(c.get('/api/settings').json()['settings']['pinned_works'], twenty)

    def test_phone_may_rename_work_but_not_archive_or_delete(self):
        c = self.client()
        wid = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'ilk', 'workspace_path': self.folder()}).json()['id']
        host, login = 'mac.tail1234.ts.net', 'me@example.com'
        gw = TestClient(mobile.MobileGuard(c.app, str(self.db), host), base_url=f'https://{host}')
        code = c.post('/api/mobile/pairing').json()['code']
        paired = gw.post('/api/mobile/pair', json={'code': code, 'device_name': 'Phone'},
                         headers={'Tailscale-User-Login': login}).json()
        token, device_id = paired['token'], paired['device_id']
        headers = {'Authorization': f'Bearer {token}', 'Tailscale-User-Login': login, 'Origin': f'https://{host}'}

        # Without command scope -> 403 scope_denied
        no_scope = gw.patch(f'/api/works/{wid}', json={'expected_revision': 0, 'title': 'yeni'}, headers=headers)
        self.assertEqual((no_scope.status_code, no_scope.json()['code']), (403, 'scope_denied'))

        # Grant command scope -> rename succeeds (200), archive is rejected (403 mobile_field), delete is 404
        c.put(f'/api/mobile/devices/{device_id}/scopes', json={'scopes': ['command']})
        ren = gw.patch(f'/api/works/{wid}', json={'expected_revision': 0, 'title': 'telefondan'}, headers=headers)
        self.assertEqual(ren.status_code, 200, ren.text)
        self.assertEqual(ren.json()['title'], 'telefondan')

        arch = gw.patch(f'/api/works/{wid}', json={'expected_revision': 1, 'archived': True}, headers=headers)
        self.assertEqual((arch.status_code, arch.json()['code']), (403, 'mobile_field'))

        del_gw = gw.delete(f'/api/works/{wid}?expected_revision=1', headers=headers)
        self.assertEqual(del_gw.status_code, 404)


if __name__ == '__main__':
    unittest.main()
