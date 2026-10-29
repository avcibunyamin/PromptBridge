"""TB-26 (K-034): a work may run in its own git worktree instead of the user's folder.

The copy is `~/.conductor/worktrees/<repo>-<id8>` on a new `conductor/…` branch from the source's HEAD; runs,
locks, snapshots, previews and the GitHub steps all use it like any work folder. Bringing the result back is a
human step on the Mac: commit the copy's changes on its branch, then `merge --no-ff` into the source's current
branch while the source is clean; a conflict is aborted and nothing changes. Git runs with TB-21's guards (no hooks,
no fsmonitor, the source's own config must pass the allow-list)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from app import github as gh
from app import works_store as ws
from app.github import GitHubError, git

ROOT_ENV = 'CONDUCTOR_WORKTREES'   # tests point this at a temp folder
MAX_COPIES = 10   # Argon P2: open copies per source (a phone can start copies; it cannot remove them)
# ponytail: one lock for every apply/remove — two merges into one source must never interleave (a failing one's
# `merge --abort` would undo the other). Per-source locks if this ever becomes a bottleneck.
_GIT_STEP = threading.Lock()


def root() -> Path:
    import os
    return Path(os.environ.get(ROOT_ENV) or Path.home() / '.conductor' / 'worktrees')


def _child_repos(path: Path, limit: int = 8) -> list[str]:
    """K-055: git repositories directly inside a folder that is not one, so the panel can offer them."""
    found = []
    try:
        children = sorted(path.resolve().iterdir())
    except OSError:
        return []
    for p in children:
        try:   # one unreadable sibling (EPERM under macOS privacy) must not hide the others
            if not p.is_symlink() and p.is_dir() and (p / '.git').exists():
                found.append(str(p))
        except OSError:
            continue
        if len(found) == limit:
            break
    return found


def _repo_root(path: Path) -> Path:
    try:
        top = Path(git(path, 'rev-parse', '--show-toplevel').strip()).resolve()
    except GitHubError:
        top = None
    if top != path.resolve():
        raise GitHubError('not_a_repo', 'Ayrı kopya için klasör bir git deposunun kökü olmalı', 422, repos=_child_repos(path))
    problems = gh.unsafe_config(path)
    if problems:
        raise GitHubError('unsafe_git_config', 'Depo ayarında izin verilmeyen anahtarlar: ' + ', '.join(problems))
    if gh._head(path) is None:
        raise GitHubError('empty_repo', "Depoda henüz commit yok; önce bir ilk commit oluşturun")
    return top


def plan(source: str, work_id: str, title: str) -> tuple[Path, str]:
    """Where the copy goes and its branch; deterministic per work so a retried create reuses it."""
    src = _repo_root(Path(source))
    branch = gh.branch_for(title, work_id)
    gh._check_branch_name(branch)
    return root() / f'{gh.slug(src.name, 40)}-{work_id.replace("-", "")[:8].lower()}', branch


def open_copies(conn, source: str) -> int:
    return conn.execute("""SELECT count(*) FROM work_events e WHERE e.type='work_created'
        AND json_extract(e.payload, '$.isolated_from')=? AND NOT EXISTS (SELECT 1 FROM work_events r
        WHERE r.work_id=e.work_id AND r.type='isolation_removed')""", (source,)).fetchone()[0]


def create(source: str, work_id: str, title: str, conn=None) -> tuple[str, str]:
    src = _repo_root(Path(source))
    if conn is not None and open_copies(conn, str(src)) >= MAX_COPIES:
        raise GitHubError('too_many_copies', f'Bu projede zaten {MAX_COPIES} açık kopya var; önce eskileri kaldırın', 409)
    path, branch = plan(source, work_id, title)
    if path.exists():   # a create that died after `worktree add`: reuse only our own copy of this source
        listed = git(src, 'worktree', 'list', '--porcelain')
        if f'worktree {path.resolve()}\n' not in listed + '\n':
            raise GitHubError('worktree_exists', f'Kopya klasörü zaten var: {path}')
        return str(path.resolve()), git(path, 'branch', '--show-current').strip()
    try:
        git(src, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch)
    except GitHubError:
        pass
    else:
        raise GitHubError('branch_exists', 'Bu adla bir dal zaten var: ' + branch, 409)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(src, 'worktree', 'add', '-b', branch, str(path), 'HEAD')
    except GitHubError:   # Argon P3: a half-made copy leaves no record or branch behind (the branch is this work's own)
        for args in (('worktree', 'remove', '--force', str(path)), ('worktree', 'prune'), ('branch', '-D', branch)):
            try:
                git(src, *args)
            except GitHubError:
                pass
        raise
    return str(path.resolve()), branch


def info(conn, work_id) -> dict | None:
    row = conn.execute("SELECT payload FROM work_events WHERE work_id=? AND type='work_created' ORDER BY seq LIMIT 1",
                       (work_id,)).fetchone()
    data = json.loads(row[0]) if row else {}
    if not data.get('isolated_from'):
        return None
    removed = conn.execute("SELECT 1 FROM work_events WHERE work_id=? AND type='isolation_removed'", (work_id,)).fetchone()
    return {'source': data['isolated_from'], 'branch': data['branch'], 'removed': bool(removed)}


def _isolated(conn, work_id):
    work, path = gh._work(conn, work_id)
    iso = info(conn, work_id)
    if not iso or iso['removed']:
        raise GitHubError('not_isolated', 'Bu çalışma ayrı bir kopyada değil', 409)
    gh._idle(conn, path)
    src = Path(iso['source'])
    # A run may have rewritten the copy's `.git` file or switched its branch: then a commit here would land in
    # another repo or on another branch. The copy must still share the source's git dir and sit on its own branch.
    common = lambda p: Path(git(p, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()).resolve()
    if common(path) != common(src) or git(path, 'branch', '--show-current').strip() != iso['branch']:
        raise GitHubError('copy_moved', 'Kopya artık kendi dalında ya da kaynağın deposunda değil; elle bakın', 409)
    if gh.unsafe_config(path):   # the copy's local + worktree config (shared with the source) must pass too
        raise GitHubError('unsafe_git_config', 'Kopyanın git ayarında izin verilmeyen anahtarlar var')
    return work, path, src, iso


def _source_dirty(src: Path) -> list[str]:
    out = git(src, 'status', '--porcelain=v1', '-z', '--untracked-files=no')
    return [f[3:] for f in out.split('\0') if f]


def apply_preview(conn, work_id) -> dict:
    work, path, src, iso = _isolated(conn, work_id)
    _repo_root(src)
    gh._idle(conn, src)   # never merge into a folder a run is writing in (a plain work on the source)
    target = git(src, 'branch', '--show-current').strip()
    files = [f['path'] for f in gh.changes(path)]
    log = git(path, 'log', '--format=%H %s', f'{gh._head(src)}..HEAD')
    commits = [line.split(' ', 1) for line in log.splitlines() if line]
    committed = git(path, 'diff', '--name-only', '-z', f'{gh._head(src)}...HEAD').split('\0')
    # Argon P3: files that change what git itself does later (submodules, attributes) or what CI runs
    attention = sorted({f for f in [*files, *committed] if f and (Path(f).name in ('.gitmodules', '.gitattributes')
                                                                    or f.startswith(gh.WORKFLOWS))})
    view = {'source': str(src), 'target': target or None, 'branch': iso['branch'], 'uncommitted': files,
            'commits': [{'sha': c[0], 'subject': c[1] if len(c) > 1 else ''} for c in commits],
            'source_dirty': _source_dirty(src), 'source_head': gh._head(src), 'copy_head': gh._head(path),
            'attention': attention}
    view['digest'] = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
    return view


def apply(conn, work_id, expected_digest: str) -> dict:
    with _GIT_STEP:
        return _apply(conn, work_id, expected_digest)


def _apply(conn, work_id, expected_digest: str) -> dict:
    view = apply_preview(conn, work_id)
    if view['digest'] != expected_digest:
        raise GitHubError('stale', 'Kopya ya da ana klasör değişti; önizlemeyi yenileyin')
    if not view['target']:
        raise GitHubError('detached', 'Ana klasör bir dalda değil (detached HEAD)')
    if view['source_dirty']:
        raise GitHubError('source_dirty', 'Ana klasörde kaydedilmemiş değişiklik var: ' + ', '.join(view['source_dirty'][:5]))
    if not view['uncommitted'] and not view['commits']:
        raise GitHubError('nothing', 'Getirilecek değişiklik yok')
    work, path, src, iso = _isolated(conn, work_id)
    in_progress = lambda name: os.path.lexists(src / git(src, 'rev-parse', '--git-path', name).strip())
    if any(map(in_progress, ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'))):
        raise GitHubError('source_busy', 'Ana klasörde yarım bir birleştirme/rebase var; önce onu bitirin', 409)
    who = _who(path)
    if view['uncommitted']:
        git(path, 'add', '-A')
        git(path, *who, 'commit', '--no-verify', '-F', '-', stdin=f"{work['title']}\n\nConductor çalışması {work_id}")
    # git's ort merge overwrites ignored files even with --no-overwrite-ignore (git 2.50): refuse instead
    added = git(src, 'diff', '--name-only', '-z', '--diff-filter=A', f"HEAD...{iso['branch']}").split('\0')
    clash = [f for f in added if f and os.path.lexists(src / f)]
    if clash:
        raise GitHubError('source_untracked', 'Ana klasörde getirilecek dosyalarla aynı adda yerel dosya var: '
                          + ', '.join(clash[:5]), 409)
    try:
        git(src, *who, 'merge', '--no-ff', '--no-edit', '-m', f"Conductor: {work['title']}", iso['branch'])
    except GitHubError as exc:
        try:
            if in_progress('MERGE_HEAD'):   # a merge git refused up front left nothing to undo
                git(src, 'merge', '--abort')
        except GitHubError:
            raise GitHubError('merge_abort_failed', 'Birleştirme çakıştı ve geri alınamadı; ana klasöre elle bakın: ' + str(exc)) from None
        raise GitHubError('merge_conflict', 'Birleştirme çakıştı; ana klasör değişmedi. Ayrıntı: ' + str(exc)) from None
    head = gh._head(src)
    ws.append_event(conn, work_id, 'isolation_applied', {'branch': iso['branch'], 'target': view['target'], 'commit': head})
    return {'target': view['target'], 'commit': head}


def _who(path) -> list[str]:
    """The user's own identity when git has one; otherwise a local-only name (no network, nothing published)."""
    try:
        git(path, 'config', '--get', 'user.email')
        return []
    except GitHubError:
        return ['-c', 'user.name=Conductor', '-c', 'user.email=conductor@localhost']


def remove(conn, work_id) -> dict:
    with _GIT_STEP:
        return _remove(conn, work_id)


def _remove(conn, work_id) -> dict:
    work, path, src, iso = _isolated(conn, work_id)
    if not work['archived_at']:
        raise GitHubError('not_archived', 'Kopyayı kaldırmadan önce çalışmayı arşivleyin')
    if gh.changes(path):
        raise GitHubError('copy_dirty', 'Kopyada kaydedilmemiş değişiklik var; önce ana klasöre getirin')
    git(src, 'worktree', 'remove', str(path))
    try:   # Argon P3: a branch fully merged into the source's branch goes too; `-d` refuses anything else
        git(src, 'branch', '-d', iso['branch'])
        deleted = True
    except GitHubError:
        deleted = False
    ws.append_event(conn, work_id, 'isolation_removed', {'branch': iso['branch'], 'branch_deleted': deleted})
    return {'removed': True, 'branch': iso['branch'], 'branch_deleted': deleted}


# ── API (Mac only: the mobile gateway's allow-list has no such path; mac_only is defence in depth) ───────────────
from fastapi import APIRouter, Depends, Request   # noqa: E402
from pydantic import BaseModel, ConfigDict, Field   # noqa: E402

from app.security import mutation_guard   # noqa: E402

router = APIRouter(prefix='/api/isolation', dependencies=[Depends(mutation_guard), Depends(gh.mac_only)])


class Apply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    digest: str = Field(min_length=64, max_length=64)


@router.get('/works/{work_id}/apply')
def get_apply(work_id: str, request: Request):
    return gh._call(request, apply_preview, work_id)


@router.post('/works/{work_id}/apply')
def post_apply(work_id: str, body: Apply, request: Request):
    return gh._call(request, apply, work_id, body.digest)


@router.post('/works/{work_id}/remove')
def post_remove(work_id: str, request: Request):
    return gh._call(request, remove, work_id)
