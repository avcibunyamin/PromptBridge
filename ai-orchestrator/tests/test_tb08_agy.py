"""TB-08 (K-017, K-018, K-020): the Antigravity CLI adapter.

Fake `agy` and `security` executables only (no keychain, no provider quota); the real agy run is the
DoD 4 check at go-live.
"""
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import provider_auth, runner_store as repo, store
from app.adapters.agy import AgyAdapter, RULES, followup_prompt, prepare_home, workspace_config
from app.adapters.base import AdapterResult
from app.provider_errors import agy_error
from app.provider_policy import normalize
from app.runner import SessionRunner, _get_adapter
from tests.test_works import Scripted

FAKE_AGY = '''import json, os, sys
argv = sys.argv[1:]
with open(os.environ['FAKE_AGY_LOG'], 'a') as f:
    f.write(json.dumps({'argv': argv, 'home': os.environ.get('HOME'), 'cwd': os.getcwd()}) + '\\n')
scenario = os.environ.get('FAKE_AGY_SCENARIO', 'ok')
model = argv[argv.index('--model') + 1]
conv = argv[argv.index('--conversation') + 1] if '--conversation' in argv else 'conv-new'
if scenario == 'new_conversation':
    conv = 'conv-other'
if scenario == 'auth':
    print('Error: Please sign in to view available models.', file=sys.stderr); sys.exit(1)
print(json.dumps({'event': 'init', 'conversation_id': conv, 'init': {'tools': ['view_file', 'invoke_subagent'],
                  'model': 'gemini-3.8-flash-high' if scenario == 'wrong_model' else model}}), flush=True)
print(json.dumps({'event': 'step_update', 'step_update': {'conversation_id': conv, 'state': 'DONE'}}), flush=True)
# K-061: a tool step as agy 1.x streams it (seen live): ACTIVE with its parameters, then DONE/ERROR
print(json.dumps({'event': 'step_update', 'step_update': {'conversation_id': conv, 'state': 'ACTIVE', 'step_type': 'tool',
    'tool_name': 'run_command', 'tool_info': {'name': 'run_command', 'parameters': {'CommandLine': 'npm   test'}}}}), flush=True)
print(json.dumps({'event': 'step_update', 'step_update': {'conversation_id': conv, 'state': 'ACTIVE', 'step_type': 'tool',
    'tool_name': 'write_to_file', 'tool_info': {'parameters': {'TargetFile': '/w/app.py', 'CodeContent': 'SECRET BODY'}}}}), flush=True)
print(json.dumps({'event': 'step_update', 'step_update': {'conversation_id': conv, 'state': 'DONE', 'step_type': 'tool',
    'tool_name': 'run_command'}}), flush=True)
result = {'conversation_id': conv, 'status': 'SUCCESS', 'response': 'done', 'usage': {'total_tokens': 1}}
if scenario == 'denied_command':
    result.update(response='', denied_actions=[{'action': 'command', 'display_name': 'RunCommand'}])
if scenario == 'denied_command_narrated':
    result.update(response='A subagent is listing the files; I will report back.',
                  denied_actions=[{'action': 'command', 'display_name': 'RunCommand'}])
if scenario == 'denied_mixed':
    result.update(response='', denied_actions=[{'action': 'command'}, {'action': 'write_file'}])
if scenario == 'denied_write':
    result.update(response='', denied_actions=[{'action': 'write_file', 'display_name': 'WriteToFile'}])
if scenario == 'empty':
    result.update(response='')
if scenario == 'quota':
    result.update(status='ERROR', response='', error='429 RESOURCE_EXHAUSTED: quota exceeded')
if scenario == 'other_error':
    result.update(status='ERROR', response='', error='internal error')
print(json.dumps({'event': 'result', 'result': result}), flush=True)
sys.exit(1 if result['status'] != 'SUCCESS' else 0)
'''

FAKE_SECURITY = '''import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_SECURITY_LOG'], 'a') as f:
    f.write(json.dumps({'args': args, 'home': os.environ.get('HOME')}) + '\\n')
import time
time.sleep(float(os.environ.get('FAKE_SECURITY_SLEEP', '0')))
if args[0] == 'create-keychain' and not os.environ.get('FAKE_SECURITY_NOFILE'):
    open(args[-1], 'w').close()
if args[0] == 'unlock-keychain' and not os.path.islink(args[-1]) and open(args[-1]).read() == 'locked':
    sys.exit(51)   # K-058: a login.keychain-db macOS 27 has locked no longer takes the empty password
sys.exit(1 if os.environ.get('FAKE_SECURITY_FAIL') == args[0] else 0)
'''


def script(path, body):
    path.write_text('#!' + sys.executable + '\n' + body)
    path.chmod(0o755)
    return str(path)


def lines(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.ws = self.root / 'ws'; self.ws.mkdir()
        self.home = str(self.root / '.system' / 'agy-home-h1'); (self.root / '.system').mkdir()
        self.agy_log, self.sec_log = self.root / 'agy.log', self.root / 'security.log'
        env = {'CONDUCTOR_AGY_BIN': script(self.root / 'agy', FAKE_AGY),
               'CONDUCTOR_SECURITY_BIN': script(self.root / 'security', FAKE_SECURITY),
               'FAKE_AGY_LOG': str(self.agy_log), 'FAKE_SECURITY_LOG': str(self.sec_log)}
        patcher = patch.dict(os.environ, env); patcher.start(); self.addCleanup(patcher.stop)

    async def run_agy(self, scenario='ok', profile='READ_ONLY', sid=None, model='gemini-3.1-pro-high'):
        events = []
        with patch.dict(os.environ, {'FAKE_AGY_SCENARIO': scenario}):
            async for e in AgyAdapter(self.home, model).run('TASK', str(self.ws), sid, profile=profile):
                events.append(e)
        return events, events[-1]['result']


class Process(Base):
    async def test_tool_steps_become_activity_without_contents(self):   # K-061
        events, result = await self.run_agy()
        self.assertTrue(result.successful)
        self.assertEqual([(e['tool'], e['detail']) for e in events if e['type'] == 'activity'],
                         [('run_command', 'npm test'), ('write_to_file', '/w/app.py')])
        self.assertNotIn('SECRET BODY', json.dumps(events, default=str))
        from app.adapters.agy import tool_detail
        self.assertEqual(tool_detail({'parameters': {'Url': 'https://x/?token=abc', 'Query': 'secret'}}), '')   # review round 2

    async def test_argv_home_and_profiles(self):
        events, result = await self.run_agy(profile='WRITE', sid='conv-1')
        self.assertTrue(result.successful)
        seen = lines(self.agy_log)[0]
        self.assertEqual(seen['home'], self.home)
        self.assertEqual(seen['cwd'], os.path.realpath(self.ws))
        argv = seen['argv']
        self.assertEqual(argv[0], '-p'); self.assertTrue(argv[1].startswith('CONDUCTOR RULES') and argv[1].endswith('TASK'))
        self.assertIn('also not in subagents', argv[1])
        self.assertIn('also not in subagents', followup_prompt('WRITE'))
        started = next(e for e in events if e['type'] == 'started')
        self.assertEqual((started['model'], started['tools']), ('gemini-3.1-pro-high', ['view_file', 'invoke_subagent']))
        for flag, value in (('--model', 'gemini-3.1-pro-high'), ('--output-format', 'stream-json'),
                            ('--print-timeout', '0'), ('--mode', 'accept-edits'), ('--conversation', 'conv-1')):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertNotIn('--dangerously-skip-permissions', argv)
        self.assertEqual([e['type'] for e in events], ['process_started', 'started', 'activity', 'activity', 'process_stopped', 'completed'])
        await self.run_agy()
        self.assertNotIn('--mode', lines(self.agy_log)[1]['argv'])          # READ_ONLY: default request-review

    async def test_keychain_is_created_once_and_unlocked_every_run(self):
        await self.run_agy(); await self.run_agy()
        calls = [c['args'][0] for c in lines(self.sec_log)]
        self.assertEqual(calls, ['create-keychain', 'set-keychain-settings', 'unlock-keychain', 'unlock-keychain'])
        self.assertTrue(all(c['home'] == self.home for c in lines(self.sec_log)))
        keychain = Path(self.home) / 'Library/Keychains/agy.keychain-db'
        self.assertEqual(stat.S_IMODE(keychain.stat().st_mode), 0o600)
        login = keychain.with_name('login.keychain-db')
        self.assertEqual((os.readlink(login), {c['args'][-1] for c in lines(self.sec_log)}), ('agy.keychain-db', {str(keychain)}))

    async def test_an_old_login_keychain_is_kept_while_it_unlocks_else_set_aside(self):   # K-058 (macOS 27)
        keychains = Path(self.home) / 'Library/Keychains'; keychains.mkdir(parents=True)
        login = keychains / 'login.keychain-db'; login.write_text('signed-in')
        _, result = await self.run_agy()   # still unlocks (this login session): used as it is
        self.assertFalse(result.is_error, result.terminal_reason)
        self.assertEqual([c['args'][0] for c in lines(self.sec_log)], ['unlock-keychain'])
        self.assertEqual((login.is_symlink(), login.read_text(), (keychains / 'agy.keychain-db').exists()), (False, 'signed-in', False))
        self.sec_log.unlink(); login.write_text('locked')
        _, result = await self.run_agy()   # locked: set aside (kept), a new keychain, sign in again
        self.assertFalse(result.is_error, result.terminal_reason)
        self.assertEqual([c['args'][0] for c in lines(self.sec_log)],
                         ['unlock-keychain', 'create-keychain', 'set-keychain-settings', 'unlock-keychain'])
        aside = list(keychains.glob('login.keychain-db.unopenable-*'))
        self.assertEqual((len(aside), aside[0].read_text(), os.readlink(login)), (1, 'locked', 'agy.keychain-db'))
        login.unlink()
        outside = self.root / 'outside.keychain-db'; outside.write_text('theirs')
        login.symlink_to(outside)   # a link elsewhere: refused, the target untouched
        _, result = await self.run_agy()
        self.assertEqual((result.terminal_reason, outside.read_text()), ('agy_home_unusable', 'theirs'))
        for f in keychains.iterdir():
            f.unlink()
        keychains.rmdir(); elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        keychains.symlink_to(elsewhere)   # the keychain folder itself linked out: refused, nothing created there
        _, result = await self.run_agy()
        self.assertEqual((result.terminal_reason, list(elsewhere.iterdir())), ('agy_home_unusable', []))

    async def test_permissions_are_pinned_empty(self):
        settings = Path(self.home) / '.gemini/antigravity-cli/settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({'permissions': {'allow': ['command(*)']}, 'theme': 'x'}))
        await self.run_agy()
        self.assertEqual(json.loads(settings.read_text()), {'permissions': {'allow': []}, 'theme': 'x'})

    async def test_unusable_home_fails_closed_without_a_process(self):
        with patch.dict(os.environ, {'FAKE_SECURITY_FAIL': 'unlock-keychain'}):
            _, result = await self.run_agy()
        self.assertEqual((result.error_code, result.terminal_reason), ('AUTH', 'agy_home_unusable'))
        self.assertEqual(lines(self.agy_log), [])
        link = self.root / 'link'; link.symlink_to(self.ws)
        with self.assertRaises(ValueError):
            prepare_home(str(link))
        import pwd
        with self.assertRaises(ValueError):
            prepare_home(pwd.getpwuid(os.getuid()).pw_dir)


class Review(Base):
    """Gemini review round 1 (P2/P3): event loop, concurrent pinning, keychain creation race."""
    async def test_slow_security_does_not_block_the_event_loop(self):
        ticks = 0
        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02); ticks += 1
        task = asyncio.create_task(ticker())
        with patch.dict(os.environ, {'FAKE_SECURITY_SLEEP': '0.3'}):
            await self.run_agy()                        # three security calls, ~0.9 s
        task.cancel()
        self.assertGreater(ticks, 20)

    def test_concurrent_pinning_of_one_account(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.adapters.agy import pin_permissions
        settings = Path(self.home) / '.gemini/antigravity-cli/settings.json'
        settings.parent.mkdir(parents=True)
        for _ in range(5):
            settings.write_text(json.dumps({'permissions': {'allow': ['command(*)']}}))
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda _: pin_permissions(self.home), range(16)))
            self.assertEqual(json.loads(settings.read_text()), {'permissions': {'allow': []}})
        self.assertEqual([p.name for p in settings.parent.iterdir() if p.name.endswith('.tmp')], [])

    async def test_losing_the_keychain_creation_race_is_fine(self):
        with patch.dict(os.environ, {'FAKE_SECURITY_FAIL': 'create-keychain'}):
            _, result = await self.run_agy()           # create "failed" but the keychain exists
        self.assertTrue(result.successful)
        os.remove(Path(self.home) / 'Library/Keychains/agy.keychain-db')   # K-058: the real file; login only points at it
        with patch.dict(os.environ, {'FAKE_SECURITY_FAIL': 'create-keychain', 'FAKE_SECURITY_NOFILE': '1'}):
            _, result = await self.run_agy()           # really failed: no process
        self.assertEqual(result.terminal_reason, 'agy_home_unusable')


class Preflight(Base):
    async def test_workspace_mcp_and_hooks_stop_before_any_process(self):
        for folder, name in (('.agents', 'mcp_config.json'), ('.Agent', 'hooks.json'), ('_agents', 'MCP_CONFIG.json'),
                             ('_agent', 'plugins/x/mcp_config.json')):
            with self.subTest(folder=folder, name=name):
                target = self.ws / folder / name; target.parent.mkdir(parents=True); target.write_text('{}')
                _, result = await self.run_agy()
                self.assertEqual(result.terminal_reason, 'agy_workspace_config')
                self.assertEqual(result.provider_error['path'], str(Path(folder) / name) if 'plugins' not in name
                                 else str(Path(folder) / 'plugins/x'))
                self.assertEqual(lines(self.agy_log), [])
                self.assertEqual(lines(self.sec_log), [])
                os.remove(target)

    async def test_instruction_files_do_not_block(self):
        (self.ws / '.agents/skills/s').mkdir(parents=True); (self.ws / '.agents/skills/s/SKILL.md').write_text('x')
        (self.ws / 'GEMINI.md').write_text('x')
        self.assertIsNone(workspace_config(str(self.ws)))
        _, result = await self.run_agy()
        self.assertTrue(result.successful)


class Classification(Base):
    async def test_outcomes(self):
        cases = {'wrong_model': ('UNKNOWN', 'model_mismatch'), 'empty': (None, 'completed'),
                 'denied_command': ('COMMAND_DENIED', 'permission_denied'), 'denied_write': ('PERMISSION', 'permission_denied'),
                 'denied_command_narrated': ('COMMAND_DENIED', 'permission_denied'), 'denied_mixed': ('PERMISSION', 'permission_denied'),
                 'quota': ('QUOTA_EXCEEDED', 'error'), 'other_error': ('UNKNOWN', 'error'), 'auth': ('AUTH', 'no_result')}
        for scenario, (code, reason) in cases.items():
            with self.subTest(scenario):
                events, result = await self.run_agy(scenario)
                self.assertFalse(result.successful)
                self.assertEqual((result.error_code, result.terminal_reason), (code, reason))
                self.assertEqual(events[-1]['type'], 'failed')
        events, _ = await self.run_agy('denied_command')
        self.assertIn('approval_required', [e['type'] for e in events])
        self.assertEqual(AdapterResult('', 'c', None, None, True, 'x', 1, 'COMMAND_DENIED').failure_state, 'WAITING_APPROVAL')

    async def test_a_resume_that_opens_another_conversation_is_refused(self):
        _, result = await self.run_agy('new_conversation', sid='conv-1')
        self.assertEqual(result.terminal_reason, 'resume_mismatch')

    def test_error_text_table(self):
        self.assertEqual(agy_error('RESOURCE_EXHAUSTED'), 'QUOTA_EXCEEDED')
        self.assertEqual(agy_error('AI: Out of credits'), 'QUOTA_EXCEEDED')
        self.assertEqual(agy_error('Please sign in to view available models.'), 'AUTH')
        self.assertEqual(agy_error('interrupted'), 'UNKNOWN')

    def test_bare_agy_is_never_probed(self):
        from starlette.testclient import TestClient
        from app.api import create_app
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n'
            'allowed:\n  author: [claude, agy@h1/gemini-3.1-pro-high]\n  reviewer: [claude]\n')
        seen = []
        with patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}), \
                patch.object(provider_auth, 'check', wraps=provider_auth.check) as check, \
                patch.object(provider_auth, '_probe', lambda p, b, h=None, m=None: (seen.append(h), ('authenticated', True))[1]), \
                patch.dict(provider_auth._cache, clear=True):
            app = create_app(); app.state.runner_db = str(self.root / '.system/state.db')
            client = TestClient(app)
            cross = client.get('/api/sessions/providers?check_auth=true', headers={'sec-fetch-site': 'cross-site'})
            self.assertEqual((cross.status_code, cross.json()['code']), (403, 'untrusted_origin'))
            self.assertEqual(check.call_count, 0)
            ok = client.get('/api/sessions/providers?check_auth=true')
            self.assertEqual(ok.status_code, 200)
            body = ok.json()
        self.assertEqual(body['availability']['agy']['auth'], 'unknown')
        self.assertEqual(body['availability']['agy@h1/gemini-3.1-pro-high']['auth'], 'authenticated')
        self.assertNotIn(str(self.root / '.system/agy-home'), seen)
        self.assertIn(str(self.root / '.system/agy-home-h1'), seen)

    def test_session_check_classification(self):
        self.assertEqual(provider_auth.classify('agy', 0, 'gemini-3.1-pro-high\tGemini 3.1 Pro'), 'authenticated')
        self.assertEqual(provider_auth.classify('agy', 1, 'Error: Please sign in to view available models.'), 'unauthenticated')
        self.assertEqual(provider_auth.classify('agy', 1, 'boom'), 'unknown')
        listing = 'Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\nclaude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n'
        self.assertEqual(provider_auth.classify('agy', 0, listing, 'claude-sonnet-5-5-high'), 'authenticated')
        self.assertEqual(provider_auth.classify('agy', 0, listing, 'claude-sonnet-4-6'), 'model_unavailable')
        self.assertEqual(provider_auth.classify('agy', 0, listing, 'gemini-3.1-pro'), 'model_unavailable')   # exact slug


class Policy(unittest.TestCase):
    def test_model_is_required_and_identities_resolve(self):
        with self.assertRaises(ValueError):
            normalize({'roles': {'author': ['agy@h1'], 'reviewer': ['claude']}})
        policy = normalize({'roles': {'author': ['claude', 'agy@h1/claude-sonnet-4-6', 'agy@h1/gemini-3.1-pro-high',
                                                 'agy@h2/gemini-3.1-pro-high'], 'reviewer': ['agy@h2/gemini-3.1-pro-high', 'claude']}})
        self.assertEqual(len(policy['roles']['author']), 4)
        adapter = _get_adapter('agy@h2/gemini-3.1-pro-high', '/x/.system/state.db')
        self.assertEqual((adapter.home, adapter.model), ('/x/.system/agy-home-h2', 'gemini-3.1-pro-high'))


def denied(sid='conv-a'):
    return AdapterResult('', sid, None, None, True, 'permission_denied', 0, 'COMMAND_DENIED',
                         {'denied_actions': [{'action': 'command'}]})


class FollowUp(unittest.IsolatedAsyncioTestCase):
    """K-020: one same-conversation follow-up after a command denial, outside the context budget."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.ws = root / 'ws'; self.ws.mkdir()
        self.db = root / 'state.db'; self.conn = store.connect(self.db); self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        self.agy = 'agy@h1/gemini-3.1-pro-high'
        frozen = normalize({'roles': {'author': [self.agy, 'claude'], 'reviewer': ['claude']}})
        with store.transaction(self.conn):
            repo.create_session(self.conn, 's', str(self.ws), 'auto', policy=frozen)
            repo.add_session_step(self.conn, 's0', 's', 0, self.agy, 'goal', role='author')

    def attempts(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM session_attempts ORDER BY attempt_no')]

    async def test_one_follow_up_in_the_same_conversation(self):
        log = []
        await SessionRunner(self.db, adapter_factory=Scripted(log, {self.agy: [denied()]})).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'SUCCEEDED')
        first, second = self.attempts()
        self.assertEqual((first['status'], first['error_code']), ('FAILED', 'COMMAND_DENIED'))
        self.assertEqual((second['status'], second['handover_reason'], second['continuation_of']),
                         ('COMPLETED', 'command_denied_followup', first['id']))
        self.assertEqual(log[1]['psid'], 'conv-a')
        self.assertEqual(log[1]['prompt'], followup_prompt('WRITE'))
        self.assertEqual(repo.continuation_count(self.conn, 's0'), 0)
        self.assertEqual([e['provider'] for e in log], [self.agy, self.agy])   # no hand-off to claude

    async def test_a_second_denial_waits_for_the_user(self):
        await SessionRunner(self.db, adapter_factory=Scripted([], {self.agy: [denied(), denied()]})).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'WAITING_APPROVAL')
        self.assertEqual(len(self.attempts()), 2)

    async def test_other_denials_are_not_followed_up(self):
        write = AdapterResult('', 'conv-a', None, None, True, 'permission_denied', 0, 'PERMISSION',
                              {'denied_actions': [{'action': 'write_file'}]})
        await SessionRunner(self.db, adapter_factory=Scripted([], {self.agy: [write]})).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'WAITING_APPROVAL')
        self.assertEqual(len(self.attempts()), 1)


if __name__ == '__main__':
    unittest.main()
