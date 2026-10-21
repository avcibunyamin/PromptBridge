"""Opt-in live subscription test, never included in unittest discovery."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import shutil
from contextlib import contextmanager
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import store, runner_store as repo
from app.runner import SessionRunner


@contextmanager
def workspace_for_smoke():
    tmp = tempfile.mkdtemp(prefix='conductor-live-')
    try:
        yield tmp
    except BaseException:
        print('Failure evidence retained:', tmp, flush=True)
        raise
    else:
        shutil.rmtree(tmp)


async def main():
    with workspace_for_smoke() as tmp:
        root = Path(tmp)
        workspace = root / 'workspace'
        workspace.mkdir()
        db = root / 'state.db'
        conn = store.connect(db)
        store.migrate(conn)
        prompts = [
            'Create only hello.txt in the workspace containing exactly CONDUCTOR_RUNNER_OK followed by a newline. '
            'Do not access other folders or network. Report what you wrote.',
            'Read hello.txt using your read-only tools. Verify its content is exactly CONDUCTOR_RUNNER_OK '
            'followed by a newline. Do not modify files. Give a concise review.',
            'Use the review. Verify hello.txt; correct only if needed. Do not create other files. '
            'Finish with a concise report and stop.'
        ]
        with store.transaction(conn):
            repo.create_session(conn, 'live', str(workspace), 'auto')
            for i, (agent, prompt) in enumerate(zip(('codex', 'claude', 'codex'), prompts)):
                repo.add_session_step(conn, f'live_{i}', 'live', i, agent, prompt)
        await SessionRunner(db, step_timeout=150).run_session('live')
        session = repo.get_session(conn, 'live')
        steps = repo.get_session_steps(conn, 'live')
        print('session:', session['status'])
        for step in steps:
            print(json.dumps({key:step[key] for key in ['step_index','agent_name','status',
                'provider_session_id','result_text','terminal_reason']},ensure_ascii=False))
        conn.close()
        assert session['status'] == 'SUCCEEDED'
        assert (workspace / 'hello.txt').read_bytes() == b'CONDUCTOR_RUNNER_OK\n'
        assert steps[0]['provider_session_id'] == steps[2]['provider_session_id']
        assert steps[1]['provider_session_id']
        print('REAL Codex -> Claude -> same Codex: OK; file bytes: OK')


if __name__ == '__main__':
    asyncio.run(main())
