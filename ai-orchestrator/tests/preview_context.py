"""Disposable F2-03b UI fixtures persisted through the real runner/store/API.

No account-backed models. Run: .venv/bin/python tests/preview_context.py
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store, runner_store as repo
from app.api import create_app
from app.runner import SessionRunner
from tests.test_runner import Fake, success
from tests.test_context import context

root=Path(tempfile.mkdtemp(prefix='conductor-context-ui-'))
(root/'.system').mkdir(); db=root/'.system/state.db'
os.environ['AI_ORCHESTRATOR_ROOT']=str(root)
conn=store.connect(db); store.migrate(conn)
for sid in ('limit-fixture','continued-fixture','prepared-fixture'):
    workspace=root/sid; workspace.mkdir()
    repo.create_session(conn,sid,str(workspace),'auto')
    repo.add_session_step(conn,sid+'_step',sid,0,'codex','UI fixture: existing work',role='author')

class StartedFake(Fake):
    async def run(self,*args,**kwargs):
        async for event in super().run(*args,**kwargs):
            if event['type']=='started':event['provider_session_id']=self.result.provider_session_id
            yield event

async def populate():
    await SessionRunner(db,adapter_factory=lambda _:StartedFake(context())).run_session('limit-fixture')
    results=iter([context(),success('Fixture continued, no real model invoked','fresh-ui')])
    await SessionRunner(db,adapter_factory=lambda _:StartedFake(next(results))).run_session('continued-fixture')
asyncio.run(populate())
sid='prepared-fixture'; step=sid+'_step'
repo.claim_session(conn,sid); repo.update_step_status(conn,step,'RUNNING')
repo.begin_attempt(conn,'parent',step,1,'codex','WRITE')
repo.end_attempt(conn,'parent','CONTEXT_EXHAUSTED',context())
owner={'pid':1,'pgid':1,'start_time':1,'boot_time':1}
repo.record_owner(conn,'parent',owner); repo.record_owner(conn,'parent',owner,stopped=True)
repo.prepare_continuation(conn,'not-started','parent',step,'codex','WRITE',{'fixture':True})
conn.close()
print('DISPOSABLE_ROOT='+str(root),flush=True)
uvicorn.run(create_app(),host='127.0.0.1',port=8879)
