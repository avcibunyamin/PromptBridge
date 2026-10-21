"""TB-23: a run that writes git hooks or config into the workspace's .git is reported (session event git_changed)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import git_guard, store, runner_store as repo, works_store as ws
from app.provider_policy import normalize, digest, DEFAULT
from app.runner import SessionRunner
from tests.test_works import Scripted


class Fingerprint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)

    def test_only_what_git_would_execute(self):
        self.assertIsNone(git_guard.fingerprint(self.ws))                        # not a repo
        subprocess.run(['git', 'init', '-q', str(self.ws)], check=True)
        before = git_guard.fingerprint(self.ws)
        self.assertIn('.git/config', before)
        self.assertFalse([k for k in before if k.endswith('.sample')])           # git's own samples never run
        (self.ws / '.git/hooks/pre-commit').write_text('#!/bin/sh\ncurl evil\n')
        (self.ws / 'src.py').write_text('x')                                     # ordinary files are not this check's job
        self.assertEqual(git_guard.changes(before, git_guard.fingerprint(self.ws)), ['.git/hooks/pre-commit'])
        subprocess.run(['git', '-C', str(self.ws), 'config', 'core.hooksPath', '/tmp/x'], check=True)
        self.assertIn('.git/config', git_guard.changes(before, git_guard.fingerprint(self.ws)))
        with_hook = git_guard.fingerprint(self.ws)
        (self.ws / '.git/hooks/pre-commit').unlink()                             # a removed hook counts too
        self.assertEqual(git_guard.changes(with_hook, git_guard.fingerprint(self.ws)), ['.git/hooks/pre-commit'])

    def test_reviewers_cases(self):   # Gemini TB-23 review: symlinked hooks folder, config.worktree, info/, +x, repo gone
        subprocess.run(['git', 'init', '-q', str(self.ws)], check=True)
        hook = self.ws / '.git/hooks/pre-push'; hook.write_text('#!/bin/sh\n'); hook.chmod(0o644)
        before = git_guard.fingerprint(self.ws)
        hook.chmod(0o755)                                                        # arming an existing hook
        self.assertEqual(git_guard.changes(before, git_guard.fingerprint(self.ws)), ['.git/hooks/pre-push'])
        before = git_guard.fingerprint(self.ws)
        (self.ws / '.git/config.worktree').write_text('[core]\n\thooksPath = /tmp/x\n')
        (self.ws / '.git/info/attributes').write_text('* filter=x\n')
        self.assertEqual(git_guard.changes(before, git_guard.fingerprint(self.ws)), ['.git/config.worktree', '.git/info/attributes'])
        before = git_guard.fingerprint(self.ws)
        elsewhere = self.ws / 'elsewhere'; elsewhere.mkdir()
        import shutil; shutil.rmtree(self.ws / '.git/hooks'); (self.ws / '.git/hooks').symlink_to(elsewhere)
        self.assertIn('.git/hooks', git_guard.changes(before, git_guard.fingerprint(self.ws)))
        self.assertEqual(git_guard.changes(before, None), ['.git'])            # the repo vanished

    def test_argon_round_two(self):   # unreadable, FIFO, submodule hooks, symlinked info/, alternates
        subprocess.run(['git', 'init', '-q', str(self.ws)], check=True)
        before = git_guard.fingerprint(self.ws)
        (self.ws / '.git/info/exclude').chmod(0)                                 # must not blind the check
        import os
        os.mkfifo(self.ws / '.git/hooks/pre-commit')                             # must not hang it
        sub = self.ws / '.git/modules/lib/hooks'; sub.mkdir(parents=True); (sub / 'post-checkout').write_text('x')
        (self.ws / '.git/objects/info').mkdir(parents=True, exist_ok=True); (self.ws / '.git/objects/info/alternates').write_text('/elsewhere/objects\n')
        after = git_guard.fingerprint(self.ws)
        self.assertEqual(git_guard.changes(before, after), ['.git/hooks/pre-commit', '.git/info/exclude',
                                                             '.git/modules/lib/hooks/post-checkout', '.git/objects/info/alternates'])
        self.assertTrue(after['.git/info/exclude'].startswith('err:') and after['.git/hooks/pre-commit'].startswith('special:'))
        (self.ws / '.git/info/exclude').chmod(0o644)
        import shutil; shutil.rmtree(self.ws / '.git/info'); (self.ws / '.git/info').symlink_to(self.ws)
        self.assertIn('.git/info', git_guard.changes(after, git_guard.fingerprint(self.ws)))

    def test_repo_appearing_or_a_git_file(self):
        self.assertEqual(git_guard.changes(None, None), [])
        self.assertEqual(git_guard.changes(None, {'.git/config': 'x'}), ['.git'])
        (self.ws / '.git').write_text('gitdir: /elsewhere\n')
        self.assertEqual(list(git_guard.fingerprint(self.ws)), ['.git'])

    def test_worktrees_logs_pruned_before_nested_limit(self):
        for i in range(60):
            wt = self.ws / f'.git/worktrees/wt-{i:02d}'
            logs = wt / 'logs'
            logs.mkdir(parents=True)
            for j in range(20):
                (logs / f'f{j}').write_text('log\n')
            (wt / 'config.worktree').write_text('initial\n')
        before = git_guard.fingerprint(self.ws)
        self.assertNotIn('too_many_entries', before.values())
        (self.ws / '.git/worktrees/wt-59/config.worktree').write_text('changed\n')
        after = git_guard.fingerprint(self.ws)
        self.assertNotIn('too_many_entries', after.values())
        self.assertEqual(git_guard.changes(before, after), ['.git/worktrees/wt-59/config.worktree'])


class RunReportsIt(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db, self.work = root / 'state.db', root / 'ws'
        self.work.mkdir(); subprocess.run(['git', 'init', '-q', str(self.work)], check=True)
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'demo', str(self.work))
        self.policy = normalize(DEFAULT)

    async def run_once(self, write_hook):
        log, scripts = [], {}
        scripted = Scripted(log, scripts)
        work = self.work

        def factory(provider):
            adapter = scripted(provider)
            original = adapter.run
            async def run(prompt, workspace, provider_session_id=None, *, profile):
                if write_hook and profile == 'WRITE':   # what an agent with file tools could do
                    (work / '.git/hooks/post-checkout').write_text('#!/bin/sh\necho owned\n')
                async for event in original(prompt, workspace, provider_session_id, profile=profile):
                    yield event
            adapter.run = run
            return adapter
        ws.add_message(self.conn, self.wid, str(uuid4()), 'task', digest(self.policy), self.policy, digest(self.policy))
        sid, reason = ws.dispatch_next(self.conn, self.wid)
        self.assertTrue(sid, reason)
        await SessionRunner(self.db, adapter_factory=factory).run_session(sid)
        return sid, [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM session_events WHERE session_id=? AND kind='git_changed'", (sid,))]

    async def test_a_written_hook_is_reported_and_a_clean_run_is_not(self):
        sid, events = await self.run_once(write_hook=False)
        self.assertEqual((repo.get_session(self.conn, sid)['status'], events), ('SUCCEEDED', []))
        sid, events = await self.run_once(write_hook=True)
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'SUCCEEDED')   # reported, not blocked
        self.assertEqual(events, [{'paths': ['.git/hooks/post-checkout']}])
        from unittest.mock import patch
        calls = {'n': 0}
        real = git_guard.fingerprint
        def flaky(workspace):   # the end-of-run check itself fails: the run must say so, not stay silent
            calls['n'] += 1
            if calls['n'] == 2:
                raise PermissionError('nope')
            return real(workspace)
        with patch.object(git_guard, 'fingerprint', flaky):
            sid3, events3 = await self.run_once(write_hook=False)
        self.assertEqual(events3, [{'paths': ['.git (denetlenemedi: PermissionError)']}])
        self.assertEqual(repo.get_session(self.conn, sid3)['status'], 'SUCCEEDED')
        runs = [m['session'] for m in ws.list_messages(self.conn, self.wid)['items']]   # what the run card reads
        self.assertEqual([r['git_changed'] for r in runs], [[], ['.git/hooks/post-checkout'], ['.git (denetlenemedi: PermissionError)']])


if __name__ == '__main__':
    unittest.main()
