"""F3-02: schema 1.5 migration and loop_store (turn decisions, CAS, time ledger, pause)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from app import store, runner_store as repo, loop_store as loop
from app.provider_policy import normalize

POLICY = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}, 'loop': {'max_turns': 3}})
NEXT = ('codex', 'claude', 'fix it', 'review again')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.ws = Path(self.tmp.name) / 'ws'; self.ws.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)

    def session(self, sid='s', max_turns=3):
        ws = self.ws / sid; ws.mkdir()   # one folder per session: QUEUED ones reserve theirs
        repo.create_session(self.conn, sid, str(ws), 'auto', policy=POLICY)
        loop.init(self.conn, sid, {'max_turns': max_turns, 'max_active_minutes': 30}, 'codex', 'claude', 'goal', 'review')
        return sid

    def snap(self, sid, files, complete=True, scope='A'):
        return repo.save_snapshot(self.conn, sid, None, 'test', {'files': files, 'complete': complete,
                                                                   'unknown': [] if complete else ['x'], 'scope': scope})

    def to_review(self, sid, turn, before=None, after=None):
        if before: loop.mark_author_snapshot(self.conn, sid, turn, 'before', before)
        if after: loop.mark_author_snapshot(self.conn, sid, turn, 'after', after)
        loop.author_done(self.conn, sid, turn)


class Migration(unittest.TestCase):
    def test_1_4_to_current_is_additive_and_idempotent(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        c = store.connect(Path(tmp.name) / 'old.db')
        c.executescript((Path(__file__).parent / 'fixtures/schema_1_4.sql').read_text())
        c.execute("INSERT OR REPLACE INTO schema_meta VALUES ('schema_version','1.4')")
        c.execute("INSERT INTO sessions VALUES ('s','/w','auto','SUCCEEDED','t','t')")
        c.execute("INSERT INTO session_steps(id,session_id,step_index,agent_name,task_prompt,status) VALUES ('s0','s',0,'codex','g','COMPLETED')")
        c.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,retry_generation) VALUES ('a1','s0',1,'codex','WRITE','t','COMPLETED',2)")
        c.commit()
        ddl = {r[0]: r[1] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
        rows = [tuple(r) for r in c.execute('SELECT * FROM session_attempts')]
        store.migrate(c)
        after = {r[0]: r[1] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}
        self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], store.SCHEMA_VERSION)
        # nothing existing rebuilt; the only change to an existing table is TB-37's additive works.deleted_at
        self.assertEqual({n: after[n] for n in ddl if n != 'works'}, {n: v for n, v in ddl.items() if n != 'works'})
        self.assertEqual(after['works'].replace(', deleted_at TEXT', ''), ddl['works'])
        self.assertIn('deleted_at TEXT', after['works'])
        self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM session_attempts')], rows)
        self.assertTrue({'idx_time_segments_open', 'session_loop_state', 'session_time_segments', 'session_turns'}
                        <= set(after) - set(ddl))
        self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        store.migrate(c)
        self.assertEqual({r[0]: r[1] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")}, after)
        c.close()


class Turns(Base):
    def test_turn_one_created_with_roles_and_cursor(self):
        sid = self.session()
        self.assertEqual((loop.state(self.conn, sid)['current_turn'], loop.state(self.conn, sid)['phase']), (1, 'author'))
        steps = repo.get_session_steps(self.conn, sid)
        self.assertEqual([(s['id'], s['role'], s['profile']) for s in steps],
                         [('s_t1a', 'author', 'WRITE'), ('s_t1r', 'reviewer', 'READ_ONLY')])
        self.assertEqual(loop.next_step(self.conn, sid)['id'], 's_t1a')
        self.assertEqual(loop.turn_of_step(self.conn, 's_t1r'), (1, 'reviewer'))
        self.assertIsNone(loop.turn_of_step(self.conn, 'legacy_0'))

    def test_decisions(self):
        cases = [('pass', None, 'verified'), ('blocked', None, 'reviewer_blocked'), ('invalid', 'no_block', 'review_invalid')]
        for n, (verdict, code, outcome) in enumerate(cases):
            with self.subTest(verdict=verdict):
                sid = self.session(f's{n}')
                self.to_review(sid, 1)
                d = loop.close_turn(self.conn, sid, 1, verdict=verdict, findings=[], findings_hash=None,
                                    invalid_code=code, next_prompts=NEXT)
                self.assertEqual(d, {'stop': True, 'outcome': outcome})
                self.assertEqual(loop.state(self.conn, sid)['phase'], 'done')
                self.assertIsNone(loop.next_step(self.conn, sid))

    def test_changes_requested_opens_next_turn_then_turn_limit(self):
        sid = self.session(max_turns=2)
        self.to_review(sid, 1)
        d = loop.close_turn(self.conn, sid, 1, verdict='changes_requested', findings=[{'id': 'F1'}], findings_hash='h1',
                            invalid_code=None, next_prompts=NEXT)
        self.assertEqual(d, {'stop': False, 'outcome': None})
        self.assertEqual((loop.state(self.conn, sid)['current_turn'], loop.next_step(self.conn, sid)['id']), (2, 's_t2a'))
        self.assertEqual([s['step_index'] for s in repo.get_session_steps(self.conn, sid)], [0, 1, 2, 3])
        self.to_review(sid, 2)
        d = loop.close_turn(self.conn, sid, 2, verdict='changes_requested', findings=[{'id': 'F1'}], findings_hash='h2',
                            invalid_code=None, next_prompts=NEXT)
        self.assertEqual(d['outcome'], 'turn_limit')
        self.assertEqual(len(loop.turns(self.conn, sid)), 2)                       # no third turn

    def test_no_progress_needs_same_findings_and_unchanged_complete_snapshots(self):
        variants = [('h1', {'a': 1}, {'a': 1}, True, 'no_progress'),
                    ('h1', {'a': 1}, {'a': 2}, True, None),                        # files changed → go on
                    ('hX', {'a': 1}, {'a': 1}, True, None),                        # findings changed → go on
                    ('h1', {'a': 1}, {'a': 1}, False, None)]                       # incomplete → unknown, go on
        for n, (fhash, before, after, complete, expected) in enumerate(variants):
            with self.subTest(n=n):
                sid = self.session(f'p{n}', max_turns=5)
                self.to_review(sid, 1)
                loop.close_turn(self.conn, sid, 1, verdict='changes_requested', findings=[{}], findings_hash='h1',
                                invalid_code=None, next_prompts=NEXT)
                self.to_review(sid, 2, self.snap(sid, before, complete), self.snap(sid, after, complete))
                d = loop.close_turn(self.conn, sid, 2, verdict='changes_requested', findings=[{}], findings_hash=fhash,
                                    invalid_code=None, next_prompts=NEXT)
                self.assertEqual(d['outcome'], expected)

    def test_before_snapshot_is_first_attempt_only(self):
        sid = self.session()
        first, second = self.snap(sid, {'a': 1}), self.snap(sid, {'a': 2})
        loop.mark_author_snapshot(self.conn, sid, 1, 'before', first)
        loop.mark_author_snapshot(self.conn, sid, 1, 'before', second)             # quota handover attempt 2
        self.assertEqual(loop.turns(self.conn, sid)[0]['snapshot_before_id'], first)

    def test_double_and_concurrent_decision_opens_one_turn(self):
        sid = self.session(max_turns=5)
        self.to_review(sid, 1)

        def decide(_):
            c = store.connect(self.db)
            try:
                loop.close_turn(c, sid, 1, verdict='changes_requested', findings=[{}], findings_hash='h',
                                invalid_code=None, next_prompts=NEXT)
                return 'ok'
            except loop.LoopStateChanged:
                return 'lost'
            finally:
                c.close()
        with ThreadPoolExecutor(6) as pool:
            got = list(pool.map(decide, range(6)))
        self.assertEqual(got.count('ok'), 1, got)
        self.assertEqual(len(loop.turns(self.conn, sid)), 2)
        with self.assertRaises(loop.LoopStateChanged):
            loop.author_done(self.conn, sid, 1)                                    # stale cursor

    def test_turn_rows_are_unique(self):
        sid = self.session()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO session_turns(session_id,turn_no,author_step_id,reviewer_step_id,created_at) "
                              "VALUES ('s',1,'x','y','t')")


class Ledger(Base):
    def test_settle_exactly_once_and_totals(self):
        sid = self.session()
        seg = loop.open_segment(self.conn, sid, 's_t1a')
        loop.heartbeat(self.conn, seg, 4000)
        loop.heartbeat(self.conn, seg, 3000)                                       # never goes backwards
        self.assertEqual(loop.used_ms(self.conn, sid), 4000)
        self.assertTrue(loop.settle(self.conn, seg, 7000))
        self.assertFalse(loop.settle(self.conn, seg, 99_000))                      # double callback: no-op
        self.assertEqual(loop.used_ms(self.conn, sid), 7000)
        seg2 = loop.open_segment(self.conn, sid, 's_t1r')
        loop.settle(self.conn, seg2, 500)
        self.assertEqual(loop.used_ms(self.conn, sid), 7500)

    def test_abandoned_segment_is_estimated_not_zero(self):
        sid = self.session()
        seg = loop.open_segment(self.conn, sid, 's_t1a')
        loop.heartbeat(self.conn, seg, 20_000)
        self.assertEqual(loop.settle_abandoned(self.conn), 1)
        row = self.conn.execute('SELECT elapsed_ms, settled, estimated FROM session_time_segments').fetchone()
        self.assertEqual(tuple(row), (20_000 + loop.HEARTBEAT_MS, 1, 1))
        self.assertEqual(loop.settle_abandoned(self.conn), 0)                     # already closed
        self.assertFalse(loop.settle(self.conn, seg, 1))                           # a late runner cannot recount


class Pause(Base):
    def test_request_apply_and_supersede(self):
        sid = self.session()
        self.assertTrue(loop.request_pause(self.conn, sid))                        # QUEUED counts
        self.assertFalse(loop.request_pause(self.conn, sid))                       # already requested
        repo.claim_session(self.conn, sid)
        loop.apply_pause(self.conn, sid, self.snap(sid, {'a': 1}))
        st = loop.state(self.conn, sid)
        self.assertEqual((repo.get_session(self.conn, sid)['status'], st['pause_revision'], st['pause_requested_at']),
                         ('PAUSED', 1, None))
        self.assertFalse(loop.request_pause(self.conn, sid))                       # PAUSED: nothing to request

    def test_no_pause_request_once_done(self):
        sid = self.session()
        self.to_review(sid, 1)
        loop.close_turn(self.conn, sid, 1, verdict='pass', findings=[], findings_hash=None, invalid_code=None, next_prompts=NEXT)
        self.assertFalse(loop.request_pause(self.conn, sid))


if __name__ == '__main__':
    unittest.main()
