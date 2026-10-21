"""TB-46 tests: Cache-Control headers for /ui and /assets static mounts."""

import os
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from app.api import create_app


class TB46CacheHeaderTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.root = Path(self.tmp.name)
        (self.root / ".system").mkdir(parents=True, exist_ok=True)
        os.environ["AI_ORCHESTRATOR_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "AI_ORCHESTRATOR_ROOT", None)

        self.static_dir = self.root / "static"
        self.assets_dir = self.static_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        (self.static_dir / "index.html").write_text(
            "<!doctype html><html><body>Conductor</body></html>",
            encoding="utf-8",
        )
        (self.static_dir / "manifest.webmanifest").write_text(
            '{"name":"Conductor"}',
            encoding="utf-8",
        )
        (self.assets_dir / "index-x.js").write_text(
            'console.log("bundle");',
            encoding="utf-8",
        )

        self.app = create_app(static_dir=self.static_dir)
        self.client = TestClient(self.app)

    def test_ui_html_returns_no_cache(self):
        r = self.client.get("/ui/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("content-type", ""))
        self.assertEqual(r.headers.get("cache-control"), "no-cache")

        # Non-HTML under /ui keeps manifest MIME type and does not force no-cache
        m = self.client.get("/ui/manifest.webmanifest")
        self.assertEqual(m.status_code, 200)
        self.assertIn("application/manifest+json", m.headers.get("content-type", ""))
        self.assertNotEqual(m.headers.get("cache-control"), "no-cache")

    def test_assets_existing_file_returns_immutable(self):
        r = self.client.get("/assets/index-x.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.headers.get("cache-control"),
            "public, max-age=31536000, immutable",
        )

    def test_assets_missing_file_returns_404_without_immutable(self):
        r = self.client.get("/assets/nonexistent.js")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("immutable", r.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
