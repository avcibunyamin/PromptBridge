import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from app import runner_store as repo, store, works_api, works_store as ws
from app.adapters.process import ProcessCleanupError
from app.provider_policy import account_base, digest as policy_digest, normalize
from app.runner import SessionRunner
from tests.test_runner import Fake, success

OWNER = {'pid': 100, 'pgid': 100, 'start_time': 1.0, 'boot_time': 1.0}


class AccountLockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state.db'
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def create_session(self, sid, author_provider='agy@hesap1/gemini-2.5-pro', reviewer_provider='claude'):
        ws = self.root / f'ws_{sid}'
        ws.mkdir(exist_ok=True)
        frozen = normalize({'roles': {'author': [author_provider], 'reviewer': [reviewer_provider]}})
        with store.transaction(self.conn):
            repo.create_session(self.conn, sid, str(ws), 'auto', policy=frozen)
            repo.add_session_step(self.conn, f'{sid}-0', sid, 0, author_provider, f'goal {sid}', role='author')
        return sid

    def events(self, kind, session_id=None):
        if session_id is not None:
            rows = self.conn.execute(
                'SELECT session_id, step_id, payload FROM session_events WHERE kind=? AND session_id=? ORDER BY seq',
                (kind, session_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                'SELECT session_id, step_id, payload FROM session_events WHERE kind=? ORDER BY seq',
                (kind,),
            ).fetchall()
        return [(r['session_id'], r['step_id'], json.loads(r['payload'])) for r in rows]

    def test_account_base_strips_model_and_effort(self):
        self.assertEqual(account_base('agy@hesap1/gemini-2.5-pro'), 'agy@hesap1')
        self.assertEqual(account_base('codex@is/gpt-5~high'), 'codex@is')
        self.assertEqual(account_base('codex/gpt-5~high'), 'codex')
        self.assertEqual(account_base('claude/sonnet~medium'), 'claude')
        self.assertEqual(account_base('codex'), 'codex')

    async def test_a_same_account_base_serializes_and_records_wait_and_shifts_deadline(self):
        self.create_session('s1', author_provider='agy@hesap1/gemini-2.5-pro')
        self.create_session('s2', author_provider='agy@hesap1/gemini-2.5-flash')

        order = []
        active = 0
        max_active = 0

        class SerialAdapter:
            def __init__(self, provider):
                self.provider = provider

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                order.append(('start', self.provider))
                try:
                    yield {'type': 'process_started', 'ownership': OWNER}
                    yield {'type': 'started', 'provider_session_id': 'sid-1'}
                    await asyncio.sleep(0.07)
                    yield {'type': 'process_stopped', 'ownership': OWNER}
                    yield {'type': 'completed', 'result': success('ok', 'sid-1')}
                finally:
                    order.append(('end', self.provider))
                    active -= 1

        # step_timeout=0.12 < 0.07 + 0.07: s2 only succeeds because waiting shifts its deadline forward.
        sr = SessionRunner(self.db, adapter_factory=SerialAdapter, step_timeout=0.12)
        await asyncio.gather(sr.run_session('s1'), sr.run_session('s2'))

        self.assertEqual(repo.get_session(self.conn, 's1')['status'], 'SUCCEEDED')
        self.assertEqual(repo.get_session(self.conn, 's2')['status'], 'SUCCEEDED')
        self.assertEqual(max_active, 1)
        self.assertEqual(
            order,
            [
                ('start', 'agy@hesap1/gemini-2.5-pro'),
                ('end', 'agy@hesap1/gemini-2.5-pro'),
                ('start', 'agy@hesap1/gemini-2.5-flash'),
                ('end', 'agy@hesap1/gemini-2.5-flash'),
            ],
        )

        waits = self.events('account_wait')
        self.assertEqual(len(waits), 1)
        wait_sid, wait_step, wait_payload = waits[0]
        s2_attempt = self.conn.execute("SELECT id FROM session_attempts WHERE step_id='s2-0'").fetchone()['id']
        self.assertEqual(wait_sid, 's2')
        self.assertEqual(wait_step, 's2-0')
        self.assertEqual(
            wait_payload,
            {'attempt_id': s2_attempt, 'account': 'agy@hesap1', 'holder_session_id': 's1'},
        )

        acquired = self.events('account_acquired')
        self.assertEqual(len(acquired), 1)
        acq_sid, acq_step, acq_payload = acquired[0]
        self.assertEqual(acq_sid, 's2')
        self.assertEqual(acq_step, 's2-0')
        self.assertEqual(acq_payload['attempt_id'], s2_attempt)
        self.assertEqual(acq_payload['account'], 'agy@hesap1')
        self.assertGreaterEqual(acq_payload['waited_seconds'], 0.04)

        self.assertFalse(sr._account_locks['agy@hesap1'].locked())
        self.assertEqual(sr._account_holders, {})

    async def test_b_different_account_bases_run_in_parallel_without_wait(self):
        self.create_session('s1', author_provider='agy@hesap1/gemini-2.5-pro')
        self.create_session('s2', author_provider='agy@hesap2/gemini-2.5-pro')

        started = {'agy@hesap1': asyncio.Event(), 'agy@hesap2': asyncio.Event()}
        active = 0
        max_active = 0

        class ParallelAdapter:
            def __init__(self, provider):
                self.base = account_base(provider)

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                started[self.base].set()
                other = 'agy@hesap2' if self.base == 'agy@hesap1' else 'agy@hesap1'
                try:
                    yield {'type': 'process_started', 'ownership': OWNER}
                    yield {'type': 'started', 'provider_session_id': 'sid'}
                    await asyncio.wait_for(started[other].wait(), timeout=1.0)
                    yield {'type': 'process_stopped', 'ownership': OWNER}
                    yield {'type': 'completed', 'result': success('ok', 'sid')}
                finally:
                    active -= 1

        sr = SessionRunner(self.db, adapter_factory=ParallelAdapter)
        await asyncio.gather(sr.run_session('s1'), sr.run_session('s2'))

        self.assertEqual(repo.get_session(self.conn, 's1')['status'], 'SUCCEEDED')
        self.assertEqual(repo.get_session(self.conn, 's2')['status'], 'SUCCEEDED')
        self.assertEqual(max_active, 2)
        self.assertEqual(self.events('account_wait'), [])
        self.assertEqual(self.events('account_acquired'), [])
        self.assertEqual(sr._account_holders, {})

    async def test_c_cancel_while_waiting_cancels_waiter_and_preserves_holder(self):
        self.create_session('s1', author_provider='agy@hesap1/gemini-2.5-pro')
        self.create_session('s2', author_provider='agy@hesap1/gemini-2.5-flash')

        release_s1 = asyncio.Event()
        s2_adapter_calls = 0

        class BlockingFirstAdapter:
            def __init__(self, provider):
                self.provider = provider

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                nonlocal s2_adapter_calls
                if self.provider.endswith('flash'):
                    s2_adapter_calls += 1
                yield {'type': 'process_started', 'ownership': OWNER}
                yield {'type': 'started', 'provider_session_id': 'sid-1'}
                if self.provider.endswith('pro'):
                    await release_s1.wait()
                yield {'type': 'process_stopped', 'ownership': OWNER}
                yield {'type': 'completed', 'result': success('ok', 'sid-1')}

        sr = SessionRunner(self.db, adapter_factory=BlockingFirstAdapter)
        orig_checkpoint = repo.checkpoint

        def on_checkpoint(conn, sid, step_id, kind, payload):
            orig_checkpoint(conn, sid, step_id, kind, payload)
            if kind == 'account_wait' and sid == 's2':
                asyncio.get_running_loop().call_soon(sr.cancel_session, 's2')

        with patch.object(repo, 'checkpoint', side_effect=on_checkpoint):
            t1 = asyncio.create_task(sr.run_session('s1'))
            t2 = asyncio.create_task(sr.run_session('s2'))
            await t2
            # While s1 is still running, it still holds the lock and is unaffected by s2's cancellation.
            self.assertEqual(repo.get_session(self.conn, 's2')['status'], 'CANCELLED')
            self.assertEqual(s2_adapter_calls, 0)
            self.assertTrue(sr._account_locks['agy@hesap1'].locked())
            self.assertEqual(sr._account_holders.get('agy@hesap1'), 's1')
            release_s1.set()
            await t1

        self.assertEqual(repo.get_session(self.conn, 's1')['status'], 'SUCCEEDED')
        self.assertEqual(len(self.events('account_wait', 's2')), 1)
        self.assertEqual(self.events('account_acquired', 's2'), [])
        self.assertFalse(sr._account_locks['agy@hesap1'].locked())
        self.assertEqual(sr._account_holders, {})

    async def test_d_adapter_exception_releases_lock_for_next_session(self):
        self.create_session('s1', author_provider='codex')
        self.create_session('s2', author_provider='codex')
        self.create_session('s3', author_provider='codex')
        self.create_session('s4', author_provider='codex')

        call_no = 0

        class FailingThenOkAdapter:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                nonlocal call_no
                call_no += 1
                current = call_no
                yield {'type': 'process_started', 'ownership': OWNER}
                yield {'type': 'started', 'provider_session_id': f'sid-{current}'}
                if current == 1:
                    await asyncio.sleep(0.03)
                    raise RuntimeError('adapter boom')
                if current == 3:
                    raise ProcessCleanupError('group remains')
                yield {'type': 'process_stopped', 'ownership': OWNER}
                yield {'type': 'completed', 'result': success('ok', f'sid-{current}')}

        sr = SessionRunner(self.db, adapter_factory=lambda _: FailingThenOkAdapter())
        await asyncio.gather(sr.run_session('s1'), sr.run_session('s2'))

        self.assertEqual(repo.get_session(self.conn, 's1')['status'], 'FAILED')
        self.assertEqual(repo.get_session(self.conn, 's2')['status'], 'SUCCEEDED')
        self.assertEqual(len(self.events('account_wait', 's2')), 1)
        self.assertEqual(len(self.events('account_acquired', 's2')), 1)
        self.assertFalse(sr._account_locks['codex'].locked())
        self.assertEqual(sr._account_holders, {})

        # Even when ProcessCleanupError raises and leaves s3 in RECOVERY_REQUIRED, the account lock is released.
        await sr.run_session('s3')
        self.assertEqual(repo.get_session(self.conn, 's3')['status'], 'RECOVERY_REQUIRED')
        self.assertFalse(sr._account_locks['codex'].locked())
        self.assertEqual(sr._account_holders, {})

        await sr.run_session('s4')
        self.assertEqual(repo.get_session(self.conn, 's4')['status'], 'SUCCEEDED')

    def create_work_session(self, wid, title, sid, author_provider='agy@hesap1/gemini-2.5-pro'):
        workspace = self.root / f'ws_{sid}'
        workspace.mkdir(exist_ok=True)
        frozen = normalize({'roles': {'author': [author_provider], 'reviewer': ['claude']}})
        digest = policy_digest(frozen)
        ws.create_work(self.conn, wid, title, str(workspace))
        msg, _ = ws.add_message(self.conn, wid, str(uuid4()), f'goal {sid}', digest, frozen, digest)
        with store.transaction(self.conn):
            repo.create_session(self.conn, sid, str(workspace), 'auto', policy=frozen)
            repo.add_session_step(self.conn, f'{sid}-0', sid, 0, author_provider, f'goal {sid}', role='author')
            self.conn.execute("UPDATE work_messages SET status='LINKED' WHERE id=?", (msg['id'],))
            self.conn.execute(
                'INSERT INTO work_runs VALUES (?,?,?,?,?)',
                (msg['id'], sid, wid, 'QUEUED', '2026-01-01T00:00:00Z'),
            )
        return wid, sid

    def get_work_api(self, wid):
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runner_db=self.db)))
        return works_api.get_work(wid, req)

    async def test_e_get_work_account_wait_while_waiting_and_null_when_acquired(self):
        w1, _ = self.create_work_session(str(uuid4()), 'Birinci çalışma', 's1', 'agy@hesap1/gemini-2.5-pro')
        w2, _ = self.create_work_session(str(uuid4()), 'İkinci çalışma', 's2', 'agy@hesap1/gemini-2.5-flash')

        release_s1 = asyncio.Event()
        release_s2 = asyncio.Event()
        wait_recorded = asyncio.Event()
        acquired_recorded = asyncio.Event()

        class ControlledAdapter:
            def __init__(self, provider):
                self.provider = provider

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                yield {'type': 'process_started', 'ownership': OWNER}
                yield {'type': 'started', 'provider_session_id': 'sid-1'}
                if self.provider.endswith('pro'):
                    await release_s1.wait()
                else:
                    await release_s2.wait()
                yield {'type': 'process_stopped', 'ownership': OWNER}
                yield {'type': 'completed', 'result': success('ok', 'sid-1')}

        sr = SessionRunner(self.db, adapter_factory=ControlledAdapter)
        orig_checkpoint = repo.checkpoint

        def on_checkpoint(conn, sid, step_id, kind, payload):
            orig_checkpoint(conn, sid, step_id, kind, payload)
            if sid == 's2' and kind == 'account_wait':
                wait_recorded.set()
            elif sid == 's2' and kind == 'account_acquired':
                acquired_recorded.set()

        with patch.object(repo, 'checkpoint', side_effect=on_checkpoint):
            t1 = asyncio.create_task(sr.run_session('s1'))
            t2 = asyncio.create_task(sr.run_session('s2'))
            await asyncio.wait_for(wait_recorded.wait(), timeout=1.0)

            waiting_view = self.get_work_api(w2)
            self.assertEqual(waiting_view['latest_session']['status'], 'RUNNING')
            self.assertEqual(
                waiting_view['account_wait'],
                {
                    'account': 'agy@hesap1',
                    'holder_work_id': w1,
                    'holder_work_title': 'Birinci çalışma',
                },
            )
            self.assertIsNone(self.get_work_api(w1)['account_wait'])

            release_s1.set()
            await asyncio.wait_for(acquired_recorded.wait(), timeout=1.0)

            acquired_view = self.get_work_api(w2)
            self.assertEqual(acquired_view['latest_session']['status'], 'RUNNING')
            self.assertIsNone(acquired_view['account_wait'])

            release_s2.set()
            await asyncio.gather(t1, t2)

        done_view = self.get_work_api(w2)
        self.assertEqual(done_view['latest_session']['status'], 'SUCCEEDED')
        self.assertIsNone(done_view['account_wait'])

    async def test_f_three_sessions_same_account_two_waiters_finish_in_order_and_clear_waiting(self):
        self.create_session('s1', author_provider='agy@hesap1/gemini-2.5-pro')
        self.create_session('s2', author_provider='agy@hesap1/gemini-2.5-flash')
        self.create_session('s3', author_provider='agy@hesap1/gemini-2.5-pro')

        fake = Fake(delay=0.04)
        sr = SessionRunner(self.db, adapter_factory=lambda _: fake)
        await asyncio.gather(sr.run_session('s1'), sr.run_session('s2'), sr.run_session('s3'))

        for sid in ('s1', 's2', 's3'):
            self.assertEqual(repo.get_session(self.conn, sid)['status'], 'SUCCEEDED')
        self.assertEqual([prompt for prompt, _ in fake.calls], ['goal s1', 'goal s2', 'goal s3'])
        self.assertEqual([sid for sid, _, _ in self.events('account_wait')], ['s2', 's3'])
        self.assertEqual([sid for sid, _, _ in self.events('account_acquired')], ['s2', 's3'])
        self.assertEqual(sr._account_waiting, {})
        self.assertEqual(sr._account_holders, {})
        self.assertFalse(sr._account_locks['agy@hesap1'].locked())


if __name__ == '__main__':
    unittest.main()
