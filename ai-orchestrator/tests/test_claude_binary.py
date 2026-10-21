"""Hotfix (2026-10-07): Claude Code is found in both layouts the Claude app has used, newest first."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.adapters.claude import claude_binary


class ClaudeBinary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / 'Library/Application Support/Claude/claude-code'
        env = patch.dict(os.environ, {'PATH': '/nonexistent'}); env.start(); self.addCleanup(env.stop)
        os.environ.pop('CONDUCTOR_CLAUDE_BIN', None)
        home = patch.object(Path, 'home', return_value=self.home); home.start(); self.addCleanup(home.stop)

    def make(self, *parts, age=0):
        exe = self.root.joinpath(*parts, 'claude.app/Contents/MacOS/claude')
        exe.parent.mkdir(parents=True); exe.write_text('#!/bin/sh\n'); exe.chmod(0o755)
        os.utime(exe, (time.time() - age, time.time() - age))
        return exe.resolve()             # /var → /private/var on macOS

    def test_old_and_new_layout_newest_wins(self):
        with self.assertRaises(FileNotFoundError):
            claude_binary()
        old = self.make('2.1.284', age=100)                       # <version>/claude.app
        self.assertEqual(Path(claude_binary()).resolve(), old)
        new = self.make('2.1.289', 'ee67e3f1ea60')               # <version>/<hash>/claude.app (2.1.289+)
        self.assertEqual(Path(claude_binary()).resolve(), new)


if __name__ == '__main__':
    unittest.main()
