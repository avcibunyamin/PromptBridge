"""Does a plain HTTP server on 127.0.0.1 answer here? Tests that start a real local server skip when it does not
(observed on GitHub's macOS runners, where even `python -m http.server` timed out on loopback)."""
import functools
import socket
import subprocess
import sys
import time
import urllib.request


@functools.lru_cache(maxsize=None)
def loopback_http_ok(wait: float = 8.0) -> bool:
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    server = subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1):
                    return True
            except OSError:
                time.sleep(0.2)
        return False
    finally:
        server.terminate(); server.wait(5)


SKIP_REASON = 'loopback HTTP does not answer in this environment'
