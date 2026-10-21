"""Quota injected into real local provider subprocesses; no account quota spent."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from app import store, runner_store as repo
from app.runner import SessionRunner
from app.provider_policy import normalize

CODEX = r'''
import json,sys
from pathlib import Path
def emit(m): print(json.dumps(m),flush=True)
def read(): return json.loads(sys.stdin.readline())
m=read();emit({'id':m['id'],'result':{}})
assert read()['method']=='initialized'
m=read();emit({'id':m['id'],'result':{'thread':{'id':'primary-thread'}}})
m=read();assert m['params']['sandboxPolicy']['type']=='workspaceWrite'
Path('partial.txt').write_text('part one')
emit({'id':m['id'],'result':{'turn':{'id':'t'}}})
emit({'method':'turn/completed','params':{'threadId':'primary-thread','turn':{
'id':'t','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded','message':'fixture quota'}}}})
sys.stdin.read()
'''
CLAUDE = r'''
import json,sys
from pathlib import Path
assert 'Read,Glob,Grep,Write,Edit' in sys.argv
assert sys.argv[sys.argv.index('--permission-mode')+1]=='acceptEdits'
prompt=sys.stdin.read()
assert 'HANDOFF' in prompt and 'partial.txt' in prompt
assert Path('partial.txt').read_text()=='part one'
Path('partial.txt').write_text('part one + backup')
print(json.dumps({'type':'result','subtype':'success','session_id':'backup-thread',
'is_error':False,'result':'continued existing work','terminal_reason':'completed'}),flush=True)
'''


class FallbackSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_process_quota_cleanup_backup_and_persistent_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); ws=root/'workspace';ws.mkdir()
            for name,source in [('codex',CODEX),('claude',CLAUDE)]:
                f=root/name;f.write_text('#!'+sys.executable+'\n'+source);f.chmod(0o700)
            db=root/'db';conn=store.connect(db);store.migrate(conn)
            policy=normalize({'roles':{'author':['codex','claude'],'reviewer':['claude']}})
            repo.create_session(conn,'s',str(ws),'auto',policy=policy)
            repo.add_session_step(conn,'step','s',0,'codex','finish the file',role='author')
            with patch.dict(os.environ,{'CONDUCTOR_CODEX_BIN':str(root/'codex'),'CONDUCTOR_CLAUDE_BIN':str(root/'claude')}):
                await SessionRunner(db,step_timeout=10).run_session('s')
            session=repo.get_session(conn,'s')
            attempts=[dict(r) for r in conn.execute('SELECT * FROM session_attempts ORDER BY attempt_no')]
            owners=[dict(r) for r in conn.execute('SELECT * FROM process_owners ORDER BY recorded_at')]
            self.assertEqual(session['status'],'SUCCEEDED',attempts)
            self.assertEqual([a['status'] for a in attempts],['QUOTA','COMPLETED'])
            self.assertEqual([o['status'] for o in owners],['STOPPED','STOPPED'])
            self.assertLessEqual(owners[0]['stopped_at'],owners[1]['recorded_at'])
            self.assertEqual((ws/'partial.txt').read_text(),'part one + backup')
            self.assertEqual(conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0],0)
            conn.close()

    async def test_claude_structured_quota_to_codex_readonly(self):
        claude = '''import json,sys
assert 'Read,Glob,Grep' in sys.argv
assert sys.argv[sys.argv.index('--permission-mode')+1]=='dontAsk'
sys.stdin.read()
print(json.dumps({'type':'assistant','error':'rate_limit','message':{'content':[]}}),flush=True)
print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'result':'limit'}),flush=True)
sys.exit(1)
'''
        codex = '''import json,sys
def emit(m): print(json.dumps(m),flush=True)
def read(): return json.loads(sys.stdin.readline())
m=read();emit({'id':m['id'],'result':{}})
assert read()['method']=='initialized'
m=read();assert m['params']['sandbox']=='read-only'
emit({'id':m['id'],'result':{'thread':{'id':'review-thread'}}})
m=read();assert m['params']['sandboxPolicy']['type']=='readOnly'
assert 'HANDOFF' in m['params']['input'][0]['text']
emit({'id':m['id'],'result':{'turn':{'id':'t'}}})
emit({'method':'item/completed','params':{'threadId':'review-thread','item':{'id':'a','type':'agentMessage','text':'Review complete'}}})
emit({'method':'turn/completed','params':{'threadId':'review-thread','turn':{'id':'t','status':'completed'}}})
sys.stdin.read()
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); ws=root/'work'; ws.mkdir()
            for name,source in [('claude',claude),('codex',codex)]:
                file=root/name;file.write_text('#!'+sys.executable+'\n'+source);file.chmod(0o700)
            db=root/'db';conn=store.connect(db);store.migrate(conn)
            policy=normalize({'roles':{'author':['codex'],'reviewer':['claude','codex']}})
            repo.create_session(conn,'s',str(ws),'auto',policy=policy)
            repo.add_session_step(conn,'step','s',0,'claude','review only',role='reviewer')
            with patch.dict(os.environ,{'CONDUCTOR_CODEX_BIN':str(root/'codex'),'CONDUCTOR_CLAUDE_BIN':str(root/'claude')}):
                await SessionRunner(db,step_timeout=10).run_session('s')
            self.assertEqual(repo.get_session(conn,'s')['status'],'SUCCEEDED')
            rows=conn.execute('SELECT provider,profile,status FROM session_attempts ORDER BY attempt_no').fetchall()
            self.assertEqual([tuple(r) for r in rows],[('claude','READ_ONLY','QUOTA'),('codex','READ_ONLY','COMPLETED')])
            conn.close()
