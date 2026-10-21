"""Bounded hash evidence; never follows symlinks or claims authorship/protection."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess

IGNORED = {'.git', '.venv', 'node_modules', '__pycache__', '.system'}
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 10000, 20*1024*1024, 256*1024*1024


SCOPE_VERSION = 2


def scope_of(workspace, excluded=()):
    """What a snapshot covered; two snapshots are only comparable with equal scopes."""
    return {'version': SCOPE_VERSION, 'root': str(Path(workspace)), 'ignored': sorted(IGNORED),
            'excluded': sorted(str(Path(p).resolve()) for p in excluded),
            'limits': [MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES]}


def snapshot(workspace, *, excluded=()):
    root = Path(workspace)
    skip = {str(Path(p).resolve()) for p in excluded}
    files, unknown = {}, []
    total = 0
    scope = scope_of(workspace, excluded)
    # A missing/unreadable root must never look like a complete, empty workspace.
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        return {'files': files, 'unknown': ['root_unavailable'], 'complete': False, 'scope': scope}
    walk_errors = []
    def on_walk_error(error):
        # os.walk skips unreadable directories silently unless onerror records them.
        try:
            walk_errors.append('walk_error:' + str(Path(error.filename).relative_to(root)))
        except (TypeError, ValueError):
            walk_errors.append('walk_error')
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=on_walk_error):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        links = [d for d in dirs if (Path(directory)/d).is_symlink()]
        dirs[:] = [d for d in dirs if d not in links]
        for name in sorted(names + links):
            path = Path(directory)/name
            rel = str(path.relative_to(root))
            if str(path.absolute()) in skip:
                continue
            if len(files) + len(unknown) >= MAX_FILES:
                return {'files': files, 'unknown': unknown + walk_errors + ['scan_limit'], 'complete': False, 'scope': scope}
            try:
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode):
                    files[rel] = {'kind':'symlink','sha256':hashlib.sha256(os.readlink(path).encode()).hexdigest()}
                    continue
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES or total + before.st_size > MAX_TOTAL_BYTES:
                    unknown.append(rel); continue
                # O_NOFOLLOW closes the symlink swap window for a file itself.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev,before.st_ino):
                        unknown.append(rel); continue
                    data = stream.read(MAX_FILE_BYTES+1)
                    after = os.fstat(stream.fileno())
                if len(data)>MAX_FILE_BYTES or (opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    unknown.append(rel); continue
                total += len(data)
                files[rel] = {'kind':'file','sha256':hashlib.sha256(data).hexdigest()}
            except OSError:
                unknown.append(rel)
    unknown += walk_errors
    return {'files':files,'unknown':unknown,'complete':not unknown,'scope':scope}


def differences(before, after):
    changes = []
    for path in sorted(set(before['files']) | set(after['files'])):
        old,new = before['files'].get(path),after['files'].get(path)
        if old != new:
            changes.append({'path':path,'before':old,'after':new,
                            'attribution':'unknown', 'certainty': 'unknown' if path in before['unknown'] or path in after['unknown'] or (old is None and not before['complete']) or (new is None and not after['complete']) else 'hash'})
    return changes


def git_baseline(workspace):
    try:
        result = subprocess.run(['git','-C',workspace,'status','--porcelain=v1','-z'],
                                capture_output=True,timeout=3)
        if result.returncode == 0:
            return result.stdout.decode('utf-8',errors='replace').split('\0')[:MAX_FILES]
    except (OSError,subprocess.TimeoutExpired):
        pass
    return 'unknown'
