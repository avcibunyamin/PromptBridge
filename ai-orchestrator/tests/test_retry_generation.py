"""TB-02: human retry after WAITING_* really re-runs (explicit retry generation), guards kept."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from app import store, runner_store as repo
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import normalize
from app.runner import SessionRunner


def ok(text='done'):
    return AdapterResult(text, None, None, 'stop', False, 'completed', 0)


def err(code):
    return AdapterResult('broken', 'thread-x', None, 'error', True, 'failed', 1, code, {'code': code})


class Script:
    """Per-call scripted results; records (provider, provider_session_id) for every call."""
    def __init__(self, *results):
        self.results, self.calls, self.n = list(results), [], 0

    def __call__(self, provider):
        outer = self

        class Adapter:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                outer.n += 1
                outer.calls.append((provider, provider_session_id))
                result = outer.results.pop(0) if outer.results else ok()
                if not result.is_error and not result.provider_session_id:
                    result.provider_session_id = f'{provider}-thread-{outer.n}'
                owner = {'pid': 500 + outer.n, 'pgid': 500 + outer.n, 'start_time': 1.0, 'boot_time': 1.0}
                yield {'type': 'process_started', 'ownership': owner}
                yield {'type': 'started', 'provider_session_id': result.provider_session_id}
                yield {'type': 'process_stopped', 'ownership': owner}
                yield {'type': 'completed' if result.successful else 'failed', 'result': result}
        return Adapter()


class RetryGeneration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.ws = Path(self.tmp.name) / 'ws'; self.ws.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)

    def create(self, sid='s', author=('codex',), continuations=3):
        policy = normalize({'roles': {'author': list(author), 'reviewer': ['claude']},
                            'max_context_continuations': continuations})
        repo.create_session(self.conn, sid, str(self.ws), 'auto', policy=policy)
        for i, agent in enumerate((author[0], 'claude', author[0])):
            repo.add_session_step(self.conn, f'{sid}_{i}', sid, i, agent, f'task {i}')
        return sid

    async def execute(self, script, sid="s"):
        await SessionRunner(self.db, adapter_factory=script).run_session(sid)
        return repo.get_session(self.conn, sid)['status']

    def attempts(self, step):
        return [dict(r) for r in self.conn.execute('SELECT * FROM session_attempts WHERE step_id=? ORDER BY attempt_no', (step,))]

    async def test_capacity_and_auth_retry_really_rerun_to_success(self):
        for code, waiting in (('QUOTA_EXCEEDED', 'WAITING_CAPACITY'), ('AUTH', 'WAITING_AUTH')):
            with self.subTest(code=code):
                sid = self.create(f's-{code}')
                self.assertEqual(await self.execute(Script(err(code)), sid), waiting)
                old = self.attempts(f'{sid}_0')
                repo.retry_session(self.conn, sid)
                script = Script()
                self.assertEqual(await self.execute(script, sid), 'SUCCEEDED')
                rows = self.attempts(f'{sid}_0')
                self.assertEqual(rows[0], old[0])                                   # history not rewritten
                self.assertEqual([(r['attempt_no'], r['provider'], r['retry_generation'], r['continuation_no'])
                                  for r in rows], [(1, 'codex', 0, 0), (2, 'codex', 1, 0)])
                self.assertIsNone(script.calls[0][1])                               # retried step starts fresh
                audit = [json.loads(r[0]) for r in self.conn.execute(
                    "SELECT payload FROM session_events WHERE session_id=? AND kind='human_retry'", (sid,))]
                self.assertEqual([(a['from_state'], a['from_generation'], a['retry_generation']) for a in audit],
                                 [(waiting, 0, 1)])

    async def test_retry_of_a_later_step_starts_fresh_not_in_the_earlier_author_thread(self):
        sid = self.create()
        first = Script(ok('step 1'), ok('review'), err('QUOTA_EXCEEDED'))    # step 3 (author) hits quota
        self.assertEqual(await self.execute(first), 'WAITING_CAPACITY')
        self.assertEqual(first.calls[2], ('codex', 'codex-thread-1'))          # normal flow: same author thread
        repo.retry_session(self.conn, sid)
        again = Script()
        self.assertEqual(await self.execute(again), 'SUCCEEDED')
        self.assertEqual(again.calls, [('codex', None)])                       # only step 3 re-ran, fresh

    async def test_same_generation_still_has_one_root_per_provider(self):
        sid = self.create()
        await self.execute(Script(err('QUOTA_EXCEEDED')))
        with self.assertRaises(sqlite3.IntegrityError):   # the pre-TB-02 quota guard is intact
            self.conn.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,retry_generation) "
                              "VALUES ('dup','s_0',9,'codex','WRITE','t','RUNNING',0)")

    async def test_double_and_concurrent_retry_create_one_generation(self):
        sid = self.create()
        await self.execute(Script(err('QUOTA_EXCEEDED')))
        repo.retry_session(self.conn, sid)
        with self.assertRaises(repo.RetryNotAllowed) as again:
            repo.retry_session(self.conn, sid)
        self.assertEqual(again.exception.code, 'session_not_retryable')
        self.assertEqual(self.conn.execute("SELECT retry_generation FROM session_steps WHERE id='s_0'").fetchone()[0], 1)
        # Concurrent: a second session in the same shape, retried from 6 connections at once.
        sid2 = 's2'; self.ws2 = self.ws; await self.execute(Script(ok(), ok(), ok()), sid)   # finish s, free folder
        self.create(sid2)
        await self.execute(Script(err('QUOTA_EXCEEDED')), sid2)

        def attempt(_):
            c = store.connect(self.db)
            try:
                repo.retry_session(c, sid2); return 'ok'
            except repo.RetryNotAllowed as exc:
                return exc.code
            finally:
                c.close()
        with ThreadPoolExecutor(6) as pool:
            got = list(pool.map(attempt, range(6)))
        self.assertEqual(got.count('ok'), 1, got)
        self.assertEqual(self.conn.execute("SELECT retry_generation FROM session_steps WHERE id='s2_0'").fetchone()[0], 1)

    async def test_waiting_context_retry_respects_the_shared_budget(self):
        sid = self.create(continuations=0)                      # no allowance at all
        from tests.test_context import context
        self.assertEqual(await self.execute(Script(context())), 'WAITING_CONTEXT')
        with self.assertRaises(repo.RetryNotAllowed) as exhausted:
            repo.retry_session(self.conn, sid)
        self.assertEqual(exhausted.exception.code, 'context_budget_exhausted')
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'WAITING_CONTEXT')   # nothing changed

    async def test_waiting_context_retry_with_allowance_reruns(self):
        sid = self.create(continuations=1)
        await self.execute(Script(err('QUOTA_EXCEEDED')))           # a clean stop with a FAILED step ...
        self.conn.execute("UPDATE sessions SET status='WAITING_CONTEXT' WHERE id=?", (sid,))   # ... filed as context
        repo.retry_session(self.conn, sid)
        self.assertEqual(await self.execute(Script()), 'SUCCEEDED')

    async def test_continuation_parent_must_be_in_the_current_generation(self):
        sid = self.create(continuations=2)
        repo.claim_session(self.conn, sid); repo.update_step_status(self.conn, 's_0', 'RUNNING')
        from tests.test_context import context
        repo.begin_attempt(self.conn, 'parent', 's_0', 1, 'codex', 'WRITE')
        repo.end_attempt(self.conn, 'parent', 'CONTEXT_EXHAUSTED', context())
        owner = {'pid': 1, 'pgid': 1, 'start_time': 1, 'boot_time': 1}
        repo.record_owner(self.conn, 'parent', owner); repo.record_owner(self.conn, 'parent', owner, stopped=True)
        self.conn.execute("UPDATE session_steps SET retry_generation=1 WHERE id='s_0'")   # a later generation
        with self.assertRaisesRegex(ValueError, 'invalid continuation parent'):
            repo.prepare_continuation(self.conn, 'child', 'parent', 's_0', 'codex', 'WRITE', {'x': 1})

    async def test_retry_refused_while_folder_busy_or_cleanup_unresolved(self):
        sid = self.create()
        await self.execute(Script(err('QUOTA_EXCEEDED')))
        repo.create_session(self.conn, 'other', str(self.ws), 'auto')    # QUEUED in the same folder
        with self.assertRaises(repo.RetryNotAllowed) as busy:
            repo.retry_session(self.conn, sid)
        self.assertEqual(busy.exception.code, 'workspace_busy')
        repo.update_session_status(self.conn, 'other', 'SUCCEEDED')
        self.conn.execute("UPDATE process_owners SET status='RUNNING' WHERE attempt_id IN "
                          "(SELECT id FROM session_attempts WHERE step_id='s_0')")
        with self.assertRaises(repo.RetryNotAllowed) as dirty:
            repo.retry_session(self.conn, sid)
        self.assertEqual(dirty.exception.code, 'recovery_required')


class MigrationFrom13(unittest.TestCase):
    def test_real_1_3_schema_with_attempt_chain_upgrades_losslessly(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        c = store.connect(Path(tmp.name) / 'old.db')
        c.executescript((Path(__file__).parent / 'fixtures/schema_1_3.sql').read_text())
        c.execute("INSERT OR REPLACE INTO schema_meta VALUES ('schema_version','1.3')")
        c.execute("INSERT INTO sessions VALUES ('s','/w','auto','WAITING_CONTEXT','t','t')")
        c.execute("INSERT INTO session_steps(id,session_id,step_index,agent_name,task_prompt,status) VALUES ('s0','s',0,'codex','g','FAILED')")
        c.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status) VALUES ('a1','s0',1,'codex','WRITE','t','CONTEXT_EXHAUSTED')")
        c.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,continuation_no,continuation_of) VALUES ('a2','s0',2,'codex','WRITE','t','FAILED',1,'a1')")
        before = [dict(r) for r in c.execute('SELECT * FROM session_attempts ORDER BY id')]
        c.commit()
        store.migrate(c)
        after = [dict(r) for r in c.execute('SELECT * FROM session_attempts ORDER BY id')]
        self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual([{k: v for k, v in r.items() if k != 'retry_generation'} for r in after], before)
        self.assertEqual({r['retry_generation'] for r in after}, {0})
        self.assertEqual(c.execute("SELECT retry_generation FROM session_steps").fetchone()[0], 0)
        self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        store.migrate(c)                                                             # idempotent
        self.assertEqual([dict(r) for r in c.execute('SELECT * FROM session_attempts ORDER BY id')], after)
        c.close()

    def test_injected_failure_leaves_1_3_untouched(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        c = store.connect(Path(tmp.name) / 'old.db')
        c.executescript((Path(__file__).parent / 'fixtures/schema_1_3.sql').read_text())
        c.execute("INSERT OR REPLACE INTO schema_meta VALUES ('schema_version','1.3')"); c.commit()
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE name='session_attempts'").fetchone()[0]
        with patch.object(store, '_rebuild_attempts', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                store.migrate(c)
        self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], '1.3')
        self.assertEqual(c.execute("SELECT sql FROM sqlite_master WHERE name='session_attempts'").fetchone()[0], ddl)
        self.assertNotIn('retry_generation', {r[1] for r in c.execute('PRAGMA table_info(session_steps)')})
        c.close()


class RetryApi(unittest.TestCase):
    def test_refusal_is_a_flat_409_with_a_code(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); (root / '.system').mkdir(); (root / 'ws').mkdir()
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root)}); env.start(); self.addCleanup(env.stop)
        conn = store.connect(root / '.system/state.db'); store.migrate(conn)
        repo.create_session(conn, 's', str(root / 'ws'), 'auto'); repo.update_session_status(conn, 's', 'SUCCEEDED'); conn.close()
        with TestClient(create_app()) as client:
            r = client.post('/api/sessions/s/retry')
        self.assertEqual((r.status_code, r.json()), (409, {'detail': 'session is not retryable', 'code': 'session_not_retryable'}))


if __name__ == '__main__':
    unittest.main()
