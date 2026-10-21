"""Disposable F3-04 UI fixture: real store/runner/dispatcher/scheduler/API, a fake Codex that always hits quota.

No account-backed models, no real CLI, no production data. Run:
  .venv/bin/python tests/preview_wait.py [STATIC_DIR] [PORT]
Works: a PENDING wait plan, a WAITING_HUMAN run (folder changed while waiting), an EXPIRED wait.
"""
import asyncio
from datetime import timedelta
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store, works_store as ws, runner, wait_plans
import app.adapters.process as process
import app.adapters.claude as claude_adapter
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import load_policy, digest

STATIC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8884
root = Path(tempfile.mkdtemp(prefix='conductor-f304-ui-'))
(root / '.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
db = root / '.system/state.db'
(root / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n')
process.executable = lambda *a, **k: '/fixture/codex'
claude_adapter.claude_binary = lambda *a, **k: '/fixture/claude'


class QuotaAdapter:
    counter = 0

    def __init__(self, provider):
        self.provider = provider

    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        QuotaAdapter.counter += 1
        n = QuotaAdapter.counter
        owner = {'pid': 41000 + n, 'pgid': 41000 + n, 'start_time': 1.0, 'boot_time': 1.0}
        yield {'type': 'process_started', 'ownership': owner}
        yield {'type': 'started', 'provider_session_id': None}
        yield {'type': 'output_delta', 'text': f'[{self.provider} fixture] kota doldu (gerçek model yok)\n'}
        yield {'type': 'process_stopped', 'ownership': owner}
        yield {'type': 'failed', 'result': AdapterResult('kota doldu', None, None, 'error', True, 'failed', 1,
                                                         'QUOTA_EXCEEDED', {'code': 'usageLimitExceeded'})}


runner._get_adapter = lambda provider, *_: QuotaAdapter(provider)
conn = store.connect(db)
store.migrate(conn)
policy = load_policy(root / '.system/providers.yaml')


async def work(title, wait):
    path = root / title.split()[0].lower(); path.mkdir(); (path / 'readme.txt').write_text('v1')
    wid = str(uuid4()); ws.create_work(conn, wid, title, str(path))
    ws.add_message(conn, wid, str(uuid4()), 'Basit bir hesap makinesi yaz.', digest(policy), dict(policy, wait=wait),
                   digest(policy), None, wait)
    sid, reason = ws.dispatch_next(conn, wid)
    assert sid, reason
    await runner.SessionRunner(db).run_session(sid)
    return path, sid


def fire_only(sid, ahead):
    """The scheduler's claim + fire for one session's plan (others stay pending)."""
    now = wait_plans.utcnow() + ahead
    pid = conn.execute("SELECT id FROM retry_plans WHERE session_id=? AND status='PENDING'", (sid,)).fetchone()[0]
    claimed = wait_plans.claim(conn, pid, now)
    if claimed != 'fired':
        return claimed
    plan = dict(conn.execute('SELECT * FROM retry_plans WHERE id=?', (pid,)).fetchone())
    return wait_plans.fire(conn, plan, runner.snapshot_exclusions(db), now)


async def populate():
    await work('Bekleyen kota', {'deadline_hours': 4, 'max_attempts': 3})
    changed, sid = await work('Değişen klasör', {'deadline_hours': 4, 'max_attempts': 3})
    (changed / 'readme.txt').write_text('başka biri düzenledi')
    print('FIRE changed', fire_only(sid, timedelta(hours=1)), flush=True)
    _, sid = await work('Süresi dolan', {'deadline_hours': 1, 'max_attempts': 3})
    print('FIRE expired', fire_only(sid, timedelta(hours=2)), flush=True)

asyncio.run(populate())
conn.close()
new = root / 'new-work'; new.mkdir()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
print('WORKSPACE_FOR_NEW=' + str(new), flush=True)
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
