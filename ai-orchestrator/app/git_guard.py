"""TB-23 (Argon 4's risk note, 2026-10-14): what a run left in the workspace's `.git` that git itself would
execute or follow — hooks, config (helpers, hooksPath, filters, fsmonitor), attributes, alternates, also for
submodules and linked worktrees. Conductor's own git steps ignore hooks and refuse such config (TB-21), but the
user's own `git commit` in that folder would run them with the user's identity; the run reports the change so
the user can look before using git there. Never blocks, never hangs, never goes blind on an unreadable file."""
import hashlib
import os
import stat
from pathlib import Path

WATCHED = ('config', 'config.worktree', 'info/attributes', 'info/exclude', 'objects/info/alternates')
DIRS = ('hooks', 'info', 'objects/info', 'modules', 'worktrees')   # a symlinked one is reported itself
NESTED_LIMIT = 500   # ponytail: entries read under modules/ and worktrees/; more is reported as such


def _digest(path: Path) -> str:
    """Content + exec bit for a regular file; never opens a FIFO or device; errors become a stable value."""
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            return 'symlink:' + os.readlink(path)
        if not stat.S_ISREG(st.st_mode):
            return f'special:{stat.S_IFMT(st.st_mode):o}'
        h = hashlib.sha256()
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return f'special:{stat.S_IFMT(st.st_mode):o}'
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        # git runs a hook only when it is executable: chmod +x alone arms a hook
        return h.hexdigest() + (':x' if st.st_mode & 0o111 else '')
    except OSError as exc:   # chmod 000 must not make the check blind (Argon TB-23 P1)
        return f'err:{exc.errno}'


def _hooks(folder: Path, prefix: str, found: dict):
    try:
        for p in sorted(folder.iterdir()):
            if not p.name.endswith('.sample'):
                found[f'{prefix}/{p.name}'] = _digest(p)
    except OSError as exc:
        found[prefix] = f'err:{exc.errno}'


def _linked(git: Path, found: dict | None = None) -> Path | None:
    """The common git directory a linked worktree's `.git` file points to (TB-26: Conductor's own copies)."""
    try:
        st = git.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            return None
        with os.fdopen(os.open(git, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            text = f.read(4096).decode('utf-8', errors='replace').strip()
        if not text.startswith('gitdir:'):
            return None
        gitdir = (git.parent / text[7:].strip()).resolve()
        if not gitdir.is_dir():
            return None
        common = gitdir / 'commondir'
        try:
            cst = common.lstat()
        except FileNotFoundError:
            cst = None
        except OSError as exc:
            if found is not None:
                found['common:commondir'] = f'err:{exc.errno}'
            cst = None
        if cst is not None:
            if not stat.S_ISREG(cst.st_mode) or cst.st_size > 4096:
                if found is not None:
                    found['common:commondir'] = _digest(common)
            else:
                try:
                    with os.fdopen(os.open(common, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
                        fst = os.fstat(f.fileno())
                        if stat.S_ISREG(fst.st_mode):
                            target = (gitdir / f.read(4096).decode('utf-8', errors='replace').strip()).resolve()
                            if target.is_dir():
                                gitdir = target
                            elif found is not None:
                                found['common:commondir'] = _digest(common)
                        elif found is not None:
                            found['common:commondir'] = f'special:{stat.S_IFMT(fst.st_mode):o}'
                except OSError as exc:
                    if found is not None:
                        found['common:commondir'] = f'err:{exc.errno}'
        return gitdir if gitdir.is_dir() else None
    except OSError:
        return None


def fingerprint(workspace) -> dict | None:
    """{relative path: digest}; None when the folder is not a git repo root. A `.git` *file* (a linked worktree,
    a submodule checkout) is fingerprinted itself and, when it points to a git directory, that directory too:
    hooks and config there are shared with every worktree (TB-26)."""
    git = Path(workspace) / '.git'
    if git.is_symlink() or git.is_file():
        found = {'.git': _digest(git)}
        common = None if git.is_symlink() else _linked(git, found)
        if common is not None:
            found.update({'common:' + k[5:]: v for k, v in _git_dir(common).items()})
        return found
    if not git.is_dir():
        return None
    return _git_dir(git)


def _git_dir(git: Path) -> dict:
    found = {}
    for name in DIRS:
        d = git / name
        if d.is_symlink():
            found[f'.git/{name}'] = _digest(d)
    for name in WATCHED:
        p = git / name
        if os.path.lexists(p) and not any(f'.git/{d}' in found and name.startswith(d + '/') for d in DIRS):
            found[f'.git/{name}'] = _digest(p)
    if (git / 'hooks').is_dir() and '.git/hooks' not in found:
        _hooks(git / 'hooks', '.git/hooks', found)
    for top in ('modules', 'worktrees'):
        base = git / top
        if f'.git/{top}' in found or not base.is_dir():
            continue
        seen = 0
        for directory, dirs, files in os.walk(base, followlinks=False):
            rel = '.git/' + str(Path(directory).relative_to(git))
            dname = Path(directory).name.casefold()
            if dname == 'objects':
                dirs[:] = [d for d in dirs if d.casefold() == 'info']
                files = []
            for name in files:
                seen += 1
                low = name.casefold()
                if (
                    (dname == 'hooks' and not low.endswith('.sample'))
                    # commondir/gitdir are pointers git rewrites on every `worktree add/remove` (Conductor's
                    # own copies): watching them would flag every parallel run; they execute nothing
                    or low in ('config', 'config.worktree')
                    or (dname == 'info' and low in ('attributes', 'exclude'))
                    or (rel.casefold().endswith('/objects/info') and low == 'alternates')
                ):
                    found[f'{rel}/{name}'] = _digest(Path(directory) / name)
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    found[f'{rel}/{name}'] = _digest(Path(directory) / name)
            if dname != 'objects':
                dirs[:] = [d for d in dirs if d.casefold() not in ('logs', 'refs', 'lfs')]
            if seen > NESTED_LIMIT:
                found[f'.git/{top}'] = 'too_many_entries'
                break
    return found


def changes(before: dict | None, after: dict | None) -> list[str]:
    """Paths added, removed or changed; a repo that appeared or vanished counts as `.git`."""
    if before is None and after is None:
        return []
    if before is None or after is None:
        return ['.git']
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
