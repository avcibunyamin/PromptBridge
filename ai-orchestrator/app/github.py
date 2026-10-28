"""TB-21 (K-026/3): GitHub through this Mac's `gh` session.

Conductor never reads or stores the token: it runs `gh` and `git` with fixed argv, no shell, in their own process
group and with a time limit. The workspace is agent-written, so every git call disables hooks and fsmonitor, and a
repository whose own config sets anything beyond a short allowlist (hooks, helpers, url rewrites, filters, includes…)
is refused. Push and PR each need a human confirmation bound to a digest of what the confirmation showed."""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from app import loop_store, store, works_store as ws, user_settings
from app.adapters.process import executable
from app.security import mutation_guard

GITHUB = 'https://github.com/'   # tests point this at a local folder of bare repositories
TIMEOUT, CLONE_TIMEOUT = 60, 600
PART = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?')
# What a cloned or `git init`-ed repository may set in its own config; anything else is refused (agent-writable).
LOCAL_OK = re.compile(r'core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks)'
                      r'|remote\.(origin|upstream)\.(url|fetch|gh-resolved)|branch\..+\.(remote|merge)'
                      r'|user\.(name|email)|extensions\.(objectformat|worktreeconfig)')
SECRET = ('.env', '.env.*', '*.env', '.netrc', '.npmrc', '*.pem', '*.key', '*.p12', '*.pfx', 'id_*', 'credentials*')   # K3: listed, never preselected
WORKFLOWS = '.github/workflows/'                                         # K2: push needs a separate confirmation
TOKEN = re.compile(r'\b(gh[opsur]_|github_pat_)[A-Za-z0-9_]+')
URL_AUTH = re.compile('://[^/\\s?#\'"`<>]+@')


class GitHubError(Exception):
    def __init__(self, code, detail, status=409):
        super().__init__(detail)
        self.code, self.status = code, status


# ── validation (pure) ──────────────────────────────────────────────────────────

def repo_name(value: str) -> str:
    """`owner/name`, each part starting and ending with a letter or digit (no `-x` flag look-alikes, no `.`/`..`)."""
    owner, sep, name = value.partition('/')
    if not (sep and PART.fullmatch(owner) and PART.fullmatch(name)) or name.lower().endswith('.git'):
        raise GitHubError('invalid_repo', 'Depo adı sahip/ad biçiminde olmalı', 422)
    return value


def slug(title: str, limit: int = 30) -> str:
    text = unicodedata.normalize('NFKD', title.replace('ı', 'i').replace('İ', 'I')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:limit].strip('-') or 'calisma'


def branch_for(title: str, work_id: str, now: float | None = None) -> str:
    return f"conductor/{time.strftime('%Y%m%d-%H%M', time.localtime(now))}-{slug(title)}-{work_id[:6].lower()}"


def is_secret(path: str) -> bool:
    name = Path(path).name.lower()
    return any(fnmatch.fnmatch(name, p) for p in SECRET)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def remote_repo(url: str) -> str | None:
    for prefix in (GITHUB, 'git@github.com:', 'ssh://git@github.com/'):
        if url.startswith(prefix):
            rest = url[len(prefix):].rstrip('/')
            rest = rest[:-4] if rest.endswith('.git') else rest
            try:
                return repo_name(rest)
            except GitHubError:
                return None
    return None


# ── processes ─────────────────────────────────────────────────────────────────

def gh_bin():
    return executable('CONDUCTOR_GH_BIN', 'gh', ('/opt/homebrew/bin/gh', '/usr/local/bin/gh'))


def git_bin():
    return executable('CONDUCTOR_GIT_BIN', 'git')


def _env():
    # Conductor's own steps use the user's session; only inherited git plumbing variables are dropped.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return {**env, 'GIT_TERMINAL_PROMPT': '0', 'GH_PROMPT_DISABLED': '1', 'GH_NO_UPDATE_NOTIFIER': '1',
            'GH_SPINNER_DISABLED': '1', 'NO_COLOR': '1'}


def run(argv, *, cwd=None, stdin='', timeout=None):
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=_env(), start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout or TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
            proc.poll()
        raise GitHubError('timeout', f'{Path(argv[0]).name} zaman aşımına uğradı', 504) from None
    return proc.returncode, out, err


def _say(err: str) -> str:
    return URL_AUTH.sub('://***@', TOKEN.sub(r'\1***', err.strip()))[-500:]


def gh(*args, stdin='', timeout=None, ok=(0,)):
    try:
        binary = gh_bin()
    except FileNotFoundError:
        raise GitHubError('gh_missing', 'GitHub CLI (gh) kurulu değil', 409) from None
    code, out, err = run([binary, *args], stdin=stdin, timeout=timeout)
    if code not in ok:
        raise GitHubError('gh_auth' if code == 4 else 'gh_failed', _say(err) or f'gh çıkış kodu {code}', 409)
    return code, out


def git(repo, *args, stdin='', timeout=None, push=False):
    """Hooks and fsmonitor never run; for a push the helper list is reset to `gh auth git-credential` only."""
    flags = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false']
    if push:
        flags += ['-c', 'credential.helper=', '-c', f'credential.helper=!{shlex.quote(gh_bin())} auth git-credential']
    code, out, err = run([git_bin(), *flags, '-C', str(repo), *args], stdin=stdin, timeout=timeout)
    if code:
        raise GitHubError('git_failed', _say(err) or f'git çıkış kodu {code}', 409)
    return out


def unsafe_config(repo) -> list[str]:
    out = git(repo, 'config', '--list', '--show-scope', '-z')
    fields = out.split('\0')
    keys = [entry.split('\n', 1)[0] for scope, entry in zip(fields[::2], fields[1::2]) if scope in ('local', 'worktree')]
    return sorted({k for k in keys if not LOCAL_OK.fullmatch(k)})


# ── Mac-side state ────────────────────────────────────────────────────────────

def status():
    try:
        code, out = gh('auth', 'status', '--json', 'hosts', ok=(0, 1))
    except GitHubError as exc:
        if exc.code == 'gh_missing':
            return {'installed': False, 'logged_in': False, 'account': None}
        raise
    try:
        raw = json.loads(out)
    except ValueError:
        raw = None
    hosts = raw.get('hosts') if isinstance(raw, dict) else None
    hosts = hosts.get('github.com') if isinstance(hosts, dict) else []
    if not isinstance(hosts, list):
        hosts = []
    active = next((h for h in hosts if isinstance(h, dict) and h.get('active') and h.get('state') == 'success'), None)
    login = active.get('login') if active else None
    # Only these fields leave this function; `gh` never prints the token without --show-token, and it is not read.
    return {'installed': True, 'logged_in': bool(active), 'account': login if isinstance(login, str) else None}


def repos(owner=None):
    args = ['repo', 'list', *([repo_name(f'{owner}/x').split('/')[0]] if owner else []), '--limit', '200',
            '--json', 'nameWithOwner,isPrivate,isArchived,updatedAt']
    _, out = gh(*args)
    try:
        raw = json.loads(out or '[]')
    except ValueError:
        raise GitHubError('gh_failed', 'gh çıktısı okunamadı', 409) from None
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw:
        name = item.get('nameWithOwner') if isinstance(item, dict) else None
        if not isinstance(name, str):
            continue
        try:
            repo_name(name)
        except GitHubError:
            continue   # a name that cannot be cloned safely is not offered
        items.append({k: item.get(k) for k in ('nameWithOwner', 'isPrivate', 'isArchived', 'updatedAt')})
    return items


def clone_root(db_path) -> Path:
    settings, _ = user_settings.load(db_path)
    value = (settings.get('github') or {}).get('clone_dir')
    if not value:
        raise GitHubError('clone_dir_missing', 'Önce Ayarlar → GitHub bölümünde klon klasörünü seçin')
    root = Path(os.environ.get('AI_ORCHESTRATOR_ROOT') or Path(__file__).resolve().parents[1]).resolve()
    problem = user_settings._clone_dir_problem(value, root)
    if problem:
        raise GitHubError('clone_dir_invalid', f'Klon klasörü kullanılamıyor: {problem}')
    return Path(value).resolve()


def clone(db_path, repo):
    repo, root = repo_name(repo), clone_root(db_path)
    target = root / repo.split('/')[1]
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise GitHubError('target_exists', f'{target} bir klasör değil')
    if target.exists():   # also catches a different-case name on a case-insensitive disk
        try:
            same = (remote_repo(git(target, 'remote', 'get-url', 'origin').strip()) or '').lower() == repo.lower()
        except GitHubError:
            same = False
        if not same or target.resolve().parent != root:
            raise GitHubError('target_exists', f'{target} başka bir içerikle dolu')
        return {'path': str(target.resolve()), 'cloned': False}
    try:
        gh('repo', 'clone', repo, str(target), timeout=CLONE_TIMEOUT)
    except GitHubError:
        if not target.is_symlink() and target.exists() and target.resolve().parent == root:
            shutil.rmtree(target, ignore_errors=True)
        raise
    if target.is_symlink() or target.resolve().parent != root:
        raise GitHubError('target_exists', 'Klon beklenmeyen bir yere yazıldı')
    return {'path': str(target.resolve()), 'cloned': True}


# ── a work's repository ───────────────────────────────────────────────────────

def _work(conn, work_id):
    try:
        work = ws.get_work(conn, work_id)
    except ws.WorkError:
        raise GitHubError('not_found', 'Çalışma bulunamadı', 404) from None
    return work, Path(work['workspace_path'])


def _idle(conn, path):
    # ponytail: checked once before the step; a run that starts while git works races it (the queue is per work and
    # the panel offers these steps only between runs).
    busy = conn.execute("SELECT 1 FROM sessions WHERE workspace_path=? AND status IN ('QUEUED','RUNNING','RECOVERY_REQUIRED')",
                        (str(path),)).fetchone() or conn.execute('SELECT 1 FROM workspace_locks WHERE workspace_path=?',
                                                                 (str(path),)).fetchone()
    if busy:
        raise GitHubError('workspace_busy', 'Bu klasörde bir yürütme sürüyor; bitince deneyin')


def changes(path):
    out = git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    items, fields, i = [], out.split('\0'), 0
    while i < len(fields) and fields[i]:
        code, name = fields[i][:2], fields[i][3:]
        i += 2 if code[0] in 'RC' else 1   # a rename carries its old name in the next field
        items.append({'path': name, 'status': code.strip() or '?', 'secret': is_secret(name),
                      'workflow': name.startswith(WORKFLOWS)})
    return items


def state(conn, work_id):
    work, path = _work(conn, work_id)
    try:
        top = Path(git(path, 'rev-parse', '--show-toplevel').strip()).resolve()
    except GitHubError:
        return {'git': False}
    if top != path.resolve():
        return {'git': False, 'reason': 'not_repo_root'}
    problems = unsafe_config(path)
    try:
        origin = git(path, 'remote', 'get-url', 'origin').strip()
    except GitHubError:
        origin = ''
    branch = git(path, 'branch', '--show-current').strip()
    # `git status` may run a config-defined clean filter for a `.gitattributes` entry: not with an unsafe config.
    return {'git': True, 'repo': remote_repo(origin), 'branch': branch or None, 'head': _head(path),
            'unsafe_config': problems, 'files': [] if problems else changes(path)}


def _head(path):
    try:
        return git(path, 'rev-parse', '--verify', 'HEAD').strip()
    except GitHubError:
        return None


def _repo_info(repo):
    _, out = gh('repo', 'view', repo, '--json', 'nameWithOwner,defaultBranchRef,viewerPermission')
    info = json.loads(out)
    base = (info.get('defaultBranchRef') or {}).get('name') or ''
    if not base:   # DoD 4: a repository created without a README has no branch for a PR to target
        raise GitHubError('empty_repo', "Depo boş: GitHub'da bir README ile ilk commit'i oluşturun, sonra yeniden deneyin")
    _check_branch_name(base)
    return base, info.get('viewerPermission')


def _check_branch_name(name):
    code, _, _ = run([git_bin(), 'check-ref-format', '--branch', name])
    if code or not name or name.startswith('-'):
        raise GitHubError('invalid_branch', f'Geçersiz dal adı: {name!r}', 422)


def _ready(conn, work_id):
    """The checks every writing step shares: idle workspace, repo root, safe config, a GitHub origin."""
    work, path = _work(conn, work_id)
    _idle(conn, path)
    st = state(conn, work_id)
    if not st['git']:
        raise GitHubError('not_a_repo', 'Çalışma klasörü bir git deposunun kökü değil')
    if st['unsafe_config']:
        raise GitHubError('unsafe_git_config', 'Depo ayarında izin verilmeyen anahtarlar: ' + ', '.join(st['unsafe_config']))
    if not st['repo']:
        raise GitHubError('not_github', 'origin bir GitHub deposu değil')
    return work, path, st


def _identity(path):
    """With no user.email of the user's own (repo or global), git would invent `<user>@<hostname>.local` and publish
    the Mac's name; the account's GitHub noreply address is used instead (DoD 4 finding)."""
    try:
        git(path, 'config', '--get', 'user.email')
        return []
    except GitHubError:
        pass
    _, out = gh('api', 'user')
    user = json.loads(out or '{}')
    login, uid = user.get('login') or '', user.get('id')
    if not (isinstance(uid, int) and uid > 0 and PART.fullmatch(login)):
        return []
    name = ' '.join(str(user.get('name') or '').split())[:100] or login   # Gemini P3: a blank profile name
    return ['-c', f'user.name={name}', '-c', f'user.email={uid}+{login}@users.noreply.github.com']


def commit(conn, work_id, files, message, expected_head, secrets_ack=False):
    work, path, st = _ready(conn, work_id)
    if st['head'] != expected_head:
        raise GitHubError('stale', 'Depo değişti; listeyi yenileyin')
    known = {f['path']: f for f in st['files']}
    if not files or any(f not in known for f in files) or len(set(files)) != len(files):
        raise GitHubError('invalid_files', 'Yalnız değişen dosyalar listesinden seçin', 422)
    if any(known[f]['secret'] for f in files) and not secrets_ack:
        raise GitHubError('secrets_ack_required', 'Gizli olabilecek dosyalar seçildi; ayrıca onaylayın')
    base, _ = _repo_info(st['repo'])
    branch = st['branch']
    if not branch or branch == base:   # never on the default branch (or a detached HEAD)
        branch = branch_for(work['title'], work_id)
        _check_branch_name(branch)
        git(path, 'switch', '-c', branch)
    git(path, 'add', '-A', '--', *files)
    git(path, *_identity(path), 'commit', '--no-verify', '--only', '-F', '-', '--', *files, stdin=message)
    sha = _head(path)
    ws.append_event(conn, work_id, 'github_commit', {'branch': branch, 'commit': sha, 'files': len(files)})
    return {'branch': branch, 'commit': sha}


def push_preview(conn, work_id):
    work, path, st = _ready(conn, work_id)
    base, _ = _repo_info(st['repo'])
    if not st['branch'] or st['branch'] == base:
        raise GitHubError('default_branch', 'Ana dala doğrudan gönderilmez; önce değişiklikleri kaydedin (yeni dal açılır)')
    _check_branch_name(st['branch'])
    log = git(path, 'log', '--format=%H%x09%s', 'HEAD', '--not', '--remotes=origin')
    commits = [dict(zip(('sha', 'subject'), line.split('\t', 1))) for line in log.splitlines() if '\t' in line]
    files = sorted(set(git(path, 'log', '--name-only', '--format=', 'HEAD', '--not', '--remotes=origin').split()))
    view = {'repo': st['repo'], 'branch': st['branch'], 'base': base, 'head': st['head'], 'url': f"{GITHUB}{st['repo']}.git",
            'commits': commits, 'files': len(files), 'workflow_files': [f for f in files if f.startswith(WORKFLOWS)]}
    return {**view, 'digest': digest({k: view[k] for k in ('repo', 'branch', 'base', 'head', 'url', 'workflow_files')})}


def push(conn, work_id, expected_digest, workflows_ack=False):
    view = push_preview(conn, work_id)
    if view['digest'] != expected_digest:
        raise GitHubError('stale', 'Gönderilecek içerik onaydan sonra değişti; yeniden bakın')
    if view['workflow_files'] and not workflows_ack:
        raise GitHubError('workflows_ack_required', 'Commit iş akışı dosyası (.github/workflows) içeriyor; ayrıca onaylayın')
    work, path = _work(conn, work_id)
    # An explicit URL (built from the validated repo name), never the remote name, and never a force or delete refspec.
    git(path, 'push', '--porcelain', view['url'], f"HEAD:refs/heads/{view['branch']}", push=True, timeout=CLONE_TIMEOUT)
    ws.append_event(conn, work_id, 'github_push', {k: view[k] for k in ('repo', 'branch', 'head')})
    return {'repo': view['repo'], 'branch': view['branch'], 'head': view['head']}


# The panel's wording (ui shared.tsx outcomeLabels) for the reviewer's final word on a run.
OUTCOMES = {'verified': 'İnceleyici hedefi doğruladı', 'reviewer_blocked': 'İnceleyici engel bildirdi',
            'review_invalid': 'İnceleme sonucu okunamadı', 'no_progress': 'İlerleme yok (aynı bulgular)',
            'turn_limit': 'Tur sınırı doldu; inceleyici hâlâ değişiklik istiyordu', 'budget_exhausted': 'Etkin süre bütçesi doldu'}


def pr_body(title: str, status: str | None, loop: dict | None) -> str:
    """TB-21 card §2: the draft PR says what Conductor ran and what its reviewer concluded (an editable suggestion)."""
    lines = [f"Conductor çalışması: {title}", "", f"Son yürütme: {status or '—'}"]
    if loop and loop.get('outcome'):
        lines.append(f"İnceleyici sonucu: {OUTCOMES.get(loop['outcome'], loop['outcome'])} (tur {loop.get('current_turn') or '?'})")
    elif loop:
        lines.append("İnceleyici sonucu: henüz yok")
    return "\n".join(lines) + "\n"


def pr_preview(conn, work_id):
    work, path, st = _ready(conn, work_id)
    base, _ = _repo_info(st['repo'])
    if not st['branch'] or st['branch'] == base:
        raise GitHubError('default_branch', 'PR için önce değişiklikleri bir dala gönderin')
    subject = git(path, 'log', '-1', '--format=%s').strip()
    summary = ws.work_summary(conn, work_id)['latest_session'] or {}
    body = pr_body(work['title'], summary.get('status'), summary.get('id') and loop_store.state(conn, summary['id']))
    view = {'repo': st['repo'], 'base': base, 'head': st['branch'], 'sha': st['head']}
    return {**view, 'title': subject or work['title'], 'body': body, 'digest': digest(view)}


def pull_request(conn, work_id, expected_digest, title, body):
    view = pr_preview(conn, work_id)
    if view['digest'] != expected_digest:
        raise GitHubError('stale', 'Dal onaydan sonra değişti; yeniden bakın')
    _, out = gh('pr', 'create', '--repo', view['repo'], '--base', view['base'], '--head', view['head'],
                '--title', title, '--body-file', '-', '--draft', '--no-maintainer-edit', stdin=body)   # K1: draft
    url = (out.strip().splitlines() or [''])[-1]
    if not re.fullmatch(rf"https://github\.com/{re.escape(view['repo'])}/pull/\d+", url, re.I):
        raise GitHubError('gh_failed', 'gh beklenmeyen bir çıktı verdi')
    ws.append_event(conn, work_id, 'github_pr', {'repo': view['repo'], 'base': view['base'], 'head': view['head'], 'url': url})
    return {'url': url}


# ── API ───────────────────────────────────────────────────────────────────────

def mac_only(request: Request):
    """Defence in depth: the mobile gateway does not route these paths at all (not in its allowlist)."""
    if getattr(request.state, 'conductor_mobile_device', None):
        raise HTTPException(404, detail={'code': 'not_found', 'detail': 'Not found'})


router = APIRouter(prefix='/api/github', dependencies=[Depends(mutation_guard), Depends(mac_only)])


def _call(request, fn, *args, **kwargs):
    conn = store.connect(request.app.state.runner_db)
    try:
        return fn(conn, *args, **kwargs)
    except GitHubError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'detail': str(exc)}) from None
    finally:
        conn.close()


def _plain(fn, *args):
    try:
        return fn(*args)
    except GitHubError as exc:
        raise HTTPException(exc.status, detail={'code': exc.code, 'detail': str(exc)}) from None


class Clone(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repo: str = Field(min_length=3, max_length=201)


class Commit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    files: list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=2000)
    message: str = Field(min_length=1, max_length=65536)
    expected_head: str | None = Field(default=None, max_length=64)
    secrets_ack: bool = False


class Push(BaseModel):
    model_config = ConfigDict(extra='forbid')
    digest: str = Field(min_length=64, max_length=64)
    workflows_ack: bool = False


class PullRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    digest: str = Field(min_length=64, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=65536)


def _no_nul(*texts):
    if any('\0' in t for t in texts):
        raise HTTPException(422, detail={'code': 'validation_error', 'detail': 'NUL karakteri kullanılamaz'})


@router.get('/status')
def get_status():
    return _plain(status)


@router.get('/repos')
def get_repos(owner: str | None = Query(default=None, max_length=100)):
    return {'items': _plain(repos, owner)}


@router.post('/clone')
def post_clone(body: Clone, request: Request):
    return _plain(clone, request.app.state.runner_db, body.repo)


@router.get('/works/{work_id}')
def get_state(work_id: str, request: Request):
    return _call(request, state, work_id)


@router.post('/works/{work_id}/commit')
def post_commit(work_id: str, body: Commit, request: Request):
    _no_nul(body.message, *body.files)
    return _call(request, commit, work_id, body.files, body.message, body.expected_head, body.secrets_ack)


@router.get('/works/{work_id}/push')
def get_push(work_id: str, request: Request):
    return _call(request, push_preview, work_id)


@router.post('/works/{work_id}/push')
def post_push(work_id: str, body: Push, request: Request):
    return _call(request, push, work_id, body.digest, body.workflows_ack)


@router.get('/works/{work_id}/pr')
def get_pr(work_id: str, request: Request):
    return _call(request, pr_preview, work_id)


@router.post('/works/{work_id}/pr')
def post_pr(work_id: str, body: PullRequest, request: Request):
    _no_nul(body.title, body.body)
    return _call(request, pull_request, work_id, body.digest, body.title, body.body)
