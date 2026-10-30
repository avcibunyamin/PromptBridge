"""K-067: an account that just hit its quota is skipped at the start of the next step's chain (never the last one)."""
import json
import unittest
from pathlib import Path
import tempfile

from app import runner_store as repo, store
from app.runner import SessionRunner
from app.provider_policy import normalize
from tests.test_fallback import POLICY, quota
from tests.test_runner import Fake


class QuotaSkipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / 'workspace'; self.ws.mkdir()
        self.db = Path(self.tmp.name) / 'state.db'; self.conn = store.connect(self.db); self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def session(self, sid, policy=None):
        frozen = normalize(policy or POLICY)
        with store.transaction(self.conn):
            repo.create_session(self.conn, sid, str(self.ws), 'auto', policy=frozen)
            repo.add_session_step(self.conn, sid + '0', sid, 0, frozen['roles']['author'][0]['provider'], 'goal', role='author')

    def providers(self, sid):
        return [r[0] for r in self.conn.execute('SELECT a.provider FROM session_attempts a JOIN session_steps s ON s.id=a.step_id '
                                                'WHERE s.session_id=? ORDER BY a.started_at', (sid,))]

    async def run_with(self, sid, codex):
        await SessionRunner(self.db, adapter_factory=lambda p: codex if p == 'codex' else Fake()).run_session(sid)

    async def test_recent_quota_is_skipped_and_recorded(self):
        self.session('a'); await self.run_with('a', Fake(quota()))
        self.assertEqual(self.providers('a'), ['codex', 'claude'])
        self.session('b'); await self.run_with('b', Fake())
        self.assertEqual(self.providers('b'), ['claude'])              # codex is not opened again
        self.assertEqual(repo.get_session(self.conn, 'b')['status'], 'SUCCEEDED')
        skip = json.loads(self.conn.execute("SELECT payload FROM session_events WHERE session_id='b' AND kind='handoff'").fetchone()[0])
        self.assertEqual((skip['from'], skip['to'], skip['reason']), ('codex', 'claude', 'QUOTA_RECENT'))

    async def test_old_quota_or_later_success_is_tried_again(self):
        self.session('a'); await self.run_with('a', Fake(quota()))
        self.conn.execute("UPDATE session_attempts SET ended_at='2020-01-01T00:00:00+00:00' WHERE error_code='QUOTA_EXCEEDED'")
        self.conn.commit()
        self.session('b'); await self.run_with('b', Fake())
        self.assertEqual(self.providers('b'), ['codex'])                # older than the window
        self.session('c'); await self.run_with('c', Fake(quota()))
        self.session('d'); await self.run_with('d', Fake())
        self.assertEqual(self.providers('d'), ['claude'])
        # codex worked again after its quota error (here: b's success is moved after c's error) → it is tried first again
        self.conn.execute("UPDATE session_attempts SET ended_at=? WHERE provider='codex' AND status='COMPLETED'", (store._now(),))
        self.conn.commit()
        self.assertEqual(repo.skip_exhausted(self.conn, 'd', 'd0', normalize(POLICY)['roles']['author'], 'author', 60), 0)

    async def test_last_entry_is_never_skipped(self):
        solo = {'roles': {'author': ['codex'], 'reviewer': ['claude']}}
        self.session('a', solo); await self.run_with('a', Fake(quota()))
        self.assertEqual(repo.get_session(self.conn, 'a')['status'], 'WAITING_CAPACITY')
        self.session('b', solo); await self.run_with('b', Fake())
        self.assertEqual(self.providers('b'), ['codex'])



if __name__ == '__main__':
    unittest.main()
