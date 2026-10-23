# PromptBridge (Conductor) — User Guide

> **Türkçe sürüm:** [docs/KULLANIM-KILAVUZU.md](KULLANIM-KILAVUZU.md)

**PromptBridge (Conductor)** is a human-in-the-loop multi-AI orchestration system that runs AI coding CLI tools (**Claude Code**, **Antigravity `agy` / Gemini**, **Codex**, **Ollama**) in a supervised **author → reviewer** team workflow using your existing subscriptions—without API keys—entirely on your local machine (`127.0.0.1`).

---

## Table of Contents

1. [Installation (Python venv, npm build, launchd)](#1-installation-python-venv-npm-build-launchd)
2. [Installing Provider CLIs and Linking Accounts](#2-installing-provider-clis-and-linking-accounts)
3. [`providers.yaml` Ceiling and Default Team](#3-providersyaml-ceiling-and-default-team)
4. [Your First Work (Folder Selection, Isolated Copy, Team, Acceptance)](#4-your-first-work-folder-selection-isolated-copy-team-acceptance)
5. [Message Composer (Model / Effort / Ring, `/` Shortcuts)](#5-message-composer-model--effort--ring--shortcuts)
6. [Tests, Lint, and Preview](#6-tests-lint-and-preview)
7. [GitHub Integration](#7-github-integration)
8. [Phone Pairing (Tailscale)](#8-phone-pairing-tailscale)
9. [Mac App (`apps/mac`, `make install`)](#9-mac-app-appsmac-make-install)
10. [Security Model](#10-security-model)
11. [Troubleshooting](#11-troubleshooting)
12. [Frequently Asked Questions (FAQ)](#12-frequently-asked-questions-faq)

---

## 1. Installation (Python venv, npm build, launchd)

### Prerequisites

- **Python 3.12+**
- **Node.js 18+** and **npm**
- At least one supported provider CLI tool (`claude`, `agy`, `codex`, or `ollama`)

### Step 1: Python Virtual Environment (`venv`) and Dependencies

After cloning the repository, create a Python 3.12 virtual environment inside `ai-orchestrator/`:

```sh
cd ~/Projects/PromptBridge/ai-orchestrator
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### Step 2: Build the Web Panel (`npm run build`) (optional)

A built panel ships in `ai-orchestrator/app/static/`; rebuild it only after changing the UI. Build the React + TypeScript + Vite web panel. The build output is written directly to `ai-orchestrator/app/static/`, which is served by FastAPI:

```sh
cd ui
npm install
npm run build
cd ..
```

### Step 3: Create the Provider Policy File

Copy the example policy template to `.system/providers.yaml`:

```sh
mkdir -p .system
cp providers.example.yaml .system/providers.yaml
```

### Check the setup

Before starting the server, run:

```sh
.venv/bin/python -m app doctor
```

It checks Python, `.system/providers.yaml`, the provider CLIs, the built panel, Node and the local server. It never calls a provider. The exit code is 1 if something must be fixed. Sign-in is still checked in the panel (**Settings → Accounts → Check session status**).

### Step 4: Start the Server and Install the `launchd` Agent (macOS)

To run the server in the foreground for a single session:

```sh
.venv/bin/python -m app serve
```

On macOS, install the `launchd` user agent (`com.promptbridge.conductor`) so the server starts automatically on login and restarts if it exits (`KeepAlive`):

```sh
deploy/launchd.sh install      # Installs/updates the agent, captures current PATH, and waits for /health
deploy/launchd.sh status       # Shows launchd state (PID, last exit code) and /health output
deploy/launchd.sh restart      # Restarts the service
deploy/launchd.sh uninstall    # Stops the service and removes the plist file
```

> **Note:** While the `launchd` agent is installed, running `kill` will not permanently stop the server—`launchd` will restart it within 30 seconds. Always use `deploy/launchd.sh uninstall` to stop the server. Logs are written to `~/Library/Logs/Conductor/server-launchd.log`.

Once running, open the management panel at: **<http://127.0.0.1:8765/ui/>**

### Interface language

The panel is available in Turkish and English. Choose the language under **Settings → Appearance**; the choice is saved on each device separately. Without a saved choice, the panel follows the browser language: Turkish if the browser language is `tr`, English otherwise. The page reloads when you change the language.

---

## 2. Installing Provider CLIs and Linking Accounts

PromptBridge does not use API keys; it runs each provider via its official CLI tool and your logged-in user session. To avoid interfering with your personal settings, plugins, or system keychain, `agy` and `codex` run inside isolated `HOME` directories under `.system/`.

### 2.1 Antigravity CLI (`agy` / Gemini and Claude Models)

1. **Installation:** Install via the official installer script (installs to `~/.local/bin/agy`; set `CONDUCTOR_AGY_BIN` if installed elsewhere):
   ```sh
   curl -fsSL https://antigravity.google/cli/install.sh | bash
   ```
2. **Isolated `HOME` and Keychain Preparation:** Each `agy` account runs in its own `.system/agy-home-<account>` directory (your real `~` directory is never touched). First, add `agy@<account>/<model>` to `.system/providers.yaml` and click **Check session status** in the panel (or start a first `agy` step). This creates an isolated keychain with an empty password under `.system/agy-home-<account>/Library/Keychains/`.
3. **Account Login Command:** From inside `ai-orchestrator/`, run `agy` with the account's isolated `HOME`:
   ```sh
   (H="$PWD/.system/agy-home-<account>"; cd /tmp && HOME="$H" ~/.local/bin/agy)
   ```
   - In the browser OAuth prompt, sign in with the Google account for `<account>`.
   - Paste the authorization code back into the terminal.
   - Answer the telemetry prompt, and when asked about folder trust, select **"No, exit"**.
4. **Important Rules:**
   - Do not delete `.system/agy-home-<account>` (deleting it removes the isolated keychain).
   - Specifying a model is **mandatory** for `agy` identities (e.g., `agy@<account>/gemini-3.8-flash-high` or `agy@<account>/gemini-4-argon-high`).
   - When available models change, click **Advanced → Refresh** in the panel to run `agy models` for each configured account without consuming model quota.

### 2.2 Codex CLI (`codex`)

1. **Installation:** Use either the standalone `codex` CLI or the binary bundled inside the macOS ChatGPT app (`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`). Set `CONDUCTOR_CODEX_BIN` to use a custom binary path.
2. **Isolated Home Login:** PromptBridge uses `.system/codex-home` as `CODEX_HOME` so your personal `~/.codex` plugins, MCP servers, and settings are never loaded. Sign in once to this isolated home:
   ```sh
   cd ~/Projects/PromptBridge/ai-orchestrator
   CODEX_HOME="$PWD/.system/codex-home" codex login
   # Or if using the ChatGPT.app binary:
   CODEX_HOME="$PWD/.system/codex-home" /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex login
   ```
3. **Multiple Accounts (`codex@<account>`):** If you use `codex@<account>` in your policy, that identity uses `.system/codex-home-<account>`:
   ```sh
   CODEX_HOME="$PWD/.system/codex-home-<account>" codex login
   ```
   > **Warning:** Never copy or symlink `auth.json` across directories; sharing a refresh token between two homes can invalidate sessions.

### 2.3 Claude Code (`claude`)

1. **Installation and Login:** Install the `claude` CLI and sign in from your terminal by running `claude`. Set `CONDUCTOR_CLAUDE_BIN` if the binary is in a non-standard location.
2. **Execution Behavior:** PromptBridge launches Claude with `--safe-mode` so personal hooks and skills do not interfere. The root `CLAUDE.md` file in the workspace is validated and pinned at the start of a session.

### 2.4 Ollama (Local Models)

1. **Installation and Pulling Models:** Install the Ollama app and pull a model that supports **tool calling**:
   ```sh
   ollama pull qwen2.5-coder:14b
   ```
2. **Context Length:** Start Ollama with `OLLAMA_CONTEXT_LENGTH=16384` (or higher). The panel warns if a model reports a small context window.
3. **Connection:** PromptBridge connects exclusively to `127.0.0.1:11434` and runs Ollama models through an isolated `.system/ollama-codex-home` environment. No account login or quota applies.

---

## 3. `providers.yaml` Ceiling and Default Team

`ai-orchestrator/.system/providers.yaml` defines the **policy ceiling**. Neither the web browser nor a paired phone can modify this file; the UI can only pick from the identities permitted here.

Example `.system/providers.yaml`:

```yaml
# Default team (pre-selected author and reviewer chains in the panel)
roles:
  author:
    - provider: claude
      profile: WRITE
    - provider: agy
      account: <account1>
      model: gemini-4-argon-high
      profile: WRITE
    - provider: codex
      profile: WRITE
  reviewer:
    - provider: agy
      account: <account2>
      model: gemini-3.8-flash-high
      profile: READ_ONLY
    - provider: claude
      profile: READ_ONLY

require_handover_approval: false

# Ceiling of identities selectable from the panel per role.
# Entries without a model/effort (e.g. claude, agy@<account1>, ollama)
# act as wildcards allowing all models and efforts for that provider+account.
allowed:
  author:
    - claude
    - agy@<account1>
    - agy@<account2>
    - codex
    - codex@<account>
    - ollama
  reviewer:
    - agy@<account2>
    - claude
    - codex
    - ollama

# Total extra context continuation sessions per step (0 disables)
max_context_continuations: 3

# Optional: Author ↔ Reviewer turn loop (runs until reviewer passes or budget ends)
loop:
  max_turns: 3            # 1..5 turns
  max_active_minutes: 30  # 5..120 minutes (active execution time only)
```

### Core Rules

- **Role Profiles:** `author` links always use `WRITE`; `reviewer` links always use `READ_ONLY`.
- **Chain & Quota Fallback:** Up to **4 links** can be configured per role. When the primary link hits a verified rate limit or quota error (`QUOTA_EXCEEDED`), execution automatically falls back to the next link in the chain.
- **Saving a Default Team in the Panel:** Under **Settings** in the left sidebar, you can save your preferred default team to `.system/settings.json` (within the `allowed` ceiling). Clicking **"Save as default and accept"** pre-accepts that team so you are not prompted for confirmation on every message.

---

## 4. Your First Work (Folder Selection, Isolated Copy, Team, Acceptance)

1. **Create a New Work:** Click **New work** in the left sidebar.
2. **Select a Folder:** Enter the full path to an existing project directory (e.g., `~/Projects/sample-app`), use **Choose folder**, or clone via **From GitHub**.
3. **Work in an Isolated Copy (Optional — Git Worktree):**
   - If the project is at the root of a Git repository with at least one commit, you can enable **Work in an isolated copy**.
   - PromptBridge creates branch `conductor/<date>-<title>-<id>` and checks out a separate working tree under `~/.conductor/worktrees/<project>-<id>`.
   - Agents never touch your main working folder, and you can run up to 10 isolated works in parallel on the same repository.
   - Between runs, click **Bring to main folder** in the work header to inspect changed files and commits and merge them (`merge --no-ff`) into your main checkout, or remove the worktree after archiving the work.
4. **Team Selection and Acceptance:**
   - If you use your saved default team, no extra confirmation checkbox is needed.
   - If you change the model/team or if `providers.yaml` changed, check the confirmation box (**"Will start with this team … I accept"**).
   - Optionally enable **"Wait and automatically retry if quota is reached"** (deadline 1–24 hours, max attempts 1–5) under **Options**.
5. **Dispatch and Execution Flow:** Enter your task description and click **Start**:
   - **Step 1 (Author):** The author model implements the requested changes.
   - **Step 2 (Reviewer):** Approved tests and linters run first; then the read-only reviewer inspects the list of changed files and test/lint outputs.
   - **Step 3 (Author Fix / Loop):** Reviewer findings are handed back to the author to apply fixes. If `loop` is enabled, author ↔ reviewer turns continue until the reviewer marks the work `verified` or the turn/time budget expires.

---

## 5. Message Composer (Model / Effort / Ring, `/` Shortcuts)

The sticky message composer at the bottom of the screen lets you control your team and monitor session health:

- **Model & Account Selector:** Switch the primary author link (`provider@<account>` → `model`) directly in the composer. The active reviewer identity is displayed alongside it.
- **Reasoning Effort:** Pick the reasoning effort on supported tools (e.g., `low`, `medium`, `high`, `xhigh`, `max` for Claude, such as `claude/opus~high`, or provider defaults for Codex). For `agy`, effort is part of the model name itself (e.g., `gemini-3.8-flash-high`).
- **Quota & Context Ring:** Located in the message composer beside the model and effort selectors (`UsageRing`), its **fill** shows the active work's context token usage percentage, while its **color** indicates the remaining quota percentage. Clicking the ring opens the **Context and quota** dialog displaying used/total tokens, provider quota windows, and a **Refresh** button (which reads `agy /usage` and Codex quota endpoints without invoking a model).
- **Advanced Team Editor:** Clicking **Advanced** opens the full chain editor for both author and reviewer (links 1..4), status badges, **Check session status**, and the **Refresh** button to update the model catalog.
- **`/` Shortcuts (Slash Commands):** Typing `/` at the beginning of the message composer opens the shortcut menu (`↑`/`↓` to select, `Enter` or `Tab` to run, `Esc` to close). Slash commands never send a message or start a run on their own—they open selectors and panels on the current page:
  - **Common:** `/model` (select author model), `/efor` (select author effort), `/kota` (open Context and quota dialog), `/takim` (open Advanced fallback/reviewer editor).
  - **In a new work (Mac):** `/klasor` (choose project folder from Finder), `/github` (clone from GitHub).
  - **In an existing work:** `/onizleme` (project preview, Mac), `/testler` (pre-review tests and lint, Mac), `/github` (commit, push, draft PR — between runs, Mac), `/yeni` (open new work screen).

---

## 6. Tests, Lint, and Preview

### 6.1 Pre-Review Tests and Lint / Typecheck

Because the reviewer profile is strictly read-only and cannot run shell commands, PromptBridge runs your approved tests and linters itself and passes the results to the reviewer as structured evidence:

1. Click **Tests** in the work header.
2. PromptBridge detects and suggests commands from your project files:
   - **Tests:** `npm test` (from `package.json`), `pytest` (if `.venv` exists), or `python -m unittest discover`.
   - **Lint / Typecheck:** `npm run lint`, `npm run typecheck`, `tsc --noEmit`, or `ruff` / `flake8` / `mypy` inside `.venv`.
3. Once you click **Approve this command**, it runs automatically before every reviewer step inside a **restricted sandbox**:
   - Network access is completely disabled.
   - Writes are restricted to the project folder and a dedicated temp directory; your home directory (`~`) and secret files are inaccessible.
   - File size is capped at 200 MB and runtime at 5 minutes.
4. If configuration files (`package.json`, `pyproject.toml`, `requirements.txt`, etc.) change, approval is automatically invalidated until you review and re-approve.

### 6.2 Application Preview

1. Click **Preview** in the work header.
2. Review the suggested dev server command (or static site server), click **Approve**, and then **Start**.
3. The preview server runs in its own process group bound only to `127.0.0.1` with a sanitized environment.
4. **Sandbox Mode (`sandbox-exec`):** You can enable experimental sandboxing under **Settings → Preview**. When enabled, the preview server cannot read `.ssh`, `.netrc`, `.npmrc`, `~/.config/gh`, Keychain, or `.system`, and cannot make outbound network connections. *(Note: Frameworks like Next.js Turbopack that connect to child processes over `localhost`, or apps requiring external databases, cannot run in this sandbox; disable sandbox mode for those projects and only approve commands you trust.)*

---

## 7. GitHub Integration

PromptBridge uses your Mac's existing `gh` (GitHub CLI) session—it never reads or stores your GitHub token in its database.

### Setup

```sh
brew install gh
gh auth login
```

Verify your account appears under **Settings → GitHub**, and select a **GitHub clone folder** under your home directory (e.g., `~/Projects`).

### Starting a Work from a Repository

- In **New work**, click **From GitHub** next to the folder field and select a repository. It is cloned to `<clone-folder>/<repo-name>`.

### Publishing Changes (Commit → Push → Draft PR)

The **GitHub** button in the work header provides three explicit, human-confirmed steps:

1. **Commit:** Lists modified files. Potential secrets (`.env*`, `*.pem`, `*.key`, `id_*`, `credentials*`) are unchecked by default and require an extra confirmation if selected. If you are on `main`/`master`, PromptBridge automatically creates branch `conductor/<date>-<title>-<id>` so it never commits directly to the default branch.
2. **Push:** Displays the target repository, branch, commits, and file count. Changes under `.github/workflows/` require a dedicated confirmation checkbox. Force push is never used.
3. **Draft PR:** Edit the PR title and body and submit. Pull requests are always opened as **Draft PRs**.

> **Security Boundary:** Agent subprocesses and preview servers have credential variables stripped so they cannot run `git push` or `gh` under your identity. Furthermore, if a repository's `.git/config` contains dangerous keys (`core.hooksPath`, `credential.helper`, `filter`, `include`) or if an agent modifies `.git/hooks/*`, PromptBridge refuses to run hooks and warns you on the execution card.

---

## 8. Phone Pairing (Tailscale)

To monitor works, approve file transfers, and optionally stop runs or dispatch new tasks from your phone, PromptBridge pairs a dedicated local **Mobile Gateway** (`127.0.0.1:8770`) with **Tailscale**.

> **Important:** Never enable `tailscale funnel` (which exposes ports to the public internet). Use only `tailscale serve` within your private Tailnet.

### Step 1: One-Time Tailscale and Gateway Setup

1. Install Tailscale on both your Mac and your phone (iOS / Android) using the **same account**. Enable **MagicDNS** and **HTTPS Certificates** in the Tailscale admin console.
2. Find your Mac's Tailscale hostname:
   ```sh
   tailscale status
   # Example: my-mac.tail12345.ts.net
   ```
3. In the Mac panel, open **Settings → Mobile access**:
   - Enter your Tailscale hostname (`my-mac.tail12345.ts.net`).
   - Check **Enable mobile gateway** and click **Save**.
   - Restart the server: `deploy/launchd.sh restart` (starts the gateway on `127.0.0.1:8770`).
4. On your Mac, expose the gateway over Tailscale HTTPS:
   ```sh
   tailscale serve --bg --https=443 http://127.0.0.1:8770
   ```

### Step 2: Pair Your Phone

1. On your Mac, go to **Settings → Mobile access → Generate pairing code**. A single-use **6-digit code** valid for 5 minutes is generated (invalidated after 5 failed attempts).
2. With Tailscale connected on your phone, open `https://<your-mac>.tail12345.ts.net/ui/`, enter the 6-digit code, and tap **Pair**.
3. **Install as App (PWA):** On iPhone Safari tap *Share → Add to Home Screen*; on Android Chrome tap *Menu → Install app*.

### Step 3: Device Permissions and Revocation

Manage paired phones under **Settings → Mobile access → Devices** on your Mac:

- **Default (New Device):** View-only access to works plus file approval/rejection.
- **Can stop:** Allows the phone to stop a running execution, cancel a quota wait plan, or remove a queued message.
- **Can dispatch tasks:** Allows the phone to start a new work in an already-registered active project folder, send messages, pick models/efforts within the `providers.yaml` ceiling, and change the default team or theme. A macOS notification appears whenever a phone starts a task.
- **Revoke:** Immediately invalidates the device token and terminates active connections.

---

## 9. Mac App (`apps/mac`, `make install`)

If you prefer a dedicated macOS desktop window over a browser tab, build and install the native SwiftUI + WKWebView shell in `apps/mac`:

### Requirements

- macOS with Xcode Command Line Tools (`xcode-select --install`)
- PromptBridge server running on `127.0.0.1:8765`

### Build and Install

```sh
cd ~/Projects/PromptBridge/apps/mac
make              # Compiles build/PromptBridge.app
make install      # Installs to /Applications/PromptBridge.app
make run          # Builds and launches the app immediately
```

After installation, you can launch `/Applications/PromptBridge.app` from Spotlight or Finder.

---

## 10. Security Model

PromptBridge enforces strict local security boundaries when orchestrating AI coding agents:

1. **Loopback Only (`127.0.0.1`):** Both the main server (`8765`) and the mobile gateway (`8770`) bind strictly to `127.0.0.1`.
2. **`mutation_guard` (CSRF & Origin Enforcement):** Every state-mutating endpoint (`POST`, `PUT`, `DELETE`) requires `mutation_guard` verification. Cross-origin requests from external websites are rejected with `403 Forbidden`.
3. **No Shell Execution:** Prompts, file paths, and Git arguments are never interpolated into a shell (`sh -c`); subprocesses are spawned with explicit argument arrays (`execve`).
4. **Read-Only Reviewer (`READ_ONLY`):** Reviewer sessions run with write and shell tools disabled (e.g., Claude allows only `Read/Glob/Grep` with `dontAsk`; Codex runs in `read-only` sandbox; `agy` runs in read-only mode with command tools disabled).
5. **Isolated Provider Homes & `untrusted` Workspace Pinning:**
   - Codex runs in `.system/codex-home*` and pins target workspaces as `untrusted` before launch so repo-local `.codex/` configs or MCP servers cannot auto-execute.
   - `agy` runs per account in `.system/agy-home-<account>` with an isolated keychain and refuses to start if workspace `.agents/` or `.agent/` MCP/hook configs exist (`agy_workspace_config`).
   - Claude runs in `--safe-mode`, and project instructions (`CLAUDE.md`, `AGENTS.md`) are validated against symlinks, path traversal, and a 16 KiB limit before being pinned for the session.
6. **Human Approvals & Append-Only Audit Log:** Policy changes, test/lint/preview commands, GitHub operations, and process recoveries (`RECOVERY_REQUIRED`) require explicit human confirmation. All state transitions are logged in SQLite tables protected by append-only triggers.

---

## 11. Troubleshooting

| Symptom / Error | Cause & Solution |
|---|---|
| **Run `python -m app doctor` first** | In `ai-orchestrator/`, run `.venv/bin/python -m app doctor`. It checks Python, `.system/providers.yaml`, the provider CLIs, the built panel, Node and the local server without calling a provider; the exit code is 1 if something must be fixed. Sign-in is checked in the panel (**Settings → Accounts → Check session status**). |
| **Panel does not open (`127.0.0.1:8765` unreachable)** | Server is not running. Check `deploy/launchd.sh status` or run `.venv/bin/python -m app serve`. |
| **`install`: "port 8765 is in use by another process"** | A manually started `serve` process holds the port. Find its PID via `lsof -nP -iTCP:8765 -sTCP:LISTEN`, stop it with `kill -TERM <PID>`, then run `deploy/launchd.sh install`. |
| **Server restarts after `kill`** | The `launchd` agent uses `KeepAlive`. Stop it permanently with `deploy/launchd.sh uninstall`. |
| **Codex shows "not logged in" / `WAITING_AUTH`** | Sign in to PromptBridge's isolated Codex home: `CODEX_HOME="$PWD/.system/codex-home" codex login`. |
| **`agy` model shows "Missing model" or `model_mismatch`** | The provider updated its model catalog. Click **Advanced → Refresh** in the panel and update the model name in `.system/providers.yaml`. |
| **`409 conflict` when sending a message** | `.system/providers.yaml` changed since the panel loaded. Preview and accept the updated team. |
| **Retry returns `409 workspace_busy` or `recovery_required`** | The workspace lock is held by another session or an unclean process shutdown. Open the execution card and click **Recover** to terminate leftover processes and release the lock. |
| **Preview fails in sandbox mode (`EPERM` / `Operation not permitted`)** | The dev server attempts outbound connections or `localhost` IPC (e.g., Next.js Turbopack). Disable sandbox mode under **Settings → Preview**. |
| **Phone cannot connect or gets `421 Misdirected Request`** | Ensure Tailscale is active on the phone, `tailscale serve` is running on the Mac, and the hostname in **Settings → Mobile access** matches the URL exactly. |
| **Warning: "This run touched the repository's git hooks/config"** | The author agent modified `.git/hooks/*` or `.git/config`. Inspect `.git/hooks` and `git config --local --list` before running git commands in your own terminal. |

---

## 12. Frequently Asked Questions (FAQ)

### Do I need OpenAI, Anthropic, or Google API keys to use PromptBridge?
No. PromptBridge runs your installed CLI tools (`claude`, `agy`, `codex`) using your existing subscription logins, or local `ollama` models.

### Can the author and reviewer be the same model?
While `providers.yaml` allows it technically, configuring different providers or models for the author and reviewer (for example, `claude` as author and `agy@<account>/gemini-3.8-flash-high` or `codex` as reviewer) is strongly recommended for independent verification.

### What happens if a provider runs out of quota mid-task?
If a provider returns a verified rate-limit/quota error (`QUOTA_EXCEEDED`), PromptBridge automatically hands the step over to the next fallback link in the chain. If all links are exhausted, the session enters `WAITING_CAPACITY` and either follows your configured automatic retry wait plan (exponential backoff starting at 15 minutes) or waits until you click **Retry**.

### When should I use "Work in an isolated copy" (worktree)?
Use an isolated copy whenever you are actively editing files in your main project checkout or want to run multiple tasks in parallel on the same repository without workspace lock conflicts.

### What happens if the server crashes or my Mac restarts during a run?
All state is persisted in SQLite (WAL mode). On startup, interrupted runs are never blindly restarted; they transition to `RECOVERY_REQUIRED` while keeping the workspace lock. You can inspect and terminate any leftover process groups via the **Recover** button in the panel before continuing.
