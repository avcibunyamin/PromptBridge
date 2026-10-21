"""F3-06 gate J: /health reports the running commit, the DB schema and the served panel bundle."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from app import api, store


def run_git(head='abc1234\n', head_code=0, status='', status_code=0):
    def fake(argv, **kwargs):
        assert isinstance(argv, list) and 'shell' not in kwargs and kwargs['timeout'] == 2
        if 'rev-parse' in argv:
            return subprocess.CompletedProcess(argv, head_code, head, '')
        return subprocess.CompletedProcess(argv, status_code, status, '')
    return fake


class Commit(unittest.TestCase):
    def test_clean_dirty_and_unavailable(self):
        cases = [(run_git(), 'abc1234'), (run_git(status=' M app/api.py\n'), 'abc1234+dirty'),
                 (run_git(status_code=128), 'abc1234+dirty'), (run_git(head_code=128), 'unknown'),
                 (run_git(head=''), 'unknown')]
        for fake, expected in cases:
            with self.subTest(expected=expected), patch.object(api.subprocess, 'run', side_effect=fake):
                self.assertEqual(api._git_commit(), expected)
        for error in (OSError('no git'), subprocess.TimeoutExpired('git', 2)):
            with self.subTest(error=type(error).__name__), patch.object(api.subprocess, 'run', side_effect=error):
                self.assertEqual(api._git_commit(), 'unknown')

    def test_dirty_check_is_scoped_to_the_code_folder(self):
        seen = []
        with patch.object(api.subprocess, 'run', side_effect=lambda argv, **kw: seen.append(argv) or run_git()(argv, **kw)):
            api._git_commit()
        self.assertEqual(seen[1][-2:], ['--', '.'])
        self.assertEqual(seen[1][2], str(Path(api.__file__).resolve().parent.parent))


class Health(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        self.static = self.root / 'static'; (self.static / 'assets').mkdir(parents=True)

    def health(self):
        with patch.object(api.subprocess, 'run', side_effect=run_git()), \
                TestClient(api.create_app(static_dir=self.static)) as client:
            return client.get('/health').json()

    def test_reports_commit_schema_and_served_bundle(self):
        (self.static / 'index.html').write_text('<script type="module" src="/assets/index-Ab_9-x.js"></script>')
        body = self.health()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['version'], {'commit': 'abc1234', 'schema': store.SCHEMA_VERSION, 'ui_bundle': 'index-Ab_9-x.js'})

    def test_missing_panel_build_is_null_not_an_error(self):
        self.assertIsNone(self.health()['version']['ui_bundle'])


if __name__ == '__main__':
    unittest.main()
