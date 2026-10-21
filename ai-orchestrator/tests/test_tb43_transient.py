import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from app import runner_store as repo, store
from app.adapters.base import AdapterResult
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.provider_errors import (
    AGY_TRANSIENT,
    CLAUDE,
    CODEX,
    TransientEOF,
    agy_error,
    claude_error,
    codex_error,
)
from app.provider_policy import normalize
from app.runner import SessionRunner
from tests.test_runner import Fake, success

POLICY = {'roles': {'author': ['codex', 'claude'], 'reviewer': ['claude', 'codex']}}
OWNER = {'pid': 1, 'pgid': 1, 'start_time': 1, 'boot_time': 1}


def transient():
    return AdapterResult('', 'old-session', None, 'error', True, 'server_error', 1, 'TRANSIENT', {'code': 'server_error'})


class TransientErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_codex_transient_protocol_errors(self):
        expected = (
            'internalServerError',
            'serverOverloaded',
            'httpConnectionFailed',
            'responseStreamConnectionFailed',
            'responseStreamDisconnected',
        )
        for info in expected:
            with self.subTest(codexErrorInfo=info):
                self.assertEqual(CODEX[info], 'TRANSIENT')
                self.assertEqual(codex_error({'codexErrorInfo': info}), 'TRANSIENT')
        # Existing classifications and assistant/unknown safety remain intact
        self.assertEqual(codex_error({'codexErrorInfo': 'usageLimitExceeded'}), 'QUOTA_EXCEEDED')
        self.assertEqual(codex_error({'codexErrorInfo': 'unauthorized'}), 'AUTH')
        self.assertEqual(codex_error({'message': 'internalServerError'}), 'UNKNOWN')

    def test_claude_transient_protocol_error(self):
        self.assertEqual(CLAUDE['server_error'], 'TRANSIENT')
        self.assertEqual(claude_error({'type': 'assistant', 'error': 'server_error'}), 'TRANSIENT')
        # Assistant text without protocol error field must never be classified
        self.assertEqual(claude_error({'type': 'assistant', 'content': 'server_error'}), 'UNKNOWN')
        self.assertEqual(claude_error({'type': 'result', 'error': 'server_error'}), 'UNKNOWN')

    def test_agy_transient_and_precedence(self):
        expected = ('503', '502', 'overloaded', 'connection reset', 'stream closed', 'request failed: post ')
        self.assertEqual(AGY_TRANSIENT, expected)
        for token in expected:
            with self.subTest(token=token):
                self.assertEqual(agy_error(f'Error: {token} during request'), 'TRANSIENT')
                self.assertEqual(agy_error(f'Error: {token.upper()}'), 'TRANSIENT')
        eof_msg = (
            'API error (attempt 1): request failed: Post '
            '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF'
        )
        self.assertEqual(agy_error(eof_msg), 'TRANSIENT')
        # Precedence: AUTH -> QUOTA_EXCEEDED -> TRANSIENT -> UNKNOWN
        self.assertEqual(agy_error(f'{eof_msg} resource_exhausted'), 'QUOTA_EXCEEDED')
        self.assertEqual(agy_error(f'{eof_msg} please sign in'), 'AUTH')
        self.assertEqual(agy_error('503 resource_exhausted'), 'QUOTA_EXCEEDED')
        self.assertEqual(agy_error('overloaded quota limit'), 'QUOTA_EXCEEDED')
        self.assertEqual(agy_error('503 please sign in'), 'AUTH')
        self.assertEqual(agy_error('please sign in resource_exhausted 503'), 'AUTH')
        self.assertEqual(agy_error('internal error'), 'UNKNOWN')
        self.assertEqual(agy_error('some unrelated failure'), 'UNKNOWN')

    def test_failure_state_for_transient_stays_failed(self):
        res = AdapterResult('', 'sid', None, 'error', True, 'error', 1, 'TRANSIENT', {})
        self.assertEqual(res.failure_state, 'FAILED')

    def test_transient_eof_is_value_error_subclass(self):
        err = TransientEOF('Claude EOF without result')
        self.assertIsInstance(err, ValueError)
        self.assertIsInstance(err, TransientEOF)
        self.assertTrue(issubclass(TransientEOF, ValueError))
        self.assertEqual(str(err), 'Claude EOF without result')

    async def test_claude_and_codex_eof_raise_transient_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_bin = root / 'fake-claude'
            claude_bin.write_text('#!' + sys.executable + '\nprint("{}")\n')
            claude_bin.chmod(0o700)
            with patch.dict(os.environ, {'CONDUCTOR_CLAUDE_BIN': str(claude_bin)}):
                with self.assertRaises(TransientEOF) as cm:
                    _ = [e async for e in ClaudeAdapter().run('goal', str(root), None, profile='READ_ONLY')]
                self.assertEqual(str(cm.exception), 'Claude EOF without result')

            codex_bin = root / 'fake-codex'
            codex_bin.write_text('#!' + sys.executable + '\nimport sys\nsys.stdin.readline()\n')
            codex_bin.chmod(0o700)
            with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': str(codex_bin)}):
                with self.assertRaises(TransientEOF) as cm:
                    _ = [e async for e in CodexAdapter(str(root / 'codex-home')).run(
                        'goal', str(root), None, profile='READ_ONLY')]
                self.assertEqual(str(cm.exception), 'Codex EOF before terminal result')


class TransientRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        self.db = self.root / 'state.db'
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        patcher = patch('app.runner.TRANSIENT_WAIT', 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, roles=('reviewer',), policy=None):
        frozen = normalize(policy or POLICY)
        with store.transaction(self.conn):
            repo.create_session(self.conn, 's', str(self.ws), 'auto', policy=frozen)
            for i, role in enumerate(roles):
                repo.add_session_step(self.conn, f's{i}', 's', i, frozen['roles'][role][0]['provider'], 'goal', role=role)

    def attempts(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM session_attempts ORDER BY started_at, attempt_no')]

    async def test_reviewer_transient_eof_retries_once_and_succeeds(self):
        self.create(roles=('reviewer',))

        class SeqAdapter(Fake):
            def __init__(self):
                super().__init__(success())
                self.count = 0

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                self.count += 1
                if self.count == 1:
                    yield {'type': 'process_started', 'ownership': OWNER}
                    raise TransientEOF('Claude EOF without result')
                self.result = success('review ok')
                async for e in super().run(prompt, workspace, provider_session_id, profile=profile):
                    yield e

        adapter = SeqAdapter()
        await SessionRunner(self.db, adapter_factory=lambda _: adapter).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'SUCCEEDED')
        attempts = self.attempts()
        self.assertEqual(len(attempts), 2)
        self.assertEqual([a['status'] for a in attempts], ['FAILED', 'COMPLETED'])
        self.assertEqual([a['provider'] for a in attempts], ['claude', 'claude'])
        self.assertIsNone(attempts[0]['handover_reason'])
        self.assertEqual(attempts[1]['handover_reason'], 'transient_retry')
        self.assertEqual(attempts[1]['continuation_no'], 1)
        row = self.conn.execute("SELECT payload FROM session_events WHERE kind='transient_retry'").fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(row[0])
        self.assertEqual(payload['attempt_id'], attempts[0]['id'])
        self.assertEqual(payload['provider'], 'claude')
        self.assertEqual(payload['role'], 'reviewer')
        self.assertEqual(payload['profile'], 'READ_ONLY')
        self.assertEqual(payload['reason'], 'transient_eof: Claude EOF without result')
        self.assertEqual(payload['wait_seconds'], 0.01)

    async def test_author_dirty_workspace_transient_does_not_retry(self):
        self.create(roles=('author',))

        class DirtyAuthor(Fake):
            def __init__(self):
                super().__init__(transient())
                self.count = 0

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                self.count += 1
                if self.count == 1:
                    (Path(workspace) / 'dirty.txt').write_text('partial edit')
                    self.result = transient()
                else:
                    self.result = success()
                async for e in super().run(prompt, workspace, provider_session_id, profile=profile):
                    yield e

        adapter = DirtyAuthor()
        await SessionRunner(self.db, adapter_factory=lambda _: adapter).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'FAILED')
        self.assertEqual(adapter.count, 1)
        attempts = self.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['status'], 'FAILED')
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM session_events WHERE kind='transient_retry'").fetchone()[0], 0
        )

    async def test_read_only_two_consecutive_transients_retries_only_once_then_fails(self):
        self.create(roles=('reviewer',))

        class TwoTransients(Fake):
            def __init__(self):
                super().__init__(transient())
                self.count = 0

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                self.count += 1
                self.result = transient() if self.count <= 2 else success()
                async for e in super().run(prompt, workspace, provider_session_id, profile=profile):
                    yield e

        adapter = TwoTransients()
        await SessionRunner(self.db, adapter_factory=lambda _: adapter).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'FAILED')
        self.assertEqual(adapter.count, 2)
        attempts = self.attempts()
        self.assertEqual(len(attempts), 2)
        self.assertEqual([a['status'] for a in attempts], ['FAILED', 'FAILED'])
        self.assertEqual([a['provider'] for a in attempts], ['claude', 'claude'])
        self.assertEqual(attempts[1]['handover_reason'], 'transient_retry')
        self.assertEqual(attempts[1]['continuation_no'], 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM session_events WHERE kind='transient_retry'").fetchone()[0], 1
        )

    async def test_cancel_during_transient_wait_cancels_session(self):
        self.create(roles=('reviewer',))

        class RetryThenSuccess(Fake):
            def __init__(self):
                super().__init__(transient())
                self.count = 0

            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                self.count += 1
                self.result = transient() if self.count == 1 else success()
                async for e in super().run(prompt, workspace, provider_session_id, profile=profile):
                    yield e

        adapter = RetryThenSuccess()
        sr = SessionRunner(self.db, adapter_factory=lambda _: adapter)
        orig_checkpoint = repo.checkpoint

        def on_checkpoint(conn, sid, step_id, kind, payload):
            orig_checkpoint(conn, sid, step_id, kind, payload)
            if kind == 'transient_retry':
                asyncio.get_running_loop().call_soon(sr.cancel_session, sid)

        with patch('app.runner.TRANSIENT_WAIT', 1), patch.object(repo, 'checkpoint', side_effect=on_checkpoint):
            await sr.run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'CANCELLED')
        self.assertEqual(adapter.count, 1)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM session_events WHERE kind='transient_retry'").fetchone()[0], 1
        )


if __name__ == '__main__':
    unittest.main()
