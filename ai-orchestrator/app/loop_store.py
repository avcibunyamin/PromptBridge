"""F3-02 turn loop persistence: cursor, turns, decisions, active-time ledger, pause.

A loop session is one with a `session_loop_state` row (execution_kind='loop'); every other
session is the legacy three-step flow and never touches these tables. Step indexes only
order steps here; meaning comes from `session_turns` (turn + role).
"""
import hashlib
import json
from uuid import uuid4
from app.handoff import differences
from app import runner_store as repo
from app.store import transaction, _now

HEARTBEAT_MS = 10_000
OUTCOMES = ('verified', 'reviewer_blocked', 'review_invalid', 'no_progress', 'turn_limit', 'budget_exhausted')


class LoopStateChanged(ValueError):
    """A CAS on the loop cursor lost: someone else already decided this turn."""


def state(conn, session_id):
    row = conn.execute('SELECT * FROM session_loop_state WHERE session_id=?', (session_id,)).fetchone()
    return dict(row) if row else None


def init(conn, session_id, budget, author_agent, reviewer_agent, author_prompt, review_prompt):
    """Loop row + turn 1 (author and reviewer steps). Called inside the dispatch transaction."""
    with transaction(conn):
        conn.execute('INSERT INTO session_loop_state(session_id, execution_kind, max_turns, max_active_ms, '
                     "current_turn, phase, updated_at) VALUES (?, 'loop', ?, ?, 1, 'author', ?)",
                     (session_id, budget['max_turns'], budget['max_active_minutes'] * 60_000, _now()))
        _add_turn(conn, session_id, 1, author_agent, reviewer_agent, author_prompt, review_prompt)


def _add_turn(conn, session_id, turn_no, author_agent, reviewer_agent, author_prompt, review_prompt):
    base = conn.execute('SELECT coalesce(max(step_index), -1) + 1 FROM session_steps WHERE session_id=?',
                        (session_id,)).fetchone()[0]
    author_id, reviewer_id = f'{session_id}_t{turn_no}a', f'{session_id}_t{turn_no}r'
    repo.add_session_step(conn, author_id, session_id, base, author_agent, author_prompt, role='author')
    repo.add_session_step(conn, reviewer_id, session_id, base + 1, reviewer_agent, review_prompt, role='reviewer')
    conn.execute('INSERT INTO session_turns(session_id, turn_no, author_step_id, reviewer_step_id, created_at) '
                 'VALUES (?, ?, ?, ?, ?)', (session_id, turn_no, author_id, reviewer_id, _now()))


def turns(conn, session_id):
    return [dict(r) for r in conn.execute('SELECT * FROM session_turns WHERE session_id=? ORDER BY turn_no',
                                          (session_id,))]


def turn_of_step(conn, step_id):
    """(turn_no, role) for a loop step, or None for a legacy step."""
    row = conn.execute('SELECT turn_no, CASE WHEN author_step_id=? THEN \'author\' ELSE \'reviewer\' END '
                       'FROM session_turns WHERE author_step_id=? OR reviewer_step_id=?',
                       (step_id, step_id, step_id)).fetchone()
    return (row[0], row[1]) if row else None


def next_step(conn, session_id):
    """The one step the cursor points at, or None when the loop is done/paused."""
    loop = state(conn, session_id)
    if not loop or loop['phase'] == 'done':
        return None
    column = 'author_step_id' if loop['phase'] == 'author' else 'reviewer_step_id'
    step_id = conn.execute(f'SELECT {column} FROM session_turns WHERE session_id=? AND turn_no=?',
                           (session_id, loop['current_turn'])).fetchone()[0]
    return next(s for s in repo.get_session_steps(conn, session_id) if s['id'] == step_id)


def mark_author_snapshot(conn, session_id, turn_no, which, snapshot_id):
    """First author attempt of a turn records `before`; the end of the author step records `after`."""
    column = 'snapshot_before_id' if which == 'before' else 'snapshot_after_id'
    guard = ' AND snapshot_before_id IS NULL' if which == 'before' else ''
    with transaction(conn):
        conn.execute(f'UPDATE session_turns SET {column}=? WHERE session_id=? AND turn_no=?{guard}',
                     (snapshot_id, session_id, turn_no))


def author_done(conn, session_id, turn_no):
    with transaction(conn):
        if conn.execute("UPDATE session_loop_state SET phase='review', updated_at=? WHERE session_id=? "
                        "AND current_turn=? AND phase='author'", (_now(), session_id, turn_no)).rowcount != 1:
            raise LoopStateChanged('author phase already closed')


def _snapshot(conn, snapshot_id):
    row = snapshot_id and conn.execute('SELECT snapshot_json FROM workspace_snapshots WHERE id=?',
                                       (snapshot_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _unchanged(before, after):
    """True only for two complete, same-scope snapshots with equal file hashes; else unknown/changed."""
    if not before or not after or not before.get('complete') or not after.get('complete'):
        return False
    return before.get('scope') == after.get('scope') and before.get('files') == after.get('files')


def close_turn(conn, session_id, turn_no, *, verdict, findings, findings_hash, invalid_code,
               next_prompts):
    """Record the review and decide: stop with an outcome, or open turn k+1 — one transaction, CAS.

    verdict: 'pass' | 'changes_requested' | 'blocked' | 'invalid'.
    next_prompts: (author_agent, reviewer_agent, author_prompt, review_prompt) for turn k+1.
    """
    with transaction(conn):
        loop = state(conn, session_id)
        if not loop or loop['current_turn'] != turn_no or loop['phase'] != 'review':
            raise LoopStateChanged('turn already decided')
        turn = conn.execute('SELECT * FROM session_turns WHERE session_id=? AND turn_no=?',
                            (session_id, turn_no)).fetchone()
        outcome = {'pass': 'verified', 'blocked': 'reviewer_blocked', 'invalid': 'review_invalid'}.get(verdict)
        if verdict == 'changes_requested':
            previous = conn.execute('SELECT findings_hash FROM session_turns WHERE session_id=? AND turn_no=?',
                                    (session_id, turn_no - 1)).fetchone()
            same_findings = bool(previous and previous[0] == findings_hash)
            if same_findings and _unchanged(_snapshot(conn, turn['snapshot_before_id']),
                                            _snapshot(conn, turn['snapshot_after_id'])):
                outcome = 'no_progress'
            elif turn_no >= loop['max_turns']:
                outcome = 'turn_limit'
        conn.execute('UPDATE session_turns SET verdict=?, findings_json=?, findings_hash=?, invalid_code=?, '
                     'ended_reason=?, closed_at=? WHERE session_id=? AND turn_no=?',
                     (verdict, json.dumps(findings, ensure_ascii=False) if findings is not None else None,
                      findings_hash, invalid_code, outcome or 'next_turn', _now(), session_id, turn_no))
        if outcome:
            conn.execute("UPDATE session_loop_state SET phase='done', outcome=?, outcome_reason=?, updated_at=? "
                         "WHERE session_id=?", (outcome, invalid_code or verdict, _now(), session_id))
        else:
            _add_turn(conn, session_id, turn_no + 1, *next_prompts)
            conn.execute("UPDATE session_loop_state SET current_turn=?, phase='author', updated_at=? "
                         "WHERE session_id=?", (turn_no + 1, _now(), session_id))
        repo.checkpoint(conn, session_id, None, 'review_parsed' if verdict != 'invalid' else 'review_invalid',
                        {'turn': turn_no, 'verdict': verdict, 'findings': len(findings or []),
                         'code': invalid_code})
        if outcome:
            repo.checkpoint(conn, session_id, None, 'loop_stopped', {'turn': turn_no, 'outcome': outcome})
        else:
            repo.checkpoint(conn, session_id, None, 'turn_started', {'turn': turn_no + 1})
        return {'stop': bool(outcome), 'outcome': outcome}


def stop_budget(conn, session_id, author_unreviewed):
    with transaction(conn):
        cur = conn.execute("UPDATE session_loop_state SET phase='done', outcome='budget_exhausted', "
                           "outcome_reason='active_time', last_author_unreviewed=?, updated_at=? WHERE session_id=? "
                           "AND phase!='done'", (int(author_unreviewed), _now(), session_id))
        if cur.rowcount != 1:
            return False
        repo.checkpoint(conn, session_id, None, 'loop_stopped', {'outcome': 'budget_exhausted',
                                                                 'last_author_unreviewed': bool(author_unreviewed)})
        return True


# ── active-time ledger ──────────────────────────────────────────────────────────

def open_segment(conn, session_id, step_id):
    segment = 'seg_' + uuid4().hex
    with transaction(conn):
        conn.execute('INSERT INTO session_time_segments(segment_id, session_id, step_id, opened_at) VALUES (?,?,?,?)',
                     (segment, session_id, step_id, _now()))
    return segment


def heartbeat(conn, segment, elapsed_ms):
    with transaction(conn):
        conn.execute('UPDATE session_time_segments SET elapsed_ms=max(elapsed_ms, ?) WHERE segment_id=? AND settled=0',
                     (int(elapsed_ms), segment))


def settle(conn, segment, elapsed_ms, *, estimated=False):
    """Exactly-once: a second settle (double callback, crash replay) is a no-op."""
    with transaction(conn):
        return conn.execute('UPDATE session_time_segments SET elapsed_ms=max(elapsed_ms, ?), settled=1, estimated=? '
                            'WHERE segment_id=? AND settled=0', (int(elapsed_ms), int(estimated), segment)).rowcount == 1


def used_ms(conn, session_id):
    return conn.execute('SELECT coalesce(sum(elapsed_ms), 0) FROM session_time_segments WHERE session_id=?',
                        (session_id,)).fetchone()[0]


def settle_abandoned(conn):
    """Startup only (no runner alive): close open segments with a conservative upper bound."""
    with transaction(conn):
        rows = [dict(r) for r in conn.execute('SELECT * FROM session_time_segments WHERE settled=0')]
        for row in rows:
            conn.execute('UPDATE session_time_segments SET elapsed_ms=?, settled=1, estimated=1 WHERE segment_id=? AND settled=0',
                         (row['elapsed_ms'] + HEARTBEAT_MS, row['segment_id']))
            repo.checkpoint(conn, row['session_id'], row['step_id'], 'time_estimated',
                            {'segment_id': row['segment_id'], 'elapsed_ms': row['elapsed_ms'] + HEARTBEAT_MS})
    return len(rows)


# ── pause ───────────────────────────────────────────────────────────────────────

def request_pause(conn, session_id):
    with transaction(conn):
        changed = conn.execute("UPDATE session_loop_state SET pause_requested_at=?, updated_at=? WHERE session_id=? "
                               "AND phase!='done' AND pause_requested_at IS NULL AND "
                               "(SELECT status FROM sessions WHERE id=?) IN ('QUEUED','RUNNING')",
                               (_now(), _now(), session_id, session_id)).rowcount
        if changed:
            repo.checkpoint(conn, session_id, None, 'pause_requested', {})
        return bool(changed)


def apply_pause(conn, session_id, snapshot_id):
    """Step boundary: RUNNING → PAUSED, new revision, the snapshot the resume compares against."""
    with transaction(conn):
        conn.execute("UPDATE session_loop_state SET pause_requested_at=NULL, pause_revision=pause_revision+1, "
                     "pause_snapshot_id=?, resume_digest=NULL, updated_at=? WHERE session_id=?",
                     (snapshot_id, _now(), session_id))
        conn.execute("UPDATE sessions SET status='PAUSED', updated_at=? WHERE id=? AND status='RUNNING'",
                     (_now(), session_id))
        loop = state(conn, session_id)
        repo.checkpoint(conn, session_id, None, 'paused', {'pause_revision': loop['pause_revision'],
                                                           'turn': loop['current_turn'], 'phase': loop['phase']})


def clear_pause_request(conn, session_id, reason):
    with transaction(conn):
        if conn.execute('UPDATE session_loop_state SET pause_requested_at=NULL WHERE session_id=? '
                        'AND pause_requested_at IS NOT NULL', (session_id,)).rowcount:
            repo.checkpoint(conn, session_id, None, 'pause_superseded', {'reason': reason})


# ── resume ──────────────────────────────────────────────────────────────────────

def changes_since_pause(conn, pause_snapshot_id, current):
    """What changed in the folder while paused. `unknown` = a side was incomplete or scoped differently."""
    before = _snapshot(conn, pause_snapshot_id)
    comparable = bool(before and before.get('complete') and current.get('complete')
                      and before.get('scope') == current.get('scope'))
    return {'differences': differences(before, current) if before else None, 'comparable': comparable,
            'unknown': sorted(set((before or {}).get('unknown', [])) | set(current.get('unknown', [])))}


def has_changes(changes):
    return not changes['comparable'] or bool(changes['differences'])


def change_digest(conn, pause_snapshot_id, current):
    """Binds a resume confirmation to exactly the changes the user saw."""
    data = changes_since_pause(conn, pause_snapshot_id, current)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def accept_resume(conn, session_id, pause_revision, digest, fresh):
    """PAUSED → QUEUED (reserves the folder; the runner's claim takes the lock and re-checks)."""
    with transaction(conn):
        loop = state(conn, session_id)
        if not loop or loop['pause_revision'] != pause_revision:
            raise LoopStateChanged('stale_pause')
        # Same transaction as the status change: another run cannot reserve the folder in between.
        workspace = conn.execute('SELECT workspace_path FROM sessions WHERE id=?', (session_id,)).fetchone()
        if workspace and (conn.execute("SELECT 1 FROM sessions WHERE workspace_path=? AND id!=? AND status IN "
                                       "('QUEUED','RUNNING','RECOVERY_REQUIRED')",
                                       (workspace[0], session_id)).fetchone()
                          or conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?',
                                          (workspace[0],)).fetchone()):
            raise LoopStateChanged('workspace_busy')   # stays PAUSED
        if conn.execute("UPDATE sessions SET status='QUEUED', updated_at=? WHERE id=? AND status='PAUSED'",
                        (_now(), session_id)).rowcount != 1:
            raise LoopStateChanged('not_paused')
        conn.execute('UPDATE session_loop_state SET resume_digest=?, resume_fresh=?, updated_at=? WHERE session_id=?',
                     (digest, int(fresh), _now(), session_id))
        repo.checkpoint(conn, session_id, None, 'resumed', {'pause_revision': pause_revision, 'folder_changed': bool(fresh)})


def consume_resume(conn, session_id):
    with transaction(conn):
        fresh = bool(state(conn, session_id)['resume_fresh'])
        conn.execute('UPDATE session_loop_state SET resume_digest=NULL, resume_fresh=0, updated_at=? WHERE session_id=?',
                     (_now(), session_id))
    return fresh
