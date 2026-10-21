"""Run a real serve subprocess against a temporary workspace, never user data.

Run: .venv/bin/python tests/smoke_bidirectional.py
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'

        def request(path, body=None):
            req = Request(base + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=3) as response:
                return response.read()

        with (root / 'serve.log').open('w+') as log:
            proc = subprocess.Popen(
                [sys.executable, '-m', 'app', '--root', str(root), 'serve',
                 '--port', str(port), '--bidirectional', '--require-ready'],
                stdout=log, stderr=log, env=dict(os.environ, PYTHONUNBUFFERED='1'))
            try:
                for _ in range(60):
                    if proc.poll() is not None:
                        raise RuntimeError('serve exited before readiness')
                    try:
                        request('/health')
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('serve readiness timeout')
                html = request('/ui/').decode()
                import re
                for asset in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html):
                    assert request(asset), asset
                print('serve + UI HTML/assets: OK')
                for source in ('chatgpt', 'claude'):
                    path = root / 'agents' / source / 'outbox' / 'smoke.md'
                    path.write_bytes(f'{source} smoke reply'.encode())
                    path.with_name(path.name + '.ready').touch()
                for _ in range(60):
                    rows = json.loads(request('/api/envelopes?state=PENDING_APPROVAL'))
                    if len(rows) == 2:
                        break
                    time.sleep(.1)
                assert len(rows) == 2, rows
                for row in rows:
                    result = json.loads(request(f"/api/envelopes/{row['id']}/approve",
                                                {'actor': 'smoke'}))
                    dest = Path(result['delivered_path'])
                    assert dest.parent == root.resolve() / 'agents' / row['target_agent'] / 'inbox'
                    raw = dest.read_bytes()
                    assert raw == f"{row['source_agent']} smoke reply".encode()
                    assert hashlib.sha256(raw).hexdigest() == result['envelope']['delivered_sha256']
                    assert result['envelope']['state'] == 'DELIVERED'
                    print(f"{row['source_agent']} -> {row['target_agent']}: DELIVERED, bytes/hash OK")
            except BaseException:
                log.flush()
                print((root / 'serve.log').read_text())
                raise
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == '__main__':
    main()
