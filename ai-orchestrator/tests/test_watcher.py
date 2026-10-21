"""G4 tests: watcher capture_file() and Observer integration."""

import tempfile
import time
import unittest
from pathlib import Path

from watchdog.observers.polling import PollingObserver

from app import store as s
from app.watcher import capture_file, run_watcher, _parse_frontmatter, _OutboxHandler


def _make_db(tmp_dir):
    path = Path(tmp_dir) / "state.db"
    conn = s.connect(path)
    s.migrate(conn)
    return conn


class ParseFrontmatterTests(unittest.TestCase):
    def test_valid_frontmatter_extracts_known_keys(self):
        raw = b'---\ntitle: "My Title"\ntype: prose\nnote: "A note"\n---\nBody here\n'
        meta, body = _parse_frontmatter(raw)
        self.assertEqual(meta["title"], "My Title")
        self.assertEqual(meta["type"], "prose")
        self.assertEqual(meta["note"], "A note")
        self.assertIn(b"Body here", body)

    def test_invalid_type_is_excluded(self):
        raw = b'---\ntype: invalid_type\n---\ncontent\n'
        meta, body = _parse_frontmatter(raw)
        self.assertNotIn("type", meta)

    def test_no_frontmatter_returns_empty_meta_and_full_body(self):
        raw = b"just some content\n"
        meta, body = _parse_frontmatter(raw)
        self.assertEqual(meta, {})
        self.assertEqual(body, raw)


class CaptureFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = _make_db(self.tmp.name)
        self.addCleanup(self.conn.close)

    def _write(self, name: str, content: bytes = b"# Hello\nContent\n") -> Path:
        p = Path(self.tmp.name) / name
        p.write_bytes(content)
        return p

    def test_happy_path_returns_eid_vid_and_state(self):
        path = self._write("output.md")
        result = capture_file(path, self.conn)
        self.assertIsNotNone(result)
        eid, vid = result
        env = s.get_envelope(self.conn, eid)
        self.assertEqual(env["state"], "PENDING_APPROVAL")
        self.assertEqual(env["source_agent"], "chatgpt")
        self.assertEqual(env["target_agent"], "claude")
        self.assertEqual(env["origin_ref"], str(path))

    def test_frontmatter_populates_meta(self):
        raw = b'---\ntitle: "Test Title"\ntype: task\n---\nBody\n'
        path = self._write("with_fm.md", raw)
        eid, _ = capture_file(path, self.conn)
        env = s.get_envelope(self.conn, eid)
        self.assertEqual(env["title"], "Test Title")
        self.assertEqual(env["type"], "task")

    def test_empty_file_is_skipped(self):
        path = self._write("empty.md", b"")
        result = capture_file(path, self.conn)
        self.assertIsNone(result)

    def test_duplicate_origin_ref_is_skipped(self):
        path = self._write("dup.md")
        r1 = capture_file(path, self.conn)
        r2 = capture_file(path, self.conn)
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)
        # Only one envelope should exist for this path
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM envelopes WHERE origin_ref=?", (str(path),)
        ).fetchone()
        self.assertEqual(rows[0], 1)

    def test_unsupported_extension_is_skipped(self):
        path = Path(self.tmp.name) / "file.pdf"
        path.write_bytes(b"%PDF binary data")
        result = capture_file(path, self.conn)
        self.assertIsNone(result)

    def test_single_capture_produces_one_history_entry_for_enqueue(self):
        path = self._write("hist.md")
        eid, _ = capture_file(path, self.conn)
        history = s.get_history(self.conn, eid)
        events = [h["event"] for h in history]
        self.assertIn("enqueue", events)

    def test_content_is_byte_identical(self):
        raw = b"---\ntitle: \"T\"\n---\nBody with unicode \xc3\xa9\n"
        path = self._write("bytes.md", raw)
        eid, vid = capture_file(path, self.conn)
        stored = s.get_content(self.conn, vid)
        self.assertEqual(stored, raw)

    def test_successful_capture_archives_source_when_requested(self):
        path = self._write("archive-me.md")
        archive = Path(self.tmp.name) / "archive" / "captured"
        eid, _ = capture_file(path, self.conn, archive)
        self.assertFalse(path.exists())
        self.assertTrue((archive / f"{eid}__archive-me.md").exists())


class WatcherContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.outbox = self.root / "agents" / "chatgpt" / "outbox"
        self.outbox.mkdir(parents=True)
        self.archive = self.root / "archive" / "captured"
        self.db_path = self.root / "state.db"
        self.conn = _make_db(self.tmp.name)
        self.addCleanup(self.conn.close)

    def test_startup_scan_captures_existing_file(self):
        path = self.outbox / "existing.md"
        path.write_bytes(b"existing")
        handler = _OutboxHandler(self.db_path, self.archive, debounce=0)
        handler.scan_startup(self.outbox)
        handler._flush()
        rows = self.conn.execute("SELECT state FROM envelopes").fetchall()
        self.assertEqual([row[0] for row in rows], ["PENDING_APPROVAL"])
        self.assertFalse(path.exists())

    def test_require_ready_does_not_capture_until_marker(self):
        path = self.outbox / "marked.md"
        path.write_bytes(b"ready content")
        handler = _OutboxHandler(self.db_path, self.archive, debounce=0,
                                 require_ready=True)
        handler._schedule(str(path))
        handler._flush()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM envelopes").fetchone()[0], 0)
        marker = path.with_name(path.name + ".ready")
        marker.write_text("")
        handler._schedule(str(marker), immediate=True)
        handler._flush()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM envelopes").fetchone()[0], 1)


class WatcherIntegrationTest(unittest.TestCase):
    """Start a real (polling) observer and drop a file in the outbox."""

    def test_observer_captures_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outbox = root / "agents" / "chatgpt" / "outbox"
            outbox.mkdir(parents=True)
            db_path = Path(tmp) / "state.db"
            conn = _make_db(tmp)
            self.addCleanup(conn.close)

            import threading

            def _run():
                run_watcher(root, db_path, observer_class=PollingObserver)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            time.sleep(0.3)  # let observer start

            test_file = outbox / "chatgpt_output.md"
            test_file.write_bytes(b"---\ntitle: \"Integration\"\n---\nHello\n")

            # Wait up to 3 s for capture
            deadline = time.monotonic() + 3.0
            captured = None
            while time.monotonic() < deadline:
                rows = conn.execute(
                    "SELECT id FROM envelopes WHERE state='PENDING_APPROVAL'"
                ).fetchall()
                if rows:
                    captured = rows[0][0]
                    break
                time.sleep(0.2)

            self.assertIsNotNone(captured, "Observer did not capture the file in time")
            env = s.get_envelope(conn, captured)
            self.assertEqual(env["state"], "PENDING_APPROVAL")


if __name__ == "__main__":
    unittest.main()
