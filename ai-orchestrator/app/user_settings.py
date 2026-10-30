"""TB-19 (K-026): user preferences edited from the Mac panel, kept in `.system/settings.json`.

The default team (author model/effort, reviewer, fallbacks), theme and the GitHub clone folder. The provider
ceiling stays in providers.yaml and is only changed by editing that file: every saved identity is checked
against it with the same rules as a team preview. Saving a team stores the digest of the policy it freezes
(`accepted_digest`, computed here, never taken from the client); the panel treats a message with that digest
as accepted (K-026/K1). Through the mobile gateway (K-033, `command` devices) only the theme and the default team.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.provider_policy import digest, effective, load_config, team_problems
from app.security import mutation_guard

DEFAULTS = {'version': 1, 'team': None, 'accepted_digest': None, 'theme': 'system', 'github': {'clone_dir': None},
            'preview': {'sandbox': False}, 'pinned_works': [],
            'work_teams': {}, 'work_waits': {}, 'step_minutes': 45}   # TB-22 K1: experimental, off by default; K-061
_LOCK = threading.Lock()


def path(db_path) -> Path:
    return Path(db_path).parent / 'settings.json'


def load(db_path) -> tuple[dict, str | None]:
    """The saved settings over the defaults; a missing file is the defaults, a broken one too (plus an error)."""
    try:
        data = json.loads(path(db_path).read_text())
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULTS)), None
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS)), 'settings_unreadable'
    if not isinstance(data, dict) or data.get('version') != 1:
        return json.loads(json.dumps(DEFAULTS)), 'settings_unreadable'
    merged = {**json.loads(json.dumps(DEFAULTS)), **{k: data[k] for k in DEFAULTS if k in data}}
    return merged, None


def preview_sandbox(db_path) -> bool:
    return (load(db_path)[0].get('preview') or {}).get('sandbox') is True


def team_check(config, team) -> list:
    from app import catalog
    return team_problems(config, team) + catalog.team_problems(team)


def _clone_dir_problem(value: str, root: Path) -> str | None:
    p = Path(value)
    if not p.is_absolute():
        return 'not_absolute'
    p = p.resolve()
    home = Path.home().resolve()
    if p != home and home not in p.parents:
        return 'outside_home'
    if not p.is_dir():
        return 'not_a_directory'
    if p == root or root in p.parents:
        return 'inside_conductor'
    return None


def _write(db_path, data: dict) -> None:
    target = path(db_path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.settings-', suffix='.json')   # created 0600
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── API ──────────────────────────────────────────────────────────────────────
router = APIRouter(prefix='/api/settings')

PinnedWorkId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
WorkId = PinnedWorkId


class Team(BaseModel):
    model_config = ConfigDict(extra='forbid')
    author: list[str] = Field(min_length=1, max_length=4)
    reviewer: list[str] = Field(min_length=1, max_length=4)


class WorkWait(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    deadline_hours: int | None = Field(default=None, ge=1, le=24, strict=True)
    max_attempts: int | None = Field(default=None, ge=1, le=5, strict=True)


class GitHub(BaseModel):
    model_config = ConfigDict(extra='forbid')
    clone_dir: str | None = Field(default=None, max_length=1024)


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sandbox: bool = Field(False, strict=True)


class Patch(BaseModel):
    """Partial update: only the fields sent change; `team: null` goes back to the providers.yaml roles."""
    model_config = ConfigDict(extra='forbid')
    team: Team | None = None
    theme: Literal['system', 'dark', 'light'] | None = None
    github: GitHub | None = None
    preview: Preview | None = None
    pinned_works: list[PinnedWorkId] | None = Field(default=None, max_length=20)
    work_teams: dict[WorkId, Team] | None = Field(default=None, max_length=50)
    work_waits: dict[WorkId, WorkWait] | None = Field(default=None, max_length=50)
    step_minutes: int | None = Field(default=None, ge=10, le=180, strict=True)   # K-061: one provider step's longest run

    @field_validator('pinned_works')
    @classmethod
    def _unique_pinned_works(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError('pinned_works must not contain duplicates')
        return value

    @field_validator('work_teams', 'work_waits')
    @classmethod
    def _limit_work_map(cls, value: dict | None) -> dict | None:
        if value is not None and len(value) > 50:
            raise ValueError('work settings map may contain at most 50 entries')
        return value


def _config(request: Request):
    try:
        return load_config(Path(request.app.state.runner_db).parent / 'providers.yaml')
    except (ValueError, OSError) as exc:
        raise HTTPException(422, {'code': 'policy_invalid', 'detail': str(exc)}) from exc


def _view(db_path, config) -> dict:
    data, error = load(db_path)
    problems, current = [], None
    if data['team']:
        problems = team_check(config, data['team'])
        current = None if problems else digest(effective(config, data['team']))
    return {'settings': data, 'error': error, 'problems': problems, 'current_digest': current}


PHONE_FIELDS = ('version', 'team', 'accepted_digest', 'theme', 'pinned_works', 'work_teams')   # K-033/4b: what a phone reads and writes


def _for(request, view: dict) -> dict:
    if getattr(request.state, 'conductor_mobile_device', None):
        view['settings'] = {k: view['settings'][k] for k in PHONE_FIELDS}
    return view


@router.get('')
def get_settings(request: Request):
    return _for(request, _view(request.app.state.runner_db, _config(request)))


@router.put('', dependencies=[Depends(mutation_guard)])
def put_settings(body: Patch, request: Request):
    sent = body.model_fields_set
    if getattr(request.state, 'conductor_mobile_device', None) and sent - {'team', 'theme', 'pinned_works', 'work_teams'}:   # K-033/4b
        raise HTTPException(403, {'code': 'mobile_field',
                                  'detail': 'From a phone only the theme, pinned works, work teams and the default team'})
    db_path, config = request.app.state.runner_db, _config(request)
    with _LOCK:
        data, error = load(db_path)                  # a broken file is backed up and replaced by a valid one on save
        if 'team' in sent:
            if body.team is None:
                data['team'], data['accepted_digest'] = None, None
            else:
                team = body.team.model_dump()
                problems = team_check(config, team)
                if problems:
                    raise HTTPException(422, {'code': 'team_invalid', 'detail': 'The selected team is not allowed',
                                              'problems': problems})
                data['team'], data['accepted_digest'] = team, digest(effective(config, team))
        if 'theme' in sent and body.theme is not None:
            data['theme'] = body.theme
        if 'github' in sent and body.github is not None:
            clone_dir = body.github.clone_dir
            if clone_dir:
                problem = _clone_dir_problem(clone_dir, Path(db_path).resolve().parent.parent)
                if problem:
                    raise HTTPException(422, {'code': 'clone_dir_invalid', 'detail': problem})
                clone_dir = str(Path(clone_dir).resolve())
            data['github'] = {'clone_dir': clone_dir or None}
        if 'preview' in sent and body.preview is not None:
            data['preview'] = {'sandbox': body.preview.sandbox}
        if 'step_minutes' in sent and body.step_minutes is not None:
            data['step_minutes'] = body.step_minutes
        if 'pinned_works' in sent and body.pinned_works is not None:
            data['pinned_works'] = list(body.pinned_works)
        if 'work_teams' in sent and body.work_teams is not None:
            teams = {}
            for wid, team_model in body.work_teams.items():
                team = team_model.model_dump()
                problems = team_check(config, team)
                if problems:
                    raise HTTPException(422, {'code': 'team_invalid', 'detail': 'The selected team is not allowed',
                                              'work_id': wid, 'problems': problems})
                teams[wid] = team
            data['work_teams'] = teams
        if 'work_waits' in sent and body.work_waits is not None:
            data['work_waits'] = {wid: w.model_dump(exclude_none=True) for wid, w in body.work_waits.items()}
        if error == 'settings_unreadable':
            target = path(db_path)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            try:
                shutil.copy2(target, target.with_name(f'settings.json.broken-{stamp}'))
            except FileNotFoundError:
                pass
        _write(db_path, data)
    return _for(request, _view(db_path, config))
