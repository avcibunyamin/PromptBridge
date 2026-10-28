"""F3-03: per-message team selection under the providers.yaml ceiling (card Rev 4, DoD 1-7)."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import psutil
from starlette.testclient import TestClient

from app import catalog, provider_auth, store, works_store as ws
from app.adapters.base import AdapterResult
from app.api import create_app
from app.provider_policy import DEFAULT, digest, effective, load_config, load_policy, normalize, team_problems
import tests.fakebin  # noqa: F401  (provider CLIs are stubs, not whatever is installed)

V2_DEFAULT_DIGEST = '60a36e1cdf6fb4ae030ea2eac2558be778a6709ea2447c7b36fdfb5e2b3ef8b9'   # pre-F3-03 main
V3_LOOP_DIGEST = '72e4b0ced27930f1eb3714328e3ead38c60123fa1e3cc2077fa2adb7dc2b24fe'


def write(root, text):
    (root / 'providers.yaml').write_text(text)
    return load_config(root / 'providers.yaml')


class Ceiling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)

    def reasons(self, config, team):
        return [(p['role'], p['provider'], p['reason']) for p in team_problems(config, team)]

    def test_without_allowed_each_role_is_limited_to_its_own_chain(self):
        config = load_config(self.root / 'missing.yaml')   # DEFAULT: author codex, reviewer claude
        self.assertEqual(self.reasons(config, {'author': ['claude'], 'reviewer': ['claude']}),
                         [('author', 'claude', 'provider_not_allowed')])   # not the union of roles
        self.assertEqual(self.reasons(config, {'author': ['codex'], 'reviewer': ['claude']}), [])

    def test_explicit_allowed_and_problem_reasons(self):
        config = write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\n'
                                  'allowed:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
        self.assertEqual(self.reasons(config, {'author': ['claude', 'codex'], 'reviewer': ['codex']}), [])
        cases = {
            'unknown': ({'author': ['gemini'], 'reviewer': ['claude']}, [('author', 'gemini', 'unknown_provider')]),
            'duplicate': ({'author': ['codex', 'codex'], 'reviewer': ['claude']}, [('author', 'codex', 'duplicate_in_role')]),
            'empty': ({'author': [], 'reviewer': ['claude']}, [('author', '', 'chain_length')]),
            'five': ({'author': ['codex', 'claude', 'codex@a', 'codex@b', 'codex@c'], 'reviewer': ['claude']}, [('author', '', 'chain_length')]),   # TB-06: up to four
            'missing role': ({'author': ['codex']}, [('reviewer', '', 'chain_length')]),
        }
        for name, (team, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.reasons(config, team), expected)
        narrow = write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\n'
                                  'allowed:\n  author: [codex]\n  reviewer: [claude]\n')
        self.assertEqual(self.reasons(narrow, {'author': ['claude'], 'reviewer': ['claude']}),
                         [('author', 'claude', 'provider_not_allowed')])

    def test_roles_must_be_a_subset_of_allowed_and_profiles_follow_the_matrix(self):
        with self.assertRaisesRegex(ValueError, 'subset'):
            write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\nallowed:\n  author: [claude]\n  reviewer: [claude]\n')
        with self.assertRaises(ValueError):
            normalize({'roles': {'author': [{'provider': 'codex', 'profile': 'READ_ONLY'}], 'reviewer': ['claude']}})
        with self.assertRaises(ValueError):
            write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\nallowed:\n  author: [codex, nope]\n  reviewer: [claude]\n')


    def test_a_provider_without_the_role_profile_is_refused(self):
        """Both real providers support both profiles today; a write-only one must never review."""
        from app import provider_policy
        with patch.dict(provider_policy.CAPABILITIES, {'writeonly': {'profiles': ('WRITE',), 'context_continuation': False}}):
            config = write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\n'
                                      'allowed:\n  author: [codex, writeonly]\n  reviewer: [claude, writeonly]\n')
            self.assertEqual(team_problems(config, {'author': ['writeonly'], 'reviewer': ['writeonly']}),
                             [{'role': 'reviewer', 'provider': 'writeonly', 'reason': 'profile_unsupported'}])
            with self.assertRaisesRegex(ValueError, 'profile'):
                normalize({'roles': {'author': ['codex'], 'reviewer': ['writeonly']}})


class DefaultPreserved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)

    def test_digests_are_byte_identical_for_v2_and_v3(self):
        self.assertEqual(digest(load_policy(self.root / 'missing.yaml')), V2_DEFAULT_DIGEST)
        v2 = load_config(self.root / 'missing.yaml')
        self.assertEqual(digest(effective(v2, {'author': ['codex'], 'reviewer': ['claude']})), V2_DEFAULT_DIGEST)
        v3 = write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\nloop:\n')
        self.assertEqual(digest(effective(v3)), V3_LOOP_DIGEST)
        self.assertEqual(digest(effective(v3, {'author': ['codex'], 'reviewer': ['claude']})), V3_LOOP_DIGEST)
        self.assertNotIn('loop', effective(v2, {'author': ['codex'], 'reviewer': ['claude']}))   # absence kept
        with_allowed = write(self.root, 'roles:\n  author: [codex]\n  reviewer: [claude]\n'
                                        'allowed:\n  author: [codex, claude]\n  reviewer: [claude]\n')
        self.assertEqual(digest(effective(with_allowed)), V2_DEFAULT_DIGEST)   # allowed is not in the digest


class FakeProviders:
    def __init__(self): self.calls = []
    def __call__(self, provider):
        outer = self
        class Adapter:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                outer.calls.append((provider, profile)); sid = provider_session_id or f'{provider}-{len(outer.calls)}'
                owner = {'pid': 1, 'pgid': 1, 'start_time': 1.0, 'boot_time': 1.0}
                yield {'type': 'process_started', 'ownership': owner}
                yield {'type': 'started', 'provider_session_id': sid}
                yield {'type': 'process_stopped', 'ownership': owner}
                yield {'type': 'completed', 'result': AdapterResult('ok', sid, None, 'stop', False, 'completed', 0)}
        return Adapter()


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.system').mkdir()
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        installed = patch('app.session_api.installed_providers', return_value={'codex': True, 'claude': True})
        installed.start(); self.addCleanup(installed.stop)
        self.probes = []
        probe = patch.object(provider_auth, '_probe', side_effect=lambda p, b, h=None, m=None: self.probes.append(p) or ('authenticated', True))
        probe.start(); self.addCleanup(probe.stop); provider_auth._cache.clear(); self.addCleanup(provider_auth._cache.clear)
        self.policy('roles:\n  author: [codex]\n  reviewer: [claude]\n'
                    'allowed:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
        self.app = create_app(); self.c = TestClient(self.app); self.c.__enter__(); self.addCleanup(self.c.__exit__, None, None, None)
        self.providers = FakeProviders()
        self.app.state.runner.adapter_factory = self.providers
        self.wid = str(uuid4())
        self.assertEqual(self.c.post('/api/works', json={'client_work_id': self.wid, 'title': 't',
                                                         'workspace_path': str(self.root)}).status_code, 201)

    def policy(self, text): (self.root / '.system/providers.yaml').write_text(text)

    def preview(self, author, reviewer):
        return self.c.get('/api/sessions/providers/preview', params={'author': author, 'reviewer': reviewer})

    def send(self, digest_, team=None, text='goal', cmid=None):
        body = {'client_message_id': cmid or str(uuid4()), 'text': text, 'policy_digest': digest_}
        if team is not None:
            body['team'] = team
        return self.c.post(f'/api/works/{self.wid}/messages', json=body)

    def test_preview_freeze_and_view(self):
        team = {'author': ['claude', 'codex'], 'reviewer': ['codex']}
        p = self.preview('claude,codex', 'codex').json()
        self.assertEqual(p['warnings'], ['same_provider_both_roles'])
        r = self.send(p['digest'], team)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()['team'], team)
        self.assertEqual(self.c.get(f'/api/works/{self.wid}').json()['latest_team'], team)   # follow-up default
        conn = store.connect(self.root / '.system/state.db'); self.addCleanup(conn.close)
        frozen = json.loads(conn.execute('SELECT policy_json FROM work_messages').fetchone()[0])
        self.assertEqual([i['provider'] for i in frozen['roles']['author']], ['claude', 'codex'])
        self.policy('roles:\n  author: [codex]\n  reviewer: [claude]\nmax_context_continuations: 1\n'
                    'allowed:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
        self.assertEqual(json.loads(conn.execute('SELECT policy_json FROM work_messages').fetchone()[0]), frozen)
        stale = self.send(p['digest'], team)                                   # budget changed → 409
        self.assertEqual((stale.status_code, stale.json()['code']), (409, 'conflict'))

    def test_idempotency_includes_the_team(self):
        p = self.preview('codex', 'claude').json(); cmid = str(uuid4())
        self.assertEqual(self.send(p['digest'], {'author': ['codex'], 'reviewer': ['claude']}, cmid=cmid).status_code, 201)
        self.assertEqual(self.send(p['digest'], {'author': ['codex'], 'reviewer': ['claude']}, cmid=cmid).status_code, 200)
        other = self.send(p['digest'], {'author': ['codex'], 'reviewer': ['codex']}, cmid=cmid)   # same text/digest
        self.assertEqual((other.status_code, other.json()['code']), (409, 'IDEMPOTENCY_CONFLICT'))
        no_team = str(uuid4())
        self.assertEqual(self.send(p['digest'], None, cmid=no_team).status_code, 201)
        added = self.send(p['digest'], {'author': ['codex'], 'reviewer': ['claude']}, cmid=no_team)
        self.assertEqual((added.status_code, added.json()['code']), (409, 'IDEMPOTENCY_CONFLICT'))
        conn = store.connect(self.root / '.system/state.db'); self.addCleanup(conn.close)
        stored = conn.execute('SELECT payload_hash FROM work_messages WHERE client_message_id=?', (no_team,)).fetchone()[0]
        self.assertEqual(stored, ws._hash({'text': 'goal', 'policy_digest': p['digest']}))   # pre-F3-03 hash

    def test_replay_ignores_a_broken_config_even_with_a_team(self):
        p = self.preview('claude', 'codex').json(); cmid = str(uuid4()); team = {'author': ['claude'], 'reviewer': ['codex']}
        first = self.send(p['digest'], team, cmid=cmid)
        self.policy('roles: [')
        replay = self.send(p['digest'], team, cmid=cmid)
        self.assertEqual((replay.status_code, replay.json()['id']), (200, first.json()['id']))

    def test_allowed_semantics(self):
        p = self.preview('claude', 'claude').json(); team = {'author': ['claude'], 'reviewer': ['claude']}
        self.policy('roles:\n  author: [codex]\n  reviewer: [claude]\n'
                    'allowed:\n  author: [codex, claude]\n  reviewer: [claude]\n')   # unrelated narrowing
        self.assertEqual(self.preview('claude', 'claude').json()['digest'], p['digest'])
        self.assertEqual(self.send(p['digest'], team).status_code, 201)
        self.policy('roles:\n  author: [codex]\n  reviewer: [claude]\n'
                    'allowed:\n  author: [codex]\n  reviewer: [claude]\n')             # the selected one removed
        r = self.send(p['digest'], team)
        self.assertEqual((r.status_code, r.json()['code']), (422, 'team_invalid'))
        self.assertEqual(r.json()['problems'], [{'role': 'author', 'provider': 'claude', 'reason': 'provider_not_allowed'}])

    def test_malformed_types_are_422_not_500(self):
        """Rev 4 (Codex P3): a missing `allowed` differs from `allowed: null`; items are typed before set ops."""
        roles = 'roles:\n  author: [codex]\n  reviewer: [claude]\n'
        bad = ['allowed: null\n', 'allowed: [codex]\n', 'allowed:\n  author: null\n  reviewer: [claude]\n',
               'allowed:\n  author: [[codex]]\n  reviewer: [claude]\n', 'allowed:\n  author: [123]\n  reviewer: [claude]\n',
               'allowed:\n  author: [codex, null]\n  reviewer: [claude]\n']
        ok = self.preview('codex', 'claude').json()['digest']
        for text in [roles + b for b in bad] + ['roles:\n  author: [{provider: [codex], profile: WRITE}]\n  reviewer: [claude]\n']:
            with self.subTest(config=text):
                self.policy(text)
                for r in (self.c.get('/api/sessions/providers'), self.preview('codex', 'claude'),
                          self.send(ok, {'author': ['codex'], 'reviewer': ['claude']})):
                    self.assertEqual((r.status_code, r.json()['code']), (422, 'validation_error'), r.text)
                    self.assertEqual(set(r.json()), {'detail', 'code'})
        self.policy(roles)                                                     # no `allowed` key: unchanged default
        self.assertEqual(self.c.get('/api/sessions/providers').json()['ceiling']['allowed'],
                         {'author': ['codex'], 'reviewer': ['claude']})
        for author in ([123], [['codex']], [None]):
            with self.subTest(team_author=author):
                r = self.send(ok, {'author': author, 'reviewer': ['claude']})
                self.assertEqual((r.status_code, r.json()['code']), (422, 'validation_error'), r.text)

    def test_unverified_probe_cleanup_blocks_that_provider_and_is_visible(self):
        """Rev 4 (Codex P2): GET stays 200, health is reported, no further probe for that provider."""
        self.addCleanup(provider_auth._blocked.clear)
        results = {'codex': ('unknown', False), 'claude': ('authenticated', True)}
        with patch.object(provider_auth, '_probe', side_effect=lambda p, b, h=None, m=None: self.probes.append(p) or results[p]):
            r = self.c.get('/api/sessions/providers', params={'check_auth': 'true'})
            self.assertEqual(r.status_code, 200)
            a = r.json()['availability']
            self.assertEqual((a['codex']['auth'], a['codex']['health'], a['codex']['checked_at']), ('unknown', 'probe_cleanup_failed', None))
            self.assertEqual((a['claude']['auth'], a['claude']['health']), ('authenticated', 'ok'))
            provider_auth._cache.clear()                                       # even after the cache expires
            again = self.c.get('/api/sessions/providers', params={'check_auth': 'true'}).json()['availability']
        self.assertEqual(self.probes.count('codex'), 1)
        self.assertEqual(self.probes.count('claude'), 2)
        self.assertEqual(again['codex']['health'], 'probe_cleanup_failed')
        self.assertEqual(self.c.get('/api/sessions/providers').json()['availability']['codex']['health'], 'probe_cleanup_failed')

    def test_error_bodies(self):
        r = self.preview('gemini', 'claude')
        self.assertEqual(r.json(), {'detail': 'The selected team is not allowed', 'code': 'team_invalid',
                                    'problems': [{'role': 'author', 'provider': 'gemini', 'reason': 'unknown_provider'}]})
        extra = self.send('0' * 64, {'author': ['codex'], 'reviewer': ['claude'], 'x': []})
        self.assertEqual((extra.status_code, extra.json()['code']), (422, 'validation_error'))
        long = self.send('0' * 64, {'author': ['codex'] * 5, 'reviewer': ['claude']})
        self.assertEqual((long.status_code, long.json()['code']), (422, 'validation_error'))
        self.assertEqual(set(long.json()), {'detail', 'code'})
        cross = self.c.post(f'/api/works/{self.wid}/messages', headers={'Origin': 'http://evil.example'},
                            json={'client_message_id': str(uuid4()), 'text': 'x', 'policy_digest': '0' * 64,
                                  'team': {'author': ['codex'], 'reviewer': ['claude']}})
        self.assertEqual(cross.status_code, 403)

    def test_no_subprocess_on_write_paths_and_opt_in_check(self):
        p = self.preview('codex', 'claude').json()
        self.send(p['digest'], {'author': ['codex'], 'reviewer': ['claude']})
        self.send(p['digest'])
        self.c.get('/api/sessions/providers')
        self.assertEqual(self.probes, [])                                     # POST, preview, plain GET: none
        body = self.c.get('/api/sessions/providers', params={'check_auth': 'true'}).json()
        self.assertEqual(sorted(self.probes), ['claude', 'codex'])
        self.assertEqual({k: v['auth'] for k, v in body['availability'].items()}, {'codex': 'authenticated', 'claude': 'authenticated'})
        self.c.get('/api/sessions/providers', params={'check_auth': 'true'})
        self.assertEqual(len(self.probes), 2)                                  # cached for 60 s
        self.assertEqual(self.c.get('/api/sessions/providers').json()['availability']['codex']['auth'], 'authenticated')
        self.assertEqual(body['ceiling']['allowed'], {'author': ['codex', 'claude'], 'reviewer': ['claude', 'codex']})

    def test_codex_effort_is_rejected_before_the_runner_opens_a_provider(self):
        saved = dict(catalog._cache); self.addCleanup(lambda: catalog._cache.update(saved))
        catalog._cache.update(entries=[{'identity': 'codex/gpt-5.5', 'provider': 'codex', 'account': None,
                                        'model': 'gpt-5.5', 'efforts': ['low', 'medium', 'high', 'xhigh'],
                                        'status': 'available', 'source': 'live'}],
                              errors={}, checked_at=time.time())
        p = self.preview('codex/gpt-5.5~max', 'claude')
        self.assertEqual((p.status_code, p.json()['code']), (422, 'team_invalid'))
        ok = self.preview('codex/gpt-5.5~xhigh', 'claude').json()
        r = self.send(ok['digest'], {'author': ['codex/gpt-5.5~max'], 'reviewer': ['claude']})
        self.assertEqual((r.status_code, r.json()['code']), (422, 'team_invalid'))
        self.assertEqual(r.json()['problems'], [{'role': 'author', 'provider': 'codex/gpt-5.5~max',
                                                 'reason': 'effort_unsupported'}])
        time.sleep(0.1)
        self.assertEqual(self.providers.calls, [])

    def test_default_team_codex_effort_uses_catalog_validation_too(self):
        saved = dict(catalog._cache); self.addCleanup(lambda: catalog._cache.update(saved))
        catalog._cache.update(entries=[{'identity': 'codex/gpt-5.5', 'provider': 'codex', 'account': None,
                                        'model': 'gpt-5.5', 'efforts': ['low', 'medium', 'high', 'xhigh'],
                                        'status': 'available', 'source': 'live'}],
                              errors={}, checked_at=time.time())
        self.policy('roles:\n  author: [codex/gpt-5.5~max]\n  reviewer: [claude]\n'
                    'allowed:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
        digest_ = self.c.get('/api/sessions/providers').json()['digest']
        r = self.send(digest_)
        self.assertEqual((r.status_code, r.json()['code']), (422, 'team_invalid'))
        self.assertEqual(r.json()['problems'], [{'role': 'author', 'provider': 'codex/gpt-5.5~max',
                                                 'reason': 'effort_unsupported'}])
        time.sleep(0.1)
        self.assertEqual(self.providers.calls, [])


class SessionProbe(unittest.TestCase):
    def setUp(self):
        provider_auth._cache.clear(); self.addCleanup(provider_auth._cache.clear)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def script(self, body):
        path = Path(self.tmp.name) / f'cli{len(os.listdir(self.tmp.name))}'
        path.write_text('#!/bin/sh\n' + body); path.chmod(0o700); return str(path)

    def test_classification(self):
        cases = [('codex', 0, 'Logged in using ChatGPT\n', 'authenticated'), ('codex', 1, 'Not logged in\n', 'unauthenticated'),
                 ('codex', 0, 'something new', 'unknown'), ('claude', 0, '{"loggedIn": true}', 'authenticated'),
                 ('claude', 0, 'warning: x\n{"loggedIn": true}', 'authenticated'),
                 ('claude', 1, '{"loggedIn": false}', 'unauthenticated'), ('claude', 0, 'not json', 'unknown'),
                 ('claude', 0, '{"loggedIn": "yes"}', 'unknown')]
        for provider, code, out, expected in cases:
            with self.subTest(provider=provider, out=out):
                self.assertEqual(provider_auth.classify(provider, code, out), expected)

    def test_large_output_drains_pipe_without_timing_out(self):
        cli = self.script('head -c 204800 /dev/zero\n')
        proc = provider_auth.subprocess.Popen([cli], stdin=provider_auth.subprocess.DEVNULL,
                                              stdout=provider_auth.subprocess.PIPE,
                                              stderr=provider_auth.subprocess.STDOUT, start_new_session=True)
        try:
            buf, exited = provider_auth._read(proc, 2.0)
        finally:
            _, gone = provider_auth._cleanup(proc)
        self.assertTrue(exited)
        self.assertTrue(gone)
        self.assertEqual(len(buf), provider_auth.LIMIT)

    def test_real_subprocess_output_is_not_kept_and_timeouts_are_unknown(self):
        secret = 'person@example.com'
        ok = self.script(f'echo \'{{"loggedIn": true, "email": "{secret}"}}\'\n')
        with self.assertNoLogs(level='DEBUG'):
            state, checked = provider_auth.check('claude', ok)
        self.assertEqual(state, 'authenticated'); self.assertIsInstance(checked, float)
        self.assertNotIn(secret, repr(provider_auth._cache))
        provider_auth._cache.clear()
        slow = self.script('sleep 10\n')
        with patch.object(provider_auth, 'TIMEOUT', 0.3):
            start = time.monotonic(); state, _ = provider_auth.check('codex', slow, self.tmp.name)
        self.assertEqual(state, 'unknown'); self.assertLess(time.monotonic() - start, 3)
        provider_auth._cache.clear()
        self.assertEqual(provider_auth.check('codex', str(Path(self.tmp.name) / 'missing'), self.tmp.name)[0], 'unknown')

    def child_pid(self):
        path = Path(self.tmp.name) / 'child.pid'
        for _ in range(100):
            if path.exists() and path.read_text().strip():
                return int(path.read_text())
            time.sleep(0.02)
        self.fail('child pid not written')

    def assertDead(self, pid):
        # Group death was verified by the probe; an orphan may linger only as a zombie until launchd reaps it.
        try:
            self.assertEqual(psutil.Process(pid).status(), psutil.STATUS_ZOMBIE)
        except psutil.NoSuchProcess:
            pass

    def test_leader_exits_while_a_child_lives(self):
        """Rev 4 DoD 6a: the leader exits, a child stays in the group (holding the pipe or not)."""
        self.addCleanup(provider_auth._blocked.clear)
        pidfile = Path(self.tmp.name) / 'child.pid'
        for redirect in ('', ' >/dev/null 2>&1'):
            with self.subTest(child_holds_pipe=not redirect):
                provider_auth._cache.clear(); pidfile.unlink(missing_ok=True)
                cli = self.script(f'sleep 30{redirect} &\necho $! > {pidfile}\necho \'{{"loggedIn": true}}\'\n')
                with patch.object(provider_auth, 'TIMEOUT', 10.0):            # bound tied to TIMEOUT, not to machine load
                    start = time.monotonic()
                    state, checked = provider_auth.check('claude', cli)
                    self.assertLess(time.monotonic() - start, provider_auth.TIMEOUT / 2)   # no wait for the pipe to close
                self.assertEqual(state, 'authenticated'); self.assertIsInstance(checked, float)
                self.assertDead(self.child_pid())
                self.assertEqual(provider_auth.health('claude'), 'ok')
                provider_auth._cache.clear()
                self.assertEqual(provider_auth.check('claude', cli)[0], 'authenticated')   # the next check still runs
                self.assertDead(self.child_pid())

    def test_timeout_kills_a_hung_leader_and_its_child(self):
        pidfile = Path(self.tmp.name) / 'child.pid'
        cli = self.script(f'sleep 30 &\necho $! > {pidfile}\nsleep 30\n')
        with patch.object(provider_auth, 'TIMEOUT', 3.0):                   # long enough for a cold shell start under load
            start = time.monotonic(); state, _ = provider_auth.check('codex', cli, self.tmp.name)
        self.assertEqual(state, 'unknown'); self.assertLess(time.monotonic() - start, 3.0 + provider_auth.VERIFY + 1)
        self.assertDead(self.child_pid())
        self.assertEqual(provider_auth.health('codex'), 'ok')

    def test_unverifiable_group_death_blocks_new_probes(self):
        self.addCleanup(provider_auth._blocked.clear)
        cli = self.script('echo \'{"loggedIn": true}\'\n')
        real, spawned = provider_auth.subprocess.Popen, []
        def counting(*a, **k):
            spawned.append(a[0][0]); return real(*a, **k)
        with patch.object(provider_auth, 'group_members', return_value=[{'pid': 1}]), \
                patch.object(provider_auth, 'VERIFY', 0.1), patch.object(provider_auth.subprocess, 'Popen', side_effect=counting):
            with self.assertLogs('app.provider_auth', 'WARNING') as logs:
                self.assertEqual(provider_auth.check('claude', cli), ('unknown', None))
            self.assertEqual(provider_auth.health('claude'), 'probe_cleanup_failed')
            self.assertNotIn('loggedIn', ''.join(logs.output))
            self.assertEqual(provider_auth.check('claude', cli), ('unknown', None))
            self.assertEqual(len(spawned), 1)                                  # no new probe for that provider
            self.assertEqual(provider_auth.health('codex'), 'ok')              # other providers unaffected

    def test_concurrent_checks_share_one_probe(self):
        calls, results = [], []
        def slow_probe(provider, binary, home=None, model=None):
            calls.append(provider); time.sleep(0.2); return 'authenticated', True
        with patch.object(provider_auth, '_probe', side_effect=slow_probe):
            threads = [threading.Thread(target=lambda: results.append(provider_auth.check('codex', 'x'))) for _ in range(5)]
            for t in threads: t.start()
            for t in threads: t.join()
        self.assertEqual(calls, ['codex'])
        self.assertEqual(len(results), 5)
        self.assertTrue(all(state == 'authenticated' and isinstance(at, float) for state, at in results), results)
        self.assertEqual(provider_auth.health('codex'), 'ok')

    def test_an_error_after_the_start_keeps_the_cleanup_verdict(self):
        """Codex code review P2: a psutil error while reading must not hide an unverified cleanup."""
        self.addCleanup(provider_auth._blocked.clear)
        cli = self.script('echo \'{"loggedIn": true}\'\n')
        real, spawned = provider_auth.subprocess.Popen, []
        def counting(*a, **k):
            spawned.append(a[0][0]); return real(*a, **k)
        denied = psutil.AccessDenied(pid=1)
        with patch.object(provider_auth, '_exited', side_effect=denied), \
                patch.object(provider_auth.subprocess, 'Popen', side_effect=counting):
            with patch.object(provider_auth, 'group_members', return_value=[{'pid': 1}]), patch.object(provider_auth, 'VERIFY', 0.1), \
                    self.assertLogs('app.provider_auth', 'WARNING'):
                self.assertEqual(provider_auth.check('claude', cli), ('unknown', None))
            self.assertEqual(provider_auth.health('claude'), 'probe_cleanup_failed')
            provider_auth._cache.clear()
            self.assertEqual(provider_auth.check('claude', cli), ('unknown', None))
            self.assertEqual(len(spawned), 1)                                  # blocked: no second process
            # Same read error with a verified cleanup: unknown, but the provider stays usable.
            state, checked = provider_auth.check('codex', cli, self.tmp.name)
            self.assertEqual((state, provider_auth.health('codex')), ('unknown', 'ok')); self.assertIsInstance(checked, float)
        self.assertEqual(provider_auth._probe('codex', str(Path(self.tmp.name) / 'missing')), ('unknown', True))   # never started
        with patch.object(provider_auth, '_probe', side_effect=RuntimeError('unexpected')), self.assertLogs('app.provider_auth', 'WARNING'):
            self.assertEqual(provider_auth.check('claude', cli), ('unknown', None))           # already blocked above
            provider_auth._blocked.clear()
            self.assertEqual(provider_auth.check('claude', cli), ('unknown', None))           # an escaping error blocks too
        self.assertEqual(provider_auth.health('claude'), 'probe_cleanup_failed')


if __name__ == '__main__':
    unittest.main()
