"""TB-22: experimental preview sandbox (macOS sandbox-exec). Profile rules, the setting, and a real preview whose
project code tries to read a "secret" outside its folder and to reach the network."""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import preview, user_settings
from app.api import create_app

HAS_SANDBOX = os.path.exists(preview.SANDBOX_EXEC)


class Profile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name).resolve()
        self.home = self.t / 'home'; (self.home / '.nvm').mkdir(parents=True); (self.home / '.ssh').mkdir()

    def test_only_what_a_preview_needs(self):
        p = preview.sandbox_profile(str(self.t / 'ws'), str(self.home), str(self.t / 'tmp'), str(self.t / 'conductor'))
        self.assertTrue(p.startswith('(version 1)\n(deny default)'))
        self.assertIn(f'(subpath "{self.home}/.nvm")', p)                 # an existing toolchain
        self.assertNotIn(f'(subpath "{self.home}/.volta")', p)            # a missing one is not listed
        last = p.strip().splitlines()[-1]
        for secret in ('.ssh', '.netrc', '.npmrc', '.config/gh', 'Library/Keychains'):
            self.assertIn(f'(subpath "{self.home}/{secret}")', last, secret)   # the final rule wins
        self.assertIn(f'(subpath "{self.t}/conductor/.system")', last)
        self.assertIn('(global-name "com.apple.SecurityServer")', p)
        self.assertNotIn('network-outbound', p)                          # no outbound at all, localhost included
        self.assertNotIn('(allow network*', p)
        self.assertIn('(literal "/usr/bin/open")', p)

    def test_paths_are_quoted(self):
        self.assertEqual(preview._sb('/a"b\\c'), '"/a\\"b\\\\c"')


class Setting(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); (self.root / '.system').mkdir()
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        app = create_app(); app.state.runner_db = str(self.root / '.system/state.db')
        self.client = TestClient(app, base_url='http://127.0.0.1:8765')

    def test_off_by_default_and_strict(self):
        self.assertFalse(user_settings.preview_sandbox(self.root / '.system/state.db'))
        self.assertEqual(self.client.put('/api/settings', json={'preview': {'sandbox': 'yes'}}).status_code, 422)
        self.assertEqual(self.client.put('/api/settings', json={'preview': {'sandbox': True}}).json()['settings']['preview'], {'sandbox': True})
        self.assertTrue(user_settings.preview_sandbox(self.root / '.system/state.db'))


@unittest.skipUnless(HAS_SANDBOX, 'sandbox-exec not available')
class RealPreview(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve(); (self.root / '.system').mkdir()
        home = self.root / 'home'; (home / '.ssh').mkdir(parents=True); (home / '.ssh/id_fake').write_text('SECRET')
        (self.root / 'outside.txt').write_text('OUTSIDE')
        port = preview.free_port()
        chooser = patch('app.preview._choose_port', return_value=port); chooser.start(); self.addCleanup(chooser.stop)
        import socket   # stands in for Conductor's API on localhost
        self.listener = socket.socket(); self.listener.bind(('127.0.0.1', 0)); self.listener.listen(); self.addCleanup(self.listener.close)
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root), 'HOME': str(home)}); env.start(); self.addCleanup(env.stop)
        self.app = create_app()
        self.project = self.root / 'project'; self.project.mkdir()
        (self.project / 'app.py').write_text(textwrap.dedent(f"""
            import json, os, urllib.request
            from http.server import BaseHTTPRequestHandler, HTTPServer
            def probe():
                out = {{}}
                for name, path in (('ssh', os.path.expanduser('~/.ssh/id_fake')), ('outside', {str(self.root / 'outside.txt')!r}),
                                   ('own', 'app.py')):
                    try: open(path).read(); out[name] = 'read'
                    except OSError: out[name] = 'denied'
                try: urllib.request.urlopen('http://93.184.216.34/', timeout=3); out['net'] = 'open'
                except Exception: out['net'] = 'denied'
                import socket
                s = socket.socket(); s.settimeout(2)
                try: s.connect(('127.0.0.1', {self.listener.getsockname()[1]})); out['conductor'] = 'open'
                except OSError as e: out['conductor'] = 'denied' if e.errno == 1 else 'closed'
                try:
                    import subprocess; subprocess.run(['/usr/bin/pbpaste'], capture_output=True); out['pbpaste'] = 'ran'
                except OSError: out['pbpaste'] = 'denied'
                return out
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(probe()).encode())
                def log_message(self, *_): pass
            HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
        """))

    def run_preview(self, client, restart=False):
        detected = client.get('/api/previews/detect', params={'workspace_path': str(self.project)}).json()
        body = {'workspace_path': str(self.project), 'command_digest': detected['command_digest']}
        self.assertEqual(client.post('/api/previews/approve', json={**body, 'actor': 'test'}).status_code, 200)
        started = client.post('/api/previews/start', json=body)
        if restart:   # Gemini P3: restart carries the setting too
            started = client.post('/api/previews/restart', json=body)
        self.assertEqual(started.status_code, 200, started.text)
        import urllib.request
        try:
            return json.loads(urllib.request.urlopen(detected['url'], timeout=10).read())
        finally:
            self.assertEqual(client.post('/api/previews/stop', json={'workspace_path': str(self.project)}).json()['server']['status'], 'stopped')

    def test_sandbox_keeps_secrets_and_network_out(self):
        with TestClient(self.app) as client:
            self.assertEqual(self.run_preview(client)['ssh'], 'read')            # control: without the sandbox it leaks
            leaky = self.run_preview(client)
            self.assertEqual((leaky['conductor'], leaky['pbpaste']), ('open', 'ran'))
            self.assertEqual(client.put('/api/settings', json={'preview': {'sandbox': True}}).status_code, 200)
            closed = {'ssh': 'denied', 'outside': 'denied', 'own': 'read', 'net': 'denied', 'conductor': 'denied', 'pbpaste': 'denied'}
            self.assertEqual(self.run_preview(client), closed)
            self.assertEqual(self.run_preview(client, restart=True), closed)


if __name__ == '__main__':
    unittest.main()
