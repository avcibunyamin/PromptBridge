"""G6 tests: FastAPI endpoints via httpx TestClient."""

import hashlib
import tempfile
import unittest
from pathlib import Path

# httpx + starlette TestClient — synchronous test harness for FastAPI
try:
    from httpx import Client as HttpxClient
    from starlette.testclient import TestClient
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

from app import store as s
from app.api import create_app
from app.transitions import transition, Event


def _make_db(tmp_dir: str) -> str:
    path = Path(tmp_dir) / ".system" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = s.connect(str(path))
    s.migrate(conn)
    conn.close()
    return str(path)


def _capture_and_advance(db_path: str, content=b"Hello world",
                          title="Test", state="PENDING_APPROVAL"):
    """Insert a captured envelope and advance it to *state*."""
    import os
    conn = s.connect(db_path)
    eid = "msg_20260101T000000_test"
    meta = {"id": eid, "schema_version": "1.0",
            "source_agent": "chatgpt", "target_agent": "claude",
            "title": title}
    eid, vid = s.insert_captured(conn, meta, content)
    transition(conn, eid, Event.ENQUEUE,
               expected_state="CAPTURED", actor="test", payload={})
    if state in ("APPROVED", "DELIVERING", "DELIVERED", "FAILED"):
        transition(conn, eid, Event.HUMAN_APPROVE,
                   expected_state="PENDING_APPROVAL", actor="test",
                   payload={"version_id": vid})
    conn.close()
    return eid, vid


@unittest.skipUnless(_HAS_HTTPX, "httpx not installed — skipping API tests")
class ApiTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.root = Path(self.tmp.name)
        self.inbox = self.root / "agents" / "claude" / "inbox"
        self.inbox.mkdir(parents=True)
        (self.root / ".system").mkdir(parents=True)
        self.db_path = str(self.root / ".system" / "state.db")
        
        # Ensure schema is created before tests
        _make_db(self.tmp.name)

        import os
        os.environ["AI_ORCHESTRATOR_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "AI_ORCHESTRATOR_ROOT", None)

        self.app = create_app()
        self.client = TestClient(self.app, raise_server_exceptions=True)

    # ── list ────────────────────────────────────────────────────────────────────

    def test_list_envelopes_empty(self):
        r = self.client.get("/api/envelopes")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_list_envelopes_state_filter(self):
        _capture_and_advance(self.db_path)
        r = self.client.get("/api/envelopes?state=PENDING_APPROVAL")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["state"], "PENDING_APPROVAL")

    def test_list_state_filter_excludes_others(self):
        _capture_and_advance(self.db_path)
        r = self.client.get("/api/envelopes?state=DELIVERED")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    # ── get ─────────────────────────────────────────────────────────────────────

    def test_get_envelope_not_found(self):
        r = self.client.get("/api/envelopes/nonexistent")
        self.assertEqual(r.status_code, 404)

    def test_get_envelope_returns_versions_and_history(self):
        eid, _ = _capture_and_advance(self.db_path)
        r = self.client.get(f"/api/envelopes/{eid}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("envelope", body)
        self.assertIn("versions", body)
        self.assertIn("history", body)
        self.assertTrue(len(body["versions"]) >= 1)

    # ── content ─────────────────────────────────────────────────────────────────

    def test_get_content_returns_plain_text(self):
        eid, _ = _capture_and_advance(self.db_path, content=b"Hello world")
        r = self.client.get(f"/api/envelopes/{eid}/content")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r.headers["content-type"])
        self.assertEqual(r.text, "Hello world")

    # ── approve ─────────────────────────────────────────────────────────────────

    def test_approve_happy_path(self):
        eid, _ = _capture_and_advance(self.db_path)
        r = self.client.post(f"/api/envelopes/{eid}/approve",
                             json={"actor": "tester"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["envelope"]["state"], "DELIVERED")
        self.assertIn("delivered_path", body)
        # File must exist on disk
        self.assertTrue(Path(body["delivered_path"]).exists())

    def test_approve_wrong_state_returns_409(self):
        # No envelope yet — create one in CAPTURED only
        conn = s.connect(self.db_path)
        s.migrate(conn)
        eid_raw = "msg_20260101T000000_test"
        meta = {"id": eid_raw, "schema_version": "1.0",
                "source_agent": "chatgpt", "target_agent": "claude"}
        eid, _ = s.insert_captured(conn, meta, b"x")
        conn.close()
        r = self.client.post(f"/api/envelopes/{eid}/approve",
                             json={"actor": "tester"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_approve_not_found_returns_404(self):
        r = self.client.post("/api/envelopes/missing/approve",
                             json={"actor": "tester"})
        self.assertEqual(r.status_code, 404, r.text)

    # ── reject ──────────────────────────────────────────────────────────────────

    def test_reject_happy_path(self):
        eid, _ = _capture_and_advance(self.db_path)
        r = self.client.post(f"/api/envelopes/{eid}/reject",
                             json={"actor": "tester", "reason": "not relevant"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["envelope"]["state"], "REJECTED")

    # ── edit ────────────────────────────────────────────────────────────────────

    def test_edit_adds_version(self):
        eid, _ = _capture_and_advance(self.db_path)
        r = self.client.post(f"/api/envelopes/{eid}/edit",
                             content=b"Edited content",
                             headers={"content-type": "application/octet-stream"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("version_id", body)
        # Now there should be 2 versions (captured + edited)
        detail = self.client.get(f"/api/envelopes/{eid}")
        self.assertEqual(len(detail.json()["versions"]), 2)

    def test_edited_version_is_previewed_and_delivered_when_selected(self):
        eid, _ = _capture_and_advance(self.db_path, content=b"Original")
        edited = self.client.post(f"/api/envelopes/{eid}/edit", content=b"Edited",
                                  headers={"content-type": "application/octet-stream"})
        self.assertEqual(edited.status_code, 200)
        vid = edited.json()["version_id"]
        preview = self.client.get(f"/api/envelopes/{eid}/content?version_id={vid}")
        self.assertEqual(preview.text, "Edited")
        approved = self.client.post(f"/api/envelopes/{eid}/approve",
                                    json={"actor": "tester", "version_id": vid})
        self.assertEqual(approved.status_code, 200, approved.text)
        delivered = Path(approved.json()["delivered_path"])
        self.assertEqual(delivered.read_bytes(), b"Edited")

    def test_edit_after_delivery_is_rejected(self):
        eid, _ = _capture_and_advance(self.db_path)
        approved = self.client.post(f"/api/envelopes/{eid}/approve",
                                    json={"actor": "tester"})
        self.assertEqual(approved.status_code, 200)
        response = self.client.post(f"/api/envelopes/{eid}/edit", content=b"late",
                                    headers={"content-type": "application/octet-stream"})
        self.assertEqual(response.status_code, 409)

    # ── reconcile ───────────────────────────────────────────────────────────────

    def test_reconcile_empty(self):
        r = self.client.post("/api/reconcile")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"], [])

    # ── health ──────────────────────────────────────────────────────────────────

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
