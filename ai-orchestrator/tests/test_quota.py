"""K-039: quota readings (agy /usage in a pty, Codex rate limits, Claude rate_limit_event) and their API."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import quota
from app.adapters.claude import _with_window
from tests.test_tb08_agy import FAKE_SECURITY

# agy 1.4.2 draws rows with cursor moves (ESC[r;cH), not newlines, and writes the first title over a spinner with \r.
SCREEN = ('\x1b[2J/usage\x1b[5;1H└ Models & Quota\x1b[6;3HAccount: a@example.com\x1b[7;1H  ⣷  Loading quota summary...\r\n\x1b[3A  ⣯ \x08\x08⣟ \x08\x08⡿ \r\x1b[1mGEMINI MODELS\x1b[m\x1b[K\r\n'
          '\x1b[8;3HModels within this group: Gemini Flash, Gemini Pro\x1b[9;3HWeekly Limit Remaining'
          '\x1b[10;5H[████░] 99.38%\x1b[11;1HRefreshes in 167h 25m\x1b[12;3HFive Hour Limit Remaining'
          '\x1b[13;1H[███░] 97.83%\x1b[14;1HRefreshes in 4h 25m\x1b[15;1HCLAUDE AND GPT MODELS'
          '\x1b[16;3HModels within this group: Claude Opus, Claude Sonnet, GPT-OSS\x1b[17;3HWeekly Limit Remaining'
          '\x1b[18;1H[█████] 100.00%\x1b[19;1HQuota available\x1b[20;3HFive Hour Limit Remaining\x1b[21;1H[█████] 0.00%'
          '\x1b[22;1HRefreshes in 2h 1m\x1b[23;1HGEMINI 4 ARGON\x1b[24;3HFor paid Pro plans, Argon quota is separate.'
          '\x1b[25;3HGemini 4 Argon Weekly Limit\x1b[26;1H[████░] 96.06%\x1b[27;1HRefreshes in 167h 22m'
          '\x1b[28;3H│Within each group, models share a weekly limit\x1b[29;1H↑/↓ Scroll · pgup/pgdown Page')

FAKE_AGY = '''import os, sys, termios, tty
if os.environ.get('FAKE_AGY_LOG'):
    open(os.environ['FAKE_AGY_LOG'], 'a').write(os.getcwd() + ' ' + ' '.join(sys.argv[1:]) + '\\n')
mode = os.environ.get('FAKE_AGY_MODE', 'ok')
out = sys.stdout
if mode == 'signin':
    out.write('Welcome. You are currently not signed in.\\n Select login method:\\n > 1. Google OAuth\\n'); out.flush()
    sys.stdin.read(1); sys.exit(0)
out.write('Welcome to the Antigravity CLI. You are currently not signed in.\\n Signing in...\\n')
if mode in ('trust', 'trust_signin'):
    out.write('Do you trust the contents of this project?\\n > Yes, I trust this folder\\n'); out.flush()
    tty.setraw(0)
    if os.read(0, 1) != b'\\r':
        sys.exit(3)
    if mode == 'trust_signin':
        out.write(' Select login method:\\n > 1. Google OAuth\\n'); out.flush()
        while os.read(0, 64):
            pass
out.write('>\\n? for shortcuts\\n'); out.flush()
tty.setraw(0)
typed = b''
while not typed.endswith(b'\\r'):
    typed += os.read(0, 64)
if b'/usage' in typed:
    out.write(os.environ['FAKE_AGY_SCREEN']); out.flush()
while os.read(0, 64):
    pass
'''


class Quota(unittest.TestCase):
    def setUp(self):
        quota._cache.clear(); quota._running.clear()
        self.root = Path(tempfile.mkdtemp())
        (self.root / '.system').mkdir()
        self.db = str(self.root / '.system/state.db')
        bins = self.root / 'bin'; bins.mkdir()
        for name, body in (('agy', FAKE_AGY), ('security', FAKE_SECURITY)):
            (bins / name).write_text('#!' + sys.executable + '\n' + body); (bins / name).chmod(0o755)
        self.env = patch.dict(os.environ, {'CONDUCTOR_AGY_BIN': str(bins / 'agy'), 'CONDUCTOR_SECURITY_BIN': str(bins / 'security'),
                                           'FAKE_SECURITY_LOG': str(self.root / 'security.log'), 'FAKE_AGY_SCREEN': SCREEN,
                                           'FAKE_AGY_LOG': str(self.root / 'agy.log')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_parses_the_usage_screen_into_three_groups(self):
        groups = quota.parse_agy_usage(SCREEN)
        self.assertEqual([g['key'] for g in groups], ['gemini', 'claude_gpt', 'argon'])
        gemini, claude, argon = groups
        self.assertEqual(gemini['models'], 'Gemini Flash, Gemini Pro')
        self.assertEqual([(w['kind'], w['remaining'], w['resets_in']) for w in gemini['windows']],
                         [('weekly', 99.38, '167h 25m'), ('five_hour', 97.83, '4h 25m')])
        self.assertEqual([(w['kind'], w['remaining'], w['resets_in']) for w in claude['windows']],
                         [('weekly', 100.0, None), ('five_hour', 0.0, '2h 1m')])
        self.assertEqual([(w['kind'], w['remaining']) for w in argon['windows']], [('weekly', 96.06)])
        self.assertEqual([w['label'] for w in gemini['windows']] + [argon['windows'][0]['label']], ['Haftalık', '5 saat', 'Haftalık'])
        # A redraw repeats the screen: groups and windows are not duplicated.
        self.assertEqual(quota.parse_agy_usage(SCREEN + SCREEN), groups)
        with self.assertRaises(ValueError):
            quota.parse_agy_usage('? for shortcuts\nsomething else entirely')

    def test_reads_agy_in_a_pty_without_typing_a_message(self):
        groups, error = quota._agy(self.db, 'h1')
        self.assertIsNone(error)
        self.assertEqual([g['key'] for g in groups], ['gemini', 'claude_gpt', 'argon'])
        cwd, *args = (self.root / 'agy.log').read_text().split()
        self.assertEqual(args, [])                                       # plain interactive agy, no -p prompt
        self.assertEqual(cwd, os.path.realpath(self.root / '.system/agy-probe-h1'))
        self.assertEqual(os.listdir(cwd), [])
        self.assertTrue((self.root / '.system/agy-home-h1/Library/Keychains/login.keychain-db').exists())

    def test_answers_the_trust_question_only_for_its_own_folder_and_detects_sign_in(self):
        with patch.dict(os.environ, {'FAKE_AGY_MODE': 'trust'}):
            self.assertIsNone(quota._agy(self.db, 'h1')[1])
        with patch.dict(os.environ, {'FAKE_AGY_MODE': 'signin'}):
            self.assertEqual(quota._agy(self.db, 'h2'), ([], 'unauthenticated'))
        with patch.dict(os.environ, {'FAKE_AGY_MODE': 'trust_signin'}):
            started = time.monotonic()
            self.assertEqual(quota._agy(self.db, 'h4'), ([], 'unauthenticated'))     # sign-in after the trust question
            self.assertLess(time.monotonic() - started, 8)
        probe = self.root / '.system/agy-probe-h3'
        probe.mkdir(parents=True); (probe / 'stray.txt').write_text('x')
        self.assertEqual(quota._agy(self.db, 'h3'), ([], 'probe_dir_not_empty'))

    def test_a_missing_binary_leaks_no_terminal(self):
        before = len(os.listdir('/dev/fd'))
        with patch.dict(os.environ, {'CONDUCTOR_AGY_BIN': str(self.root / 'no-such-agy')}):
            for _ in range(5):
                self.assertEqual(quota._agy(self.db, 'h1'), ([], 'unavailable'))
        with patch('subprocess.Popen', side_effect=OSError('spawn failed')):
            self.assertEqual(quota._agy(self.db, 'h1'), ([], 'unavailable'))
        self.assertEqual(len(os.listdir('/dev/fd')), before)

    def test_one_reading_per_account_at_a_time(self):
        import threading
        gate, calls = threading.Event(), []
        def slow(db, account):
            calls.append(account); gate.wait(5); return [], 'unavailable'
        with patch.object(quota, '_agy', side_effect=slow):
            first = threading.Thread(target=quota._refresh_one, args=(self.db, 'agy@h1')); first.start()
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.01)
            quota._refresh_one(self.db, 'agy@h1')                                  # skipped: one is running
            self.assertEqual(quota.snapshot()['running'], ['agy@h1'])
            gate.set(); first.join(5)
        self.assertEqual(calls, ['h1'])
        self.assertEqual(quota.snapshot()['running'], [])

    def test_codex_rate_limits(self):
        groups = quota.codex_groups({'rateLimits': {'planType': 'plus', 'primary': {'usedPercent': 37, 'windowDurationMins': 300,
                                                                                    'resetsAt': 1794904630},
                                                    'secondary': {'usedPercent': 12.5, 'windowDurationMins': 10080}}})
        self.assertEqual(groups[0]['title'], 'Codex (plus)')
        self.assertEqual([(w['kind'], w['remaining'], w['resets_at']) for w in groups[0]['windows']],
                         [('five_hour', 63.0, 1794904630), ('weekly', 87.5, None)])
        with self.assertRaises(ValueError):
            quota.codex_groups({'rateLimits': {'primary': None}})

    def test_claude_rate_limit_events_are_kept_per_window(self):
        quota.record_claude({'status': 'allowed', 'rateLimitType': 'five_hour', 'resetsAt': 1794904630, 'utilization': 0.42})
        quota.record_claude({'status': 'allowed_warning', 'rateLimitType': 'seven_day', 'utilization': 81})
        quota.record_claude({'status': 'allowed', 'rateLimitType': 'five_hour', 'utilization': 0.5})
        quota.record_claude({'nonsense': True})
        windows = quota.snapshot()['accounts']['claude']['groups'][0]['windows']
        self.assertEqual([(w['id'], w['remaining'], w['status']) for w in windows],
                         [('seven_day', 19.0, 'allowed_warning'), ('five_hour', 50.0, 'allowed')])

    def test_claude_usage_gets_the_context_window(self):
        result = {'usage': {'input_tokens': 4}, 'modelUsage': {'claude-sonnet-5-5': {'contextWindow': 200000}}}
        self.assertEqual(_with_window(result, 'claude-sonnet-5-5')['context_window'], 200000)
        self.assertEqual(_with_window({'usage': {'input_tokens': 4}}, 'x'), {'input_tokens': 4})
        self.assertIsNone(_with_window({'modelUsage': {}}, 'x'))

    def test_after_run_is_throttled_and_skips_other_providers(self):
        calls = []
        quota.after_run(self.db, 'agy@h1/gemini-4-argon-high')                  # off unless `serve` turned it on
        self.assertEqual(quota._running, set())
        with patch.object(quota, '_refresh_one', side_effect=lambda db, base: calls.append(base)), patch.object(quota, 'AUTO', True):
            quota.after_run(self.db, 'agy@h1/gemini-4-argon-high')
            quota.after_run(self.db, 'claude/opus')
            quota.after_run(self.db, 'agy/gemini-3.8-flash-high')               # bare agy: no account home
            for _ in range(50):
                if not quota._running:
                    break
                time.sleep(0.02)
            quota._cache['agy@h1'] = {'checked_at': time.time()}
            quota.after_run(self.db, 'agy@h1/gemini-3.8-flash-low')           # read moments ago: skipped
            time.sleep(0.1)
        self.assertEqual(calls, ['agy@h1'])

    def test_api_serves_the_cache_guards_the_refresh_and_the_phone_only_reads(self):
        from app.api import create_app
        from app import mobile
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n'
                                                          'allowed:\n  author: [claude, agy@h1]\n  reviewer: [claude]\n')
        with patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}):
            app = create_app(); app.state.runner_db = self.db
            client = TestClient(app)
            self.assertEqual(client.get('/api/sessions/quota').json(), {'accounts': {}, 'running': []})
            self.assertEqual(client.post('/api/sessions/quota/refresh', headers={'Origin': 'https://evil.example'}).status_code, 403)
            body = client.post('/api/sessions/quota/refresh').json()
        self.assertEqual([g['key'] for g in body['accounts']['agy@h1']['groups']], ['gemini', 'claude_gpt', 'argon'])
        self.assertIsNone(body['accounts']['agy@h1']['error'])
        allowed = [(m, p.pattern) for m, p, _ in mobile._ALLOW]
        self.assertIn(('GET', r'/api/sessions/quota'), allowed)
        self.assertFalse(any(p.fullmatch('/api/sessions/quota/refresh') for m, p, _ in mobile._ALLOW if m == 'POST'))


if __name__ == '__main__':
    unittest.main()
