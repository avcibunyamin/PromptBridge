"""K-039: remaining provider quota, shown only (it never steers a run or a hand-off).

agy: the interactive `/usage` screen per account, read in a pty (no message is typed, so no model call).
Codex: app-server `account/rateLimits/read` per account. Claude: the `rate_limit_event`s its runs emit,
kept in memory. Served from memory; refreshed on an explicit Mac request or after an agy/Codex step.
"""
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from app.provider_policy import split

AGY_TIMEOUT, CODEX_TIMEOUT, AUTO_GAP = 30.0, 20.0, 120.0
AUTO = False                   # `serve` turns on the refresh after a step; tests and tools never start one
_lock = threading.Lock()
_cache = {}                     # base identity (`agy@hesap1`, `codex`, `claude`) -> account snapshot
_running = set()                # bases being read right now
# Cursor moves become line breaks (a redrawn screen would otherwise glue its rows together); the rest goes.
_MOVES = re.compile(r'\x1b\[[0-9;]*[HfBEd]')
_ANSI = re.compile(r'\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]')


def _text(screen):
    # A bare carriage return redraws the row from its start (a spinner, then the title): a new line here.
    return _ANSI.sub('', _MOVES.sub('\n', screen)).replace('\r\n', '\n').replace('\r', '\n').replace('\x08', '')


def _window_kind(label):
    text = label.lower()
    return 'weekly' if 'week' in text else 'five_hour' if 'five hour' in text or '5 hour' in text else 'other'


def _group_key(title):
    text = title.lower()
    return 'argon' if 'argon' in text else 'claude_gpt' if 'claude' in text or 'gpt' in text else \
        'gemini' if 'gemini' in text else re.sub(r'[^a-z0-9]+', '_', text).strip('_')


def parse_agy_usage(screen):
    """agy 1.4.2 `/usage`: GROUP TITLE / Models within this group: … / <label> Limit … / [bar] NN.NN% /
    `Quota available` or `Refreshes in 167h 37m`. Raises ValueError when no group is found."""
    groups, group, window = [], None, None
    for raw in _text(screen).splitlines():
        line = raw.strip().strip('│┃').strip()
        if not line:
            continue
        if re.fullmatch(r'[A-Z0-9][A-Z0-9 .&-]{3,}', line):
            group = {'key': _group_key(line), 'title': line.title(), 'models': None, 'windows': []}
            if all(g['key'] != group['key'] for g in groups):
                groups.append(group)
            else:
                group = next(g for g in groups if g['key'] == group['key'])   # the screen redraws
            window = None
        elif group is None:
            continue
        elif line.startswith('Models within this group:'):
            group['models'] = line.split(':', 1)[1].strip()
        elif 'Limit' in line and '%' not in line:
            kind_ = _window_kind(line)
            window = next((w for w in group['windows'] if w['kind'] == kind_), None)
            if window is None:
                window = {'label': {'weekly': 'Haftalık', 'five_hour': '5 saat'}.get(kind_, line[:40]), 'kind': kind_,
                          'remaining': None, 'resets_in': None}
                group['windows'].append(window)
        elif window is not None and (m := re.search(r'(\d{1,3}(?:\.\d+)?)%', line)):
            window['remaining'] = float(m[1])
        elif window is not None and line.startswith('Refreshes in'):
            window['resets_in'] = line[len('Refreshes in'):].strip()[:40]
    groups = [g for g in groups if any(w['remaining'] is not None for w in g['windows'])]
    if not groups:
        raise ValueError('no quota group on the /usage screen')
    return groups


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _agy(db_path, account):
    """(groups, error). Interactive `agy` in the account's own HOME, in Conductor's empty probe folder."""
    from app.adapters.agy import agy_binary, agy_home, prepare_home
    probe_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), f'agy-probe-{account}')   # own per account
    try:
        os.makedirs(probe_dir, 0o700, exist_ok=True)
        if os.listdir(probe_dir):
            return [], 'probe_dir_not_empty'                  # never let agy see files here
        binary = agy_binary()
        env = {**prepare_home(agy_home(db_path, account)), 'TERM': 'xterm-256color', 'COLUMNS': '160', 'LINES': '90'}
    except Exception:
        return [], 'unavailable'
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 90, 160, 0, 0))
        proc = subprocess.Popen([binary], stdin=slave, stdout=slave, stderr=slave, cwd=probe_dir, env=env,
                                start_new_session=True)
    except Exception:
        os.close(master)
        return [], 'unavailable'
    finally:
        os.close(slave)
    buf, deadline = bytearray(), time.monotonic() + AGY_TIMEOUT

    def pump(seconds, until=None):
        end = min(deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk[: (1 << 20) - len(buf)])
                if until and until(bytes(buf)):
                    return
    # "not signed in" flashes before "Signing in..." on every start, so only the final prompts count.
    ready = lambda b: b'for shortcuts' in b or b'login method' in b.lower() or b'trust the contents' in b
    try:
        pump(15, ready)
        if b'login method' in bytes(buf).lower():
            return [], 'unauthenticated'
        if b'trust the contents' in buf:
            mark = len(buf)                                    # only Conductor's own empty probe folder
            os.write(master, b'\r')                            # default choice: "Yes, I trust this folder"
            pump(10, lambda b: b'for shortcuts' in b[mark:] or b'login method' in b[mark:].lower())
            if b'login method' in bytes(buf[mark:]).lower():
                return [], 'unauthenticated'
        if b'for shortcuts' not in buf:
            return [], 'timeout' if time.monotonic() >= deadline else 'unavailable'
        os.write(master, b'\x1b')                              # dismiss a notice, if any
        pump(1)
        mark = len(buf)
        os.write(master, b'/usage')
        pump(1.5)
        os.write(master, b'\r')
        # The viewer opens with "Loading quota summary..."; every window then ends in one of these two lines.
        pump(12, lambda b: b'Quota available' in b[mark:] or b'Refreshes in' in b[mark:])
        pump(1.5)
        screen = bytes(buf[mark:]).decode('utf-8', 'replace')
    finally:
        try:
            os.write(master, b'\x1b\x03\x03')
        except OSError:
            pass
        _kill(proc)
        os.close(master)
    try:
        return parse_agy_usage(screen), None
    except ValueError:
        return [], 'timeout' if time.monotonic() >= deadline else 'unavailable'


def codex_groups(result):
    """account/rateLimits/read -> groups (codex-cli app-server, verified 2026-10-18)."""
    limits = (result or {}).get('rateLimits') or {}
    windows = []
    for name in ('primary', 'secondary'):
        w = limits.get(name)
        if not isinstance(w, dict) or not isinstance(w.get('usedPercent'), (int, float)):
            continue
        mins = w.get('windowDurationMins')
        kind = 'five_hour' if mins == 300 else 'weekly' if mins == 10080 else 'other'
        label = {300: '5 saat', 10080: 'Haftalık', 43200: '30 gün'}.get(mins, f'{mins} dk' if mins else name)
        windows.append({'label': label, 'kind': kind, 'remaining': max(0.0, 100.0 - float(w['usedPercent'])),
                        'resets_at': w.get('resetsAt') if isinstance(w.get('resetsAt'), int) else None})
    if not windows:
        raise ValueError('no codex rate limit window')
    return [{'key': 'codex', 'title': 'Codex' + (f" ({limits['planType']})" if limits.get('planType') else ''),
             'models': None, 'windows': windows}]


def _codex(db_path, base):
    from app.adapters.codex import codex_binary, codex_home, codex_env, CODEX_ARGS
    from app.catalog import _codex_request
    from app import provider_auth
    try:
        _, account, _, _ = split(base)
        proc = subprocess.Popen([codex_binary(), *CODEX_ARGS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env=codex_env(codex_home(db_path, account)), cwd='/')
    except Exception:
        return [], 'unavailable'
    try:
        deadline = time.monotonic() + CODEX_TIMEOUT
        _codex_request(proc, 1, 'initialize', {'clientInfo': {'name': 'conductor', 'title': 'Conductor',
                                                              'version': '0.2.0'}}, deadline)
        proc.stdin.write(b'{"method":"initialized"}\n'); proc.stdin.flush()
        return codex_groups(_codex_request(proc, 2, 'account/rateLimits/read', {}, deadline)), None
    except TimeoutError:
        return [], 'timeout'
    except Exception as exc:
        return [], 'unauthenticated' if 'not logged in' in str(exc).lower() else 'unavailable'
    finally:
        provider_auth._cleanup(proc)
        if proc.stdin:
            proc.stdin.close()


def record_claude(info):
    """A Claude `rate_limit_event`'s rate_limit_info (Claude Code 2.1.299): one window per rateLimitType."""
    if not isinstance(info, dict) or not isinstance(info.get('rateLimitType'), str):
        return
    kind_ = info['rateLimitType']
    used = info.get('utilization')
    if isinstance(used, (int, float)):
        used = used * 100 if used <= 1 else used
    window = {'label': {'five_hour': '5 saat', 'seven_day': 'Haftalık', 'seven_day_opus': 'Haftalık (Opus)',
                        'seven_day_sonnet': 'Haftalık (Sonnet)'}.get(kind_, kind_[:40]),
              'kind': 'five_hour' if kind_ == 'five_hour' else 'weekly' if kind_.startswith('seven_day') else 'other',
              'remaining': max(0.0, 100.0 - float(used)) if isinstance(used, (int, float)) else None,
              'status': str(info.get('status'))[:30] if info.get('status') else None,
              'resets_at': info.get('resetsAt') if isinstance(info.get('resetsAt'), int) else None, 'id': kind_}
    with _lock:
        entry = _cache.setdefault('claude', {'groups': [{'key': 'claude', 'title': 'Claude', 'models': None,
                                                         'windows': []}], 'error': None, 'source': 'run'})
        windows = [w for w in entry['groups'][0]['windows'] if w.get('id') != kind_] + [window]
        entry['groups'][0]['windows'] = windows
        entry['checked_at'] = time.time()


def _bases(config):
    from app.catalog import _bases as catalog_bases
    return catalog_bases(config, 'agy') + catalog_bases(config, 'codex')


def _refresh_one(db_path, base):
    """One reading per account at a time: a second request while one runs is skipped (it gets that result)."""
    with _lock:
        if base in _running:
            return
        _running.add(base)
    try:
        kind_ = split(base)[0]
        groups, error = _agy(db_path, split(base)[1]) if kind_ == 'agy' else _codex(db_path, base)
        with _lock:
            _cache[base] = {'groups': groups, 'error': error, 'checked_at': time.time(), 'source': 'probe'}
    finally:
        with _lock:
            _running.discard(base)


def refresh(db_path, config):
    """Every configured agy account and Codex base, one after another (an explicit Mac request)."""
    bases = [b for b in _bases(config) if not (split(b)[0] == 'agy' and split(b)[1] is None)]   # bare agy: no home
    threads = [threading.Thread(target=_refresh_one, args=(db_path, b), daemon=True) for b in bases]
    for t in threads:
        t.start()
    deadline = time.monotonic() + AGY_TIMEOUT + 10
    for t in threads:
        t.join(max(0, deadline - time.monotonic()))
    while time.monotonic() < deadline:                      # a reading another caller started is waited for too
        with _lock:
            if not _running.intersection(bases):
                break
        time.sleep(0.1)
    return snapshot()


def after_run(db_path, identity):
    """Background refresh of one account after its step ended; at most once per AUTO_GAP per account."""
    if not AUTO:
        return
    try:
        kind_, account, _, _ = split(identity)
    except ValueError:
        return
    if kind_ not in ('agy', 'codex') or (kind_ == 'agy' and account is None):
        return
    base = kind_ + (f'@{account}' if account else '')
    with _lock:
        last = (_cache.get(base) or {}).get('checked_at') or 0
        if base in _running or time.time() - last < AUTO_GAP:
            return

    def work():
        try:
            _refresh_one(db_path, base)
        except Exception:
            pass
    threading.Thread(target=work, name=f'quota-{base}', daemon=True).start()


def snapshot():
    with _lock:
        return {'accounts': json.loads(json.dumps(_cache)), 'running': sorted(_running)}
