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
