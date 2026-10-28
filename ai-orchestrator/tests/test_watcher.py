"""G4 tests: watcher capture_file() and Observer integration."""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from watchdog.observers.polling import PollingObserver

from app import store as s
from app.watcher import (
    MAX_ENVELOPE_BYTES,
    _OutboxHandler,
    _make_eid,
    _move_to_failed,
    _parse_frontmatter,
    capture_file,
    run_watcher,
)


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

    def test_utf8_bom_and_crlf_frontmatter_extracts_title(self):
        raw = (
            b'\xef\xbb\xbf---\r\n'
            b'title: "BOM CRLF Title"\r\n'
            b'type: spec\r\n'
            b'note: "CRLF note"\r\n'
            b'---\r\n'
            b'Body line\r\n'
        )
        meta, body = _parse_frontmatter(raw)
        self.assertEqual(meta["title"], "BOM CRLF Title")
        self.assertEqual(meta["type"], "spec")
        self.assertEqual(meta["note"], "CRLF note")
        self.assertEqual(body, b"Body line\r\n")

    def test_unclosed_frontmatter_with_body_rule_and_title_has_no_title(self):
        raw = (
            b"---\n"
            b'note: "Open frontmatter"\n\n'
            + (b"Body paragraph line.\n" * 52)
            + b"title: Gizli\n"
            b"---\n"
            b"Section after horizontal rule\n"
        )
        meta, body = _parse_frontmatter(raw)
        self.assertNotIn("title", meta)
        self.assertEqual(meta, {})
        self.assertEqual(body, raw)

    def test_sixty_line_frontmatter_returns_empty_meta(self):
        extra = b"".join(f"extra_{i}: value_{i}\n".encode("utf-8") for i in range(59))
        raw = b'---\ntitle: "Too Long"\n' + extra + b"---\nBody\n"
        meta, body = _parse_frontmatter(raw)
        self.assertEqual(meta, {})
        self.assertEqual(body, raw)

    def test_empty_frontmatter_returns_empty_meta_without_error(self):
        raw = b"---\n---\nBody line\n"
        meta, body = _parse_frontmatter(raw)
        self.assertEqual(meta, {})
        self.assertEqual(body, b"Body line\n")

        meta_crlf, body_crlf = _parse_frontmatter(b"---\r\n---\r\nBody line\r\n")
        self.assertEqual(meta_crlf, {})
        self.assertEqual(body_crlf, b"Body line\r\n")

    def test_duplicate_title_keeps_first_value(self):
        raw = b'---\ntitle: "First Title"\ntitle: "Second Title"\n---\nBody\n'
        meta, body = _parse_frontmatter(raw)
        self.assertEqual(meta["title"], "First Title")
        self.assertEqual(body, b"Body\n")


class MoveToFailedRaceTests(unittest.TestCase):
    def test_race_on_target_retries_atomically_without_replace(self):
        path = Path("/virtual/outbox/not.md")
        attempts = []

        def fake_link(src, dst, *, follow_symlinks=True):
            attempts.append((Path(src), Path(dst), follow_symlinks))
            if len(attempts) == 1:
                raise FileExistsError("simulated concurrent target creation")

        fake_stat = os.stat_result((stat.S_IFREG | 0o644, 1, 1, 1, 0, 0, 10, 0, 0, 0))
        with mock.patch.object(Path, "mkdir"), \
             mock.patch.object(Path, "lstat", return_value=fake_stat), \
             mock.patch.object(Path, "unlink") as unlink_mock, \
             mock.patch.object(Path, "replace", side_effect=AssertionError("replace must not be called")), \
             mock.patch("app.watcher.os.link", side_effect=fake_link):
            moved_to = _move_to_failed(path)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][1], Path("/virtual/outbox/.failed/not.md"))
        self.assertFalse(attempts[0][2])
        self.assertRegex(moved_to.name, r"^not\.\d{8}T\d{6}Z-[0-9a-f]{4}\.md$")
        self.assertEqual(attempts[1][1], moved_to)
        unlink_mock.assert_called_once_with(missing_ok=True)

    def test_symlink_and_fifo_race_retries_atomically(self):
        sym_path = Path("/virtual/outbox/sym.md")
        sym_attempts = []

        def fake_symlink(src, dst):
            sym_attempts.append((src, Path(dst)))
            if len(sym_attempts) == 1:
                raise FileExistsError("simulated symlink collision")

        sym_stat = os.stat_result((stat.S_IFLNK | 0o777, 1, 1, 1, 0, 0, 8, 0, 0, 0))
        with mock.patch.object(Path, "mkdir"), \
             mock.patch.object(Path, "lstat", return_value=sym_stat), \
             mock.patch.object(Path, "unlink") as sym_unlink, \
             mock.patch("app.watcher.os.readlink", return_value="../outside.md"), \
             mock.patch("app.watcher.os.symlink", side_effect=fake_symlink):
            sym_dest = _move_to_failed(sym_path)

        self.assertEqual(len(sym_attempts), 2)
        self.assertEqual(sym_attempts[0], ("../outside.md", Path("/virtual/outbox/.failed/sym.md")))
        self.assertRegex(sym_dest.name, r"^sym\.\d{8}T\d{6}Z-[0-9a-f]{4}\.md$")
        sym_unlink.assert_called_once_with(missing_ok=True)

        fifo_path = Path("/virtual/outbox/pipe.md")
        fifo_attempts = []

        def fake_mkfifo(dst, mode=0o666):
            fifo_attempts.append((Path(dst), mode))
            if len(fifo_attempts) == 1:
                raise FileExistsError("simulated fifo collision")

        fifo_stat = os.stat_result((stat.S_IFIFO | 0o600, 1, 1, 1, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(Path, "mkdir"), \
             mock.patch.object(Path, "lstat", return_value=fifo_stat), \
             mock.patch.object(Path, "unlink") as fifo_unlink, \
             mock.patch("app.watcher.os.mkfifo", side_effect=fake_mkfifo):
            fifo_dest = _move_to_failed(fifo_path)

        self.assertEqual(len(fifo_attempts), 2)
        self.assertEqual(fifo_attempts[0], (Path("/virtual/outbox/.failed/pipe.md"), 0o600))
        self.assertRegex(fifo_dest.name, r"^pipe\.\d{8}T\d{6}Z-[0-9a-f]{4}\.md$")
        fifo_unlink.assert_called_once_with(missing_ok=True)


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

    def test_make_eid_uses_eight_hex_random_suffix(self):
        self.assertRegex(_make_eid(), r"^msg_\d{8}T\d{6}_[0-9a-f]{8}$")

    def test_bom_and_crlf_frontmatter_preserves_raw_bytes(self):
        raw = b'\xef\xbb\xbf---\r\ntitle: "BOM Title"\r\ntype: task\r\n---\r\nBody\r\n'
        path = self._write("bom.md", raw)
        eid, vid = capture_file(path, self.conn)
        env = s.get_envelope(self.conn, eid)
        self.assertEqual(env["title"], "BOM Title")
        self.assertEqual(env["type"], "task")
        self.assertEqual(s.get_content(self.conn, vid), raw)

    def test_hard_link_is_rejected(self):
        orig = self._write("orig.md", b"shared content\n")
        hard = Path(self.tmp.name) / "hard.md"
        os.link(orig, hard)
        with self.assertRaises(ValueError):
            capture_file(hard, self.conn)

    def test_fifo_is_rejected_without_blocking(self):
        fifo = Path(self.tmp.name) / "pipe.md"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            capture_file(fifo, self.conn)

    def test_oversized_file_is_rejected(self):
        big = Path(self.tmp.name) / "oversized.md"
        with open(big, "wb") as fh:
            fh.seek(MAX_ENVELOPE_BYTES)
            fh.write(b"x")
        with self.assertRaises(ValueError):
            capture_file(big, self.conn)

    def test_long_utf8_filename_is_archived_within_255_bytes(self):
        long_name = "x" + ("\u015f" * 115) + ".md"
        path = self._write(long_name, b"long filename body\n")
        archive = Path(self.tmp.name) / "archive" / "captured"
        eid, _ = capture_file(path, self.conn, archive)
        self.assertFalse(path.exists())
        archived_files = list(archive.iterdir())
        self.assertEqual(len(archived_files), 1)
        archived_name = archived_files[0].name
        self.assertTrue(archived_name.startswith(f"{eid}__x"))
        self.assertTrue(archived_name.endswith(".md"))
        self.assertLessEqual(len(archived_name.encode("utf-8")), 255)
        self.assertNotIn("\ufffd", archived_name)
        self.assertEqual(archived_name[len(eid) + 3:-3], "\u015f" * 110)


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

    def test_symlink_fifo_and_oversized_move_to_failed_without_hanging(self):
        outside = self.root / "outside.md"
        outside.write_bytes(b"secret\n")
        sym = self.outbox / "symlink.md"
        sym.symlink_to(outside)

        fifo = self.outbox / "pipe.md"
        os.mkfifo(fifo)

        big = self.outbox / "too_big.md"
        with open(big, "wb") as fh:
            fh.seek(MAX_ENVELOPE_BYTES)
            fh.write(b"x")

        valid = self.outbox / "valid.md"
        valid.write_bytes(b"valid content\n")

        handler = _OutboxHandler(self.db_path, self.archive, debounce=0)
        self.addCleanup(
            lambda: handler._thread_conn and handler._thread_conn.close()
        )
        handler.scan_startup(self.outbox)
        handler._flush()

        failed_dir = self.outbox / ".failed"
        self.assertTrue((failed_dir / "symlink.md").is_symlink())
        self.assertTrue(stat.S_ISFIFO((failed_dir / "pipe.md").lstat().st_mode))
        self.assertTrue((failed_dir / "too_big.md").exists())
        self.assertFalse(sym.exists() or sym.is_symlink())
        self.assertFalse(fifo.exists())
        self.assertFalse(big.exists())
        self.assertFalse(valid.exists())
        rows = self.conn.execute("SELECT state FROM envelopes").fetchall()
        self.assertEqual([r[0] for r in rows], ["PENDING_APPROVAL"])

    def test_failed_move_preserves_existing_file_and_uniquifies_name(self):
        failed_dir = self.outbox / ".failed"
        failed_dir.mkdir(parents=True)
        existing = failed_dir / "not.md"
        existing.write_bytes(b"first failed content\n")

        linked_src = self.root / "linked_src.md"
        linked_src.write_bytes(b"second failed content\n")
        second = self.outbox / "not.md"
        os.link(linked_src, second)

        handler = _OutboxHandler(self.db_path, self.archive, debounce=0)
        self.addCleanup(
            lambda: handler._thread_conn and handler._thread_conn.close()
        )
        handler.scan_startup(self.outbox)
        handler._flush()

        self.assertFalse(second.exists())
        self.assertEqual(existing.read_bytes(), b"first failed content\n")
        extra_files = [p for p in failed_dir.iterdir() if p.name != "not.md"]
        self.assertEqual(len(extra_files), 1)
        self.assertRegex(
            extra_files[0].name,
            r"^not\.\d{8}T\d{6}Z-[0-9a-f]{4}\.md$",
        )
        self.assertEqual(extra_files[0].read_bytes(), b"second failed content\n")

        third = self.outbox / "not.md"
        third.write_bytes(b"")
        handler._schedule(str(third), immediate=True)
        handler._flush()

        self.assertFalse(third.exists())
        self.assertEqual(existing.read_bytes(), b"first failed content\n")
        all_extra = sorted(p.name for p in failed_dir.iterdir() if p.name != "not.md")
        self.assertEqual(len(all_extra), 2)
        for name in all_extra:
            self.assertRegex(name, r"^not\.\d{8}T\d{6}Z-[0-9a-f]{4}\.md$")

    def test_failed_move_concurrent_race_does_not_overwrite(self):
        linked_src = self.root / "race_src.md"
        linked_src.write_bytes(b"loser content\n")
        contender = self.outbox / "not.md"
        os.link(linked_src, contender)

        real_link = os.link
        raced_once = False

        def racing_link(src, dst, *, follow_symlinks=True):
            nonlocal raced_once
            if not raced_once:
                raced_once = True
                Path(dst).write_bytes(b"concurrent winner content\n")
            return real_link(src, dst, follow_symlinks=follow_symlinks)

        with mock.patch("app.watcher.os.link", side_effect=racing_link):
            moved_to = _move_to_failed(contender)

        failed_dir = self.outbox / ".failed"
        self.assertFalse(contender.exists())
        self.assertEqual(
            (failed_dir / "not.md").read_bytes(),
            b"concurrent winner content\n",
        )
        self.assertRegex(moved_to.name, r"^not\.\d{8}T\d{6}Z-[0-9a-f]{4}\.md$")
        self.assertEqual(moved_to.read_bytes(), b"loser content\n")


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
