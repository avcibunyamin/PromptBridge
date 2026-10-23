"""Hermetic provider executables for tests: importing this points CONDUCTOR_*_BIN at harmless stubs.

Tests fake every provider run, but several code paths still resolve the CLI first ("is codex installed?").
Without this they passed only on a machine with the real CLIs installed (and failed in CI)."""
import atexit
import os
import shutil
import stat
import tempfile
from pathlib import Path

_DIR = Path(tempfile.mkdtemp(prefix='pb-fakebin-'))
atexit.register(shutil.rmtree, _DIR, True)
for _name, _env in (('claude', 'CONDUCTOR_CLAUDE_BIN'), ('codex', 'CONDUCTOR_CODEX_BIN'), ('agy', 'CONDUCTOR_AGY_BIN')):
    _path = _DIR / _name
    _path.write_text('#!/bin/sh\nexit 0\n')
    _path.chmod(_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ[_env] = str(_path)
