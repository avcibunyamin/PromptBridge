"""TB-39: provider account management endpoints (/api/accounts) and safe providers.yaml updates."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import mobile, quota, store
from app.adapters.agy import agy_home
from app.adapters.codex import codex_home
from app.provider_policy import MAX_ALLOWED, allowed_for, load_config

INITIAL_YAML = (
    'roles:\n'
    '  author: [claude]\n'
    '  reviewer: [claude]\n'
    'allowed:\n'
    '  author: [claude, codex@is, agy@h1/gemini-3.1-pro-high]\n'
    '  reviewer: [claude, agy@h1/gemini-3.1-pro-high, ollama]\n'
)

FAKE_SECURITY = """import os, sys
args = sys.argv[1:]
if args and args[0] == 'create-keychain':
    os.makedirs(os.path.dirname(args[-1]), exist_ok=True)
    open(args[-1], 'w').close()
sys.exit(0)
"""


class AccountsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / 'conductor'
        self.system = self.root / '.system'
        self.system.mkdir(parents=True)
        self.providers_file = self.system / 'providers.yaml'
        self.providers_file.write_text(INITIAL_YAML, encoding='utf-8')
        self.changes_file = self.system / 'providers-changes.jsonl'

        sec_bin = self.home / 'fake-security'
        sec_bin.write_text('#!' + sys.executable + '\n' + FAKE_SECURITY, encoding='utf-8')
        sec_bin.chmod(0o755)

        fake_agy = self.home / 'fake-agy'
        fake_agy.write_text('#!' + sys.executable + '\nsys.exit(0)\n', encoding='utf-8')
        fake_agy.chmod(0o755)

        env = patch.dict(os.environ, {
            'AI_ORCHESTRATOR_ROOT': str(self.root),
            'CONDUCTOR_SECURITY_BIN': str(sec_bin),
            'CONDUCTOR_AGY_BIN': str(fake_agy),
        })
        env.start()
        self.addCleanup(env.stop)

        home_patch = patch.object(Path, 'home', return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

        self.refresh_calls = []

        def fake_refresh(db_path, config):
            self.refresh_calls.append((db_path, config))
            return {'entries': [], 'errors': {}, 'notes': {}, 'checked_at': 1.0, 'efforts': {}}

        refresh_patch = patch('app.accounts.catalog.refresh', side_effect=fake_refresh)
        refresh_patch.start()
        self.addCleanup(refresh_patch.stop)

        from app.api import create_app
        self.db = str(self.system / 'state.db')
        self.app = create_app()
        self.app.state.runner_db = self.db
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')

    def test_list_accounts(self):
        r = self.client.get('/api/accounts')
        self.assertEqual(r.status_code, 200, r.text)
        accounts = r.json()['accounts']
        self.assertEqual([a['base'] for a in accounts], ['claude', 'codex@is', 'agy@h1', 'ollama'])

        by_base = {a['base']: a for a in accounts}
        self.assertEqual(by_base['claude']['provider'], 'claude')
        self.assertIsNone(by_base['claude']['account'])
        self.assertEqual(by_base['claude']['roles'], ['author', 'reviewer'])
        self.assertTrue(by_base['claude']['in_default_team'])
        self.assertEqual(by_base['claude']['login'], {'command': None, 'note': "claude (Claude Code'da /login)"})

        expected_codex_home = codex_home(self.db, 'is')
        self.assertEqual(by_base['codex@is']['provider'], 'codex')
        self.assertEqual(by_base['codex@is']['account'], 'is')
        self.assertEqual(by_base['codex@is']['roles'], ['author'])
        self.assertFalse(by_base['codex@is']['in_default_team'])
        self.assertEqual(by_base['codex@is']['login']['command'], f'CODEX_HOME="{expected_codex_home}" codex login')

        expected_agy_home = agy_home(self.db, 'h1')
        self.assertEqual(by_base['agy@h1']['provider'], 'agy')
        self.assertEqual(by_base['agy@h1']['account'], 'h1')
        self.assertEqual(by_base['agy@h1']['roles'], ['author', 'reviewer'])
        self.assertFalse(by_base['agy@h1']['in_default_team'])
        self.assertTrue(by_base['agy@h1']['installed'])
        self.assertEqual(
            by_base['agy@h1']['login']['command'],
            f'(H="{expected_agy_home}"; cd /tmp && HOME="$H" ~/.local/bin/agy)',
        )

        self.assertEqual(by_base['ollama']['provider'], 'ollama')
        self.assertIsNone(by_base['ollama']['account'])
        self.assertEqual(by_base['ollama']['roles'], ['reviewer'])
        self.assertFalse(by_base['ollama']['in_default_team'])
        self.assertEqual(by_base['ollama']['login'], {'command': None, 'note': 'ollama serve'})

    def test_add_agy_and_codex_creates_home_backup_header_and_jsonl(self):
        r = self.client.post('/api/accounts', json={
            'provider': 'agy',
            'account': 'hesap2',
            'roles': ['author', 'reviewer'],
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['account']['base'], 'agy@hesap2')

        home = Path(agy_home(self.db, 'hesap2'))
        self.assertTrue(home.is_dir())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertTrue((home / 'Library' / 'Keychains' / 'login.keychain-db').is_file())
        settings_json = json.loads((home / '.gemini' / 'antigravity-cli' / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(settings_json.get('permissions', {}).get('allow'), [])

        cfg = load_config(self.providers_file)
        allowed = allowed_for(cfg)
        self.assertIn('agy@hesap2', allowed['author'])
        self.assertIn('agy@hesap2', allowed['reviewer'])

        backups = sorted(self.system.glob('providers.pre-accounts-*.yaml'))
        self.assertEqual(len(backups), 1)
        backup_name = backups[0].name
        self.assertRegex(backup_name, r'^providers\.pre-accounts-\d{8}-\d{6}\.yaml$')
        self.assertEqual(backups[0].read_text(encoding='utf-8'), INITIAL_YAML)

        first_line = self.providers_file.read_text(encoding='utf-8').splitlines()[0]
        self.assertEqual(first_line, f'# PromptBridge Ayarlar → Hesaplar ile değişti; önceki: {backup_name}')

        changes = [json.loads(line) for line in self.changes_file.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['action'], 'add')
        self.assertEqual(changes[0]['base'], 'agy@hesap2')
        self.assertEqual(changes[0]['roles'], ['author', 'reviewer'])
        self.assertEqual(changes[0]['backup'], backup_name)
        self.assertTrue(changes[0]['at'])

        # Codex account creation creates 0700 CODEX_HOME
        r2 = self.client.post('/api/accounts', json={
            'provider': 'codex',
            'account': 'is2',
            'roles': ['author'],
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        c_home = Path(codex_home(self.db, 'is2'))
        self.assertTrue(c_home.is_dir())
        self.assertEqual(stat.S_IMODE(c_home.stat().st_mode), 0o700)

    def test_invalid_account_names_return_422(self):
        bad_payloads = (
            {'provider': 'agy', 'account': None, 'roles': ['author']},
            {'provider': 'agy', 'account': '', 'roles': ['author']},
            {'provider': 'agy', 'account': '!!! ---', 'roles': ['author']},   # K-060: nothing usable left
            {'provider': 'agy', 'account': 'a' * 61, 'roles': ['author']},
            {'provider': 'codex', 'account': '***', 'roles': ['author']},
            {'provider': 'claude', 'account': 'h1', 'roles': ['author']},
            {'provider': 'ollama', 'account': 'h1', 'roles': ['reviewer']},
            {'provider': 'agy', 'account': 'valid1', 'roles': []},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                r = self.client.post('/api/accounts', json=payload)
                self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.providers_file.read_text(encoding='utf-8'), INITIAL_YAML)
        self.assertFalse(self.changes_file.exists())

    def test_free_names_become_safe_ids_and_clash_only_on_the_same_id(self):   # K-060
        from app.accounts import account_id
        self.assertEqual([account_id(n) for n in ('İş Hesabı 2', 'Büyük_Çalışma', '  Ana / Şirket  ', 'a' * 30, '-x-', None, '  ')],
                         ['is-hesabi-2', 'buyuk-calisma', 'ana-sirket', 'a' * 16, 'x', None, None])
        r = self.client.post('/api/accounts', json={'provider': 'agy', 'account': 'İş Hesabı 2', 'roles': ['author']})
        self.assertEqual((r.status_code, r.json()['account']['base']), (200, 'agy@is-hesabi-2'), r.text)
        self.assertTrue(Path(agy_home(self.db, 'is-hesabi-2')).is_dir())
        again = self.client.post('/api/accounts', json={'provider': 'agy', 'account': 'is hesabi 2', 'roles': ['reviewer']})
        self.assertEqual((again.status_code, again.json()['code']), (409, 'exists'))
        self.assertIn('agy@is-hesabi-2', again.json()['detail'])

    def test_sign_in_opens_terminal_with_a_fixed_script_and_check_reports_it(self):   # K-060
        calls = []
        with patch('app.accounts._open_terminal', side_effect=calls.append):
            r = self.client.post('/api/accounts/agy@h1/login')
        self.assertEqual(r.status_code, 200, r.text)
        script = self.system / 'login' / 'agy-h1.command'
        self.assertEqual(calls, [script])
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        body = script.read_text()
        self.assertIn(f"HOME={agy_home(self.db, 'h1')} exec {(self.home / 'fake-agy').resolve()}", body)
        self.assertTrue((Path(agy_home(self.db, 'h1')) / 'Library/Keychains/agy.keychain-db').exists())   # keychain made first
        with patch('app.accounts._open_terminal', side_effect=calls.append):
            self.assertEqual(self.client.post('/api/accounts/codex@is/login').status_code, 200)
        self.assertIn('login', (self.system / 'login' / 'codex-is.command').read_text())
        for base, code in (('claude', 422), ('ollama', 422), ('agy@yok', 404), ('agy@h1/model', 404)):   # a slash is no route
            with self.subTest(base=base):
                self.assertEqual(self.client.post(f'/api/accounts/{base}/login').status_code, code)
        with patch('app.accounts._open_terminal', side_effect=OSError('no Terminal')):
            failed = self.client.post('/api/accounts/agy@h1/login')
        self.assertEqual((failed.status_code, failed.json()['code']), (409, 'login_open_failed'))
        with patch('app.accounts.catalog.refresh_base', return_value=(False, 'unauthenticated')) as probe:
            self.assertEqual(self.client.post('/api/accounts/agy@h1/check').json(), {'signed_in': False, 'error': 'unauthenticated'})
        probe.assert_called_once_with(self.db, 'agy', 'h1')
        evil = {'Origin': 'http://evil.example'}
        self.assertEqual(self.client.post('/api/accounts/agy@h1/login', headers=evil).status_code, 403)

    def test_duplicate_base_returns_409_exists(self):
        for payload in (
            {'provider': 'agy', 'account': 'h1', 'roles': ['author']},
            {'provider': 'codex', 'account': 'is', 'roles': ['reviewer']},
            {'provider': 'claude', 'account': None, 'roles': ['author']},
        ):
            with self.subTest(payload=payload):
                r = self.client.post('/api/accounts', json=payload)
                self.assertEqual(r.status_code, 409, r.text)
                self.assertEqual(r.json()['code'], 'exists')
        self.assertEqual(self.providers_file.read_text(encoding='utf-8'), INITIAL_YAML)

    def test_cannot_delete_base_in_default_team_or_saved_settings_team(self):
        # 1. `claude` is in providers.yaml roles
        r = self.client.delete('/api/accounts/claude')
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()['code'], 'in_default_team')

        # 2. Save `agy@h1/gemini-3.1-pro-high` in settings.json default team
        put_r = self.client.put('/api/settings', json={
            'team': {
                'author': ['claude/claude-sonnet-5-5~low'],
                'reviewer': ['agy@h1/gemini-3.1-pro-high'],
            }
        })
        self.assertEqual(put_r.status_code, 200, put_r.text)

        listed = {a['base']: a for a in self.client.get('/api/accounts').json()['accounts']}
        self.assertTrue(listed['agy@h1']['in_default_team'])

        r2 = self.client.delete('/api/accounts/agy@h1')
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertEqual(r2.json()['code'], 'in_default_team')

    def test_delete_removes_from_allowed_and_keeps_home(self):
        add_r = self.client.post('/api/accounts', json={
            'provider': 'agy',
            'account': 'gecici',
            'roles': ['author', 'reviewer'],
        })
        self.assertEqual(add_r.status_code, 200, add_r.text)
        home = Path(agy_home(self.db, 'gecici'))
        self.assertTrue(home.is_dir())
        started, release = threading.Event(), threading.Event()

        def slow_agy(db_path, account):
            started.set()
            release.wait(5)
            return [{'key': 'gemini', 'title': 'Gemini', 'models': None, 'windows': [{'remaining': 90.0}]}], None

        with quota._lock:
            quota._cache['agy@gecici'] = {'groups': [], 'error': None, 'checked_at': 1.0, 'source': 'probe'}
        with patch.object(quota, '_agy', side_effect=slow_agy):
            probe = threading.Thread(target=quota._refresh_one, args=(self.db, 'agy@gecici'))
            probe.start()
            self.assertTrue(started.wait(5))
            self.assertIn('agy@gecici', quota.snapshot()['accounts'])

            del_r = self.client.delete('/api/accounts/agy@gecici')
            self.assertEqual(del_r.status_code, 200, del_r.text)
            self.assertEqual(del_r.json()['deleted'], 'agy@gecici')
            self.assertNotIn('agy@gecici', quota.snapshot()['accounts'])

            release.set()
            probe.join(5)
        self.assertNotIn('agy@gecici', quota.snapshot()['accounts'])

        cfg = load_config(self.providers_file)
        allowed = allowed_for(cfg)
        self.assertNotIn('agy@gecici', allowed['author'])
        self.assertNotIn('agy@gecici', allowed['reviewer'])
        self.assertTrue(home.is_dir())

        changes = [json.loads(line) for line in self.changes_file.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(changes[-1]['action'], 'delete')
        self.assertEqual(changes[-1]['base'], 'agy@gecici')
        self.assertEqual(changes[-1]['roles'], ['author', 'reviewer'])

    def test_foreign_origin_and_mobile_gateway_rejected(self):
        evil = {'Origin': 'https://evil.example'}
        self.assertEqual(
            self.client.post('/api/accounts', json={'provider': 'agy', 'account': 'x1', 'roles': ['author']}, headers=evil).status_code,
            403,
        )
        self.assertEqual(self.client.delete('/api/accounts/codex@is', headers=evil).status_code, 403)
        self.assertEqual(self.providers_file.read_text(encoding='utf-8'), INITIAL_YAML)

        conn = store.connect(self.db)
        self.addCleanup(conn.close)
        store.migrate(conn)
        code = mobile.create_pairing(conn)['code']
        host = 'mac.tail1234.ts.net'
        login = 'operator@example.com'
        gw = TestClient(mobile.MobileGuard(self.app, self.db, host), base_url=f'https://{host}')
        token = gw.post('/api/mobile/pair', json={'code': code, 'device_name': 'Phone'},
                        headers={'Tailscale-User-Login': login}).json()['token']
        auth_headers = {'Authorization': f'Bearer {token}', 'Tailscale-User-Login': login, 'Origin': f'https://{host}'}

        self.assertEqual(gw.get('/api/accounts', headers=auth_headers).status_code, 404)
        self.assertEqual(
            gw.post('/api/accounts', json={'provider': 'agy', 'account': 'x1', 'roles': ['author']}, headers=auth_headers).status_code,
            404,
        )
        self.assertEqual(gw.delete('/api/accounts/codex@is', headers=auth_headers).status_code, 404)

    def test_max_allowed_exceeded_keeps_file_unchanged(self):
        full_authors = ['claude', *[f'agy@a{i}' for i in range(1, MAX_ALLOWED)]]
        self.assertEqual(len(full_authors), MAX_ALLOWED)
        full_yaml = (
            'roles:\n'
            '  author: [claude]\n'
            '  reviewer: [claude]\n'
            'allowed:\n'
            f"  author: [{', '.join(full_authors)}]\n"
            '  reviewer: [claude]\n'
        )
        self.providers_file.write_text(full_yaml, encoding='utf-8')

        r = self.client.post('/api/accounts', json={
            'provider': 'agy',
            'account': 'overflow',
            'roles': ['author'],
        })
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.providers_file.read_text(encoding='utf-8'), full_yaml)
        self.assertFalse(Path(agy_home(self.db, 'overflow')).exists())
        self.assertEqual(list(self.system.glob('providers.pre-accounts-*.yaml')), [])
        self.assertEqual(list(self.system.glob('.providers-*')), [])
        self.assertFalse(self.changes_file.exists())


if __name__ == '__main__':
    unittest.main()
