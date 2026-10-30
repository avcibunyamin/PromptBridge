"""TB-10 (K-023): the provider → account → model → effort catalog behind the team picker.

Live where the tool can list its models (`agy models`, per account home, no model call), a curated fallback
where it cannot (Claude has no list command: aliases are offered as `unverified` and checked at run time
against the model the CLI reports, TB-10 Rev 3). Refreshed only on an explicit request; served from memory.
The catalog only offers options: `providers.yaml` and its `allowed` ceiling stay the authority.
"""
import json
import os
import select
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from app.provider_policy import CAPABILITIES, MODEL_RE, split

CLAUDE_FALLBACKS = (
    # Claude Code has no quota-free model list command. Offer explicit ids first; aliases remain available
    # but are labeled as aliases in the panel and verified against the running model after each turn.
    ('claude-opus-5-5', 'Claude Opus 5.5'),
    ('claude-sonnet-5-5', 'Claude Sonnet 5.5'),
    ('claude-haiku-5-5', 'Claude Haiku 5.5'),
    ('claude-haiku-4-5-20251001', 'Claude Haiku 4.5'),
    ('claude-fable-5-1', 'Claude Fable 5.1'),
    ('opus', 'Opus (latest alias)'),
    ('sonnet', 'Sonnet (latest alias)'),
    ('haiku', 'Haiku (latest alias)'),
    ('fable', 'Fable (latest alias)'),
)
_lock = threading.Lock()
_cache = {'entries': None, 'errors': {}, 'notes': {}, 'checked_at': None}
# TB-14: Ollama's API on loopback only (a test may point it at another 127.0.0.1 port).
OLLAMA_TIMEOUT, MIN_CONTEXT = 3.0, 16384


def identity(provider, account=None, model=None, effort=None):
    return provider + (f'@{account}' if account else '') + (f'/{model}' if model else '') + (f'~{effort}' if effort else '')


def _claude():
    return [{'identity': identity('claude', model=m), 'provider': 'claude', 'account': None, 'model': m,
             'display_name': label, 'efforts': list(CAPABILITIES['claude']['efforts']), 'status': 'unverified',
             'source': 'fallback'}
            for m, label in CLAUDE_FALLBACKS]


def _accounts(config, provider):
    """Named accounts of `provider` in the default team or the `allowed` ceiling."""
    names = [i['provider'] if isinstance(i, dict) else i for chain in (config.get('roles') or {}).values() for i in chain]
    names += [p for chain in (config.get('allowed') or {}).values() for p in chain]
    found = []
    for name in names:
        try:
            kind_, account, _, _ = split(name if isinstance(name, str) else '')
        except ValueError:
            continue
        if kind_ == provider and account is not None and account not in found:   # a bare `agy` has no home to list
            found.append(account)
    return found


def _bases(config, provider):
    """Configured identities of a provider at account granularity; bare codex uses the default CODEX_HOME."""
    names = [i['provider'] if isinstance(i, dict) else i for chain in (config.get('roles') or {}).values() for i in chain]
    names += [p for chain in (config.get('allowed') or {}).values() for p in chain]
    found = []
    for name in names:
        try:
            kind_, account, _, _ = split(name if isinstance(name, str) else '')
        except ValueError:
            continue
        if kind_ == provider:
            base = identity(provider, account)
            if base not in found:
                found.append(base)
    return found


def _agy(db_path, account):
    """(entries, error). `agy models` in the account's own HOME (keychain unlocked first), bounded, own group."""
    from app.adapters.agy import agy_binary, agy_home, prepare_home
    from app import provider_auth
    try:
        env = prepare_home(agy_home(db_path, account))
        proc = subprocess.Popen([agy_binary(), 'models'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env, cwd='/')
    except Exception as exc:   # not installed, home unusable: the catalog says so instead of failing
        return [], 'unavailable'
    buf, exited = b'', False
    try:
        buf, exited = provider_auth._read(proc, provider_auth.SLOW['agy'])
    except Exception:
        pass                                     # unknown result; the cleanup below still runs
    finally:
        code, gone = provider_auth._cleanup(proc)
    text = buf.decode('utf-8', 'replace')
    if not gone:
        return [], 'cleanup_unverified'
    if not exited or code is None:
        return [], 'timeout'
    if 'please sign in' in text.lower():
        return [], 'unauthenticated'
    if code != 0:
        return [], 'unavailable'
    slugs = [line.split('\t', 1)[0].strip() for line in text.splitlines() if '\t' in line]
    by_slug = {}
    for line in text.splitlines():
        if '\t' not in line:
            continue
        slug, label = [p.strip() for p in line.split('\t', 1)]
        if MODEL_RE.fullmatch(slug):
            by_slug[slug] = label
    return [{'identity': identity('agy', account, s), 'provider': 'agy', 'account': account, 'model': s,
             'display_name': by_slug.get(s) or s, 'efforts': [], 'status': 'available', 'source': 'live'}
            for s in slugs if MODEL_RE.fullmatch(s)], None


def _rpc_line(proc, deadline):
    """Read one app-server JSON line before `deadline`; notifications are returned to the caller."""
    buf = getattr(proc, '_conductor_buf', bytearray())
    fd = proc.stdout.fileno()
    while time.monotonic() < deadline:
        if b'\n' in buf:
            line, rest = bytes(buf).split(b'\n', 1)
            proc._conductor_buf = bytearray(rest)
            return json.loads(line)
        ready = select.select([fd], [], [], min(0.05, max(0, deadline - time.monotonic())))[0]
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            raise ValueError('codex app-server exited')
        buf += chunk
    raise TimeoutError('codex model/list timed out')


def _codex_request(proc, counter, method, params, deadline):
    proc.stdin.write((json.dumps({'id': counter, 'method': method, 'params': params}) + '\n').encode())
    proc.stdin.flush()
    while True:
        msg = _rpc_line(proc, deadline)
        if msg.get('id') != counter or 'method' in msg:
            continue
        if 'error' in msg:
            raise ValueError(str(msg['error'].get('message') or msg['error'])[:300])
        return msg.get('result') or {}


def _codex(db_path, base):
    """(entries, error). Codex app-server model/list in that account's CODEX_HOME; no model turn."""
    from app.adapters.codex import codex_binary, codex_home, codex_env, CODEX_ARGS
    from app import provider_auth
    try:
        _, account, _, _ = split(base)
        proc = subprocess.Popen([codex_binary(), *CODEX_ARGS], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                env=codex_env(codex_home(db_path, account)), cwd='/')
    except Exception:
        return [], 'unavailable'
    models, exited, err, timed_out = [], False, '', False
    try:
        deadline = time.monotonic() + provider_auth.SLOW.get('codex', provider_auth.TIMEOUT)
        n = 1
        _codex_request(proc, n, 'initialize', {'clientInfo': {'name': 'conductor', 'title': 'Conductor',
                                                              'version': '0.2.0'}}, deadline)
        proc.stdin.write((json.dumps({'method': 'initialized'}) + '\n').encode()); proc.stdin.flush()
        cursor = None
        while True:
            n += 1
            params = {'cursor': cursor} if cursor else {}
            result = _codex_request(proc, n, 'model/list', params, deadline)
            data = result.get('data') or []
            if not isinstance(data, list):
                raise ValueError('invalid codex model/list response')
            models.extend(data)
            cursor = result.get('nextCursor')
            if not cursor:
                break
        exited = True
    except TimeoutError:
        timed_out = True
    except Exception as exc:
        err = str(exc)
        models = None
    finally:
        code, gone = provider_auth._cleanup(proc)
        if proc.stdin:
            proc.stdin.close()
    if not gone:
        return [], 'cleanup_unverified'
    if not exited:
        return [], 'timeout' if timed_out else 'unauthenticated' if 'not logged in' in err.lower() else 'unavailable'
    if code not in (0, -9, None):
        return [], 'unavailable'
    entries = []
    _, account, _, _ = split(base)
    for m in models or []:
        if not isinstance(m, dict):
            continue
        slug = m.get('id') or m.get('slug') or m.get('name')
        raw_efforts = m.get('supportedReasoningEfforts') or []
        efforts = []
        for effort in raw_efforts:
            value = effort.get('reasoningEffort') if isinstance(effort, dict) else effort
            if value in CAPABILITIES['codex']['efforts']:
                efforts.append(value)
        if slug:
            entries.append({'identity': identity('codex', account, slug), 'provider': 'codex', 'account': account,
                            'model': slug, 'display_name': m.get('displayName') or m.get('name') or m.get('title') or slug,
                            'efforts': efforts, 'default_effort': m.get('defaultReasoningEffort'),
                            'status': 'available', 'source': 'live'})
    return entries, None


def _ollama_url():
    url = os.environ.get('CONDUCTOR_OLLAMA_URL', 'http://127.0.0.1:11434')
    if urllib.parse.urlsplit(url).hostname != '127.0.0.1':
        raise ValueError('Ollama is only reached on 127.0.0.1')
    return url.rstrip('/')


def _ollama_json(path, body=None):
    request = urllib.request.Request(_ollama_url() + path, method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
        return json.loads(response.read(1 << 20))


def _ollama():
    """(entries, error, note). Ollama's own API (no model call): models, tool support per model and, for a
    loaded model, its context (a small context breaks Codex's tool calls, TB-14 feasibility)."""
    try:
        tags = _ollama_json('/api/tags')
    except TimeoutError:
        return [], 'timeout', None
    except Exception:
        return [], 'unavailable', None
    entries = []
    for m in tags.get('models') or []:
        name = m.get('name') or m.get('model')
        if not isinstance(name, str) or not MODEL_RE.fullmatch(name):
            continue                                 # e.g. namespaced `hf.co/...` names: not an identity
        try:
            tools = 'tools' in (_ollama_json('/api/show', {'model': name}).get('capabilities') or [])
        except Exception:
            tools = False
        entries.append({'identity': identity('ollama', None, name), 'provider': 'ollama', 'account': None,
                        'model': name, 'efforts': [], 'status': 'available' if tools else 'unsupported',
                        'source': 'live'})
    note = None
    try:
        for m in _ollama_json('/api/ps').get('models') or []:
            ctx = m.get('context_length')
            if isinstance(ctx, int) and ctx < MIN_CONTEXT:
                note = f'small_context:{ctx}'
    except Exception:
        pass
    return entries, None, note


def _uses(config, provider):
    names = [i['provider'] if isinstance(i, dict) else i for chain in (config.get('roles') or {}).values() for i in chain]
    names += [p for chain in (config.get('allowed') or {}).values() for p in chain]
    return any(isinstance(n, str) and n.split('/')[0] == provider for n in names)


def _store_ollama(found, error, note):
    """Replace the Ollama part of the cache (caller holds _lock)."""
    entries = [e for e in (_cache['entries'] if _cache['entries'] is not None else _claude()) if e['provider'] != 'ollama']
    errors, notes = dict(_cache['errors']), dict(_cache['notes'])
    errors.pop('ollama', None); notes.pop('ollama', None)
    if error:
        errors['ollama'] = error
    if note:
        notes['ollama'] = note
    _cache.update(entries=entries + found, errors=errors, notes=notes, checked_at=time.time())


def refresh(db_path, config):
    """Rebuild the catalog (runs live list commands; never starts a model turn). One refresh at a time."""
    with _lock:
        entries, errors = _claude(), {}
        for base in _bases(config, 'codex'):
            found, error = _codex(db_path, base)
            entries += found
            if error:
                errors[base] = error
        for account in _accounts(config, 'agy'):
            found, error = _agy(db_path, account)
            entries += found
            if error:
                errors[identity('agy', account)] = error
        _cache.update(entries=entries, errors=errors, notes={}, checked_at=time.time())
        if _uses(config, 'ollama'):
            _store_ollama(*_ollama())
        return snapshot()


def refresh_base(db_path, provider, account):
    """K-060: re-list one agy/codex account (after a sign-in) and replace its part of the catalog.
    (signed_in, error): True when its models came back, False when the tool says it is signed out, else None."""
    base = identity(provider, account)
    found, error = _agy(db_path, account) if provider == 'agy' else _codex(db_path, base)
    with _lock:
        entries = _replace_live_base(_cache['entries'] if _cache['entries'] is not None else _claude(), provider, account, found)
        errors = dict(_cache['errors'])
        errors.pop(base, None)
        if error:
            errors[base] = error
        _cache.update(entries=entries, errors=errors, checked_at=time.time())
    return (True if found and not error else False if error == 'unauthenticated' else None), error


def signed_in(provider, account):
    """K-064: what the last catalog says about one agy/codex sign-in, without a subprocess:
    True when its models were listed, False when the tool said signed out, None when not known yet."""
    if _cache['entries'] is None:
        return None
    error = _cache['errors'].get(identity(provider, account))
    if error:
        return False if error == 'unauthenticated' else None
    return True if _live_listed(snapshot(), provider, account) else None


def snapshot():
    """The last catalog; before any refresh only the Claude fallback (no subprocess)."""
    entries = _cache['entries'] if _cache['entries'] is not None else _claude()
    return {'entries': entries, 'errors': dict(_cache['errors']), 'notes': dict(_cache['notes']), 'checked_at': _cache['checked_at'],
            'efforts': {p: list(c.get('efforts', ())) for p, c in CAPABILITIES.items()}}


def _entry_for(identity_):
    try:
        provider, account, model, _ = split(identity_)
    except ValueError:
        return None
    base = identity(provider, account, model)
    snap = snapshot()
    for entry in snap['entries']:
        if entry['identity'] == base:
            return entry
    return None


def _live_listed(snap, provider, account):
    return any(e['provider'] == provider and e['account'] == account and e['source'] == 'live'
               for e in snap['entries'])


def _replace_live_base(entries, provider, account, found):
    return [e for e in entries if not (e['provider'] == provider and e['account'] == account and e['source'] == 'live')] + found


def status(identity_):
    """available | unverified | model_unavailable | unknown — for a chain identity (its effort is ignored)."""
    try:
        provider, account, model, _ = split(identity_)
    except ValueError:
        return 'unknown'
    entry = _entry_for(identity_)
    if entry:
        return entry['status']
    snap = snapshot()
    listed = _cache['entries'] is not None and identity(provider, account) not in snap['errors'] and _live_listed(snap, provider, account)
    return 'model_unavailable' if listed and model else 'unknown'


def problem(identity_):
    """Dynamic catalog validation for model-specific constraints that provider_policy cannot know."""
    try:
        provider, account, model, effort = split(identity_)
    except ValueError:
        return None
    if provider == 'ollama' and model is not None:
        entry = _entry_for(identity_)
        if entry is not None:
            return 'tools_unsupported' if entry['status'] == 'unsupported' else None
        snap = snapshot()
        listed = 'ollama' not in snap['errors'] and _live_listed(snap, 'ollama', None)
        return 'model_unavailable' if listed else None
    if provider != 'codex' or model is None:
        return None
    entry = _entry_for(identity_)
    if entry is None:
        snap = snapshot()
        listed = _cache['entries'] is not None and identity(provider, account) not in snap['errors'] and _live_listed(snap, provider, account)
        return 'model_unavailable' if listed else None
    if effort is not None and effort not in entry.get('efforts', []):
        return 'effort_unsupported'
    return None


def validate_identity(db_path, identity_):
    """Final Codex gate before a model turn: refresh one account when the cache is unknown.

    Preview and message POST use the cache only. The runner/adapter calls this just before
    opening a turn, so restart-empty catalogs do not block valid teams and unsupported
    efforts are still rejected before the provider model runs.
    """
    try:
        provider, account, model, _ = split(identity_)
    except ValueError:
        return None
    if provider == 'ollama' and model is not None:
        with _lock:                                    # TB-14: cheap and local, so always fresh
            _store_ollama(*_ollama())
        error = snapshot()['errors'].get('ollama')
        if error:
            return 'ollama_' + error                   # unavailable | timeout: Ollama is not running
        return problem(identity_) or (None if _entry_for(identity_) is not None else 'model_unavailable')
    if provider != 'codex' or model is None:
        return None
    reason = problem(identity_)
    if reason:
        return reason
    if _entry_for(identity_) is not None:
        return None
    base = identity(provider, account)
    snap = snapshot()
    known = _cache['entries'] is not None and _live_listed(snap, provider, account)
    if not known:
        with _lock:
            reason = problem(identity_)
            if reason or _entry_for(identity_) is not None:
                return reason
            found, error = _codex(db_path, base)
            entries = list(_cache['entries'] if _cache['entries'] is not None else _claude())
            entries = _replace_live_base(entries, provider, account, found)
            errors = dict(_cache['errors'])
            errors.pop(base, None)
            if error:
                errors[base] = error
            _cache.update(entries=entries, errors=errors, checked_at=time.time())
    reason = problem(identity_)
    if reason:
        return reason
    snap = snapshot()
    if base in snap['errors']:
        return 'unauthenticated' if snap['errors'][base] == 'unauthenticated' else 'catalog_unknown'
    return None if _entry_for(identity_) is not None else 'model_unavailable'


def team_problems(team):
    """Catalog-backed problems for a selected team; no subprocess, only the last refresh."""
    return [{'role': role, 'provider': provider, 'reason': reason}
            for role, chain in team.items() for provider in chain
            for reason in [problem(provider)] if reason]
