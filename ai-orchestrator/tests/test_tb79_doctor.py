"""TB-79: `python -m app doctor` installation check (no provider calls, no network)."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertTrue(any(l.startswith('FAIL panel') and 'npm run build' in l for l in lines), lines)

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


if __name__ == '__main__':
    unittest.main()
