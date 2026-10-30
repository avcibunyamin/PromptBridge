"""F3-01a: persistent works — migration, idempotency, queue/dispatch, resume gates, prompt gate.

Adapters here are scripted fakes; they prove runner/dispatcher behaviour, not real
provider resume (that needs a real CLI run, reported separately).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from starlette.testclient import TestClient
from app import store, runner_store as repo, works_store as ws, work_prompt
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import normalize, digest, DEFAULT
from app.runner import SessionRunner
import tests.fakebin  # noqa: F401  (provider CLIs are stubs, not whatever is installed)

NEW_TABLES = ('works', 'work_messages', 'work_runs', 'work_events', 'work_resume_refs')


def ok(text='done', sid=None):
    return AdapterResult(text, sid, None, 'stop', False, 'completed', 0)


def failed(code='UNKNOWN'):
    return AdapterResult('broken', 'x', None, 'error', True, 'failed', 1, code, {'code': code})


class Scripted:
    """Per-provider scripted adapter recording (provider, prompt, provider_session_id)."""
    def __init__(self, log, scripts):
        self.log, self.scripts, self.counter = log, scripts, {}

    def __call__(self, provider):
        outer = self

        class Adapter:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                n = outer.counter[provider] = outer.counter.get(provider, 0) + 1
                outer.log.append({'provider': provider, 'prompt': prompt, 'psid': provider_session_id, 'profile': profile})
                queue = outer.scripts.get(provider, [])
                result = queue.pop(0) if queue else ok(f'{provider} output {n}')
                if not result.is_error and not result.provider_session_id:
                    result.provider_session_id = f'{provider}-thread-{n}'
                owner = {'pid': 100 + n, 'pgid': 100 + n, 'start_time': 1.0, 'boot_time': 1.0}
                delay = outer.scripts.get('delay', 0)
                yield {'type': 'process_started', 'ownership': owner}
                yield {'type': 'started', 'provider_session_id': result.provider_session_id or provider_session_id}
                if delay:
                    await asyncio.sleep(delay)
                yield {'type': 'process_stopped', 'ownership': owner}
                yield {'type': 'completed' if result.successful else 'failed', 'result': result}
        return Adapter()


def rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


# ── migration ────────────────────────────────────────────────────────────────

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.ws_dir = Path(self.tmp.name) / 'ws'; self.ws_dir.mkdir()

    def make_12(self):
        """A DB in real 1.2 shape: current schema minus every 1.3 object, labelled 1.2."""
        c = store.connect(self.db)
        schema = (Path(__file__).parent / 'fixtures/schema_1_3.sql').read_text().split('-- F3-01a (schema 1.3)')[0]
        c.executescript(schema)   # the fixture creates schema_meta itself
        c.execute("INSERT OR REPLACE INTO schema_meta VALUES ('schema_version','1.2')")
        repo.create_session(c, 's', str(self.ws_dir), 'auto')
        repo.add_session_step(c, 's0', 's', 0, 'codex', 'goal')
        c.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status) VALUES ('a0','s0',1,'codex','WRITE','old','FAILED')")
        return c

    def test_1_2_upgrade_preserves_data_and_adds_generation(self):
        c = self.make_12()
        attempts_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='session_attempts'").fetchone()[0]
        before = rows(c, 'SELECT * FROM session_attempts')
        store.migrate(c)
        self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], store.SCHEMA_VERSION)
        self.assertNotEqual(c.execute("SELECT sql FROM sqlite_master WHERE name='session_attempts'").fetchone()[0], attempts_sql)
        self.assertEqual([{k: v for k, v in r.items() if k != 'retry_generation'} for r in rows(c, 'SELECT * FROM session_attempts')], before)          # rebuilt, data identical
        for t in NEW_TABLES:
            self.assertTrue(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone(), t)
        self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        store.migrate(c)                                                                 # idempotent
        self.assertEqual([{k: v for k, v in r.items() if k != 'retry_generation'} for r in rows(c, 'SELECT * FROM session_attempts')], before)
        c.close()

    def test_injected_failure_rolls_back_to_1_2(self):
        c = self.make_12()

        class Boom:
            def __init__(self, inner): self.inner = inner
            def execute(self, sql, *a):
                if 'CREATE TABLE IF NOT EXISTS work_resume_refs' in sql:
                    raise sqlite3.OperationalError('injected migration failure')
                return self.inner.execute(sql, *a)
            def __getattr__(self, name): return getattr(self.inner, name)
        with self.assertRaises(sqlite3.OperationalError):
            store.migrate(Boom(c))
        self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], '1.2')
        self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='works'").fetchone())
        store.migrate(c)                                                                  # retry succeeds
        self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], store.SCHEMA_VERSION)
        c.close()

    def test_unknown_version_rejected_without_writes(self):
        c = self.make_12(); c.execute("UPDATE schema_meta SET value='9.9' WHERE key='schema_version'")
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            store.migrate(c)
        self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='works'").fetchone())
        c.close()

    def test_work_events_append_only_and_message_payload_frozen(self):
        c = store.connect(self.db); store.migrate(c)
        ws.create_work(c, str(uuid4()), 't', str(self.ws_dir))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
            c.execute('UPDATE work_events SET type=?', ('x',))
        c.close()


# ── resume gates, prompts and the success-end snapshot (direct runner) ───────

class ResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / 'state.db'
        self.work_dir = root / 'ws'; self.work_dir.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.policy = normalize(DEFAULT)
        self.wid = str(uuid4())
        ws.create_work(self.conn, self.wid, 'demo', str(self.work_dir))
        self.log, self.scripts = [], {}
        self.factory = Scripted(self.log, self.scripts)

    async def run_message(self, text, policy=None):
        policy = policy or self.policy
        ws.add_message(self.conn, self.wid, str(uuid4()), text, digest(policy), policy, digest(policy))
        sid, reason = ws.dispatch_next(self.conn, self.wid)
        self.assertTrue(sid, reason)
        start = len(self.log)
        await SessionRunner(self.db, adapter_factory=self.factory).run_session(sid)
        return sid, self.log[start:]

    def decision(self, sid):
        return dict(self.conn.execute('SELECT * FROM work_resume_refs WHERE session_id=?', (sid,)).fetchone())

    def status(self, sid):
        return repo.get_session(self.conn, sid)['status']

    async def test_a_failed_run_says_why(self):   # K-062
        self.scripts['codex'] = [failed('UNKNOWN')]
        sid, _ = await self.run_message('breaks')
        session = ws.list_messages(self.conn, self.wid)['items'][-1]['session']
        self.assertEqual(session['status'], 'FAILED')
        self.assertEqual(session['reason'], 'error')   # a code, not the provider's text
        self.assertEqual(ws.blocker(self.conn, self.wid), 'needs_continue')   # the panel offers "devam ettir" here

    async def test_a_signed_out_run_names_the_account_to_sign_in(self):   # K-060
        self.scripts['codex'] = [failed('AUTH')]
        sid, _ = await self.run_message('needs codex')
        self.assertEqual(self.status(sid), 'WAITING_AUTH')
        session = ws.list_messages(self.conn, self.wid)['items'][-1]['session']
        self.assertEqual((session['status'], session['auth_base']), ('WAITING_AUTH', 'codex'))

    async def test_read_only_run_gives_the_author_the_read_only_profile_and_rule(self):   # K-057
        from app.provider_policy import read_only
        accepted = digest(self.policy)
        ws.add_message(self.conn, self.wid, str(uuid4()), 'explain the parser', accepted, read_only(self.policy), accepted,
                       read_only=True)
        sid, reason = ws.dispatch_next(self.conn, self.wid)
        self.assertTrue(sid, reason)
        await SessionRunner(self.db, adapter_factory=self.factory).run_session(sid)
        self.assertEqual(self.status(sid), 'SUCCEEDED')
        self.assertEqual({c['profile'] for c in self.log}, {'READ_ONLY'})
        self.assertIn('READ-ONLY mode', self.log[0]['prompt'])
        self.assertNotIn('write profile', self.log[0]['prompt'])
        self.assertEqual({r['profile'] for r in rows(self.conn, 'SELECT profile FROM step_roles')}, {'READ_ONLY'})
        _, calls = await self.run_message('now change it')   # the next, ordinary message writes again
        self.assertEqual(calls[0]['profile'], 'WRITE')
        self.assertIn('write profile', calls[0]['prompt'])

    async def test_first_run_fresh_second_run_resumes_last_author_thread(self):
        s1, calls1 = await self.run_message('build the first feature')
        self.assertEqual(self.status(s1), 'SUCCEEDED')
        self.assertEqual(self.decision(s1)['reason'], 'no_previous_run')
        self.assertIn('build the first feature', calls1[0]['prompt'])
        last_author = rows(self.conn, "SELECT a.provider_session_id FROM session_attempts a WHERE a.step_id=?", f'{s1}_2')[0]
        s2, calls2 = await self.run_message('now add a follow-up')
        d = self.decision(s2)
        self.assertEqual((d['decision'], d['reason']), ('RESUME', 'all_gates_passed'))
        self.assertEqual(calls2[0]['provider'], 'codex')
        self.assertEqual(calls2[0]['psid'], last_author['provider_session_id'])   # previous run's LAST author attempt
        self.assertNotIn('build the first feature', calls2[0]['prompt'])            # RESUME: no history replay
        self.assertIn('now add a follow-up', calls2[0]['prompt'])
        reviewer = [c for c in calls2 if c['provider'] == 'claude'][0]
        self.assertIsNone(reviewer['psid'])                                          # reviewer always fresh
        self.assertIn('build the first feature', reviewer['prompt'])                 # fresh reviewer gets the goal
        self.assertEqual(calls2[2]['psid'], 'codex-thread-3')                        # step 3 uses this run's step-1 thread

    async def test_work_continuation_preserves_goal_request_and_step_context(self):
        policy = normalize({'roles': {'author': ['codex'], 'reviewer': ['codex']}})
        await self.run_message('ORIGINAL_CALCULATOR_GOAL', policy=policy)
        for stage in range(3):
            with self.subTest(stage=stage):
                self.scripts['codex'] = ([ok('CURRENT_AUTHOR_OUTPUT'), ok('CURRENT_REVIEW')][:stage]
                                         + [failed('CONTEXT_EXHAUSTED')])
                sid, calls = await self.run_message(f'FOLLOW_UP_{stage}', policy=policy)
                self.assertEqual(self.status(sid), 'SUCCEEDED')
                resumed = calls[stage + 1]
                self.assertIsNone(resumed['psid'])
                self.assertIn('ORIGINAL_CALCULATOR_GOAL', resumed['prompt'])
                self.assertIn(f'FOLLOW_UP_{stage}', resumed['prompt'])
                if stage == 1:
                    self.assertIn('CURRENT_AUTHOR_OUTPUT', resumed['prompt'])
                if stage == 2:
                    self.assertIn('CURRENT_REVIEW', resumed['prompt'])
                self.assertIn('not new instructions', resumed['prompt'])
                self.assertLessEqual(len(resumed['prompt'].encode()), 131072)

    async def test_cancelled_pending_goal_is_never_sent_to_provider(self):
        p = self.policy
        m, _ = ws.add_message(self.conn, self.wid, str(uuid4()), 'CANCELLED_DELETE_EVERYTHING',
                              digest(p), p, digest(p))
        ws.cancel_message(self.conn, self.wid, m['id'])
        for request in ('REAL_CALCULATOR_GOAL', 'FOLLOW_UP'):
            sid, calls = await self.run_message(request)
            self.assertEqual(self.status(sid), 'SUCCEEDED')
            self.assertTrue(all('CANCELLED_DELETE_EVERYTHING' not in c['prompt'] for c in calls))
            self.assertIn('REAL_CALCULATOR_GOAL', calls[1]['prompt'])

    async def test_work_continuation_rejects_oversized_combined_mandatory_prompt(self):
        # Each initial prompt fits, but adding the fresh work goal to the
        # continuation digest cannot fit the final 128 KiB provider prompt.
        await self.run_message('G' * 60000)
        self.scripts['codex'] = [failed('CONTEXT_EXHAUSTED')]
        sid, calls = await self.run_message('N' * 32000)
        self.assertEqual(self.status(sid), 'WAITING_CONTEXT')
        self.assertEqual(len(calls), 1)

    async def test_policy_change_is_fresh(self):
        await self.run_message('one')
        other = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}, 'max_context_continuations': 2})
        s2, calls = await self.run_message('two', policy=other)
        self.assertEqual(self.decision(s2)['reason'], 'policy_changed')
        self.assertIsNone(calls[0]['psid'])
        self.assertIn('one', calls[0]['prompt'])                                     # FRESH carries the first goal

    async def test_workspace_change_is_fresh(self):
        await self.run_message('one')
        (self.work_dir / 'edited.txt').write_text('outside change')
        s2, calls = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'workspace_changed')
        self.assertIsNone(calls[0]['psid'])

    async def test_unreadable_directory_is_incomplete_not_unchanged(self):
        await self.run_message('one')
        locked = self.work_dir / 'locked'; locked.mkdir(); os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        s2, _ = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'snapshot_incomplete')

    async def test_other_conductor_writer_is_fresh_even_if_hash_equal(self):
        s1, _ = await self.run_message('one')
        repo.create_session(self.conn, 'legacy', str(self.work_dir), 'auto')
        repo.update_session_status(self.conn, 'legacy', 'SUCCEEDED')
        s2, calls = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'other_writer')
        self.assertIsNone(calls[0]['psid'])

    async def test_unclean_owner_and_missing_baseline_are_fresh(self):
        s1, _ = await self.run_message('one')
        a = rows(self.conn, 'SELECT id FROM session_attempts WHERE step_id=?', f'{s1}_2')[0]['id']
        self.conn.execute("UPDATE process_owners SET status='RUNNING' WHERE attempt_id=?", (a,))
        s2, _ = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'author_attempt_unclean')
        self.conn.execute("DELETE FROM workspace_snapshots WHERE session_id=? AND kind='work_success_end'", (s2,))
        s3, _ = await self.run_message('three')
        self.assertEqual(self.decision(s3)['reason'], 'baseline_missing')

    async def test_retried_older_session_counts_as_other_writer(self):
        repo.create_session(self.conn, 'older', str(self.work_dir), 'auto')      # created before run 1
        repo.update_session_status(self.conn, 'older', 'SUCCEEDED')
        s1, _ = await self.run_message('one')
        repo.add_session_step(self.conn, 'older_0', 'older', 0, 'codex', 'x')
        repo.begin_attempt(self.conn, 'older_a', 'older_0', 1, 'codex', 'WRITE')  # ran again after run 1
        s2, _ = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'other_writer')

    async def test_new_work_in_same_folder_never_takes_another_works_thread(self):
        await self.run_message('one')
        other = str(uuid4()); ws.create_work(self.conn, other, 'second', str(self.work_dir))
        self.wid = other
        s2, calls = await self.run_message('fresh start')
        self.assertEqual(self.decision(s2)['reason'], 'no_previous_run')
        self.assertIsNone(calls[0]['psid'])

    async def test_delete_and_symlink_retarget_are_fresh(self):
        (self.work_dir / 'a.txt').write_text('a'); (self.work_dir / 'b.txt').write_text('b')
        os.symlink('a.txt', self.work_dir / 'link')
        await self.run_message('one')
        os.remove(self.work_dir / 'link'); os.symlink('b.txt', self.work_dir / 'link')
        s2, _ = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'workspace_changed')
        os.remove(self.work_dir / 'a.txt')
        s3, _ = await self.run_message('three')
        self.assertEqual(self.decision(s3)['reason'], 'workspace_changed')
        s4, _ = await self.run_message('four')                                   # nothing changed since s3
        self.assertEqual(self.decision(s4)['decision'], 'RESUME')

    async def test_scope_change_and_scan_limit_are_fresh(self):
        from app import handoff
        await self.run_message('one')
        with patch.object(handoff, 'SCOPE_VERSION', 99):
            s2, _ = await self.run_message('two')
        self.assertEqual(self.decision(s2)['reason'], 'snapshot_scope_mismatch')
        (self.work_dir / 'x').write_text('1'); (self.work_dir / 'y').write_text('2')
        await self.run_message('three')
        with patch.object(handoff, 'MAX_FILES', 1):
            s4, _ = await self.run_message('four')
        self.assertEqual(self.decision(s4)['reason'], 'snapshot_incomplete')

    async def test_missing_root_is_incomplete(self):
        await self.run_message('one')
        policy = self.policy
        ws.add_message(self.conn, self.wid, str(uuid4()), 'two', digest(policy), policy, digest(policy))
        sid, _ = ws.dispatch_next(self.conn, self.wid)
        ctx = ws.run_context(self.conn, sid)
        from app import handoff
        current = handoff.snapshot(self.work_dir / 'gone')
        d = ws.decide_resume(self.conn, sid, ctx, current, None, policy['roles']['author'])
        self.assertEqual((d['decision'], d['reason']), ('FRESH', 'snapshot_incomplete'))
        self.assertEqual(current['unknown'], ['root_unavailable'])

    async def test_failed_previous_run_never_resumes(self):
        self.scripts['codex'] = [failed()]
        s1, _ = await self.run_message('one')
        self.assertEqual(self.status(s1), 'FAILED')
        self.assertIsNone(ws.dispatch_next(self.conn, self.wid)[0])
        ws.add_message(self.conn, self.wid, str(uuid4()), 'two', digest(self.policy), self.policy, digest(self.policy))
        self.assertEqual(ws.dispatch_next(self.conn, self.wid), (None, 'needs_continue'))
        ws.continue_work(self.conn, self.wid, s1)
        s2, _ = ws.dispatch_next(self.conn, self.wid)
        await SessionRunner(self.db, adapter_factory=self.factory).run_session(s2)
        self.assertEqual(self.decision(s2)['reason'], 'previous_run_not_succeeded')

    async def test_continue_waiting_approval_requires_cancel(self):
        s1, _ = await self.run_message('one')
        self.conn.execute("UPDATE sessions SET status='WAITING_APPROVAL' WHERE id=?", (s1,))
        with self.assertRaises(ws.WorkError) as err:
            ws.continue_work(self.conn, self.wid, s1)
        self.assertEqual(err.exception.code, 'cancel_first')
        self.assertIsNone(ws.get_work(self.conn, self.wid)['continued_session_id'])

    def test_delete_cancels_pending_messages(self):
        p = self.policy
        m, _ = ws.add_message(self.conn, self.wid, str(uuid4()), 'queued', digest(p), p, digest(p))
        ws.delete_work(self.conn, self.wid, ws.get_work(self.conn, self.wid)['revision'])
        self.assertEqual(rows(self.conn, 'SELECT status FROM work_messages WHERE id=?', m['id'])[0]['status'],
                         'CANCELLED')
        self.assertEqual(ws.dispatch_next(self.conn, self.wid), (None, 'work_deleted'))

    def test_create_replay_survives_deleted_folder(self):
        folder = Path(self.tmp.name) / 'moved_away'; folder.mkdir()
        wid = str(uuid4())
        first, created = ws.create_work(self.conn, wid, 'gone', str(folder))
        self.assertTrue(created)
        folder.rmdir()
        again, created = ws.create_work(self.conn, wid, 'gone', str(folder))
        self.assertFalse(created)
        self.assertEqual((again['id'], again['workspace_path']), (first['id'], first['workspace_path']))
        with self.assertRaises(ws.WorkError) as err:
            ws.create_work(self.conn, wid, 'other title', str(folder))
        self.assertEqual(err.exception.code, 'IDEMPOTENCY_CONFLICT')

    async def test_last_author_claude_blocks_old_codex_seed(self):
        policy = normalize({'roles': {'author': ['codex', 'claude'], 'reviewer': ['claude']}})
        quota = failed('QUOTA_EXCEEDED')
        self.scripts['codex'] = [ok(), quota]   # run 1: step 1 codex ok, step 3 codex quota -> claude
        s1, calls1 = await self.run_message('one', policy=policy)
        self.assertEqual(self.status(s1), 'SUCCEEDED')
        self.assertEqual(calls1[-1]['provider'], 'claude')
        s2, calls2 = await self.run_message('two', policy=policy)
        self.assertEqual(self.decision(s2)['reason'], 'last_author_not_codex')
        self.assertIsNone(calls2[0]['psid'])

    async def test_step1_fallback_gets_fresh_prompt_and_seed_is_not_revived(self):
        policy = normalize({'roles': {'author': ['codex', 'claude'], 'reviewer': ['claude']}})
        await self.run_message('one', policy=policy)
        self.scripts['codex'] = [failed('QUOTA_EXCEEDED')]            # run 2 step 1: codex (seeded) hits quota
        s2, calls = await self.run_message('two', policy=policy)
        self.assertEqual(self.decision(s2)['decision'], 'RESUME')
        self.assertIsNotNone(calls[0]['psid'])                         # seed used once, on codex
        claude = calls[1]
        self.assertEqual(claude['provider'], 'claude')
        self.assertIsNone(claude['psid'])
        self.assertIn('one', claude['prompt'])                         # fallback is FRESH: it never saw the thread
        codex_step3 = [c for c in calls[3:] if c['provider'] == 'codex']
        self.assertTrue(all(c['psid'] != calls[0]['psid'] for c in codex_step3))

    async def test_prompt_too_large_starts_no_provider(self):
        s, calls = await self.run_message('🙂' * 40000)   # 160000 bytes > 131072
        self.assertEqual(calls, [])
        step = repo.get_session_steps(self.conn, s)[0]
        self.assertEqual((step['status'], step['terminal_reason'], self.status(s)), ('FAILED', 'PROMPT_TOO_LARGE', 'FAILED'))
        event = rows(self.conn, "SELECT payload FROM session_events WHERE session_id=? AND kind='prompt_rejected'", s)
        self.assertEqual(json.loads(event[0]['payload'])['reason'], 'mandatory_exceeds_limit')
        self.assertEqual(rows(self.conn, 'SELECT status FROM work_messages')[0]['status'], 'LINKED')

    async def test_success_end_snapshot_recorded_before_lock_release(self):
        seen = {}
        real = repo.release_lock

        def spy(conn, path, sid):
            seen['snap'] = conn.execute("SELECT count(*) FROM workspace_snapshots WHERE session_id=? "
                                        "AND kind='work_success_end'", (sid,)).fetchone()[0]
            seen['status'] = repo.get_session(conn, sid)['status']
            return real(conn, path, sid)
        with patch.object(repo, 'release_lock', spy):
            s1, _ = await self.run_message('one')
        self.assertEqual(seen, {'snap': 1, 'status': 'SUCCEEDED'})

    async def test_end_snapshot_persistence_failure_is_not_a_resume_source(self):
        real = repo.save_snapshot

        def boom(conn, sid, aid, kind, data):
            if kind == 'work_success_end':
                raise sqlite3.OperationalError('injected')
            return real(conn, sid, aid, kind, data)
        with patch.object(repo, 'save_snapshot', boom):
            s1, _ = await self.run_message('one')
        self.assertEqual(self.status(s1), 'FAILED')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_events WHERE session_id=? "
                                           "AND kind='work_success_end'", (s1,)).fetchone()[0], 0)
        self.assertEqual([e['type'] for e in rows(self.conn, 'SELECT type FROM work_events WHERE work_id=?', self.wid)
                          if e['type'] == 'run_succeeded'], [])

    def test_cancel_and_dispatch_race_is_single_winner(self):
        p = self.policy
        m, _ = ws.add_message(self.conn, self.wid, str(uuid4()), 'x', digest(p), p, digest(p))
        ws.cancel_message(self.conn, self.wid, m['id'])
        self.assertEqual(ws.dispatch_next(self.conn, self.wid), (None, 'no_pending'))
        m2, _ = ws.add_message(self.conn, self.wid, str(uuid4()), 'y', digest(p), p, digest(p))
        sid, _ = ws.dispatch_next(self.conn, self.wid)
        with self.assertRaises(ws.WorkError) as err:
            ws.cancel_message(self.conn, self.wid, m2['id'])
        self.assertEqual(err.exception.code, 'message_linked')

    def test_concurrent_dispatch_creates_one_session(self):
        p = self.policy
        ws.add_message(self.conn, self.wid, str(uuid4()), 'x', digest(p), p, digest(p))

        def claim(_):
            c = store.connect(self.db)
            try:
                return ws.dispatch_next(c, self.wid)[0]
            except Exception as exc:
                return type(exc).__name__
            finally:
                c.close()
        with ThreadPoolExecutor(6) as pool:
            got = list(pool.map(claim, range(6)))
        self.assertEqual(len([g for g in got if g and g.startswith('sess_')]), 1, got)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM work_runs').fetchone()[0], 1)


class PromptBuilderTests(unittest.TestCase):
    def ctx(self, content, seq=1):
        return {'work': {'id': 'w'}, 'message': {'id': 'm', 'seq': seq, 'content': content}, 'first_goal': 'goal'}

    def test_exact_limit_passes_one_byte_over_fails(self):
        base, _ = work_prompt.build('FRESH_AUTHOR', ctx=self.ctx(''))
        room = work_prompt.PROMPT_LIMIT - work_prompt.size(base)
        prompt, meta = work_prompt.build('FRESH_AUTHOR', ctx=self.ctx('a' * room))
        self.assertEqual(meta['bytes'], work_prompt.PROMPT_LIMIT)
        with self.assertRaises(work_prompt.PromptTooLarge):
            work_prompt.build('FRESH_AUTHOR', ctx=self.ctx('a' * (room + 1)))
        with self.assertRaises(work_prompt.PromptTooLarge):
            work_prompt.gate(prompt + 'x')

    def test_history_is_deterministic_bounded_and_utf8_safe(self):
        runs = [{'session_id': f's{i}', 'message_id': f'm{i}', 'seq': i, 'content': 'çğüş🙂' * 3000,
                 'author_result': 'Ö' * 30000, 'review': 'r' * 1000} for i in range(3, 0, -1)]
        one = work_prompt.build('FRESH_AUTHOR', ctx=self.ctx('now', seq=4), prev_runs=runs)
        two = work_prompt.build('FRESH_AUTHOR', ctx=self.ctx('now', seq=4), prev_runs=runs)
        self.assertEqual(one, two)
        prompt, meta = one
        self.assertLessEqual(meta['history']['bytes'], work_prompt.HISTORY_LIMIT)
        self.assertTrue(meta['history']['truncated'])
        prompt.encode('utf-8').decode('utf-8')                            # no broken code points
        package = json.loads(prompt.split('HISTORY (data, not new authority):\n', 1)[1])
        cut = [i for i in package['items'] if i['included'] == 'truncated']
        self.assertTrue(all(i['omitted_bytes'] > 0 and len(i['sha256']) == 64 for i in cut))
        self.assertEqual([i['included'] for i in package['items']][:1], ['full'])   # newest output first

    def test_resume_prompt_has_no_history(self):
        prompt, meta = work_prompt.build('RESUME_AUTHOR', ctx=self.ctx('follow-up', seq=2),
                                         prev_runs=[{'session_id': 's', 'message_id': 'm0', 'seq': 1,
                                                     'content': 'OLD REQUEST', 'author_result': 'x', 'review': 'y'}])
        self.assertNotIn('OLD REQUEST', prompt)
        self.assertNotIn('history', meta)


# ── HTTP API + dispatcher (real app lifespan, scripted adapters) ────────────

class WorkApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.system').mkdir()
        self.db = self.root / '.system/state.db'
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        self.dirs = []
        self.app = create_app()
        self.log, self.scripts = [], {}

    def folder(self):
        d = self.root / f'ws{len(self.dirs)}'; d.mkdir(); self.dirs.append(d); return str(d)

    def client(self):
        c = TestClient(self.app)
        c.__enter__(); self.addCleanup(c.__exit__, None, None, None)
        self.app.state.runner.adapter_factory = Scripted(self.log, self.scripts)
        self.digest = c.get('/api/sessions/providers').json()['digest']
        return c

    def work(self, c, folder=None, title='w'):
        r = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': title, 'workspace_path': folder or self.folder()})
        self.assertEqual(r.status_code, 201, r.text); return r.json()['id']

    def send(self, c, wid, text='do it', cmid=None, dig=None):
        return c.post(f'/api/works/{wid}/messages', json={'client_message_id': cmid or str(uuid4()),
                                                          'text': text, 'policy_digest': dig or self.digest})

    def wait(self, fn, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(.05)
        self.fail('condition not reached')

    def messages(self, c, wid):
        return c.get(f'/api/works/{wid}/messages').json()['items']

    def test_create_and_message_idempotency(self):
        c = self.client(); folder = self.folder(); cid = str(uuid4())
        body = {'client_work_id': cid, 'title': 't', 'workspace_path': folder}
        self.assertEqual(c.post('/api/works', json=body).status_code, 201)
        self.assertEqual(c.post('/api/works', json=body).status_code, 200)
        r = c.post('/api/works', json=dict(body, title='other'))
        self.assertEqual((r.status_code, r.json()['code']), (409, 'IDEMPOTENCY_CONFLICT'))
        self.scripts['delay'] = 0.5
        mid = str(uuid4())
        first = self.send(c, cid, 'hello', mid)
        self.assertEqual(first.status_code, 201)
        again = self.send(c, cid, 'hello', mid)
        self.assertEqual((again.status_code, again.json()['id']), (200, first.json()['id']))
        conflict = self.send(c, cid, 'changed', mid)
        self.assertEqual((conflict.status_code, conflict.json()['code']), (409, 'IDEMPOTENCY_CONFLICT'))
        other = self.work(c)
        self.assertEqual(self.send(c, other, 'hello', mid).status_code, 201)       # UUID is per work
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\nmax_context_continuations: 1\n')
        self.assertEqual(self.send(c, cid, 'hello', mid).status_code, 200)         # replay survives policy change
        stale = self.send(c, cid, 'new one')
        self.assertEqual(stale.status_code, 409)                                     # new message with stale digest

    def test_replay_survives_invalid_and_unreadable_policy(self):
        c = self.client(); wid = self.work(c); mid = str(uuid4())
        first = self.send(c, wid, 'accepted', mid)
        self.assertEqual(first.status_code, 201)
        self.wait(lambda: self.messages(c, wid)[0]['session'] and
                  self.messages(c, wid)[0]['session']['status'] == 'SUCCEEDED')
        (self.root / '.system/providers.yaml').write_text('roles: [')
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                # Both malformed YAML and a file-read error must be irrelevant to replay.
                with patch('app.works_api.load_config', side_effect=OSError('unreadable')) if unreadable else patch('app.works_api._wake'):
                    replay = self.send(c, wid, 'accepted', mid)
                    self.assertEqual(replay.status_code, 200, replay.text)
                    self.assertEqual(replay.json()['id'], first.json()['id'])
                    conflict = self.send(c, wid, 'changed', mid)
                    self.assertEqual(conflict.status_code, 409)
                    self.assertEqual(conflict.json()['code'], 'IDEMPOTENCY_CONFLICT')
                    self.assertEqual(self.send(c, wid, 'new message').status_code, 422)
        conn = store.connect(self.db)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_messages').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_runs').fetchone()[0], 1)
        conn.close()

    def test_concurrent_same_uuid_single_message_and_session(self):
        c = self.client(); wid = self.work(c); mid = str(uuid4())
        with ThreadPoolExecutor(4) as pool:
            codes = sorted(pool.map(lambda _: self.send(c, wid, 'same', mid).status_code, range(4)))
        self.assertEqual(codes, [200, 200, 200, 201])
        self.wait(lambda: self.messages(c, wid)[0]['session'] and self.messages(c, wid)[0]['session']['status'] == 'SUCCEEDED')
        conn = store.connect(self.db)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_messages').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_runs').fetchone()[0], 1)
        conn.close()

    def test_queue_while_active_then_single_dispatch_each(self):
        c = self.client(); wid = self.work(c); self.scripts['delay'] = 0.4
        self.send(c, wid, 'first')
        self.wait(lambda: self.messages(c, wid)[0]['status'] == 'LINKED')
        self.send(c, wid, 'second')
        self.assertEqual(self.messages(c, wid)[1]['status'], 'PENDING')              # queued, active prompt untouched
        self.wait(lambda: all(m['session'] and m['session']['status'] == 'SUCCEEDED' for m in self.messages(c, wid)))
        conn = store.connect(self.db)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_runs').fetchone()[0], 2)
        conn.close()

    def test_two_works_same_folder_single_writer(self):
        c = self.client(); folder = self.folder(); self.scripts['delay'] = 0.4
        a, b = self.work(c, folder), self.work(c, folder)
        self.send(c, a, 'a1')
        self.wait(lambda: self.messages(c, a)[0]['status'] == 'LINKED')
        self.send(c, b, 'b1')
        events = lambda: [e for e in self.events(c, b) if e['type'] == 'dispatch_waiting']
        self.assertEqual(self.wait(events)[0]['payload']['reason'], 'workspace_busy')
        self.wait(lambda: self.messages(c, b)[0]['session'] and self.messages(c, b)[0]['session']['status'] == 'SUCCEEDED')

    def events(self, c, wid, **headers):
        text = c.get(f'/api/works/{wid}/events?until_current=true', headers=headers).text
        return [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]

    def test_failed_run_blocks_queue_until_explicit_continue(self):
        c = self.client(); wid = self.work(c); self.scripts['codex'] = [failed()]
        self.send(c, wid, 'first')
        self.wait(lambda: self.messages(c, wid)[0]['session'] and self.messages(c, wid)[0]['session']['status'] == 'FAILED')
        self.send(c, wid, 'second'); time.sleep(.3)
        self.assertEqual(self.messages(c, wid)[1]['status'], 'PENDING')
        self.assertEqual(c.get(f'/api/works/{wid}').json()['blocked_reason'], 'needs_continue')
        sid = self.messages(c, wid)[0]['session']['id']
        self.assertEqual(c.post(f'/api/works/{wid}/continue', json={'expected_session_id': sid}).status_code, 422)
        stale = c.post(f'/api/works/{wid}/continue', json={'confirm': True, 'expected_session_id': 'nope'})
        self.assertEqual((stale.status_code, stale.json()['code']), (409, 'stale_session'))
        self.assertEqual(c.post(f'/api/works/{wid}/continue', json={'confirm': True, 'expected_session_id': sid}).status_code, 200)
        self.wait(lambda: self.messages(c, wid)[1]['session'] and self.messages(c, wid)[1]['session']['status'] == 'SUCCEEDED')

    def test_cancel_archive_patch_and_cross_site(self):
        c = self.client(); wid = self.work(c); self.scripts['delay'] = 0.4
        self.send(c, wid, 'first')
        self.wait(lambda: self.messages(c, wid)[0]['status'] == 'LINKED')
        linked = self.messages(c, wid)[0]['id']
        self.assertEqual(c.post(f'/api/works/{wid}/messages/{linked}/cancel').status_code, 409)
        pending = self.send(c, wid, 'second').json()['id']
        rev = c.get(f'/api/works/{wid}').json()['revision']
        blocked = c.patch(f'/api/works/{wid}', json={'expected_revision': rev, 'archived': True})
        self.assertEqual((blocked.status_code, blocked.json()['code']), (409, 'work_active'))
        self.assertEqual(c.post(f'/api/works/{wid}/messages/{pending}/cancel').json()['status'], 'CANCELLED')
        self.assertEqual(c.post(f'/api/works/{wid}/messages/{pending}/cancel').status_code, 200)   # idempotent
        self.assertEqual(c.patch(f'/api/works/{wid}', json={'expected_revision': rev + 99, 'title': 'x'}).json()['code'], 'revision_conflict')
        conn = store.connect(self.db); before = conn.execute('SELECT count(*) FROM work_events').fetchone()[0]; conn.close()
        evil = {'origin': 'https://evil.invalid'}
        for method, path, body in [('post', '/api/works', {'client_work_id': str(uuid4()), 'title': 't', 'workspace_path': self.folder()}),
                                   ('post', f'/api/works/{wid}/messages', {'client_message_id': str(uuid4()), 'text': 'x', 'policy_digest': self.digest}),
                                   ('post', f'/api/works/{wid}/messages/{pending}/cancel', None),
                                   ('patch', f'/api/works/{wid}', {'expected_revision': rev, 'title': 'x'}),
                                   ('post', f'/api/works/{wid}/continue', {'confirm': True, 'expected_session_id': 'x'})]:
            r = getattr(c, method)(path, json=body, headers=evil)
            self.assertEqual(r.status_code, 403, path)
        conn = store.connect(self.db)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_events').fetchone()[0], before)
        conn.close()

    def test_waiting_work_frees_folder_and_its_retry_waits_for_other_work(self):
        c = self.client(); folder = self.folder()
        a, b = self.work(c, folder), self.work(c, folder)
        self.scripts['codex'] = [failed('QUOTA_EXCEEDED')]
        self.send(c, a, 'a1')
        self.wait(lambda: self.messages(c, a)[0]['session'] and self.messages(c, a)[0]['session']['status'] == 'WAITING_CAPACITY')
        a_sid = self.messages(c, a)[0]['session']['id']
        self.assertEqual(c.get(f'/api/works/{a}').json()['blocked_reason'], 'run_waiting')
        self.scripts['delay'] = 0.6
        self.send(c, b, 'b1')
        self.wait(lambda: self.messages(c, b)[0]['session'] and self.messages(c, b)[0]['session']['status'] == 'RUNNING')
        r = c.post(f'/api/sessions/{a_sid}/retry')
        self.assertEqual(r.status_code, 409, r.text)
        self.wait(lambda: self.messages(c, b)[0]['session']['status'] == 'SUCCEEDED')
        self.assertEqual(c.post(f'/api/sessions/{a_sid}/retry').status_code, 202)
        self.wait(lambda: self.messages(c, a)[0]['session']['status'] == 'SUCCEEDED')

    def test_archived_work_rejects_new_message_but_replays_old(self):
        c = self.client(); wid = self.work(c); mid = str(uuid4())
        self.send(c, wid, 'first', mid)
        self.wait(lambda: self.messages(c, wid)[0]['session'] and self.messages(c, wid)[0]['session']['status'] == 'SUCCEEDED')
        rev = c.get(f'/api/works/{wid}').json()['revision']
        self.assertEqual(c.patch(f'/api/works/{wid}', json={'expected_revision': rev, 'archived': True}).status_code, 200)
        r = self.send(c, wid, 'after archive')
        self.assertEqual((r.status_code, r.json()['code']), (409, 'work_archived'))
        self.assertEqual(self.send(c, wid, 'first', mid).status_code, 200)
        self.assertEqual(c.get('/api/works').json()['counts'], {'active': 0, 'archived': 1})

    def test_list_search_by_title_or_folder(self):   # K-059
        c = self.client()
        conn = store.connect(self.db)
        reels = self.root / 'ReelsVideo'; reels.mkdir()
        ws.create_work(conn, str(uuid4()), 'Hesap makinesi', self.folder())
        ws.create_work(conn, str(uuid4()), 'Kapak', str(reels))
        ws.create_work(conn, str(uuid4()), '100% done_x', self.folder())
        conn.close()
        titles = lambda q: sorted(w['title'] for w in c.get('/api/works', params={'q': q}).json()['items'])
        self.assertEqual(titles('makine'), ['Hesap makinesi'])
        self.assertEqual(titles('reels'), ['Kapak'])                 # the folder, case-insensitive
        self.assertEqual(titles('0%'), ['100% done_x'])               # % and _ are literal, not wildcards
        self.assertEqual(titles('e_x'), ['100% done_x']); self.assertEqual(titles('x_d'), [])
        page = c.get('/api/works', params={'q': 'makine'}).json()
        self.assertEqual((page['total'], page['counts']['active']), (1, 3))   # the tab counts stay whole
        self.assertEqual(len(titles('  ')), 3)

    def test_list_paging_same_timestamp_counts_and_legacy_filter(self):
        c = self.client()
        conn = store.connect(self.db)
        for i in range(5):
            ws.create_work(conn, f'00000000-0000-0000-0000-00000000000{i}', f't{i}', self.folder())
        conn.execute("UPDATE works SET created_at='2026-01-01T00:00:00+00:00'")      # identical timestamps
        conn.execute("UPDATE works SET archived_at='x' WHERE id LIKE '%4'")
        repo.create_session(conn, 'legacy1', self.folder(), 'auto'); repo.update_session_status(conn, 'legacy1', 'SUCCEEDED')
        conn.close()
        seen, cursor = [], None
        while True:
            page = c.get('/api/works', params={'limit': 2, **({'cursor': cursor} if cursor else {})}).json()
            self.assertEqual((page['total'], page['counts']), (4, {'active': 4, 'archived': 1}))
            seen += [w['id'] for w in page['items']]; cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual(len(seen), 4); self.assertEqual(len(set(seen)), 4)
        wid = seen[0]; self.send(c, wid, 'linked run')
        self.wait(lambda: self.messages(c, wid)[0]['status'] == 'LINKED')
        linked = self.messages(c, wid)[0]['session']['id']
        legacy, cursor = [], None
        while True:
            r = c.get('/api/sessions', params={'unlinked_only': 'true', 'limit': 1, **({'cursor': cursor} if cursor else {})})
            legacy += [s['id'] for s in r.json()]; cursor = r.headers.get('x-next-cursor')
            if not cursor:
                break
        self.assertIn('legacy1', legacy); self.assertNotIn(linked, legacy)
        self.assertIn(linked, [s['id'] for s in c.get('/api/sessions').json()])        # unfiltered stays compatible

    def test_events_replay_and_last_event_id(self):
        c = self.client(); wid = self.work(c)
        all_events = self.events(c, wid)
        self.assertEqual(all_events[0]['type'], 'work_created')
        self.assertEqual(self.events(c, wid, **{'Last-Event-ID': str(all_events[-1]['seq'])}), [])


class RestartTests(unittest.TestCase):
    def test_queued_linked_session_needs_recovery_pending_message_dispatches(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); (root / '.system').mkdir(); db = root / '.system/state.db'
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root)}); env.start(); self.addCleanup(env.stop)
        a_dir, b_dir = root / 'a', root / 'b'; a_dir.mkdir(); b_dir.mkdir()
        conn = store.connect(db); store.migrate(conn); policy = normalize(DEFAULT)
        a, b = str(uuid4()), str(uuid4())
        ws.create_work(conn, a, 'a', str(a_dir)); ws.create_work(conn, b, 'b', str(b_dir))
        ws.add_message(conn, a, str(uuid4()), 'committed but never spawned', digest(policy), policy, digest(policy))
        crashed, _ = ws.dispatch_next(conn, a)                       # commit, then "crash" before spawn
        ws.add_message(conn, b, str(uuid4()), 'still pending', digest(policy), policy, digest(policy))
        conn.close()
        app = create_app(); log = []
        with TestClient(app) as client:
            app.state.runner.adapter_factory = Scripted(log, {})
            self.assertEqual(client.get(f'/api/sessions/{crashed}').json()['session']['status'], 'RECOVERY_REQUIRED')
            end = time.monotonic() + 10
            while time.monotonic() < end:
                items = client.get(f'/api/works/{b}/messages').json()['items']
                if items[0]['session'] and items[0]['session']['status'] == 'SUCCEEDED':
                    break
                time.sleep(.05)
            self.assertEqual(items[0]['session']['status'], 'SUCCEEDED')
            self.assertEqual(client.get(f'/api/works/{a}').json()['blocked_reason'], 'recovery_required')
        conn = store.connect(db)
        self.assertEqual(conn.execute('SELECT count(*) FROM work_runs WHERE work_id=?', (a,)).fetchone()[0], 1)  # no blind replay
        conn.close()


if __name__ == '__main__':
    unittest.main()
