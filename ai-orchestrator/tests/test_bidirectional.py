"""F2-01: real watcher, two sources, HTTP approval and immutable delivery."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import store
from app.api import create_app
from app.cli import main
from app.routes import inbox_for
from app.watcher import capture_file, run_watcher


class BidirectionalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for source in ('chatgpt', 'claude'):
            (self.root / 'agents' / source / 'outbox').mkdir(parents=True)
        self.db = self.root / '.system' / 'state.db'
        self.db.parent.mkdir()
        self.conn = store.connect(self.db)
        store.migrate(self.conn)
        self.addCleanup(self.conn.close)
        env = patch.dict(os.environ, AI_ORCHESTRATOR_ROOT=str(self.root))
        env.start()
        self.addCleanup(env.stop)

    def test_two_outboxes_http_approval_no_feedback_loop(self):
        stopped = threading.Event()
        failures = []
        notifications = []

        def worker():
            try:
                run_watcher(self.root, self.db, bidirectional=True,
                            require_ready=True, stop_event=stopped,
                            on_capture=lambda eid, state: notifications.append((eid, state)))
            except BaseException as exc:
                failures.append(exc)

        contents = {'chatgpt': b'forward\x00\xff', 'claude': b'reverse\r\nreply'}
        # Before observer startup, to exercise startup scanning too.
        for source, raw in contents.items():
            path = self.root / 'agents' / source / 'outbox' / 'reply.md'
            path.write_bytes(raw)
            path.with_name(path.name + '.ready').touch()
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = store.list_by_state(self.conn, 'PENDING_APPROVAL')
                if len(rows) == 2:
                    break
                time.sleep(.05)
            self.assertEqual(len(rows), 2, failures)
            self.assertEqual(list((self.root / 'agents').glob('*/inbox/*.md')), [])
            with TestClient(create_app()) as client:
                for row in rows:
                    response = client.post(f"/api/envelopes/{row['id']}/approve",
                                           json={'actor': 'tester'})
                    self.assertEqual(response.status_code, 200, response.text)
                    path = Path(response.json()['delivered_path'])
                    self.assertEqual(path.parent, inbox_for(self.root, row))
                    self.assertEqual(path.read_bytes(), contents[row['source_agent']])
            time.sleep(.9)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM envelopes').fetchone()[0], 2)
            self.assertEqual(len(list((self.root / 'archive' / 'captured').glob('*.md'))), 2)
            self.assertEqual(len(notifications), 2)
        finally:
            stopped.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_payload_cannot_override_reverse_route_and_cli_delivers(self):
        path = self.root / 'agents' / 'claude' / 'outbox' / 'reverse.md'
        raw = b'---\nsource_agent: chatgpt\ntarget_agent: ../../elsewhere\n---\nreply'
        path.write_bytes(raw)
        eid, _ = capture_file(path, self.conn, source_agent='claude')
        row = store.get_envelope(self.conn, eid)
        self.assertEqual((row['source_agent'], row['target_agent']), ('claude', 'chatgpt'))
        self.assertEqual(main(['--root', str(self.root), 'approve', eid]), 0)
        delivered = store.get_envelope(self.conn, eid)
        self.assertEqual(Path(delivered['delivered_path']).read_bytes(), raw)
        self.assertEqual(Path(delivered['delivered_path']).parent,
                         self.root / 'agents' / 'chatgpt' / 'inbox')

    def test_invalid_pair_rejected_before_approval(self):
        eid, _ = store.insert_captured(self.conn, {
            'id': 'invalid-route', 'source_agent': 'claude', 'target_agent': '../escape'
        }, b'data')
        from app.transitions import transition
        transition(self.conn, eid, 'enqueue', expected_state='CAPTURED', actor='test', payload={})
        with TestClient(create_app()) as client:
            response = client.post(f'/api/envelopes/{eid}/approve', json={'actor': 'test'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(store.get_envelope(self.conn, eid)['state'], 'PENDING_APPROVAL')

    def test_default_watcher_does_not_capture_claude(self):
        path = self.root / 'agents' / 'claude' / 'outbox' / 'reply.md'
        path.write_bytes(b'not enabled')
        stopped = threading.Event()
        thread = threading.Thread(target=run_watcher,
                                  args=(self.root, self.db), kwargs={'stop_event': stopped})
        thread.start()
        try:
            time.sleep(1)
        finally:
            stopped.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(path.exists())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM envelopes').fetchone()[0], 0)
