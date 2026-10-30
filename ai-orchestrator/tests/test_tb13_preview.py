import asyncio
import json
import os
import socket
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from app import preview, store
from app.api import create_app
from tests import netcheck


class PreviewCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_vite_detection_uses_argv_and_digest_changes_with_package(self):
        project = self.root / "vite"; project.mkdir()
        pkg = {"scripts": {"dev": "vite --clearScreen false"}, "devDependencies": {"vite": "^5"}}
        (project / "package.json").write_text(json.dumps(pkg))
        with patch("app.preview._which", return_value="/usr/bin/npm"):
            first = preview.detect(str(project), port=5174)
            self.assertEqual(first["argv"], ["/usr/bin/npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5174"])
            self.assertEqual(first["env"]["HOST"], "127.0.0.1")
            pkg["scripts"]["dev"] = "vite --host 0.0.0.0"
            (project / "package.json").write_text(json.dumps(pkg))
            second = preview.detect(str(project), port=5174)
        self.assertNotEqual(first["command_digest"], second["command_digest"])

    def test_package_json_scripts_order_truncation_and_stable_digest(self):
        project = self.root / "npm_scripts"; project.mkdir()
        pkg_file = project / "package.json"
        pkg = {
            "scripts": {
                "postdev": "x" * 600,
                "dev": "vite --clearScreen false",
                "predev": "echo before",
                "build": "vite build",
            },
            "devDependencies": {"vite": "^5"},
        }
        pkg_file.write_text(json.dumps(pkg))
        with patch("app.preview._which", return_value="/usr/bin/npm"):
            detected = preview.detect(str(project), port=5174)
        self.assertEqual(detected["scripts"], [
            ["predev", "echo before"],
            ["dev", "vite --clearScreen false"],
            ["postdev", "x" * 500 + "…"],
        ])
        expected_digest = preview._digest_payload({
            "workspace_path": str(project.resolve()),
            "kind": "vite",
            "argv": ["/usr/bin/npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5174"],
            "env": {"HOST": "127.0.0.1", "PORT": "5174", "BROWSER": "none"},
            "script_path": "package.json",
            "script_digest": preview._sha_file(pkg_file),
        })
        self.assertEqual(detected["command_digest"], expected_digest)

        pkg["scripts"] = {"prestart": None, "start": "a" * 500, "poststart": 123}
        del pkg["devDependencies"]
        pkg_file.write_text(json.dumps(pkg))
        with patch("app.preview._which", return_value="/usr/bin/npm"):
            start_detected = preview.detect(str(project), port=3000)
        self.assertEqual(start_detected["scripts"], [["start", "a" * 500]])

        py_project = self.root / "py_only"; py_project.mkdir()
        (py_project / "app.py").write_text("print('ok')\n")
        self.assertNotIn("scripts", preview.detect(str(py_project), port=8000))
        static_project = self.root / "static_only"; static_project.mkdir()
        (static_project / "index.html").write_text("<h1>ok</h1>\n")
        self.assertNotIn("scripts", preview.detect(str(static_project), port=8080))

    def test_static_site_is_served_by_the_file_server_on_loopback(self):
        project = self.root / "site"; project.mkdir()
        (project / "index.html").write_text("<h1>hi</h1>")
        first = preview.detect(str(project), port=8099)
        self.assertEqual(first["kind"], "static")
        self.assertEqual(first["argv"][1:], ["-m", "http.server", "8099", "--bind", "127.0.0.1", "--directory", str(project.resolve())])
        (project / "index.html").write_text("<h1>edited</h1>")                   # editing the page keeps the approval
        self.assertEqual(preview.detect(str(project), port=8099)["command_digest"], first["command_digest"])
        (project / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))   # a real project wins
        self.assertNotEqual(preview.detect(str(project), port=8099)["kind"], "static")
        empty = self.root / "empty"; empty.mkdir()
        with self.assertRaises(preview.PreviewError):
            preview.detect(str(empty))

    def test_env_is_an_allowlist(self):
        secrets = {"CODEX_HOME": "/secret", "CONDUCTOR_X": "1", "AI_ORCHESTRATOR_ROOT": "/r", "OPENAI_API_KEY": "k",
                   "GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "a", "SSH_AUTH_SOCK": "/s", "NPM_TOKEN": "n"}
        kept = {"PATH": "/bin", "HOME": "/Users/u", "LANG": "tr_TR.UTF-8", "LC_ALL": "tr_TR.UTF-8", "TMPDIR": "/t"}
        with patch.dict(os.environ, {**secrets, **kept}, clear=True):
            env = preview.sanitized_env({"PORT": "5000", "HOST": "127.0.0.1"})
        self.assertEqual(env, {**kept, "PORT": "5000", "HOST": "127.0.0.1"})   # nothing else, in particular no secret

    def test_ready_accepts_4xx_http_error_and_waits_on_5xx(self):
        class Writer:
            def write(self, _data): pass
            async def drain(self): pass
            def close(self): pass

        async def response(status):
            reader = asyncio.StreamReader()
            reader.feed_data(f"HTTP/1.1 {status} Response\r\nContent-Length: 0\r\n\r\n".encode())
            reader.feed_eof()
            return reader, Writer()

        async def not_found(*_args):
            return await response(404)

        async def server_error(*_args):
            return await response(500)

        with patch("app.preview.asyncio.open_connection", side_effect=not_found):
            self.assertTrue(asyncio.run(preview._ready("http://127.0.0.1:8000/", timeout=0.05)))
        with patch("app.preview.asyncio.open_connection", side_effect=server_error), patch("app.preview.POLL_INTERVAL", 0.01):
            self.assertFalse(asyncio.run(preview._ready("http://127.0.0.1:8000/", timeout=0.03)))

    def test_ready_bounds_a_server_that_never_finishes_response_headers(self):
        class Writer:
            def write(self, _data): pass
            async def drain(self): pass
            def close(self): pass

        async def incomplete_response(*_args):
            reader = asyncio.StreamReader()
            reader.feed_data(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            return reader, Writer()

        with patch("app.preview.asyncio.open_connection", side_effect=incomplete_response):
            self.assertFalse(asyncio.run(preview._ready("http://127.0.0.1:8000/", timeout=0.03)))

    def test_migration_adds_preview_tables(self):
        db = self.root / "state.db"
        conn = store.connect(db); self.addCleanup(conn.close)
        store.migrate(conn)
        self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], store.SCHEMA_VERSION)   # TB-12: the current version, not a fixed one
        for table in ("preview_approvals", "preview_servers"):
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table)

    def test_1_8_database_migrates_to_1_9_losslessly(self):
        db = self.root / "state.db"
        conn = store.connect(db); self.addCleanup(conn.close)
        store.migrate(conn)
        store.insert_captured(conn, {
            "id": "msg_tb13_18",
            "source_agent": "chatgpt",
            "target_agent": "claude",
            "title": "TB-13 migration",
        }, b"preview migration keeps existing rows")
        before = {
            "envelopes": conn.execute("SELECT count(*) FROM envelopes").fetchone()[0],
            "versions": conn.execute("SELECT count(*) FROM versions").fetchone()[0],
            "history": conn.execute("SELECT count(*) FROM history").fetchone()[0],
        }
        conn.execute("DROP TABLE preview_approvals")
        conn.execute("DROP TABLE preview_servers")
        conn.execute("UPDATE schema_meta SET value='1.8' WHERE key='schema_version'")

        store.migrate(conn)
        store.migrate(conn)

        self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], store.SCHEMA_VERSION)   # TB-12: the current version, not a fixed one
        for table in ("preview_approvals", "preview_servers"):
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone(), table)
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(
            {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in before},
            before,
        )
        self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())


class PreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        # F5-Kapanış: a per-test default port instead of 8000, so parallel suite runs do not share one port.
        self.default_port = preview.free_port()
        choose = preview._choose_port
        chooser = patch("app.preview._choose_port", side_effect=lambda _default: choose(self.default_port))
        chooser.start(); self.addCleanup(chooser.stop)
        self.root = Path(self.tmp.name)
        (self.root / ".system").mkdir()
        self.db = self.root / ".system/state.db"
        env = patch.dict(os.environ, {"AI_ORCHESTRATOR_ROOT": str(self.root)})
        env.start(); self.addCleanup(env.stop)
        self.app = create_app()
        self.project = self.root / "project"; self.project.mkdir()

    def _write_server(self, body: str = "ok"):
        (self.project / "app.py").write_text(textwrap.dedent(f"""
            import os
            from http.server import BaseHTTPRequestHandler, HTTPServer
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write({body!r}.encode())
                def log_message(self, *_):
                    pass
            HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
        """))

    @unittest.skipUnless(netcheck.loopback_http_ok(), netcheck.SKIP_REASON)
    def test_approve_start_status_stop_e2e(self):
        self._write_server("preview-ok")
        with TestClient(self.app) as client:
            detected = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            self.assertEqual(detected["kind"], "python-http")
            initial = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()
            self.assertFalse(initial["approved"])
            self.assertEqual(client.post("/api/previews/start", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"]
            }).status_code, 409)
            r = client.post("/api/previews/approve", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"], "actor": "tester"
            })
            self.assertEqual(r.status_code, 200)
            started = client.post("/api/previews/start", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"]
            })
            self.assertEqual(started.status_code, 200, started.text)
            server = started.json()["server"]
            self.assertEqual(server["status"], "running")
            body = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()
            self.assertTrue(body["approved"])
            self.assertEqual(body["server"]["url"], detected["url"])
            stopped = client.post("/api/previews/stop", json={"workspace_path": str(self.project)})
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json()["server"]["status"], "stopped")

    @unittest.skipUnless(netcheck.loopback_http_ok(), netcheck.SKIP_REASON)
    def test_fallback_port_stays_stable_across_approve_status_and_start(self):
        self._write_server("fallback-port")
        chosen_port = preview.free_port()
        other_port = preview.free_port()
        with (
            TestClient(self.app) as client,
            patch("app.preview._port_available", side_effect=lambda port: port != self.default_port),  # default taken
            patch("app.preview.free_port", side_effect=[chosen_port, other_port]),
        ):
            detected = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()["suggestion"]
            self.assertEqual(detected["port"], chosen_port)
            approved = client.post("/api/previews/approve", json={
                "workspace_path": str(self.project),
                "command_digest": detected["command_digest"],
                "port": detected["port"],
            })
            self.assertEqual(approved.status_code, 200, approved.text)
            status = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()
            self.assertTrue(status["approved"])
            self.assertEqual(status["suggestion"]["port"], chosen_port)
            started = client.post("/api/previews/start", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"]
            })
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["server"]["url"], f"http://127.0.0.1:{chosen_port}/")

    def test_start_refuses_a_port_already_in_use(self):
        self._write_server("never-started")
        with socket.socket() as busy, TestClient(self.app) as client:
            detected = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            body = {"workspace_path": str(self.project), "command_digest": detected["command_digest"]}
            self.assertEqual(client.post("/api/previews/approve", json=body).status_code, 200)
            busy.bind(("127.0.0.1", detected["port"])); busy.listen()      # another server takes the port before start
            r = client.post("/api/previews/start", json=body)
            self.assertEqual((r.status_code, r.json()["code"]), (409, "port_in_use"))
            self.assertIsNone(client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()["server"])

    def test_a_foreign_server_answering_is_not_running_when_ours_died(self):
        # Codex review P2: the port was free at the check, then another server took it; ours cannot bind and exits.
        self._write_server("ours")
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class Foreign(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"foreign")

            def log_message(self, *_):
                pass

        with TestClient(self.app) as client:
            detected = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            body = {"workspace_path": str(self.project), "command_digest": detected["command_digest"]}
            self.assertEqual(client.post("/api/previews/approve", json=body).status_code, 200)
            foreign = HTTPServer(("127.0.0.1", detected["port"]), Foreign)
            threading.Thread(target=foreign.serve_forever, daemon=True).start()
            self.addCleanup(foreign.server_close); self.addCleanup(foreign.shutdown)
            with patch("app.preview._port_available", return_value=True), patch("app.preview.SETTLE", 10):
                r = client.post("/api/previews/start", json=body)
            self.assertEqual((r.status_code, r.json()["code"]), (409, "process_exited"))
            server = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()["server"]
            self.assertEqual(server["status"], "failed")

    def test_digest_change_requires_reapproval(self):
        self._write_server("one")
        with TestClient(self.app) as client:
            old = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            self.assertEqual(client.post("/api/previews/approve", json={
                "workspace_path": str(self.project), "command_digest": old["command_digest"]
            }).status_code, 200)
            self._write_server("two")
            new = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            self.assertNotEqual(old["command_digest"], new["command_digest"])
            changed = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()
            self.assertFalse(changed["approved"])
            self.assertEqual(client.post("/api/previews/approve", json={
                "workspace_path": str(self.project), "command_digest": old["command_digest"]
            }).status_code, 409)
            self.assertEqual(client.post("/api/previews/start", json={
                "workspace_path": str(self.project), "command_digest": new["command_digest"]
            }).status_code, 409)

    def test_start_timeout_marks_failed_and_cleans_process(self):
        (self.project / "app.py").write_text("import time; time.sleep(30)\n")
        with TestClient(self.app) as client, patch("app.preview.READY_TIMEOUT", 0.35):
            detected = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            client.post("/api/previews/approve", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"]
            })
            r = client.post("/api/previews/start", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"]
            })
            self.assertEqual(r.status_code, 504)
            status = client.get("/api/previews/status", params={"workspace_path": str(self.project)}).json()["server"]
            self.assertEqual(status["status"], "failed")

    def test_mutating_preview_endpoints_reject_cross_site(self):
        self._write_server("x")
        with TestClient(self.app) as client:
            detected = client.get("/api/previews/detect", params={"workspace_path": str(self.project)}).json()
            r = client.post("/api/previews/approve", json={
                "workspace_path": str(self.project), "command_digest": detected["command_digest"]
            }, headers={"origin": "https://evil.example"})
            self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
