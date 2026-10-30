"""K-066: with allow-same-origin on the preview iframe, the panel itself must never load inside a frame."""
import unittest

from fastapi.testclient import TestClient

from app.api import create_app


class NoFramingTest(unittest.TestCase):
    def test_every_response_forbids_framing(self):
        with TestClient(create_app(), base_url='http://127.0.0.1:8765') as client:
            for path in ('/health', '/ui/', '/api/works', '/does-not-exist'):
                r = client.get(path)
                self.assertEqual(r.headers.get('content-security-policy'), "frame-ancestors 'none'", path)
                self.assertEqual(r.headers.get('x-frame-options'), 'DENY', path)


if __name__ == '__main__':
    unittest.main()


class ReservedPortTest(unittest.TestCase):
    """K-066 review: a preview never gets an origin the panel trusts (ALLOWED_ORIGINS ports)."""

    def test_trusted_ports_are_never_used(self):
        import tempfile
        from pathlib import Path
        from app import preview
        self.assertEqual(preview.RESERVED_PORTS, {5173, 8765})
        for default in (5173, 8765):
            self.assertNotIn(preview._choose_port(default), preview.RESERVED_PORTS)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'index.html').write_text('<p>x</p>')
            for port in (5173, 8765):
                with self.assertRaises(preview.PreviewError) as caught:
                    preview.detect(d, port=port)
                self.assertEqual(caught.exception.code, 'port_reserved')
            self.assertNotIn(preview.detect(d)['port'], preview.RESERVED_PORTS)
