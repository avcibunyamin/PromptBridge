"""Disposable TB-12 browser fixture: the Mac panel (8885) and the mobile gateway (8886) of one app.

  .venv/bin/python tests/preview_tb12.py STATIC_DIR

A browser cannot send the ts.net Host or Tailscale's identity header, so only here: the gateway's host is
127.0.0.1:8886 and a thin wrapper adds `Tailscale-User-Login` (what `tailscale serve` adds in production).
Everything else is the real app, MobileGuard and panel. Temp root; one pending envelope to approve.
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store, mobile
from app.api import create_app

STATIC = Path(sys.argv[1])
root = Path(tempfile.mkdtemp(prefix='conductor-tb12-ui-')); (root / '.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
(root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
db = root / '.system/state.db'
conn = store.connect(db); store.migrate(conn)
outbox = root / 'agents' / 'claude' / 'outbox'; outbox.mkdir(parents=True)
conn.close()

app = create_app(static_dir=STATIC)


class TailscaleHeader:
    def __init__(self, inner): self.inner = inner
    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            scope = dict(scope, headers=[*scope['headers'], (b'tailscale-user-login', b'operator@example.com')])
        await self.inner(scope, receive, send)


async def main():
    panel = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8885, log_level='warning'))
    gateway = uvicorn.Server(uvicorn.Config(TailscaleHeader(mobile.MobileGuard(app, db, '127.0.0.1')),
                                            host='127.0.0.1', port=8886, log_level='warning', lifespan='off'))
    print('ROOT=' + str(root), flush=True)
    await asyncio.gather(panel.serve(), gateway.serve())

asyncio.run(main())
