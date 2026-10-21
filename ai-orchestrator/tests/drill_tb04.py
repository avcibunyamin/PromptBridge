"""TB-04 drill: deploy/launchd.sh against the real launchd (gui/<uid>) with a throwaway label, temp root and port.

Not part of test discovery; never touches the production agent, port 8765 or ai-orchestrator/.system/.
Fake `codex`/`claude` binaries are put first on the PATH that `install` copies into the plist, which also proves
that Codex subcommands see the installer's PATH (the fake codex runs `node --version`).
Run: <venv python> tests/drill_tb04.py [PORT]   → timestamped log, exit 0 when every check passed.
"""
import json
import os
from pathlib import Path
import plistlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4

PY = sys.executable
CODE = Path(__file__).resolve().parents[1]
CURRENT = subprocess.run([PY, '-c', 'from app import store; print(store.SCHEMA_VERSION)'], cwd=CODE,
                         capture_output=True, text=True).stdout.strip()   # F5: the current schema, not a fixed one
SCRIPT = CODE / 'deploy/launchd.sh'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
LABEL = 'com.promptbridge.conductor.drill.' + uuid4().hex[:8]
ROOT = Path(tempfile.mkdtemp(prefix='conductor-tb04-'))
BIN = ROOT / 'bin'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
SERVICE = f'gui/{os.getuid()}/{LABEL}'
FAILED = []

CODEX = r'''
import json, os, subprocess, sys, time
from pathlib import Path
mode = Path('__MODE_FILE__')   # launchd passes only PATH, so the drill bakes the path in
mode = mode.read_text().strip() if mode.exists() else 'success'
def emit(m): print(json.dumps(m), flush=True)
def read(): return json.loads(sys.stdin.readline())
m = read(); emit({'id': m['id'], 'result': {}})
assert read()['method'] == 'initialized'
m = read(); emit({'id': m['id'], 'result': {'thread': {'id': 'tb04'}}})
m = read(); emit({'id': m['id'], 'result': {'turn': {'id': 't'}}})
if mode == 'hang':
    subprocess.Popen(['sleep', '300'])
    for n in range(600):
        try:
            emit({'method': 'item/agentMessage/delta', 'params': {'threadId': 'tb04', 'turnId': 't', 'itemId': 'a', 'delta': f'{n}\n'}})
        except BrokenPipeError:
            break
        time.sleep(0.5)
    time.sleep(300)
node = subprocess.run(['node', '--version'], capture_output=True, text=True)   # resolved through the agent PATH
Path('codex.txt').write_text(node.stdout.strip() or ('no node: ' + node.stderr.strip()))
emit({'method': 'item/agentMessage/delta', 'params': {'threadId': 'tb04', 'turnId': 't', 'itemId': 'a', 'delta': 'node ' + node.stdout.strip()}})
emit({'method': 'turn/completed', 'params': {'threadId': 'tb04', 'turn': {'id': 't', 'status': 'completed'}}})
sys.stdin.read()
'''
CLAUDE = r'''
import json, sys
sys.stdin.read()
print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 'tb04-claude', 'is_error': False,
                  'result': 'review ok', 'terminal_reason': 'completed'}), flush=True)
'''


def log(*parts):
    print(time.strftime('[%H:%M:%S]'), *parts, flush=True)


def check(name, ok, detail=''):
    log('PASS' if ok else 'FAIL', name, detail)
    if not ok:
        FAILED.append(name)


def env(path=None):
    e = dict(os.environ, LABEL=LABEL, ROOT=str(ROOT), PORT=str(PORT), PY=PY)
    e['PATH'] = path or f'{BIN}:{os.environ["PATH"]}'
    return e


def sh(cmd):
    r = subprocess.run([str(SCRIPT), cmd], env=env(), capture_output=True, text=True, timeout=180)
    log(f'launchd.sh {cmd} → exit {r.returncode}', (r.stdout + r.stderr).strip().splitlines()[-1:] or '')
    return r


def call(method, path, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', method=method,
                                 data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'null')


def pid_on_port():
    out = subprocess.run(['lsof', '-nP', f'-iTCP:{PORT}', '-sTCP:LISTEN', '-t'], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def wait(pred, seconds, step=0.5):
    end = time.time() + seconds
    while time.time() < end:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return None


def loaded():
    return subprocess.run(['launchctl', 'print', SERVICE], capture_output=True).returncode == 0


def message(ws_name, mode):
    (ROOT / 'codex-mode').write_text(mode)
    ws = ROOT / ws_name; ws.mkdir()
    wid = str(uuid4())
    assert call('POST', '/api/works', {'client_work_id': wid, 'title': ws_name, 'workspace_path': str(ws)})[0] == 201
    digest = call('GET', '/api/sessions/providers')[1]['digest']
    assert call('POST', f'/api/works/{wid}/messages', {'client_message_id': str(uuid4()), 'text': 'tb04', 'policy_digest': digest})[0] == 201
    sid = wait(lambda: (call('GET', f'/api/works/{wid}/messages')[1]['items'][0]['session'] or {}).get('id'), 30)
    return ws, sid


def status(sid):
    return call('GET', f'/api/sessions/{sid}')[1]['session']['status']


def main():
    (ROOT / '.system').mkdir(); BIN.mkdir()
    (ROOT / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n')
    for name, source in (('codex', CODEX), ('claude', CLAUDE)):
        f = BIN / name; f.write_text('#!' + PY + '\n' + source.replace('__MODE_FILE__', str(ROOT / 'codex-mode'))); f.chmod(0o700)
    log('label', LABEL, 'root', ROOT, 'port', PORT)

    # DoD 5a: a busy port is refused before anything is loaded
    blocker = subprocess.Popen([PY, '-m', 'http.server', str(PORT), '--bind', '127.0.0.1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait(pid_on_port, 10)
    r = sh('install')
    check('DoD5a install refuses a busy port', r.returncode != 0 and 'in use' in r.stderr and not loaded() and not PLIST.exists())
    blocker.terminate(); blocker.wait(5); wait(lambda: pid_on_port() is None, 10)

    # DoD 1, 2, 6
    r = sh('install')
    lint = subprocess.run(['plutil', '-lint', str(PLIST)], capture_output=True, text=True)
    plist = plistlib.load(open(PLIST, 'rb'))
    check('DoD1 plist lint, argv only, PATH only, KeepAlive/Throttle',
          lint.returncode == 0 and set(plist['EnvironmentVariables']) == {'PATH'} and plist['EnvironmentVariables']['PATH'].startswith(str(BIN))
          and plist['ProgramArguments'][:3] == [PY, '-m', 'app'] and plist['KeepAlive'] is True and plist['ThrottleInterval'] == 30
          and plist['RunAtLoad'] is True, json.dumps({k: plist[k] for k in ('ProgramArguments', 'KeepAlive', 'ThrottleInterval')}))
    health = call('GET', '/health')[1]
    installed = call('GET', '/api/sessions/providers')[1]['installed']
    check('DoD2 bootstrap → /health 200 with version', r.returncode == 0 and health['version']['commit'] != 'unknown'
          and health['version']['schema'] == CURRENT, json.dumps(health['version']))
    ws, sid = message('ws-node', 'success')
    final = wait(lambda: status(sid) if status(sid) in ('SUCCEEDED', 'FAILED') else None, 60)
    node = (ws / 'codex.txt').read_text() if (ws / 'codex.txt').exists() else ''
    check('DoD6 agent env: providers found, Codex subcommand sees the installer PATH',
          installed.get('codex') is True and installed.get('claude') is True and final == 'SUCCEEDED' and node.startswith('v'),  # F5: agy/ollama may be listed too
          f'installed={installed} session={final} node={node!r}')

    # DoD 7b: install is idempotent
    before = pid_on_port()
    r = sh('install')
    after = pid_on_port()
    check('DoD7b second install replaces the agent', r.returncode == 0 and after and after != before and loaded(), f'pid {before} → {after}')

    # DoD 4: kill -TERM does not stop it (KeepAlive)
    time.sleep(31)   # past ThrottleInterval so the restart is prompt
    old = pid_on_port()
    if old:
        os.kill(old, signal.SIGTERM)
    new = old and wait(lambda: (p := pid_on_port()) and p != old and p, 45)
    check('DoD4 kill -TERM → launchd restarts it', bool(new) and call('GET', '/health')[0] == 200, f'pid {old} → {new}')

    # DoD 3: crash during a run → restart → RECOVERY_REQUIRED, lock kept → confirmed recovery
    time.sleep(31)
    ws, sid = message('ws-crash', 'hang')
    wait(lambda: status(sid) == 'RUNNING', 30)
    time.sleep(1)
    old = pid_on_port()
    if old:
        os.kill(old, signal.SIGKILL)
    new = old and wait(lambda: (p := pid_on_port()) and p != old and p, 45)
    time.sleep(1)
    after_crash = status(sid)
    lock = call('GET', f'/api/sessions/{sid}/recovery')[1].get('lock_retained')
    rec = call('POST', f'/api/sessions/{sid}/recover', {'confirm': True})
    check('DoD3 SIGKILL mid-run → restart → RECOVERY_REQUIRED, lock kept → recovered',
          bool(new) and after_crash == 'RECOVERY_REQUIRED' and lock is True and rec[0] == 200,
          f'pid {old} → {new} status={after_crash} lock_retained={lock} recover={rec[0]}')

    # DoD 4: bootout stops it for good
    r = sh('uninstall')
    time.sleep(35)
    check('DoD4 uninstall (bootout) stops it; nothing comes back', r.returncode == 0 and not loaded() and not PLIST.exists()
          and pid_on_port() is None)

    # DoD 5b: a permanently failing start is throttled, not a tight loop
    db = sqlite3.connect(ROOT / '.system/state.db'); db.execute("UPDATE schema_meta SET value='9.9'"); db.commit(); db.close()
    log_file = ROOT / '.system/server-launchd.log'
    mark = log_file.read_text().count('Traceback')   # one traceback per failed start
    r = sh('install')
    time.sleep(70)
    starts = log_file.read_text().count('Traceback') - mark
    check('DoD5b failing start is throttled (~every 30 s) and install reports failure',
          r.returncode != 0 and 2 <= starts <= 5, f'failed starts in ~90 s (20 s install wait + 70 s): {starts}')
    sh('uninstall')

    log('RESULT', 'ALL PASS' if not FAILED else 'FAILED: ' + ', '.join(FAILED), '| root', ROOT)
    return 1 if FAILED else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        subprocess.run([str(SCRIPT), 'uninstall'], env=env(), capture_output=True)   # never leave the drill agent behind
        subprocess.run(['pkill', '-f', str(ROOT)], capture_output=True)
    sys.exit(code)
