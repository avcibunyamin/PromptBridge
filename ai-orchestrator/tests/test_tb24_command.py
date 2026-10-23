"""TB-24 / K-033: a phone the Mac allowed (`command`) may start a work in a folder a work already uses, send a
message, preview a team and change the theme or default team. Every other control stays on the Mac."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_f303_team import FakeProviders
from tests import test_tb12b_interrupt as tb12b
from tests.test_tb12b_interrupt import HOST, LOGIN
from app import mobile
import tests.fakebin  # noqa: F401  (provider CLIs are stubs, not whatever is installed)

COMMAND = (('POST', '/api/works'), ('POST', '/api/works/w-missing/messages'),
           ('GET', '/api/sessions/providers/preview'), ('GET', '/api/settings'), ('PUT', '/api/settings'))


class Command(unittest.TestCase):
    phone, scopes = tb12b.Interrupt.phone, tb12b.Interrupt.scopes

    def setUp(self):
        tb12b.Interrupt.setUp(self)
        p = patch('app.mobile.notify_mac'); self.notify = p.start(); self.addCleanup(p.stop)   # never a real notification

    def send(self, method, path, **kw):
        return self.gw.request(method, path, headers={'Authorization': f'Bearer {self.token}', 'Tailscale-User-Login': LOGIN,
                                                      'Origin': f'https://{HOST}'}, **kw)

    def folder(self, name):
        p = Path(self.db).parent.parent / name; p.mkdir(); return str(p.resolve())

    def test_new_devices_cannot_give_work(self):   # K-033/2
        for method, path in COMMAND:
            with self.subTest(path=path):
                r = self.phone(method, path)
                self.assertEqual((r.status_code, r.json().get('code')), (403, 'scope_denied'))

    def test_a_phone_with_command_starts_work_only_in_a_known_folder(self):   # K-033/1, /3
        self.app.state.runner.adapter_factory = FakeProviders()            # no real provider is ever started
        known, other = self.folder('known'), self.folder('other')
        self.assertEqual(self.local.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'mac',
                                                             'workspace_path': known}).status_code, 201)
        self.assertEqual(self.scopes(['approve', 'command']).json()['scopes'], ['view', 'approve', 'command'])

        for path in (other, known + '/../other', '/tmp', '/nope/x', known + '/missing'):   # missing or not: same answer
            with self.subTest(path=path):
                r = self.send('POST', '/api/works', json={'client_work_id': str(uuid4()), 'title': 't', 'workspace_path': path})
                self.assertEqual((r.status_code, r.json().get('code')), (403, 'mobile_folder'))

        wid = str(uuid4())
        r = self.send('POST', '/api/works', json={'client_work_id': wid, 'title': 'phone', 'workspace_path': known})
        self.assertEqual(r.status_code, 201, r.text)
        p = self.send('GET', '/api/sessions/providers/preview', params={'author': 'claude', 'reviewer': 'claude'})
        self.assertEqual(p.status_code, 200, p.text)
        r = self.send('POST', f'/api/works/{wid}/messages', json={'client_message_id': str(uuid4()), 'text': 'hi',
                                                                  'policy_digest': p.json()['digest']})
        self.assertEqual(r.status_code, 201, r.text)
        old = self.folder('old'); oid = str(uuid4())                    # an archived work's folder is not offered
        self.local.post('/api/works', json={'client_work_id': oid, 'title': 'old', 'workspace_path': old})
        self.local.patch(f'/api/works/{oid}', json={'expected_revision': 0, 'archived': True})
        r = self.send('POST', '/api/works', json={'client_work_id': str(uuid4()), 'title': 't', 'workspace_path': old})
        self.assertEqual((r.status_code, r.json().get('code')), (403, 'mobile_folder'))
        self.assertEqual([c.args for c in self.notify.call_args_list], [('work',), ('message',)])   # refusals: none
        audit = [tuple(x) for x in self.conn.execute("SELECT method, path, status FROM mobile_audit WHERE method='POST' "
                                                    "AND path LIKE '/api/works%' AND status<300 ORDER BY id")]
        self.assertEqual(audit, [('POST', '/api/works', 201), ('POST', f'/api/works/{wid}/messages', 201)])

    def test_settings_from_a_phone_are_theme_and_team_only(self):   # K-033/4b, TB-37, TB-41
        self.scopes(['command'])
        self.assertEqual(sorted(self.send('GET', '/api/settings').json()['settings']), ['accepted_digest', 'pinned_works', 'team', 'theme', 'version', 'work_teams'])
        self.assertIn('github', self.local.get('/api/settings').json()['settings'])
        self.assertEqual(self.send('PUT', '/api/settings', json={'theme': 'light'}).json()['settings']['theme'], 'light')
        r = self.send('PUT', '/api/settings', json={'team': {'author': ['claude'], 'reviewer': ['claude']}})
        self.assertEqual(r.status_code, 200, r.text)
        for body in ({'github': {'clone_dir': None}}, {'preview': {'sandbox': True}}, {'theme': 'dark', 'preview': {'sandbox': False}}):
            with self.subTest(body=body):
                r = self.send('PUT', '/api/settings', json=body)
                self.assertEqual((r.status_code, r.json()['code']), (403, 'mobile_field'))
        self.assertEqual(self.local.get('/api/settings').json()['settings']['theme'], 'light')   # the refused one wrote nothing
        self.assertEqual(self.local.put('/api/settings', json={'preview': {'sandbox': True}}).status_code, 200)   # the Mac may
        self.notify.assert_not_called()                                                   # settings are not work


class Notice(unittest.TestCase):
    def test_fixed_text_no_shell(self):
        with patch('app.mobile.sys.platform', 'darwin'), patch('app.mobile.subprocess.Popen') as popen:
            mobile.notify_mac('message')
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:2], ['/usr/bin/osascript', '-e'])
        self.assertIn(mobile.NOTICE['message'], argv[2])
        self.assertNotIn('shell', popen.call_args.kwargs)
        with patch('app.mobile.sys.platform', 'darwin'), patch('app.mobile.subprocess.Popen', side_effect=OSError):
            mobile.notify_mac('work')                                                     # never raises


if __name__ == '__main__':
    unittest.main()
