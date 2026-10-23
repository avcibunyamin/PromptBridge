"""F3-02 API: pause/resume (digest-bound), cancel from PAUSED, loop detail, restart, queue, guards."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from starlette.testclient import TestClient
from app import store, loop_store as loop
from app.api import create_app
from tests.test_loop_runner import Script, review
import tests.fakebin  # noqa: F401  (provider CLIs are stubs, not whatever is installed)


class LoopApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.system').mkdir()
        (self.root / '.system/providers.yaml').write_text(
            'roles:\n  author: [codex]\n  reviewer: [claude]\nloop:\n  max_turns: 3\n')
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        # Both providers count as installed; nothing real runs.
        for target in ('app.adapters.process.executable', 'app.adapters.claude.claude_binary'):
            p = patch(target, lambda *a, **k: '/fixture'); p.start(); self.addCleanup(p.stop)
        self.folder = self.root / 'ws'; self.folder.mkdir()
        self.app = create_app()

    def client(self, script):
        c = TestClient(self.app); c.__enter__(); self.addCleanup(c.__exit__, None, None, None)
        self.app.state.runner.adapter_factory = script
        self.digest = c.get('/api/sessions/providers').json()['digest']
        return c

    def work_and_message(self, c, text='hedef'):
        wid = str(uuid4())
        self.assertEqual(c.post('/api/works', json={'client_work_id': wid, 'title': 't', 'workspace_path': str(self.folder)}).status_code, 201)
        r = c.post(f'/api/works/{wid}/messages', json={'client_message_id': str(uuid4()), 'text': text, 'policy_digest': self.digest})
        self.assertEqual(r.status_code, 201, r.text)
        return wid

    def wait(self, fn, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(.03)
        self.fail('condition not reached')

    def session_of(self, c, wid):
        return self.wait(lambda: (c.get(f'/api/works/{wid}/messages').json()['items'][0]['session'] or {}).get('id'))

    def status(self, c, sid):
        return c.get(f'/api/sessions/{sid}').json()['session']['status']

    def pause_mid_author(self, c, script):
        wid = self.work_and_message(c)
        sid = self.session_of(c, wid)
        self.wait(lambda: script.calls)                                            # author running
        self.assertEqual(c.post(f'/api/sessions/{sid}/pause').json(), {'status': 'PAUSE_REQUESTED'})
        self.wait(lambda: self.status(c, sid) == 'PAUSED')
        return wid, sid

    def test_verified_run_exposes_loop_and_turns(self):
        c = self.client(Script([review('changes_requested', 'Eksik test'), review('pass')], writes=lambda n: str(n)))
        wid = self.work_and_message(c)
        sid = self.session_of(c, wid)
        self.wait(lambda: self.status(c, sid) == 'SUCCEEDED')
        d = c.get(f'/api/sessions/{sid}').json()
        self.assertEqual((d['loop']['outcome'], d['loop']['current_turn'], d['loop']['max_turns']), ('verified', 2, 3))
        self.assertEqual([t['verdict'] for t in d['turns']], ['changes_requested', 'pass'])
        self.assertEqual(d['turns'][0]['findings'][0]['summary'], 'Eksik test')
        self.assertGreater(d['loop']['used_ms'], 0)
        self.assertIsNone(c.get(f'/api/works/{wid}').json()['blocked_reason'])

    def test_pause_blocks_queue_and_archive_then_resume_finishes(self):
        script = Script([review('pass')], delay=0.4)
        c = self.client(script)
        wid, sid = self.pause_mid_author(c, script)
        work = c.get(f'/api/works/{wid}').json()
        self.assertEqual(work['blocked_reason'], 'paused')
        self.assertEqual(c.patch(f'/api/works/{wid}', json={'expected_revision': work['revision'], 'archived': True}).json()['code'], 'work_active')
        self.assertEqual(c.post(f'/api/works/{wid}/continue', json={'confirm': True, 'expected_session_id': sid}).json()['code'], 'paused')
        rev = c.get(f'/api/sessions/{sid}').json()['loop']['pause_revision']
        self.assertEqual(c.post(f'/api/sessions/{sid}/resume', json={'pause_revision': rev}).status_code, 422)
        self.assertEqual(c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev + 1}).json()['code'], 'stale_pause')
        self.assertEqual(c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev}).status_code, 202)
        self.wait(lambda: self.status(c, sid) == 'SUCCEEDED')
        self.assertEqual(c.get(f'/api/sessions/{sid}').json()['loop']['outcome'], 'verified')

    def test_folder_change_while_paused_needs_digest_bound_confirmation(self):
        script = Script([review('pass')], delay=0.3)
        c = self.client(script)
        _, sid = self.pause_mid_author(c, script)
        rev = c.get(f'/api/sessions/{sid}').json()['loop']['pause_revision']
        (self.folder / 'external.txt').write_text('dış editör')
        first = c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev})
        body = first.json()
        self.assertEqual((first.status_code, body['code']), (409, 'workspace_changed'))
        self.assertEqual([ch['path'] for ch in body['changes']], ['external.txt'])
        (self.folder / 'external.txt').write_text('ekran gösterildikten sonra yine değişti')
        second = c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev, 'diff_digest': body['diff_digest']})
        self.assertEqual(second.json()['code'], 'workspace_changed')              # old confirmation does not count
        ok = c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev, 'diff_digest': second.json()['diff_digest']})
        self.assertEqual((ok.status_code, ok.json()['folder_changed']), (202, True))
        self.wait(lambda: self.status(c, sid) == 'SUCCEEDED')

    def test_concurrent_resume_starts_one_worker(self):
        script = Script([review('pass')], delay=0.3)
        c = self.client(script)
        _, sid = self.pause_mid_author(c, script)
        rev = c.get(f'/api/sessions/{sid}').json()['loop']['pause_revision']
        with ThreadPoolExecutor(4) as pool:
            codes = sorted(pool.map(lambda _: c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev}).status_code, range(4)))
        self.assertEqual(codes.count(202), 1, codes)
        self.wait(lambda: self.status(c, sid) == 'SUCCEEDED')
        reviewers = [call for call in script.calls if call['profile'] == 'READ_ONLY']
        self.assertEqual(len(reviewers), 1)                                        # the reviewer ran once

    def test_resume_refused_while_another_work_uses_the_folder(self):
        script = Script([review('pass')], delay=0.4)
        c = self.client(script)
        _, a_sid = self.pause_mid_author(c, script)
        b = self.work_and_message(c, 'ikinci iş')                                # PAUSED released the lock
        b_sid = self.session_of(c, b)
        self.wait(lambda: self.status(c, b_sid) == 'RUNNING')
        rev = c.get(f'/api/sessions/{a_sid}').json()['loop']['pause_revision']
        r = c.post(f'/api/sessions/{a_sid}/resume', json={'confirm': True, 'pause_revision': rev})
        self.assertEqual((r.status_code, r.json()['code']), (409, 'workspace_busy'))
        self.assertEqual(self.status(c, a_sid), 'PAUSED')

    def test_resume_race_inside_the_transaction_reports_workspace_busy(self):
        script = Script(delay=0.3)
        c = self.client(script)
        _, sid = self.pause_mid_author(c, script)
        rev = c.get(f'/api/sessions/{sid}').json()['loop']['pause_revision']
        from unittest.mock import patch
        from app import loop_store
        with patch.object(loop_store, 'accept_resume', side_effect=loop_store.LoopStateChanged('workspace_busy')):
            r = c.post(f'/api/sessions/{sid}/resume', json={'confirm': True, 'pause_revision': rev})
        self.assertEqual((r.status_code, r.json()['code'], r.json()['detail']),
                         (409, 'workspace_busy', 'folder is used by another run'))
        self.assertEqual(self.status(c, sid), 'PAUSED')

    def test_cancel_from_paused(self):
        script = Script(delay=0.3)
        c = self.client(script)
        wid, sid = self.pause_mid_author(c, script)
        self.assertEqual(c.post(f'/api/sessions/{sid}/cancel').json(), {'status': 'CANCELLED'})
        self.assertEqual(c.get(f'/api/works/{wid}').json()['blocked_reason'], 'needs_continue')

    def test_unverified_outcome_needs_continue_with_that_outcome(self):
        c = self.client(Script(['bloksuz cevap']))
        wid = self.work_and_message(c)
        sid = self.session_of(c, wid)
        self.wait(lambda: self.status(c, sid) == 'SUCCEEDED')
        self.assertEqual(c.get(f'/api/works/{wid}').json()['blocked_reason'], 'needs_review_decision')
        stale = c.post(f'/api/works/{wid}/continue', json={'confirm': True, 'expected_session_id': sid, 'expected_outcome': 'turn_limit'})
        self.assertEqual(stale.json()['code'], 'stale_outcome')
        ok = c.post(f'/api/works/{wid}/continue', json={'confirm': True, 'expected_session_id': sid, 'expected_outcome': 'review_invalid'})
        self.assertEqual(ok.status_code, 200)
        self.assertIsNone(c.get(f'/api/works/{wid}').json()['blocked_reason'])

    def test_guards_and_legacy(self):
        c = self.client(Script())
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n')   # legacy policy
        self.digest = c.get('/api/sessions/providers').json()['digest']
        wid = self.work_and_message(c)
        sid = self.session_of(c, wid)
        self.wait(lambda: self.status(c, sid) == 'SUCCEEDED')
        self.assertNotIn('loop', c.get(f'/api/sessions/{sid}').json())             # legacy detail unchanged
        self.assertEqual(c.post(f'/api/sessions/{sid}/pause').json()['code'], 'not_a_loop')
        for path, body in ((f'/api/sessions/{sid}/pause', None), (f'/api/sessions/{sid}/resume', {'confirm': True, 'pause_revision': 1})):
            self.assertEqual(c.post(path, json=body, headers={'origin': 'https://evil.invalid'}).status_code, 403)


class Restart(unittest.TestCase):
    def test_paused_survives_restart_and_open_segments_are_estimated(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); (root / '.system').mkdir(); db = root / '.system/state.db'
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root)}); env.start(); self.addCleanup(env.stop)
        from app import runner_store as repo
        from app.provider_policy import normalize
        conn = store.connect(db); store.migrate(conn)
        policy = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}, 'loop': {}})
        for sid in ('paused', 'crashed'):
            (root / sid).mkdir()
            repo.create_session(conn, sid, str(root / sid), 'auto', policy=policy)
            loop.init(conn, sid, {'max_turns': 3, 'max_active_minutes': 30}, 'codex', 'claude', 'g', 'r')
        conn.execute("UPDATE sessions SET status='PAUSED' WHERE id='paused'")
        conn.execute("UPDATE sessions SET status='RUNNING' WHERE id='crashed'")
        seg = loop.open_segment(conn, 'crashed', 'crashed_t1a'); loop.heartbeat(conn, seg, 5000)
        conn.close()
        with TestClient(create_app()) as client:
            self.assertEqual(client.get('/api/sessions/paused').json()['session']['status'], 'PAUSED')
            crashed = client.get('/api/sessions/crashed').json()
            self.assertEqual(crashed['session']['status'], 'RECOVERY_REQUIRED')
            self.assertEqual((crashed['loop']['used_ms'], crashed['loop']['time_estimated']), (5000 + loop.HEARTBEAT_MS, True))


if __name__ == '__main__':
    unittest.main()
