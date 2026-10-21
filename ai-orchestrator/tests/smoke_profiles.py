"""Opt-in real Claude WRITE and Codex READ_ONLY profiles, temporary files only."""
import asyncio
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter

async def collect(adapter,prompt,workspace,profile):
    events=[]
    async with asyncio.timeout(150):
        async for e in adapter.run(prompt,str(workspace),profile=profile):events.append(e)
    result=events[-1]['result']
    print(profile, type(adapter).__name__,result.terminal_reason,result.error_code,result.final_text,flush=True)
    return result,events

async def main():
    with tempfile.TemporaryDirectory(prefix='conductor-profiles-') as tmp:
        root=Path(tmp)
        result,_=await collect(ClaudeAdapter(),'Use Write to create only author.txt containing PROFILE_OK followed by a newline. Do not use other folders.',root,'WRITE')
        assert result.successful
        assert (root/'author.txt').read_bytes()==b'PROFILE_OK\n'
        before=(root/'author.txt').read_bytes()
        result,events=await collect(CodexAdapter(),
            'This is a sandbox verification. Use your shell tool to attempt to create forbidden.txt with printf. '
            'It should be denied by read-only sandbox. Do not seek extra permission or use alternate tools. '
            'Then read author.txt and report the denial and content.',root,'READ_ONLY')
        assert not (root/'forbidden.txt').exists()
        assert (root/'author.txt').read_bytes()==before
        commands=[e for e in events if e['type']=='command_result']
        assert result.successful
        print('Structured command evidence:', commands, flush=True)
        print('Verified bytes unchanged and forbidden file absent; model denial text alone is not sandbox proof.',flush=True)
        print('REAL Claude author WRITE + Codex reviewer READ_ONLY: OK',flush=True)
asyncio.run(main())
