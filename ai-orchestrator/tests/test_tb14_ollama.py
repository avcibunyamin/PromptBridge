"""TB-14: local Ollama models through Codex app-server.

A fake Ollama HTTP server (127.0.0.1, random port) and a fake Codex app-server; no model, no quota.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import catalog, provider_auth, store, works_store as ws
from app.adapters.base import AdapterResult
from app.adapters.codex import CodexAdapter
from app.provider_policy import DEFAULT, digest, normalize, split
from app.runner import SessionRunner, _get_adapter
from tests.test_f303_team import V2_DEFAULT_DIGEST

TOOLS = {'gpt-oss:20b': ['completion', 'tools', 'thinking'], 'qwen2.5-coder:14b': ['completion', 'tools'],
         'nomic-embed-text:latest': ['embedding']}


class FakeOllama(BaseHTTPRequestHandler):
    ps = [{'name': 'gpt-oss:20b', 'context_length': 4096}]
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        FakeOllama.calls.append(self.path)
        if self.path == '/api/tags':
            self._send({'models': [{'name': n} for n in [*TOOLS, 'hf.co/someone/model:q4']]})
        elif self.path == '/api/ps':
            self._send({'models': FakeOllama.ps})
        else:
            self.send_error(404)

    def do_POST(self):
        FakeOllama.calls.append(self.path)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self._send({'capabilities': TOOLS.get(body['model'], [])})


class OllamaCase(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        url = f'http://127.0.0.1:{self.server.server_address[1]}'
        patcher = patch.dict(os.environ, {'CONDUCTOR_OLLAMA_URL': url}); patcher.start(); self.addCleanup(patcher.stop)
        saved = {k: (dict(v) if isinstance(v, dict) else v) for k, v in catalog._cache.items()}
        self.addCleanup(lambda: catalog._cache.update(saved))
        catalog._cache.update(entries=None, errors={}, notes={}, checked_at=None)
        FakeOllama.calls = []
        FakeOllama.ps = [{'name': 'gpt-oss:20b', 'context_length': 4096}]
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class Identity(unittest.TestCase):
    def test_ollama_identity_and_old_digests(self):
        self.assertEqual(split('ollama/gpt-oss:20b'), ('ollama', None, 'gpt-oss:20b', None))
        self.assertEqual(digest(normalize(DEFAULT)), V2_DEFAULT_DIGEST)                       # plain policies unchanged
        policy = normalize({'roles': {'author': ['ollama/gpt-oss:20b'], 'reviewer': ['claude']}})
        self.assertEqual(policy['roles']['author'][0]['provider'], 'ollama/gpt-oss:20b')
        for bad in ('ollama', 'ollama@hesap/gpt-oss:20b', 'ollama/gpt-oss:20b~high', 'claude/:x'):
            with self.subTest(bad), self.assertRaises(ValueError):
                normalize({'roles': {'author': [bad], 'reviewer': ['claude']}})


class Catalog(OllamaCase):
    def test_lists_models_tool_support_and_small_context(self):
        entries, error, note = catalog._ollama()
        self.assertIsNone(error)
        self.assertEqual({e['model']: e['status'] for e in entries},
                         {'gpt-oss:20b': 'available', 'qwen2.5-coder:14b': 'available', 'nomic-embed-text:latest': 'unsupported'})
        self.assertEqual(note, 'small_context:4096')                                         # namespaced name skipped
        FakeOllama.ps = [{'name': 'gpt-oss:20b', 'context_length': 16384}]
        self.assertIsNone(catalog._ollama()[2])

    def test_refresh_only_when_configured_and_statuses(self):
        catalog.refresh(str(self.root / 'state.db'), {'roles': {'author': ['claude'], 'reviewer': ['claude']}})
        self.assertEqual(FakeOllama.calls, [])                                               # not configured: no call
        snap = catalog.refresh(str(self.root / 'state.db'),
                               {'roles': {'author': ['claude'], 'reviewer': ['claude']},
                                'allowed': {'author': ['claude', 'ollama'], 'reviewer': ['claude']}})
        self.assertIn('ollama/gpt-oss:20b', [e['identity'] for e in snap['entries']])
        self.assertEqual(snap['notes'], {'ollama': 'small_context:4096'})
        self.assertIsNone(catalog.problem('ollama/gpt-oss:20b'))
        self.assertEqual(catalog.problem('ollama/nomic-embed-text:latest'), 'tools_unsupported')
        self.assertEqual(catalog.problem('ollama/llama9:1b'), 'model_unavailable')
        self.assertEqual(catalog.status('ollama/llama9:1b'), 'model_unavailable')

    def test_ollama_down_or_not_loopback(self):
        with patch.dict(os.environ, {'CONDUCTOR_OLLAMA_URL': 'http://127.0.0.1:9'}):
            self.assertEqual(catalog._ollama()[1], 'unavailable')
            self.assertEqual(catalog.validate_identity(str(self.root / 'state.db'), 'ollama/gpt-oss:20b'), 'ollama_unavailable')
            self.assertIsNone(catalog.problem('ollama/llama9:1b'))                          # unknown, not "removed"
        with patch.dict(os.environ, {'CONDUCTOR_OLLAMA_URL': 'http://example.com:11434'}):
            with self.assertRaises(ValueError):                                                # never leaves loopback
                catalog._ollama_url()
            self.assertEqual(catalog._ollama()[1], 'unavailable')
        self.assertEqual(FakeOllama.calls, [])

    def test_validate_identity_is_fresh_and_strict(self):
        db = str(self.root / 'state.db')
        self.assertIsNone(catalog.validate_identity(db, 'ollama/gpt-oss:20b'))
        self.assertEqual(catalog.validate_identity(db, 'ollama/nomic-embed-text:latest'), 'tools_unsupported')
        self.assertEqual(catalog.validate_identity(db, 'ollama/llama9:1b'), 'model_unavailable')

    def test_session_check_uses_the_api(self):
        self.assertEqual(provider_auth._probe('ollama', '/x', None, 'gpt-oss:20b'), ('authenticated', True))
        self.assertEqual(provider_auth._probe('ollama', '/x', None, 'nomic-embed-text:latest'), ('model_unavailable', True))
        self.assertEqual(provider_auth._probe('ollama', '/x', None, 'llama9:1b'), ('model_unavailable', True))
        with patch.dict(os.environ, {'CONDUCTOR_OLLAMA_URL': 'http://127.0.0.1:9'}):
            self.assertEqual(provider_auth._probe('ollama', '/x', None, 'gpt-oss:20b'), ('unknown', True))


CODEX = '''import json, os, pathlib, sys
argv = sys.argv[1:]
json.dump(argv, open(os.environ['FAKE_ARGV'], 'w'))
def receive(): return json.loads(sys.stdin.readline())
def emit(m): sys.stdout.write(json.dumps(m) + '\\n'); sys.stdout.flush()
m = receive(); emit({'id': m['id'], 'result': {}})
receive()
m = receive(); emit({'id': m['id'], 'result': {'thread': {'id': 't'}}})
m = receive(); assert m['params']['model'] == 'gpt-oss:20b'
emit({'id': m['id'], 'result': {'turn': {'id': 'turn'}}})
p = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions'; p.mkdir(parents=True, exist_ok=True)
(p / 'rollout-t.jsonl').write_text(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-oss:20b'}}) + '\\n')
status = os.environ.get('FAKE_STATUS', 'completed')
emit({'method': 'item/completed', 'params': {'threadId': 't', 'turnId': 'turn', 'item': {'id': 'a', 'type': 'agentMessage', 'text': 'done'}}})
turn = {'id': 'turn', 'status': status}
if status == 'failed':
    turn['error'] = {'codexErrorInfo': 'usageLimitExceeded', 'message': 'quota-like text from a local server'}
emit({'method': 'turn/completed', 'params': {'threadId': 't', 'turn': turn}})
sys.stdin.read()
'''


class Adapter(OllamaCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        path = self.root / 'codex'; path.write_text('#!' + sys.executable + '\n' + CODEX); path.chmod(0o755)
        self.argv = self.root / 'argv.json'
        patcher = patch.dict(os.environ, {'CONDUCTOR_CODEX_BIN': str(path), 'FAKE_ARGV': str(self.argv)})
        patcher.start(); self.addCleanup(patcher.stop)
        (self.root / '.system').mkdir(); self.db = str(self.root / '.system/state.db')
        self.ws = self.root / 'ws'; self.ws.mkdir()

    async def run_adapter(self, adapter):
        return [e async for e in adapter.run('goal', str(self.ws), None, profile='WRITE')]

    async def test_local_provider_argv_home_and_model_check(self):
        adapter = _get_adapter('ollama/gpt-oss:20b', self.db)
        self.assertTrue(adapter.home.endswith('.system/ollama-codex-home'))
        events = await self.run_adapter(adapter)
        self.assertEqual(events[-1]['type'], 'completed', events[-1])
        argv = json.loads(self.argv.read_text())
        self.assertEqual(argv[-3:], ['-c', 'model_provider="ollama"', '--stdio'])
        codex = _get_adapter('codex', self.db)                                                  # Codex itself unchanged
        self.assertEqual((codex.extra_args, codex.local), ((), False))

    async def test_missing_model_never_starts_codex(self):
        events = await self.run_adapter(_get_adapter('ollama/llama9:1b', self.db))
        self.assertEqual((events[-1]['type'], events[-1]['result'].terminal_reason), ('failed', 'model_unavailable'))
        self.assertFalse(self.argv.exists())

    async def test_local_errors_never_hand_off(self):
        with patch.dict(os.environ, {'FAKE_STATUS': 'failed'}):
            events = await self.run_adapter(_get_adapter('ollama/gpt-oss:20b', self.db))
            self.assertEqual(events[-1]['result'].error_code, 'UNKNOWN')                       # not QUOTA_EXCEEDED
            codex = CodexAdapter(str(self.root / 'h'), 'gpt-oss:20b')
            self.assertEqual(codex._error_code({'codexErrorInfo': 'usageLimitExceeded'}), 'QUOTA_EXCEEDED')


class NoChanges(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.db = root / 'state.db'; self.ws = root / 'ws'; self.ws.mkdir()
        (self.ws / 'a.txt').write_text('a')
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)

    async def run_with(self, author, writes):
        workspace = self.ws

        def factory(identity):
            class Fake:
                async def run(self, prompt, path, sid=None, *, profile):
                    if profile == 'WRITE' and writes:
                        (workspace / f'{uuid4().hex}.txt').write_text('x')
                    owner = {'pid': 1, 'pgid': 1, 'start_time': 1.0, 'boot_time': 1.0}
                    yield {'type': 'process_started', 'ownership': owner}
                    yield {'type': 'started', 'provider_session_id': 't'}
                    yield {'type': 'process_stopped', 'ownership': owner}
                    yield {'type': 'completed', 'result': AdapterResult('done', 't', None, 'stop', False, 'completed', 0)}
            return Fake()

        wid = str(uuid4()); ws.create_work(self.conn, wid, 'demo', str(self.ws))
        policy = normalize({'roles': {'author': [author], 'reviewer': ['claude']}})
        ws.add_message(self.conn, wid, str(uuid4()), 'goal', digest(policy), policy, digest(policy))
        sid, _ = ws.dispatch_next(self.conn, wid)
        await SessionRunner(self.db, adapter_factory=factory).run_session(sid)
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM session_events WHERE session_id=? AND kind='no_changes'", (sid,))]

    async def test_local_author_without_changes_is_flagged(self):
        flagged = await self.run_with('ollama/gpt-oss:20b', writes=False)
        self.assertEqual([f['provider'] for f in flagged], ['ollama/gpt-oss:20b', 'ollama/gpt-oss:20b'])   # both author steps

    async def test_changes_or_other_providers_are_not_flagged(self):
        self.assertEqual(await self.run_with('ollama/gpt-oss:20b', writes=True), [])
        self.assertEqual(await self.run_with('claude', writes=False), [])


if __name__ == '__main__':
    unittest.main()
