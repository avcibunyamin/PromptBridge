"""TB-39: provider account management from the Mac panel, backed by `.system/providers.yaml`.

Loopback only (`_loopback_only`), never reachable through the mobile gateway. Every mutation validates the
candidate `providers.yaml` via `provider_policy.load_config` on a temporary file first, writes a timestamped
backup (`.system/providers.pre-accounts-<YYYYmmdd-HHMMSS>.yaml`), atomically replaces `providers.yaml` with a
header comment naming the backup, appends an audit record to `.system/providers-changes.jsonl`, and triggers a
background catalog refresh (no model call).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Literal
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import yaml

from app import catalog, mobile, user_settings
from app.adapters.agy import agy_binary, agy_home, prepare_home
from app.adapters.claude import claude_binary
from app.adapters.codex import codex_binary, codex_home
from app.provider_policy import ACCOUNT_RE, CAPABILITIES, PROFILES, allowed_for, effective, load_config, split
from app.security import mutation_guard

log = logging.getLogger(__name__)

router = APIRouter(prefix='/api/accounts')
_WRITE_LOCK = threading.Lock()
ROLE_ORDER = ('author', 'reviewer')


def _loopback_only(request: Request) -> None:
    if bool(getattr(request.state, mobile.GATEWAY_MARK, False)):
        raise HTTPException(404, detail={'code': 'not_found', 'detail': 'Not found'})


def _db_path(request: Request) -> Path:
    db = getattr(request.app.state, 'runner_db', None)
    if db:
        return Path(db)
    from app.api import _db_path as default_db
    return default_db()


def _providers_path(db_path: Path) -> Path:
    return db_path.parent / 'providers.yaml'


def _changes_path(db_path: Path) -> Path:
    return db_path.parent / 'providers-changes.jsonl'


def _load_current_config(db_path: Path) -> dict:
    try:
        return load_config(_providers_path(db_path))
    except (ValueError, OSError) as exc:
        raise HTTPException(422, detail={'code': 'policy_invalid', 'detail': str(exc)}) from exc


def base_of(identity_str: str) -> str:
    provider, account, _, _ = split(identity_str)
    return f'{provider}@{account}' if account else provider


def _installed_map() -> dict[str, bool]:
    binaries = {'agy': agy_binary, 'claude': claude_binary, 'codex': codex_binary, 'ollama': codex_binary}
    out: dict[str, bool] = {}
    for provider in CAPABILITIES:
        try:
            binaries.get(provider, codex_binary)()
            out[provider] = True
        except FileNotFoundError:
            out[provider] = False
    return out


def _login_info(db_path: Path, provider: str, account: str | None) -> dict[str, str | None]:
    if provider == 'agy':
        home = agy_home(str(db_path), account)
        return {
            'command': f'(H="{home}"; cd /tmp && HOME="$H" ~/.local/bin/agy)',
            'note': 'Ayrı HOME ve anahtar zinciri ile giriş',
        }
    if provider == 'codex':
        home = codex_home(str(db_path), account)
        return {
            'command': f'CODEX_HOME="{home}" codex login',
            'note': 'Ayrı CODEX_HOME ile giriş',
        }
    if provider == 'claude':
        return {
            'command': None,
            'note': "claude (Claude Code'da /login)",
        }
    return {
        'command': None,
        'note': 'ollama serve',
    }


def _default_team_bases(db_path: Path, config: dict) -> set[str]:
    bases: set[str] = set()
    policy = effective(config)
    for chain in policy['roles'].values():
        for item in chain:
            try:
                bases.add(base_of(item['provider']))
            except ValueError:
                pass
    saved, _ = user_settings.load(db_path)
    team = saved.get('team')
    if isinstance(team, dict):
        for chain in team.values():
            if isinstance(chain, list):
                for entry in chain:
                    if isinstance(entry, str):
                        try:
                            bases.add(base_of(entry))
                        except ValueError:
                            pass
    return bases


def _list_accounts(db_path: Path, config: dict) -> list[dict]:
    allowed = allowed_for(config)
    default_bases = _default_team_bases(db_path, config)
    installed = _installed_map()
    ordered_bases: list[str] = []
    parsed: dict[str, tuple[str, str | None]] = {}
    roles_by_base: dict[str, list[str]] = {}

    for role in ROLE_ORDER:
        for entry in allowed.get(role, ()):
            provider, account, _, _ = split(entry)
            base = f'{provider}@{account}' if account else provider
            if base not in parsed:
                ordered_bases.append(base)
                parsed[base] = (provider, account)
                roles_by_base[base] = []
            if role not in roles_by_base[base]:
                roles_by_base[base].append(role)

    items: list[dict] = []
    for base in ordered_bases:
        provider, account = parsed[base]
        items.append({
            'base': base,
            'provider': provider,
            'account': account,
            'roles': roles_by_base[base],
            'in_default_team': base in default_bases,
            'installed': bool(installed.get(provider, False)),
            'login': _login_info(db_path, provider, account),
            'signed_in': catalog.signed_in(provider, account) if provider in ('agy', 'codex') else None,
        })
    return items


def _prepare_account_home(db_path: Path, provider: str, account: str | None) -> None:
    if provider == 'agy':
        prepare_home(agy_home(str(db_path), account))
    elif provider == 'codex':
        home = codex_home(str(db_path), account)
        try:
            os.mkdir(home, 0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(os.lstat(home).st_mode):
            raise ValueError(f'Codex home is not a real directory: {home}')
        os.chmod(home, 0o700)


def _write_providers_yaml(
    db_path: Path,
    new_config: dict,
    *,
    action: str,
    base: str,
    roles: list[str],
    before_replace=None,
) -> dict:
    """Validate `new_config` in a temp file via `load_config`, back up `providers.yaml`, atomically replace it,
    and append an audit line to `.system/providers-changes.jsonl`. Caller must hold `_WRITE_LOCK`."""
    target = _providers_path(db_path)
    system_dir = target.parent
    system_dir.mkdir(parents=True, exist_ok=True)
    backup_name = f"providers.pre-accounts-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
    backup_path = system_dir / backup_name
    header = f'# PromptBridge Ayarlar → Hesaplar ile değişti; önceki: {backup_name}\n'
    body = yaml.safe_dump(new_config, sort_keys=False, allow_unicode=True)
    content = header + body

    fd, tmp_str = tempfile.mkstemp(dir=system_dir, prefix='.providers-', suffix='.yaml.tmp')
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        try:
            validated = load_config(tmp_path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail={'code': 'validation_error', 'detail': str(exc)}) from exc

        if before_replace is not None:
            try:
                before_replace()
            except (ValueError, OSError) as exc:
                raise HTTPException(422, detail={'code': 'validation_error', 'detail': str(exc)}) from exc

        if target.exists():
            shutil.copy2(target, backup_path)
        else:
            backup_path.write_text(
                yaml.safe_dump(load_config(target), sort_keys=False, allow_unicode=True),
                encoding='utf-8',
            )
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    record = {
        'at': datetime.now(timezone.utc).isoformat(),
        'action': action,
        'base': base,
        'roles': list(roles),
        'backup': backup_name,
    }
    with open(_changes_path(db_path), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    return validated


def _refresh_catalog_in_background(db_path: Path, config: dict) -> None:
    def _run() -> None:
        try:
            catalog.refresh(str(db_path), config)
        except Exception:
            log.warning('accounts: background catalog refresh failed', exc_info=True)

    threading.Thread(target=_run, name='accounts-catalog-refresh', daemon=True).start()


class AddAccountRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['agy', 'codex', 'claude', 'ollama']
    account: str | None = Field(None, max_length=60)   # K-060: any name; the server derives the safe id
    roles: list[Literal['author', 'reviewer']] = Field(min_length=1)


def account_id(name: str | None) -> str | None:
    """K-060: a freely typed account name → the id used in folder names and `agy@id/model` (lower-case ASCII
    letters, digits and dashes, at most 16). None for no name, '' when nothing usable is left."""
    if name is None or not name.strip():
        return None
    text = unicodedata.normalize('NFKD', name.replace('ı', 'i').replace('İ', 'I')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-')[:16].strip('-')


def _validate_add_request(body: AddAccountRequest) -> tuple[str, str | None, list[str], str]:
    provider = body.provider
    account = account_id(body.account)
    unusable = HTTPException(422, detail={'code': 'validation_error', 'detail': 'Hesap adında en az bir harf ya da rakam olmalı'})
    if provider == 'agy':
        if account is None:
            raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'agy için bir hesap adı yazın'})
        if not account:
            raise unusable
    elif provider == 'codex':
        if account == '':
            raise unusable
    else:
        if account is not None:
            raise HTTPException(422, detail={'code': 'validation_error', 'detail': f'{provider} hesap adı desteklemiyor'})
    if account is not None and not ACCOUNT_RE.fullmatch(account):   # account_id always fits; kept as the guard
        raise unusable

    roles = [r for r in ROLE_ORDER if r in body.roles]
    if not roles:
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'En az bir rol seçilmelidir'})
    base = f'{provider}@{account}' if account else provider
    return provider, account, roles, base


@router.get('', dependencies=[Depends(_loopback_only)])
def get_accounts(request: Request):
    db_path = _db_path(request)
    config = _load_current_config(db_path)
    return {'accounts': _list_accounts(db_path, config)}


@router.post('', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def add_account(body: AddAccountRequest, request: Request):
    provider, account, roles, base = _validate_add_request(body)
    db_path = _db_path(request)

    with _WRITE_LOCK:
        config = _load_current_config(db_path)
        allowed = {r: list( items) for r, items in allowed_for(config).items()}
        if any(base_of(x) == base for r in PROFILES for x in allowed[r]):
            raise HTTPException(409, detail={'code': 'exists', 'detail': f'Bu adda bir hesap zaten var ({base})'})
        for role in roles:
            allowed[role].append(base)
        new_config = dict(config, allowed=allowed)
        validated = _write_providers_yaml(
            db_path,
            new_config,
            action='add',
            base=base,
            roles=roles,
            before_replace=lambda: _prepare_account_home(db_path, provider, account),
        )
        accounts = _list_accounts(db_path, validated)

    _refresh_catalog_in_background(db_path, validated)
    created = next((item for item in accounts if item['base'] == base), None)
    return {'account': created, 'accounts': accounts}


def _configured(db_path: Path, base: str) -> tuple[str, str | None]:
    """A sign-in can only be opened or checked for an account that is configured (agy or codex)."""
    try:
        provider, account, model, effort = split(base)
    except ValueError as exc:
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'Geçersiz sağlayıcı tabanı'}) from exc
    if model is not None or effort is not None or provider not in ('agy', 'codex') or (provider == 'agy' and not account):
        raise HTTPException(422, detail={'code': 'login_unsupported', 'detail': 'Bu hesap için giriş burada açılmaz'})
    allowed = allowed_for(_load_current_config(db_path))
    if not any(base_of(x) == base for r in PROFILES for x in allowed[r]):
        raise HTTPException(404, detail={'code': 'not_found', 'detail': f'{base} bulunamadı'})
    return provider, account


def _login_script(db_path: Path, provider: str, account: str | None) -> Path:
    """K-060: a fixed `.command` file Terminal runs; only server-made paths go in it, quoted, never user text."""
    _prepare_account_home(db_path, provider, account)   # agy: its own keychain is unlocked first (K-058)
    if provider == 'agy':
        body = f'cd /tmp || exit 1\nHOME={shlex.quote(agy_home(str(db_path), account))} exec {shlex.quote(agy_binary())}\n'
    else:
        body = f'CODEX_HOME={shlex.quote(str(codex_home(str(db_path), account)))} exec {shlex.quote(codex_binary())} login\n'
    folder = db_path.parent / 'login'
    folder.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(folder).st_mode):
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'login klasörü gerçek bir klasör değil'})
    path = folder / f"{provider}-{account or 'default'}.command"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.login-', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(f'#!/bin/sh\n# PromptBridge: {provider} sign-in (K-060)\n{body}')
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return path


def _open_terminal(path: Path) -> None:
    subprocess.run(['/usr/bin/open', '-a', 'Terminal', str(path)], check=True, capture_output=True, timeout=15)


@router.post('/{base}/login', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def open_login(base: str, request: Request):
    """K-060: opens the account's sign-in in Terminal (the person signs in there; nothing is typed for them)."""
    db_path = _db_path(request)
    provider, account = _configured(db_path, base)
    try:
        path = _login_script(db_path, provider, account)
        _open_terminal(path)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(409, detail={'code': 'login_open_failed', 'detail': f'Terminal açılamadı: {exc}'[:300]}) from exc
    return {'opened': True}


@router.post('/{base}/check', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def check_login(base: str, request: Request):
    """K-060: is this account signed in now? Re-lists its models (no model turn) and refreshes its catalog part."""
    db_path = _db_path(request)
    provider, account = _configured(db_path, base)
    signed_in, error = catalog.refresh_base(str(db_path), provider, account)
    return {'signed_in': signed_in, 'error': error}


@router.delete('/{base}', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def delete_account(base: str, request: Request):
    try:
        provider, account, model, effort = split(base)
    except ValueError as exc:
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'Geçersiz sağlayıcı tabanı'}) from exc
    if model is not None or effort is not None or provider not in CAPABILITIES:
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'Geçersiz sağlayıcı tabanı'})

    db_path = _db_path(request)
    with _WRITE_LOCK:
        config = _load_current_config(db_path)
        allowed = {r: list(items) for r, items in allowed_for(config).items()}
        removed_roles = [r for r in ROLE_ORDER if any(base_of(x) == base for x in allowed[r])]
        if not removed_roles:
            raise HTTPException(404, detail={'code': 'not_found', 'detail': f'{base} bulunamadı'})
        if base in _default_team_bases(db_path, config):
            raise HTTPException(409, detail={'code': 'in_default_team', 'detail': f'{base} varsayılan takımda kullanılıyor'})

        new_allowed = {r: [x for x in allowed[r] if base_of(x) != base] for r in ROLE_ORDER}
        new_config = dict(config, allowed=new_allowed)
        validated = _write_providers_yaml(
            db_path,
            new_config,
            action='delete',
            base=base,
            roles=removed_roles,
        )
        accounts = _list_accounts(db_path, validated)

    from app import quota
    quota.forget(base)
    _refresh_catalog_in_background(db_path, validated)
    return {'deleted': base, 'accounts': accounts}
