# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
