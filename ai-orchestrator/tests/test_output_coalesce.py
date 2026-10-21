import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from app import store, runner_store as repo
from app.runner import SessionRunner
from app.adapters.base import AdapterResult


def _result(text='done', is_error=False, exit_code=0):
    return AdapterResult(
        text, 'provider-1', None,
        'error' if is_error else 'stop',
        is_error,
        'failed' if is_error else 'completed',
        exit_code,
    )


OWNERSHIP = {'pid': 100, 'pgid': 100, 'start_time': 1.0, 'boot_time': 1.0}


class StreamAdapter:
    def __init__(self, scripted):
        self.scripted = scripted

    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        for item in self.scripted:
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item


class OutputCoalesceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def create_session(self, sid='s'):
        with store.transaction(self.conn):
            repo.create_session(self.conn, sid, self.tmp.name, 'auto')
            repo.add_session_step(self.conn, f'{sid}-0', sid, 0, 'codex', 'goal 0')
        return sid

    def events(self, sid='s'):
        rows = self.conn.execute(
            'SELECT seq, kind, payload FROM session_events WHERE session_id=? ORDER BY seq',
            (sid,),
        ).fetchall()
        return [(r['seq'], r['kind'], json.loads(r['payload'])) for r in rows]

    async def test_many_small_deltas_coalesce_and_preserve_text(self):
        chunks = [f'parca-{i:03d}|' for i in range(80)]  # 80 * 10 = 800 chars (> 512)
        scripted = [
            {'type': 'process_started', 'ownership': OWNERSHIP},
            {'type': 'started', 'provider_session_id': 'p-1'},
            *[{'type': 'output_delta', 'text': c} for c in chunks],
            {'type': 'process_stopped', 'ownership': OWNERSHIP},
            {'type': 'completed', 'result': _result(''.join(chunks))},
        ]
        self.create_session('s')
        await SessionRunner(self.db, adapter_factory=lambda _: StreamAdapter(scripted)).run_session('s')

        deltas = [p for _, kind, p in self.events('s') if kind == 'output_delta']
        self.assertLess(len(deltas), len(chunks))
        self.assertEqual(len(deltas), 2)
        self.assertGreaterEqual(len(deltas[0]['text']), 512)
        self.assertEqual(''.join(d['text'] for d in deltas), ''.join(chunks))
        self.assertTrue(all(d.get('attempt_id') for d in deltas))

    async def test_time_threshold_flushes_after_150ms(self):
        scripted = [
            {'type': 'process_started', 'ownership': OWNERSHIP},
            {'type': 'started', 'provider_session_id': 'p-1'},
            {'type': 'output_delta', 'text': 'ilk-'},
            {'type': 'output_delta', 'text': 'ikinci-'},
            0.17,
            {'type': 'output_delta', 'text': 'ucuncu'},
            {'type': 'output_delta', 'text': '-son'},
            {'type': 'process_stopped', 'ownership': OWNERSHIP},
            {'type': 'completed', 'result': _result()},
        ]
        self.create_session('s')
        await SessionRunner(self.db, adapter_factory=lambda _: StreamAdapter(scripted)).run_session('s')

        deltas = [p['text'] for _, kind, p in self.events('s') if kind == 'output_delta']
        self.assertEqual(deltas, ['ilk-ikinci-', 'ucuncu-son'])
        self.assertEqual(''.join(deltas), 'ilk-ikinci-ucuncu-son')

    async def test_pending_text_flushed_before_completed_failed_and_cancel(self):
        # (a) completed
        self.create_session('s-ok')
        scripted_ok = [
            {'type': 'started', 'provider_session_id': 'p-1'},
            {'type': 'output_delta', 'text': 'kisa-'},
            {'type': 'output_delta', 'text': 'metin'},
            {'type': 'completed', 'result': _result('kisa-metin')},
        ]
        await SessionRunner(self.db, adapter_factory=lambda _: StreamAdapter(scripted_ok)).run_session('s-ok')
        deltas_ok = [p['text'] for _, kind, p in self.events('s-ok') if kind == 'output_delta']
        self.assertEqual(deltas_ok, ['kisa-metin'])

        # (b) failed
        self.create_session('s-fail')
        scripted_fail = [
            {'type': 'started', 'provider_session_id': 'p-1'},
            {'type': 'output_delta', 'text': 'hata-oncesi-'},
            {'type': 'output_delta', 'text': 'cikti'},
            {'type': 'failed', 'result': _result('boom', is_error=True, exit_code=1)},
        ]
        await SessionRunner(self.db, adapter_factory=lambda _: StreamAdapter(scripted_fail)).run_session('s-fail')
        self.assertEqual(repo.get_session(self.conn, 's-fail')['status'], 'FAILED')
        deltas_fail = [p['text'] for _, kind, p in self.events('s-fail') if kind == 'output_delta']
        self.assertEqual(deltas_fail, ['hata-oncesi-cikti'])

        # (c) cancel / exception while delta is buffered
        self.create_session('s-cancel')
        ready = asyncio.Event()

        class CancelableAdapter:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                yield {'type': 'process_started', 'ownership': OWNERSHIP}
                yield {'type': 'started', 'provider_session_id': 'p-1'}
                yield {'type': 'output_delta', 'text': 'iptal-'}
                yield {'type': 'output_delta', 'text': 'oncesi'}
                ready.set()
                await asyncio.sleep(30)

        runner = SessionRunner(self.db, adapter_factory=lambda _: CancelableAdapter())
        task = asyncio.create_task(runner.run_session('s-cancel'))
        await asyncio.wait_for(ready.wait(), 1)
        self.assertTrue(runner.cancel_session('s-cancel'))
        await asyncio.wait_for(task, 1)
        self.assertEqual(repo.get_session(self.conn, 's-cancel')['status'], 'CANCELLED')
        deltas_cancel = [p['text'] for _, kind, p in self.events('s-cancel') if kind == 'output_delta']
        self.assertEqual(deltas_cancel, ['iptal-oncesi'])

    async def test_event_sequence_order_preserved(self):
        scripted = [
            {'type': 'process_started', 'ownership': OWNERSHIP},
            {'type': 'started', 'provider_session_id': 'p-1'},
            {'type': 'output_delta', 'text': 'bir '},
            {'type': 'output_delta', 'text': 'iki'},
            {'type': 'approval_required', 'command': 'ls'},
            {'type': 'output_delta', 'text': 'uc '},
            {'type': 'output_delta', 'text': 'dort'},
            {'type': 'command_result', 'exit_code': 0},
            {'type': 'output_delta', 'text': 'bes'},
            {'type': 'process_group_snapshot', 'ownership': {'members': []}},
            {'type': 'output_delta', 'text': 'alti'},
            {'type': 'process_stopped', 'ownership': OWNERSHIP},
            {'type': 'completed', 'result': _result()},
        ]
        self.create_session('s')
        await SessionRunner(self.db, adapter_factory=lambda _: StreamAdapter(scripted)).run_session('s')

        ev = self.events('s')
        seqs = [seq for seq, _, _ in ev]
        self.assertEqual(seqs, sorted(seqs))
        kinds = [kind for _, kind, _ in ev]
        self.assertEqual(
            kinds,
            [
                'attempt_started',
                'started',
                'output_delta',
                'approval_required',
                'output_delta',
                'command_result',
                'output_delta',
                'process_group_snapshot',
                'output_delta',
                'step_completed',
            ],
        )
        texts = [p['text'] for _, kind, p in ev if kind == 'output_delta']
        self.assertEqual(texts, ['bir iki', 'uc dort', 'bes', 'alti'])
