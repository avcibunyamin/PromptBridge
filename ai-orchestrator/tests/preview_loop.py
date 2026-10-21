"""Disposable F3-02 UI fixture: real store/runner/dispatcher/API with the turn loop enabled.

No account-backed models and no production data. Run:
  .venv/bin/python tests/preview_loop.py STATIC_DIR [PORT]
Fake reviewer, steered by words in the work message:
  TWICE   → changes_requested in turn 1, pass in turn 2
  STUCK   → the same finding every turn; the author of the 'İlerleme yok' work changes nothing (no_progress)
  NEVER   → changes_requested every turn, author keeps writing (turn_limit)
  GARBAGE → an answer without the result block (review_invalid)
  BLOCKED → blocked
  SLOW    → every call takes 4 s (time to press “pause”)
  otherwise → pass
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store, works_store as ws, runner
import app.adapters.process as process
import app.adapters.claude as claude_adapter
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import normalize, digest

STATIC = Path(sys.argv[1])
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8895
root = Path(tempfile.mkdtemp(prefix='conductor-f302-ui-'))
(root / '.system').mkdir()
(root / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\nloop:\n  max_turns: 3\n  max_active_minutes: 30\n')
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
process.executable = lambda *a, **k: '/fixture/codex'
claude_adapter.claude_binary = lambda *a, **k: '/fixture/claude'


def block(verdict, *findings, reason=None):
    body = {'verdict': verdict, 'findings': [{'id': f'F{i + 1}', 'severity': sev, 'summary': text, 'evidence': ev}
                                             for i, (sev, text, ev) in enumerate(findings)]}
    if reason:
        body['blocked_reason'] = reason
    return 'İnceleme tamam.\n```conductor-review\n' + json.dumps(body, ensure_ascii=False) + '\n```\n'


class LoopAdapter:
    calls = 0

    def __init__(self, provider):
        self.provider = provider

    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        LoopAdapter.calls += 1
        n = LoopAdapter.calls
        owner = {'pid': 60000 + n, 'pgid': 60000 + n, 'start_time': 1.0, 'boot_time': 1.0}
        sid = provider_session_id or f'{self.provider}-fixture-{n}'
        turn = int(prompt.split('turn=')[1].split()[0]) if 'turn=' in prompt else 1
        yield {'type': 'process_started', 'ownership': owner}
        yield {'type': 'started', 'provider_session_id': sid}
        for i in range(2):
            await asyncio.sleep(2.0 if 'SLOW' in prompt else .15)
            yield {'type': 'output_delta', 'text': f'[{self.provider} fixture] tur {turn} çıktı {i + 1}\n'}
        yield {'type': 'process_stopped', 'ownership': owner}
        if profile == 'WRITE':
            if not Path(workspace).name.startswith('İlerleme'):   # the no-progress work's author changes nothing
                Path(workspace, 'calisma.txt').write_text(f'yazar çağrısı {n}\n')
            text = f'Yazar (tur {turn}) değişiklikleri yaptı.'
        elif 'GARBAGE' in prompt:
            text = 'Her şey iyi görünüyor, bence tamam.'
        elif 'BLOCKED' in prompt:
            text = block('blocked', reason='Test ortamına erişim yok')
        elif 'STUCK' in prompt or 'NEVER' in prompt or ('TWICE' in prompt and turn == 1):
            text = block('changes_requested', ('high', 'Boş girişte bölme hatası', ['calc.py:42', 'pytest -k empty -> FAILED']),
                         ('low', 'Değişken adı belirsiz', []))
        else:
            text = block('pass')
        yield {'type': 'completed', 'result': AdapterResult(text, sid, None, 'stop', False, 'completed', 0)}


runner._get_adapter = lambda provider, *_: LoopAdapter(provider)
conn = store.connect(root / '.system/state.db')
store.migrate(conn)
policy = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}, 'loop': {'max_turns': 3, 'max_active_minutes': 30}})


async def seed(title, text):
    folder = root / title.replace(' ', '-'); folder.mkdir()
    wid = str(uuid4()); ws.create_work(conn, wid, title, str(folder))
    ws.add_message(conn, wid, str(uuid4()), text, digest(policy), policy, digest(policy))
    sid, _ = ws.dispatch_next(conn, wid)
    await runner.SessionRunner(root / '.system/state.db').run_session(sid)


async def populate():
    await seed('İki tur doğrulandı', 'TWICE hesap makinesi yaz.')
    await seed('Tur sınırı', 'NEVER bitmeyen istek.')
    await seed('İlerleme yok', 'STUCK aynı hata.')
    await seed('Okunamayan inceleme', 'GARBAGE bloksuz cevap.')
    await seed('İnceleyici engeli', 'BLOCKED erişim sorunu.')

asyncio.run(populate())
conn.close()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
print('NEW_FOLDER=' + str(root / 'yeni'), flush=True); (root / 'yeni').mkdir()
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
