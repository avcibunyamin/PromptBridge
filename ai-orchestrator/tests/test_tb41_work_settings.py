"""TB-41: per-work team (work_teams) and quota wait (work_waits) settings, bounds, team_check and phone scope."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import mobile, user_settings
from app.api import create_app


class TB41WorkSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.system').mkdir()
        self.db = self.root / '.system/state.db'
        (self.root / '.system/providers.yaml').write_text(
            'roles:\n  author: [codex]\n  reviewer: [claude]\nmax_context_continuations: 2\n'
        )
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        scan = patch('app.work_dispatcher.SCAN_SECONDS', 3600)
        scan.start()
        self.addCleanup(scan.stop)

    def client(self):
        app = create_app()
        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)

        class _NeverRunAdapter:
            async def run(self, *args, **kwargs):
                raise AssertionError('real provider adapter must never run in unit tests')
                yield  # pragma: no cover

        app.state.runner.adapter_factory = lambda _provider: _NeverRunAdapter()
        return c

    def test_defaults_and_phone_fields_membership(self):
        c = self.client()
        res = c.get('/api/settings')
        self.assertEqual(res.status_code, 200)
        settings = res.json()['settings']
        self.assertEqual(settings['work_teams'], {})
        self.assertEqual(settings['work_waits'], {})
        self.assertIn('work_teams', user_settings.PHONE_FIELDS)
        self.assertNotIn('work_waits', user_settings.PHONE_FIELDS)

    def test_work_teams_save_bounds_and_invalid_team_422(self):
        c = self.client()
        valid_team = {'author': ['codex'], 'reviewer': ['claude']}

        # Save a single work team
        put_one = c.put('/api/settings', json={'work_teams': {'work_1': valid_team}})
        self.assertEqual(put_one.status_code, 200, put_one.text)
        self.assertEqual(put_one.json()['settings']['work_teams'], {'work_1': valid_team})

        # Save 50 keys (exact max limit) + 64-char key
        fifty = {f'w_{i:02d}': valid_team for i in range(49)}
        key_64 = 'k' * 64
        fifty[key_64] = valid_team
        self.assertEqual(len(fifty), 50)
        put_50 = c.put('/api/settings', json={'work_teams': fifty})
        self.assertEqual(put_50.status_code, 200, put_50.text)
        self.assertEqual(len(put_50.json()['settings']['work_teams']), 50)

        # 51 keys -> 422
        fifty_one = {f'w_{i:02d}': valid_team for i in range(51)}
        self.assertEqual(c.put('/api/settings', json={'work_teams': fifty_one}).status_code, 422)

        # Key > 64 chars or empty key -> 422
        self.assertEqual(c.put('/api/settings', json={'work_teams': {'k' * 65: valid_team}}).status_code, 422)
        self.assertEqual(c.put('/api/settings', json={'work_teams': {'': valid_team}}).status_code, 422)

        # Invalid team via team_check (claude is not allowed in author role) -> 422 team_invalid
        bad_team = {'author': ['claude'], 'reviewer': ['claude']}
        bad_res = c.put('/api/settings', json={'work_teams': {'work_1': bad_team}})
        self.assertEqual(bad_res.status_code, 422)
        self.assertEqual(bad_res.json().get('code'), 'team_invalid')
        self.assertTrue(bad_res.json().get('problems'))

        # Previous valid 50 entries remain untouched on disk after rejected writes
        self.assertEqual(len(c.get('/api/settings').json()['settings']['work_teams']), 50)

        # Resetting to empty dict clears work_teams
        clear_res = c.put('/api/settings', json={'work_teams': {}})
        self.assertEqual(clear_res.status_code, 200)
        self.assertEqual(clear_res.json()['settings']['work_teams'], {})

    def test_work_waits_save_and_bounds(self):
        c = self.client()
        waits_payload = {
            'w_on': {'enabled': True, 'deadline_hours': 6, 'max_attempts': 4},
            'w_off': {'enabled': False},
        }
        put_res = c.put('/api/settings', json={'work_waits': waits_payload})
        self.assertEqual(put_res.status_code, 200, put_res.text)
        self.assertEqual(put_res.json()['settings']['work_waits'], waits_payload)

        # 50 keys allowed
        fifty_waits = {f'w_{i:02d}': {'enabled': True, 'deadline_hours': 4, 'max_attempts': 3} for i in range(50)}
        self.assertEqual(c.put('/api/settings', json={'work_waits': fifty_waits}).status_code, 200)

        # 51 keys -> 422
        fifty_one_waits = {f'w_{i:02d}': {'enabled': False} for i in range(51)}
        self.assertEqual(c.put('/api/settings', json={'work_waits': fifty_one_waits}).status_code, 422)

        # Key > 64 chars -> 422
        self.assertEqual(c.put('/api/settings', json={'work_waits': {'x' * 65: {'enabled': False}}}).status_code, 422)

        # Out-of-range deadline_hours / max_attempts -> 422
        self.assertEqual(
            c.put('/api/settings', json={'work_waits': {'w1': {'enabled': True, 'deadline_hours': 0}}}).status_code,
            422,
        )
        self.assertEqual(
            c.put('/api/settings', json={'work_waits': {'w1': {'enabled': True, 'deadline_hours': 25}}}).status_code,
            422,
        )
        self.assertEqual(
            c.put('/api/settings', json={'work_waits': {'w1': {'enabled': True, 'max_attempts': 0}}}).status_code,
            422,
        )
        self.assertEqual(
            c.put('/api/settings', json={'work_waits': {'w1': {'enabled': True, 'max_attempts': 6}}}).status_code,
            422,
        )

    def test_phone_can_write_work_teams_but_cannot_read_or_write_work_waits(self):
        c = self.client()
        # Mac saves a work_waits entry first
        c.put('/api/settings', json={'work_waits': {'w1': {'enabled': True, 'deadline_hours': 4, 'max_attempts': 3}}})

        host, login = 'mac.tail1234.ts.net', 'me@example.com'
        gw = TestClient(mobile.MobileGuard(c.app, str(self.db), host), base_url=f'https://{host}')
        code = c.post('/api/mobile/pairing').json()['code']
        paired = gw.post(
            '/api/mobile/pair',
            json={'code': code, 'device_name': 'Phone'},
            headers={'Tailscale-User-Login': login},
        ).json()
        token, device_id = paired['token'], paired['device_id']
        headers = {'Authorization': f'Bearer {token}', 'Tailscale-User-Login': login, 'Origin': f'https://{host}'}
        c.put(f'/api/mobile/devices/{device_id}/scopes', json={'scopes': ['command']})

        # Phone GET returns work_teams, hides work_waits
        phone_get = gw.get('/api/settings', headers=headers)
        self.assertEqual(phone_get.status_code, 200)
        self.assertEqual(phone_get.json()['settings']['work_teams'], {})
        self.assertNotIn('work_waits', phone_get.json()['settings'])

        # Phone PUT work_teams succeeds
        valid_team = {'author': ['codex'], 'reviewer': ['claude']}
        phone_put_team = gw.put('/api/settings', json={'work_teams': {'w1': valid_team}}, headers=headers)
        self.assertEqual(phone_put_team.status_code, 200, phone_put_team.text)
        self.assertEqual(phone_put_team.json()['settings']['work_teams'], {'w1': valid_team})
        self.assertNotIn('work_waits', phone_put_team.json()['settings'])

        # Phone PUT work_waits is rejected with 403 mobile_field
        phone_put_wait = gw.put(
            '/api/settings',
            json={'work_waits': {'w1': {'enabled': False}}},
            headers=headers,
        )
        self.assertEqual((phone_put_wait.status_code, phone_put_wait.json()['code']), (403, 'mobile_field'))

        # Mac still sees both work_teams and the earlier work_waits
        mac_get = c.get('/api/settings').json()['settings']
        self.assertEqual(mac_get['work_teams'], {'w1': valid_team})
        self.assertEqual(mac_get['work_waits'], {'w1': {'enabled': True, 'deadline_hours': 4, 'max_attempts': 3}})


if __name__ == '__main__':
    unittest.main()
