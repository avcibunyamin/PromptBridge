"""Conductor entry point.

With no command, retain the G1 health message.  With a command, dispatch the
Phase 1 CLI (``python -m app watch|list|...``).
"""
import sys
from .cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("Conductor Faz 1 — iskelet ayakta")
        raise SystemExit(0)
    sys.exit(main())
