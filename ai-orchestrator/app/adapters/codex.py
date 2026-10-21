"""Codex app-server JSON-RPC adapter, one owned server per turn."""
import asyncio
import glob
import json
import os
import stat
import subprocess
import tomllib
from app.adapters.base import BaseAdapter, AdapterResult
from app.provider_errors import codex_error, TransientEOF
from app.adapters.process import executable, drain_stderr, stop_process, spawn_owned, release_gate, remember_group


# ChatGPT app bundles; 26.9x moved the CLI into codex-cli/bin (found by the F3-06 suite run, 2026-09-30).
CODEX_CANDIDATES = ('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex',
                    '/Applications/ChatGPT.app/Contents/Resources/codex')


def codex_binary():
    return executable('CONDUCTOR_CODEX_BIN', 'codex', CODEX_CANDIDATES)


def codex_home(db_path, account=None):
    """K-014: Conductor's own CODEX_HOME next to its DB (`.system/codex-home`), never the user's ~/.codex.
    TB-06: a named account gets its own `codex-home-<account>`; the env override applies to the default only."""
    if account:
        return os.path.abspath(os.path.join(os.path.dirname(db_path), f'codex-home-{account}'))
    return os.path.abspath(os.environ.get('CONDUCTOR_CODEX_HOME') or os.path.join(os.path.dirname(db_path), 'codex-home'))


def ollama_home(db_path):
    """TB-14: the CODEX_HOME for local Ollama models (no sign-in; never a Codex account's home: an account
    named `ollama` would be `codex-home-ollama`)."""
    return os.path.abspath(os.path.join(os.path.dirname(db_path), 'ollama-codex-home'))


def codex_env(home, workspace=None):
    """The environment for every Codex process Conductor starts: its own CODEX_HOME (an inherited one is
    replaced). Created 0700 if missing; a symlink or non-directory is refused. With a workspace, the
    workspace is pinned untrusted first (K-015)."""
    try:
        os.mkdir(home, 0o700)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(home).st_mode):
        raise ValueError(f'Codex home is not a real directory: {home}')
    if workspace is not None:
        distrust(home, workspace)
    return {**os.environ, 'CODEX_HOME': home}


def _project_roots(workspace):
    """The keys Codex trusts: the workspace and, inside git, its top level (both as given and resolved)."""
    roots = [workspace, os.path.realpath(workspace)]
    try:
        top = subprocess.run(['git', '-C', workspace, 'rev-parse', '--show-toplevel'], capture_output=True,
                             text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            roots += [top.stdout.strip(), os.path.realpath(top.stdout.strip())]
    except (OSError, subprocess.SubprocessError):
        pass
    return list(dict.fromkeys(os.path.abspath(r) for r in roots))


def distrust(home, workspace):
    """K-015: Codex's thread/start writes `trust_level = "trusted"` for its project root into CODEX_HOME,
    which loads the repo's .codex/ layer (MCP servers, hooks). Pin every root `untrusted` before Codex
    starts (Codex keeps an existing entry) and verify by reading the file back; otherwise refuse.
    Only `[projects."<root>"]` tables are touched, only in Conductor's own home."""
    # ponytail: no file lock against a concurrent Codex rewrite of config.toml; the read-back check
    # below catches a lost entry before this process starts, not one lost by another process later.
    path = os.path.join(home, 'config.toml')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        text = ''
    roots = _project_roots(workspace)
    if all(tomllib.loads(text).get('projects', {}).get(r, {}).get('trust_level') == 'untrusted' for r in roots):
        return
    headers = {f'[projects.{json.dumps(r)}]' for r in roots}
    kept, skipping = [], False
    for line in text.splitlines():
        if line.strip().startswith('['):
            skipping = line.strip() in headers          # drop the old table for a root; it is re-added below
        if not skipping:
            kept.append(line)
    blocks = ''.join(f'\n[projects.{json.dumps(r)}]\ntrust_level = "untrusted"\n' for r in roots)
    new = ('\n'.join(kept).rstrip('\n') + '\n' + blocks).lstrip('\n')
    projects = tomllib.loads(new).get('projects', {})
    if any(projects.get(r, {}).get('trust_level') != 'untrusted' for r in roots):
        raise ValueError('could not pin the workspace untrusted in the Codex home')
    tmp = path + '.conductor.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new)
    os.replace(tmp, path)


# K-014 (C): Codex's own AGENTS.md loader is off; Conductor injects the frozen snapshot instead.
# Codex's default "available but not installed" plugin catalog is kept out of the context too:
# `features.recommended_plugins=false` alone did not remove it (TB-05 DoD 3, codex-cli 0.158), `features.plugins=false` did.
CODEX_ARGS = ('app-server', '-c', 'project_doc_max_bytes=0', '-c', 'features.plugins=false', '--stdio')


class CodexAdapter(BaseAdapter):
    def __init__(self, home, model=None, effort=None, *, db_path=None, identity=None, extra_args=(), local=False):
        self.home, self.model, self.effort = home, model, effort
        # TB-14: a local model (`-c model_provider="ollama"`) has no quota or sign-in; its errors never hand off.
        self.extra_args, self.local = tuple(extra_args), local
        self.db_path, self.identity = db_path, identity

    def _error_code(self, error):
        code = codex_error(error)
        # TB-14: Ollama has no quota or account; a "quota"/"auth" text from it must not hand off or wait.
        return 'UNKNOWN' if self.local and code in ('QUOTA_EXCEEDED', 'AUTH', 'CONTEXT_EXHAUSTED') else code

    def _turn_context_from_session_log(self, thread_id):
        """Read Codex's rollout record for the thread.

        TB-15a observed the running model/effort in CODEX_HOME/sessions JSONL files, not in
        live app-server notifications. Keep the read bounded and use the newest matching context.
        """
        sessions = os.path.join(self.home, 'sessions')
        paths = glob.glob(os.path.join(sessions, '**', f'*{thread_id}*.jsonl'), recursive=True)
        paths.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
        for path in paths[:8]:
            try:
                if os.path.getsize(path) > 4 * 1024 * 1024:
                    continue
                found = None
                with open(path, encoding='utf-8') as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if item.get('type') != 'turn_context':
                            continue
                        payload = item.get('payload') or {}
                        if isinstance(payload, dict):
                            found = payload
                if found is not None:
                    return found
            except OSError:
                continue
        return None

    async def run(self, prompt, workspace_path, provider_session_id=None, *, profile):
        if profile not in ('WRITE', 'READ_ONLY'):
            raise ValueError('invalid profile')
        if self.db_path and self.identity:
            from app import catalog
            reason = catalog.validate_identity(self.db_path, self.identity)
            if reason:
                error_code = 'AUTH' if reason == 'unauthenticated' else 'UNKNOWN'
                yield {'type': 'failed', 'result': AdapterResult(
                    '', None, None, reason, True, reason, None, error_code,
                    {'provider': self.identity, 'reason': reason})}
                return
        binary = codex_binary()
        process, owner, gate = await spawn_owned([binary, *CODEX_ARGS[:-1], *self.extra_args, CODEX_ARGS[-1]], workspace_path, codex_env(self.home, workspace_path))
        stderr = asyncio.create_task(drain_stderr(process.stderr))
        counter = 0
        pending = []
        blocked = False

        async def send(message):
            process.stdin.write((json.dumps(message) + '\n').encode())
            await process.stdin.drain()

        async def receive():
            nonlocal blocked
            line = await process.stdout.readline()
            if not line:
                raise TransientEOF('Codex EOF before terminal result')
            message = json.loads(line)
            if 'id' in message and 'method' in message:
                # Never grant permissions implicitly. Surface unsupported requests.
                blocked = True
                await send({'id': message['id'], 'error': {
                    'code': -32601, 'message': 'Human approval required in Conductor'}})
            return message

        async def request(method, params):
            nonlocal counter
            counter += 1
            rpc_id = counter
            await send({'id': rpc_id, 'method': method, 'params': params})
            while True:
                message = await receive()
                if message.get('id') == rpc_id and 'method' not in message:
                    if 'error' in message:
                        raise ValueError(f'Codex {method}: {message["error"].get("message")}')
                    return message['result']
                if 'method' in message and 'id' not in message:
                    pending.append(message)

        try:
            yield {'type': 'process_started', 'ownership': owner}
            release_gate(gate)
            gate = None
            await request('initialize', {'clientInfo': {
                'name': 'conductor', 'title': 'Conductor', 'version': '0.2.0'}})
            await send({'method': 'initialized'})
            params = {'cwd': workspace_path, 'approvalPolicy': 'never',
                      'sandbox': 'workspace-write' if profile == 'WRITE' else 'read-only',
                      'config': {'sandbox_workspace_write.network_access': False}}
            if provider_session_id:
                params['threadId'] = provider_session_id
            thread = await request('thread/resume' if provider_session_id else 'thread/start', params)
            thread_id = thread['thread']['id']
            yield {'type': 'started', 'provider_session_id': thread_id}
            turn_params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}],
                           'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [workspace_path],
                                             'networkAccess': False, 'excludeSlashTmp': True,
                                             'excludeTmpdirEnvVar': True} if profile == 'WRITE' else
                                            {'type': 'readOnly', 'networkAccess': False}}
            if self.model:
                turn_params['model'] = self.model
            if self.effort:
                turn_params['effort'] = self.effort
            turn = await request('turn/start', turn_params)
            turn_id = turn['turn']['id']
            messages = {}
            usage = None
            terminal = None
            last_error = None
            turn_context = None
            while terminal is None:
                message = pending.pop(0) if pending else await receive()
                method, params = message.get('method'), message.get('params', {})
                if params.get('threadId') not in (None, thread_id):
                    continue
                if params.get('turnId') not in (None, turn_id):
                    continue
                context = params.get('turn_context') or params.get('turnContext')
                if isinstance(context, dict):
                    turn_context = context
                if method == 'item/agentMessage/delta':
                    key = params['itemId']
                    messages[key] = messages.get(key, '') + params['delta']
                    yield {'type': 'output_delta', 'text': params['delta']}
                elif method == 'item/completed':
                    item = params.get('item', {})
                    if item.get('type') == 'commandExecution' and isinstance(item.get('exitCode'), int):
                        # TB-15c: a command without output arrives with aggregatedOutput null
                        yield {'type':'command_result','command':item.get('command') or 'unknown',
                               'exit_code':item['exitCode'],'output':(item.get('aggregatedOutput') or '')[:32768]}
                    if item.get('type') == 'agentMessage':   # a null text keeps what the deltas built
                        messages[item['id']] = item.get('text') or messages.get(item['id'], '')
                elif method == 'error':
                    last_error = params.get('error')
                elif method == 'thread/tokenUsage/updated':
                    usage = params.get('tokenUsage')
                elif method == 'turn/completed' and params.get('turn', {}).get('id') == turn_id:
                    terminal = params['turn']
                    context = terminal.get('turn_context') or terminal.get('turnContext')
                    if isinstance(context, dict):
                        turn_context = context
            if blocked:
                yield {'type': 'approval_required', 'details': 'Codex requested unsupported approval/input'}
            status = terminal['status']
            text = '\n\n'.join(messages.values())
            failed = status != 'completed' or blocked
            reason = 'permission_denied' if blocked else status
            if turn_context is None and (self.model is not None or self.effort is not None):
                for _ in range(3):
                    turn_context = self._turn_context_from_session_log(thread_id)
                    if turn_context is not None:
                        break
                    await asyncio.sleep(0.1)
            turn_context = turn_context or {}
            got_model = turn_context.get('model')
            got_effort = turn_context.get('effort') or turn_context.get('reasoning_effort') or turn_context.get('reasoningEffort')
            missing_context = (not turn_context) and (self.model is not None or self.effort is not None)
            mismatch = (missing_context or
                        (self.model is not None and got_model != self.model) or
                        (self.effort is not None and got_effort != self.effort))
            if mismatch:
                failed, reason = True, 'model_context_missing' if missing_context else 'model_mismatch'
            # app-server is long-lived. A completed turn, not server EOF, ends work.
            # Close transport gracefully and verify its exit before yielding result.
            remember_group(owner)
            yield {'type':'process_group_snapshot', 'ownership':owner}
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 5)
            code = process.returncode
            answer = AdapterResult(text, thread_id, usage, status,
                                   failed or code != 0, reason, code,
                                   'PERMISSION' if blocked else 'UNKNOWN' if mismatch else
                                   self._error_code(terminal.get('error') or last_error) if failed else None,
                                   {'wanted': {'model': self.model, 'effort': self.effort},
                                    'got': {'model': got_model, 'effort': got_effort}} if mismatch else
                                   terminal.get('error') or last_error)
        finally:
            if gate is not None:
                os.close(gate)
            try:
                await stop_process(process, owner)
            finally:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
        yield {'type': 'process_stopped', 'ownership': owner}
        yield {'type': 'completed' if answer.successful else 'failed', 'result': answer}
