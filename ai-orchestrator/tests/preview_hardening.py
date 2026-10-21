"""Disposable UI read-failure fixture; no production data/provider calls."""
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from fastapi.responses import JSONResponse
from app import store
from app.api import create_app
from app.transitions import transition

root=Path(tempfile.mkdtemp(prefix='conductor-f204-ui-'))
(root/'.system').mkdir()
os.environ['AI_ORCHESTRATOR_ROOT']=str(root)
conn=store.connect(root/'.system/state.db'); store.migrate(conn)
eid,_=store.insert_captured(conn,{'id':'failure-fixture','title':'F2-04 okuma hatası testi','source_agent':'chatgpt','target_agent':'claude'},b'fixture')
transition(conn,eid,'enqueue',expected_state='CAPTURED',actor='test',payload={}); conn.close()
app=create_app()

@app.middleware('http')
async def inject_content_error(request,call_next):
    if request.url.path.endswith('/content'):
        return JSONResponse({'detail':'F2-04 kontrollü okuma hatası','code':'internal_error'},status_code=500)
    return await call_next(request)

uvicorn.run(app,host='127.0.0.1',port=8879)
