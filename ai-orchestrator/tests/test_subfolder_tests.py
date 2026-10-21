"""Subfolder detection, approval digest, and execution cwd tests for test_runner."""
import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import preview, store, test_runner


class SubfolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "ws"
        self.root.mkdir()
        self.db_path = Path(self.temp.name) / "state.db"
        self.conn = store.connect(self.db_path)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def test_subfolder_detected_when_root_has_no_markers(self):
        alt = self.root / "alt"
        (alt / "tests").mkdir(parents=True)
        (alt / "requirements.txt").write_text("unittest-xml-reporting\n")
        s = test_runner.detect(str(self.root), "tests")
        self.assertIsNotNone(s)
        self.assertEqual(s["cwd"], "alt")
        self.assertEqual(s["kind"], "unittest")
        self.assertEqual(s["script_path"], "tests")
        self.assertEqual(s["markers"], {"requirements.txt": preview._sha_file(alt / "requirements.txt")})

        # Sorted order: 'a_first' wins over 'alt'
        first = self.root / "a_first"
        (first / "tests").mkdir(parents=True)
        (first / "requirements.txt").write_text("")   # a project of its own (K-042 review: a marker is required)
        s2 = test_runner.detect(str(self.root), "tests")
        self.assertEqual(s2["cwd"], "a_first")

        # Subfolder .venv interpreter and lint config
        venv_py = alt / ".venv" / "bin" / "python"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_text("#!/bin/sh\n")
        (alt / "pytest.ini").write_text("[pytest]\n")
        (alt / "ruff.toml").write_text("line-length = 100\n")
        shutil.rmtree(first)

        s_pytest = test_runner.detect(str(self.root), "tests")
        self.assertEqual(s_pytest["cwd"], "alt")
        self.assertEqual(s_pytest["kind"], "pytest")
        self.assertEqual(s_pytest["argv"][0], str(venv_py))
        self.assertEqual(s_pytest["script_digest"], preview._sha_file(alt / "pytest.ini"))

        s_lint = test_runner.detect(str(self.root), "lint")
        self.assertEqual(s_lint["cwd"], "alt")
        self.assertEqual(s_lint["kind"], "ruff")
        self.assertEqual(s_lint["script_path"], "ruff.toml")
        self.assertEqual(s_lint["script_digest"], preview._sha_file(alt / "ruff.toml"))

    def test_root_marker_takes_precedence_with_dot_cwd(self):
        (self.root / "tests").mkdir()
        (self.root / "requirements.txt").write_text("pytest\n")
        alt = self.root / "alt"
        (alt / "tests").mkdir(parents=True)
        (alt / "requirements.txt").write_text("pytest\n")

        s = test_runner.detect(str(self.root), "tests")
        self.assertIsNotNone(s)
        self.assertEqual(s["cwd"], ".")
        self.assertEqual(s["kind"], "unittest")
        self.assertEqual(s["script_path"], "tests")

    def test_cwd_change_changes_digest_and_invalidates_approval(self):
        alt = self.root / "alt"
        (alt / "tests").mkdir(parents=True)
        (alt / "requirements.txt").write_text("foo==1.0\n")
        s_alt = test_runner.detect(str(self.root), "tests")
        self.assertEqual(s_alt["cwd"], "alt")

        st = test_runner.approve(self.conn, str(self.root), s_alt["command_digest"], "tests")
        self.assertTrue(st["approved"])
        self.assertEqual(st["suggestion"]["cwd"], "alt")
        self.assertEqual(test_runner.plan(self.conn, str(self.root), "tests")["status"], "ready")

        # Move identical files to another subfolder -> cwd changes -> digest changes -> approval becomes stale
        baska = self.root / "baska"
        alt.rename(baska)
        s_baska = test_runner.detect(str(self.root), "tests")
        self.assertEqual(s_baska["cwd"], "baska")
        self.assertNotEqual(s_alt["command_digest"], s_baska["command_digest"])

        st_after = test_runner.status(self.conn, str(self.root), "tests")
        self.assertFalse(st_after["approved"])
        self.assertTrue(st_after["stale"])
        self.assertEqual(
            test_runner.plan(self.conn, str(self.root), "tests"),
            {"status": "not_run", "reason": "approval_stale"},
        )

        # Also different from root cwd '.'
        (baska / "tests").rename(self.root / "tests")
        (baska / "requirements.txt").rename(self.root / "requirements.txt")
        baska.rmdir()
        s_root = test_runner.detect(str(self.root), "tests")
        self.assertEqual(s_root["cwd"], ".")
        self.assertNotEqual(s_alt["command_digest"], s_root["command_digest"])

    def test_excluded_and_hidden_directories_not_scanned(self):
        for skipped in ("node_modules", ".venv", "venv", "dist", "build", ".gizli", ".git"):
            d = self.root / skipped
            (d / "tests").mkdir(parents=True)
            (d / "pytest.ini").write_text("[pytest]\n")
            (d / "ruff.toml").write_text("line-length = 88\n")

        self.assertIsNone(test_runner.detect(str(self.root), "tests"))
        self.assertIsNone(test_runner.detect(str(self.root), "lint"))

    def test_plan_and_run_use_subfolder_cwd(self):
        alt = self.root / "alt"
        (alt / "tests").mkdir(parents=True)
        (alt / "tests" / "__init__.py").write_text("")
        (alt / "tests" / "test_ok.py").write_text(
            "import os, pathlib, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_cwd(self):\n"
            "        self.assertEqual(pathlib.Path(os.getcwd()).name, 'alt')\n"
        )
        (alt / "ruff.toml").write_text("line-length = 100\n")

        # Lint needs .venv/bin/ruff inside alt/
        s_lint = test_runner.detect(str(self.root), "lint")
        test_runner.approve(self.conn, str(self.root), s_lint["command_digest"], "lint")
        self.assertEqual(test_runner.plan(self.conn, str(self.root), "lint")["reason"], "deps_missing")
        ruff_bin = alt / ".venv" / "bin" / "ruff"
        ruff_bin.parent.mkdir(parents=True)
        ruff_bin.write_text("#!/bin/sh\n")
        self.assertEqual(test_runner.plan(self.conn, str(self.root), "lint")["status"], "ready")

        # Run unittest in alt/
        s_tests = test_runner.detect(str(self.root), "tests")
        seen = {}

        def fake_sandboxed(argv, workspace, env, home_write=True):
            seen["workspace"] = workspace
            seen["home_write"] = home_write
            env["TMPDIR"] = str(self.root)
            return argv

        with patch("app.preview.sandboxed", side_effect=fake_sandboxed):
            res = asyncio.run(test_runner.run(s_tests, str(self.root)))
        self.assertEqual(res["status"], "passed", res)
        self.assertEqual(seen["workspace"], str(self.root))
        self.assertFalse(seen["home_write"])


if __name__ == "__main__":
    unittest.main()


class RootDigestUnchanged(unittest.TestCase):
    def test_a_root_suggestion_keeps_its_earlier_digest(self):
        """K-042 review (Claude): adding `cwd` must not invalidate approvals of root commands."""
        import tempfile
        from pathlib import Path
        from app import test_runner, preview
        root = Path(tempfile.mkdtemp()); (root / 'tests').mkdir()
        s = test_runner.detect(str(root))
        old = {'kind': s['kind'], 'command': s['command'], 'script_path': s['script_path'],
               'script_digest': s['script_digest'], 'markers': s['markers']}
        self.assertEqual(s['cwd'], '.')
        self.assertEqual(s['command_digest'], preview._digest_payload(old))

    def test_a_subfolder_without_a_marker_is_not_a_project(self):
        import tempfile
        from pathlib import Path
        from app import test_runner
        root = Path(tempfile.mkdtemp()); (root / 'moved').mkdir(); (root / 'moved' / 'test_x.py').write_text('')
        self.assertIsNone(test_runner.detect(str(root)))                     # test files alone: not a subproject
        (root / 'moved' / 'requirements.txt').write_text('')
        self.assertEqual(test_runner.detect(str(root))['cwd'], 'moved')
