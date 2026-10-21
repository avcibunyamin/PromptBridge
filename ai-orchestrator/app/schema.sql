-- Şema sürümü (idempotent migration için)
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);  -- ör. ('schema_version','1.1')

-- versions: DEĞİŞMEZ içerik sürümleri (K-003). INSERT-only; content ASLA UPDATE edilmez.
CREATE TABLE IF NOT EXISTS versions (
  version_id  TEXT PRIMARY KEY,          -- captured: ver_<msg_id>_0; edited: UUID
  envelope_id TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('captured','edited')),
  content     BLOB NOT NULL,             -- donmuş tam içerik (bayt)
  sha256      TEXT NOT NULL,             -- content baytlarının hash'i
  bytes       INTEGER NOT NULL,
  created_at  TEXT NOT NULL,             -- ISO8601
  created_by  TEXT,                      -- 'system'(capture) | 'human'(edit)
  FOREIGN KEY (envelope_id) REFERENCES envelopes(id)
);

-- envelopes: her mesaj için bir satır (meta + güncel durum + işaretçiler)
CREATE TABLE IF NOT EXISTS envelopes (
  id                   TEXT PRIMARY KEY,   -- msg_2026-...-a1b2
  schema_version       TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  source_agent         TEXT NOT NULL,      -- 'chatgpt'
  target_agent         TEXT NOT NULL,      -- 'claude' (Faz 1 tek hedef)
  type                 TEXT,               -- spec|prose|task|code
  title                TEXT,
  origin_ref           TEXT,               -- kaynak outbox yolu: SADECE köken, otorite DEĞİL
  bytes                INTEGER,
  -- Sürüm işaretçileri (K-003) — mantıksal referans (uygulama + test ile güvence):
  captured_version_id  TEXT NOT NULL,      -- ilk dondurulan sürüm
  approved_version_id  TEXT,               -- ONAYLANAN sürüm (captured VEYA edited); onaya dek NULL
  -- Teslim kaydı (K-004) — teslimden önce NULL:
  delivered_version_id TEXT,
  delivered_path       TEXT,
  delivered_sha256     TEXT,
  delivered_at         TEXT,
  -- Rota + durum:
  mode                 TEXT NOT NULL,      -- manual|auto (Faz 1: manual, K-002)
  trigger              TEXT NOT NULL,      -- human|agent-poll|ui-nudge (Faz 1: human)
  state                TEXT NOT NULL,      -- DETECTED|CAPTURED|PENDING_APPROVAL|APPROVED|DELIVERING|DELIVERED|REJECTED|...
  approved_by          TEXT,
  note                 TEXT
);

-- history: APPEND-ONLY denetim; her durum geçişi + önemli olay
CREATE TABLE IF NOT EXISTS history (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  envelope_id TEXT NOT NULL,
  at          TEXT NOT NULL,
  from_state  TEXT,                        -- geçişten önceki durum (guard/izlenebilirlik)
  to_state    TEXT NOT NULL,
  event       TEXT,                        -- capture|edited|human_approve|human_reject|deliver_intent|deliver_ok|...
  actor       TEXT,                        -- system|human
  detail      TEXT,                        -- JSON: {version_id, sha256, path, base_version_id, diff_ref...}
  FOREIGN KEY (envelope_id) REFERENCES envelopes(id)
);

CREATE INDEX IF NOT EXISTS idx_env_state ON envelopes(state);
CREATE INDEX IF NOT EXISTS idx_ver_env   ON versions(envelope_id);
CREATE INDEX IF NOT EXISTS idx_hist_env  ON history(envelope_id, seq);

-- F2-02 Session and Runner Tables

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL, 
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_steps (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  step_index INTEGER NOT NULL,
  agent_name TEXT NOT NULL,
  task_prompt TEXT NOT NULL,
  status TEXT NOT NULL,
  provider_session_id TEXT,
  started_at TEXT,
  completed_at TEXT,
  result_text TEXT,
  usage_info TEXT,
  stop_reason TEXT,
  is_error INTEGER DEFAULT 0,
  terminal_reason TEXT,
  retry_generation INTEGER NOT NULL DEFAULT 0 CHECK(retry_generation >= 0),
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS workspace_locks (
  workspace_path TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  locked_at TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Durable Runner event/checkpoint journal; never used for envelope states.
CREATE TABLE IF NOT EXISTS session_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  step_id TEXT REFERENCES session_steps(id),
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events ON session_events(session_id, seq);

-- F2-03a additions; old sessions/envelopes remain readable.
CREATE TABLE IF NOT EXISTS session_policies (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  policy_json TEXT NOT NULL,
  policy_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_roles (
  step_id TEXT PRIMARY KEY REFERENCES session_steps(id),
  role TEXT NOT NULL CHECK(role IN ('author','reviewer')),
  profile TEXT NOT NULL CHECK(profile IN ('WRITE','READ_ONLY'))
);
CREATE TABLE IF NOT EXISTS session_attempts (
  id TEXT PRIMARY KEY,
  step_id TEXT NOT NULL REFERENCES session_steps(id),
  attempt_no INTEGER NOT NULL,
  provider TEXT NOT NULL,
  profile TEXT NOT NULL,
  provider_session_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  error_code TEXT,
  terminal_reason TEXT,
  handover_reason TEXT,
  result_text TEXT,
  exit_code INTEGER,
  provider_error TEXT,
  retry_generation INTEGER NOT NULL DEFAULT 0 CHECK(retry_generation >= 0),
  continuation_no INTEGER NOT NULL DEFAULT 0 CHECK(continuation_no >= 0),
  continuation_of TEXT REFERENCES session_attempts(id),
  CHECK((continuation_no=0 AND continuation_of IS NULL) OR
        (continuation_no>0 AND continuation_of IS NOT NULL)),
  UNIQUE(step_id,attempt_no),
  UNIQUE(step_id,provider,retry_generation,continuation_no),
  UNIQUE(continuation_of)
);
CREATE TABLE IF NOT EXISTS workspace_snapshots (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  attempt_id TEXT REFERENCES session_attempts(id),
  kind TEXT NOT NULL,
  at TEXT NOT NULL,
  snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS process_owners (
  attempt_id TEXT PRIMARY KEY REFERENCES session_attempts(id),
  pid INTEGER NOT NULL,
  pgid INTEGER NOT NULL,
  start_time REAL NOT NULL,
  boot_time REAL NOT NULL,
  status TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  stopped_at TEXT
);
CREATE TRIGGER IF NOT EXISTS frozen_policy_no_update BEFORE UPDATE ON session_policies
BEGIN SELECT RAISE(ABORT,'session policy is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_policy_no_delete BEFORE DELETE ON session_policies
BEGIN SELECT RAISE(ABORT,'session policy is immutable'); END;

-- F3-01a (schema 1.3): persistent works. A session stays one execution; works only link to it.
CREATE TABLE IF NOT EXISTS works (
  id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  title TEXT NOT NULL,
  create_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT,
  deleted_at TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  continued_session_id TEXT REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_works_list ON works(created_at, id);
CREATE TABLE IF NOT EXISTS work_messages (
  id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(id),
  client_message_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK(seq >= 1),
  content TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  policy_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','LINKED','CANCELLED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(work_id, client_message_id),
  UNIQUE(work_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_work_messages_pending ON work_messages(status, created_at);
CREATE TABLE IF NOT EXISTS work_runs (
  message_id TEXT PRIMARY KEY REFERENCES work_messages(id),
  session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
  work_id TEXT NOT NULL REFERENCES works(id),
  last_status TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_events (
  work_id TEXT NOT NULL REFERENCES works(id),
  seq INTEGER NOT NULL CHECK(seq >= 1),
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(work_id, seq)
);
CREATE TABLE IF NOT EXISTS work_resume_refs (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  decision TEXT NOT NULL CHECK(decision IN ('RESUME','FRESH')),
  reason TEXT NOT NULL,
  source_session_id TEXT REFERENCES sessions(id),
  source_attempt_id TEXT REFERENCES session_attempts(id),
  provider_session_id TEXT,
  baseline_snapshot_id TEXT REFERENCES workspace_snapshots(id),
  current_snapshot_id TEXT REFERENCES workspace_snapshots(id),
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS work_events_no_update BEFORE UPDATE ON work_events
BEGIN SELECT RAISE(ABORT,'work events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS work_events_no_delete BEFORE DELETE ON work_events
BEGIN SELECT RAISE(ABORT,'work events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS work_messages_content_frozen BEFORE UPDATE OF
  work_id, client_message_id, seq, content, payload_hash, policy_json, policy_digest ON work_messages
BEGIN SELECT RAISE(ABORT,'work message payload is immutable'); END;
CREATE TRIGGER IF NOT EXISTS work_resume_refs_frozen BEFORE UPDATE ON work_resume_refs
BEGIN SELECT RAISE(ABORT,'resume decision is immutable'); END;

-- F3-02 (schema 1.5): bounded author → reviewer turn loop. Additive; legacy sessions have no loop row.
CREATE TABLE IF NOT EXISTS session_loop_state (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  execution_kind TEXT NOT NULL CHECK(execution_kind IN ('loop')),
  max_turns INTEGER NOT NULL CHECK(max_turns BETWEEN 1 AND 5),
  max_active_ms INTEGER NOT NULL CHECK(max_active_ms > 0),
  current_turn INTEGER NOT NULL DEFAULT 1 CHECK(current_turn >= 1),
  phase TEXT NOT NULL CHECK(phase IN ('author','review','done')),
  pause_requested_at TEXT,
  pause_revision INTEGER NOT NULL DEFAULT 0 CHECK(pause_revision >= 0),
  pause_snapshot_id TEXT REFERENCES workspace_snapshots(id),
  resume_digest TEXT,
  resume_fresh INTEGER NOT NULL DEFAULT 0 CHECK(resume_fresh IN (0,1)),
  outcome TEXT CHECK(outcome IS NULL OR outcome IN
    ('verified','reviewer_blocked','review_invalid','no_progress','turn_limit','budget_exhausted')),
  outcome_reason TEXT,
  last_author_unreviewed INTEGER NOT NULL DEFAULT 0 CHECK(last_author_unreviewed IN (0,1)),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_turns (
  session_id TEXT NOT NULL REFERENCES sessions(id),
  turn_no INTEGER NOT NULL CHECK(turn_no >= 1),
  author_step_id TEXT NOT NULL UNIQUE REFERENCES session_steps(id),
  reviewer_step_id TEXT NOT NULL UNIQUE REFERENCES session_steps(id),
  verdict TEXT CHECK(verdict IS NULL OR verdict IN ('pass','changes_requested','blocked','invalid')),
  findings_json TEXT,
  findings_hash TEXT,
  invalid_code TEXT,
  snapshot_before_id TEXT REFERENCES workspace_snapshots(id),
  snapshot_after_id TEXT REFERENCES workspace_snapshots(id),
  ended_reason TEXT,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  PRIMARY KEY(session_id, turn_no)
);
CREATE TABLE IF NOT EXISTS session_time_segments (
  segment_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  step_id TEXT NOT NULL REFERENCES session_steps(id),
  opened_at TEXT NOT NULL,
  elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK(elapsed_ms >= 0),
  settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
  estimated INTEGER NOT NULL DEFAULT 0 CHECK(estimated IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_time_segments_open ON session_time_segments(settled, session_id);

-- TB-03 (schema 1.6): immutable workspace CLAUDE.md snapshot for work sessions.
CREATE TABLE IF NOT EXISTS session_project_instructions (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  status TEXT NOT NULL CHECK(status IN ('absent','included','rejected')),
  reason TEXT,
  path TEXT,
  source_path TEXT,
  line INTEGER,
  combined_digest TEXT,
  section TEXT,
  manifest_json TEXT NOT NULL,
  CHECK((status='included' AND combined_digest IS NOT NULL AND section IS NOT NULL AND reason IS NULL) OR
        (status='absent' AND combined_digest IS NULL AND section IS NULL AND reason IS NULL) OR
        (status='rejected' AND combined_digest IS NULL AND section IS NULL AND reason IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS project_instructions_no_update BEFORE UPDATE ON session_project_instructions
BEGIN SELECT RAISE(ABORT,'project instructions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS project_instructions_no_delete BEFORE DELETE ON session_project_instructions
BEGIN SELECT RAISE(ABORT,'project instructions are immutable'); END;

-- TB-05 (schema 1.8, K-014): immutable workspace AGENTS.md snapshot for work sessions whose policy
-- runs Codex. A row also marks that the session's Codex ran under Conductor's own CODEX_HOME.
CREATE TABLE IF NOT EXISTS session_agents_instructions (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  status TEXT NOT NULL CHECK(status IN ('absent','included','rejected')),
  reason TEXT,
  path TEXT,
  source_path TEXT,
  line INTEGER,
  combined_digest TEXT,
  section TEXT,
  manifest_json TEXT NOT NULL,
  CHECK((status='included' AND combined_digest IS NOT NULL AND section IS NOT NULL AND reason IS NULL) OR
        (status='absent' AND combined_digest IS NULL AND section IS NULL AND reason IS NULL) OR
        (status='rejected' AND combined_digest IS NULL AND section IS NULL AND reason IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS agents_instructions_no_update BEFORE UPDATE ON session_agents_instructions
BEGIN SELECT RAISE(ABORT,'project instructions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agents_instructions_no_delete BEFORE DELETE ON session_agents_instructions
BEGIN SELECT RAISE(ABORT,'project instructions are immutable'); END;

-- F3-04 (schema 1.7): opt-in bounded quota wait (K-012). One plan row per session carries the
-- whole wait budget: attempts accumulate across re-quotas and the deadline is fixed at the first quota.
CREATE TABLE IF NOT EXISTS retry_plans (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  step_id TEXT NOT NULL REFERENCES session_steps(id),
  baseline_snapshot_id TEXT NOT NULL REFERENCES workspace_snapshots(id),
  created_at TEXT NOT NULL,
  first_quota_at TEXT NOT NULL,
  next_retry_at TEXT NOT NULL,
  deadline TEXT NOT NULL,
  max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 5),
  attempts_so_far INTEGER NOT NULL DEFAULT 0 CHECK(attempts_so_far >= 0),
  reason TEXT NOT NULL CHECK(reason IN ('QUOTA_EXCEEDED')),
  status TEXT NOT NULL CHECK(status IN ('PENDING','FIRED','CANCELLED','EXPIRED')),
  closed_reason TEXT,
  fired_at TEXT,
  cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_retry_plans_due ON retry_plans(status, next_retry_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_retry_plans_live ON retry_plans(session_id)
  WHERE status IN ('PENDING','FIRED');

-- TB-13 (schema 1.9): user-approved local preview/dev-server commands.
-- A preview command runs project code, so approval is tied to project path, argv/env and
-- the detected script file digest. Runtime rows are mutable state, unlike session/event history.
CREATE TABLE IF NOT EXISTS preview_approvals (
  workspace_path TEXT PRIMARY KEY,
  command_digest TEXT NOT NULL,
  suggestion_json TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  approved_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_servers (
  workspace_path TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('stopped','starting','running','failed')),
  command_digest TEXT,
  suggestion_json TEXT,
  pid INTEGER,
  pgid INTEGER,
  start_time REAL,
  boot_time REAL,
  port INTEGER,
  url TEXT,
  log_tail TEXT NOT NULL DEFAULT '',
  error TEXT,
  started_at TEXT,
  updated_at TEXT NOT NULL
);

-- TB-12 (schema 1.10, K-024): mobile access through a Tailscale-served gateway. Tokens and pairing codes are
-- stored only as hashes; the audit log is append-only.
CREATE TABLE IF NOT EXISTS mobile_devices (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL,
  tailscale_login TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS pairing_codes (
  id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS mobile_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  device_id TEXT,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  status INTEGER NOT NULL,
  outcome TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mobile_audit_no_update BEFORE UPDATE ON mobile_audit
BEGIN SELECT RAISE(ABORT,'mobile audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mobile_audit_no_delete BEFORE DELETE ON mobile_audit
BEGIN SELECT RAISE(ABORT,'mobile audit is append-only'); END;

-- TB-27 (schema 1.11, K-035): a test command a human approved for a folder (an isolated copy uses its source's row).
-- Bound to the digest of what defines the command (package.json, pyproject.toml, …); the runner runs it sandboxed.
CREATE TABLE IF NOT EXISTS test_approvals (
  workspace_path TEXT PRIMARY KEY,
  command_digest TEXT NOT NULL,
  suggestion_json TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  approved_by TEXT NOT NULL
);

-- TB-29 (schema 1.12, K-036): one Web Push subscription per paired phone; gone with the device.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  device_id TEXT PRIMARY KEY REFERENCES mobile_devices(id),
  endpoint TEXT NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at TEXT NOT NULL
);
