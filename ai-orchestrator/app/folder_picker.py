"""K-041: pick a project folder in Finder from the Mac panel in a browser (the Mac app uses its own native panel).

`osascript` shows the standard “choose folder” dialog on this Mac: fixed argv, no shell, one dialog at a time,
bounded. Only on loopback behind mutation_guard; the phone gateway does not list this route.
"""
import os
import subprocess
import threading
from fastapi import APIRouter, Depends, HTTPException
from app.security import mutation_guard

router = APIRouter(prefix='/api/system', tags=['system'])
OSASCRIPT = '/usr/bin/osascript'
TIMEOUT = 300.0                       # the person is choosing; a forgotten dialog is closed after this
# AppleScript's own 120 s event timeout would close the dialog first; it gets the same 300 s as the process.
SCRIPT = ('activate', 'with timeout of 300 seconds',
          'set chosen to POSIX path of (choose folder with prompt "PromptBridge: proje klasörünü seçin")',
          'end timeout', 'return chosen')
_one = threading.Lock()


def choose():
    """(path or None, reason). reason: None | cancelled | busy | timeout | unavailable."""
    if not _one.acquire(blocking=False):
        return None, 'busy'
    try:
        args = [OSASCRIPT]
        for line in SCRIPT:
            args += ['-e', line]
        try:
            p = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT,
                               start_new_session=True)
        except subprocess.TimeoutExpired:
            return None, 'timeout'
        except OSError:
            return None, 'unavailable'
        if p.returncode != 0:
            return None, 'cancelled' if '-128' in p.stderr else 'timeout' if '-1712' in p.stderr else 'unavailable'
        path = p.stdout.strip()
        path = path[:-1] if len(path) > 1 and path.endswith('/') else path
        if not os.path.isabs(path) or not os.path.isdir(path):
            return None, 'unavailable'
        return path, None
    finally:
        _one.release()


@router.post('/choose-folder', dependencies=[Depends(mutation_guard)])
def choose_folder():
    path, reason = choose()
    if reason == 'busy':
        raise HTTPException(409, {'code': 'picker_open', 'detail': 'Klasör penceresi zaten açık.'})
    return {'path': path, 'cancelled': reason == 'cancelled', 'error': reason if reason not in (None, 'cancelled') else None}
