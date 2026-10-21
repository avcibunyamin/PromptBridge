"""TB-12 (K-024): mobile endpoints.

On the main listener (8765, loopback + mutation_guard): gateway settings, pairing codes, devices, audit.
On the gateway (8770, through MobileGuard): only `pair` (no token) and `me` (device). Each handler checks
which listener it is on via the server-side marks MobileGuard sets; a client cannot set them.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import mobile, store
from .security import mutation_guard

router = APIRouter(prefix='/api/mobile')


def _gateway(request: Request) -> bool:
    return bool(getattr(request.state, mobile.GATEWAY_MARK, False))


def _loopback_only(request: Request):
    if _gateway(request):
        raise HTTPException(404, detail={'code': 'not_found', 'detail': 'Not found'})


def _conn(request: Request):
    return store.connect(request.app.state.runner_db)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    host: str = Field(default='', max_length=253)
    port: int = 8770


class Pair(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=6, max_length=6, pattern=r'^[0-9]{6}$')
    device_name: str = Field(default='Telefon', max_length=60)


@router.get('/status', dependencies=[Depends(_loopback_only)])
def status(request: Request):
    """Mac panel: gateway settings as saved, whether this server runs the gateway, devices and recent audit."""
    db = request.app.state.runner_db
    conn = _conn(request)
    try:
        saved = None
        try:
            import json
            saved = json.loads(mobile.config_path(db).read_text())
        except (OSError, ValueError):
            pass
        return {'saved': saved, 'active': getattr(request.app.state, 'mobile_gateway', None),
                'devices': mobile.devices(conn), 'audit': mobile.recent_audit(conn, 30)}
    finally:
        conn.close()


@router.put('/settings', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def settings(body: Settings, request: Request):
    """Writes .system/mobile.json; the gateway starts or stops when Conductor restarts."""
    try:
        return mobile.save_config(request.app.state.runner_db, body.enabled, body.host, body.port)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/pairing', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def pairing(request: Request):
    conn = _conn(request)
    try:
        return mobile.create_pairing(conn)
    finally:
        conn.close()


@router.delete('/devices/{device_id}', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def revoke(device_id: str, request: Request):
    conn = _conn(request)
    try:
        if not mobile.revoke(conn, device_id):
            raise HTTPException(404, detail={'code': 'not_found', 'detail': 'No active device with this id'})
        return {'revoked': device_id}
    finally:
        conn.close()


class Scopes(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scopes: list[Annotated[str, StringConstraints(max_length=20)]] = Field(max_length=5)


@router.put('/devices/{device_id}/scopes', dependencies=[Depends(_loopback_only), Depends(mutation_guard)])
def device_scopes(device_id: str, body: Scopes, request: Request):
    """K-027/4: the Mac grants or takes back `interrupt` per device."""
    conn = _conn(request)
    try:
        try:
            scopes = mobile.set_scopes(conn, device_id, body.scopes)
        except ValueError as exc:
            raise HTTPException(422, detail={'code': 'validation_error', 'detail': str(exc)}) from exc
        if scopes is None:
            raise HTTPException(404, detail={'code': 'not_found', 'detail': 'No active device with this id'})
        return {'device_id': device_id, 'scopes': scopes}
    finally:
        conn.close()


def _gateway_only(request: Request):
    if not _gateway(request):
        raise HTTPException(404, detail={'code': 'not_found', 'detail': 'Not found'})


@router.post('/pair', dependencies=[Depends(_gateway_only), Depends(mutation_guard)])
def pair(body: Pair, request: Request):
    """Gateway only, no token: a valid code from the Mac panel becomes a device token (returned once)."""
    if not _gateway(request):
        raise HTTPException(404, detail={'code': 'not_found', 'detail': 'Not found'})
    conn = _conn(request)
    try:
        try:
            result = mobile.pair(conn, body.code, body.device_name, getattr(request.state, 'conductor_tailscale_login', None))
        except mobile.PairingError as exc:
            mobile.audit(conn, None, 'POST', '/api/mobile/pair', 403, str(exc))
            raise HTTPException(403, detail={'code': str(exc), 'detail': 'Pairing refused'}) from exc
        mobile.audit(conn, result['device_id'], 'POST', '/api/mobile/pair', 200, 'paired')
        return result
    finally:
        conn.close()


@router.get('/me')
def me(request: Request):
    """Gateway only: the authenticated device. On 8765 this is 404, which is how the panel knows it is local."""
    device = getattr(request.state, mobile.DEVICE_MARK, None)
    if not device:
        raise HTTPException(404, detail={'code': 'not_found', 'detail': 'Not found'})
    return {'device_id': device, 'scopes': getattr(request.state, 'conductor_mobile_scopes', [])}
