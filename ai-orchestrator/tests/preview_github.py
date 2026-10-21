"""Disposable TB-21 UI fixture: a fake `gh`, local bare repositories as "GitHub", one work in a cloned repo.

No network, no token, no real GitHub. Run:
  .venv/bin/python tests/preview_github.py [STATIC_DIR] [PORT]
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from tests.test_tb21_github import FAKE_GH

STATIC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8885
base = Path(tempfile.mkdtemp(prefix='conductor-tb21-ui-')).resolve()
root, home, remotes = base / 'conductor', base / 'home', base / 'remotes'   # the clone folder may not be inside Conductor
(root / '.system').mkdir(parents=True); (home / 'code').mkdir(parents=True)
Path.home = classmethod(lambda cls: home)   # the clone folder must be under "home"
(root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
(root / '.system/settings.json').write_text(json.dumps({'version': 1, 'github': {'clone_dir': str(home / 'code')}}))
gh = base / 'bin/gh'; gh.parent.mkdir(); gh.write_text(FAKE_GH.format(python=sys.executable)); gh.chmod(0o755)
os.environ.update({'AI_ORCHESTRATOR_ROOT': str(root), 'CONDUCTOR_GH_BIN': str(gh), 'FAKE_GH_LOG': str(root / 'gh.log'),
                   'FAKE_GITHUB': f'file://{remotes}/'})
from app import github, store, works_store as ws   # noqa: E402
from app.api import create_app                      # noqa: E402
github.GITHUB = f'file://{remotes}/'


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


for name in ('demo', 'site'):
    bare = remotes / f'octo/{name}.git'; sh('git', 'init', '-q', '--bare', '-b', 'main', str(bare))
    seed = base / f'seed-{name}'; sh('git', 'init', '-q', '-b', 'main', str(seed)); (seed / 'README.md').write_text(f'{name}\n')
    sh('git', 'add', '.', cwd=seed); sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'init', cwd=seed)
    sh('git', 'push', '-q', str(bare), 'main', cwd=seed)

db = root / '.system/state.db'
conn = store.connect(db); store.migrate(conn)
path = Path(github.clone(str(db), 'octo/demo')['path'])
sh('git', 'config', 'user.name', 'Fixture', cwd=path); sh('git', 'config', 'user.email', 'f@example.com', cwd=path)
(path / 'app.py').write_text('print("merhaba")\n'); (path / '.env').write_text('TOKEN=fake\n')
(path / '.github/workflows').mkdir(parents=True); (path / '.github/workflows/ci.yml').write_text('on: push\n')
ws.create_work(conn, str(uuid4()), 'Hesap makinesi (GitHub)', str(path))
conn.close()
print('DISPOSABLE_ROOT=' + str(root), flush=True)
uvicorn.run(create_app(static_dir=STATIC), host='127.0.0.1', port=PORT)
