"""Bounded work API, a single local worker owner, and replayable event stream."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, HTTPException, Request, Depends, Query, Response
from app.security import mutation_guard
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from app import store, runner_store as repo, loop_store, project_instructions, wait_plans
from app.handoff import snapshot
from app.runner import SessionRunner, snapshot_exclusions
from app.provider_policy import (load_policy, load_config, effective, allowed_for, team_problems,
                                 team_warnings, digest, CAPABILITIES, kind, account_of, identity_problem)
from app import provider_auth

router = APIRouter(prefix='/api/sessions')


@asynccontextmanager
async def worker_lifespan(app, db_path):
    # Avoid marking another live server's work as interrupted on startup.
    owner = open(Path(db_path).with_suffix('.runner.lock'), 'a')
    try:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn = store.connect(db_path)
        try:
            store.migrate(conn, worker_owned=True)
            with store.transaction(conn):
                conn.execute("UPDATE sessions SET status='RECOVERY_REQUIRED', updated_at=? "
                             "WHERE status IN ('QUEUED','RUNNING')", (store._now(),))
            # F3-02: no runner is alive yet, so open time segments belonged to a crashed worker —
            # close them with a conservative upper bound (never zero). PAUSED sessions stay PAUSED.
            loop_store.settle_abandoned(conn)
                # Retain workspace locks: a crashed server may have left children alive.
            wait_plans.reconcile(conn)   # F3-04: after the marking above; never auto-starts a session
        finally:
            conn.close()
        app.state.runner = SessionRunner(db_path)
        app.state.runner_jobs = {}
        app.state.runner_db = db_path
        # F3-01a trigger (3): only after lock + migration + RECOVERY_REQUIRED marking above.
        from app.work_dispatcher import WorkDispatcher
        app.state.dispatcher = WorkDispatcher(db_path, app.state.runner, app.state.runner_jobs)
        app.state.dispatcher.start()
        app.state.retry_scheduler = wait_plans.RetryScheduler(
            db_path, lambda sid: start_job(app, sid), excluded=snapshot_exclusions(db_path),
            on_change=app.state.dispatcher.wake)
        app.state.retry_scheduler.start()
        yield
    finally:
        scheduler = getattr(app.state, 'retry_scheduler', None)
        if scheduler:
            await scheduler.stop()
        dispatcher = getattr(app.state, 'dispatcher', None)
        if dispatcher:
            await dispatcher.stop()   # stop scheduling before cancelling running jobs
        jobs = getattr(app.state, 'runner_jobs', {})
        for task in list(jobs.values()):
            task.cancel()
        if jobs:
            await asyncio.gather(*list(jobs.values()), return_exceptions=True)
        owner.close()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_digest: str = Field(min_length=64, max_length=64)
    require_handover_approval: Literal[False] = False
    workspace_path: str = Field(min_length=1)
    task_prompt: str = Field(min_length=1, max_length=100000)
    mode: Literal['auto'] = 'auto'
    agents: tuple[Literal['codex'], Literal['claude'], Literal['codex']] = ('codex', 'claude', 'codex')


class RecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


from app.adapters.codex import codex_home


def _binary(name):
    from app.adapters.agy import agy_binary
    from app.adapters.claude import claude_binary
    from app.adapters.codex import codex_binary
    return {'claude': claude_binary, 'agy': agy_binary}.get(kind(name), codex_binary)()


def _home(db, name):
    """The account home a session check runs in (Claude has none)."""
    from app.adapters.agy import agy_home
    return agy_home(db, account_of(name)) if kind(name) == 'agy' else codex_home(db, account_of(name))


def installed_providers():
    """Side-effect free: resolves executables only. POST, replay, preview and legacy start use this."""
    installed = {}
    for name in CAPABILITIES:
        try:
            _binary(name); installed[name] = True
        except FileNotFoundError:
            installed[name] = False
    return installed


def _config(request):
    return load_config(Path(request.app.state.runner_db).parent / 'providers.yaml')


@router.get('/providers')
def provider_options(request: Request, check_auth: bool = False):
    if check_auth:
        mutation_guard(request)
    try:
        config = _config(request)   # one snapshot for policy, ceiling and digest
        policy = effective(config)
        installed = installed_providers()
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    # TB-06: the panel looks every option up by identity (`codex@is`), so the maps carry each tool
    # plus every identity the ceiling or the default team names.
    allowed = allowed_for(config)
    names = list(dict.fromkeys([*installed, *(p for chain in allowed.values() for p in chain),
                                *(i['provider'] for chain in policy['roles'].values() for i in chain)]))
    availability = {}
    for name in names:
        ok = installed[kind(name)]
        # F3-03 (K5): the session check runs only here and only when the user asks for it
        # (check_auth=true); a plain list refresh reports the cached result. Never during a write.
        home = _home(request.app.state.runner_db, name)
        # A bare `agy` is not a usable identity (model required): nothing to check, no home created.
        state, checked = (provider_auth.check(name, _binary(name), home) if check_auth else provider_auth.cached(name)) \
            if ok and not identity_problem(name) else ('unknown', None)
        availability[name] = {'installed': ok, 'auth': state, 'checked_at': checked, 'health': provider_auth.health(name)}
    return {'policy': policy, 'digest': digest(policy), 'installed': {n: installed[kind(n)] for n in names},
            'authentication': 'validated by provider at execution; never silently skipped',
            'ceiling': {'allowed': allowed, 'max_context_continuations': policy['max_context_continuations'],
                        'loop': policy.get('loop')},
            'capabilities': {n: {'profiles': list(CAPABILITIES[kind(n)]['profiles']),
                                 'context_continuation': CAPABILITIES[kind(n)]['context_continuation'],
                                 'models': CAPABILITIES[kind(n)]['models'],
                                 'model_required': CAPABILITIES[kind(n)].get('model_required', False),
                                 'efforts': list(CAPABILITIES[kind(n)].get('efforts', ()))} for n in names},
            'availability': availability}


@router.get('/catalog')
def catalog_view():
    """TB-10: the last catalog (no subprocess; before a refresh only the Claude fallback)."""
    from app import catalog
    return catalog.snapshot()


@router.post('/catalog/refresh', dependencies=[Depends(mutation_guard)])
def catalog_refresh(request: Request):
    """TB-10: an explicit user action; lists models without calling one (agy models per configured account)."""
    from app import catalog
    try:
        config = _config(request)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return catalog.refresh(request.app.state.runner_db, config)


@router.get('/quota')
def quota_view():
    """K-039: the last remaining-quota readings (no subprocess). Display only."""
    from app import quota
    return quota.snapshot()


@router.post('/quota/refresh', dependencies=[Depends(mutation_guard)])
def quota_refresh(request: Request):
    """K-039: an explicit Mac action; reads agy `/usage` and Codex rate limits per account (no model call)."""
    from app import quota
    try:
        config = _config(request)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return quota.refresh(request.app.state.runner_db, config)


@router.get('/providers/preview')
def preview_team(request: Request, author: str = Query(max_length=400), reviewer: str = Query(max_length=400)):
    """Side-effect free (no subprocess): the policy a message with this team would freeze."""
    team = {'author': [p.strip() for p in author.split(',')], 'reviewer': [p.strip() for p in reviewer.split(',')]}
    try:
        config = _config(request)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    from app import catalog
    problems = team_problems(config, team) + catalog.team_problems(team)
    if problems:
        raise HTTPException(422, {'detail': 'The selected team is not allowed', 'code': 'team_invalid', 'problems': problems})
    policy = effective(config, team)
    return {'policy': policy, 'digest': digest(policy), 'warnings': team_warnings(team),
            'catalog': {p: catalog.status(p) for chain in team.values() for p in chain}}


@router.post('/start', status_code=202, dependencies=[Depends(mutation_guard)])
async def start_session(body: StartRequest, request: Request):
    try:
        policy = load_policy(Path(request.app.state.runner_db).parent / 'providers.yaml')
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    availability = installed_providers()
    if any(not availability[kind(item['provider'])] for chain in policy['roles'].values() for item in chain):
        raise HTTPException(422, 'An authorized provider is not installed; fix the configured chain')
    if body.policy_digest != digest(policy):
        raise HTTPException(409, 'Provider policy changed; review and accept the current chain')
    sid = 'sess_' + uuid4().hex
    conn = store.connect(request.app.state.runner_db)
    try:
        with store.transaction(conn):
            repo.create_session(conn, sid, body.workspace_path, body.mode, policy=policy)
            prompts = [body.task_prompt,
                'Review the implementation against the original goal. Read only; do not change files. '
                'Report concrete problems and checks, or say no changes needed.',
                'Address the review against the original goal. Make only needed changes, verify them, '
                'then summarize the outcome. Stop when finished.']
            for i, (role, prompt) in enumerate(zip(('author', 'reviewer', 'author'), prompts)):
                repo.add_session_step(conn, f'{sid}_{i}', sid, i, policy['roles'][role][0]['provider'], prompt, role=role)
    except repo.WorkspaceBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()
    task = asyncio.create_task(request.app.state.runner.run_session(sid))
    request.app.state.runner_jobs[sid] = task
    track_job(request.app, sid, task)
    return {'session_id': sid, 'status': 'QUEUED'}


def track_job(app, sid, task):
    app.state.runner_jobs[sid] = task

    def finished(_):
        app.state.runner_jobs.pop(sid, None)
        dispatcher = getattr(app.state, 'dispatcher', None)
        if dispatcher:
            dispatcher.wake()   # a finished session may unblock a work queue

    task.add_done_callback(finished)


def start_job(app, sid):
    """Run a session this worker just moved to QUEUED (human retry or F3-04 automatic retry)."""
    track_job(app, sid, asyncio.create_task(app.state.runner.run_session(sid)))


@router.get('')
def list_sessions(request: Request, response: Response, unlinked_only: bool = False,
                  cursor: str | None = None, limit: int | None = Query(None, ge=1, le=200)):
    conn = store.connect(request.app.state.runner_db)
    try:
        # recovered_by_human is derived from real step records (sessions has no terminal_reason).
        select = ("SELECT s.*, EXISTS(SELECT 1 FROM session_steps st WHERE st.session_id=s.id "
                  "AND st.terminal_reason='recovered_by_human') AS recovered_by_human FROM sessions s")
        if not unlinked_only and cursor is None and limit is None:
            return [dict(r) for r in conn.execute(select + " ORDER BY s.created_at DESC LIMIT 200")]
        # F3-01a legacy view: filter before paging; the list body keeps its old shape.
        from app.works_store import decode_cursor, encode_cursor
        where, args = [], []
        if unlinked_only:
            where.append('NOT EXISTS(SELECT 1 FROM work_runs r WHERE r.session_id=s.id)')
        if cursor:
            created_at, sid = decode_cursor(cursor)
            where.append('(s.created_at < ? OR (s.created_at = ? AND s.id < ?))'); args += [created_at, created_at, sid]
        size = limit or 200
        rows = [dict(r) for r in conn.execute(
            select + (' WHERE ' + ' AND '.join(where) if where else '') +
            ' ORDER BY s.created_at DESC, s.id DESC LIMIT ?', (*args, size + 1))]
        if len(rows) > size:
            response.headers['X-Next-Cursor'] = encode_cursor([rows[size - 1]['created_at'], rows[size - 1]['id']])
        return rows[:size]
    finally:
        conn.close()


@router.get('/{sid}')
def get_session(sid: str, request: Request):
    conn = store.connect(request.app.state.runner_db)
    try:
        session = repo.get_session(conn, sid)
        if not session:
            raise HTTPException(404, 'session not found')
        attempts=[dict(r) for r in conn.execute('SELECT a.* FROM session_attempts a JOIN session_steps s ON s.id=a.step_id WHERE s.session_id=? ORDER BY s.step_index,a.attempt_no',(sid,))]
        policy=conn.execute('SELECT policy_json,policy_hash FROM session_policies WHERE session_id=?',(sid,)).fetchone()
        def summary(instructions):
            return None if not instructions else {
                'status': instructions['status'], 'reason': instructions['reason'],
                'path': instructions['path'], 'source_path': instructions['source_path'], 'line': instructions['line'],
                'combined_digest': instructions['combined_digest'],
                'files': instructions['manifest']['files'],
            }
        return {'session': session, 'steps': repo.get_session_steps(conn, sid), 'attempts':attempts,
                'policy':json.loads(policy[0]) if policy else None,
                'project_instructions': summary(project_instructions.snapshot(conn, sid)),
                'agents_instructions': summary(project_instructions.snapshot(conn, sid, 'codex')), 'wait_plan': wait_plans.view(conn, sid),
                **_loop_view(conn, sid)}
    finally:
        conn.close()


@router.post('/{sid}/cancel', status_code=202, dependencies=[Depends(mutation_guard)])
async def cancel_session(sid: str, request: Request):
    if request.app.state.runner.cancel_session(sid):
        return {'status': 'CANCEL_REQUESTED'}
    conn = store.connect(request.app.state.runner_db)
    try:
        with store.transaction(conn):
            session = repo.get_session(conn, sid)
            if not session:
                raise HTTPException(404, 'session not found')
            if session['status'] in (
                'QUEUED', 'PAUSED', 'WAITING_HUMAN',
                'WAITING_APPROVAL', 'WAITING_AUTH', 'WAITING_CONTEXT',
            ):  # no worker holds queued/paused/operator-wait states
                repo.update_session_status(conn, sid, 'CANCELLED')
                _wake(request)
                return {'status': 'CANCELLED'}
            if session['status'] in ('RUNNING', 'RECOVERY_REQUIRED'):
                raise HTTPException(409, 'worker ownership unresolved; inspect before recovery')
            return {'status': session['status']}
    finally:
        conn.close()


@router.get('/{sid}/recovery')
def recovery_detail(sid: str, request: Request):
    from app.adapters.process import identity, same_identity, group_exists
    conn = store.connect(request.app.state.runner_db)
    try:
        session = repo.get_session(conn, sid)
        if not session:
            raise HTTPException(404, 'session not found')
        owners = repo.unstopped_owners(conn, sid)
        for owner in owners:
            recorded = {k: owner[k] for k in ('pid', 'pgid', 'start_time', 'boot_time')}
            owner['leader_alive'] = same_identity(recorded, identity(owner['pid']))
            owner['group_alive'] = group_exists(owner['pgid'])
        locked = bool(conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?',
                                   (session['workspace_path'],)).fetchone())
        return {'status': session['status'], 'workspace_path': session['workspace_path'],
                'lock_retained': locked, 'owners': owners}
    finally:
        conn.close()


@router.post('/{sid}/recover', status_code=200, dependencies=[Depends(mutation_guard)])
async def recover_session(sid: str, body: RecoverRequest, request: Request):
    if not body.confirm:
        raise HTTPException(422, 'recovery requires explicit confirm:true')
    if sid in request.app.state.runner_jobs:
        raise HTTPException(409, 'session still running; cancel before recovery')
    from app.adapters.process import reap_owned
    conn = store.connect(request.app.state.runner_db)
    try:
        session = repo.get_session(conn, sid)
        if not session:
            raise HTTPException(404, 'session not found')
        if session['status'] != 'RECOVERY_REQUIRED':
            raise HTTPException(409, 'session is not awaiting recovery')
        proofs, blocking = [], []
        for owner in repo.unstopped_owners(conn, sid):
            recorded = {k: owner[k] for k in ('pid', 'pgid', 'start_time', 'boot_time')}
            # killpg → verify group death BEFORE any lock release (security order).
            proof = await reap_owned(recorded, repo.trusted_members(conn, owner['attempt_id']))
            proof['attempt_id'], proof['step_id'] = owner['attempt_id'], owner['step_id']
            proofs.append(proof)
            if not proof['ok']:
                blocking.extend(proof.get('blocking', []))
        if blocking:
            # Unproven processes may still touch the workspace: keep the lock, stay stuck.
            return {'status': 'RECOVERY_REQUIRED', 'blocking': blocking, 'proofs': proofs,
                    'detail': 'Some processes could not be verified terminated; '
                              'OS-level manual cleanup may be needed'}
        repo.complete_recovery(conn, sid, proofs)
        return {'status': 'FAILED', 'terminal_reason': 'recovered_by_human', 'proofs': proofs}
    finally:
        conn.close()


@router.post('/{sid}/retry', status_code=202, dependencies=[Depends(mutation_guard)])
async def retry_session(sid: str, request: Request):
    if sid in request.app.state.runner_jobs:
        raise HTTPException(409, 'session already running')
    conn = store.connect(request.app.state.runner_db)
    try:
        if not repo.get_session(conn, sid):
            raise HTTPException(404, 'session not found')
        try:
            repo.retry_session(conn, sid)
        except repo.RetryNotAllowed as exc:
            raise HTTPException(409, detail={'detail': str(exc), 'code': exc.code}) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    finally:
        conn.close()
    start_job(request.app, sid)
    return {'status': 'QUEUED'}


@router.delete('/{sid}/wait', dependencies=[Depends(mutation_guard)])
def cancel_wait(sid: str, request: Request):
    """F3-04: cancel a pending automatic retry; the waiting session ends CANCELLED."""
    conn = store.connect(request.app.state.runner_db)
    try:
        if not repo.get_session(conn, sid):
            raise HTTPException(404, 'session not found')
        try:
            wait_plans.cancel(conn, sid)
        except wait_plans.WaitError as exc:
            raise HTTPException(409, detail={'detail': str(exc), 'code': exc.code}) from exc
    finally:
        conn.close()
    _wake(request)
    return {'status': 'CANCELLED'}


def _wake(request):
    dispatcher = getattr(request.app.state, 'dispatcher', None)
    if dispatcher:
        dispatcher.wake()


def _loop_view(conn, sid):
    """F3-02 loop state for the panel; empty for legacy sessions (their response is unchanged)."""
    state = loop_store.state(conn, sid)
    if state is None:
        return {}
    estimated = conn.execute('SELECT count(*) FROM session_time_segments WHERE session_id=? AND estimated=1',
                             (sid,)).fetchone()[0]
    keep = ('max_turns', 'max_active_ms', 'current_turn', 'phase', 'pause_requested_at', 'pause_revision',
            'outcome', 'outcome_reason', 'last_author_unreviewed')
    return {'loop': dict({k: state[k] for k in keep}, used_ms=loop_store.used_ms(conn, sid),
                         time_estimated=bool(estimated)),
            'turns': [{k: t[k] for k in ('turn_no', 'author_step_id', 'reviewer_step_id', 'verdict', 'invalid_code',
                                         'ended_reason')} | {'findings': json.loads(t['findings_json'] or 'null')}
                      for t in loop_store.turns(conn, sid)]}


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm: bool = False
    pause_revision: int = Field(ge=1)
    diff_digest: str | None = Field(default=None, min_length=64, max_length=64)


@router.post('/{sid}/pause', dependencies=[Depends(mutation_guard)])
def pause_session(sid: str, request: Request):
    """Ask a loop run to stop at the next step boundary (the running step is not killed)."""
    conn = store.connect(request.app.state.runner_db)
    try:
        if not repo.get_session(conn, sid):
            raise HTTPException(404, 'session not found')
        if loop_store.state(conn, sid) is None:
            raise HTTPException(409, detail={'detail': 'only loop runs can pause', 'code': 'not_a_loop'})
        if not loop_store.request_pause(conn, sid):
            raise HTTPException(409, detail={'detail': 'nothing to pause (finished, paused or already requested)',
                                             'code': 'cannot_pause'})
        return {'status': 'PAUSE_REQUESTED'}
    finally:
        conn.close()


@router.post('/{sid}/resume', status_code=202, dependencies=[Depends(mutation_guard)])
async def resume_session(sid: str, body: ResumeBody, request: Request):
    """PAUSED → QUEUED. Folder changes since the pause need a second, digest-bound confirmation."""
    if not body.confirm:
        raise HTTPException(422, 'resume requires explicit confirm:true')
    if sid in request.app.state.runner_jobs:
        raise HTTPException(409, detail={'detail': 'session already running', 'code': 'already_running'})
    conn = store.connect(request.app.state.runner_db)
    try:
        session = repo.get_session(conn, sid)
        state = loop_store.state(conn, sid)
        if not session:
            raise HTTPException(404, 'session not found')
        if state is None or session['status'] != 'PAUSED':
            raise HTTPException(409, detail={'detail': 'session is not paused', 'code': 'not_paused'})
        if state['pause_revision'] != body.pause_revision:
            raise HTTPException(409, detail={'detail': 'pause changed; reload', 'code': 'stale_pause'})
        # Fast path only; accept_resume re-checks inside its transaction.
        busy = conn.execute("SELECT 1 FROM sessions WHERE workspace_path=? AND id!=? AND status IN "
                            "('QUEUED','RUNNING','RECOVERY_REQUIRED')", (session['workspace_path'], sid)).fetchone()
        if busy or conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?',
                                (session['workspace_path'],)).fetchone():
            raise HTTPException(409, detail={'detail': 'folder is used by another run', 'code': 'workspace_busy'})
        current = snapshot(session['workspace_path'], excluded=snapshot_exclusions(request.app.state.runner_db))
        changes = loop_store.changes_since_pause(conn, state['pause_snapshot_id'], current)
        seen_digest = loop_store.change_digest(conn, state['pause_snapshot_id'], current)
        changed = loop_store.has_changes(changes)
        if changed and body.diff_digest != seen_digest:
            # Show exactly these changes; a confirmation only counts for this digest.
            return JSONResponse(status_code=409, content={
                'detail': 'the folder changed while paused; review and confirm', 'code': 'workspace_changed',
                'diff_digest': seen_digest, 'changes': changes['differences'], 'comparable': changes['comparable'],
                'unknown': changes['unknown']})
        try:
            loop_store.accept_resume(conn, sid, body.pause_revision, seen_digest, changed)
        except loop_store.LoopStateChanged as exc:
            code = str(exc)
            detail = 'folder is used by another run' if code == 'workspace_busy' else 'pause changed; reload'
            raise HTTPException(409, detail={'detail': detail, 'code': code}) from exc
    finally:
        conn.close()
    task = asyncio.create_task(request.app.state.runner.run_session(sid))
    track_job(request.app, sid, task)
    return {'status': 'QUEUED', 'folder_changed': changed}


@router.get('/{sid}/events')
async def session_events(sid: str, request: Request, after: int = 0):
    get_session(sid, request)
    try:
        cursor = max(after, int(request.headers.get('last-event-id', '0')))
    except ValueError as exc:
        raise HTTPException(422, 'invalid event cursor') from exc

    async def generate():
        nonlocal cursor
        while not await request.is_disconnected():
            conn = store.connect(request.app.state.runner_db)
            try:
                rows = list(conn.execute('SELECT * FROM session_events WHERE session_id=? AND seq>? '
                                         'ORDER BY seq LIMIT 200', (sid, cursor)))
                session = repo.get_session(conn, sid)
            finally:
                conn.close()
            for row in rows:
                cursor = row['seq']
                payload = dict(row)
                payload['payload'] = json.loads(payload['payload'])
                yield f'id: {cursor}\nevent: work_event\ndata: {json.dumps(payload)}\n\n'
            if len(rows) == 200:
                continue
            yield 'event: work_status\ndata: ' + json.dumps({'status': session['status']}) + '\n\n'
            if session['status'] not in ('QUEUED', 'RUNNING') and len(rows) < 200:
                return
            await asyncio.sleep(.25)
    return StreamingResponse(generate(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache'})
