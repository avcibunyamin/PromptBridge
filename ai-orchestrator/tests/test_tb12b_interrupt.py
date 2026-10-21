"""TB-12b / K-027: a phone may stop a run, cancel a quota wait and take a queued message back — only with the
`interrupt` scope, which only the Mac grants, per device. Everything else that controls work stays Mac-only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import mobile, store

HOST, LOGIN = 'mac.tail1234.ts.net', 'me@example.com'
INTERRUPT = (('POST', '/api/sessions/s-missing/cancel'), ('DELETE', '/api/sessions/s-missing/wait'),
             ('POST', '/api/works/w-missing/messages/m-missing/cancel'))
STILL_MAC_ONLY = (('POST', '/api/sessions/s/retry'), ('POST', '/api/sessions/s/resume'), ('POST', '/api/sessions/s/pause'),
                  ('POST', '/api/sessions/s/recover'), ('POST', '/api/works/w/continue'),
                  ('POST', '/api/previews/start'), ('POST', '/api/github/clone'),
                  ('PUT', '/api/mobile/devices/d/scopes'))   # K-033: works, messages, settings → `command`


class Interrupt(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); (root / '.system').mkdir()
        static = root / 'static'; (static / 'assets').mkdir(parents=True); (static / 'index.html').write_text('<html></html>')
        (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        p = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root)}); p.start(); self.addCleanup(p.stop)
        from app.api import create_app
        self.db = str(root / '.system/state.db')
        self.app = create_app(static_dir=static)
        self.local = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.local.__enter__(); self.addCleanup(self.local.__exit__, None, None, None)
        self.app.state.runner_db = self.db
        self.gw = TestClient(mobile.MobileGuard(self.app, self.db, HOST), base_url=f'https://{HOST}')
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close)
        code = self.local.post('/api/mobile/pairing').json()['code']
        paired = self.gw.post('/api/mobile/pair', json={'code': code, 'device_name': 'Pixel'}, headers={'Tailscale-User-Login': LOGIN}).json()
        self.token, self.device = paired['token'], paired['device_id']

    def phone(self, method, path):
        return self.gw.request(method, path, headers={'Authorization': f'Bearer {self.token}', 'Tailscale-User-Login': LOGIN,
                                                      'Origin': f'https://{HOST}'})

    def scopes(self, scopes, **kw):
        return self.local.put(f'/api/mobile/devices/{self.device}/scopes', json={'scopes': scopes}, **kw)

    def test_new_devices_cannot_interrupt(self):   # K-027/1
        for method, path in INTERRUPT:
            with self.subTest(path=path):
                r = self.phone(method, path)
                self.assertEqual((r.status_code, r.json().get('code')), (403, 'scope_denied'))

    def test_the_mac_grants_interrupt_and_takes_it_back(self):   # K-027/2, /4
        r = self.scopes(['approve', 'interrupt'])
        self.assertEqual(r.json()['scopes'], ['view', 'approve', 'interrupt'])          # view always stays
        self.assertEqual(self.phone('GET', '/api/mobile/me').json()['scopes'], ['view', 'approve', 'interrupt'])
        for method, path in INTERRUPT:
            with self.subTest(path=path):
                r = self.phone(method, path)                                            # reached the app: no such item
                self.assertEqual((r.status_code, r.json()['detail']), (404, 'work not found' if '/works/' in path else 'session not found'))
        self.assertEqual(self.scopes(['view']).json()['scopes'], ['view'])
        for method, path in INTERRUPT:
            self.assertEqual(self.phone(method, path).status_code, 403)
        outcomes = [r[0] for r in self.conn.execute("SELECT outcome FROM mobile_audit WHERE method='PUT' ORDER BY id")]
        self.assertEqual(outcomes, ['scopes:view approve interrupt', 'scopes:view'])

    def test_everything_else_stays_on_the_mac(self):   # K-027/3
        self.scopes(['approve', 'interrupt'])
        for method, path in STILL_MAC_ONLY:
            with self.subTest(path=path):
                r = self.phone(method, path)
                self.assertEqual((r.status_code, r.json()['detail']), (404, 'not_found'))   # the gateway's own 404

    def test_scope_changes_are_checked_and_mac_only(self):
        self.assertEqual(self.scopes(['admin']).status_code, 422)
        self.assertEqual(self.scopes(['interrupt'], headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.local.put('/api/mobile/devices/nope/scopes', json={'scopes': ['view']}).status_code, 404)
        self.local.delete(f'/api/mobile/devices/{self.device}')
        self.assertEqual(self.scopes(['interrupt']).status_code, 404)                  # a revoked device stays revoked


if __name__ == '__main__':
    unittest.main()
