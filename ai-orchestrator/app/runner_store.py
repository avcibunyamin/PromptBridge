"""Transactional work claims, steps and checkpoints, separate from envelopes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from app.store import transaction, _now


class RetryNotAllowed(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


class WorkspaceBusy(ValueError):
    pass


def canonical_workspace(workspace_path):
    path = Path(workspace_path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError('workspace must be an existing directory')
    return str(path)


def acquire_lock(conn, workspace_path, session_id):
    workspace_path = canonical_workspace(workspace_path)
    with transaction(conn):
        # No reentrant claim: even a second worker for the same session is refused.
        return conn.execute(
            'INSERT OR IGNORE INTO workspace_locks VALUES (?, ?, ?)',
            (workspace_path, session_id, _now())).rowcount == 1


def release_lock(conn, workspace_path, session_id):
    # Use the persisted canonical path, even if a user renamed/deleted the folder.
    with transaction(conn):
        conn.execute('DELETE FROM workspace_locks WHERE workspace_path=? AND session_id=?',
                     (workspace_path, session_id))


def claim_session(conn, session_id):
    with transaction(conn):
        session = get_session(conn, session_id)
        if not session or session['status'] != 'QUEUED':
            return False
        if not acquire_lock(conn, session['workspace_path'], session_id):
            update_session_status(conn, session_id, 'FAILED')
            return False
        return conn.execute("UPDATE sessions SET status='RUNNING', updated_at=? "
                            "WHERE id=? AND status='QUEUED'", (_now(), session_id)).rowcount == 1


def create_session(conn, session_id, workspace_path, mode, *, policy=None):
    workspace_path = canonical_workspace(workspace_path)
    if mode != 'auto':
        raise ValueError('Only the bounded auto workflow is implemented')
    with transaction(conn):
        # Refuse another accepted work item too, before a worker claims it.
        busy = conn.execute("SELECT 1 FROM sessions WHERE workspace_path=? AND status IN "
                            "('QUEUED','RUNNING','RECOVERY_REQUIRED')", (workspace_path,)).fetchone()
        locked = conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?',
                              (workspace_path,)).fetchone()
        if busy or locked:
            raise WorkspaceBusy('workspace already has active or unresolved work')
        conn.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)',
                     (session_id, workspace_path, mode, 'QUEUED', _now(), _now()))
        from app.provider_policy import normalize, DEFAULT, digest
        frozen = policy if policy is not None else normalize(DEFAULT)
        conn.execute('INSERT INTO session_policies VALUES (?, ?, ?)',
                     (session_id, json.dumps(frozen, sort_keys=True), digest(frozen)))


def add_session_step(conn, step_id, session_id, step_index, agent_name, task_prompt, *, role=None):
    if not task_prompt.strip():
        raise ValueError('task prompt must not be empty')
    with transaction(conn):
        conn.execute('INSERT INTO session_steps(id, session_id, step_index, agent_name, '
                     'task_prompt, status) VALUES (?, ?, ?, ?, ?, ?)',
                     (step_id, session_id, step_index, agent_name, task_prompt, 'PENDING'))
        from app.provider_policy import role_profile
        role = role or ('reviewer' if step_index == 1 else 'author')
        frozen = conn.execute('SELECT policy_json FROM session_policies WHERE session_id=?', (session_id,)).fetchone()
        profile = role_profile(json.loads(frozen[0]) if frozen else None, role)   # K-057: a read-only run's author too
        conn.execute('INSERT INTO step_roles VALUES (?, ?, ?)', (step_id, role, profile))


def get_session(conn, session_id):
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    return dict(row) if row else None


def get_session_steps(conn, session_id):
    return [dict(row) for row in conn.execute(
        'SELECT s.*, r.role, r.profile FROM session_steps s LEFT JOIN step_roles r ON r.step_id=s.id WHERE s.session_id=? ORDER BY s.step_index', (session_id,))]


def update_session_status(conn, session_id, status):
    with transaction(conn):
        conn.execute('UPDATE sessions SET status=?, updated_at=? WHERE id=?',
                     (status, _now(), session_id))
    from app import push   # TB-29: every status write passes here; a notice only when a phone subscribed
    push.on_session_status(conn, session_id, status)


def update_step_status(conn, step_id, status, result_text=None, provider_session_id=None,
                       usage_info=None, stop_reason=None, is_error=None, terminal_reason=None):
    with transaction(conn):
        fields, values = ['status=?'], [status]
        if status == 'RUNNING':
            fields += ['started_at=?', 'completed_at=NULL']
            values.append(_now())
        elif status in ('COMPLETED', 'FAILED', 'CANCELLED'):
            fields.append('completed_at=?')
            values.append(_now())
        for name, value in [('result_text', result_text), ('provider_session_id', provider_session_id),
                            ('usage_info', usage_info), ('stop_reason', stop_reason),
                            ('is_error', is_error), ('terminal_reason', terminal_reason)]:
            if value is not None:
                fields.append(name + '=?')
                values.append(value)
        conn.execute(f'UPDATE session_steps SET {", ".join(fields)} WHERE id=?', [*values, step_id])


def checkpoint(conn, session_id, step_id, kind, payload):
    with transaction(conn):
        conn.execute('INSERT INTO session_events(session_id, step_id, kind, payload, at) '
                     'VALUES (?, ?, ?, ?, ?)',
                     (session_id, step_id, kind, json.dumps(payload, ensure_ascii=False), _now()))
        return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def frozen_policy(conn, session_id):
    row = conn.execute('SELECT policy_json FROM session_policies WHERE session_id=?',(session_id,)).fetchone()
    if not row:
        raise ValueError('legacy work has no accepted policy; recovery required')
    return json.loads(row[0])


TRANSIENT_RETRY = 'transient_retry'


def begin_attempt(conn, aid, step_id, number, provider, profile, reason=None):
    with transaction(conn):
        generation = conn.execute('SELECT retry_generation FROM session_steps WHERE id=?',
                                  (step_id,)).fetchone()[0]
        # TB-43: a retry on the same provider chains to its latest attempt, so UNIQUE(step,provider,generation,no) holds
        prior = (conn.execute('SELECT id, continuation_no FROM session_attempts '
                              'WHERE step_id=? AND provider=? AND retry_generation=? '
                              'ORDER BY continuation_no DESC LIMIT 1',
                              (step_id, provider, generation)).fetchone()
                 if reason == TRANSIENT_RETRY else None)
        if not prior:   # the pre-TB-43 insert, unchanged (older schemas in migration tests have no continuation columns)
            conn.execute('INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,handover_reason,retry_generation) '
                         "VALUES (?,?,?,?,?,?,'RUNNING',?,?)",(aid,step_id,number,provider,profile,_now(),reason,generation))
            return
        conn.execute('INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,'
                     'handover_reason,retry_generation,continuation_no,continuation_of) '
                     "VALUES (?,?,?,?,?,?,'RUNNING',?,?,?,?)",
                     (aid,step_id,number,provider,profile,_now(),reason,generation,prior['continuation_no'] + 1,prior['id']))


FOLLOWUP = 'command_denied_followup'


def continuation_count(conn, step_id):
    """Context continuations only; a K-020 follow-up or transient retry is not one and does not use that budget."""
    return conn.execute('SELECT count(*) FROM session_attempts WHERE step_id=? AND continuation_no>0 '
                        'AND handover_reason IS NOT ? AND handover_reason IS NOT ?',
                        (step_id, FOLLOWUP, TRANSIENT_RETRY)).fetchone()[0]


def prepare_followup(conn, aid, parent_id, step_id, provider, profile):
    """K-020: the single same-conversation follow-up after an agy command denial. It is its own
    attempt (own process owner), chained to the parent like a continuation so the unique keys hold."""
    from app.provider_policy import kind
    with transaction(conn):
        parent = conn.execute('SELECT * FROM session_attempts WHERE id=?', (parent_id,)).fetchone()
        step = conn.execute('SELECT * FROM session_steps WHERE id=?', (step_id,)).fetchone()
        done = conn.execute('SELECT 1 FROM session_attempts WHERE step_id=? AND retry_generation=? AND handover_reason=?',
                            (step_id, step['retry_generation'] if step else -1, FOLLOWUP)).fetchone()
        if (not parent or not step or done or kind(provider) != 'agy' or parent['step_id'] != step_id
                or parent['provider'] != provider or parent['profile'] != profile or step['status'] != 'RUNNING'
                or parent['retry_generation'] != step['retry_generation'] or parent['error_code'] != 'COMMAND_DENIED'
                or not parent['ended_at'] or not parent['provider_session_id'] or not attempt_clean(conn, parent_id)):
            raise ValueError('follow-up not authorized')
        number = conn.execute('SELECT max(attempt_no)+1 FROM session_attempts WHERE step_id=?', (step_id,)).fetchone()[0]
        conn.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,handover_reason,"
                     "continuation_no,continuation_of,retry_generation) VALUES (?,?,?,?,?,?,'RUNNING',?,?,?,?)",
                     (aid, step_id, number, provider, profile, _now(), FOLLOWUP, parent['continuation_no'] + 1,
                      parent_id, step['retry_generation']))
        checkpoint(conn, step['session_id'], step_id, 'command_denied_followup',
                   {'from_attempt_id': parent_id, 'to_attempt_id': aid, 'provider': provider,
                    'provider_session_id': parent['provider_session_id']})


def prepare_continuation(conn, aid, parent_id, step_id, provider, profile, digest):
    """Guard parent and frozen authority, then persist preparation atomically."""
    with transaction(conn):
        parent = conn.execute('SELECT * FROM session_attempts WHERE id=?', (parent_id,)).fetchone()
        step = conn.execute('SELECT s.*,r.role,r.profile FROM session_steps s JOIN step_roles r ON r.step_id=s.id WHERE s.id=?', (step_id,)).fetchone()
        if (not parent or not step or parent['step_id'] != step_id or
                parent['provider'] != provider or parent['profile'] != profile or
                step['profile'] != profile or step['status'] != 'RUNNING' or
                parent['retry_generation'] != step['retry_generation'] or
                parent['status'] != 'CONTEXT_EXHAUSTED' or parent['error_code'] != 'CONTEXT_EXHAUSTED' or
                not parent['ended_at'] or not attempt_clean(conn, parent_id)):
            raise ValueError('invalid continuation parent')
        policy = frozen_policy(conn, step['session_id'])
        limit = policy.get('max_context_continuations', 0)
        used = continuation_count(conn, step_id)
        from app.provider_policy import kind
        if (kind(provider) != 'codex' or kind(provider) not in policy.get('context_continuation_providers', []) or
                used >= limit or {'provider':provider,'profile':profile} not in policy['roles'][step['role']]):
            raise ValueError('continuation not authorized')
        number = conn.execute('SELECT max(attempt_no)+1 FROM session_attempts WHERE step_id=?', (step_id,)).fetchone()[0]
        no = parent['continuation_no']+1
        conn.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,continuation_no,continuation_of,retry_generation) VALUES (?,?,?,?,?,?,'RUNNING',?,?,?)",
                     (aid,step_id,number,provider,profile,_now(),no,parent_id,step['retry_generation']))
        ref = checkpoint(conn, step['session_id'], step_id, 'context_digest', digest)
        checkpoint(conn, step['session_id'], step_id, 'context_continuation',
                   {'from_attempt_id':parent_id,'to_attempt_id':aid,'provider':provider,
                    'role':step['role'],'profile':profile,'continuation_no':no,
                    'step_continuations_used':used+1,'max_context_continuations':limit,
                    'digest_ref':f'session_events:{ref}','reason':'CONTEXT_EXHAUSTED'})
        return aid


def end_attempt(conn, aid, status, result=None, reason=None):
    with transaction(conn):
        conn.execute('UPDATE session_attempts SET ended_at=?,status=?,error_code=?,terminal_reason=?,'
                     'provider_session_id=COALESCE(?,provider_session_id),result_text=?,exit_code=?,provider_error=? WHERE id=? AND ended_at IS NULL',
                     (_now(),status,result.error_code if result else None,
                      result.terminal_reason if result else reason,result.provider_session_id if result else None,
                      result.final_text if result else None,result.exit_code if result else None,
                      json.dumps(result.provider_error) if result else None,aid))


def record_owner(conn, aid, owner, stopped=False):
    with transaction(conn):
        if stopped:
            changed = conn.execute("UPDATE process_owners SET status='STOPPED',stopped_at=? WHERE attempt_id=? "
                                   'AND pid=? AND start_time=? AND pgid=? AND boot_time=?',
                                   (_now(),aid,owner['pid'],owner['start_time'],owner['pgid'],owner['boot_time'])).rowcount
            if changed != 1:
                raise ValueError('process ownership mismatch')
        else:
            conn.execute('INSERT INTO process_owners VALUES (?,?,?,?,?,?,?,NULL)',
                         (aid,owner['pid'],owner['pgid'],owner['start_time'],owner['boot_time'],'RUNNING',_now()))


def attempt_clean(conn, aid):
    row=conn.execute('SELECT status FROM process_owners WHERE attempt_id=?',(aid,)).fetchone()
    return row is not None and row[0]=='STOPPED'


def save_snapshot(conn, sid, aid, kind, data):
    from uuid import uuid4
    key='snap_'+uuid4().hex
    with transaction(conn):
        conn.execute('INSERT INTO workspace_snapshots VALUES (?,?,?,?,?,?)',
                     (key,sid,aid,kind,_now(),json.dumps(data,sort_keys=True)))
    return key


def unstopped_owners(conn, session_id):
    """Recorded process groups for this session's attempts never confirmed STOPPED."""
    return [dict(r) for r in conn.execute(
        'SELECT o.attempt_id, a.step_id, o.pid, o.pgid, o.start_time, o.boot_time '
        'FROM process_owners o JOIN session_attempts a ON a.id=o.attempt_id '
        'JOIN session_steps s ON s.id=a.step_id '
        "WHERE s.session_id=? AND o.status!='STOPPED' ORDER BY o.recorded_at", (session_id,))]


def trusted_members(conn, attempt_id):
    """Member identities captured in the attempt's process_group_snapshot checkpoint."""
    for row in conn.execute("SELECT payload FROM session_events "
                            "WHERE kind='process_group_snapshot' ORDER BY seq DESC"):
        data = json.loads(row['payload'])
        if data.get('attempt_id') == attempt_id:
            return data.get('ownership', {}).get('members', []) or []
    return []


def complete_recovery(conn, session_id, proofs):
    """All owned groups proven gone: persist proof, mark owners STOPPED, release lock, FAIL.

    Security order is the caller's: reap+verify happens first; this only runs once every
    proof is ok. Terminal is FAILED + terminal_reason='recovered_by_human' (K1).
    """
    with transaction(conn):
        session = get_session(conn, session_id)
        if not session or session['status'] != 'RECOVERY_REQUIRED':
            raise ValueError('session is not awaiting recovery')
        for p in proofs:
            checkpoint(conn, session_id, p['step_id'], 'recovery', p)
            conn.execute("UPDATE process_owners SET status='STOPPED', stopped_at=? "
                         "WHERE attempt_id=? AND status!='STOPPED'", (_now(), p['attempt_id']))
        # A crash leaves the in-flight attempt open and its step RUNNING; close both so a
        # terminal session never shows running work. PENDING steps never started: untouched.
        now = _now()
        conn.execute("UPDATE session_attempts SET status='FAILED', ended_at=?, "
                     "terminal_reason='recovered_by_human' WHERE ended_at IS NULL AND step_id IN "
                     "(SELECT id FROM session_steps WHERE session_id=?)", (now, session_id))
        conn.execute("UPDATE session_steps SET status='FAILED', is_error=1, "
                     "terminal_reason='recovered_by_human', completed_at=? "
                     "WHERE session_id=? AND status IN ('RUNNING','FAILED')", (now, session_id))
        release_lock(conn, session['workspace_path'], session_id)
        conn.execute("UPDATE sessions SET status='FAILED', updated_at=? WHERE id=?",
                     (_now(), session_id))


def retry_session(conn, session_id):
    """Human retry: reserve the workspace via QUEUED, advance failed-step generations.

    Root attempts are unique within a generation. Context allowance spans all
    generations; completed steps and attempt/ownership evidence are never reset.
    """
    with transaction(conn):
        session = get_session(conn, session_id)
        if not session or session['status'] not in ('WAITING_CONTEXT', 'WAITING_CAPACITY', 'WAITING_AUTH', 'WAITING_HUMAN'):
            raise RetryNotAllowed('session_not_retryable', 'session is not retryable')
        busy = conn.execute("SELECT 1 FROM sessions WHERE workspace_path=? AND id!=? "
                            "AND status IN ('QUEUED','RUNNING','RECOVERY_REQUIRED')",
                            (session['workspace_path'], session_id)).fetchone()
        if busy or conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?',
                                (session['workspace_path'],)).fetchone():
            raise RetryNotAllowed('workspace_busy', 'workspace still reserved or locked; recover first')
        if unstopped_owners(conn, session_id) or conn.execute(
                'SELECT 1 FROM session_attempts a JOIN session_steps s ON s.id=a.step_id '
                'WHERE s.session_id=? AND a.ended_at IS NULL', (session_id,)).fetchone():
            raise RetryNotAllowed('recovery_required', 'attempt cleanup is unresolved; recover first')
        failed = [dict(r) for r in conn.execute(
            "SELECT * FROM session_steps WHERE session_id=? AND status='FAILED'", (session_id,))]
        if not failed:
            raise RetryNotAllowed('no_failed_step', 'session has no failed step to retry')
        if session['status'] == 'WAITING_CONTEXT':
            policy = frozen_policy(conn, session_id)
            limit = policy.get('max_context_continuations', 0)
            if any(continuation_count(conn, step['id']) >= limit for step in failed):
                raise RetryNotAllowed('context_budget_exhausted', 'accepted context continuation budget is exhausted')
        changed = conn.execute("UPDATE sessions SET status='QUEUED', updated_at=? WHERE id=? AND status=?",
                               (_now(), session_id, session['status'])).rowcount
        if changed != 1:
            raise RetryNotAllowed('session_not_retryable', 'session state changed')
        # F3-04: a human retry supersedes a pending automatic one (a FIRED plan is the scheduler's own call).
        conn.execute("UPDATE retry_plans SET status='CANCELLED', closed_reason='human_retry', cancelled_at=? "
                     "WHERE session_id=? AND status='PENDING'", (_now(), session_id))
        for step in failed:
            changed = conn.execute("UPDATE session_steps SET status='PENDING', retry_generation=retry_generation+1, "
                                   "is_error=NULL, terminal_reason=NULL, result_text=NULL, provider_session_id=NULL, "
                                   "usage_info=NULL, stop_reason=NULL, completed_at=NULL "
                                   "WHERE id=? AND status='FAILED' AND retry_generation=?",
                                   (step['id'], step['retry_generation'])).rowcount
            if changed != 1:
                raise RetryNotAllowed('session_not_retryable', 'step state changed')
            checkpoint(conn, session_id, step['id'], 'human_retry',
                       {'from_state': session['status'], 'from_generation': step['retry_generation'],
                        'retry_generation': step['retry_generation'] + 1,
                        'context_continuations_used': continuation_count(conn, step['id'])})


def skip_exhausted(conn, session_id, step_id, chain, role, minutes):
    """K-067: index of the first chain entry to try. Leading entries whose provider got a verified QUOTA_EXCEEDED in
    the last `minutes` (and no successful attempt since) are skipped and recorded; the last entry is never skipped.
    ponytail: unindexed scans of session_attempts (hundreds of rows, once per step); add an index on
    (provider, error_code, ended_at) in a schema migration if it grows to many thousands. A run that succeeds on the
    same provider in the same instant can still be skipped once; the next chain entry then does the step."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    index = 0
    while index < len(chain) - 1:
        provider = chain[index]['provider']
        hit = conn.execute("SELECT MAX(ended_at) FROM session_attempts WHERE provider=? AND error_code='QUOTA_EXCEEDED' "
                           "AND ended_at>=?", (provider, since)).fetchone()[0]
        if not hit or conn.execute("SELECT 1 FROM session_attempts WHERE provider=? AND status='COMPLETED' AND ended_at>?",
                                   (provider, hit)).fetchone():
            break
        checkpoint(conn, session_id, step_id, 'handoff',
                   {'from': provider, 'to': chain[index + 1]['provider'], 'role': role, 'reason': 'QUOTA_RECENT', 'quota_at': hit})
        index += 1
    return index
