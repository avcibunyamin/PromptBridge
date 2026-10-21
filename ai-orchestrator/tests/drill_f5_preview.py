"""F5-Kapanış gate C: preview manager against a real `serve` process (not part of test discovery).

Temp root and a static-site project only; never touches the live instance.
detect → start refused before approval → approve → start → page answers on 127.0.0.1 → stop → the whole
process group is gone → a restart of `serve` reconciles nothing back to life.
Run: .venv/bin/python tests/drill_f5_preview.py [PORT]
"""
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse

PY = sys.executable
CODE = Path(__file__).resolve().parents[1]
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8895
FAILED = []


def log(*parts):
    print(time.strftime('[%H:%M:%S]'), *parts, flush=True)


def check(name, ok, detail=''):
    log('PASS' if ok else 'FAIL', name, detail)
    if not ok:
        FAILED.append(name)


def call(method, path, body=None, port=PORT, host='127.0.0.1'):
    c = http.client.HTTPConnection(host, port, timeout=30)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={'Content-Type': 'application/json'})
    r = c.getresponse(); data = r.read(); c.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


def serve(root):
    proc = subprocess.Popen([PY, '-m', 'app', '--root', str(root), 'serve', '--port', str(PORT)], cwd=CODE,
                            stdout=open(root / 'server.log', 'a'), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        try:
            if call('GET', '/health')[0] == 200:
                return proc
        except OSError:
            time.sleep(0.25)
    return proc


def stop(proc):
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGTERM); proc.wait(15)


def group_alive(pgid):
    try:
        os.killpg(pgid, 0); return True
    except ProcessLookupError:
        return False


def main():
    root = Path(tempfile.mkdtemp(prefix='conductor-f5-preview-')); (root / '.system').mkdir()
    (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
    project = root / 'site'; project.mkdir(); (project / 'index.html').write_text('<h1>f5-preview-ok</h1>')
    log('root', root)
    proc = serve(root)
    q = '?workspace_path=' + urllib.parse.quote(str(project))
    detected = call('GET', '/api/previews/detect' + q)[1]
    body = {'workspace_path': str(project), 'command_digest': detected['command_digest']}
    refused = call('POST', '/api/previews/start', body)[0]
    approved = call('POST', '/api/previews/approve', body)[0]
    status, started = call('POST', '/api/previews/start', body)
    server = started.get('server', {}) if isinstance(started, dict) else {}
    url = urllib.parse.urlsplit(server.get('url') or detected.get('url', ''))
    page = call('GET', '/', port=url.port, host=url.hostname) if url.port else (0, b'')
    check('C-P1 approval gate, start, page on 127.0.0.1', refused == 409 and approved == 200 and status == 200
          and url.hostname == '127.0.0.1' and page[0] == 200 and b'f5-preview-ok' in page[1],
          f'kind={detected.get("kind")} refused={refused} start={status} url={server.get("url")} page={page[0]}')
    row = call('GET', '/api/previews/status' + q)[1].get('server') or {}
    pgid = row.get('pgid') or server.get('pgid')
    alive_running = bool(pgid) and group_alive(pgid)
    stopped = call('POST', '/api/previews/stop', {'workspace_path': str(project)})
    time.sleep(0.5)
    check('C-P2 stop ends the whole process group', stopped[0] == 200 and alive_running and not group_alive(pgid),
          f'pgid={pgid} alive_while_running={alive_running} alive_after_stop={bool(pgid) and group_alive(pgid)}')
    stop(proc)
    proc = serve(root)
    after = call('GET', '/api/previews/status' + q)[1].get('server') or {}
    check('C-P3 restart of serve brings nothing back', after.get('status') in ('stopped', None) and not group_alive(pgid),
          f'status_after_restart={after.get("status")}')
    stop(proc)
    log('RESULT', 'ALL PASS' if not FAILED else 'FAILED: ' + ', '.join(FAILED), '| root', root)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
