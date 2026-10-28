# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-10-28

### Added
- Setup wizard (`#/setup`): each installation step with its status and a command to copy, a progress bar and a recheck button; it opens on a fresh install. Backed by `GET /api/setup/checks`, the same read-only checks as `python -m app doctor` (not available from the phone).
- Team editor: reorder a chain's links by dragging the handle (or with ↑/↓ on it); the top link is the primary.

### Changed
- New look: translucent glass surfaces over a soft ambient background, a floating sidebar, smooth transitions and clickable cards that lift on hover. Motion turns off with the system's reduced-motion setting; browsers without backdrop blur get plain surfaces. No new dependencies.
- Settings are split into tabs (team, accounts, models, appearance, integrations, phone); the tab is in the URL (`#/settings/<tab>`) and remembered.
- Overview: clickable status cards with icons, relative times in recent works and a quick start card with recipes.
- `doctor` shows Node.js's version without the install path.
- README: a setup wizard section with screenshots.

### Fixed
- A run that timed out could refuse to stop its own process, and recovery could then release the folder lock while that process kept running: process identity now tolerates macOS's second-scale jitter in the reported boot time.

## [0.3.11] - 2026-10-28

### Security
- Mac app: only the panel itself can open a new window in the default browser; a page in the preview frame cannot. The app treats the server as running only when `/health` is Conductor's.

### Fixed
- Preview readiness check is fully bounded in time.
- Envelope front matter is read line by line (closing marker within the first 50 lines; the first value of a repeated key wins), and a failed source never overwrites an earlier one in `.failed/`.
- On shutdown, preview processes are stopped even when the database cannot be opened; every file under `/ui` is served with `Cache-Control: no-cache`.
- A budget stop on a loop that already finished writes no record; the Antigravity quota probe folder must be a real directory with mode 0700.
- `deploy/launchd.sh install` checks the new plist before stopping the running agent.
- Tests that could fail on a slow machine (quota probe terminal mode, catalog hang) are now stable.

### Changed
- Operator guide: remove `state.db-wal`/`-shm` before restoring a backup, a backup command that never overwrites, full rehearsal commands, notes on `restart`, environment variables under launchd, and `doctor`.

## [0.3.10] - 2026-10-28

### Security
- The server stays on loopback: `serve --host` accepts only `127.0.0.1`, `::1` or `localhost`, and the app answers only loopback clients even when started another way.

### Added
- `providers.agy-only.yaml` preset (two Antigravity accounts).
- `doctor` warns when Node.js is older than 22.6.

### Changed
- README (English and Turkish): `python3 -m venv`, one `cp` line per preset, sign-in order, notes on the doctor warning before `serve`, `--port`, `--help`, and stopping `serve` before installing launchd.

### Fixed
- The live event stream no longer leaks a subscription when a client disconnects before the first message.

## [0.3.9] - 2026-10-28

### Added
- Preview, test and lint approvals show the `package.json` commands npm will actually run, including `pre`/`post` hooks. Existing approvals stay valid.

### Fixed
- Git check after a run: files are opened without following links or blocking; an unreadable hooks folder is reported instead of failing; nested git folders under `modules/` and `worktrees/` are scanned case-insensitively with a separate limit each, and their `info/attributes` and `objects/info/alternates` are watched. A symlink with a non-UTF-8 target no longer breaks the workspace snapshot.
- Project instructions: an imported file must not be a hard link, at most 64 imports are followed, a backtick fence whose info string contains a backtick is not a fence, and indented lines are neither fences nor imports. A hard-linked root `CLAUDE.md` keeps working.

## [0.3.8] - 2026-10-28

### Fixed
- Database: when SQLite ends a transaction by itself inside a nested block, the original error is reported instead of "no such savepoint".
- Quota view: percentages stay between 0 and 100 and true/false values are not read as numbers; a deleted account no longer stays in the view.
- GitHub: a failed or timed-out clone leaves no half repository that a later clone would treat as done; the wait after killing a timed-out command is bounded; unexpected `gh` JSON no longer causes a server error; credentials inside URLs are masked in error text; secret file names (`.ENV`, `prod.env`, `.netrc`, `.npmrc`, `*.p12`, `*.pfx`) are recognised regardless of case.
- Quota wait: one plan's error no longer skips the other due plans or leaves the plan stuck until a restart.
- Provider checks: a command that prints a lot no longer times out because its output was not drained; a warning line before Claude's status JSON no longer hides a signed-in session; the catalog skips invalid model names and malformed Codex entries.

## [0.3.7] - 2026-10-26

### Fixed
- Envelope outbox: only regular files with a single link and at most 10 MB are captured; symlinks, FIFOs, hard links and oversized files move to `.failed/` instead of being read (a FIFO no longer stalls the watcher). Envelope ids carry more randomness; front matter with a UTF-8 BOM or CRLF line endings is read; very long file names are archived within the 255-byte limit.
- Delivery: an envelope stays delivered when the receiver takes the file right away; one unreadable file during recovery no longer stops the others.
- Preview: a server that answers 4xx at its root counts as ready.
- Settings: simultaneous saves no longer lose each other's changes; a broken settings file is backed up before it is replaced; a team with an unknown role is refused.

## [0.3.6] - 2026-10-26

### Fixed
- Queue: a run stopped at a denied tool can no longer be continued (it must be cancelled first, with a clear message); deleting a work cancels its queued messages; repeating a create request returns the existing work before the path is checked again; one work's error no longer stops the dispatcher for the others.
- Phone gateway: paths with `.`/`..` segments, backslashes or empty segments are refused; the pairing limit is per user; refused requests count against the device rate; a device revoked while a request waits is refused; an audit row is written even when the app raises; over-long login names are refused instead of shortened.
- Push notifications: endpoints with whitespace, control characters, backslashes, bad ports or a non-canonical form are refused; a revoked device can no longer subscribe.

## [0.3.5] - 2026-10-23

### Added
- Continuous integration (GitHub Actions): backend tests on macOS and Linux, UI tests and build on Linux, and a check that the committed panel in `app/static/` matches a fresh build. Read-only permissions; actions pinned to commit SHAs.

### Fixed
- Tests no longer depend on the provider CLIs installed on the machine (they use stub executables), so they pass on a clean machine.
- Tests that start a real local HTTP server skip, with a reason, where loopback HTTP does not answer.

### Changed
- Documentation: Node.js 22.6+ is needed to run the UI tests or rebuild the panel; CI badge; contributor notes about CI.

## [0.3.4] - 2026-10-23

### Changed
- README and user guides: the built panel ships in `app/static/` (Node.js is needed only to rebuild it); a "Your first work" section; `python -m app doctor` and the interface language are documented.
- One English label for "Bring to main folder"; internal decision codes removed from interface texts (with a test).

## [0.3.3] - 2026-10-23

### Added
- `python -m app doctor`: checks Python, `.system/providers.yaml`, provider CLIs, the built panel, Node and the local server without calling any provider.
- A panel screenshot in the README (demo fixture, fake providers).

### Changed
- SECURITY.md: the Antigravity per-account keychain is created with an empty password and kept unlocked (any process of the same macOS user can read it); code and prompts go to the cloud providers you choose.
- Turkish README no longer says "fully local".

## [0.3.2] - 2026-10-23

### Added
- English-first README (Turkish moved to `README.tr.md`) with provider sign-in steps, a "what runs without asking you" section, data flow & security, a platform table and a terms-of-service note.
- Provider presets: `providers.claude-only.yaml`, `providers.codex-only.yaml`, `providers.ollama-local.yaml`.
- Issue and pull request templates; an external contributor section in CONTRIBUTING.

### Fixed
- `deploy/launchd.sh install` now starts an agent that macOS loaded but did not start.

## [0.3.1] - 2026-10-23

### Fixed
- Server error messages are shown in the interface language (54 more error codes translated); details such as file lists or git output are kept.

## [0.3.0] - 2026-10-22

### Added
- English user interface. Choose the language in Settings → Appearance; it is saved on each device. Without a saved choice the panel follows the browser language (Turkish for `tr`, English otherwise).
- This changelog.

### Changed
- Accessibility: keyboard focus returns to the work after renaming it and to the edit button after editing a title; Escape cancels title editing; long command output is keyboard-scrollable; the event feed is announced as a live log.

## [0.2.0] - 2026-10-21

### Added
- `ai-orchestrator/deploy/release-public.sh`: exports `main` to a separate public tree as a single release commit and tag; checks forbidden paths, a local (untracked) personal-pattern denylist and secret patterns, rolls back on failure, never pushes.

### Changed
- Accessibility: WCAG AA contrast for secondary text, status colors and dark-theme buttons; visible keyboard focus in the work menu and composer; screen-reader labels, roles and live regions (error banner, filters, per-work menu buttons, connection and progress status).

### Fixed
- A session waiting for quota without an automatic retry plan can now be stopped (it previously could not be cancelled or continued).

## [0.1.0] - 2026-10-21

### Added
- First public release. Local orchestrator that runs AI coding CLIs (Claude Code, Antigravity `agy`, Codex, Ollama) as a supervised author → reviewer team with human approval.
- SQLite state machine with append-only history, versioned migrations and crash recovery.
- React panel (Mac and phone layouts): works as conversations, provider/model/effort per message, quota and context display, first-run setup guide, Settings → Accounts, work recipes (`/` menu).
- Isolated git worktree per work with "bring to source folder" and review-before-merge; pre-review tests in a sandbox; preview server.
- Phone access through a Tailscale gateway with pairing codes and per-device scopes; Web Push notifications.
- Safety: loopback-only server, cross-site guard on every mutating endpoint, prompts never passed through a shell, read-only reviewers, git hook/config change reporting.
- Turkish and English user guides, MIT license.

[Unreleased]: https://github.com/avcibunyamin/PromptBridge/compare/v0.3.5...HEAD
[0.3.5]: https://github.com/avcibunyamin/PromptBridge/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/avcibunyamin/PromptBridge/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/avcibunyamin/PromptBridge/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/avcibunyamin/PromptBridge/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/avcibunyamin/PromptBridge/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/avcibunyamin/PromptBridge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/avcibunyamin/PromptBridge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/avcibunyamin/PromptBridge/releases/tag/v0.1.0
