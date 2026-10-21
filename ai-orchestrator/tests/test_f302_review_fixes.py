"""F3-02 code review (Codex, 2026-09-27) fixes: resume race, missing outcome, double continue."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app import store, runner_store as repo, works_store as ws, loop_store as loop
from app.provider_policy import normalize, digest


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws_dir = Path(self.tmp.name) / 'ws'; self.ws_dir.mkdir()
        self.conn = store.connect(Path(self.tmp.name) / 'state.db'); self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'fixes', str(self.ws_dir))

    def dispatch(self, loop_policy=True):
        roles = {'author': ['codex'], 'reviewer': ['claude']}
        policy = normalize({'roles': roles, 'loop': {}} if loop_policy else {'roles': roles})
        ws.add_message(self.conn, self.wid, str(uuid4()), 'goal', digest(policy), policy, digest(policy))
        sid, reason = ws.dispatch_next(self.conn, self.wid)
        self.assertIsNone(reason)
        return sid

    def set_status(self, sid, status):
        self.conn.execute('UPDATE sessions SET status=? WHERE id=?', (status, sid))
        ws.sync_run_status(self.conn)


class ResumeRace(Base):
    def paused(self):
        sid = self.dispatch()
        self.set_status(sid, 'RUNNING')
        loop.apply_pause(self.conn, sid, None)
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'PAUSED')
        return sid

    def test_resume_rechecks_a_competing_reservation_inside_the_transaction(self):
        sid = self.paused()
        # Another run reserves the folder after the API's fast-path check would have passed.
        repo.create_session(self.conn, 'competitor', str(self.ws_dir), 'auto')
        with self.assertRaises(loop.LoopStateChanged) as busy:
            loop.accept_resume(self.conn, sid, 1, 'digest', False)
        self.assertEqual(str(busy.exception), 'workspace_busy')
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'PAUSED')
        self.assertIsNone(loop.state(self.conn, sid)['resume_digest'])
        self.conn.execute("UPDATE sessions SET status='SUCCEEDED' WHERE id='competitor'")
        loop.accept_resume(self.conn, sid, 1, 'digest', False)
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'QUEUED')

    def test_resume_rechecks_the_workspace_lock(self):
        sid = self.paused()
        repo.create_session(self.conn, 'holder', str(self.ws_dir), 'auto')
        self.conn.execute("UPDATE sessions SET status='FAILED' WHERE id='holder'")
        self.assertTrue(repo.acquire_lock(self.conn, str(self.ws_dir), 'holder'))   # canonical path, like the runner
        with self.assertRaises(loop.LoopStateChanged) as busy:
            loop.accept_resume(self.conn, sid, 1, 'digest', False)
        self.assertEqual(str(busy.exception), 'workspace_busy')
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'PAUSED')


class ContinueRules(Base):
    def test_loop_success_without_outcome_can_never_be_continued(self):
        sid = self.dispatch()
        self.set_status(sid, 'SUCCEEDED')
        self.assertIsNone(loop.state(self.conn, sid)['outcome'])
        for expected in (None, 'turn_limit', 'verified'):
            with self.subTest(expected=expected):
                with self.assertRaises(ws.WorkError) as refused:
                    ws.continue_work(self.conn, self.wid, sid, expected)
                self.assertEqual((refused.exception.code, refused.exception.status), ('outcome_missing', 409))
        self.assertIsNone(ws.get_work(self.conn, self.wid)['continued_session_id'])

    def test_every_unverified_outcome_is_continuable_with_its_name(self):
        self.assertEqual(set(ws.CONTINUABLE_OUTCOMES),
                         {'reviewer_blocked', 'review_invalid', 'no_progress', 'turn_limit', 'budget_exhausted'})

    def test_second_continue_is_a_conflict(self):
        sid = self.dispatch(loop_policy=False)
        self.set_status(sid, 'FAILED')
        self.assertTrue(ws.continue_work(self.conn, self.wid, sid))
        with self.assertRaises(ws.WorkError) as again:
            ws.continue_work(self.conn, self.wid, sid)
        self.assertEqual((again.exception.code, again.exception.status), ('already_continued', 409))
        events = [r[0] for r in self.conn.execute("SELECT type FROM work_events WHERE work_id=? AND type='continued'",
                                                  (self.wid,))]
        self.assertEqual(len(events), 1)


if __name__ == '__main__':
    unittest.main()
