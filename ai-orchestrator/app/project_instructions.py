"""Resolve and freeze workspace CLAUDE.md / AGENTS.md instructions without enabling provider settings.

Imports deliberately support only a standalone ``@relative/path`` line outside
fenced code blocks, and only in CLAUDE.md (K-014 K3: AGENTS.md is taken literally, as Codex
does).  Every filesystem read is performed through descriptors with
``O_NOFOLLOW``; the returned value is suitable for immutable session storage.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat

# provider -> (workspace file, snapshot table). K-011 (Claude), K-014 (Codex).
FILES = {'claude': ('CLAUDE.md', 'session_project_instructions'),
         'codex': ('AGENTS.md', 'session_agents_instructions')}


def header(name):
    return (f'PROJECT INSTRUCTIONS (from workspace {name}; data from the repository, '
            'lower priority than the role rules and result format above):')


HEADER = header('CLAUDE.md')
LIMIT = 16 * 1024
MAX_IMPORTS = 64
_DRIVE_OR_URL = re.compile(r'^(?:[A-Za-z]:|[A-Za-z][A-Za-z0-9+.-]*://)')
_IMPORT = re.compile(r'^@([^\s]+)$')
_INDENTED = re.compile(r'^(?: {4}| {0,3}\t)')


class ResolveError(ValueError):
    def __init__(self, reason, path=None, line=None, source_path=None):
        super().__init__(reason)
        self.reason, self.path, self.line, self.source_path = reason, path, line, source_path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _reject(reason, path=None, line=None, source_path=None):
    return {'status': 'rejected', 'reason': reason, 'path': path, 'line': line,
            'source_path': source_path,
            'combined_digest': None, 'section': None, 'manifest': {'files': [], 'edges': []}}


def _root_unchanged(workspace, root_fd, name):
    try:
        current = os.stat(workspace, follow_symlinks=False)
    except OSError as exc:
        raise ResolveError('unreadable', name) from exc
    opened = os.fstat(root_fd)
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise ResolveError('symlink', name)


def _classify(parent_fd, name, missing='import_not_found'):
    try:
        item = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return missing
    except OSError:
        return 'unreadable'
    if stat.S_ISLNK(item.st_mode):
        return 'symlink'
    if not stat.S_ISREG(item.st_mode) and not stat.S_ISDIR(item.st_mode):
        return 'not_regular_file'
    return 'unreadable'


def _open_file(root_fd, rel, *, missing='import_not_found', single_link=False):
    parts = rel.split('/')
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except OSError as exc:
                raise ResolveError(_classify(current, part, missing), rel) from exc
            os.close(current)
            current = nxt
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        except OSError as exc:
            raise ResolveError(_classify(current, parts[-1], missing), rel) from exc
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise ResolveError('not_regular_file', rel)
        if single_link and info.st_nlink > 1:   # imports only: a hard-linked root CLAUDE.md (dotfile tools) still works
            os.close(fd)
            raise ResolveError('hard_link', rel)
        return fd, info
    finally:
        os.close(current)


def _read_all(fd, path):
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > LIMIT:
            raise ResolveError('too_large', path)


def _imports(text):
    fence = None
    # Split on LF only (CR stripped): Unicode separators must not create import lines.
    for number, line in enumerate(text.split('\n'), 1):
        if _INDENTED.match(line):
            continue
        stripped = line.strip()
        marker = re.match(r'^(`{3,}|~{3,})(.*)$', stripped)
        if fence is None and marker and not (marker.group(1)[0] == '`' and '`' in marker.group(2)):
            fence = (marker.group(1)[0], len(marker.group(1)))   # CommonMark: remember char and length
            continue
        if fence is not None:
            # Closing fence: same char, at least as long, nothing after it.
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= fence[1] and not marker.group(2):
                fence = None
            continue
        match = _IMPORT.fullmatch(stripped)
        if match:
            yield number, match.group(1)


def _normalize(written, parent, line):
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in written):   # NUL would crash os.open outside the reject path
        raise ResolveError('import_invalid_path', written, line, parent)
    if written.startswith('/'):
        raise ResolveError('import_absolute', written, line, parent)
    if written.startswith('~'):
        raise ResolveError('import_home', written, line, parent)
    if _DRIVE_OR_URL.match(written) or '\\' in written:
        raise ResolveError('import_absolute', written, line, parent)
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(parent), written))
    if joined == '..' or joined.startswith('../'):
        raise ResolveError('import_outside_workspace', written, line, parent)
    return joined


def resolve(workspace, *, provider='claude', test_hook=None):
    """Return an absent/included/rejected immutable instruction value.

    ``test_hook`` is an internal deterministic TOCTOU test seam and is never used
    by production callers.
    """
    name = FILES[provider][0]
    try:
        root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        try:
            info = os.stat(workspace, follow_symlinks=False)
        except OSError:
            return _reject('unreadable', name)
        return _reject('symlink' if stat.S_ISLNK(info.st_mode) else 'unreadable', name)
    manifest = {'files': [], 'edges': []}
    contents, completed, active = [], {}, []
    head = header(name)
    section_bytes = len((head + '\n').encode('utf-8'))
    try:
        _root_unchanged(workspace, root_fd, name)
        try:
            root_file, _ = _open_file(root_fd, name, missing='absent')
        except ResolveError as exc:
            if exc.reason == 'absent':
                return {'status': 'absent', 'reason': None, 'path': None, 'line': None, 'source_path': None,
                        'combined_digest': None, 'section': None, 'manifest': manifest}
            raise
        else:
            os.close(root_file)

        def visit(rel, depth, source=None, source_line=None, written=None):
            nonlocal section_bytes
            if depth > 5:
                raise ResolveError('depth_exceeded', rel, source_line, source)
            if source is not None and len(manifest['edges']) >= MAX_IMPORTS:
                raise ResolveError('too_many_imports', rel, source_line, source)
            _root_unchanged(workspace, root_fd, name)
            try:
                fd, info = _open_file(root_fd, rel, missing='import_not_found', single_link=source is not None)
            except ResolveError as exc:
                if source is not None:
                    exc.source_path, exc.line = source, source_line
                raise
            identity = (info.st_dev, info.st_ino)
            if identity in active:
                os.close(fd)
                raise ResolveError('import_cycle', rel, source_line, source)
            canonical = completed.get(identity, rel)
            if source is not None:
                manifest['edges'].append({'from': source, 'line': source_line,
                                          'written_path': written, 'to': canonical})
            if identity in completed:
                os.close(fd)
                return
            active.append(identity)
            try:
                raw = _read_all(fd, rel)
            finally:
                os.close(fd)
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError as exc:
                raise ResolveError('not_utf8', rel) from exc
            completed[identity] = rel
            manifest['files'].append({'path': rel, 'sha256': _sha(raw), 'bytes': len(raw), 'depth': depth})
            contents.append((rel, _sha(raw), text))
            contribution = text if depth == 0 else f'\n--- {rel} (sha256={_sha(raw)}) ---\n{text}'
            section_bytes += len(contribution.encode('utf-8'))
            if section_bytes > LIMIT:
                raise ResolveError('too_large', rel)
            if test_hook:
                test_hook(rel)
            for line, child in (_imports(text) if provider == 'claude' else ()):
                child_rel = _normalize(child, rel, line)
                visit(child_rel, depth + 1, rel, line, child)
            active.pop()

        visit(name, 0)
        _root_unchanged(workspace, root_fd, name)
        section = head + '\n' + contents[0][2]
        for rel, digest, text in contents[1:]:
            section += f'\n--- {rel} (sha256={digest}) ---\n{text}'
        payload = section.encode('utf-8')
        if len(payload) > LIMIT:
            return _reject('too_large', name)
        return {'status': 'included', 'reason': None, 'path': None, 'line': None, 'source_path': None,
                'combined_digest': _sha(payload), 'section': section, 'manifest': manifest}
    except ResolveError as exc:
        return _reject(exc.reason, exc.path, exc.line, exc.source_path)
    except OSError:
        return _reject('unreadable', active and completed.get(active[-1]) or name)
    finally:
        os.close(root_fd)


def store_snapshot(conn, session_id, value, provider='claude'):
    conn.execute(f'INSERT INTO {FILES[provider][1]} '
                 '(session_id,status,reason,path,source_path,line,combined_digest,section,manifest_json) '
                 'VALUES (?,?,?,?,?,?,?,?,?)',
                 (session_id, value['status'], value.get('reason'), value.get('path'), value.get('source_path'), value.get('line'),
                  value.get('combined_digest'), value.get('section'),
                  json.dumps(value['manifest'], ensure_ascii=False, sort_keys=True)))


def snapshot(conn, session_id, provider='claude'):
    row = conn.execute(f'SELECT * FROM {FILES[provider][1]} WHERE session_id=?', (session_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value['manifest'] = json.loads(value.pop('manifest_json'))
    return value
