"""TB-19 (K-026): panel preferences in .system/settings.json, checked against the providers.yaml ceiling."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import user_settings
from app.provider_policy import digest, effective, load_config, team_problems

CEILING = ('roles:\n  author: [claude]\n  reviewer: [claude]\n'
           'allowed:\n  author: [claude, agy@h1]\n  reviewer: [claude, agy@h2/gemini-3.1-pro-high]\n')
TEAM = {'author': ['claude/claude-sonnet-5-5~low'], 'reviewer': ['agy@h2/gemini-3.1-pro-high']}


class Settings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name); self.root = self.home / 'conductor'; (self.root / '.system').mkdir(parents=True)
        (self.root / '.system/providers.yaml').write_text(CEILING)
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        home = patch.object(Path, 'home', return_value=self.home); home.start(); self.addCleanup(home.stop)
        from app.api import create_app
        self.app = create_app()
        self.app.state.runner_db = str(self.root / '.system/state.db')   # lifespan does not run here
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.file = self.root / '.system/settings.json'

    def put(self, body, **headers):
        return self.client.put('/api/settings', json=body, headers=headers)

    def test_defaults_without_a_file(self):
        body = self.client.get('/api/settings').json()
        self.assertEqual(body['settings'], user_settings.DEFAULTS)
        self.assertEqual((body['error'], body['problems'], body['current_digest']), (None, [], None))
        self.assertFalse(self.file.exists())

    def test_team_is_checked_against_the_ceiling_and_the_digest_is_computed_here(self):
        for team in ({'author': ['codex'], 'reviewer': ['claude']},                        # not allowed
                     {'author': ['agy@h2/gemini-3.1-pro-high'], 'reviewer': ['claude']},     # account outside the ceiling
                     {'author': ['claude', 'claude/opus', 'claude/sonnet', 'claude/haiku', 'claude/fable'], 'reviewer': ['claude']}):
            with self.subTest(team=team):
                r = self.put({'team': team})
                self.assertEqual(r.status_code, 422, r.text)
                self.assertFalse(self.file.exists())
        self.assertEqual(self.put({'team': TEAM, 'accepted_digest': 'f' * 64}).status_code, 422)   # extra key refused
        r = self.put({'team': TEAM})
        self.assertEqual(r.status_code, 200, r.text)
        expected = digest(effective(load_config(self.root / '.system/providers.yaml'), TEAM))
        saved = json.loads(self.file.read_text())
        self.assertEqual((saved['team'], saved['accepted_digest']), (TEAM, expected))
        self.assertEqual(r.json()['current_digest'], expected)
        # the same team previewed for a message has the same digest: the panel can treat it as accepted (K1)
        q = {'author': ','.join(TEAM['author']), 'reviewer': ','.join(TEAM['reviewer'])}
        self.assertEqual(self.client.get('/api/sessions/providers/preview', params=q).json()['digest'], expected)

    def test_partial_update_keeps_the_rest_and_team_null_resets(self):
        self.assertEqual(self.put({'team': TEAM}).status_code, 200)
        self.assertEqual(self.put({'theme': 'dark'}).status_code, 200)
        saved = json.loads(self.file.read_text())
        self.assertEqual((saved['team'], saved['theme']), (TEAM, 'dark'))
        self.assertEqual(self.put({'theme': 'blue'}).status_code, 422)
        self.assertEqual(self.put({'team': None}).status_code, 200)
        saved = json.loads(self.file.read_text())
        self.assertEqual((saved['team'], saved['accepted_digest'], saved['theme']), (None, None, 'dark'))

    def test_clone_dir_rules(self):
        projects = self.home / 'Projeler'; projects.mkdir()
        for value, code in (('Projeler', 'not_absolute'), ('/tmp', 'outside_home'), (str(self.home / 'yok'), 'not_a_directory'),
                            (str(self.root / '.system'), 'inside_conductor'), (str(self.root), 'inside_conductor')):
            with self.subTest(value=value):
                r = self.put({'github': {'clone_dir': value}})
                self.assertEqual((r.status_code, r.json()['code']), (422, 'clone_dir_invalid'), r.text)
                self.assertEqual(r.json()['detail'], code)
        self.assertEqual(self.put({'github': {'clone_dir': str(projects)}}).status_code, 200)
        self.assertEqual(json.loads(self.file.read_text())['github']['clone_dir'], str(projects.resolve()))

    def test_write_is_private_and_atomic(self):
        self.assertEqual(self.put({'theme': 'light'}).status_code, 200)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in (self.root / '.system').iterdir() if p.name.startswith('.settings-')], [])
        with patch('app.user_settings.json.dump', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                self.put({'theme': 'dark'})
        self.assertEqual(json.loads(self.file.read_text())['theme'], 'light')          # old file intact
        self.assertEqual([p.name for p in (self.root / '.system').iterdir() if p.name.startswith('.settings-')], [])

    def test_broken_file_falls_back_and_a_save_repairs_it(self):
        self.file.write_bytes(b'{not json')
        body = self.client.get('/api/settings').json()
        self.assertEqual((body['settings'], body['error']), (user_settings.DEFAULTS, 'settings_unreadable'))
        self.assertEqual(self.put({'theme': 'dark'}).status_code, 200)
        self.assertIsNone(self.client.get('/api/settings').json()['error'])
        backups = list(self.file.parent.glob('settings.json.broken-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'{not json')
        self.assertEqual(json.loads(self.file.read_text())['theme'], 'dark')

    def test_concurrent_puts_keep_both_fields(self):
        real_load = user_settings.load
        def slow_load(db_path):
            res = real_load(db_path)
            time.sleep(0.05)
            return res
        barrier = threading.Barrier(2)
        statuses = []
        def worker(payload):
            barrier.wait()
            statuses.append(self.put(payload).status_code)
        with patch('app.user_settings.load', side_effect=slow_load):
            t1 = threading.Thread(target=worker, args=({'theme': 'dark'},))
            t2 = threading.Thread(target=worker, args=({'pinned_works': ['w1']},))
            t1.start(); t2.start()
            t1.join(); t2.join()
        self.assertEqual(statuses, [200, 200])
        saved = json.loads(self.file.read_text())
        self.assertEqual((saved['theme'], saved['pinned_works']), ('dark', ['w1']))

    def test_team_problems_rejects_unknown_roles_and_missing_roles(self):
        cfg = load_config(self.root / '.system/providers.yaml')
        self.assertEqual(
            team_problems(cfg, {**TEAM, 'arbiter': ['claude']}),
            [{'role': 'arbiter', 'provider': '', 'reason': 'unknown_role'}],
        )
        self.assertEqual(
            team_problems(cfg, {'author': TEAM['author']}),
            [{'role': 'reviewer', 'provider': '', 'reason': 'chain_length'}],
        )

    def test_a_changed_ceiling_reports_problems_and_keeps_the_file(self):
        self.assertEqual(self.put({'team': TEAM}).status_code, 200)
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        body = self.client.get('/api/settings').json()
        self.assertTrue(body['problems'])
        self.assertIsNone(body['current_digest'])
        self.assertEqual(json.loads(self.file.read_text())['team'], TEAM)

    def test_guard_and_gateway(self):
        self.assertEqual(self.put({'theme': 'dark'}, Origin='https://evil.example').status_code, 403)
        self.assertFalse(self.file.exists())
        # K-033: through the gateway only a `command` device, and only theme/team (tests/test_tb24_command.py)


if __name__ == '__main__':
    unittest.main()
