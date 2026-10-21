"""Disposable F3-03 UI fixture: real store/runner/dispatcher/API, scripted fake providers and session check.

No account-backed models, no real CLI, no production data. Run:
  .venv/bin/python tests/preview_team.py [STATIC_DIR] [PORT]
Ceiling: both providers allowed in both roles. The fake session check answers codex=authenticated and
leaves claude's process group "unverified" (probe_cleanup_failed); every probe is printed as PROBE <name>.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store, works_store as ws, runner, provider_auth
import app.adapters.process as process
import app.adapters.claude as claude_adapter
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import load_policy, digest

STATIC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8883
root = Path(tempfile.mkdtemp(prefix='conductor-f303-ui-'))
(root / '.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
db = root / '.system/state.db'
(root / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n'
                                              'allowed:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')

process.executable = lambda *a, **k: '/fixture/codex'
claude_adapter.claude_binary = lambda *a, **k: '/fixture/claude'


def fake_probe(provider, binary, home=None):
    print('PROBE', provider, flush=True)
    return ('authenticated', True) if provider == 'codex' else ('unknown', False)


provider_auth._probe = fake_probe


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
        await asyncio.sleep(.3)
        yield {'type': 'output_delta', 'text': f'[{self.provider} fixture · {profile}] çıktı\n'}
        yield {'type': 'process_stopped', 'ownership': owner}
        yield {'type': 'completed', 'result': AdapterResult(f'{self.provider} fixture tamamlandı (gerçek model yok).',
                                                            sid, None, 'stop', False, 'completed', 0)}


runner._get_adapter = lambda provider, *_: PreviewAdapter(provider)

conn = store.connect(db)
store.migrate(conn)
policy = load_policy(root / '.system/providers.yaml')


async def populate():
    path = root / 'calc'; path.mkdir()
    wid = str(uuid4()); ws.create_work(conn, wid, 'Takım takibi (varsayılan takım)', str(path))
    ws.add_message(conn, wid, str(uuid4()), 'Basit bir hesap makinesi yaz.', digest(policy), policy, digest(policy))
    sid, reason = ws.dispatch_next(conn, wid)
    assert sid, reason
    await runner.SessionRunner(db).run_session(sid)

asyncio.run(populate())
conn.close()
new = root / 'new-work'; new.mkdir()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
print('WORKSPACE_FOR_NEW=' + str(new), flush=True)
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
