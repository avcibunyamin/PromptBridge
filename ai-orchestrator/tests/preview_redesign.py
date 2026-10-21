"""Disposable UI smoke server. Fake providers; never runs account-backed models.
Run from ai-orchestrator: .venv/bin/python tests/preview_redesign.py
"""
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from app import store
from app.api import create_app
from app.runner import SessionRunner
from app.adapters.base import AdapterResult
from app.transitions import transition
from tests.test_runner import Fake, success

root = Path(tempfile.mkdtemp(prefix='conductor-redesign-'))
(root/'.system').mkdir()
(root/'ui-smoke-work').mkdir()
(root/'agents/claude/inbox').mkdir(parents=True)
(root/'.system/providers.yaml').write_text('roles:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
os.environ['AI_ORCHESTRATOR_ROOT'] = str(root)
conn = store.connect(root/'.system/state.db'); store.migrate(conn)
for i in range(2):
    eid, _ = store.insert_captured(conn, {'id':f'ui_smoke_{i}', 'schema_version':'1.0', 'source_agent':'chatgpt', 'target_agent':'claude', 'title':f'UI testi {i+1}'}, b'# UI smoke\n\nOnly temporary test data.\n')
    transition(conn,eid,'enqueue',expected_state='CAPTURED',actor='test',payload={})
conn.close()
original = SessionRunner.__init__
class PreviewAdapter(Fake):
    async def run(self, prompt, workspace, provider_session_id=None, *, profile):
        if 'slow' in prompt:self.delay=30
        yield {'type':'output_delta','text':'Yalıtılmış UI test çıktısı. '}
        yield {'type':'output_delta','text':'Bu sağlayıcı simülasyondur.'}
        yield {'type':'command_result','command':'fixture-check --ui','exit_code':0,'output':'Simulated command result for UI rendering only.'}
        async for event in super().run(prompt,workspace,provider_session_id,profile=profile):yield event

def initialize(self,*args,**kwargs):
    original(self,*args,**kwargs)
    calls=[]
    def factory(provider):
        calls.append(provider)
        result=AdapterResult('Fixture quota',None,None,'failed',True,'quota',1,'QUOTA_EXCEEDED') if len(calls)==1 else success('UI fixture tamamlandı.\nGerçek model çağrısı yapılmadı.')
        return PreviewAdapter(result,delay=1)
    self.adapter_factory=factory
SessionRunner.__init__=initialize
print('WORKSPACE='+str(root/'ui-smoke-work'),flush=True)
uvicorn.run(create_app(),host='127.0.0.1',port=8879)
