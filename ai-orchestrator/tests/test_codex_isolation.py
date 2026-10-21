"""TB-05 (K-014): Codex runs in Conductor's own CODEX_HOME with its AGENTS.md loader off; Conductor
freezes the workspace AGENTS.md and injects it only into new Codex conversations.

Fake binaries and scripted adapters only (no provider quota); the real Codex check is reported separately.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import project_instructions as pi, provider_auth, store, works_store as ws, runner_store as repo
from app.adapters.base import AdapterResult
from app.adapters.codex import CodexAdapter, codex_home
from app.provider_policy import DEFAULT, normalize, digest
from app.runner import SessionRunner
from tests.test_project_instructions import FakeFactory
from tests.test_works import Scripted

DUMP = '''import json, os, sys
home = os.environ.get('CODEX_HOME')
config = os.path.join(home, 'config.toml') if home else ''
with open(sys.argv[0] + '.seen', 'w') as f:
    json.dump({'argv': sys.argv[1:], 'home': home,
               'config': open(config).read() if os.path.exists(config) else None}, f)
'''


class ProcessEnvironment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cli = self.root / 'codex'
        self.cli.write_text(f'#!{sys.executable}\n' + DUMP + 'sys.stdin.readline()\n')
        self.cli.chmod(0o755)

    def seen(self):
        return json.loads(Path(str(self.cli) + '.seen').read_text())

    async def run_adapter(self, home, workspace=None):
        events = []
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': str(self.cli), 'CODEX_HOME': '/users/own/.codex'}):
            with self.assertRaises(ValueError) as caught:
                async for event in CodexAdapter(str(home)).run('goal', str(workspace or self.root), profile='WRITE'):
                    events.append(event)
        return caught.exception

    async def test_codex_gets_its_own_home_and_no_agents_loader(self):
        home = self.root / '.system' / 'codex-home'; home.parent.mkdir()
        error = await self.run_adapter(home)
        self.assertIn('EOF', str(error))                       # the fake exits after one line
        seen = self.seen()
        self.assertEqual(seen['home'], str(home))              # the inherited CODEX_HOME is replaced
        self.assertEqual(seen['argv'], ['app-server', '-c', 'project_doc_max_bytes=0',
                                        '-c', 'features.plugins=false', '--stdio'])
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)

    async def test_workspace_and_git_root_are_pinned_untrusted_before_codex_starts(self):
        repo_root = self.root / 'repo'; workspace = repo_root / 'sub'; workspace.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(repo_root)], check=True)
        top = os.path.realpath(repo_root)
        home = self.root / 'codex-home'; home.mkdir(mode=0o700)
        (home / 'config.toml').write_text(f'model = "kept"\n\n[projects.{json.dumps(top)}]\ntrust_level = "trusted"\n\n'
                                          '[projects."/elsewhere"]\ntrust_level = "trusted"\n')
        await self.run_adapter(home, workspace)
        seen = tomllib.loads(self.seen()['config'])          # what Codex found when it started
        self.assertEqual(seen['projects'][top]['trust_level'], 'untrusted')
        self.assertEqual(seen['projects'][str(workspace)]['trust_level'], 'untrusted')
        self.assertEqual(seen['projects']['/elsewhere']['trust_level'], 'trusted')   # other entries untouched
        self.assertEqual(seen['model'], 'kept')
        before = (home / 'config.toml').read_text()
        await self.run_adapter(home, workspace)
        self.assertEqual((home / 'config.toml').read_text(), before)                  # idempotent

    async def test_unreadable_home_config_refuses_start(self):
        home = self.root / 'codex-home'; home.mkdir(mode=0o700)
        (home / 'config.toml').write_text('broken = [')
        await self.run_adapter(home)
        self.assertFalse(Path(str(self.cli) + '.seen').exists())

    async def test_symlinked_home_is_refused_before_any_process(self):
        real = self.root / 'real'; real.mkdir()
        link = self.root / 'codex-home'; link.symlink_to(real)
        error = await self.run_adapter(link)
        self.assertIn('not a real directory', str(error))
        self.assertFalse(Path(str(self.cli) + '.seen').exists())

    def test_home_sits_next_to_the_db_unless_overridden(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CONDUCTOR_CODEX_HOME', None)
            self.assertEqual(codex_home('/x/.system/state.db'), '/x/.system/codex-home')
        with patch.dict(os.environ, {'CONDUCTOR_CODEX_HOME': '/y/home'}):
            self.assertEqual(codex_home('/x/.system/state.db'), '/y/home')

    def test_session_check_uses_the_home_and_fails_closed_without_one(self):
        home = self.root / 'codex-home'
        with patch.dict(os.environ, {'CODEX_HOME': '/users/own/.codex'}):
            provider_auth._probe('codex', str(self.cli), str(home))
            self.assertEqual(self.seen()['home'], str(home))
            Path(str(self.cli) + '.seen').unlink()
            self.assertEqual(provider_auth._probe('codex', str(self.cli)), ('unknown', True))
            self.assertFalse(Path(str(self.cli) + '.seen').exists())
            provider_auth._probe('claude', str(self.cli))          # Claude's environment is untouched
            self.assertEqual(self.seen()['home'], '/users/own/.codex')


class Migration(unittest.TestCase):
    def test_1_7_database_gains_agents_snapshots_losslessly(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / 'state.db'); self.addCleanup(conn.close); store.migrate(conn)
        repo.create_session(conn, 'old', tmp.name, 'auto')
        conn.execute('DROP TABLE session_agents_instructions'); conn.execute("UPDATE schema_meta SET value='1.7'")
        before = repo.get_session(conn, 'old')
        store.migrate(conn); store.migrate(conn)                                # idempotent
        self.assertEqual(conn.execute("SELECT value FROM schema_meta").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(repo.get_session(conn, 'old'), before)
        self.assertEqual(conn.execute('SELECT count(*) FROM session_agents_instructions').fetchone()[0], 0)
        self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())


class AgentsResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def resolve(self):
        return pi.resolve(str(self.root), provider='codex')

    def test_states(self):
        self.assertEqual(self.resolve()['status'], 'absent')
        (self.root / 'AGENTS.md').write_text('rule\n@other.md\n')
        value = self.resolve()
        self.assertEqual(value['status'], 'included')
        self.assertEqual(value['section'], pi.header('AGENTS.md') + '\nrule\n@other.md\n')   # K3: no import
        self.assertEqual([f['path'] for f in value['manifest']['files']], ['AGENTS.md'])
        self.assertEqual(self.resolve()['combined_digest'], value['combined_digest'])
        (self.root / 'AGENTS.md').write_bytes(b'\xff\xfe')
        self.assertEqual((self.resolve()['reason'], self.resolve()['path']), ('not_utf8', 'AGENTS.md'))
        (self.root / 'AGENTS.md').write_text('x' * (pi.LIMIT + 1))
        self.assertEqual(self.resolve()['reason'], 'too_large')
        (self.root / 'AGENTS.md').unlink(); (self.root / 'real.md').write_text('x')
        (self.root / 'AGENTS.md').symlink_to(self.root / 'real.md')
        self.assertEqual(self.resolve()['reason'], 'symlink')


class AgentsLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.db = root / 'state.db'; self.workspace = root / 'ws'; self.workspace.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        (self.workspace / 'CLAUDE.md').write_text('CLAUDE_RULE')
        (self.workspace / 'AGENTS.md').write_text('AGENTS_RULE')

    def queue(self, policy):
        wid = str(uuid4()); ws.create_work(self.conn, wid, 'demo', str(self.workspace))
        ws.add_message(self.conn, wid, str(uuid4()), 'goal', digest(policy), policy, digest(policy))
        return wid

    def traces(self, sid):
        return [json.loads(r['payload'])['project_instructions'] for r in self.conn.execute(
            "SELECT payload FROM session_events WHERE session_id=? AND kind='attempt_started' ORDER BY seq", (sid,))]

    async def test_each_provider_gets_its_own_frozen_file_only_in_new_conversations(self):
        sid, reason = ws.dispatch_next(self.conn, self.queue(normalize(DEFAULT))); self.assertIsNone(reason)
        (self.workspace / 'AGENTS.md').write_text('CHANGED_AFTER_DISPATCH')
        fake = FakeFactory(); await SessionRunner(self.db, adapter_factory=fake).run_session(sid)
        author, reviewer, correction = fake.calls
        self.assertIn('AGENTS_RULE', author[1]); self.assertNotIn('CLAUDE_RULE', author[1])
        self.assertNotIn('CHANGED_AFTER_DISPATCH', author[1])
        self.assertIn('CLAUDE_RULE', reviewer[1]); self.assertNotIn('AGENTS_RULE', reviewer[1])
        self.assertIsNotNone(correction[2]); self.assertNotIn('AGENTS_RULE', correction[1])   # thread/resume
        self.assertEqual([(t['source'], t['injection_reason']) for t in self.traces(sid)],
                         [('AGENTS.md', 'fresh_conversation'), ('CLAUDE.md', 'fresh_conversation'), ('AGENTS.md', 'resume')])

    async def test_claude_to_codex_fallback_receives_the_agents_snapshot(self):
        policy = normalize({'roles': {'author': ['claude', 'codex'], 'reviewer': ['claude']}})
        sid, _ = ws.dispatch_next(self.conn, self.queue(policy))
        quota = AdapterResult('quota', 'claude-q', None, 'error', True, 'failed', 1,
                              'QUOTA_EXCEEDED', {'code': 'QUOTA_EXCEEDED'})
        fake = FakeFactory({'claude': [quota]}); await SessionRunner(self.db, adapter_factory=fake).run_session(sid)
        codex = next(call for call in fake.calls if call[0] == 'codex')
        self.assertIn('AGENTS_RULE', codex[1]); self.assertNotIn('CLAUDE_RULE', codex[1])

    def test_agents_snapshot_and_rejection_follow_the_policy(self):
        (self.workspace / 'AGENTS.md').write_bytes(b'\xff')
        claude_only = normalize({'roles': {'author': ['claude'], 'reviewer': ['claude']}})
        sid, reason = ws.dispatch_next(self.conn, self.queue(claude_only))
        self.assertIsNone(reason)                               # Codex never runs: AGENTS.md is not read for it
        self.assertIsNone(pi.snapshot(self.conn, sid, 'codex'))
        repo.update_session_status(self.conn, sid, 'CANCELLED')   # frees the workspace for the next dispatch
        sid, reason = ws.dispatch_next(self.conn, self.queue(normalize(DEFAULT)))
        self.assertEqual(reason, 'rejected')
        self.assertEqual(repo.get_session(self.conn, sid)['status'], 'FAILED')
        self.assertEqual(pi.snapshot(self.conn, sid, 'codex')['reason'], 'not_utf8')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM session_steps WHERE session_id=?', (sid,)).fetchone()[0], 0)
        with self.assertRaises(Exception):
            self.conn.execute('DELETE FROM session_agents_instructions WHERE session_id=?', (sid,))


class PreIsolationThreads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.db = root / 'state.db'; work_dir = root / 'ws'; work_dir.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'demo', str(work_dir))
        self.factory = Scripted([], {})

    async def run_message(self, text):
        policy = normalize(DEFAULT)
        ws.add_message(self.conn, self.wid, str(uuid4()), text, digest(policy), policy, digest(policy))
        sid, _ = ws.dispatch_next(self.conn, self.wid)
        await SessionRunner(self.db, adapter_factory=self.factory).run_session(sid)
        return sid

    def decision(self, sid):
        row = self.conn.execute('SELECT decision, reason FROM work_resume_refs WHERE session_id=?', (sid,)).fetchone()
        return tuple(row)

    async def test_threads_from_before_tb05_are_not_resumed(self):
        first = await self.run_message('one')
        self.assertEqual(self.decision(await self.run_message('two')), ('RESUME', 'all_gates_passed'))
        # A run from before schema 1.8 has no AGENTS.md row: its thread lives in the user's ~/.codex.
        last = self.conn.execute('SELECT session_id FROM work_runs ORDER BY created_at DESC LIMIT 1').fetchone()[0]
        self.conn.execute('DROP TRIGGER agents_instructions_no_delete')
        self.conn.execute('DELETE FROM session_agents_instructions WHERE session_id=?', (last,))
        self.assertNotEqual(first, last)
        self.assertEqual(self.decision(await self.run_message('three')), ('FRESH', 'codex_home_changed'))


if __name__ == '__main__':
    unittest.main()
