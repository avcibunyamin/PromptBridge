"""TB-10 (K-023): provider/model/effort catalog, effort identities, Claude --model/--effort and model check.

Fake executables only (no keychain, no provider quota); the real turns are DoD 7.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import catalog, provider_policy
from app.adapters.claude import ClaudeAdapter, model_matches
from app.provider_policy import (DEFAULT, covers, digest, effort_of, load_config, normalize, parse, split, team_problems)
from app.runner import _get_adapter
from tests.test_f303_team import V2_DEFAULT_DIGEST
from tests.test_tb08_agy import FAKE_SECURITY

CLAUDE = '''import json, os, sys
with open(os.environ['FAKE_CLAUDE_LOG'], 'w') as f:
    json.dump(sys.argv[1:], f)
sys.stdin.read()
model = os.environ.get('FAKE_CLAUDE_MODEL', 'claude-sonnet-5-5')
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's1', 'model': model}))
print(json.dumps({'type': 'result', 'session_id': 's1', 'is_error': False, 'subtype': 'success', 'result': 'ok'}))
'''

AGY_MODELS = '''import os, time
if os.environ.get('FAKE_AGY_HANG'):
    tmp = os.environ['FAKE_AGY_HANG'] + '.tmp'
    with open(tmp, 'w') as f:
        f.write(str(os.getpid()))
    os.replace(tmp, os.environ['FAKE_AGY_HANG'])
    time.sleep(60)
import sys
if os.environ.get('FAKE_AGY_SIGNED_OUT'):
    print('Error: Please sign in to view available models.'); sys.exit(1)
print('Fetching available models...')
print('--flag\\tX')
print('gemini-3.1-pro-high\\tGemini 3.1 Pro (High)')
print('claude-sonnet-5-5-high\\tClaude Sonnet 5.5 (High)')
'''

CODEX_MODELS = '''import json, sys, os, time
pages = {
 None: {'data': [{'id': 'gpt-6-sol', 'supportedReasoningEfforts': [{'reasoningEffort': e} for e in ['low','medium','high','xhigh','max','ultra']],
                  'defaultReasoningEffort': 'medium', 'displayName': 'GPT-6 Sol'}], 'nextCursor': 'p2'},
 'p2': {'data': ['not-a-dict', {'id': 'gpt-5.5', 'supportedReasoningEfforts': ['low','medium','high','xhigh'],
                  'defaultReasoningEffort': 'low'}], 'nextCursor': None},
}
def emit(m):
 sys.stdout.write(json.dumps(m)+'\\n'); sys.stdout.flush()
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method') == 'initialized':
  continue
 if os.environ.get('FAKE_CODEX_HANG') and m.get('method') == 'model/list':
  time.sleep(60)
 if m.get('method') == 'initialize':
  emit({'id': m['id'], 'result': {}})
 elif m.get('method') == 'model/list':
  if os.environ.get('FAKE_CODEX_ERROR'):
   emit({'id': m['id'], 'error': {'code': -32000, 'message': 'not logged in'}})
  else:
   emit({'id': m['id'], 'result': pages[m.get('params', {}).get('cursor')]})
'''


def script(path, body):
    path.write_text('#!' + sys.executable + '\n' + body)
    path.chmod(0o755)
    return str(path)


class Identities(unittest.TestCase):
    def test_effort_dimension_and_compatibility(self):
        self.assertEqual(split('claude/opus~high'), ('claude', None, 'opus', 'high'))
        self.assertEqual(split('agy@h1/gemini-3.1-pro-high'), ('agy', 'h1', 'gemini-3.1-pro-high', None))
        self.assertEqual(effort_of('claude~max'), 'max')
        self.assertEqual(digest(normalize(DEFAULT)), V2_DEFAULT_DIGEST)                 # plain policies unchanged
        fields = normalize({'roles': {'author': [{'provider': 'claude', 'profile': 'WRITE', 'model': 'opus', 'effort': 'high'}],
                                      'reviewer': ['claude']}})
        self.assertEqual(fields['roles']['author'], [{'provider': 'claude/opus~high', 'profile': 'WRITE'}])
        for bad in ('agy@h1/gemini-3.1-pro-high~high', 'codex~high', 'claude~ultra', 'claude~H', 'claude/opus~'):
            with self.subTest(bad), self.assertRaises(ValueError):
                normalize({'roles': {'author': [bad], 'reviewer': ['claude']}})
        with self.assertRaises(ValueError):                                                # inline and field forms never mix
            normalize({'roles': {'author': [{'provider': 'claude~high', 'profile': 'WRITE', 'model': 'opus'}], 'reviewer': ['claude']}})

    def test_allowed_entries_without_model_or_effort_are_wildcards(self):
        self.assertTrue(covers('claude', 'claude/opus~high'))
        self.assertTrue(covers('agy@h1', 'agy@h1/gemini-3.1-pro-high'))
        self.assertFalse(covers('agy@h1', 'agy@h2/gemini-3.1-pro-high'))                   # the account always matches
        self.assertFalse(covers('claude/opus', 'claude/sonnet'))
        self.assertTrue(covers('claude/opus', 'claude/opus~low'))
        self.assertFalse(covers('claude/opus~low', 'claude/opus~high'))
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'providers.yaml'
        path.write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n'
                        'allowed:\n  author: [claude, agy@h1]\n  reviewer: [claude]\n')
        config = load_config(path)                                                         # a model-less agy ceiling is valid
        reasons = lambda team: [p['reason'] for p in team_problems(config, team)]
        self.assertEqual(reasons({'author': ['claude/opus~high', 'agy@h1/gemini-3.1-pro-high'], 'reviewer': ['claude']}), [])
        self.assertEqual(reasons({'author': ['agy@h1'], 'reviewer': ['claude']}), ['model_required'])
        self.assertEqual(reasons({'author': ['agy@h2/x'], 'reviewer': ['claude']}), ['provider_not_allowed'])
        self.assertEqual(reasons({'author': ['claude~ultra'], 'reviewer': ['claude']}), ['effort_unsupported'])

    def test_old_identities_keep_their_meaning(self):
        self.assertEqual(parse('agy@h1/gemini-3.1-pro-high'), ('agy', 'h1', 'gemini-3.1-pro-high'))   # callers keep a 3-tuple
        self.assertEqual(parse('claude/opus~high'), ('claude', None, 'opus'))
        for old in ('claude', 'codex', 'codex@is', 'agy@h1/gemini-3.1-pro-high'):
            self.assertIsNone(effort_of(old))
        legacy = {'roles': {'author': ['claude', 'codex@is', 'agy@h1/gemini-3.1-pro-high'], 'reviewer': ['codex']}}
        self.assertEqual([i['provider'] for i in normalize(legacy)['roles']['author']],
                         ['claude', 'codex@is', 'agy@h1/gemini-3.1-pro-high'])          # same string, so the same digest
        self.assertNotIn('~', json.dumps(normalize(legacy)))

    def test_fixed_model_or_effort_in_allowed_does_not_widen(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'providers.yaml'

        def config(allowed, roles='[claude/opus~high]'):
            path.write_text(f'roles:\n  author: {roles}\n  reviewer: [claude]\nallowed:\n  author: {allowed}\n  reviewer: [claude]\n')
            return load_config(path)
        reasons = lambda c, *author: [p['reason'] for p in team_problems(c, {'author': list(author), 'reviewer': ['claude']})]
        fixed = config('[claude/opus]')
        self.assertEqual(reasons(fixed, 'claude/opus~low'), [])                          # effort open, model fixed
        self.assertEqual(reasons(fixed, 'claude/sonnet~high'), ['provider_not_allowed'])
        self.assertEqual(reasons(fixed, 'claude'), ['provider_not_allowed'])             # no model is not "opus"
        effort = config('[claude~high]')
        self.assertEqual(reasons(effort, 'claude/sonnet~high'), [])                      # model open, effort fixed
        self.assertEqual(reasons(effort, 'claude/sonnet~max'), ['provider_not_allowed'])
        with self.assertRaises(ValueError):                                                # the default team must fit the ceiling
            config('[claude/sonnet]')
        for bad in ('[claude~ultra]', '[agy@h1~high]', '[codex~high]'):                  # ultra stays off; codex effort needs a model
            with self.subTest(bad), self.assertRaises(ValueError):
                config(bad, roles='[claude]')


class ClaudeModel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.log = self.root / 'argv.json'
        env = {'CONDUCTOR_CLAUDE_BIN': script(self.root / 'claude', CLAUDE), 'FAKE_CLAUDE_LOG': str(self.log)}
        patcher = patch.dict(os.environ, env); patcher.start(); self.addCleanup(patcher.stop)

    async def run_claude(self, adapter, running='claude-sonnet-5-5'):
        with patch.dict(os.environ, {'FAKE_CLAUDE_MODEL': running}):
            events = [e async for e in adapter.run('goal', str(self.root), None, profile='READ_ONLY')]
        return events, json.loads(self.log.read_text())

    def test_alias_and_full_name_matching(self):
        yes = [('sonnet', 'claude-sonnet-5-5'), ('sonnet', 'claude-3-5-sonnet-20241022'), ('haiku', 'claude-haiku-4-5-20251001'),
               ('opus', 'claude-opus-5-5[1m]'), ('claude-opus-5-5', 'claude-opus-5-5')]
        no = [('sonnet', 'claude-sonnetx-5'), ('sonnet', 'claude-opus-5-5'), ('opus', 'gpt-opus'), ('claude-opus-5-5', 'claude-opus-5-6'),
              ('sonnet', None)]
        for wanted, running in yes:
            self.assertTrue(model_matches(wanted, running), (wanted, running))
        for wanted, running in no:
            self.assertFalse(model_matches(wanted, running), (wanted, running))

    async def test_model_and_effort_reach_the_cli_and_are_checked(self):
        events, argv = await self.run_claude(ClaudeAdapter('sonnet', 'high'))
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertFalse(any(a.startswith('--fallback-model') for a in argv))
        self.assertEqual(next(e for e in events if e['type'] == 'started')['model'], 'claude-sonnet-5-5')
        self.assertEqual(events[-1]['type'], 'completed')
        events, _ = await self.run_claude(ClaudeAdapter('opus'), running='claude-sonnet-5-5')
        self.assertEqual((events[-1]['type'], events[-1]['result'].terminal_reason), ('failed', 'model_mismatch'))
        events, argv = await self.run_claude(ClaudeAdapter(), running='anything')      # no model asked: no check
        self.assertNotIn('--model', argv); self.assertNotIn('--effort', argv)
        self.assertEqual(events[-1]['type'], 'completed')

    def test_identity_reaches_the_adapter(self):
        adapter = _get_adapter('claude/opus~xhigh', '/x/.system/state.db')
        self.assertEqual((adapter.model, adapter.effort), ('opus', 'xhigh'))


class Catalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.system').mkdir()
        self.db = str(self.root / '.system/state.db')
        env = {'CONDUCTOR_AGY_BIN': script(self.root / 'agy', AGY_MODELS),
               'CONDUCTOR_CODEX_BIN': script(self.root / 'codex', CODEX_MODELS),
               'CONDUCTOR_SECURITY_BIN': script(self.root / 'security', FAKE_SECURITY), 'FAKE_SECURITY_LOG': str(self.root / 'sec.log')}
        patcher = patch.dict(os.environ, env); patcher.start(); self.addCleanup(patcher.stop)
        saved = dict(catalog._cache); self.addCleanup(lambda: catalog._cache.update(saved))
        catalog._cache.update(entries=None, errors={}, checked_at=None)
        self.config = {'roles': {'author': ['claude', 'agy@h1/gemini-3.1-pro-high'], 'reviewer': ['claude']},
                       'allowed': {'author': ['claude', 'agy@h1', 'agy@h2', 'codex@is'], 'reviewer': ['claude', 'codex']}}

    def test_snapshot_before_refresh_is_the_claude_fallback_only(self):
        snap = catalog.snapshot()
        self.assertEqual({e['identity'] for e in snap['entries']},
                         {'claude/claude-opus-5-5', 'claude/claude-sonnet-5-5', 'claude/claude-haiku-5-5', 'claude/claude-haiku-4-5-20251001',
                          'claude/claude-fable-5-1', 'claude/fable', 'claude/opus', 'claude/sonnet', 'claude/haiku'})
        self.assertTrue(all(e['status'] == 'unverified' and e['source'] == 'fallback' for e in snap['entries']))
        self.assertEqual(next(e for e in snap['entries'] if e['identity'] == 'claude/claude-opus-5-5')['display_name'],
                         'Claude Opus 5.5')
        self.assertNotIn('default_effort', next(e for e in snap['entries'] if e['identity'] == 'claude/claude-opus-5-5'))
        self.assertEqual(next(e for e in snap['entries'] if e['identity'] == 'claude/claude-fable-5-1')['display_name'],
                         'Claude Fable 5.1')
        self.assertEqual(snap['efforts']['claude'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertEqual(snap['efforts']['agy'], [])
        self.assertEqual(snap['efforts']['codex'], ['low', 'medium', 'high', 'xhigh', 'max'])

    def test_refresh_lists_each_agy_account_in_its_own_home(self):
        with patch.dict(os.environ, {}):
            snap = catalog.refresh(self.db, self.config)
        live = sorted(e['identity'] for e in snap['entries'] if e['source'] == 'live')
        self.assertEqual(live, ['agy@h1/claude-sonnet-5-5-high', 'agy@h1/gemini-3.1-pro-high',
                                'agy@h2/claude-sonnet-5-5-high', 'agy@h2/gemini-3.1-pro-high',
                                'codex/gpt-5.5', 'codex/gpt-6-sol', 'codex@is/gpt-5.5', 'codex@is/gpt-6-sol'])
        sol = next(e for e in snap['entries'] if e['identity'] == 'codex@is/gpt-6-sol')
        self.assertEqual(sol['efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])        # ultra filtered out
        self.assertEqual(sol['default_effort'], 'medium')
        self.assertEqual(sol['display_name'], 'GPT-6 Sol')
        self.assertEqual(next(e for e in snap['entries'] if e['identity'] == 'codex@is/gpt-5.5')['default_effort'], 'low')
        homes = {json.loads(l)['home'] for l in (self.root / 'sec.log').read_text().splitlines()}
        self.assertEqual(homes, {str(self.root / '.system/agy-home-h1'), str(self.root / '.system/agy-home-h2')})
        self.assertEqual(catalog.status('agy@h1/gemini-3.1-pro-high'), 'available')
        self.assertEqual(catalog.status('codex@is/gpt-6-sol~high'), 'available')
        self.assertIsNone(catalog.problem('codex@is/gpt-6-sol~high'))
        self.assertEqual(catalog.problem('codex@is/gpt-5.5~max'), 'effort_unsupported')
        self.assertEqual(catalog.problem('codex@is/gone~low'), 'model_unavailable')
        self.assertEqual(catalog.status('agy@h1/claude-sonnet-4-6'), 'model_unavailable')
        self.assertEqual(catalog.status('claude/opus~high'), 'unverified')
        self.assertEqual(catalog.status('claude/claude-opus-5-5'), 'unverified')     # explicit fallback, still checked at run time

    def test_signed_out_account_is_reported_not_listed(self):
        with patch.dict(os.environ, {'FAKE_AGY_SIGNED_OUT': '1'}):
            snap = catalog.refresh(self.db, self.config)
        self.assertEqual(snap['errors'], {'agy@h1': 'unauthenticated', 'agy@h2': 'unauthenticated'})
        self.assertEqual(catalog.status('agy@h1/gemini-3.1-pro-high'), 'unknown')     # unknown, not "removed"

    def test_one_account_is_relisted_after_a_sign_in(self):   # K-060
        with patch.dict(os.environ, {'FAKE_AGY_SIGNED_OUT': '1'}):
            catalog.refresh(self.db, self.config)
            self.assertEqual(catalog.refresh_base(self.db, 'agy', 'h1'), (False, 'unauthenticated'))
        self.assertEqual(catalog.refresh_base(self.db, 'agy', 'h1'), (True, None))   # signed in now
        snap = catalog.snapshot()
        self.assertEqual(snap['errors'], {'agy@h2': 'unauthenticated'})            # only h1 changed
        self.assertEqual(catalog.status('agy@h1/gemini-3.1-pro-high'), 'available')

    def test_a_hanging_listing_times_out_and_its_group_is_gone(self):
        from app import provider_auth
        from app.adapters.process import identity
        pidfile = self.root / 'hang.pid'
        with patch.dict(os.environ, {'FAKE_AGY_HANG': str(pidfile)}), patch.dict(provider_auth.SLOW, {'agy': 3.0}):
            snap = catalog.refresh(self.db, {'roles': {'author': ['agy@h1/x'], 'reviewer': ['claude']}})
        self.assertEqual(snap['errors'], {'agy@h1': 'timeout'})
        self.assertEqual({e['source'] for e in snap['entries']}, {'fallback'})               # Claude is still offered
        # Missing pidfile means timeout killed Python before it wrote its PID (not unstarted), so timeout + fallback suffice.
        if pidfile.exists():
            self.assertIsNone(identity(int(pidfile.read_text())))                     # the hung process was killed

    def test_missing_binary_is_unavailable_not_an_error(self):
        with patch.dict(os.environ, {'CONDUCTOR_AGY_BIN': str(self.root / 'nope')}):
            snap = catalog.refresh(self.db, self.config)
        self.assertEqual(snap['errors'], {'agy@h1': 'unavailable', 'agy@h2': 'unavailable'})

    def test_snapshot_is_served_from_memory(self):
        first = catalog.refresh(self.db, self.config)
        calls = (self.root / 'sec.log').read_text()
        os.remove(os.environ['CONDUCTOR_AGY_BIN'])                                        # nothing can be listed any more
        self.assertEqual(catalog.snapshot(), first)
        self.assertEqual((self.root / 'sec.log').read_text(), calls)                     # no keychain/home work either
        self.assertNotIn(str(self.root), json.dumps(first))                               # no home path, no account detail

    def test_bare_agy_is_not_listed_and_gets_no_home(self):
        config = {'roles': {'author': ['claude'], 'reviewer': ['claude']}, 'allowed': {'author': ['claude', 'agy'], 'reviewer': ['claude']}}
        snap = catalog.refresh(self.db, config)
        self.assertEqual(snap['errors'], {})
        self.assertFalse((self.root / '.system/agy-home').exists())
        self.assertFalse((self.root / 'sec.log').exists())

    def test_malformed_agy_and_codex_model_rows_are_skipped(self):
        agy_entries, agy_err = catalog._agy(self.db, 'h1')
        self.assertIsNone(agy_err)
        self.assertEqual([e['model'] for e in agy_entries], ['gemini-3.1-pro-high', 'claude-sonnet-5-5-high'])
        codex_entries, codex_err = catalog._codex(self.db, 'codex@is')
        self.assertIsNone(codex_err)
        self.assertEqual([e['model'] for e in codex_entries], ['gpt-6-sol', 'gpt-5.5'])

    def test_codex_listing_errors_are_bounded(self):
        with patch.dict(os.environ, {'FAKE_CODEX_ERROR': '1'}):
            snap = catalog.refresh(self.db, {'roles': {'author': ['codex@is/gpt-6-sol'], 'reviewer': ['claude']}})
        self.assertEqual(snap['errors'], {'codex@is': 'unauthenticated'})
        self.assertEqual(catalog.status('codex@is/gpt-6-sol'), 'unknown')
        from app import provider_auth
        with patch.dict(os.environ, {'FAKE_CODEX_HANG': '1'}), patch.dict(provider_auth.SLOW, {'codex': 1.0}):
            snap = catalog.refresh(self.db, {'roles': {'author': ['codex@is/gpt-6-sol'], 'reviewer': ['claude']}})
        self.assertEqual(snap['errors'], {'codex@is': 'timeout'})

    def test_codex_unknown_cache_does_not_claim_model_removed_and_preflight_refreshes_one_base(self):
        self.assertIsNone(catalog.problem('codex@is/gpt-5.5~xhigh'))
        self.assertEqual(catalog.status('codex@is/gpt-5.5~xhigh'), 'unknown')
        self.assertIsNone(catalog.validate_identity(self.db, 'codex@is/gpt-5.5~xhigh'))
        self.assertEqual(catalog.problem('codex@is/gpt-5.5~max'), 'effort_unsupported')
        self.assertEqual(catalog.problem('codex@is/missing~low'), 'model_unavailable')
        live = sorted(e['identity'] for e in catalog.snapshot()['entries'] if e['source'] == 'live')
        self.assertEqual(live, ['codex@is/gpt-5.5', 'codex@is/gpt-6-sol'])

    def test_codex_preflight_retries_a_cached_listing_error(self):
        catalog._cache.update(entries=catalog._claude(), errors={'codex@is': 'timeout'}, checked_at=time.time())
        self.assertIsNone(catalog.validate_identity(self.db, 'codex@is/gpt-6-sol~high'))
        snap = catalog.snapshot()
        self.assertNotIn('codex@is', snap['errors'])
        self.assertIn('codex@is/gpt-6-sol', [e['identity'] for e in snap['entries']])
        catalog._cache.update(entries=catalog._claude(), errors={'codex@is': 'timeout'}, checked_at=time.time())
        with patch.dict(os.environ, {'FAKE_CODEX_ERROR': '1'}):
            self.assertEqual(catalog.validate_identity(self.db, 'codex@is/gpt-6-sol~high'), 'unauthenticated')
        self.assertEqual(catalog.snapshot()['errors'], {'codex@is': 'unauthenticated'})

    def test_api_serves_the_cache_and_guards_the_refresh(self):
        from app.api import create_app
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n'
                                                          'allowed:\n  author: [claude, agy@h1, codex@is]\n  reviewer: [claude]\n')
        with patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}):
            app = create_app(); app.state.runner_db = self.db
            client = TestClient(app)
            self.assertEqual(len(client.get('/api/sessions/catalog').json()['entries']), 9)
            self.assertEqual(client.post('/api/sessions/catalog/refresh', headers={'Origin': 'https://evil.example'}).status_code, 403)
            body = client.post('/api/sessions/catalog/refresh').json()
            preview = client.get('/api/sessions/providers/preview',
                                 params={'author': 'claude/opus~high,agy@h1/gemini-3.1-pro-high,agy@h1/gone-model,codex@is/gpt-6-sol~high',
                                         'reviewer': 'claude'}).json()
            invalid = client.get('/api/sessions/providers/preview',
                                 params={'author': 'codex@is/gpt-5.5~max', 'reviewer': 'claude'})
            (self.root / '.system/providers.yaml').write_text('roles: [broken\n')
            broken = client.post('/api/sessions/catalog/refresh')
        self.assertIn('agy@h1/gemini-3.1-pro-high', [e['identity'] for e in body['entries']])
        self.assertIn('codex@is/gpt-6-sol', [e['identity'] for e in body['entries']])
        self.assertIsNotNone(body['checked_at'])
        self.assertEqual(preview['catalog'], {'claude/opus~high': 'unverified', 'agy@h1/gemini-3.1-pro-high': 'available',
                                              'agy@h1/gone-model': 'model_unavailable',
                                              'codex@is/gpt-6-sol~high': 'available', 'claude': 'unknown'})
        self.assertEqual((invalid.status_code, invalid.json()['code']), (422, 'team_invalid'))
        self.assertEqual(invalid.json()['problems'], [{'role': 'author', 'provider': 'codex@is/gpt-5.5~max',
                                                       'reason': 'effort_unsupported'}])
        self.assertEqual(broken.status_code, 422)
        self.assertEqual(set(broken.json()), {'detail', 'code'})


if __name__ == '__main__':
    unittest.main()
