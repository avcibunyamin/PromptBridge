"""F3-04: opt-in, bounded quota wait with automatic retry (K-010, K-012).

One `retry_plans` row per session carries the whole wait budget: the deadline is fixed at the
first quota and attempts accumulate across re-quotas, so waiting always ends. The runner enters a
plan at the last quota of a chain (`enter`), the scheduler claims due rows with a DB CAS (`claim`)
and fires them through the human-retry path (`runner_store.retry_session`) so every existing guard
(workspace lock, unresolved cleanup, failed steps) still applies. A folder that changed while
waiting stops at WAITING_HUMAN. Only QUOTA_EXCEEDED ever creates a plan.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from uuid import uuid4
from app import store, runner_store as repo
from app.handoff import snapshot, differences
from app.store import transaction

log = logging.getLogger(__name__)

BACKOFF_BASE = timedelta(minutes=15)
BACKOFF_CAP = timedelta(hours=4)
BUSY_DELAY = timedelta(minutes=15)      # folder held by another run: try again later, not an attempt
RESTART_DELAY = timedelta(minutes=5)    # a plan fired by a server that then stopped


class WaitError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utcnow():
    return datetime.now(timezone.utc)


def iso(moment):
    # One fixed-width UTC form, so plan timestamps compare correctly as strings in SQL.
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds')


def backoff(attempts):
    return min(BACKOFF_BASE * (2 ** attempts), BACKOFF_CAP)


def _next(now, delay, deadline):
    return min(iso(now + delay), deadline)   # never past the deadline, so expiry is not late


def _close(conn, plan_id, expected, status, reason, now):
    changed = conn.execute(
        "UPDATE retry_plans SET status=?, closed_reason=?, cancelled_at=? WHERE id=? AND status=?",
        (status, reason, iso(now) if status == 'CANCELLED' else None, plan_id, expected)).rowcount
    if changed != 1:
        raise WaitError('plan_changed', 'wait plan changed concurrently')


def _fail_session(conn, sid, plan, reason, now):
    """EXPIRED plan; the session fails only if it is still waiting (a human retry may have started it)."""
    _close(conn, plan['id'], plan['status'], 'EXPIRED', reason, now)
    conn.execute("UPDATE sessions SET status='FAILED', updated_at=? WHERE id=? AND status='WAITING_CAPACITY'",
                 (store._now(), sid))
    repo.checkpoint(conn, sid, plan['step_id'], 'wait_expired',
                    {'plan_id': plan['id'], 'reason': reason, 'attempts_so_far': plan['attempts_so_far'],
                     'max_attempts': plan['max_attempts'], 'deadline': plan['deadline']})


def _exhausted(plan, now):
    if plan['attempts_so_far'] >= plan['max_attempts']:
        return 'max_retries_exceeded'
    if iso(now) >= plan['deadline']:
        return 'retry_deadline_exceeded'
    return None


def enter(conn, sid, step_id, wait, snap, now=None):
    """At the last quota of a chain, inside the runner's transaction; returns the session status.

    The folder snapshot taken now (lock still held) is the new comparison base, so changes made by
    the attempt itself never count as outside changes at the next fire."""
    now = now or utcnow()
    snap_id = repo.save_snapshot(conn, sid, None, 'wait_start', snap)
    plan = conn.execute("SELECT * FROM retry_plans WHERE session_id=? AND status='FIRED'", (sid,)).fetchone()
    if plan:   # a fired retry hit the quota again: same row, same budget
        plan = dict(plan)
        reason = _exhausted(plan, now)
        if reason:
            _fail_session(conn, sid, plan, reason, now)
            return 'FAILED'
        next_at = _next(now, backoff(plan['attempts_so_far']), plan['deadline'])
        conn.execute("UPDATE retry_plans SET status='PENDING', step_id=?, baseline_snapshot_id=?, next_retry_at=? "
                     "WHERE id=? AND status='FIRED'", (step_id, snap_id, next_at, plan['id']))
        pid = plan['id']
    else:
        pid, deadline = 'rp_' + uuid4().hex, iso(now + timedelta(hours=wait['deadline_hours']))
        next_at = _next(now, backoff(0), deadline)
        conn.execute('INSERT INTO retry_plans(id,session_id,step_id,baseline_snapshot_id,created_at,first_quota_at,'
                     "next_retry_at,deadline,max_attempts,reason,status) VALUES (?,?,?,?,?,?,?,?,?,'QUOTA_EXCEEDED','PENDING')",
                     (pid, sid, step_id, snap_id, iso(now), iso(now), next_at, deadline, wait['max_attempts']))
    repo.checkpoint(conn, sid, step_id, 'wait_planned', {'plan_id': pid, 'next_retry_at': next_at,
                                                          'renewed': plan is not None})
    return 'WAITING_CAPACITY'


def claim(conn, plan_id, now):
    """CAS PENDING → FIRED (or EXPIRED); two schedulers can never both fire one plan."""
    with transaction(conn):
        plan = conn.execute("SELECT * FROM retry_plans WHERE id=? AND status='PENDING'", (plan_id,)).fetchone()
        if not plan:
            return None
        plan = dict(plan)
        session = repo.get_session(conn, plan['session_id'])
        if not session or session['status'] != 'WAITING_CAPACITY':
            _close(conn, plan_id, 'PENDING', 'CANCELLED', 'session_not_waiting', now)
            return None
        reason = _exhausted(plan, now)
        if reason:
            _fail_session(conn, plan['session_id'], plan, reason, now)
            return 'expired'
        conn.execute("UPDATE retry_plans SET status='FIRED', fired_at=? WHERE id=? AND status='PENDING'",
                     (iso(now), plan_id))
        return 'fired'


def _busy(conn, workspace, sid):
    return conn.execute("SELECT 1 FROM sessions WHERE workspace_path=? AND id!=? AND status IN "
                        "('QUEUED','RUNNING','RECOVERY_REQUIRED')", (workspace, sid)).fetchone() or \
        conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?', (workspace,)).fetchone()


def _reschedule(conn, plan, now, reason):
    with transaction(conn):
        plan = dict(conn.execute('SELECT * FROM retry_plans WHERE id=?', (plan['id'],)).fetchone())
        if plan['status'] != 'FIRED':
            return 'cancelled'
        if iso(now) >= plan['deadline']:
            _fail_session(conn, plan['session_id'], plan, 'retry_deadline_exceeded', now)
            return 'expired'
        next_at = _next(now, BUSY_DELAY, plan['deadline'])
        conn.execute("UPDATE retry_plans SET status='PENDING', next_retry_at=? WHERE id=? AND status='FIRED'",
                     (next_at, plan['id']))
        repo.checkpoint(conn, plan['session_id'], plan['step_id'], 'wait_rescheduled',
                        {'plan_id': plan['id'], 'reason': reason, 'next_retry_at': next_at})
        return 'rescheduled'


def fire(conn, plan, excluded, now):
    """A claimed (FIRED) plan: folder check, then one atomic attempt count + human-retry transition.

    Returns 'queued' (the caller starts the run), 'rescheduled', 'waiting_human', 'cancelled' or 'expired'."""
    sid = plan['session_id']
    workspace = repo.get_session(conn, sid)['workspace_path']
    if _busy(conn, workspace, sid):
        return _reschedule(conn, plan, now, 'workspace_busy')
    base = json.loads(conn.execute('SELECT snapshot_json FROM workspace_snapshots WHERE id=?',
                                   (plan['baseline_snapshot_id'],)).fetchone()[0])
    current = snapshot(workspace, excluded=excluded)
    changes = differences(base, current)
    if changes or current['unknown'] != base['unknown']:
        with transaction(conn):
            _close(conn, plan['id'], 'FIRED', 'CANCELLED', 'workspace_changed', now)
            conn.execute("UPDATE sessions SET status='WAITING_HUMAN', updated_at=? WHERE id=? AND status='WAITING_CAPACITY'",
                         (store._now(), sid))
            repo.checkpoint(conn, sid, plan['step_id'], 'wait_workspace_changed',
                            {'plan_id': plan['id'], 'paths': [c['path'] for c in changes][:50],
                             'changed_count': len(changes), 'unknown': current['unknown'][:20]})
        return 'waiting_human'
    try:
        with transaction(conn):
            counted = conn.execute("UPDATE retry_plans SET attempts_so_far=attempts_so_far+1 WHERE id=? AND status='FIRED'",
                                   (plan['id'],)).rowcount
            if counted != 1:
                raise repo.RetryNotAllowed('session_not_retryable', 'wait plan changed')
            repo.retry_session(conn, sid)   # nested: a savepoint of this transaction
            repo.checkpoint(conn, sid, plan['step_id'], 'wait_fired',
                            {'plan_id': plan['id'], 'attempt': plan['attempts_so_far'] + 1,
                             'max_attempts': plan['max_attempts']})
    except repo.RetryNotAllowed as exc:
        if exc.code == 'workspace_busy':
            return _reschedule(conn, plan, now, 'workspace_busy')
        with transaction(conn):
            current_status = conn.execute('SELECT status FROM retry_plans WHERE id=?', (plan['id'],)).fetchone()[0]
            if current_status == 'FIRED':
                _close(conn, plan['id'], 'FIRED', 'CANCELLED', exc.code, now)
                repo.checkpoint(conn, sid, plan['step_id'], 'wait_cancelled', {'plan_id': plan['id'], 'reason': exc.code})
        return 'cancelled'
    return 'queued'


def cancel(conn, sid, now=None):
    """DELETE .../wait: a PENDING plan and its WAITING_CAPACITY session end CANCELLED together."""
    now = now or utcnow()
    with transaction(conn):
        plan = conn.execute("SELECT * FROM retry_plans WHERE session_id=? AND status='PENDING'", (sid,)).fetchone()
        if not plan:
            fired = conn.execute("SELECT 1 FROM retry_plans WHERE session_id=? AND status='FIRED'", (sid,)).fetchone()
            raise WaitError('wait_already_fired' if fired else 'no_wait_plan',
                            'the retry already started' if fired else 'no pending wait plan')
        if conn.execute("UPDATE sessions SET status='CANCELLED', updated_at=? WHERE id=? AND status='WAITING_CAPACITY'",
                        (store._now(), sid)).rowcount != 1:
            raise WaitError('session_not_waiting', 'session is no longer waiting')
        _close(conn, plan['id'], 'PENDING', 'CANCELLED', 'cancelled_by_user', now)
        repo.checkpoint(conn, sid, plan['step_id'], 'wait_cancelled', {'plan_id': plan['id'], 'reason': 'cancelled_by_user'})


def reconcile(conn, now=None):
    """Server start, after QUEUED/RUNNING were marked RECOVERY_REQUIRED (that rule is unchanged)."""
    now = now or utcnow()
    with transaction(conn):
        rows = [dict(r) for r in conn.execute(
            "SELECT p.*, s.status AS session_status FROM retry_plans p JOIN sessions s ON s.id=p.session_id "
            "WHERE p.status IN ('PENDING','FIRED')")]
        for plan in rows:
            waiting = plan['session_status'] == 'WAITING_CAPACITY'
            if plan['status'] == 'FIRED' and waiting:   # fired, but the retry never committed
                next_at = _next(now, RESTART_DELAY, plan['deadline'])
                conn.execute("UPDATE retry_plans SET status='PENDING', next_retry_at=? WHERE id=?", (next_at, plan['id']))
                repo.checkpoint(conn, plan['session_id'], plan['step_id'], 'wait_rescheduled',
                                {'plan_id': plan['id'], 'reason': 'server_restart', 'next_retry_at': next_at})
            elif (plan['status'] == 'FIRED' and plan['session_status'] == 'RECOVERY_REQUIRED') or \
                    (plan['status'] == 'PENDING' and not waiting):
                reason = 'recovery_required' if plan['session_status'] == 'RECOVERY_REQUIRED' else 'session_not_waiting'
                _close(conn, plan['id'], plan['status'], 'CANCELLED', reason, now)
                repo.checkpoint(conn, plan['session_id'], plan['step_id'], 'wait_cancelled',
                                {'plan_id': plan['id'], 'reason': reason})


def view(conn, sid):
    """The newest plan of a session for the API; `resets_at` stays null until verified (K-012)."""
    row = conn.execute('SELECT * FROM retry_plans WHERE session_id=? ORDER BY created_at DESC LIMIT 1', (sid,)).fetchone()
    if not row:
        return None
    keys = ('id', 'status', 'next_retry_at', 'deadline', 'first_quota_at', 'attempts_so_far', 'max_attempts',
            'reason', 'closed_reason')
    return dict({k: row[k] for k in keys}, resets_at=None)


class RetryScheduler:
    """One per server (the worker lifespan owns the runner lock); DB CAS guards every fire anyway."""

    def __init__(self, db_path, start_run, *, excluded=(), interval=60.0, clock=utcnow, on_change=None):
        self.db_path, self.start_run, self.excluded = db_path, start_run, list(excluded)
        self.interval, self.clock, self.on_change = interval, clock, on_change
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name='retry-scheduler')

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception('retry scheduler pass failed')
            await asyncio.sleep(self.interval)

    def tick(self):
        """One pass over due plans; returns [(session_id, outcome)]. Runs on the event loop thread."""
        now, results = self.clock(), []
        conn = store.connect(self.db_path)
        try:
            due = [r[0] for r in conn.execute(
                "SELECT id FROM retry_plans WHERE status='PENDING' AND (next_retry_at<=? OR deadline<=?) "
                'ORDER BY next_retry_at', (iso(now), iso(now)))]
            for plan_id in due:
                claimed = claim(conn, plan_id, now)
                if claimed is None:
                    continue
                plan = dict(conn.execute('SELECT * FROM retry_plans WHERE id=?', (plan_id,)).fetchone())
                outcome = fire(conn, plan, self.excluded, now) if claimed == 'fired' else claimed
                if outcome == 'queued':
                    self.start_run(plan['session_id'])
                results.append((plan['session_id'], outcome))
        finally:
            conn.close()
        if results and self.on_change:
            self.on_change()   # e.g. wake the work dispatcher so run-status events go out now
        return results
