"""K-041: the Finder folder dialog for the browser panel — fixed argv, cancel, one at a time, Mac only."""
import os
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import folder_picker, mobile


def done(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, out, err)


class FolderPicker(unittest.TestCase):
    def test_runs_osascript_without_a_shell_and_returns_the_folder(self):
        folder = tempfile.mkdtemp()
        with patch('subprocess.run', return_value=done(out=folder + '/\n')) as run:
            self.assertEqual(folder_picker.choose(), (folder, None))
        args, kw = run.call_args
        self.assertEqual(args[0][0], '/usr/bin/osascript')
        self.assertEqual(args[0][1::2], ['-e'] * len(folder_picker.SCRIPT))
        self.assertTrue(any('choose folder' in a for a in args[0]))
        self.assertTrue(kw['start_new_session'])
        self.assertNotIn('shell', kw)
        self.assertEqual(kw['timeout'], folder_picker.TIMEOUT)

    def test_cancel_timeout_and_nonsense(self):
        with patch('subprocess.run', return_value=done(1, err='execution error: User canceled. (-128)')):
            self.assertEqual(folder_picker.choose(), (None, 'cancelled'))
        with patch('subprocess.run', return_value=done(1, err='execution error: AppleEvent timed out. (-1712)')):
            self.assertEqual(folder_picker.choose(), (None, 'timeout'))
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('osascript', 1)):
            self.assertEqual(folder_picker.choose(), (None, 'timeout'))
        with patch('subprocess.run', return_value=done(out='relative/path\n')):
            self.assertEqual(folder_picker.choose(), (None, 'unavailable'))
        with patch('subprocess.run', return_value=done(out='/no/such/folder\n')):
            self.assertEqual(folder_picker.choose(), (None, 'unavailable'))

    def test_one_dialog_at_a_time(self):
        gate, started = threading.Event(), threading.Event()
        def slow(*a, **k):
            started.set(); gate.wait(5); return done(1, err='(-128)')
        with patch('subprocess.run', side_effect=slow):
            t = threading.Thread(target=folder_picker.choose); t.start(); started.wait(5)
            self.assertEqual(folder_picker.choose(), (None, 'busy'))
            gate.set(); t.join(5)

    def test_api_is_guarded_and_not_on_the_phone(self):
        from app.api import create_app
        root = tempfile.mkdtemp(); os.makedirs(os.path.join(root, '.system'))
        with patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': root}):
            client = TestClient(create_app())
            self.assertEqual(client.post('/api/system/choose-folder', headers={'Origin': 'https://evil.example'}).status_code, 403)
            with patch('subprocess.run', return_value=done(1, err='(-128)')):
                self.assertEqual(client.post('/api/system/choose-folder').json(), {'path': None, 'cancelled': True, 'error': None})
        self.assertFalse(any(p.fullmatch('/api/system/choose-folder') for _, p, _ in mobile._ALLOW))


if __name__ == '__main__':
    unittest.main()
