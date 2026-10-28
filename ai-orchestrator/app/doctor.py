"""Installation check (`python -m app doctor`). Side-effect free: never calls a provider.

Each check prints one line: `OK   <name> — <detail>`, `WARN <name> — <detail>` or
`FAIL <name> — <detail and fix>`. The return code is 1 when at least one check failed, else 0.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HEALTH_URL = 'http://127.0.0.1:8765/health'


def _node_version(node: str) -> tuple[tuple[int, int], str] | None:
    try:
        proc = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return None
        raw = (proc.stdout or '').strip().splitlines()[0].strip().lstrip('vV')
        parts = raw.split('.')
        return (int(parts[0]), int(parts[1])), raw
    except Exception:
        return None


def _kind(name):
    from app.provider_policy import kind
    try:
        return kind(name)
    except Exception:   # an unknown name keeps its own text as the type
        return str(name)


def run(root: Path, out=print) -> int:
    root = Path(root)
    failed = False

    def line(level, name, detail):
        nonlocal failed
        failed = failed or level == 'FAIL'
        out(f'{level:<4} {name} — {detail}')

    # a) python
    v = sys.version_info
    if v >= (3, 12):
        line('OK', 'python', f'{v.major}.{v.minor}.{v.micro}')
    else:
        line('FAIL', 'python', f'{v.major}.{v.minor}.{v.micro} is too old; install Python 3.12 or newer')

    # b) providers.yaml
    policy = None
    path = root / '.system' / 'providers.yaml'
    if not path.exists():
        line('FAIL', 'providers.yaml', f'{path} is missing; copy providers.example.yaml or a preset '
                                       'to .system/providers.yaml')
    else:
        try:
            from app.provider_policy import load_config, effective
            policy = effective(load_config(path))
        except Exception as exc:
            line('FAIL', 'providers.yaml', f'invalid: {exc}; fix .system/providers.yaml '
                                           '(see providers.example.yaml)')
        else:
            roles = policy.get('roles', {})
            chains = '; '.join(
                f"{role}: {' → '.join(str(i.get('provider')) for i in roles.get(role, []))}"
                for role in ('author', 'reviewer'))
            line('OK', 'providers.yaml', chains)

    # c) provider CLIs
    try:
        from app import session_api
        installed = session_api.installed_providers()
    except Exception as exc:
        line('FAIL', 'provider CLIs', f'cannot resolve provider executables: {exc}')
    else:
        installed_kinds = {}
        for name, ok in installed.items():
            k = _kind(name)
            installed_kinds[k] = installed_kinds.get(k, False) or bool(ok)
        policy_kinds = []
        if policy is not None:
            for items in policy.get('roles', {}).values():
                for item in items:
                    k = _kind(item.get('provider'))
                    if k not in policy_kinds:
                        policy_kinds.append(k)
        for k in policy_kinds:
            if installed_kinds.get(k):
                line('OK', f'{k} CLI', 'installed')
            else:
                line('FAIL', f'{k} CLI', f'not found on PATH; install the {k} CLI or remove it '
                                         'from .system/providers.yaml')
        extra = sorted(k for k, ok in installed_kinds.items() if ok and k not in policy_kinds)
        if extra:
            line('OK', 'other CLIs', f"installed but not in the policy: {', '.join(extra)}")

    # d) panel
    if (root / 'app' / 'static' / 'index.html').exists():
        line('OK', 'panel', 'app/static/index.html is built')
    else:
        line('FAIL', 'panel', 'app/static/index.html is missing; cd ui && npm install && npm run build && cd ..')

    # e) node
    node = shutil.which('node')
    if node:
        ver = _node_version(node)
        if ver and ver[0] < (22, 6):
            line('WARN', 'node', f'node v{ver[1]} found; 22.6+ is needed to build the panel or run UI tests')
        else:
            line('OK', 'node', f'{node} (v{ver[1]})' if ver else node)
    else:
        line('WARN', 'node', 'not found; needed only to build the panel')

    # f) server
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            body = resp.read()
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        info = data.get('version') if isinstance(data.get('version'), dict) else data   # /health nests them
        commit = info.get('commit', '?')
        schema = info.get('schema', '?')
        line('OK', 'server', f'running at {HEALTH_URL} (commit {commit}, schema {schema})')
    except Exception:
        line('WARN', 'server', 'not running: .venv/bin/python -m app serve')

    # g) sign-in note
    out('Sign-in is checked in the panel (Settings → Accounts → Check session status); '
        'doctor does not call providers.')
    return 1 if failed else 0
