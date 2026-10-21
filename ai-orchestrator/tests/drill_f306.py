"""F3-06 gate D: resilience drills against a real server process (not part of test discovery).

Real `python -m app serve` processes, real Codex/Claude adapters and process groups; only the provider
binaries are local fakes (CONDUCTOR_CODEX_BIN / CONDUCTOR_CLAUDE_BIN). No model, no account quota, no
production data: everything lives in a fresh temp root. Time is simulated only where F3-04 waits
15+ minutes: with the server stopped, a plan's `next_retry_at` is moved into the past (logged).
Run: .venv/bin/python tests/drill_f306.py [PORT]   → prints a timestamped log, exit 0 when every check passed.
"""
import json
import os
from pathlib import Path
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
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8891
BASE = f'http://127.0.0.1:{PORT}'
ROOT = Path(tempfile.mkdtemp(prefix='conductor-f306-drill-'))
DB = ROOT / '.system/state.db'
FAILED = []

CODEX = r'''
import json, os, subprocess, sys, time
from pathlib import Path
ROOT = Path(os.environ['DRILL_ROOT'])
mode = (ROOT / 'codex-mode').read_text().strip()
if mode == 'context_once':
    flag = ROOT / 'context-used'
    mode = 'success' if flag.exists() else 'context'
    flag.touch()
def emit(m):
    print(json.dumps(m), flush=True)
def read():
    return json.loads(sys.stdin.readline())
m = read(); emit({'id': m['id'], 'result': {}})
assert read()['method'] == 'initialized'
m = read(); thread = 'drill-thread-' + str(os.getpid())
emit({'id': m['id'], 'result': {'thread': {'id': thread}}})
m = read(); emit({'id': m['id'], 'result': {'turn': {'id': 't'}}})
def done(status, error=None):
    turn = {'id': 't', 'status': status}
    if error:
        turn['error'] = {'codexErrorInfo': error, 'message': 'drill ' + error}
    emit({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': turn}})
    sys.stdin.read()
if mode == 'success':
    Path('codex.txt').write_text('codex drill output\n')
    emit({'method': 'item/agentMessage/delta', 'params': {'threadId': thread, 'turnId': 't', 'itemId': 'a', 'delta': 'drill ok'}})
    done('completed')
elif mode == 'quota':
    done('failed', 'usageLimitExceeded')
elif mode == 'context':
    done('failed', 'contextWindowExceeded')
elif mode == 'hang':
    subprocess.Popen(['sleep', '300'])   # a child in the owned process group
    for n in range(600):
        try:
            emit({'method': 'item/agentMessage/delta', 'params': {'threadId': thread, 'turnId': 't', 'itemId': 'a', 'delta': f'tick {n}\n'}})
        except BrokenPipeError:
            break
        time.sleep(0.5)
    time.sleep(300)
'''
CLAUDE = r'''
import json, sys
from pathlib import Path
write = sys.argv[sys.argv.index('--permission-mode') + 1] == 'acceptEdits'
sys.stdin.read()
if write:
    Path('claude.txt').write_text('claude drill output\n')
print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 'claude-drill', 'is_error': False,
                  'result': 'drill review: no changes needed' if not write else 'drill author done',
                  'terminal_reason': 'completed'}), flush=True)
'''


def log(*parts):
    print(time.strftime('[%H:%M:%S]'), *parts, flush=True)


def check(name, ok, detail=''):
    log(('PASS' if ok else 'FAIL'), name, detail)
    if not ok:
        FAILED.append(name)


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'null')


def start():
    env = dict(os.environ, CONDUCTOR_CODEX_BIN=str(ROOT / 'bin/codex'), CONDUCTOR_CLAUDE_BIN=str(ROOT / 'bin/claude'),
               DRILL_ROOT=str(ROOT))
    out = open(ROOT / 'server.log', 'a')
    proc = subprocess.Popen([PY, '-m', 'app', '--root', str(ROOT), 'serve', '--port', str(PORT)], cwd=CODE, env=env,
                            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        try:
            if call('GET', '/health')[0] == 200:
                log('server up pid', proc.pid); return proc
        except OSError:
            pass
        time.sleep(0.25)
    raise SystemExit('server did not start')


def stop(proc, sig=signal.SIGTERM):
    os.kill(proc.pid, sig); proc.wait(15); log('server', proc.pid, 'stopped by', signal.Signals(sig).name)


def db():
    return sqlite3.connect(DB)


def work(name, team=None, **wait):
    ws = ROOT / ('ws-' + name); ws.mkdir(); (ws / 'readme.txt').write_text('v1\n')
    wid = str(uuid4())
    assert call('POST', '/api/works', {'client_work_id': wid, 'title': name, 'workspace_path': str(ws)})[0] == 201
    digest = (call('GET', '/api/sessions/providers/preview?author=' + ','.join(team['author']) + '&reviewer=' +
                   ','.join(team['reviewer']))[1]['digest'] if team else call('GET', '/api/sessions/providers')[1]['digest'])
    body = dict({'client_message_id': str(uuid4()), 'text': 'drill goal', 'policy_digest': digest}, **wait)
    if team:
        body['team'] = team
    code, msg = call('POST', f'/api/works/{wid}/messages', body)
    assert code == 201, msg
    return wid, ws


def session_of(wid):
    for _ in range(120):
        items = call('GET', f'/api/works/{wid}/messages')[1]['items']
        if items and items[0]['session']:
            return items[0]['session']['id']
        time.sleep(0.25)
    raise SystemExit('never dispatched')


def wait_status(sid, wanted, seconds=30):
    status = None
    for _ in range(int(seconds / 0.25)):
        status = call('GET', f'/api/sessions/{sid}')[1]['session']['status']
        if status in wanted:
            return status
        time.sleep(0.25)
    return status


def group_alive(pgid):
    try:
        os.killpg(pgid, 0); return True
    except ProcessLookupError:
        return False


def main():
    (ROOT / '.system').mkdir(); (ROOT / 'bin').mkdir()
    (ROOT / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n'
                                                  'allowed:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
    for name, source in (('codex', CODEX), ('claude', CLAUDE)):
        f = ROOT / 'bin' / name; f.write_text('#!' + PY + '\n' + source); f.chmod(0o700)
    log('root', ROOT)
    server = start()
    health = call('GET', '/health')[1]
    check('J /health version', set(health.get('version', {})) == {'commit', 'schema', 'ui_bundle'} and
          health['version']['schema'] == CURRENT, json.dumps(health['version']))

    # D1 quota → authorized fallback (F2-03a)
    (ROOT / 'codex-mode').write_text('quota')
    wid, ws = work('d1-fallback', team={'author': ['codex', 'claude'], 'reviewer': ['claude']})
    sid = session_of(wid)
    status = wait_status(sid, {'SUCCEEDED', 'FAILED', 'WAITING_CAPACITY'})
    with db() as c:
        attempts = [r[0] for r in c.execute("SELECT a.status FROM session_attempts a JOIN session_steps s ON s.id=a.step_id "
                                            "WHERE s.session_id=? ORDER BY s.step_index, a.attempt_no", (sid,))]
        handoffs = c.execute("SELECT count(*) FROM session_events WHERE session_id=? AND kind='handoff'", (sid,)).fetchone()[0]
    check('D1 quota → fallback', status == 'SUCCEEDED' and attempts[:2] == ['QUOTA', 'COMPLETED'] and handoffs >= 1
          and (ws / 'claude.txt').exists(), f'status={status} attempts={attempts} handoffs={handoffs}')

    # D6 context exhausted → bounded continuation in a fresh thread (F2-03b)
    (ROOT / 'codex-mode').write_text('context_once')
    wid, ws = work('d6-context')
    sid = session_of(wid)
    status = wait_status(sid, {'SUCCEEDED', 'FAILED', 'WAITING_CONTEXT'})
    with db() as c:
        attempts = [(r[0], r[1]) for r in c.execute("SELECT a.status, a.continuation_no FROM session_attempts a JOIN session_steps s "
                                                    "ON s.id=a.step_id WHERE s.session_id=? AND s.step_index=0 ORDER BY a.attempt_no", (sid,))]
        cont = c.execute("SELECT count(*) FROM session_events WHERE session_id=? AND kind='context_continuation'", (sid,)).fetchone()[0]
    check('D6 context → continuation', status == 'SUCCEEDED' and attempts == [('CONTEXT_EXHAUSTED', 0), ('COMPLETED', 1)] and cont == 1,
          f'status={status} step0={attempts} continuation_events={cont}')

    # D2/D3/D4: opt-in quota wait plans (F3-04)
    (ROOT / 'codex-mode').write_text('quota')
    plans = {}
    for name in ('d2-retry', 'd3-changed', 'd4-fired'):
        wid, ws = work(name, wait_on_quota=True, wait_deadline_hours=4, wait_max_attempts=3)
        sid = session_of(wid)
        plans[name] = (wid, ws, sid, wait_status(sid, {'WAITING_CAPACITY', 'FAILED', 'SUCCEEDED'}))
    check('D2-4 quota → WAITING_CAPACITY + plan', all(p[3] == 'WAITING_CAPACITY' for p in plans.values()) and all(
        call('GET', f'/api/sessions/{p[2]}')[1]['wait_plan']['status'] == 'PENDING' for p in plans.values()),
        json.dumps({k: v[3] for k, v in plans.items()}))
    stop(server)
    (ROOT / 'codex-mode').write_text('success')
    (plans['d3-changed'][1] / 'readme.txt').write_text('edited by someone else\n')
    with db() as c:
        past = '2000-01-01T00:00:00.000000+00:00'
        for name in ('d2-retry', 'd3-changed'):
            c.execute("UPDATE retry_plans SET next_retry_at=? WHERE session_id=?", (past, plans[name][2]))
        c.execute("UPDATE retry_plans SET status='FIRED', fired_at=? WHERE session_id=?", (past, plans['d4-fired'][2]))
    log('SIMULATED: d2/d3 next_retry_at moved to the past; d4 plan set FIRED as if the server stopped mid-fire')
    server = start()   # reconcile, then the scheduler's first pass runs immediately
    d2 = wait_status(plans['d2-retry'][2], {'SUCCEEDED', 'FAILED'})
    view = call('GET', f"/api/sessions/{plans['d2-retry'][2]}")[1]['wait_plan']
    check('D2 due plan → automatic retry → SUCCEEDED', d2 == 'SUCCEEDED' and view['status'] == 'FIRED' and view['attempts_so_far'] == 1,
          f"status={d2} plan={view['status']} attempts={view['attempts_so_far']}")
    d3 = wait_status(plans['d3-changed'][2], {'WAITING_HUMAN'})
    blocked = call('GET', f"/api/works/{plans['d3-changed'][0]}")[1]['blocked_reason']
    check('D3 folder changed while waiting → WAITING_HUMAN, queue held', d3 == 'WAITING_HUMAN' and blocked == 'run_waiting',
          f'status={d3} blocked_reason={blocked}')
    code, body = call('POST', f"/api/sessions/{plans['d3-changed'][2]}/cancel")
    check('D3 WAITING_HUMAN → cancel', code == 202 and body == {'status': 'CANCELLED'}, f'{code} {body}')
    view = call('GET', f"/api/sessions/{plans['d4-fired'][2]}")[1]
    with db() as c:
        ev = [json.loads(r[0])['reason'] for r in c.execute("SELECT payload FROM session_events WHERE session_id=? AND kind='wait_rescheduled'",
                                                           (plans['d4-fired'][2],))]
    check('D4 FIRED plan + restart → PENDING again (no session auto-start)', view['session']['status'] == 'WAITING_CAPACITY'
          and view['wait_plan']['status'] == 'PENDING' and ev == ['server_restart'], f"status={view['session']['status']} plan={view['wait_plan']['status']} events={ev}")

    # D5 crash during a run → RECOVERY_REQUIRED → confirmed recovery (F2-03c)
    (ROOT / 'codex-mode').write_text('hang')
    wid, ws = work('d5-crash')
    sid = session_of(wid)
    wait_status(sid, {'RUNNING'})
    owners = []
    for _ in range(40):
        with db() as c:
            owners = c.execute("SELECT o.pgid FROM process_owners o JOIN session_attempts a ON a.id=o.attempt_id JOIN session_steps s "
                               "ON s.id=a.step_id WHERE s.session_id=?", (sid,)).fetchall()
        if owners:
            break
        time.sleep(0.25)
    time.sleep(1)
    pgid = owners[0][0]
    stop(server, signal.SIGKILL)
    alive_after_crash = group_alive(pgid)
    server = start()
    status = call('GET', f'/api/sessions/{sid}')[1]['session']['status']
    detail = call('GET', f'/api/sessions/{sid}/recovery')[1]
    no_confirm = call('POST', f'/api/sessions/{sid}/recover', {})[0]
    code, result = call('POST', f'/api/sessions/{sid}/recover', {'confirm': True})
    time.sleep(0.5)
    with db() as c:
        locks = c.execute('SELECT count(*) FROM workspace_locks').fetchone()[0]
    check('D5 crash → RECOVERY_REQUIRED, lock kept → confirmed recovery reaps the group',
          alive_after_crash and status == 'RECOVERY_REQUIRED' and detail['lock_retained'] and no_confirm == 422 and code == 200
          and result['status'] == 'FAILED' and not group_alive(pgid) and locks == 0,
          f'group_alive_after_crash={alive_after_crash} status={status} lock_retained={detail["lock_retained"]} '
          f'no_confirm={no_confirm} recover={code}/{result.get("status")} group_alive_now={group_alive(pgid)} locks={locks}')

    stop(server)
    log('RESULT', 'ALL PASS' if not FAILED else 'FAILED: ' + ', '.join(FAILED), '| root', ROOT)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
