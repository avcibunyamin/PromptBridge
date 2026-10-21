"""Real local subprocesses with deterministic provider protocol fixtures."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.process import group_exists


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def script(self, source):
        path = self.root / 'provider'
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)
        return str(path)

    async def collect(self, adapter):
        return [event async for event in adapter.run('goal', str(self.root), 'existing', profile='READ_ONLY' if isinstance(adapter,ClaudeAdapter) else 'WRITE')]

    async def test_claude_resume_stdin_readonly_and_nonzero_exit(self):
        script = self.script('''import json,sys
assert '--resume' in sys.argv and '--session-id' not in sys.argv
assert '--safe-mode' in sys.argv
assert '--tools' in sys.argv and 'Read,Glob,Grep' in sys.argv
assert sys.stdin.read() == 'goal'
print(json.dumps({'type':'result','session_id':'real-session','is_error':False,'terminal_reason':'completed','result':'answer','usage':{'output_tokens':1}}))
sys.exit(2)
''')
        with patch.dict(os.environ, {'CONDUCTOR_CLAUDE_BIN': script}):
            events = await self.collect(ClaudeAdapter())
        self.assertEqual(events[-1]['type'], 'failed')
        self.assertEqual(events[-1]['result'].exit_code, 2)
        self.assertEqual(events[-1]['result'].provider_session_id, 'real-session')

    async def test_claude_missing_and_duplicate_result_rejected(self):
        for source in ["print('{}')", "import json\nr={'type':'result'}\nprint(json.dumps(r));print(json.dumps(r))"]:
            with patch.dict(os.environ, {'CONDUCTOR_CLAUDE_BIN': self.script(source)}):
                with self.assertRaises(ValueError):
                    await self.collect(ClaudeAdapter())

    async def test_codex_correlates_notifications_and_parses_terminal(self):
        script = self.script('''import json,sys,time
counter=0
def receive():
 global counter
 m=json.loads(sys.stdin.readline())
 if 'id' in m:
  assert m['id']>counter;counter=m['id']
 return m
def emit(m):
 s=json.dumps(m)+'\\n';sys.stdout.write(s[:7]);sys.stdout.flush();sys.stdout.write(s[7:]);sys.stdout.flush()
m=receive();assert m['params']['clientInfo']['name']=='conductor'
emit({'id':m['id'],'result':{}})
assert receive()['method']=='initialized'
m=receive();assert m['method']=='thread/resume' and m['params']['threadId']=='existing'
assert m['params']['sandbox']=='workspace-write'
emit({'method':'thread/started','params':{'thread':{'id':'t'}}})
emit({'id':m['id'],'result':{'thread':{'id':'t'}}})
m=receive();assert m['params']['threadId']=='t' and m['params']['input'][0]['text']=='goal'
sys.stderr.write('x'*100000);sys.stderr.flush()
emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
emit({'method':'item/agentMessage/delta','params':{'threadId':'other','turnId':'turn','itemId':'x','delta':'wrong'}})
emit({'method':'item/agentMessage/delta','params':{'threadId':'t','turnId':'turn','itemId':'a','delta':'answer'}})
# TB-15c: a command without output arrives with aggregatedOutput null (seen with codex-cli 0.159.2)
emit({'method':'item/completed','params':{'threadId':'t','turnId':'turn','item':{'id':'c1','type':'commandExecution','command':'echo ok > y.txt','exitCode':0,'aggregatedOutput':None}}})
emit({'method':'item/completed','params':{'threadId':'t','turnId':'turn','item':{'id':'c2','type':'commandExecution','command':'ls','exitCode':0,'aggregatedOutput':'y.txt'}}})
emit({'method':'item/completed','params':{'threadId':'t','turnId':'turn','item':{'id':'a','type':'agentMessage','text':None}}})
emit({'method':'thread/tokenUsage/updated','params':{'threadId':'t','tokenUsage':{'total':{'totalTokens':3}}}})
emit({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'turn','status':'completed'}}})
sys.stdin.read()
''')
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': script}):
            events = await asyncio.wait_for(self.collect(CodexAdapter(str(self.root / 'codex-home'))), 3)
        self.assertEqual(events[-1]['type'], 'completed')
        self.assertEqual(events[-1]['result'].final_text, 'answer')
        self.assertEqual(events[-1]['result'].provider_session_id, 't')
        self.assertEqual(events[-1]['result'].usage['total']['totalTokens'], 3)
        self.assertEqual([(e['command'], e['output']) for e in events if e['type'] == 'command_result'],
                         [('echo ok > y.txt', ''), ('ls', 'y.txt')])

    async def test_codex_model_and_effort_reach_turn_start_and_session_log_is_checked(self):
        script = self.script('''import json,os,pathlib,sys
def receive(): return json.loads(sys.stdin.readline())
def emit(m): sys.stdout.write(json.dumps(m)+'\\n'); sys.stdout.flush()
m=receive(); emit({'id':m['id'],'result':{}})
assert receive()['method']=='initialized'
m=receive(); emit({'id':m['id'],'result':{'thread':{'id':'t'}}})
m=receive()
assert m['method']=='turn/start'
assert m['params']['model']=='gpt-6-sol'
assert m['params']['effort']=='high'
emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
p=pathlib.Path(os.environ['CODEX_HOME'])/'sessions'/'2026'/'10'/'04'
p.mkdir(parents=True, exist_ok=True)
(p/'rollout-t.jsonl').write_text(json.dumps({'type':'turn_context','payload':{'model':'gpt-6-sol','effort':'high'}})+'\\n')
emit({'method':'item/completed','params':{'threadId':'t','turnId':'turn','item':{'id':'a','type':'agentMessage','text':'answer'}}})
emit({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'turn','status':'completed'}}})
sys.stdin.read()
''')
        async def run():
            return [e async for e in CodexAdapter(str(self.root / 'codex-home'), 'gpt-6-sol', 'high').run(
                'goal', str(self.root), None, profile='READ_ONLY')]
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': script}):
            events = await asyncio.wait_for(run(), 3)
        self.assertEqual([e['type'] for e in events].count('started'), 1)
        self.assertEqual(events[-1]['type'], 'completed')

    async def test_codex_model_mismatch_fails_without_silent_fallback(self):
        script = self.script('''import json,os,pathlib,sys
def receive(): return json.loads(sys.stdin.readline())
def emit(m): sys.stdout.write(json.dumps(m)+'\\n'); sys.stdout.flush()
m=receive(); emit({'id':m['id'],'result':{}})
assert receive()['method']=='initialized'
m=receive(); emit({'id':m['id'],'result':{'thread':{'id':'t'}}})
m=receive(); emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
p=pathlib.Path(os.environ['CODEX_HOME'])/'sessions'/'2026'/'10'/'04'
p.mkdir(parents=True, exist_ok=True)
(p/'rollout-t.jsonl').write_text(json.dumps({'type':'turn_context','payload':{'model':'gpt-5.5','effort':'high'}})+'\\n')
emit({'method':'item/completed','params':{'threadId':'t','turnId':'turn','item':{'id':'a','type':'agentMessage','text':'answer'}}})
emit({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'turn','status':'completed'}}})
sys.stdin.read()
''')
        async def run():
            return [e async for e in CodexAdapter(str(self.root / 'codex-home'), 'gpt-6-sol', 'high').run(
                'goal', str(self.root), None, profile='READ_ONLY')]
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': script}):
            events = await asyncio.wait_for(run(), 3)
        self.assertEqual((events[-1]['type'], events[-1]['result'].terminal_reason), ('failed', 'model_mismatch'))
        self.assertEqual(events[-1]['result'].provider_error['got']['model'], 'gpt-5.5')

    async def test_codex_missing_session_context_has_its_own_reason(self):
        script = self.script('''import json,sys
def receive(): return json.loads(sys.stdin.readline())
def emit(m): sys.stdout.write(json.dumps(m)+'\\n'); sys.stdout.flush()
m=receive(); emit({'id':m['id'],'result':{}})
assert receive()['method']=='initialized'
m=receive(); emit({'id':m['id'],'result':{'thread':{'id':'t'}}})
m=receive(); emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
emit({'method':'item/completed','params':{'threadId':'t','turnId':'turn','item':{'id':'a','type':'agentMessage','text':'answer'}}})
emit({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'turn','status':'completed'}}})
sys.stdin.read()
''')
        async def run():
            return [e async for e in CodexAdapter(str(self.root / 'codex-home'), 'gpt-6-sol', 'high').run(
                'goal', str(self.root), None, profile='READ_ONLY')]
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': script}):
            events = await asyncio.wait_for(run(), 3)
        self.assertEqual((events[-1]['type'], events[-1]['result'].terminal_reason), ('failed', 'model_context_missing'))

    async def test_codex_preflight_rejects_catalog_problem_before_process(self):
        from app import catalog
        saved = dict(catalog._cache); self.addCleanup(lambda: catalog._cache.update(saved))
        catalog._cache.update(entries=[{'identity': 'codex/gpt-5.5', 'provider': 'codex', 'account': None,
                                        'model': 'gpt-5.5', 'efforts': ['low', 'medium', 'high', 'xhigh'],
                                        'status': 'available', 'source': 'live'}],
                              errors={}, checked_at=time.time())
        async def run():
            return [e async for e in CodexAdapter(str(self.root / 'codex-home'), 'gpt-5.5', 'max',
                                                  db_path=str(self.root / '.system/state.db'),
                                                  identity='codex/gpt-5.5~max').run('goal', str(self.root), None,
                                                                                   profile='READ_ONLY')]
        with patch('app.adapters.codex.codex_binary', side_effect=AssertionError('must not start')):
            events = await run()
        self.assertEqual((events[-1]['type'], events[-1]['result'].terminal_reason), ('failed', 'effort_unsupported'))

    async def test_codex_preflight_unauthenticated_uses_auth_flow(self):
        async def run():
            return [e async for e in CodexAdapter(str(self.root / 'codex-home'), 'gpt-6-sol', 'high',
                                                  db_path=str(self.root / '.system/state.db'),
                                                  identity='codex/gpt-6-sol~high').run('goal', str(self.root), None,
                                                                                      profile='READ_ONLY')]
        with patch('app.catalog.validate_identity', return_value='unauthenticated'), \
                patch('app.adapters.codex.codex_binary', side_effect=AssertionError('must not start')):
            events = await run()
        result = events[-1]['result']
        self.assertEqual((events[-1]['type'], result.terminal_reason, result.error_code), ('failed', 'unauthenticated', 'AUTH'))

    async def test_codex_eof_not_success(self):
        script = self.script('import sys\nsys.stdin.readline()')
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': script}):
            with self.assertRaisesRegex(ValueError, 'EOF'):
                await self.collect(CodexAdapter(str(self.root / 'codex-home')))

    async def test_cancelling_claude_stops_parent_and_child_group(self):
        pidfile = self.root / 'pid'
        script = self.script('''import os,subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
Path('pid').write_text(str(os.getpid()))
sys.stdin.read()
time.sleep(30)
''')
        with patch.dict(os.environ, {'CONDUCTOR_CLAUDE_BIN': script}):
            task = asyncio.create_task(self.collect(ClaudeAdapter()))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if pidfile.exists() and pidfile.read_text().strip(): break
                await asyncio.sleep(.02)
            self.assertTrue(pidfile.exists())
            pid = int(pidfile.read_text().strip())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            # Allow reparented children a moment to be reaped by the OS.
            deadline = time.monotonic() + 10
            while group_exists(pid) and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            self.assertFalse(group_exists(pid))


class CodexBinary(unittest.TestCase):
    """F3-06: the ChatGPT app moved its CLI; the new bundle path wins, the old one still works."""
    def test_new_bundle_path_first_old_as_fallback_env_always_wins(self):
        import app.adapters.codex as codex
        with tempfile.TemporaryDirectory() as tmp:
            new, old, custom = (Path(tmp) / n for n in ('new-codex', 'old-codex', 'custom-codex'))
            for f in (new, old, custom):
                f.write_text('#!/bin/sh\n'); f.chmod(0o700)
            with patch.object(codex, 'CODEX_CANDIDATES', (str(new), str(old))), \
                    patch('app.adapters.process.shutil.which', return_value=None), patch.dict(os.environ, {}, clear=False):
                os.environ.pop('CONDUCTOR_CODEX_BIN', None)
                self.assertEqual(codex.codex_binary(), str(new.resolve()))
                new.unlink()
                self.assertEqual(codex.codex_binary(), str(old.resolve()))
                with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': str(custom)}):
                    self.assertEqual(codex.codex_binary(), str(custom.resolve()))
                old.unlink()
                with self.assertRaises(FileNotFoundError):
                    codex.codex_binary()
