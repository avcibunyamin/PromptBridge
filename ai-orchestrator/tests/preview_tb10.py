"""Disposable TB-10 UI fixture: real store/runner/API/catalog, scripted fake providers, no quota, no real HOME.

  .venv/bin/python tests/preview_tb10.py [STATIC_DIR] [PORT]

Team like K-022 with a removed model in the reviewer chain. A fake `agy` lists models for `hesap1` and asks
`hesap2` to sign in, so the catalog shows "Model yok", "Giriş yok" and Claude's "Doğrulanmadı". Fake keychain tool.
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
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import load_policy, digest
from tests.test_tb08_agy import FAKE_SECURITY

STATIC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8884
root = Path(tempfile.mkdtemp(prefix='conductor-tb10-ui-'))
(root / '.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
db = root / '.system/state.db'
(root / '.system/providers.yaml').write_text(
    'roles:\n'
    '  author: [claude/sonnet~high, agy@hesap1/claude-sonnet-5-5-high, agy@hesap1/gemini-3.1-pro-high, agy@hesap2/gemini-3.1-pro-high]\n'
    '  reviewer: [claude, agy@hesap1/claude-sonnet-4-6]\n'
    'allowed:\n  author: [claude, agy@hesap1, agy@hesap2]\n  reviewer: [claude, agy@hesap1, agy@hesap2]\n')

bins = root / 'bin'; bins.mkdir()


def script(name, body):
    path = bins / name
    path.write_text('#!' + sys.executable + '\n' + body); path.chmod(0o755)
    return str(path)


os.environ.update(
    CONDUCTOR_CLAUDE_BIN=script('claude', 'raise SystemExit(0)\n'),
    CONDUCTOR_CODEX_BIN=script('codex', 'raise SystemExit(0)\n'),
    CONDUCTOR_SECURITY_BIN=script('security', FAKE_SECURITY), FAKE_SECURITY_LOG=str(root / 'security.log'),
    CONDUCTOR_AGY_BIN=script('agy', '''import os, sys
if os.environ['HOME'].endswith('hesap2'):
    print('Error: Please sign in to view available models.'); sys.exit(1)
print('Fetching available models...')
for m in ('gemini-3.1-pro-high\\tGemini 3.1 Pro (High)', 'gemini-3-flash\\tGemini 3 Flash',
          'claude-sonnet-5-5-high\\tClaude Sonnet 5.5 (High)', 'claude-opus-5-5-high\\tClaude Opus 5.5 (High)'):
    print(m)
'''))


def fake_probe(provider, binary, home=None, model=None):
    print('PROBE', provider, home, flush=True)
    return ('unauthenticated' if home and home.endswith('hesap2') else 'authenticated'), True


provider_auth._probe = fake_probe


class PreviewAdapter:
    counter = 0

    def __init__(self, provider):
        self.provider = provider

    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        PreviewAdapter.counter += 1
        n = PreviewAdapter.counter
        owner = {'pid': 40000 + n, 'pgid': 40000 + n, 'start_time': 1.0, 'boot_time': 1.0}
        sid = provider_session_id or f'fixture-{n}'
        yield {'type': 'process_started', 'ownership': owner}
        yield {'type': 'started', 'provider_session_id': sid}
        await asyncio.sleep(1.5)
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
    wid = str(uuid4()); ws.create_work(conn, wid, 'Hesap makinesi (K-022 takımı)', str(path))
    ws.add_message(conn, wid, str(uuid4()), 'Basit bir hesap makinesi yaz.', digest(policy), policy, digest(policy))
    sid, reason = ws.dispatch_next(conn, wid)
    assert sid, reason
    await runner.SessionRunner(db).run_session(sid)

asyncio.run(populate())
conn.close()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
