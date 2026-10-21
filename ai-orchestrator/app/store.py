"""G2 SQLite repository. No watcher, delivery I/O or transition policy.

Use connect() and migrate() explicitly. Each write owns a BEGIN IMMEDIATE
transaction, or a savepoint inside a caller-owned transaction(). No implicit
commit occurs when repository methods are composed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import difflib
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

SCHEMA_VERSION = "1.13"
# TB-02 (1.4) upgrades attempt chains to generation zero (rebuild under the owner lock);
# F3-02 (1.5), TB-03 (1.6), F3-04 (1.7, retry_plans), TB-05 (1.8, AGENTS.md
# snapshots), TB-13 (1.9, preview approvals/process state), TB-12 (1.10, mobile devices/audit) and TB-27 (1.11,
# test approvals), TB-29 (1.12, push subscriptions) and TB-37 (1.13, work soft-delete) are additive.
SUPPORTED_VERSIONS = ("1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12", SCHEMA_VERSION)
RESET_FIELDS = (
    "approved_version_id", "approved_by", "delivered_version_id",
    "delivered_path", "delivered_sha256", "delivered_at",
)


class InvalidTransition(ValueError):
    def __init__(self, eid, expected_from, to_state):
        super().__init__(f"{eid}: expected {expected_from}, cannot change to {to_state}")


class VersionMismatch(ValueError):
    def __init__(self, eid, version_id):
        super().__init__(f"{eid}: invalid or unapproved version {version_id}")


class DeliveredHashMismatch(ValueError):
    def __init__(self, eid, expected, actual):
        super().__init__(f"{eid}: delivered hash {actual} does not match {expected}")


def connect(database, *, timeout=5.0):
    """Open only the supplied SQLite DB (or :memory:); never create folders.

    Each thread must have its own connection. Persistent databases use the
    accepted 5000ms busy timeout and WAL/NORMAL; memory databases retain timeout.
    Contending writers can still exhaust the timeout and raise OperationalError.
    """
    conn = sqlite3.connect(database, isolation_level=None, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute('PRAGMA database_list').fetchone()[2]:
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn):
    """Acquire the writer lock before validation reads; nested calls use savepoints."""
    nested = conn.in_transaction
    savepoint = f"repo_{uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        conn.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn, *, worker_owned=False):
    """Migrate before workers start. Rebuilds require an idle, dedicated connection."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_meta'").fetchone()
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() if exists else None
    if row and row[0] not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported database schema version: {row[0]}")
    owner = None
    upgrading = bool(row and row[0] != SCHEMA_VERSION)
    if upgrading:
        if conn.in_transaction:
            raise ValueError('migration requires a dedicated connection outside a transaction')
        database = conn.execute('PRAGMA database_list').fetchone()[2]
        if database and not worker_owned:
            import fcntl
            owner = open(Path(database).with_suffix('.runner.lock'), 'a')
            try:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                owner.close()
                raise
        conn.execute('PRAGMA foreign_keys=OFF')
    try:
        _migrate_transaction(conn)
    finally:
        if upgrading:
            conn.execute('PRAGMA foreign_keys=ON')
        if owner:
            owner.close()


def _migrate_transaction(conn):
    with transaction(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row and row[0] not in SUPPORTED_VERSIONS:
            raise ValueError(f"Unsupported database schema version: {row[0]}")
        if row and row[0] in ('1.1', '1.2', '1.3') and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_attempts'").fetchone():
            _rebuild_attempts(conn)   # pre-1.4 attempt shape only; 1.4 → 1.5 is additive
        if row and row[0] != SCHEMA_VERSION:
            step_columns = {r[1] for r in conn.execute('PRAGMA table_info(session_steps)')}
            if step_columns and 'retry_generation' not in step_columns:
                conn.execute('ALTER TABLE session_steps ADD COLUMN retry_generation INTEGER '
                             'NOT NULL DEFAULT 0 CHECK(retry_generation >= 0)')
            work_columns = {r[1] for r in conn.execute('PRAGMA table_info(works)')}
            if work_columns and 'deleted_at' not in work_columns:
                conn.execute('ALTER TABLE works ADD COLUMN deleted_at TEXT')
        # Avoid executescript(): it can commit the caller's transaction early.
        statement = ""
        for line in Path(__file__).with_name("schema.sql").read_text().splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ""
        for table in ("versions", "history"):
            for action in ("UPDATE", "DELETE"):
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                    BEFORE {action} ON {table} BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only'); END""")
        conn.execute("INSERT INTO schema_meta(key,value) VALUES ('schema_version',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('migration foreign key check failed')


def _rebuild_attempts(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_attempts'").fetchone()
    schema = Path(__file__).with_name('schema.sql').read_text()
    start = schema.index('CREATE TABLE IF NOT EXISTS session_attempts (')
    statement = schema[start:schema.index(';', start)+1]
    
    if not exists:
        conn.execute(statement)
        return
        
    definitions = [r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='session_attempts' AND type IN ('index','trigger') AND sql IS NOT NULL")]
    columns = [r[1] for r in conn.execute('PRAGMA table_info(session_attempts)')]
    conn.execute(statement.replace('IF NOT EXISTS session_attempts (', 'session_attempts_new (', 1))
    names = ','.join('"' + name.replace('"','""') + '"' for name in columns)
    if names:
        conn.execute(f'INSERT INTO session_attempts_new ({names}) SELECT {names} FROM session_attempts')
    conn.execute('DROP TABLE session_attempts')
    conn.execute('ALTER TABLE session_attempts_new RENAME TO session_attempts')
    for definition in definitions:
        conn.execute(definition)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(content):
    if not isinstance(content, bytes):
        raise TypeError("Content must be bytes")
    return hashlib.sha256(content).hexdigest()


def _history(conn, eid, before, after, event, actor, detail=None):
    conn.execute("INSERT INTO history(envelope_id,at,from_state,to_state,event,actor,detail) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (eid, _now(), before, after, event, actor,
                  json.dumps(detail or {}, ensure_ascii=False)))


def _cas(cursor, eid, expected, target):
    if cursor.rowcount != 1:
        raise InvalidTransition(eid, expected, target)


def _owned_version(conn, eid, vid):
    row = conn.execute("SELECT * FROM versions WHERE version_id=? AND envelope_id=?",
                       (vid, eid)).fetchone()
    if row is None:
        raise VersionMismatch(eid, vid)
    return dict(row)


def insert_captured(conn, meta, content):
    eid = meta['id']
    vid = f"ver_{eid}_0"
    sha = _hash(content)
    if meta.get('mode', 'manual') != 'manual' or meta.get('trigger', 'human') != 'human':
        raise ValueError("G2 capture requires manual mode and human trigger")
    with transaction(conn):
        conn.execute("""INSERT INTO envelopes
            (id,schema_version,created_at,source_agent,target_agent,type,title,origin_ref,
             bytes,captured_version_id,mode,trigger,state,note)
            VALUES (?,?,?,?,?,?,?,?,?,?,'manual','human','CAPTURED',?)""",
            (eid, meta.get('schema_version', '1.0'), _now(), meta['source_agent'],
             meta['target_agent'], meta.get('type'), meta.get('title'), meta.get('origin_ref'),
             len(content), vid, meta.get('note')))
        conn.execute("""INSERT INTO versions
            (version_id,envelope_id,kind,content,sha256,bytes,created_at,created_by)
            VALUES (?,?,'captured',?,?,?,?, 'system')""",
            (vid, eid, content, sha, len(content), _now()))
        _history(conn, eid, None, 'CAPTURED', 'capture', 'system',
                 {'version_id': vid, 'sha256': sha})
    return eid, vid


def add_edited_version(conn, eid, content, actor='human', *, base_version_id=None):
    vid = f"ver_{uuid4().hex}"
    sha = _hash(content)
    with transaction(conn):
        envelope = get_envelope(conn, eid)
        if envelope is None:
            raise KeyError(eid)
        base = _owned_version(conn, eid, base_version_id or envelope['captured_version_id'])
        conn.execute("""INSERT INTO versions
            (version_id,envelope_id,kind,content,sha256,bytes,created_at,created_by)
            VALUES (?,?,'edited',?,?,?,?,?)""",
            (vid, eid, content, sha, len(content), _now(), actor))
        # Byte-preserving audit diff, even for content that is not valid UTF-8.
        diff = b''.join(difflib.diff_bytes(
            difflib.unified_diff, base['content'].splitlines(keepends=True),
            content.splitlines(keepends=True), fromfile=b'base', tofile=b'edited'))
        _history(conn, eid, envelope['state'], envelope['state'], 'edited', actor,
                 {'version_id': vid, 'sha256': sha, 'base_version_id': base['version_id'],
                  'diff_base64': base64.b64encode(diff).decode('ascii')})
    return vid


def set_state(conn, eid, expected_from, to_state, event, actor, detail=None):
    """Low-level CAS only. G3 will enforce the legal transition/event map."""
    with transaction(conn):
        _cas(conn.execute("UPDATE envelopes SET state=? WHERE id=? AND state=?",
                          (to_state, eid, expected_from)), eid, expected_from, to_state)
        _history(conn, eid, expected_from, to_state, event, actor, detail)


def set_approved(conn, eid, approved_version_id, approved_by,
                 expected_from='PENDING_APPROVAL'):
    with transaction(conn):
        _owned_version(conn, eid, approved_version_id)
        _cas(conn.execute("UPDATE envelopes SET state='APPROVED', approved_version_id=?, "
                          "approved_by=? WHERE id=? AND state=?",
                          (approved_version_id, approved_by, eid, expected_from)),
             eid, expected_from, 'APPROVED')
        _history(conn, eid, expected_from, 'APPROVED', 'human_approve', approved_by,
                 {'version_id': approved_version_id})


def record_delivery_intent(conn, eid, version_id, dest_path, expected_from='APPROVED',
                           *, actor='system', event='deliver_intent'):
    with transaction(conn):
        envelope = get_envelope(conn, eid)
        if not envelope or not envelope['approved_version_id'] or envelope['approved_version_id'] != version_id:
            raise VersionMismatch(eid, version_id)
        version = _owned_version(conn, eid, version_id)
        if not isinstance(dest_path, str) or not dest_path:
            raise ValueError("Delivery path must be nonempty text")
        if expected_from == 'FAILED' and (
                envelope['delivered_version_id'] != version_id or
                envelope['delivered_path'] != dest_path):
            raise ValueError("Retry must preserve the recorded delivery intent")
        _cas(conn.execute("UPDATE envelopes SET state='DELIVERING', delivered_version_id=?, "
                          "delivered_path=? WHERE id=? AND state=?",
                          (version_id, dest_path, eid, expected_from)),
             eid, expected_from, 'DELIVERING')
        _history(conn, eid, expected_from, 'DELIVERING', event, actor,
                 {'version_id': version_id, 'path': dest_path, 'sha256': version['sha256']})


def record_delivered(conn, eid, actual_sha256, expected_from='DELIVERING', *, actor='system'):
    with transaction(conn):
        row = conn.execute("""SELECT v.sha256 FROM envelopes e JOIN versions v
            ON v.version_id=e.delivered_version_id AND v.envelope_id=e.id
            AND v.version_id=e.approved_version_id WHERE e.id=?""", (eid,)).fetchone()
        expected = row[0] if row else None
        if expected is None or actual_sha256 != expected:
            raise DeliveredHashMismatch(eid, expected, actual_sha256)
        _cas(conn.execute("UPDATE envelopes SET state='DELIVERED', delivered_sha256=?, "
                          "delivered_at=? WHERE id=? AND state=?",
                          (actual_sha256, _now(), eid, expected_from)),
             eid, expected_from, 'DELIVERED')
        _history(conn, eid, expected_from, 'DELIVERED', 'deliver_ok', actor,
                 {'sha256': actual_sha256})


def reset_for_reapproval(conn, eid, *, actor, reason):
    with transaction(conn):
        envelope = get_envelope(conn, eid)
        if not envelope or envelope['state'] != 'FAILED':
            raise InvalidTransition(eid, 'FAILED', 'PENDING_APPROVAL')
        previous = {field: envelope[field] for field in RESET_FIELDS}
        assignments = ', '.join(f'{field}=NULL' for field in RESET_FIELDS)
        _cas(conn.execute(f"UPDATE envelopes SET state='PENDING_APPROVAL', {assignments} "
                          "WHERE id=? AND state='FAILED'", (eid,)),
             eid, 'FAILED', 'PENDING_APPROVAL')
        _history(conn, eid, 'FAILED', 'PENDING_APPROVAL', 'escalate', actor,
                 {'reason': reason, 'previous': previous})
        return get_envelope(conn, eid)


def get_envelope(conn, eid):
    row = conn.execute('SELECT * FROM envelopes WHERE id=?', (eid,)).fetchone()
    return dict(row) if row else None


def get_version(conn, vid):
    row = conn.execute('SELECT * FROM versions WHERE version_id=?', (vid,)).fetchone()
    return dict(row) if row else None


def get_content(conn, vid):
    version = get_version(conn, vid)
    if version is None:
        raise KeyError(vid)
    return version['content']


def list_by_state(conn, state):
    return [dict(row) for row in conn.execute(
        'SELECT * FROM envelopes WHERE state=? ORDER BY created_at,id', (state,))]


def get_history(conn, eid):
    return [dict(row) | {'detail': json.loads(row['detail'])} for row in conn.execute(
        'SELECT * FROM history WHERE envelope_id=? ORDER BY seq', (eid,))]
