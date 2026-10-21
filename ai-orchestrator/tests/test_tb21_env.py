"""TB-21 step 1: processes Conductor starts get no GitHub/git/SSH identity (fake helper, no network, no quota)."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.process import spawn_owned, release_gate, without_credentials

LEAKS = {'GH_TOKEN': 'gho_fake', 'GITHUB_TOKEN': 'ghp_fake', 'GH_CONFIG_DIR': '/Users/x/.config/gh', 'GH_HOST': 'github.com',
         'GIT_ASKPASS': '/bin/askpass', 'GIT_CONFIG_PARAMETERS': "'credential.helper'='store'", 'SSH_AUTH_SOCK': '/tmp/agent.sock',
         'SSH_ASKPASS': '/bin/askpass'}


class WithoutCredentials(unittest.TestCase):
    def test_strips_identity_keeps_the_rest(self):
        env = without_credentials({**LEAKS, 'HOME': '/Users/x', 'PATH': '/usr/bin', 'CODEX_HOME': '/c'})
        self.assertEqual({k: env[k] for k in ('HOME', 'PATH', 'CODEX_HOME')}, {'HOME': '/Users/x', 'PATH': '/usr/bin', 'CODEX_HOME': '/c'})
        for k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_HOST', 'GIT_ASKPASS', 'GIT_CONFIG_PARAMETERS', 'SSH_AUTH_SOCK', 'SSH_ASKPASS'):
            self.assertNotIn(k, env)
        self.assertNotEqual(env['GH_CONFIG_DIR'], LEAKS['GH_CONFIG_DIR'])
        self.assertEqual(os.listdir(env['GH_CONFIG_DIR']), [])
        self.assertEqual((env['GIT_TERMINAL_PROMPT'], env['GIT_CONFIG_NOSYSTEM'], env['GIT_CONFIG_GLOBAL']), ('0', '1', os.devnull))

    def test_spawned_process_gets_clean_env_even_when_the_caller_passes_secrets(self):
        async def child_env(env):
            proc, _, gate = await spawn_owned([sys.executable, '-c', 'import json,os;print(json.dumps(dict(os.environ)))'],
                                              tempfile.gettempdir(), env)
            release_gate(gate)
            out, _ = await proc.communicate()
            return json.loads(out)
        with mock.patch.dict(os.environ, LEAKS):
            for given in (None, {**os.environ, 'HOME': '/tmp'}):   # Claude passes none; Codex/agy pass their own
                env = asyncio.run(child_env(given))
                self.assertFalse((set(LEAKS) - {'GH_CONFIG_DIR'}) & set(env), given is None)
                self.assertNotEqual(env['GH_CONFIG_DIR'], LEAKS['GH_CONFIG_DIR'])


@unittest.skipUnless(shutil.which('git'), 'git not installed')
class GitFindsNoCredentials(unittest.TestCase):
    """A workspace .git/config may name a credential helper (an agent can write it); git must not call it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.marker = root / 'called'
        helper = root / 'helper.sh'
        helper.write_text(f'#!/bin/sh\ntouch {self.marker}\necho username=u\necho password=leaked\n'); helper.chmod(0o700)
        self.repo = root / 'ws'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'credential.helper', str(helper)], check=True)

    def fill(self, env):
        return subprocess.run(['git', '-C', str(self.repo), 'credential', 'fill'], input='protocol=https\nhost=example.invalid\n\n',
                              env=env, capture_output=True, text=True, timeout=20)

    def test_helper_is_called_without_the_scrub(self):   # the control: the fixture really leaks
        out = self.fill({'PATH': os.environ['PATH'], 'HOME': self.tmp.name, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
        self.assertIn('password=leaked', out.stdout)

    def test_helper_is_not_called_with_the_scrub(self):
        out = self.fill(without_credentials())
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn('leaked', out.stdout)
        self.assertFalse(self.marker.exists())

    def test_url_specific_helper_and_askpass_are_not_used_either(self):   # Gemini TB-21 step 1 review (P2, refuted)
        root = Path(self.tmp.name); askpass = root / 'askpass.sh'
        askpass.write_text(f'#!/bin/sh\ntouch {root}/askpass-ran\necho x\n'); askpass.chmod(0o700)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'credential.https://example.invalid.helper',
                        str(root / 'helper.sh')], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'core.askPass', str(askpass)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', '--unset', 'credential.helper'], check=True)
        out = self.fill(without_credentials())
        self.assertNotIn('leaked', out.stdout)
        self.assertFalse(self.marker.exists())
        self.assertFalse((root / 'askpass-ran').exists())

    def test_no_system_or_global_helper_is_left(self):   # e.g. Xcode's osxkeychain, ~/.gitconfig
        out = subprocess.run(['git', 'config', '--get-all', 'credential.helper'], env=without_credentials(),
                             capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(out.stdout.splitlines(), [''])


@unittest.skipUnless(shutil.which('gh'), 'gh not installed')
class GhIsSignedOut(unittest.TestCase):
    def test_status_reports_no_login(self):   # prints no token either way; the empty config has no host
        out = subprocess.run(['gh', 'auth', 'status'], env=without_credentials(), capture_output=True, text=True, timeout=30)
        self.assertIn('not logged into any GitHub hosts', out.stdout + out.stderr)


if __name__ == '__main__':
    unittest.main()
