"""F3-01a persistent works: messages queue executions; a session stays one execution.

Every mutation owns a transaction (BEGIN IMMEDIATE or a caller savepoint), so the
idempotency lookup, sequence assignment and dispatch claim are atomic.
"""
import base64
import hashlib
import json
import os
from uuid import uuid4
from app import runner_store as repo, loop_store, project_instructions
from app.provider_policy import loop_budget, team_of, kind, split
from app.store import transaction, _now

ACTIVE = ('QUEUED', 'RUNNING')
WAITING = ('WAITING_CAPACITY', 'WAITING_AUTH', 'WAITING_APPROVAL', 'WAITING_CONTEXT', 'WAITING_HUMAN')
REVIEW_PROMPT = ('Review the implementation against the original goal. Read only; do not change files. '
                 'Report concrete problems and checks, or say no changes needed.')
ADDRESS_PROMPT = ('Address the review against the original goal. Make only needed changes, verify them, '
                  'then summarize the outcome. Stop when finished.')


class WorkError(ValueError):
    status, code = 409, 'conflict'

    def __init__(self, detail, code=None, status=None):
        super().__init__(detail)
        self.code = code or self.code
        self.status = status or self.status


class WorkNotFound(WorkError):
    status, code = 404, 'not_found'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def encode_cursor(values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def decode_cursor(cursor):
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError) as exc:
        raise WorkError('invalid cursor', 'validation_error', 422) from exc


def append_event(conn, work_id, kind, payload):
    with transaction(conn):
        seq = conn.execute('SELECT coalesce(max(seq),0)+1 FROM work_events WHERE work_id=?',
                           (work_id,)).fetchone()[0]
        conn.execute('INSERT INTO work_events VALUES (?,?,?,?,?)',
                     (work_id, seq, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()))
        return seq


def get_work(conn, work_id):
    row = conn.execute('SELECT * FROM works WHERE id=? AND deleted_at IS NULL', (work_id,)).fetchone()
    if not row:
        raise WorkNotFound('work not found')
    return dict(row)


def public_work(work):
    return {k: work[k] for k in ('id', 'workspace_path', 'title', 'created_at', 'updated_at',
                                 'archived_at', 'revision')}


def _create_digest(title, workspace_path, isolation):
    return (_hash({'title': title, 'workspace_path': workspace_path}) if not isolation else
            _hash({'title': title, 'workspace_path': isolation['isolated_from'], 'isolated': True}))


def _replay_existing(row, digest):
    if row['deleted_at']:
        raise WorkNotFound('work not found')
    if row['create_hash'] != digest:
        raise WorkError('work id already used with a different payload', 'IDEMPOTENCY_CONFLICT')
    return dict(row), False


def _replay_path(row, workspace_path, isolation):
    """Path for an existing work's digest: the stored path when the request names it, so a replay
    still matches after the folder was moved or deleted; otherwise the canonical path as before."""
    stored = row['workspace_path']
    if isolation:
        return stored   # the isolated digest is keyed on the source, not on this path
    raw = os.path.abspath(os.path.expanduser(str(workspace_path)))
    if stored in (str(workspace_path), raw, os.path.realpath(raw)):
        return stored
    return repo.canonical_workspace(workspace_path)


def create_work(conn, work_id, title, workspace_path, isolation=None):
    """Idempotent by client id: same payload → same work, different payload → 409.
    TB-26: `isolation` = {'isolated_from': source, 'branch': …}; the work runs in `workspace_path` (the copy), the
    payload identity is the source (a replay passes the source and never makes a second copy).
    An existing id is compared against its stored path before any canonicalisation, so a replay
    survives a moved/deleted folder."""
    existing = conn.execute('SELECT * FROM works WHERE id=?', (work_id,)).fetchone()
    if existing:
        return _replay_existing(existing, _create_digest(
            title, _replay_path(existing, workspace_path, isolation), isolation))
    workspace_path = repo.canonical_workspace(workspace_path)
    digest = _create_digest(title, workspace_path, isolation)
    with transaction(conn):
        row = conn.execute('SELECT * FROM works WHERE id=?', (work_id,)).fetchone()
        if row:   # inserted concurrently since the lookup above
            return _replay_existing(row, digest)
        now = _now()
        conn.execute('INSERT INTO works(id,workspace_path,title,create_hash,created_at,updated_at) '
                     'VALUES (?,?,?,?,?,?)', (work_id, workspace_path, title, digest, now, now))
        append_event(conn, work_id, 'work_created', {'title': title, 'workspace_path': workspace_path, **(isolation or {})})
        return get_work(conn, work_id), True


def list_works(conn, *, archived=False, cursor=None, limit=50, q=None):
    """Keyset page (newest first) + totals from one read snapshot. K-059: `q` matches title or folder."""
    where = 'deleted_at IS NULL AND ' + ('archived_at IS NOT NULL' if archived else 'archived_at IS NULL')
    found = []
    if q and q.strip():
        like = '%' + q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where += " AND (title LIKE ? ESCAPE '\\' OR workspace_path LIKE ? ESCAPE '\\')"
        found = [like, like]
    conn.execute('BEGIN')  # one WAL read snapshot for items, total and counts
    try:
        args, page = [], ''
        if cursor:
            created_at, wid = decode_cursor(cursor)
            page = ' AND (created_at < ? OR (created_at = ? AND id < ?))'
            args = [created_at, created_at, wid]
        rows = [dict(r) for r in conn.execute(
            f'SELECT * FROM works WHERE {where}{page} ORDER BY created_at DESC, id DESC LIMIT ?',
            (*found, *args, limit + 1))]
        total = conn.execute(f'SELECT count(*) FROM works WHERE {where}', found).fetchone()[0]
        active = conn.execute('SELECT count(*) FROM works WHERE deleted_at IS NULL AND archived_at IS NULL').fetchone()[0]
        archived_count = conn.execute('SELECT count(*) FROM works WHERE deleted_at IS NULL AND archived_at IS NOT NULL').fetchone()[0]
        items = [dict(public_work(w), **work_summary(conn, w['id'])) for w in rows[:limit]]
    finally:
        conn.execute('COMMIT')
    last = rows[limit - 1] if len(rows) > limit else None
    return {'items': items, 'next_cursor': encode_cursor([last['created_at'], last['id']]) if last else None,
            'total': total, 'counts': {'active': active, 'archived': archived_count}}


def latest_run(conn, work_id):
    row = conn.execute(
        'SELECT r.session_id, r.message_id, m.seq, s.status FROM work_runs r '
        'JOIN work_messages m ON m.id=r.message_id JOIN sessions s ON s.id=r.session_id '
        'WHERE r.work_id=? ORDER BY m.seq DESC LIMIT 1', (work_id,)).fetchone()
    return dict(row) if row else None


def _account_wait(conn, run):
    if not run or run['status'] != 'RUNNING':
        return None
    row = conn.execute(
        "SELECT json_extract(e.payload, '$.account') AS account, "
        "w.id AS holder_work_id, w.title AS holder_work_title "
        "FROM session_events e "
        "LEFT JOIN work_runs hr ON hr.session_id = json_extract(e.payload, '$.holder_session_id') "
        "LEFT JOIN works w ON w.id = hr.work_id AND w.deleted_at IS NULL "
        "WHERE e.session_id = ? AND e.kind = 'account_wait' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM session_events a "
        "  WHERE a.session_id = e.session_id AND a.kind = 'account_acquired' AND a.seq > e.seq"
        ") "
        "ORDER BY e.seq DESC LIMIT 1",
        (run['session_id'],),
    ).fetchone()
    return dict(row) if row else None


def work_summary(conn, work_id):
    run = latest_run(conn, work_id)
    pending = conn.execute("SELECT count(*) FROM work_messages WHERE work_id=? AND status='PENDING'",
                           (work_id,)).fetchone()[0]
    # The panel's chain-acceptance rule needs the newest message's digest; the message list is capped.
    latest = conn.execute('SELECT policy_digest, policy_json FROM work_messages WHERE work_id=? ORDER BY seq DESC LIMIT 1',
                          (work_id,)).fetchone()
    return {'latest_session': run and {'id': run['session_id'], 'status': run['status']},
            'account_wait': _account_wait(conn, run),
            'pending_messages': pending, 'blocked_reason': blocker(conn, work_id),
            'latest_policy_digest': latest[0] if latest else None,
            'latest_team': team_of(json.loads(latest[1])) if latest else None}   # F3-03: follow-up default


def blocker(conn, work_id):
    """Why the next PENDING message may not start; None when it may."""
    run = latest_run(conn, work_id)
    if not run:
        return None
    if run['status'] == 'SUCCEEDED':
        # F3-02: a loop run only frees the queue when the reviewer verified the goal. A loop
        # SUCCEEDED without an outcome is treated as unverified (safe block); legacy is unchanged.
        loop = loop_store.state(conn, run['session_id'])
        if loop is None or loop['outcome'] == 'verified':
            return None
        work = get_work(conn, work_id)
        return None if work['continued_session_id'] == run['session_id'] else 'needs_review_decision'
    if run['status'] in ACTIVE:
        return 'run_active'
    if run['status'] in WAITING:
        return 'run_waiting'          # retry the session first; continue cannot skip it
    if run['status'] == 'RECOVERY_REQUIRED':
        return 'recovery_required'    # recover first
    if run['status'] == 'PAUSED':
        return 'paused'               # resume or cancel the run; continue cannot skip it
    work = get_work(conn, work_id)
    if run['status'] in ('FAILED', 'CANCELLED') and work['continued_session_id'] != run['session_id']:
        return 'needs_continue'
    return None


def add_message(conn, work_id, client_message_id, text, policy_digest, current_policy, current_digest, team=None,
                wait=None, recipe=None, read_only=False):
    """Replay is checked first: an accepted message keeps returning even if policy changed.

    F3-03: an explicit team is part of the payload hash, so the same id with another team is an
    IDEMPOTENCY_CONFLICT; without a team the hash is the pre-F3-03 one (stored rows unchanged).
    F3-04: an opt-in quota wait is hashed the same way (absent = the earlier hash)."""
    body = {'text': text, 'policy_digest': policy_digest}
    if team is not None:
        body['team'] = {role: list(team[role]) for role in sorted(team)}
    if wait is not None:
        body['wait'] = dict(wait)
    if read_only:   # K-057: hashed like the wait (absent = the earlier hash)
        body['read_only'] = True
    if recipe is not None:
        rid = recipe['id'] if isinstance(recipe, dict) else recipe
        body['recipe'] = rid
        if 'recipe' not in current_policy:
            from app import recipes
            current_policy = dict(current_policy, recipe=recipes.frozen(rid))
    payload = _hash(body)
    with transaction(conn):
        work = get_work(conn, work_id)
        row = conn.execute('SELECT * FROM work_messages WHERE work_id=? AND client_message_id=?',
                           (work_id, client_message_id)).fetchone()
        if row:
            if row['payload_hash'] != payload:
                raise WorkError('message id already used with a different payload', 'IDEMPOTENCY_CONFLICT')
            return message_view(conn, dict(row)), False
        if work['archived_at']:
            raise WorkError('work is archived', 'work_archived')
        if policy_digest != current_digest:
            raise WorkError('Provider policy changed; review and accept the current chain', 'conflict')
        seq = conn.execute('SELECT coalesce(max(seq),0)+1 FROM work_messages WHERE work_id=?',
                           (work_id,)).fetchone()[0]
        mid, now = 'msg_' + uuid4().hex, _now()
        conn.execute('INSERT INTO work_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (mid, work_id, client_message_id, seq, text, payload,
                      json.dumps(current_policy, sort_keys=True), current_digest, 'PENDING', now, now))
        conn.execute('UPDATE works SET updated_at=? WHERE id=?', (now, work_id))
        append_event(conn, work_id, 'message_added', {'message_id': mid, 'seq': seq})
        return message_view(conn, dict(conn.execute('SELECT * FROM work_messages WHERE id=?', (mid,)).fetchone())), True


def message_view(conn, msg):
    run = conn.execute('SELECT r.session_id, s.status FROM work_runs r JOIN sessions s ON s.id=r.session_id '
                       'WHERE r.message_id=?', (msg['id'],)).fetchone()
    policy = json.loads(msg['policy_json'])
    recipe = policy.get('recipe')
    return {'id': msg['id'], 'work_id': msg['work_id'], 'client_message_id': msg['client_message_id'],
            'seq': msg['seq'], 'text': msg['content'], 'status': msg['status'],
            'policy_digest': msg['policy_digest'], 'created_at': msg['created_at'],
            'team': team_of(policy),
            'wait': policy.get('wait'),
            'recipe': {k: recipe[k] for k in ('id', 'title', 'version')} if recipe else None,
            'read_only': policy.get('read_only') is True,
            'session': run and {'id': run['session_id'], 'status': run['status'], 'git_changed': _git_changed(conn, run['session_id']),
                                'tests': _check(conn, run['session_id'], 'tests'), 'lint': _check(conn, run['session_id'], 'lint'),
                                'auth_base': _auth_base(conn, run['session_id']) if run['status'] == 'WAITING_AUTH' else None,
                                'reason': _end_reason(conn, run['session_id']) if run['status'] in ('FAILED', 'CANCELLED', 'WAITING_HUMAN') else None}}


def _end_reason(conn, session_id):
    """K-062: why a run ended or waits, as a code (timeout | cancelled | auth | error); the raw reason stays in the
    run's details, never in this summary (it can carry provider error text)."""
    row = conn.execute("SELECT terminal_reason FROM session_steps WHERE session_id=? AND status IN ('FAILED','CANCELLED') "
                       "ORDER BY step_index DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return None
    raw = (row[0] or '').lower()
    return ('timeout' if 'step_timeout' in raw or 'timed out' in raw else 'cancelled' if 'cancelled_by_user' in raw
            else 'auth' if 'home_unusable' in raw or 'auth' in raw else 'error')


def _auth_base(conn, session_id):
    """K-060: the account a WAITING_AUTH run needs signed in again (`agy@hesap2`), from its newest AUTH attempt."""
    row = conn.execute("SELECT a.provider FROM session_attempts a JOIN session_steps s ON s.id=a.step_id "
                       "WHERE s.session_id=? AND a.error_code='AUTH' ORDER BY a.started_at DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return None
    try:
        provider, account, _, _ = split(row[0])
    except ValueError:
        return None
    return f'{provider}@{account}' if account else provider


def _check(conn, session_id, check):
    """TB-27/28: the latest sandboxed test or lint run before a reviewer step (no output; that is in the run's events)."""
    row = conn.execute("SELECT payload FROM session_events WHERE session_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                       (session_id, f'{check}_ran')).fetchone()
    if not row:
        return None
    r = json.loads(row[0])
    return {k: r.get(k) for k in ('status', 'reason', 'command', 'exit_code', 'duration_s')}


def _git_changed(conn, session_id):
    """TB-23: the `.git` hooks/config paths a run left behind (empty when none)."""
    row = conn.execute("SELECT payload FROM session_events WHERE session_id=? AND kind='git_changed' ORDER BY seq DESC LIMIT 1",
                       (session_id,)).fetchone()
    return json.loads(row[0]).get('paths', []) if row else []


def list_messages(conn, work_id, *, cursor=None, limit=50):
    get_work(conn, work_id)
    after = int(decode_cursor(cursor)) if cursor else 0
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM work_messages WHERE work_id=? AND seq>? ORDER BY seq LIMIT ?', (work_id, after, limit + 1))]
    items = [message_view(conn, m) for m in rows[:limit]]
    return {'items': items, 'next_cursor': encode_cursor(rows[limit - 1]['seq']) if len(rows) > limit else None}


def cancel_message(conn, work_id, message_id):
    with transaction(conn):
        get_work(conn, work_id)
        row = conn.execute('SELECT * FROM work_messages WHERE id=? AND work_id=?', (message_id, work_id)).fetchone()
        if not row:
            raise WorkNotFound('message not found')
        if row['status'] == 'CANCELLED':
            return message_view(conn, dict(row)), False
        if row['status'] != 'PENDING':
            raise WorkError('message already started; cancel its session instead', 'message_linked')
        changed = conn.execute("UPDATE work_messages SET status='CANCELLED', updated_at=? "
                               "WHERE id=? AND status='PENDING'", (_now(), message_id)).rowcount
        if changed != 1:
            raise WorkError('message state changed', 'conflict')
        append_event(conn, work_id, 'message_cancelled', {'message_id': message_id})
        return message_view(conn, dict(conn.execute('SELECT * FROM work_messages WHERE id=?', (message_id,)).fetchone())), True


def patch_work(conn, work_id, expected_revision, *, title=None, archived=None):
    with transaction(conn):
        work = get_work(conn, work_id)
        if work['revision'] != expected_revision:
            raise WorkError('work changed; reload and retry', 'revision_conflict')
        fields, values = ['revision=revision+1', 'updated_at=?'], [_now()]
        if title is not None:
            fields.append('title=?'); values.append(title)
        if archived is True and not work['archived_at']:
            pending = conn.execute("SELECT 1 FROM work_messages WHERE work_id=? AND status='PENDING'",
                                   (work_id,)).fetchone()
            run = latest_run(conn, work_id)
            if pending or (run and run['status'] in ACTIVE + WAITING + ('RECOVERY_REQUIRED', 'PAUSED')):
                raise WorkError('work has pending or unresolved execution', 'work_active')
            fields.append('archived_at=?'); values.append(_now())
        elif archived is False and work['archived_at']:
            fields.append('archived_at=NULL')
        changed = conn.execute(f'UPDATE works SET {", ".join(fields)} WHERE id=? AND revision=?',
                               (*values, work_id, expected_revision)).rowcount
        if changed != 1:
            raise WorkError('work changed; reload and retry', 'revision_conflict')
        append_event(conn, work_id, 'work_updated', {'title': title, 'archived': archived})
        return get_work(conn, work_id)


def delete_work(conn, work_id, expected_revision):
    with transaction(conn):
        work = get_work(conn, work_id)
        if work['revision'] != expected_revision:
            raise WorkError('work changed; reload and retry', 'revision_conflict')
        run = latest_run(conn, work_id)
        if run and run['status'] in ACTIVE + WAITING + ('RECOVERY_REQUIRED', 'PAUSED'):
            raise WorkError('work_active', 'work_active')
        now = _now()
        changed = conn.execute(
            'UPDATE works SET deleted_at=?, updated_at=?, revision=revision+1 '
            'WHERE id=? AND revision=? AND deleted_at IS NULL',
            (now, now, work_id, expected_revision)).rowcount
        if changed != 1:
            raise WorkError('work changed; reload and retry', 'revision_conflict')
        # Unlike archive, delete does not refuse queued messages: they die with the work, atomically.
        conn.execute("UPDATE work_messages SET status='CANCELLED' WHERE work_id=? AND status='PENDING'",
                     (work_id,))
        append_event(conn, work_id, 'work_deleted', {'deleted_at': now})
        return dict(conn.execute('SELECT * FROM works WHERE id=?', (work_id,)).fetchone())


CONTINUABLE_OUTCOMES = tuple(o for o in loop_store.OUTCOMES if o != 'verified')


def continue_work(conn, work_id, expected_session_id, expected_outcome=None):
    """Explicit human decision to unblock the queue after a clean FAILED/CANCELLED run, or
    (F3-02) after a loop run that ended without `verified` — CAS on session + outcome."""
    with transaction(conn):
        work = get_work(conn, work_id)
        run = latest_run(conn, work_id)
        if not run or run['session_id'] != expected_session_id:
            raise WorkError('stale session; reload the work', 'stale_session')
        if run['status'] == 'WAITING_APPROVAL':
            raise WorkError('a denied tool never retries; cancel the run first', 'cancel_first')
        if run['status'] in WAITING:
            raise WorkError('session is waiting; use its retry first', 'use_retry')
        if run['status'] == 'RECOVERY_REQUIRED':
            raise WorkError('session needs recovery first', 'recovery_required')
        if run['status'] == 'PAUSED':
            raise WorkError('session is paused; resume or cancel it', 'paused')
        loop = loop_store.state(conn, run['session_id'])
        if run['status'] == 'SUCCEEDED' and loop is not None and loop['outcome'] != 'verified':
            if loop['outcome'] not in CONTINUABLE_OUTCOMES:
                raise WorkError('loop outcome is not recorded; reload the work', 'outcome_missing')
            if expected_outcome != loop['outcome']:
                raise WorkError('outcome changed; reload the work', 'stale_outcome')
        elif run['status'] not in ('FAILED', 'CANCELLED'):
            raise WorkError('nothing to continue', 'not_blocked')
        if work['continued_session_id'] == expected_session_id:
            raise WorkError('this run was already continued; reload the work', 'already_continued')
        conn.execute('UPDATE works SET continued_session_id=?, updated_at=? WHERE id=?',
                     (expected_session_id, _now(), work_id))
        append_event(conn, work_id, 'continued', {'after_session_id': expected_session_id,
                                                  'previous_status': run['status']})
        return True


def works_with_pending(conn):
    return [r[0] for r in conn.execute(
        "SELECT m.work_id FROM work_messages m JOIN works w ON w.id=m.work_id "
        "WHERE m.status='PENDING' AND w.archived_at IS NULL AND w.deleted_at IS NULL "
        "GROUP BY m.work_id ORDER BY min(m.created_at)")]


def dispatch_next(conn, work_id):
    """Resolve project instructions outside the writer lock, then CAS-claim.

    Returns ``(sid, None)`` when runnable, ``(sid, 'rejected')`` for a visible
    preflight failure, and ``(None, reason)`` while waiting.  The second phase
    revalidates both the oldest message and workspace path before using the
    unlocked filesystem result.
    """
    with transaction(conn):
        row = conn.execute('SELECT workspace_path, archived_at, deleted_at FROM works WHERE id=?', (work_id,)).fetchone()
        if not row or row['deleted_at'] or row['archived_at']:
            return None, 'work_deleted' if (not row or row['deleted_at']) else 'work_archived'
        reason = blocker(conn, work_id)
        if reason:
            return None, reason
        candidate = conn.execute("SELECT id FROM work_messages WHERE work_id=? AND status='PENDING' "
                                 "ORDER BY seq LIMIT 1", (work_id,)).fetchone()
        if not candidate:
            return None, 'no_pending'
        workspace_path = row['workspace_path']
        candidate_id = candidate['id']

    # Both files are read here; the frozen policy (known only under the lock) decides which are used.
    instruction_values = {p: project_instructions.resolve(workspace_path, provider=p)
                          for p in project_instructions.FILES}

    with transaction(conn):
        row = conn.execute('SELECT * FROM works WHERE id=?', (work_id,)).fetchone()
        if not row or row['deleted_at'] or row['archived_at']:
            return None, 'work_deleted' if (not row or row['deleted_at']) else 'work_archived'
        reason = blocker(conn, work_id)
        if reason:
            return None, reason
        msg = conn.execute("SELECT * FROM work_messages WHERE work_id=? AND status='PENDING' "
                           "ORDER BY seq LIMIT 1", (work_id,)).fetchone()
        if not msg:
            return None, 'no_pending'
        work = dict(row)
        if msg['id'] != candidate_id or work['workspace_path'] != workspace_path:
            return None, 'dispatch_race'
        policy = json.loads(msg['policy_json'])
        sid = 'sess_' + uuid4().hex
        repo.create_session(conn, sid, work['workspace_path'], 'auto', policy=policy)
        # K-011: CLAUDE.md is stored and enforced as before (every work session). K-014: AGENTS.md
        # only when the frozen policy runs Codex.
        used = {kind(c['provider']) for chain in policy['roles'].values() for c in chain}
        failed = None
        for provider, value in instruction_values.items():
            if provider == 'claude' or provider in used:
                project_instructions.store_snapshot(conn, sid, value, provider)
                if failed is None and value['status'] == 'rejected':
                    failed = value
        rejected = failed is not None
        if not rejected:
            budget = loop_budget(policy)
            if budget:   # F3-02: only turn 1 now; later turns are appended by the turn decision
                loop_store.init(conn, sid, budget, policy['roles']['author'][0]['provider'],
                                policy['roles']['reviewer'][0]['provider'], msg['content'], REVIEW_PROMPT)
            else:        # version-2 policy (incl. messages queued before F3-02): legacy three steps
                for i, (role, prompt) in enumerate(zip(('author', 'reviewer', 'author'),
                                                       (msg['content'], REVIEW_PROMPT, ADDRESS_PROMPT))):
                    repo.add_session_step(conn, f'{sid}_{i}', sid, i, policy['roles'][role][0]['provider'], prompt, role=role)
        changed = conn.execute("UPDATE work_messages SET status='LINKED', updated_at=? WHERE id=? AND status='PENDING'",
                               (_now(), msg['id'])).rowcount
        if changed != 1:
            raise WorkError('message was claimed concurrently', 'conflict')
        conn.execute('INSERT INTO work_runs VALUES (?,?,?,?,?)', (msg['id'], sid, work_id, 'QUEUED', _now()))
        if rejected:
            repo.update_session_status(conn, sid, 'FAILED')
            payload = {k: failed.get(k) for k in ('reason', 'path', 'source_path', 'line')}
            repo.checkpoint(conn, sid, None, 'project_instructions_rejected', payload)
            append_event(conn, work_id, 'project_instructions_rejected',
                         dict(payload, message_id=msg['id'], session_id=sid, seq=msg['seq']))
            return sid, 'rejected'
        append_event(conn, work_id, 'dispatched', {'message_id': msg['id'], 'session_id': sid, 'seq': msg['seq']})
        return sid, None


def sync_run_status(conn):
    """Emit a work event whenever a linked session's status changed (idempotent)."""
    rows = [dict(r) for r in conn.execute(
        'SELECT r.message_id, r.work_id, r.session_id, r.last_status, s.status FROM work_runs r '
        'JOIN sessions s ON s.id=r.session_id WHERE r.last_status IS NOT s.status')]
    for r in rows:
        with transaction(conn):
            changed = conn.execute('UPDATE work_runs SET last_status=? WHERE message_id=? AND last_status IS ?',
                                   (r['status'], r['message_id'], r['last_status'])).rowcount
            if changed:
                append_event(conn, r['work_id'], 'run_status', {'session_id': r['session_id'], 'status': r['status']})
    return len(rows)


# ── runner integration ─────────────────────────────────────────────────────────

def run_context(conn, session_id):
    """Work facts for a linked session, or None for a legacy session."""
    row = conn.execute('SELECT r.work_id, m.* FROM work_runs r JOIN work_messages m ON m.id=r.message_id '
                       'WHERE r.session_id=?', (session_id,)).fetchone()
    if not row:
        return None
    msg = dict(row)
    work = get_work(conn, msg['work_id'])
    first = conn.execute('SELECT m.content FROM work_messages m JOIN work_runs r ON r.message_id=m.id '
                         'WHERE m.work_id=? AND m.seq<=? ORDER BY m.seq LIMIT 1',
                         (work['id'], msg['seq'])).fetchone()[0]
    return {'work': work, 'message': msg, 'first_goal': first}


def previous_runs(conn, work_id, before_seq):
    """Earlier linked runs, newest first, with their real step outputs."""
    out = []
    for r in conn.execute(
            'SELECT m.id AS message_id, m.seq, m.content, r.session_id, s.status, s.created_at '
            'FROM work_runs r JOIN work_messages m ON m.id=r.message_id JOIN sessions s ON s.id=r.session_id '
            'WHERE r.work_id=? AND m.seq<? ORDER BY m.seq DESC', (work_id, before_seq)):
        run = dict(r)
        steps = repo.get_session_steps(conn, run['session_id'])
        if loop_store.state(conn, run['session_id']) is not None:
            # Loop runs: the last completed author step and the last completed review (turn + role).
            done = [st for st in steps if st['status'] == 'COMPLETED']
            run['author_result'] = next((st['result_text'] for st in reversed(done) if st['role'] == 'author'), None)
            run['review'] = next((st['result_text'] for st in reversed(done) if st['role'] == 'reviewer'), None)
        else:
            by_index = {st['step_index']: st for st in steps}
            run['author_result'] = (by_index.get(2) or {}).get('result_text')
            run['review'] = (by_index.get(1) or {}).get('result_text')
        out.append(run)
    return out


def decide_resume(conn, session_id, ctx, current_snapshot, current_snapshot_id, author_chain):
    """RESUME only when every gate holds; otherwise FRESH with the first failing reason."""
    work, msg = ctx['work'], ctx['message']
    decision = {'decision': 'FRESH', 'reason': None, 'source_session_id': None, 'source_attempt_id': None,
                'provider_session_id': None, 'baseline_snapshot_id': None}

    def fresh(reason):
        decision['reason'] = reason
        return decision

    def evaluate():
        prev = conn.execute(
            'SELECT r.session_id, m.policy_digest, s.status, s.workspace_path, s.created_at FROM work_runs r '
            'JOIN work_messages m ON m.id=r.message_id JOIN sessions s ON s.id=r.session_id '
            'WHERE r.work_id=? AND m.seq<? ORDER BY m.seq DESC LIMIT 1', (work['id'], msg['seq'])).fetchone()
        if not prev:
            return fresh('no_previous_run')
        decision['source_session_id'] = prev['session_id']
        if prev['status'] != 'SUCCEEDED':
            return fresh('previous_run_not_succeeded')
        if not author_chain or kind(author_chain[0]['provider']) != 'codex':
            return fresh('first_provider_not_codex')
        if prev['policy_digest'] != msg['policy_digest']:
            return fresh('policy_changed')
        current = repo.get_session(conn, session_id)
        if not (prev['workspace_path'] == current['workspace_path'] == work['workspace_path']):
            return fresh('workspace_mismatch')
        last_author = conn.execute(
            "SELECT st.id FROM session_steps st JOIN step_roles r ON r.step_id=st.id "
            "WHERE st.session_id=? AND r.role='author' ORDER BY st.step_index DESC LIMIT 1",
            (prev['session_id'],)).fetchone()
        attempt = last_author and conn.execute(
            "SELECT * FROM session_attempts WHERE step_id=? AND status='COMPLETED' ORDER BY attempt_no DESC LIMIT 1",
            (last_author['id'],)).fetchone()
        if not attempt:
            return fresh('no_completed_author_attempt')
        decision['source_attempt_id'] = attempt['id']
        if kind(attempt['provider']) != 'codex' or attempt['profile'] != 'WRITE':
            return fresh('last_author_not_codex')
        if attempt['provider'] != author_chain[0]['provider']:
            # TB-06: another account (or model) of the same tool cannot continue this thread.
            return fresh('identity_changed')
        if not attempt['provider_session_id']:
            return fresh('no_provider_session')
        if project_instructions.snapshot(conn, prev['session_id'], 'codex') is None:
            # K-014: the thread lives in the user's ~/.codex (pre-TB-05), not in Conductor's CODEX_HOME.
            return fresh('codex_home_changed')
        if not repo.attempt_clean(conn, attempt['id']):
            return fresh('author_attempt_unclean')
        # Another Conductor writer since the previous run: a newer session, or an older one
        # that ran again (retry) after it — even if the folder hash came back identical.
        other = conn.execute(
            'SELECT 1 FROM sessions s WHERE s.workspace_path=? AND s.id NOT IN (?,?) AND (s.created_at>? OR EXISTS('
            'SELECT 1 FROM session_steps st JOIN session_attempts a ON a.step_id=st.id '
            'WHERE st.session_id=s.id AND a.started_at>?))',
            (work['workspace_path'], prev['session_id'], session_id, prev['created_at'], prev['created_at'])).fetchone()
        if other:
            return fresh('other_writer')
        base = conn.execute("SELECT id, snapshot_json FROM workspace_snapshots WHERE session_id=? "
                            "AND kind='work_success_end'", (prev['session_id'],)).fetchone()
        if not base:
            return fresh('baseline_missing')
        decision['baseline_snapshot_id'] = base['id']
        baseline = json.loads(base['snapshot_json'])
        if not (baseline.get('complete') and current_snapshot.get('complete')
                and baseline.get('unknown') == [] and current_snapshot.get('unknown') == []):
            return fresh('snapshot_incomplete')
        if baseline.get('scope') is None or baseline.get('scope') != current_snapshot.get('scope'):
            return fresh('snapshot_scope_mismatch')
        if baseline.get('files') != current_snapshot.get('files'):
            return fresh('workspace_changed')
        decision.update(decision='RESUME', reason='all_gates_passed',
                        provider_session_id=attempt['provider_session_id'])
        return decision

    if conn.execute('SELECT 1 FROM work_resume_refs WHERE session_id=?', (session_id,)).fetchone():
        # A human retry re-runs this session: the first decision stays immutable, the retry is FRESH.
        fresh('retry_fresh')
        with transaction(conn):
            repo.checkpoint(conn, session_id, None, 'resume_decision', dict(decision, current_snapshot_id=current_snapshot_id))
            append_event(conn, work['id'], 'resume_decision', dict(decision, session_id=session_id,
                                                                   current_snapshot_id=current_snapshot_id))
        return decision
    evaluate()
    with transaction(conn):
        conn.execute('INSERT INTO work_resume_refs VALUES (?,?,?,?,?,?,?,?,?)',
                     (session_id, decision['decision'], decision['reason'], decision['source_session_id'],
                      decision['source_attempt_id'], decision['provider_session_id'],
                      decision['baseline_snapshot_id'], current_snapshot_id, _now()))
        public = {k: v for k, v in decision.items() if k != 'provider_session_id'}
        public['current_snapshot_id'] = current_snapshot_id
        repo.checkpoint(conn, session_id, None, 'resume_decision', public)
        append_event(conn, work['id'], 'resume_decision', dict(public, session_id=session_id))
    return decision


def record_success_end(conn, session_id, ctx, end_snapshot, author_attempt_id):
    """Snapshot (or explicit unavailable record) + SUCCEEDED in one transaction; lock still held."""
    with transaction(conn):
        snap_id = repo.save_snapshot(conn, session_id, author_attempt_id, 'work_success_end', end_snapshot)
        repo.checkpoint(conn, session_id, None, 'work_success_end',
                        {'snapshot_id': snap_id, 'complete': end_snapshot.get('complete', False),
                         'unknown': end_snapshot.get('unknown', ['unavailable'])[:20]})
        repo.update_session_status(conn, session_id, 'SUCCEEDED')
        append_event(conn, ctx['work']['id'], 'run_succeeded', {'session_id': session_id, 'snapshot_id': snap_id})
