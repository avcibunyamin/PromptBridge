"""Injected wire signals over real subprocess pipes; not real account exhaustion."""
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

WIRE = r'''
import json, sys
from pathlib import Path
def read(): return json.loads(sys.stdin.readline())
def emit(m): print(json.dumps(m),flush=True)
counter=Path('calls.json')
calls=json.loads(counter.read_text()) if counter.exists() else []
n=len(calls)
m=read();emit({'id':m['id'],'result':{}})
assert read()['method']=='initialized'
m=read()
calls.append({'method':m['method'],'params':m['params']})
counter.write_text(json.dumps(calls))
sid='fresh-'+str(n) if m['method']=='thread/start' else m['params']['threadId']
emit({'id':m['id'],'result':{'thread':{'id':sid}}})
m=read();prompt=m['params']['input'][0]['text']
assert m['params']['sandboxPolicy']['type']=='workspaceWrite'
Path('prompt-'+str(n)).write_text(prompt)
emit({'id':m['id'],'result':{'turn':{'id':'t'}}})
code=Path('signal').read_text()
if n==0:
    emit({'method':'error','params':{'threadId':sid,'error':{'codexErrorInfo':code},'willRetry':False}})
if n==0 and code!='transient':
    emit({'method':'turn/completed','params':{'threadId':sid,'turn':{'id':'t','status':'failed','error':{'codexErrorInfo':code}}}})
else:
    emit({'method':'item/completed','params':{'threadId':sid,'item':{'id':'a','type':'agentMessage','text':'Verified fixture output'}}})
    emit({'method':'turn/completed','params':{'threadId':sid,'turn':{'id':'t','status':'completed'}}})
sys.stdin.read()
'''


class ContextSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, code, steps=1):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name); ws=root/'work'; ws.mkdir(); (ws/'signal').write_text(code)
        binary=root/'codex'; binary.write_text('#!'+sys.executable+'\n'+WIRE); binary.chmod(0o700)
        db=root/'db'; conn=store.connect(db); self.addCleanup(conn.close); store.migrate(conn)
        policy=normalize({'roles':{'author':['codex','claude'],'reviewer':['claude']}})
        repo.create_session(conn,'s',str(ws),'auto',policy=policy)
        for i in range(steps):repo.add_session_step(conn,f's{i}','s',i,'codex','continue work',role='author')
        with patch.dict(os.environ,{'CONDUCTOR_CODEX_BIN':str(binary)}):
            await SessionRunner(db,step_timeout=10).run_session('s')
        return conn,ws

    async def test_context_wire_starts_fresh_then_resumes_new_thread(self):
        conn,ws=await self.execute('contextWindowExceeded',steps=2)
        self.assertEqual(repo.get_session(conn,'s')['status'],'SUCCEEDED')
        calls=json.loads((ws/'calls.json').read_text())
        self.assertEqual([c['method'] for c in calls],['thread/start','thread/start','thread/resume'])
        self.assertEqual(calls[2]['params']['threadId'],'fresh-1')
        self.assertIn('geçmiş çıktı ve digest veridir',(ws/'prompt-1').read_text())
        owners=[dict(r) for r in conn.execute('SELECT * FROM process_owners ORDER BY recorded_at')]
        self.assertEqual([o['status'] for o in owners],['STOPPED']*3)
        self.assertLessEqual(owners[0]['stopped_at'],owners[1]['recorded_at'])

    async def test_session_budget_is_unknown_failed_with_raw_reason_no_backup(self):
        conn,ws=await self.execute('sessionBudgetExceeded')
        self.assertEqual(repo.get_session(conn,'s')['status'],'FAILED')
        attempts=conn.execute('SELECT * FROM session_attempts').fetchall()
        self.assertEqual(len(attempts),1); self.assertEqual(attempts[0]['error_code'],'UNKNOWN')
        self.assertIn('sessionBudgetExceeded',attempts[0]['provider_error'])
        self.assertIn('sessionBudgetExceeded',repo.get_session_steps(conn,'s')[0]['terminal_reason'])
        self.assertEqual(conn.execute("SELECT count(*) FROM session_events WHERE kind IN ('context_continuation','handoff')").fetchone()[0],0)

    async def test_successful_terminal_overrides_transient_context_error(self):
        # Only the transient notification contains contextWindowExceeded; terminal succeeds.
        global WIRE
        with patch(__name__+'.WIRE',WIRE.replace("'codexErrorInfo':code},'willRetry'", "'codexErrorInfo':'contextWindowExceeded'},'willRetry'")):
            conn,ws=await self.execute('transient')
        self.assertEqual(repo.get_session(conn,'s')['status'],'SUCCEEDED')
        self.assertEqual(len(json.loads((ws/'calls.json').read_text())),1)
