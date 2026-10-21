# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

PromptBridge / Conductor: a local orchestrator that runs AI coding assistants (Codex, Claude Code, …) as a supervised team (writer → reviewer → writer), with human approval, a SQLite state machine and a React panel. Code lives in `ai-orchestrator/`; planning and collaboration records live in `_isbirligi/` (local only, git-ignored; may be absent in a fresh clone).

## Start of every session

If `_isbirligi/` exists, read `_isbirligi/KARARLAR.md` (append-only decision log, newest first) and `_isbirligi/DURUM.md` (kanban status) before doing anything. Task cards are in `_isbirligi/gorevler/`, delivery/review reports in `_isbirligi/teslimler/`. These records, the README and the architecture doc are written in Turkish; keep them in Turkish.

## Commands (run in `ai-orchestrator/`)

- Tests: `.venv/bin/python -m unittest discover -s tests -v` (stdlib unittest, real temp SQLite, no provider quota). Single test: `.venv/bin/python -m unittest tests.test_store -v`.
- `tests/smoke_*.py` call real providers and consume quota; never run them unless asked. `tests/preview_*.py` are browser fixtures with fake providers.
- Server (watcher + API + panel): `.venv/bin/python -m app serve` → panel `http://127.0.0.1:8765/ui/`.
- UI (`ui/`, React + Vite + TS): `npm run build` type-checks (`tsc -b`) and writes to `app/static/`, which FastAPI serves. `npm run dev` proxies `/api` to :8765.
- Operations (start/stop, R1 deploy, backup/restore, troubleshooting): `OPERATOR-REHBERI.md` (Turkish). `GET /health` reports the running commit, schema and panel bundle.

## Architecture

- `app/store.py` + `schema.sql`: SQLite, versioned idempotent migrations (unknown version is refused), WAL, `BEGIN IMMEDIATE` writes. `versions`/`history` are append-only (triggers). Use `store.transaction(conn)`, never raw `with conn:`; one connection per thread (K-006).
- `app/transitions.py`: the only place that decides legal state transitions; `store.set_state` is just CAS + audit.
- Envelope flow (Faz 1–2): watcher captures outbox files → content frozen at capture (K-003) → human approval → atomic exclusive-create delivery to a unique inbox file (K-004). DELIVERED means "İletildi", not "processed" (K-005).
- Works/sessions (Faz 2–3): `runner.py`, `work_dispatcher.py`, `adapters/`, `provider_policy.py` run provider CLIs in process groups with a workspace lock. Uncertain state → `RECOVERY_REQUIRED`, lock kept. Provider chains come from `.system/providers.yaml` (template `providers.example.yaml`); a session freezes its policy digest at start.

## Process rules

- Implementer and independent reviewer must be different assistants. A task is done only after review plus the project owner's explicit acceptance in their own words; record it in DURUM.md.
- Architecture deviations: stop, write the decision to KARARLAR.md first, then implement.
- Feature work happens in a separate worktree/branch (`../PromptBridge-<branch>`), merged to `main` only after acceptance.
- In the main checkout never run `git stash`, `reset` or `checkout`; review with `git diff main...<branch>` or a worktree.
- The live instance runs from this checkout with `ai-orchestrator/.system/state.db`. Don't stop/restart the server or touch `.system/` unless asked; back up the DB before a schema migration goes live.
- Keep the security boundaries: loopback only, `mutation_guard` on every mutating endpoint, prompts never passed through a shell, reviewer profiles read-only.
