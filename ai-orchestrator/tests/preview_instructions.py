"""Disposable TB-03 UI fixture: real store/runner/dispatcher/API, fake providers, no production data.

Run:
  .venv/bin/python tests/preview_instructions.py STATIC_DIR [PORT]
Seeds three works:
  'Ret — legacy politika'  → CLAUDE.md imports a missing file (version-2 policy): step-less FAILED + banner
  'Ret — döngü politikası' → CLAUDE.md imports a symlink (loop policy): step-less FAILED + banner
  'Talimat alındı'         → CLAUDE.md + @AGENTS.md import resolve and the run completes normally
"""
import asyncio
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
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8896
root = Path(tempfile.mkdtemp(prefix='conductor-tb03-ui-'))
(root / '.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
process.executable = lambda *a, **k: '/fixture/codex'
claude_adapter.claude_binary = lambda *a, **k: '/fixture/claude'


class FixtureAdapter:
    calls = 0

    def __init__(self, provider):
        self.provider = provider

    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        FixtureAdapter.calls += 1
        n = FixtureAdapter.calls
        owner = {'pid': 61000 + n, 'pgid': 61000 + n, 'start_time': 1.0, 'boot_time': 1.0}
        sid = provider_session_id or f'{self.provider}-fixture-{n}'
        yield {'type': 'process_started', 'ownership': owner}
        yield {'type': 'started', 'provider_session_id': sid}
        seen = 'PROJECT INSTRUCTIONS' in prompt
        yield {'type': 'output_delta', 'text': f'[{self.provider} fixture] proje talimatı prompt\'ta: {"evet" if seen else "hayır"}\n'}
        yield {'type': 'process_stopped', 'ownership': owner}
        text = ('İnceleme tamam.\n```conductor-review\n{"verdict":"pass","findings":[]}\n```\n'
                if profile == 'READ_ONLY' else f'{self.provider} tamamladı (talimat: {"evet" if seen else "hayır"}).')
        yield {'type': 'completed', 'result': AdapterResult(text, sid, None, 'stop', False, 'completed', 0)}


runner._get_adapter = lambda provider, *_: FixtureAdapter(provider)
conn = store.connect(root / '.system/state.db')
store.migrate(conn)
legacy = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}})
looped = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}, 'loop': {'max_turns': 2, 'max_active_minutes': 30}})


async def seed(title, policy, files, links=()):
    folder = root / title.replace(' ', '-'); folder.mkdir()
    for name, body in files.items():
        (folder / name).write_text(body)
    for link, target in links:
        os.symlink(target, folder / link)
    wid = str(uuid4()); ws.create_work(conn, wid, title, str(folder))
    ws.add_message(conn, wid, str(uuid4()), f'{title}: README dosyasını gözden geçir.', digest(policy), policy, digest(policy))
    sid, reason = ws.dispatch_next(conn, wid)
    if reason is None:
        await runner.SessionRunner(root / '.system/state.db').run_session(sid)
    ws.sync_run_status(conn)
    print(f'{title}: session={sid} dispatch={reason or "dispatched"}', flush=True)


async def populate():
    await seed('Ret — legacy politika', legacy, {'CLAUDE.md': '# Proje\n@docs/kurallar.md\n'})
    await seed('Ret — döngü politikası', looped, {'CLAUDE.md': '# Proje\n@AGENTS.md\n', 'gercek.md': 'x'},
               links=[('AGENTS.md', 'gercek.md')])
    await seed('Talimat alındı', looped, {'CLAUDE.md': '# Proje\n@AGENTS.md\n', 'AGENTS.md': 'Testleri çalıştır.'})

asyncio.run(populate())
conn.close()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
