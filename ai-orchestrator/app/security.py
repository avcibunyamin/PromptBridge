"""Shared browser-origin policy; local non-browser clients remain supported."""
from fastapi import HTTPException, Request

ALLOWED_ORIGINS = ('http://localhost:5173', 'http://localhost:8765',
                   'http://127.0.0.1:5173', 'http://127.0.0.1:8765')


def mutation_guard(request: Request):
    # TB-12: MobileGuard authenticated this device (token, Tailscale user, scope) and marked the request on
    # the server side; the browser origin there is the Mac's ts.net name. Only the gateway sets this mark.
    if getattr(request.state, 'conductor_mobile_device', None):
        return
    origin = request.headers.get('origin')
    same_local_origin = (request.url.hostname in ('localhost', '127.0.0.1')
                         and origin == f'{request.url.scheme}://{request.url.netloc}')
    # TB-50: the pairing page on the gateway. MobileGuard already checked Host against the ts.net name; behind
    # `tailscale serve` the scheme and port seen here are the local hop's, so compare the browser's https host only.
    if getattr(request.state, 'conductor_mobile_gateway', False) and origin == f'https://{request.url.hostname}':
        same_local_origin = True
    if (origin and not same_local_origin and origin not in ALLOWED_ORIGINS) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, detail={'code':'untrusted_origin','detail':'Cross-site mutation refused'})
