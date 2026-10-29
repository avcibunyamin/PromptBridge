"""One public error shape. Unexpected exception details stay out of responses."""
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse


def _problems(items):
    from app.provider_policy import TEAM_REASONS
    out = []
    for item in (items if isinstance(items, list) else [])[:8]:
        if isinstance(item, dict) and item.get('role') in ('author', 'reviewer') and item.get('reason') in TEAM_REASONS:
            out.append({'role': item['role'], 'provider': str(item.get('provider', ''))[:32], 'reason': item['reason']})
    return out


def install_handlers(app):
    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        detail = exc.detail
        code = {403:'forbidden',404:'not_found',409:'conflict',422:'validation_error'}.get(exc.status_code,'http_error')
        extra = {}
        if isinstance(detail, dict):
            code = detail.get('code',code)
            if code == 'team_invalid':   # F3-03: bounded, enum-checked, no raw input
                extra['problems'] = _problems(detail.get('problems'))
            if code == 'not_a_repo' and isinstance(detail.get('repos'), list):   # K-055: server-made folder paths
                extra['repos'] = [str(r)[:4096] for r in detail['repos'][:8]]
            detail = detail.get('detail',code)
        return JSONResponse({'detail':str(detail),'code':code,**extra},status_code=exc.status_code,headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo input values: prompts and file bodies may contain secrets.
        detail = '; '.join('.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors())
        return JSONResponse({'detail':detail,'code':'validation_error'},status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({'detail':'Internal server error','code':'internal_error'},status_code=500)
