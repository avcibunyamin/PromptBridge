"""TB-79: `python -m app doctor` installation check (no provider calls, no network)."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import cli, doctor
from app.provider_policy import CAPABILITIES

EXAMPLE = Path(__file__).resolve().parent.parent / 'providers.example.yaml'


def _no_server(*args, **kwargs):
    raise OSError('connection refused')


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch('urllib.request.urlopen', side_effect=_no_server)
        self.urlopen = p.start()
        self.addCleanup(p.stop)

    def _install(self, value=True):
        p = mock.patch('app.session_api.installed_providers',
                       return_value={name: value for name in CAPABILITIES})
        p.start()
        self.addCleanup(p.stop)

    def _ready_root(self):
        (self.root / '.system').mkdir()
        shutil.copy(EXAMPLE, self.root / '.system' / 'providers.yaml')
        (self.root / 'app' / 'static').mkdir(parents=True)
        (self.root / 'app' / 'static' / 'index.html').write_text('<html></html>')

    def test_missing_providers_yaml_fails(self):
        self._install()
        lines = []
        self.assertEqual(doctor.run(self.root, out=lines.append), 1)
        fail = [l for l in lines if l.startswith('FAIL providers.yaml')]
        self.assertEqual(len(fail), 1, lines)
        self.assertIn('copy providers.example.yaml or a preset to .system/providers.yaml', fail[0])

    def test_ready_install_has_no_fail(self):
        self._install()
        self._ready_root()
        lines = []
        self.assertEqual(doctor.run(self.root, out=lines.append), 0, lines)
        self.assertFalse([l for l in lines if l.startswith('FAIL')], lines)
        self.assertTrue(any(l.startswith('OK   providers.yaml') and 'author:' in l
                            and 'reviewer:' in l for l in lines), lines)
        self.assertTrue(any(l.startswith('OK   codex CLI') for l in lines), lines)
        self.assertTrue(any(l.startswith('OK   claude CLI') for l in lines), lines)
        self.assertTrue(any(l.startswith('WARN server') and 'python -m app serve' in l
                            for l in lines), lines)
        self.assertIn('doctor does not call providers', lines[-1])
        self.urlopen.assert_called_once()
        self.assertEqual(self.urlopen.call_args.args[0], 'http://127.0.0.1:8765/health')

    def test_server_running_is_ok(self):
        self._install()
        self._ready_root()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"commit": "abc123", "schema": 9}'
        self.urlopen.side_effect = None
        self.urlopen.return_value = resp
        lines = []
        self.assertEqual(doctor.run(self.root, out=lines.append), 0, lines)
        self.assertTrue(any(l.startswith('OK   server') and 'abc123' in l and '9' in l
                            for l in lines), lines)

    def test_missing_cli_and_panel_fail(self):
        self._install(False)
        (self.root / '.system').mkdir()
        shutil.copy(EXAMPLE, self.root / '.system' / 'providers.yaml')
        lines = []
        self.assertEqual(doctor.run(self.root, out=lines.append), 1)
        self.assertTrue(any(l.startswith('FAIL codex CLI') for l in lines), lines)
        self.assertTrue(any(l.startswith('FAIL panel')
                            and 'cd ui && npm install && npm run build && cd ..' in l
                            for l in lines), lines)

    def test_invalid_providers_yaml_fails(self):
        self._install()
        (self.root / '.system').mkdir()
        (self.root / '.system' / 'providers.yaml').write_text('roles: [unclosed\n')
        lines = []
        self.assertEqual(doctor.run(self.root, out=lines.append), 1)
        self.assertTrue(any(l.startswith('FAIL providers.yaml') and 'invalid' in l for l in lines), lines)

    def test_cli_subcommand(self):
        self._install()
        self._ready_root()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(['--root', str(self.root), 'doctor']), 0)
        self.assertIn('OK   providers.yaml', buf.getvalue())
        self.assertIn('doctor', cli._COMMANDS)

    def test_node_version_check(self):
        self._install()
        self._ready_root()
        with mock.patch('shutil.which', return_value='/usr/bin/node'):
            old_proc = subprocess.CompletedProcess(['/usr/bin/node', '--version'], 0, stdout='v20.11.0\n', stderr='')
            with mock.patch('subprocess.run', return_value=old_proc) as run_mock:
                lines = []
                self.assertEqual(doctor.run(self.root, out=lines.append), 0)
                run_mock.assert_called_once_with(['/usr/bin/node', '--version'],
                                                 capture_output=True, text=True, timeout=5)
                self.assertTrue(any(
                    l == 'WARN node — node v20.11.0 found; 22.6+ is needed to build the panel or run UI tests'
                    for l in lines
                ), lines)

            new_proc = subprocess.CompletedProcess(['/usr/bin/node', '--version'], 0, stdout='v22.6.0\n', stderr='')
            with mock.patch('subprocess.run', return_value=new_proc):
                lines = []
                self.assertEqual(doctor.run(self.root, out=lines.append), 0)
                self.assertTrue(any(l.startswith('OK   node') for l in lines), lines)

    def test_checks_returns_structured_results_without_server_probe(self):
        self._install()
        self._ready_root()
        results = doctor.checks(self.root, server=False)
        self.assertTrue(results)
        self.assertTrue(all(set(item) == {'key', 'level', 'detail'} for item in results))
        self.assertFalse(any(item['key'] == 'server' for item in results))

    def test_setup_checks_endpoint_and_mobile_gate(self):
        from app.api import create_app

        expected = [{'key': 'python', 'level': 'OK', 'detail': '3.12.0'}]
        app = create_app(static_dir=self.root)
        with mock.patch('app.doctor.checks', return_value=expected) as check_mock:
            client = TestClient(app)
            response = client.get('/api/setup/checks')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'checks': expected, 'ok': True})
            check_mock.assert_called_once_with(mock.ANY, server=False)

            class MobileState:
                def __init__(self, inner):
                    self.inner = inner

                async def __call__(self, scope, receive, send):
                    if scope['type'] == 'http':
                        scope.setdefault('state', {})['conductor_mobile_device'] = 'phone'
                    await self.inner(scope, receive, send)

            mobile_client = TestClient(MobileState(app))
            self.assertEqual(mobile_client.get('/api/setup/checks').status_code, 404)
            self.assertEqual(mobile_client.get('/api/host').status_code, 404)   # K-059: who/where stays on the Mac
        host = TestClient(app).get('/api/host').json()
        self.assertEqual(set(host), {'user', 'login', 'computer', 'system'})
        self.assertTrue(host['user'] and host['login'] and host['computer'])

    def test_serve_loopback_host_only(self):
        from app import quota
        old_root = os.environ.get('AI_ORCHESTRATOR_ROOT')
        old_auto = quota.AUTO

        def _restore():
            quota.AUTO = old_auto
            if old_root is None:
                os.environ.pop('AI_ORCHESTRATOR_ROOT', None)
            else:
                os.environ['AI_ORCHESTRATOR_ROOT'] = old_root

        self.addCleanup(_restore)
        (self.root / 'app' / 'static').mkdir(parents=True)
        with mock.patch('uvicorn.run') as run_mock, \
             mock.patch('app.cli.run_watcher'), \
             mock.patch('app.catalog.refresh'):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(['--root', str(self.root), 'serve', '--host', '0.0.0.0'])
            self.assertEqual(rc, 2)
            self.assertIn('Conductor listens on loopback only; use 127.0.0.1, ::1 or localhost',
                          err.getvalue())
            run_mock.assert_not_called()

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(['--root', str(self.root), 'serve', '--host', '127.0.0.1'])
            self.assertEqual(rc, 0)
            run_mock.assert_called_once()
            self.assertEqual(run_mock.call_args.kwargs.get('host'), '127.0.0.1')
            self.assertNotIn('Panel →', out.getvalue())


if __name__ == '__main__':
    unittest.main()
