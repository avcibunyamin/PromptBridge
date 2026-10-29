"""F3-01a persistent works API. Mutations share mutation_guard and the flat error shape."""
import asyncio
import json
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from app import store, runner_store as repo, works_store as ws
from app.provider_policy import load_config, effective, team_problems, digest, kind
from app.security import mutation_guard

router = APIRouter(prefix='/api/works')


class CreateWork(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_work_id: UUID
    title: str = Field(min_length=1, max_length=200)
    workspace_path: str = Field(min_length=1)
    isolate: bool = Field(False, strict=True)   # TB-26 (K-034): run in a git worktree of this folder


ProviderName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]   # no int -> str


class Team(BaseModel):
    """Structural bounds only; the semantic checks (1..2, allowed, profiles) answer team_invalid."""
    model_config = ConfigDict(extra='forbid')
    author: list[ProviderName] = Field(max_length=4)
    reviewer: list[ProviderName] = Field(max_length=4)


class AddMessage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=100000)
    policy_digest: str = Field(min_length=64, max_length=64)
    team: Team | None = None   # F3-03: optional per-message team; omitted = the default team
    # F3-04 (K-012): opt-in bounded quota wait; the limits are frozen with the message.
    wait_on_quota: bool = Field(False, strict=True)
    wait_deadline_hours: int = Field(4, ge=1, le=24, strict=True)
    wait_max_attempts: int = Field(3, ge=1, le=5, strict=True)
    recipe: str | None = Field(None, max_length=32)


class PatchWork(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class DeleteWork(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)


class ContinueWork(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm: bool = False
    expected_session_id: str = Field(min_length=1)
    expected_outcome: str | None = None   # F3-02: required to pass an unverified loop outcome


def _db(request):
    return store.connect(request.app.state.runner_db)


def _fail(exc):
    if isinstance(exc, ws.WorkError):
        return HTTPException(exc.status, detail={'code': exc.code, 'detail': str(exc)})
    if isinstance(exc, (ValueError, OSError)):
        return HTTPException(422, detail={'code': 'validation_error', 'detail': str(exc)})
    raise exc


def _wake(request):
    dispatcher = getattr(request.app.state, 'dispatcher', None)
    if dispatcher:
        dispatcher.wake()


@router.post('', dependencies=[Depends(mutation_guard)])
def create_work(body: CreateWork, request: Request, response: Response):
    conn = _db(request)
    try:
        path = body.workspace_path
        if getattr(request.state, 'conductor_mobile_device', None):   # K-033/3: no arbitrary folder from a phone
            refused = HTTPException(403, detail={'code': 'mobile_folder',
                                                 'detail': 'From a phone only the folder of an active work'})
            try:
                path = repo.canonical_workspace(path)                 # the checked path is the one stored
            except (ValueError, OSError):
                raise refused from None                               # same answer: no probing which paths exist
            if not conn.execute('SELECT 1 FROM works WHERE workspace_path=? AND archived_at IS NULL AND deleted_at IS NULL', (path,)).fetchone():
                raise refused
        if body.isolate:
            from app import isolation
            from app.github import GitHubError
            source = repo.canonical_workspace(path)
            wid = str(body.client_work_id)
            copy, branch = source, None                                # a replay: compared by its source, no copy
            if not conn.execute('SELECT 1 FROM works WHERE id=?', (wid,)).fetchone():
                try:
                    copy, branch = isolation.create(source, wid, body.title, conn)
                except GitHubError as exc:
                    extra = {} if getattr(request.state, 'conductor_mobile_device', None) else exc.extra   # K-055: no folder listing for a phone
                    raise HTTPException(exc.status, detail={'code': exc.code, 'detail': str(exc), **extra}) from None
            work, created = ws.create_work(conn, wid, body.title, copy, {'isolated_from': source, 'branch': branch})
        else:
            work, created = ws.create_work(conn, str(body.client_work_id), body.title, path)
    except (ws.WorkError, ValueError, OSError) as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()
    response.status_code = 201 if created else 200
    return ws.public_work(work)


@router.get('')
def list_works(request: Request, archived: bool = False, cursor: str | None = None,
               limit: int = Query(50, ge=1, le=100)):
    conn = _db(request)
    try:
        return ws.list_works(conn, archived=archived, cursor=cursor, limit=limit)
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()


@router.get('/{wid}')
def get_work(wid: str, request: Request):
    conn = _db(request)
    try:
        from app import isolation
        return dict(ws.public_work(ws.get_work(conn, wid)), **ws.work_summary(conn, wid), isolation=isolation.info(conn, wid))
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()


@router.get('/{wid}/messages')
def list_messages(wid: str, request: Request, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
    conn = _db(request)
    try:
        return ws.list_messages(conn, wid, cursor=cursor, limit=limit)
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()


@router.post('/{wid}/messages', dependencies=[Depends(mutation_guard)])
def add_message(wid: str, body: AddMessage, request: Request, response: Response):
    from app.session_api import installed_providers   # no subprocess inside the write transaction
    recipe_frozen = None
    if body.recipe is not None:
        from app import recipes
        try:
            recipe_frozen = recipes.frozen(body.recipe)
        except ValueError as exc:
            raise HTTPException(422, detail={'code': 'recipe_unknown', 'detail': str(exc)}) from None
    conn = _db(request)
    team = body.team.model_dump() if body.team else None
    wait = ({'deadline_hours': body.wait_deadline_hours, 'max_attempts': body.wait_max_attempts}
            if body.wait_on_quota else None)
    try:
        # Serialize replay lookup with insertion. Existing requests never consult config.
        with store.transaction(conn):
            replay = conn.execute('SELECT policy_json, policy_digest FROM work_messages '
                                  'WHERE work_id=? AND client_message_id=?',
                                  (wid, str(body.client_message_id))).fetchone()
            if replay:
                policy = json.loads(replay['policy_json'])
                current_digest = replay['policy_digest']
            else:
                try:
                    config = load_config(Path(request.app.state.runner_db).parent / 'providers.yaml')
                except (ValueError, OSError) as exc:
                    raise HTTPException(422, str(exc)) from exc
                from app import catalog
                if team is not None:
                    problems = team_problems(config, team) + catalog.team_problems(team)
                else:
                    default_policy = effective(config)
                    default_team = {role: [i['provider'] for i in chain] for role, chain in default_policy['roles'].items()}
                    problems = catalog.team_problems(default_team)
                if problems:
                    raise HTTPException(422, {'detail': 'The selected team is not allowed', 'code': 'team_invalid',
                                              'problems': problems})
                policy = effective(config, team)
                installed = installed_providers()
                if any(not installed[kind(i['provider'])] for chain in policy['roles'].values() for i in chain):
                    raise HTTPException(422, 'An authorized provider is not installed; fix the configured chain')
                current_digest = digest(policy)   # the accepted chain; the wait choice is not part of it
                if wait:
                    policy = dict(policy, wait=wait)
                if recipe_frozen:
                    policy = dict(policy, recipe=recipe_frozen)
            message, created = ws.add_message(conn, wid, str(body.client_message_id), body.text,
                                              body.policy_digest, policy, current_digest, team, wait,
                                              body.recipe)
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()
    if created:
        _wake(request)   # trigger (1): after the message transaction committed
    response.status_code = 201 if created else 200
    return message


@router.post('/{wid}/messages/{mid}/cancel', dependencies=[Depends(mutation_guard)])
def cancel_message(wid: str, mid: str, request: Request):
    conn = _db(request)
    try:
        message, changed = ws.cancel_message(conn, wid, mid)
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()
    if changed:
        _wake(request)
    return message


@router.patch('/{wid}', dependencies=[Depends(mutation_guard)])
def patch_work(wid: str, body: PatchWork, request: Request):
    if body.title is None and body.archived is None:
        raise HTTPException(422, 'nothing to change')
    if getattr(request.state, 'conductor_mobile_device', None) and body.archived is not None:
        raise HTTPException(403, detail={'code': 'mobile_field',
                                         'detail': 'From a phone only the title of a work can be changed'})
    conn = _db(request)
    try:
        return ws.public_work(ws.patch_work(conn, wid, body.expected_revision, title=body.title, archived=body.archived))
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()


@router.delete('/{wid}', dependencies=[Depends(mutation_guard)])
def delete_work(wid: str, request: Request, body: DeleteWork | None = None,
                expected_revision: int | None = Query(default=None, ge=0)):
    rev = expected_revision if expected_revision is not None else (body.expected_revision if body is not None else None)
    if rev is None:
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'expected_revision is required'})
    conn = _db(request)
    try:
        work = ws.delete_work(conn, wid, rev)
        return {'deleted': True, **ws.public_work(work), 'deleted_at': work['deleted_at']}
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()


@router.post('/{wid}/continue', dependencies=[Depends(mutation_guard)])
def continue_work(wid: str, body: ContinueWork, request: Request):
    if not body.confirm:
        raise HTTPException(422, 'continue requires explicit confirm:true')
    conn = _db(request)
    try:
        changed = ws.continue_work(conn, wid, body.expected_session_id, body.expected_outcome)
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()
    _wake(request)
    return {'continued': True, 'changed': changed}


@router.get('/{wid}/events')
async def work_events(wid: str, request: Request, after: int = 0, until_current: bool = False):
    conn = _db(request)
    try:
        ws.get_work(conn, wid)
        cursor = max(after, int(request.headers.get('last-event-id', '0')))
    except ws.WorkError as exc:
        raise _fail(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, 'invalid event cursor') from exc
    finally:
        conn.close()

    async def generate():
        nonlocal cursor
        while not await request.is_disconnected():
            c = _db(request)
            try:
                rows = [dict(r) for r in c.execute('SELECT * FROM work_events WHERE work_id=? AND seq>? '
                                                   'ORDER BY seq LIMIT 200', (wid, cursor))]
            finally:
                c.close()
            for row in rows:
                cursor = row['seq']
                row['payload'] = json.loads(row['payload'])
                yield f'id: {cursor}\nevent: work_event\ndata: {json.dumps(row)}\n\n'
            if until_current and len(rows) < 200:
                return
            if len(rows) < 200:
                await asyncio.sleep(.5)
    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})
