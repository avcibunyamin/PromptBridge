"""TB-06 (K-016/K-017): `provider[@account][/model]` identities and chains of up to four links.

Fake adapters and patched probes only (no provider quota).
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import provider_auth, provider_policy, store, works_store as ws, runner_store as repo
from app.adapters.base import AdapterResult
from app.provider_policy import (DEFAULT, account_of, digest, kind, load_config, model_of, normalize,
                                 parse, team_problems, team_warnings)
from app.runner import SessionRunner, _get_adapter
from tests.test_f303_team import V2_DEFAULT_DIGEST
from tests.test_works import Scripted, ok

# A tool with accounts and models (the shape `agy` gets in TB-08), only for these tests.
MULTI = {'multi': {'profiles': ('WRITE', 'READ_ONLY'), 'context_continuation': False, 'accounts': True, 'models': True}}


def quota(text='quota'):
    return AdapterResult(text, None, None, 'error', True, 'failed', 1, 'QUOTA_EXCEEDED', {'code': 'test-fixture'})


class Identity(unittest.TestCase):
    def test_parse_table(self):
        cases = {'codex': ('codex', None, None), 'codex@is': ('codex', 'is', None),
                 'multi@hesap1/claude-sonnet-4-6': ('multi', 'hesap1', 'claude-sonnet-4-6'),
                 'multi/gemini-3.1-pro-high': ('multi', None, 'gemini-3.1-pro-high')}
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(parse(text), expected)
                self.assertEqual((kind(text), account_of(text), model_of(text)), expected)
        for bad in ('', 'Codex', 'codex@', 'codex@-x', 'codex@a/b/c', 'codex@a@b', 'codex@' + 'a' * 17,
                    'm/' + 'x' * 49, 'codex@A', 'codex@a b', 7, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse(bad)

    def test_plain_policies_are_byte_identical(self):
        self.assertEqual(digest(normalize(DEFAULT)), V2_DEFAULT_DIGEST)
        policy = normalize({'roles': {'author': ['codex', 'claude'], 'reviewer': ['claude', 'codex']}})
        self.assertTrue(all(set(e) == {'provider', 'profile'} for c in policy['roles'].values() for e in c))
        self.assertEqual(policy['context_continuation_providers'], ['codex'])

    def test_accounts_models_and_chain_length(self):
        fields = normalize({'roles': {'author': [{'provider': 'codex', 'profile': 'WRITE', 'account': 'is'}, 'codex'],
                                      'reviewer': ['claude']}})
        self.assertEqual(fields['roles']['author'], [{'provider': 'codex@is', 'profile': 'WRITE'},
                                                     {'provider': 'codex', 'profile': 'WRITE'}])
        self.assertEqual(normalize({'roles': {'author': ['codex@is'], 'reviewer': ['claude']}})['roles']['author'],
                         fields['roles']['author'][:1])     # inline and field forms freeze the same policy
        four = {'roles': {'author': ['codex', 'codex@a', 'codex@b', 'claude'], 'reviewer': ['claude']}}
        self.assertEqual(len(normalize(four)['roles']['author']), 4)
        refused = {
            'five': {'author': ['codex', 'codex@a', 'codex@b', 'codex@c', 'claude']},
            'same identity': {'author': ['codex@a', 'codex@a']},
            'claude account': {'author': ['claude@x']},
            'codex effort without model': {'author': ['codex~high']},
            'mixed forms': {'author': [{'provider': 'codex@a', 'profile': 'WRITE', 'account': 'b'}]},
            'bad account': {'author': [{'provider': 'codex', 'profile': 'WRITE', 'account': 'A'}]},
            'unknown key': {'author': [{'provider': 'codex', 'profile': 'WRITE', 'home': '/tmp'}]},
        }
        for name, roles in refused.items():
            with self.subTest(name), self.assertRaises(ValueError):
                normalize({'roles': dict({'reviewer': ['claude']}, **roles)})
        with patch.dict(provider_policy.CAPABILITIES, MULTI):
            two_models = normalize({'roles': {'author': ['multi@h1/sonnet', 'multi@h1/pro', 'multi@h2/pro'],
                                              'reviewer': ['claude']}})
            self.assertEqual([e['provider'] for e in two_models['roles']['author']],
                             ['multi@h1/sonnet', 'multi@h1/pro', 'multi@h2/pro'])

    def test_team_problems_and_warnings(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'providers.yaml'
        path.write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n'
                        'allowed:\n  author: [codex, codex@a, codex@b, codex@c, claude]\n  reviewer: [claude, codex@a]\n')
        config = load_config(path)
        reasons = lambda team: [(p['role'], p['provider'], p['reason']) for p in team_problems(config, team)]
        self.assertEqual(reasons({'author': ['codex', 'codex@a', 'codex@b', 'claude'], 'reviewer': ['claude']}), [])
        self.assertEqual(reasons({'author': ['claude@x'], 'reviewer': ['claude']}), [('author', 'claude@x', 'account_unsupported')])
        self.assertEqual(reasons({'author': ['codex/m'], 'reviewer': ['claude']}), [])   # TB-15: allowed codex covers models
        self.assertEqual(reasons({'author': ['codex~high'], 'reviewer': ['claude']}), [('author', 'codex~high', 'model_required')])
        self.assertEqual(reasons({'author': ['codex@zz'], 'reviewer': ['claude']}), [('author', 'codex@zz', 'provider_not_allowed')])
        self.assertEqual(reasons({'author': ['codex', 'codex@a', 'codex@b', 'codex@c', 'claude'], 'reviewer': ['claude']}),
                         [('author', '', 'chain_length')])
        self.assertEqual(team_warnings({'author': ['codex@a'], 'reviewer': ['codex@b']}), ['same_provider_both_roles'])
        self.assertEqual(team_warnings({'author': ['codex@a'], 'reviewer': ['claude']}), [])
        path.write_text('roles:\n  author: [codex]\n  reviewer: [claude]\nallowed:\n  author: [codex, claude@x]\n  reviewer: [claude]\n')
        with self.assertRaises(ValueError):
            load_config(path)


class Homes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'

    def test_each_account_has_its_own_codex_home(self):
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_HOME': '/override'}):
            self.assertEqual(_get_adapter('codex', self.db).home, '/override')
            self.assertEqual(_get_adapter('codex@is', self.db).home, str(Path(self.tmp.name) / 'codex-home-is'))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CONDUCTOR_CODEX_HOME', None)
            self.assertEqual(_get_adapter('codex', self.db).home, str(Path(self.tmp.name) / 'codex-home'))

    def test_session_check_is_cached_per_identity(self):
        seen = []
        def probe(provider, binary, home=None, model=None):
            seen.append((provider, home)); return ('authenticated' if home.endswith('-a') else 'unauthenticated'), True
        with patch.object(provider_auth, '_probe', probe), patch.dict(provider_auth._cache, clear=True):
            self.assertEqual(provider_auth.check('codex@a', '/bin/codex', '/h/codex-home-a')[0], 'authenticated')
            self.assertEqual(provider_auth.check('codex', '/bin/codex', '/h/codex-home')[0], 'unauthenticated')
            self.assertEqual(provider_auth.check('codex@a', '/bin/codex', '/h/codex-home-a')[0], 'authenticated')   # cached
        self.assertEqual(seen, [('codex', '/h/codex-home-a'), ('codex', '/h/codex-home')])


class Waterfall(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.ws = root / 'workspace'; self.ws.mkdir()
        self.db = root / 'state.db'; self.conn = store.connect(self.db); self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def create(self, roles, chain):
        frozen = normalize({'roles': {'author': chain, 'reviewer': ['claude']}})
        with store.transaction(self.conn):
            repo.create_session(self.conn, 's', str(self.ws), 'auto', policy=frozen)
            for i, role in enumerate(roles):
                repo.add_session_step(self.conn, f's{i}', 's', i, frozen['roles'][role][0]['provider'], 'goal', role=role)

    def attempts(self):
        return [(a['provider'], a['status']) for a in self.conn.execute('SELECT * FROM session_attempts ORDER BY started_at, attempt_no')]

    async def test_three_quotas_reach_the_fourth_link_and_the_next_step_starts_at_the_top(self):
        chain = ['codex', 'codex@a', 'codex@b', 'claude']
        self.create(('author', 'reviewer', 'author'), chain)
        log = []
        factory = Scripted(log, {'codex': [quota()], 'codex@a': [quota()], 'codex@b': [quota()]})
        await SessionRunner(self.db, adapter_factory=factory).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'SUCCEEDED')
        self.assertEqual(self.attempts(), [('codex', 'QUOTA'), ('codex@a', 'QUOTA'), ('codex@b', 'QUOTA'),
                                           ('claude', 'COMPLETED'), ('claude', 'COMPLETED'), ('codex', 'COMPLETED')])
        moves = [(p['from'], p['to']) for p in (json.loads(r[0]) for r in
                 self.conn.execute("SELECT payload FROM session_events WHERE kind='handoff' ORDER BY seq"))]
        self.assertEqual(moves, [('codex', 'codex@a'), ('codex@a', 'codex@b'), ('codex@b', 'claude')])
        self.assertEqual([e['provider'] for e in log], ['codex', 'codex@a', 'codex@b', 'claude', 'claude', 'codex'])

    async def test_four_quotas_wait_for_capacity(self):
        self.create(('author',), ['codex', 'codex@a', 'codex@b', 'claude'])
        factory = Scripted([], {p: [quota()] for p in ('codex', 'codex@a', 'codex@b', 'claude')})
        await SessionRunner(self.db, adapter_factory=factory).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'WAITING_CAPACITY')
        self.assertEqual([p for p, _ in self.attempts()], ['codex', 'codex@a', 'codex@b', 'claude'])

    async def test_auth_failure_does_not_hand_off(self):
        self.create(('author',), ['codex', 'codex@a'])
        auth = AdapterResult('login', None, None, 'error', True, 'failed', 1, 'AUTH')
        await SessionRunner(self.db, adapter_factory=Scripted([], {'codex': [auth]})).run_session('s')
        self.assertEqual(repo.get_session(self.conn, 's')['status'], 'WAITING_AUTH')
        self.assertEqual([p for p, _ in self.attempts()], ['codex'])


class ResumeIdentity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.db = root / 'state.db'; work_dir = root / 'ws'; work_dir.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'demo', str(work_dir))

    async def run_message(self, text, factory):
        policy = normalize({'roles': {'author': ['codex@a', 'codex@b'], 'reviewer': ['claude']}})
        ws.add_message(self.conn, self.wid, str(uuid4()), text, digest(policy), policy, digest(policy))
        sid, _ = ws.dispatch_next(self.conn, self.wid)
        await SessionRunner(self.db, adapter_factory=factory).run_session(sid)
        row = self.conn.execute('SELECT decision, reason FROM work_resume_refs WHERE session_id=?', (sid,)).fetchone()
        return tuple(row)

    async def test_another_account_cannot_continue_the_thread(self):
        # Both author steps of run 1 fall back to codex@b, so its thread belongs to codex@b, not chain[0].
        await self.run_message('one', Scripted([], {'codex@a': [quota(), quota()]}))
        self.assertEqual(await self.run_message('two', Scripted([], {})), ('FRESH', 'identity_changed'))
        self.assertEqual(await self.run_message('three', Scripted([], {})), ('RESUME', 'all_gates_passed'))


if __name__ == '__main__':
    unittest.main()
