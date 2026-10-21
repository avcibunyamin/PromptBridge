"""F5-Kapanış gate C: the mobile gateway against a real `serve` process (not part of test discovery).

Temp root only; never touches .system/ of the live instance or port 8765.
1) mobile.json on → main + gateway both listen on 127.0.0.1 only;
2) pairing code on the main port → pair through the gateway (Host + Tailscale header) → token works;
3) an open SSE stream through the gateway ends within seconds when the device is revoked on the main port;
4) mobile.json removed → after restart the gateway port is closed.
Run: .venv/bin/python tests/drill_f5_mobile.py [MAIN_PORT] [GATEWAY_PORT]
"""
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

PY = sys.executable
CODE = Path(__file__).resolve().parents[1]
MAIN = int(sys.argv[1]) if len(sys.argv) > 1 else 8893
GW = int(sys.argv[2]) if len(sys.argv) > 2 else 8894
HOST, LOGIN = 'drill.tail0000.ts.net', 'drill@example.com'
FAILED = []


def log(*parts):
    print(time.strftime('[%H:%M:%S]'), *parts, flush=True)


def check(name, ok, detail=''):
    log('PASS' if ok else 'FAIL', name, detail)
    if not ok:
        FAILED.append(name)


def request(port, method, path, body=None, headers=None, timeout=10):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    c.request(method, path, body=None if body is None else json.dumps(body),
              headers={'Content-Type': 'application/json', **(headers or {})})
    r = c.getresponse(); data = r.read(); c.close()
    return r.status, (json.loads(data) if data and r.getheader('content-type', '').startswith('application/json') else data)


def listeners(port):
    out = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'], capture_output=True, text=True).stdout
    return [line.split()[8] for line in out.splitlines()[1:]]


def serve(root):
    proc = subprocess.Popen([PY, '-m', 'app', '--root', str(root), 'serve', '--port', str(MAIN)], cwd=CODE,
                            stdout=open(root / 'server.log', 'a'), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        try:
            if request(MAIN, 'GET', '/health')[0] == 200:
                return proc
        except OSError:
            if proc.poll() is not None:
                return proc
        time.sleep(0.25)
    return proc


def stop(proc):
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGTERM); proc.wait(15)


def main():
    root = Path(tempfile.mkdtemp(prefix='conductor-f5-mobile-')); (root / '.system').mkdir()
    (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
    (root / '.system/mobile.json').write_text(json.dumps({'enabled': True, 'host': HOST, 'port': GW}))
    log('root', root)
    proc = serve(root)
    time.sleep(1)
    check('C-M1 both listeners on 127.0.0.1 only', listeners(MAIN) == [f'127.0.0.1:{MAIN}'] and listeners(GW) == [f'127.0.0.1:{GW}'],
          f'main={listeners(MAIN)} gateway={listeners(GW)}')

    gw = {'Host': HOST, 'Tailscale-User-Login': LOGIN}
    code = request(MAIN, 'POST', '/api/mobile/pairing')[1]['code']
    status, paired = request(GW, 'POST', '/api/mobile/pair', {'code': code, 'device_name': 'drill'}, gw)
    token = paired.get('token') if isinstance(paired, dict) else None
    auth = {**gw, 'Authorization': f'Bearer {token}'}
    works = request(GW, 'GET', '/api/works', headers=auth)[0]
    check('C-M2 pair through the gateway → token works; main port has no pair/me', status == 200 and works == 200
          and request(MAIN, 'GET', '/api/mobile/me')[0] == 404, f'pair={status} works={works}')

    ended = threading.Event(); opened = threading.Event()

    def stream():
        c = http.client.HTTPConnection('127.0.0.1', GW, timeout=30)
        c.request('GET', '/api/events', headers={**auth, 'Accept': 'text/event-stream'})
        r = c.getresponse()
        try:
            if r.status == 200:
                opened.set()
                while r.read1(1024):
                    pass
        except http.client.IncompleteRead:
            pass                                  # the server dropped the stream mid-chunk: it ended
        finally:
            ended.set(); c.close()

    t = threading.Thread(target=stream, daemon=True); t.start()
    opened.wait(5); time.sleep(1)
    still_open = opened.is_set() and not ended.is_set()
    device = request(MAIN, 'GET', '/api/mobile/status')[1]['devices'][0]['id']
    revoked_at = time.monotonic()
    revoke = request(MAIN, 'DELETE', f'/api/mobile/devices/{device}')[0]
    ended.wait(10)
    took = round(time.monotonic() - revoked_at, 2)
    check('C-M3 open SSE stream ends when the device is revoked', still_open and revoke == 200 and ended.is_set() and took < 5
          and request(GW, 'GET', '/api/works', headers=auth)[0] == 401, f'open_before={still_open} revoke={revoke} ended_after={took}s')
    stop(proc)

    (root / '.system/mobile.json').unlink()
    proc = serve(root); time.sleep(1)
    check('C-M4 gateway off → its port stays closed', request(MAIN, 'GET', '/health')[0] == 200 and listeners(GW) == [],
          f'gateway={listeners(GW)}')
    stop(proc)
    log('RESULT', 'ALL PASS' if not FAILED else 'FAILED: ' + ', '.join(FAILED), '| root', root)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
