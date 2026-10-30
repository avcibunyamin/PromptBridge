import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from app import store, runner_store as repo
from app.runner import SessionRunner, _get_adapter
from app.adapters.base import AdapterResult


def success(text='done', sid='provider-1'):
    return AdapterResult(text, sid, None, 'stop', False, 'completed', 0)


class Fake:
    def __init__(self, result=None, delay=0, events=None):
        self.result = result or success()
        self.delay = delay
        self.events = events
        self.closed = False
        self.calls = []
    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        self.calls.append((prompt, provider_session_id))
        try:
            yield {'type':'process_started','ownership':{'pid':100,'pgid':100,'start_time':1.0,'boot_time':1.0}}
            yield {'type': 'started', 'provider_session_id': provider_session_id}
            await asyncio.sleep(self.delay)
            yield {'type':'process_stopped','ownership':{'pid':100,'pgid':100,'start_time':1.0,'boot_time':1.0}}
            for event in self.events if self.events is not None else [
                    {'type': 'completed', 'result': self.result}]:
                yield event
        finally:
            self.closed = True


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def create(self, sid='s', agents=('codex',), workspace=None):
        with store.transaction(self.conn):
            repo.create_session(self.conn, sid, workspace or self.tmp.name, 'auto')
            for i, agent in enumerate(agents):
                repo.add_session_step(self.conn, f'{sid}-{i}', sid, i, agent, f'goal {i}')
        return sid

    def status(self):
        return repo.get_session(self.conn, 's')['status']

    async def run_fake(self, fake):
        self.create()
        await SessionRunner(self.db, adapter_factory=lambda _: fake).run_session('s')

    async def test_success_and_no_replay(self):
        fake = Fake()
        await self.run_fake(fake)
        await SessionRunner(self.db, adapter_factory=lambda _: fake).run_session('s')
        self.assertEqual(self.status(), 'SUCCEEDED')
        self.assertEqual(len(fake.calls), 1)
        step = repo.get_session_steps(self.conn, 's')[0]
        self.assertIsNotNone(step['started_at'])
        self.assertIsNotNone(step['completed_at'])

    async def failing_adapter(self, exc):
        class Failing:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                yield {'type':'process_started','ownership':{'pid':100,'pgid':100,'start_time':1.0,'boot_time':1.0}}
                yield {'type':'started','provider_session_id':'t'}
                raise exc   # adapter's own finally has already stopped/verified (or failed to)
        self.create()
        await SessionRunner(self.db, adapter_factory=lambda _: Failing()).run_session('s')
        owner = self.conn.execute('SELECT status FROM process_owners').fetchone()[0]
        locks = self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0]
        return owner, locks

    async def test_adapter_error_after_verified_cleanup_marks_owner_stopped(self):
        # F2-05: an EOF/protocol error used to leave process_owners RUNNING (stale audit).
        owner, locks = await self.failing_adapter(ValueError('Codex EOF before terminal result'))
        self.assertEqual((self.status(), owner, locks), ('FAILED', 'STOPPED', 0))

    async def test_stop_receipt_mismatch_logs_without_masking_adapter_error(self):
        record_owner = repo.record_owner
        def mismatch(conn, aid, owner, *, stopped=False):
            if stopped:
                raise ValueError('private-receipt-value')
            return record_owner(conn, aid, owner)
        with patch.object(repo, 'record_owner', side_effect=mismatch):
            with self.assertLogs('app.runner', level='WARNING') as logs:
                await self.failing_adapter(ValueError('original-adapter-error'))
        self.assertIn('ownership mismatch', logs.output[0])
        self.assertNotIn('private-receipt-value', ''.join(logs.output))
        self.assertNotIn('original-adapter-error', ''.join(logs.output))
        step = repo.get_session_steps(self.conn, 's')[0]
        self.assertIn('original-adapter-error', step['terminal_reason'])

    async def test_unverified_cleanup_keeps_owner_running_and_lock(self):
        from app.adapters.process import ProcessCleanupError
        owner, locks = await self.failing_adapter(ProcessCleanupError('group remains'))
        self.assertEqual((self.status(), owner, locks), ('RECOVERY_REQUIRED', 'RUNNING', 1))

    async def test_empty_stream_fails(self):
        await self.run_fake(Fake(events=[]))
        self.assertEqual(self.status(), 'FAILED')

    async def test_duplicate_terminal_fails(self):
        event = {'type': 'completed', 'result': success()}
        await self.run_fake(Fake(events=[event, event]))
        self.assertEqual(self.status(), 'FAILED')

    async def test_error_result_stops_following_step(self):
        self.create(agents=('codex', 'claude'))
        bad = success(); bad.is_error = True
        await SessionRunner(self.db, adapter_factory=lambda _: Fake(bad)).run_session('s')
        self.assertEqual(self.status(), 'FAILED')
        self.assertEqual(repo.get_session_steps(self.conn, 's')[1]['status'], 'PENDING')

    async def test_nonzero_exit_is_failure(self):
        bad = success(); bad.exit_code = 1
        await self.run_fake(Fake(bad))
        self.assertEqual(self.status(), 'FAILED')

    async def test_cancel_is_prompt_and_closes_generator_before_unlock(self):
        self.create()
        fake = Fake(delay=30)
        runner = SessionRunner(self.db, adapter_factory=lambda _: fake)
        job = asyncio.create_task(runner.run_session('s'))
        while not fake.calls:
            await asyncio.sleep(.01)
        start = time.monotonic()
        self.assertTrue(runner.cancel_session('s'))
        await asyncio.wait_for(job, 1)
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(fake.closed)
        self.assertEqual(self.status(), 'CANCELLED')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 0)

    async def test_timeout_closes_generator(self):
        self.create()
        fake = Fake(delay=30)
        await SessionRunner(self.db, adapter_factory=lambda _: fake, step_timeout=.03).run_session('s')
        self.assertTrue(fake.closed)
        self.assertEqual(self.status(), 'WAITING_HUMAN')   # K-061: the last step ran out of time — resumable, not lost
        self.assertEqual(repo.get_session_steps(self.conn, 's')[0]['terminal_reason'], 'step_timeout')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 0)
        fake.delay = 0
        repo.retry_session(self.conn, 's')   # "Tekrar dene" runs the step again on the folder as it is
        await SessionRunner(self.db, adapter_factory=lambda _: fake, step_timeout=5).run_session('s')
        self.assertEqual(self.status(), 'SUCCEEDED')

    async def test_an_author_out_of_time_hands_over_to_the_reviewer(self):   # K-061
        self.create(agents=('codex', 'claude', 'codex'))
        slow, reviewer, author = Fake(delay=30), Fake(), Fake()
        order = iter([slow, reviewer, author])
        await SessionRunner(self.db, adapter_factory=lambda _: next(order), step_timeout=.05).run_session('s')
        self.assertEqual(self.status(), 'SUCCEEDED')
        steps = repo.get_session_steps(self.conn, 's')
        self.assertEqual([(s['status'], s['terminal_reason']) for s in steps][0], ('COMPLETED', 'step_timeout'))
        self.assertIn('TIME LIMIT REACHED', reviewer.calls[0][0])       # the reviewer is told, and reviews that state
        self.assertEqual(len(author.calls), 1)                          # the correction author continued
        event = json.loads(self.conn.execute("SELECT payload FROM session_events WHERE kind='step_timed_out'").fetchone()[0])
        self.assertEqual((event['role'], event['continued']), ('author', True))
        attempt = self.conn.execute("SELECT status, terminal_reason FROM session_attempts ORDER BY started_at LIMIT 1").fetchone()
        self.assertEqual(tuple(attempt), ('FAILED', 'step_timeout'))

    async def test_a_step_that_slept_past_its_limit_ends_on_wake(self):   # K-063
        self.create()
        fake = Fake(delay=30)
        from app.runner import _clock as real_clock
        with patch('app.runner.SLEEP_CHECK', .05), patch('app.runner._clock', side_effect=lambda: real_clock() + (0 if not fake.calls else 7200)):
            await asyncio.wait_for(SessionRunner(self.db, adapter_factory=lambda _: fake, step_timeout=3600).run_session('s'), 5)
        self.assertEqual(self.status(), 'WAITING_HUMAN')   # two hours "asleep": the step ended without its monotonic hour passing
        self.assertTrue(fake.closed)

    async def test_the_step_limit_comes_from_the_setting(self):   # K-061
        runner = SessionRunner(self.db, adapter_factory=lambda _: Fake())
        self.assertEqual(runner._step_seconds(), 45 * 60)
        from app import user_settings
        user_settings.path(self.db).write_text(json.dumps({'version': 1, 'step_minutes': 90}))
        self.assertEqual(runner._step_seconds(), 90 * 60)
        user_settings.path(self.db).write_text(json.dumps({'version': 1, 'step_minutes': 5}))   # out of range
        self.assertEqual(runner._step_seconds(), 45 * 60)

    async def test_same_session_cannot_run_twice(self):
        self.create()
        fake = Fake(delay=.1)
        a = SessionRunner(self.db, adapter_factory=lambda _: fake)
        b = SessionRunner(self.db, adapter_factory=lambda _: fake)
        await asyncio.gather(a.run_session('s'), b.run_session('s'))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.status(), 'SUCCEEDED')

    async def test_path_alias_rejected_at_creation(self):
        self.create()
        with self.assertRaises(repo.WorkspaceBusy):
            self.create('s2', workspace=self.tmp.name + '/./')

    async def test_lock_is_not_reentrant(self):
        self.create()
        self.assertTrue(repo.acquire_lock(self.conn, self.tmp.name, 's'))
        self.assertFalse(repo.acquire_lock(self.conn, self.tmp.name, 's'))

    async def test_three_steps_transfer_goal_review_and_resume_id(self):
        self.create(agents=('codex', 'claude', 'codex'))
        writer, reviewer = Fake(success('implementation', 'code-1')), Fake(success('review', 'review-1'))
        await SessionRunner(self.db, adapter_factory=lambda name: writer if name == 'codex' else reviewer).run_session('s')
        self.assertEqual(self.status(), 'SUCCEEDED')
        self.assertIn('goal 0', reviewer.calls[0][0])
        self.assertIn('\nimplementation', reviewer.calls[0][0])
        self.assertIn('\nreview', writer.calls[1][0])
        self.assertEqual(writer.calls[1][1], 'code-1')
        self.assertGreater(self.conn.execute('SELECT count(*) FROM session_events').fetchone()[0], 0)

    async def test_empty_work_fails(self):
        self.create(agents=())
        await SessionRunner(self.db).run_session('s')
        self.assertEqual(self.status(), 'FAILED')

    async def test_factory_not_patched_globally(self):
        from app.adapters.codex import CodexAdapter
        self.assertIsInstance(_get_adapter('codex', self.db), CodexAdapter)

    async def test_cleanup_failure_retains_lock_for_recovery(self):
        from app.adapters.process import ProcessCleanupError
        class BrokenCleanup:
            async def run(self, *args, **kwargs):
                try:
                    yield {'type':'started'}
                    await asyncio.sleep(30)
                finally:
                    raise ProcessCleanupError('test cleanup failure')
        self.create()
        await SessionRunner(self.db, adapter_factory=lambda _:BrokenCleanup(), step_timeout=.01).run_session('s')
        self.assertEqual(self.status(), 'RECOVERY_REQUIRED')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 1)

    async def test_auth_and_quota_failures_have_distinct_states(self):
        for sid, text, expected in [('auth','Not logged in','WAITING_AUTH'),
                                    ('quota','rate_limit exceeded','WAITING_CAPACITY')]:
            self.create(sid)
            result = AdapterResult(text, None, None, 'error', True, 'api_error', 1, 'AUTH' if sid=='auth' else 'QUOTA_EXCEEDED')
            await SessionRunner(self.db, adapter_factory=lambda _:Fake(result)).run_session(sid)
            self.assertEqual(repo.get_session(self.conn,sid)['status'], expected)
