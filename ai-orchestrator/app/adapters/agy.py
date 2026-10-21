"""Antigravity CLI (`agy`) adapter, one headless process per turn (TB-08: K-017, K-018, K-020).

Every account runs with its own HOME (`.system/agy-home[-<account>]`) holding its own keychain, so two
Google accounts never share a login and the user's real ~/.gemini (shared with Antigravity desktop) is
never read or migrated. Commands are never allowed: a denied command ends the turn (COMMAND_DENIED),
which the runner may follow up once in the same conversation (K-020).
"""
import asyncio
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
from app.adapters.base import BaseAdapter, AdapterResult
from app.adapters.process import executable, stop_process, spawn_owned, release_gate
from app.provider_errors import agy_error

AGY_CANDIDATES = (str(Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/bin/agy'),)
CONFIG_DIRS = ('.agents', '.agent', '_agents', '_agent')
CONFIG_FILES = ('mcp_config.json', 'hooks.json')
STDERR_LIMIT = 65536
TOOLS = {'READ_ONLY': 'read with view_file. Do not modify any file.',
         'WRITE': 'read with view_file and change files with the file edit tools.'}
# K-021: subagents are allowed (agy 1.2.16 keeps list/search tools there) and obey the same denied permissions.
RULES = ('CONDUCTOR RULES: this session has no shell or command tool, also not in subagents. Any command request '
         '(run_command, ls, grep, tests, git) is denied and ends your turn unfinished. To list or search files use '
         'the non-command tools you or your subagents have; {tools}\n\n')
FOLLOWUP_PROMPT = ('Your previous command request was denied: there is no command tool in this session and there will '
                   'not be one, also not in subagents. Continue the same task from where you stopped without any '
                   'command; {tools} Then give your complete answer.')


def agy_binary():
    return executable('CONDUCTOR_AGY_BIN', 'agy', AGY_CANDIDATES)


def security_binary():
    return executable('CONDUCTOR_SECURITY_BIN', 'security', ('/usr/bin/security',))


def agy_home(db_path, account=None):
    return os.path.abspath(os.path.join(os.path.dirname(db_path), 'agy-home' + (f'-{account}' if account else '')))


def followup_prompt(profile):
    return FOLLOWUP_PROMPT.format(tools=TOOLS[profile])


def workspace_config(workspace):
    """K-018/1: the first agy MCP/hook definition in the workspace (relative path), or None. Names are
    matched case-insensitively (macOS volumes usually are)."""
    try:
        entries = os.listdir(workspace)
    except OSError:
        return None
    for entry in sorted(entries):
        folder = os.path.join(workspace, entry)
        if entry.lower() not in CONFIG_DIRS or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.lower() in CONFIG_FILES:
                return os.path.relpath(path, workspace)
            if name.lower() == 'plugins' and os.path.isdir(path):
                for plugin in sorted(os.listdir(path)):
                    plugin_dir = os.path.join(path, plugin)
                    if os.path.isdir(plugin_dir) and any(n.lower() == 'mcp_config.json' for n in os.listdir(plugin_dir)):
                        return os.path.relpath(plugin_dir, workspace)
    return None


def _security(home, *args):
    return subprocess.run([security_binary(), *args], env={**os.environ, 'HOME': home}, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=10).returncode


def prepare_home(home):
    """The environment for every agy process: its own HOME with its own unlocked keychain and pinned
    empty permissions. Raises ValueError when any of it cannot be ensured (the process is not started)."""
    if os.path.realpath(home) == os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir):
        raise ValueError('agy home must not be the real home directory')
    try:
        os.mkdir(home, 0o700)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(home).st_mode):
        raise ValueError(f'agy home is not a real directory: {home}')
    # K-018/3: a keychain inside this HOME keeps macOS from asking for one (a dialog nobody answers).
    # Re-created if the home was wiped; the user's own keychain search list resolves `~` to the real home.
    keychain = os.path.join(home, 'Library', 'Keychains', 'login.keychain-db')
    if not os.path.exists(keychain):
        os.makedirs(os.path.dirname(keychain), 0o700, exist_ok=True)
        # Two first runs of one account may race: losing `create-keychain` is fine once the file exists.
        if _security(home, 'create-keychain', '-p', '', keychain) and not os.path.exists(keychain):
            raise ValueError('could not create the agy keychain')
        if _security(home, 'set-keychain-settings', keychain):
            raise ValueError('could not configure the agy keychain')
        os.chmod(keychain, 0o600)
    # Unlock first: a locked keychain makes macOS show a password dialog.
    if _security(home, 'unlock-keychain', '-p', '', keychain):
        raise ValueError('could not unlock the agy keychain')
    pin_permissions(home)
    return {**os.environ, 'HOME': home}


def pin_permissions(home):
    """No permission rule may allow anything (K-018/4): `permissions.allow` is pinned to [] and read back."""
    path = os.path.join(home, '.gemini', 'antigravity-cli', 'settings.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except ValueError as exc:
        raise ValueError('agy settings.json is not valid JSON') from exc
    if not isinstance(data, dict) or not isinstance(data.get('permissions', {}), dict):
        raise ValueError('agy settings.json has an unexpected shape')
    if data.get('permissions', {}).get('allow') != []:
        data.setdefault('permissions', {})['allow'] = []
        os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='settings.', suffix='.conductor.tmp', dir=os.path.dirname(path))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:   # unique name: concurrent runs of one account
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    with open(path, encoding='utf-8') as f:
        if json.load(f).get('permissions', {}).get('allow') != []:
            raise ValueError('could not pin agy permissions')


def _failed(reason, error_code=None, detail=None, sid=None):
    return AdapterResult('', sid, None, None, True, reason, None, error_code or 'UNKNOWN', detail)


async def _read_limited(stream, sink):
    while chunk := await stream.read(8192):
        if len(sink) < STDERR_LIMIT:
            sink.extend(chunk[:STDERR_LIMIT - len(sink)])


class AgyAdapter(BaseAdapter):
    def __init__(self, home, model):
        if not model:
            raise ValueError('agy needs a model (its default is Flash)')
        self.home, self.model = home, model

    async def run(self, prompt, workspace_path, provider_session_id=None, *, profile):
        if profile not in ('WRITE', 'READ_ONLY'):
            raise ValueError('invalid profile')
        found = workspace_config(workspace_path)
        if found:
            # K-018/1: agy would start this MCP server/hook with no trust check. No process, no quota.
            yield {'type': 'failed', 'result': _failed('agy_workspace_config', None, {'path': found})}
            return
        try:
            env = await asyncio.to_thread(prepare_home, self.home)   # `security` must not block the event loop
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            yield {'type': 'failed', 'result': _failed('agy_home_unusable', 'AUTH', {'detail': str(exc)[:300]})}
            return
        args = [agy_binary(), '-p', RULES.format(tools=TOOLS[profile]) + prompt, '--model', self.model,
                '--output-format', 'stream-json', '--print-timeout', '0']
        if profile == 'WRITE':
            args += ['--mode', 'accept-edits']
        if provider_session_id:
            args += ['--conversation', provider_session_id]
        process, owner, gate = await spawn_owned(args, workspace_path, env)
        errors = bytearray()
        stderr = asyncio.create_task(_read_limited(process.stderr, errors))
        result = conversation = model = None
        try:
            yield {'type': 'process_started', 'ownership': owner}
            release_gate(gate)
            gate = None
            process.stdin.close()
            async for line in process.stdout:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue                               # agy prints nothing else on stdout; ignore noise
                event = data.get('event')
                if event == 'init':
                    conversation = data.get('conversation_id')
                    model = (data.get('init') or {}).get('model')
                    # K-021: the tools agy declared to the model are recorded every turn (they changed in 1.2.16).
                    tools = (data.get('init') or {}).get('tools')
                    yield {'type': 'started', 'provider_session_id': conversation, 'model': model,
                           'tools': [str(t) for t in tools][:200] if isinstance(tools, list) else None}
                elif event == 'result':
                    if result is not None:
                        raise ValueError('duplicate agy result')
                    result = data.get('result') or {}
            code = await process.wait()
        finally:
            if gate is not None:
                os.close(gate)
            try:
                await stop_process(process, owner)
            finally:
                await asyncio.gather(stderr, return_exceptions=True)
        answer = self._classify(result, code, conversation, model, provider_session_id, errors.decode('utf-8', 'replace'))
        if answer.error_code in ('PERMISSION', 'COMMAND_DENIED'):
            yield {'type': 'approval_required', 'details': 'agy denied: ' + ', '.join(
                sorted({str(a.get('action')) for a in (result or {}).get('denied_actions') or []}))}
        yield {'type': 'process_stopped', 'ownership': owner}
        yield {'type': 'completed' if answer.successful else 'failed', 'result': answer}

    def _classify(self, result, code, conversation, model, wanted_conversation, stderr):
        sid = conversation or wanted_conversation
        if conversation and model != self.model:
            return _failed('model_mismatch', None, {'wanted': self.model, 'got': model}, sid)
        if wanted_conversation and conversation != wanted_conversation:
            # agy silently starts a new conversation for an unknown id; never accept that as a resume.
            return _failed('resume_mismatch', None, {'wanted': wanted_conversation, 'got': conversation}, sid)
        if result is None:
            error = agy_error(stderr)
            return AdapterResult('', sid, None, None, True, 'no_result', code, error,
                                 {'stderr': stderr[-2000:]} if error != 'UNKNOWN' else None)
        text = result.get('response') or ''
        denied = result.get('denied_actions') or []
        if denied:
            # K-021: a command-only denial ends the turn unfinished even when the model already wrote some
            # narration (TB-08 DoD 4 turn 4: a subagent tried a command), so it is followed up as well.
            command_only = {a.get('action') for a in denied} == {'command'}
            return AdapterResult(text, sid, result.get('usage'), None, True, 'permission_denied', code,
                                 'COMMAND_DENIED' if command_only else 'PERMISSION', {'denied_actions': denied})
        if result.get('status') != 'SUCCESS':
            error = agy_error(f"{result.get('error') or ''}\n{stderr}")
            return AdapterResult(text, sid, result.get('usage'), None, True, str(result.get('status')).lower(), code,
                                 error, {'status': result.get('status'), 'error': str(result.get('error'))[:2000]})
        return AdapterResult(text, sid, result.get('usage'), 'stop', code != 0, 'completed', code)
