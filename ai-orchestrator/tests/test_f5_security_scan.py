"""F5-Kapanış gate F: every mutating endpoint of the real app keeps mutation_guard and refuses a foreign origin.

Scans the routes instead of listing them, so a new POST/PUT/PATCH/DELETE without the guard fails here."""
import asyncio
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from app.security import mutation_guard

MUTATING = {'POST', 'PUT', 'PATCH', 'DELETE'}
# Gateway-only: 404 on 8765 by design; on the gateway MobileGuard (token, Tailscale user, rate limit) guards it.
GATEWAY_ONLY = {('POST', '/api/mobile/pair')}


def _api_routes(routes):
    # FastAPI 0.141 keeps included routers lazily (`original_router`); walk into them.
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, 'original_router'):
            yield from _api_routes(r.original_router.routes)


def _deps(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _deps(d)


class MutationGuardScan(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name); (root / '.system').mkdir()
        (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        patcher = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root)}); patcher.start(); self.addCleanup(patcher.stop)
        from app.api import create_app
        self.app = create_app()
        self.routes = [(m, r) for r in _api_routes(self.app.routes) for m in r.methods & MUTATING]

    def test_every_mutating_route_has_the_guard(self):
        self.assertGreater(len(self.routes), 20)                       # the scan really sees the app
        missing = [(m, r.path) for m, r in self.routes
                   if (m, r.path) not in GATEWAY_ONLY and mutation_guard not in set(_deps(r.dependant))]
        self.assertEqual(missing, [])

    def test_foreign_origin_is_refused_everywhere(self):
        client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        for method, route in self.routes:
            path = re.sub(r'\{[^}]+\}', 'x', route.path)
            with self.subTest(method=method, path=route.path):
                gateway_only = (method, route.path) in GATEWAY_ONLY
                body = {'code': '123456'} if gateway_only else {}           # a valid body, so 404 is the route's answer
                r = client.request(method, path, json=body, headers={'Origin': 'https://evil.example'})
                self.assertEqual(r.status_code, 404 if gateway_only else 403)

    def test_loopback_only_asgi_middleware(self):
        async def _call(client, scope_type='http'):
            scope = {
                'type': scope_type,
                'asgi': {'version': '3.0'},
                'http_version': '1.1',
                'method': 'GET',
                'scheme': 'http' if scope_type == 'http' else 'ws',
                'path': '/health',
                'raw_path': b'/health',
                'query_string': b'',
                'root_path': '',
                'headers': [(b'host', b'127.0.0.1:8765')],
                'client': client,
                'server': ('127.0.0.1', 8765),
            }
            sent = []

            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}

            async def send(message):
                sent.append(message)

            await self.app(scope, receive, send)
            start = next(m for m in sent if m['type'].endswith('http.response.start'))
            body = b''.join(m.get('body', b'') for m in sent if m['type'].endswith('http.response.body'))
            return start['status'], body

        async def _run():
            for scope_type in ('http', 'websocket'):
                with self.subTest(scope_type=scope_type):
                    status, body = await _call(('192.168.1.5', 1234), scope_type=scope_type)
                    self.assertEqual(status, 403)
                    self.assertEqual(body, b'loopback only')

            for allowed in (('127.0.0.1', 1), ('::1', 1), None):
                with self.subTest(client=allowed):
                    status, _ = await _call(allowed)
                    self.assertEqual(status, 200)

        asyncio.run(_run())


if __name__ == '__main__':
    unittest.main()
