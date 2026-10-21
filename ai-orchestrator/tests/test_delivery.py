"""G5 tests: atomic delivery and reconcile."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from app import store as s
from app.delivery import deliver, reconcile, _slugify, DeliveryConflict
from app.transitions import transition, Event


def _make_db(tmp_dir):
    path = Path(tmp_dir) / "state.db"
    conn = s.connect(path)
    s.migrate(conn)
    return conn


def _capture_and_approve(conn, content=b"Hello world", title="Test"):
    eid = "msg_20260101T000000_abcd"
    meta = {
        "id": eid, "schema_version": "1.0",
        "source_agent": "chatgpt", "target_agent": "claude",
        "title": title,
    }
    eid, vid = s.insert_captured(conn, meta, content)
    transition(conn, eid, Event.ENQUEUE,
               expected_state="CAPTURED", actor="test", payload={})
    transition(conn, eid, Event.HUMAN_APPROVE,
               expected_state="PENDING_APPROVAL", actor="test",
               payload={"version_id": vid})
    return eid, vid


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_slugify("Hello World"), "hello-world")

    def test_special_chars_stripped(self):
        self.assertEqual(_slugify("Hello! @World#"), "hello-world")

    def test_none_returns_untitled(self):
        self.assertEqual(_slugify(None), "untitled")

    def test_max_length(self):
        long_title = "a" * 100
        self.assertLessEqual(len(_slugify(long_title)), 40)


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = _make_db(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()

    def test_happy_path_file_exists_and_state_delivered(self):
        eid, vid = _capture_and_approve(self.conn)
        path = deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")
        self.assertTrue(path.exists())
        env = s.get_envelope(self.conn, eid)
        self.assertEqual(env["state"], "DELIVERED")
        self.assertIsNotNone(env["delivered_at"])

    def test_delivered_file_hash_matches_db(self):
        content = b"Important content"
        eid, vid = _capture_and_approve(self.conn, content=content)
        path = deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        env = s.get_envelope(self.conn, eid)
        self.assertEqual(env["delivered_sha256"], actual_sha)

    def test_delivered_file_content_byte_identical(self):
        content = b"---\ntitle: \"T\"\n---\n\xc3\xa9 unicode\n"
        eid, vid = _capture_and_approve(self.conn, content=content)
        path = deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")
        self.assertEqual(path.read_bytes(), content)

    def test_filename_contains_eid_and_slug(self):
        eid, vid = _capture_and_approve(self.conn, title="My Report")
        path = deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")
        self.assertIn(eid, path.name)
        self.assertIn("my-report", path.name)

    def test_deliver_not_approved_raises(self):
        eid = "msg_20260101T000000_bbbb"
        meta = {"id": eid, "schema_version": "1.0",
                "source_agent": "chatgpt", "target_agent": "claude"}
        s.insert_captured(self.conn, meta, b"x")
        # state is CAPTURED, not APPROVED
        with self.assertRaises(ValueError):
            deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")

    def test_conflict_same_hash_is_idempotent(self):
        content = b"Same content"
        eid, vid = _capture_and_approve(self.conn, content=content)
        # Pre-create the file with identical content
        env_pre = s.get_envelope(self.conn, eid)
        from app.delivery import _dest_name
        dest = self.inbox / _dest_name(eid, env_pre.get("title"))
        dest.write_bytes(content)
        # Now deliver — should succeed idempotently
        path = deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")
        self.assertEqual(path, dest)
        self.assertEqual(s.get_envelope(self.conn, eid)["state"], "DELIVERED")

    def test_conflict_different_hash_fails_without_overwrite(self):
        content = b"New content"
        eid, vid = _capture_and_approve(self.conn, content=content)
        from app.delivery import _dest_name
        env_pre = s.get_envelope(self.conn, eid)
        dest = self.inbox / _dest_name(eid, env_pre.get("title"))
        dest.write_bytes(b"Different existing content")
        with self.assertRaises(DeliveryConflict):
            deliver(self.conn, eid, inbox_dir=self.inbox, actor="test")
        # Must not overwrite the conflicting file or route to a suffix.
        self.assertEqual(dest.read_bytes(), b"Different existing content")
        self.assertEqual(s.get_envelope(self.conn, eid)["state"], "FAILED")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = _make_db(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()

    def _setup_delivering(self, content=b"data", title="Rec"):
        eid, vid = _capture_and_approve(self.conn, content=content, title=title)
        # Manually drive to DELIVERING without completing delivery
        from app.delivery import _dest_name
        dest = self.inbox / _dest_name(eid, title)
        s.record_delivery_intent(
            self.conn, eid, vid, str(dest),
            expected_from="APPROVED", actor="test"
        )
        return eid, vid, dest

    def test_reconcile_with_file_present_marks_delivered(self):
        content = b"reconcile me"
        eid, vid, dest = self._setup_delivering(content=content)
        dest.write_bytes(content)
        results = reconcile(self.conn, inbox_dir=self.inbox, actor="test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "recovered_delivered")
        self.assertEqual(s.get_envelope(self.conn, eid)["state"], "DELIVERED")

    def test_reconcile_with_file_absent_redelivers_snapshot(self):
        eid, vid, dest = self._setup_delivering()
        # Do NOT write the file
        results = reconcile(self.conn, inbox_dir=self.inbox, actor="test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["action"], "redelivered")
        self.assertEqual(s.get_envelope(self.conn, eid)["state"], "DELIVERED")
        self.assertEqual(dest.read_bytes(), b"data")

    def test_reconcile_empty_returns_empty_list(self):
        results = reconcile(self.conn, inbox_dir=self.inbox, actor="test")
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
