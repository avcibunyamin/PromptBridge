"""Disposable F3-01b UI fixture: real store/runner/dispatcher/API, scripted fake providers.

No account-backed models and no production data. Run:
  .venv/bin/python tests/preview_works.py [STATIC_DIR] [PORT]
Fake adapter keywords in a message: FAIL_ME (author fails), QUOTA_ME (codex quota; single-provider
policy → WAITING_CAPACITY), SLOW (long run). Everything else succeeds after streaming a few deltas.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store, runner_store as repo, works_store as ws, runner
import app.adapters.process as process
import app.adapters.claude as claude_adapter
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import normalize, digest, DEFAULT

STATIC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8881
root = Path(tempfile.mkdtemp(prefix='conductor-f301b-ui-'))
(root / '.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
db = root / '.system/state.db'

# Both providers count as installed; nothing real is ever executed.
process.executable = lambda *a, **k: '/fixture/codex'
claude_adapter.claude_binary = lambda *a, **k: '/fixture/claude'


class PreviewAdapter:
    counter = 0

    def __init__(self, provider):
        self.provider = provider

    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        PreviewAdapter.counter += 1
        n = PreviewAdapter.counter
        owner = {'pid': 40000 + n, 'pgid': 40000 + n, 'start_time': 1.0, 'boot_time': 1.0}
        sid = provider_session_id or f'{self.provider}-fixture-{n}'
        yield {'type': 'process_started', 'ownership': owner}
        yield {'type': 'started', 'provider_session_id': sid}
        slow = 'SLOW' in prompt
        for i in range(6 if slow else 3):
            await asyncio.sleep(1.5 if slow else .3)
            yield {'type': 'output_delta', 'text': f'[{self.provider} fixture] adım çıktısı {i + 1}\n'}
        yield {'type': 'process_stopped', 'ownership': owner}
        if 'FAIL_ME' in prompt and profile == 'WRITE':
            result = AdapterResult('fixture failure', sid, None, 'error', True, 'failed', 1, 'UNKNOWN', {'code': 'UNKNOWN'})
        elif 'QUOTA_ME' in prompt and self.provider == 'codex':
            result = AdapterResult('quota', sid, None, 'error', True, 'failed', 1, 'QUOTA_EXCEEDED', {'code': 'QUOTA_EXCEEDED'})
        else:
            result = AdapterResult(f'{self.provider} fixture tamamlandı (gerçek model yok).', sid, None, 'stop', False, 'completed', 0)
        yield {'type': 'completed' if result.successful else 'failed', 'result': result}


runner._get_adapter = lambda provider, *_: PreviewAdapter(provider)

conn = store.connect(db)
store.migrate(conn)
policy = normalize(DEFAULT)


def folder(name):
    path = root / name
    path.mkdir()
    return str(path)


async def run_message(work_id, text):
    ws.add_message(conn, work_id, str(uuid4()), text, digest(policy), policy, digest(policy))
    sid, reason = ws.dispatch_next(conn, work_id)
    assert sid, reason
    await runner.SessionRunner(db).run_session(sid)
    return sid


async def populate():
    done = str(uuid4()); ws.create_work(conn, done, 'Hesap makinesi (iki tur)', folder('calc'))
    await run_message(done, 'Basit bir hesap makinesi yaz.')
    await run_message(done, 'Geçmiş özelliği ekle.')              # second run: RESUME decision
    failed = str(uuid4()); ws.create_work(conn, failed, 'Başarısız koşu, sırada mesaj', folder('failed'))
    await run_message(failed, 'FAIL_ME bu koşu başarısız olacak.')
    ws.add_message(conn, failed, str(uuid4()), 'Bu mesaj sırada bekliyor.', digest(policy), policy, digest(policy))
    waiting = str(uuid4()); ws.create_work(conn, waiting, 'Kota bekleyen çalışma', folder('quota'))
    await run_message(waiting, 'QUOTA_ME kota dolacak.')
    big = str(uuid4()); ws.create_work(conn, big, 'Çok büyük istem', folder('big'))
    await run_message(big, 'ğ' * 70000)                           # 140000 bytes > 131072: no provider call
    archived = str(uuid4()); ws.create_work(conn, archived, 'Arşivlenmiş iş', folder('archived'))
    ws.patch_work(conn, archived, 0, archived=True)
    for i in range(52):                                           # > one 50-row page
        ws.create_work(conn, str(uuid4()), f'Liste doldurma {i:02d}', folder(f'fill{i:02d}'))
    for i in range(3):                                            # earlier sessions, not linked to works
        sid = f'legacy-{i}'
        repo.create_session(conn, sid, folder(f'legacy{i}'), 'auto')
        repo.update_session_status(conn, sid, 'SUCCEEDED' if i else 'FAILED')

asyncio.run(populate())
conn.close()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
print('WORKSPACE_FOR_NEW=' + folder('new-work'), flush=True)
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
