"""TB-28: the lint/type check is a second approved command, run like the tests (TB-27): sandboxed, before the
reviewer, its result in its own prompt section. Approved separately; a project tool missing says so."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import store, test_runner, works_store as ws
from app.provider_policy import normalize, digest, DEFAULT
from app.runner import SessionRunner
from tests.test_works import Scripted
from tests.test_tb27_tests import project

MAC = sys.platform == 'darwin'


def fake_tsc(root: Path, code: int, text: str) -> Path:
    """A project-local `tsc` (what `npm install typescript` would put there) that prints a fixed result."""
    (root / 'tsconfig.json').write_text('{}')
    tsc = root / 'node_modules' / '.bin' / 'tsc'
    tsc.parent.mkdir(parents=True, exist_ok=True)
    tsc.write_text(f'#!/bin/sh\necho "{text}"\nexit {code}\n'); tsc.chmod(0o755)
    return root


class Detect(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root = Path(tmp.name)

    def test_suggestions(self):
        self.assertIsNone(test_runner.detect(str(self.root), 'lint'))
        with patch('app.preview._which', return_value='/usr/bin/npm'):
            (self.root / 'package.json').write_text(json.dumps({'scripts': {'typecheck': 'tsc --noEmit'}}))
            self.assertEqual(test_runner.detect(str(self.root), 'lint')['label'], 'npm run typecheck --silent')
            (self.root / 'package.json').write_text(json.dumps({'scripts': {'lint': 'eslint .', 'typecheck': 'x'}}))
            self.assertEqual(test_runner.detect(str(self.root), 'lint')['label'], 'npm run lint --silent')   # lint first
        (self.root / 'package.json').unlink()
        (self.root / 'tsconfig.json').write_text('{}')
        s = test_runner.detect(str(self.root), 'lint')
        self.assertEqual((s['label'], s['needs']), ('tsc --noEmit', ['node_modules/.bin/tsc']))
        (self.root / 'tsconfig.json').unlink()
        (self.root / 'pyproject.toml').write_text('[tool.ruff]\nline-length = 100\n')
        s = test_runner.detect(str(self.root), 'lint')
        self.assertEqual((s['label'], s['needs'], s['script_path']), ('ruff check .', ['.venv/bin/ruff'], 'pyproject.toml'))
        self.assertIsNone(test_runner.detect(str(self.root), 'tests'))                 # tests and lint are separate
        (self.root / 'pyproject.toml').unlink(); (self.root / 'setup.cfg').write_text('[flake8]\nmax-line-length = 100\n')
        self.assertEqual(test_runner.detect(str(self.root), 'lint')['label'], 'flake8 .')

    def test_lint_markers_do_not_touch_the_test_approval(self):
        project(self.root, '    def test_ok(self): pass\n')
        t = test_runner.detect(str(self.root))['command_digest']
        (self.root / 'tsconfig.json').write_text('{}')
        self.assertEqual(test_runner.detect(str(self.root))['command_digest'], t)


class Approvals(unittest.TestCase):
    def test_separate_approvals(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); root = Path(tmp.name)
        conn = store.connect(root / 'state.db'); self.addCleanup(conn.close); store.migrate(conn)
        p = str(fake_tsc(project(root / 'p', '    def test_ok(self): pass\n'), 0, 'ok'))
        test_runner.approve(conn, p, test_runner.detect(p)['command_digest'])
        self.assertIsNone(test_runner.plan(conn, p, 'lint'))                            # tests approved, lint not
        with self.assertRaises(test_runner.preview.PreviewError):                       # a test digest is no lint digest
            test_runner.approve(conn, p, test_runner.detect(p)['command_digest'], 'lint')
        test_runner.approve(conn, p, test_runner.detect(p, 'lint')['command_digest'], 'lint')
        self.assertEqual(test_runner.plan(conn, p, 'lint')['status'], 'ready')
        test_runner.revoke(conn, p, 'lint')
        self.assertIsNone(test_runner.plan(conn, p, 'lint'))
        self.assertEqual(test_runner.plan(conn, p)['status'], 'ready')                  # the test approval stays
        (Path(p) / 'node_modules/.bin/tsc').unlink()
        test_runner.approve(conn, p, test_runner.detect(p, 'lint')['command_digest'], 'lint')
        self.assertEqual(test_runner.plan(conn, p, 'lint')['reason'], 'deps_missing')   # e.g. an isolated copy


class Api(unittest.TestCase):
    def test_lint_through_the_api_and_an_isolated_copy(self):
        import os, subprocess
        from fastapi.testclient import TestClient
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); root = Path(tmp.name).resolve()
        (root / '.system').mkdir()
        (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        src = fake_tsc(project(root / 'src', '    def test_ok(self): pass\n'), 0, 'ok')
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't',
               'GIT_AUTHOR_EMAIL': 't@e', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@e'}
        (src / '.gitignore').write_text('node_modules/\n')
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'x')):
            subprocess.run(['git', '-C', str(src), *args], check=True, env=env, capture_output=True)
        p = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root), 'CONDUCTOR_WORKTREES': str(root / 'copies'),
                                    'GIT_CONFIG_GLOBAL': '/dev/null'})
        p.start(); self.addCleanup(p.stop)
        from app.api import create_app
        c = TestClient(create_app(), base_url='http://127.0.0.1:8765'); c.__enter__(); self.addCleanup(c.__exit__, None, None, None)
        sw = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 's', 'workspace_path': str(src)}).json()['id']
        st = c.get(f'/api/tests/works/{sw}?check=lint').json()
        self.assertEqual((st['suggestion']['label'], st['approved']), ('tsc --noEmit', False))
        r = c.post(f'/api/tests/works/{sw}/approve', json={'command_digest': st['suggestion']['command_digest'], 'check': 'lint'})
        self.assertTrue(r.json()['approved'])
        self.assertFalse(c.get(f'/api/tests/works/{sw}').json()['approved'])           # the tests stay unapproved
        cw = c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'c', 'workspace_path': str(src),
                                         'isolate': True}).json()
        conn = store.connect(root / '.system/state.db'); self.addCleanup(conn.close)
        self.assertEqual(test_runner.plan(conn, cw['workspace_path'], 'lint'),             # the source's lint approval…
                         {'status': 'not_run', 'reason': 'deps_missing', 'missing': ['node_modules/.bin/tsc'],
                          'command': 'tsc --noEmit'})                                    # …and no node_modules in the copy
        self.assertEqual(c.post(f'/api/tests/works/{sw}/revoke?check=lint').json()['approved'], False)
        self.assertEqual(c.post(f'/api/tests/works/{sw}/approve', headers={'Origin': 'https://evil.example'},
                                json={'command_digest': '0' * 64, 'check': 'lint'}).status_code, 403)


@unittest.skipUnless(MAC, 'sandbox-exec is macOS only')
class Runner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); root = Path(tmp.name).resolve()
        self.db = root / 'state.db'
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.ws = str(fake_tsc(project(root / 'p', '    def test_ok(self): pass\n'), 2,
                               "src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'."))
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'demo', self.ws)
        self.log = []; self.factory = Scripted(self.log, {})
        self.policy = normalize(DEFAULT)

    async def run_message(self):
        ws.add_message(self.conn, self.wid, str(uuid4()), 'fix it', digest(self.policy), self.policy, digest(self.policy))
        sid, _ = ws.dispatch_next(self.conn, self.wid)
        start = len(self.log)
        await SessionRunner(self.db, adapter_factory=self.factory).run_session(sid)
        return sid, self.log[start:]

    async def test_lint_runs_sandboxed_and_reaches_only_the_reviewer(self):
        test_runner.approve(self.conn, self.ws, test_runner.detect(self.ws, 'lint')['command_digest'], 'lint')
        sid, calls = await self.run_message()
        reviewer = [c['prompt'] for c in calls if c['profile'] == 'READ_ONLY']
        self.assertEqual(len(reviewer), 1)
        self.assertIn('LINT/TYPE CHECK RUN BY CONDUCTOR', reviewer[0]); self.assertIn('error TS2322', reviewer[0])
        self.assertNotIn('TESTS RUN BY CONDUCTOR', reviewer[0])                        # tests not approved
        self.assertFalse(any('LINT/TYPE CHECK' in c['prompt'] for c in calls if c['profile'] == 'WRITE'))
        ran = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM session_events WHERE session_id=? AND kind='lint_ran'", (sid,))]
        self.assertEqual([(r['status'], r['exit_code']) for r in ran], [('failed', 2)])
        view = ws.list_messages(self.conn, self.wid)['items'][-1]['session']
        self.assertEqual((view['lint']['status'], view['tests']), ('failed', None))
        test_runner.approve(self.conn, self.ws, test_runner.detect(self.ws)['command_digest'])
        _, calls = await self.run_message()
        reviewer = [c['prompt'] for c in calls if c['profile'] == 'READ_ONLY'][0]
        self.assertLess(reviewer.index('TESTS RUN BY CONDUCTOR'), reviewer.index('LINT/TYPE CHECK RUN BY CONDUCTOR'))


if __name__ == '__main__':
    unittest.main()
