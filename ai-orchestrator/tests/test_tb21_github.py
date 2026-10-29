"""TB-21 step 2: GitHub through `gh` — validation, clone, commit, push, draft PR. A fake `gh` and local bare
repositories stand in for GitHub: no network, no token, no quota."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import github, mobile, store, runner_store as repo, works_store as ws

FAKE_GH = '''#!{python}
import json, os, subprocess, sys, time
a = sys.argv[1:]
data = sys.stdin.read() if a[:2] == ['pr', 'create'] else ''
with open(os.environ['FAKE_GH_LOG'], 'a') as f:
    f.write(json.dumps({{'argv': a, 'stdin': data}}) + '\\n')
mode = os.environ.get('FAKE_GH_MODE', '')
if mode == 'sleep':
    time.sleep(30)
if a[:2] == ['auth', 'status']:
    if mode == 'signed_out':
        print(json.dumps({{'hosts': {{}}}})); sys.exit(1)
    print(json.dumps({{'hosts': {{'github.com': [{{'state': 'success', 'active': True, 'host': 'github.com', 'login': 'octo',
                                                 'token': 'gho_SECRETSECRET', 'scopes': 'repo, workflow'}}]}}}}))
elif a[:2] == ['repo', 'list']:
    print(json.dumps([{{'nameWithOwner': 'octo/demo', 'isPrivate': True, 'isArchived': False, 'updatedAt': 't'}},
                      {{'nameWithOwner': '-evil/x'}}, {{'nameWithOwner': 'octo/..'}}]))
elif a[:2] == ['repo', 'clone']:
    sys.exit(subprocess.call(['git', 'clone', '-q', os.environ['FAKE_GITHUB'] + a[2] + '.git', a[3]]))
elif a[:2] == ['repo', 'view']:
    print(json.dumps({{'nameWithOwner': a[2], 'defaultBranchRef': {{'name': '' if mode == 'empty' else 'main'}}, 'viewerPermission': 'ADMIN'}}))
elif a[:2] == ['pr', 'create']:
    print('https://github.com/' + a[a.index('--repo') + 1] + '/pull/7')
elif a[:2] == ['api', 'user']:
    print(json.dumps({{'login': 'octo', 'id': 42, 'name': '   ' if mode == 'blank' else 'Octo\\nCat'}}))
elif a[:2] == ['auth', 'git-credential']:
    sys.exit(0)
else:
    sys.exit(2)
'''


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Validation(unittest.TestCase):
    def test_repo_names(self):
        for ok in ('octo/demo', 'a/b', 'Octo-Cat/my.repo_1', 'x/a..b'):
            self.assertEqual(github.repo_name(ok), ok)
        for bad in ('-evil/x', 'octo/-x', 'octo/..', 'octo/.', './x', 'octo/demo.git', 'octo/DEMO.GIT', 'octo/re po',
                    'octo/x\n', 'octo/', '/x', 'a/b/c', 'octo/dеmo', 'o' * 101 + '/x', 'octo/x-', 'octo', ''):
            with self.subTest(bad=bad), self.assertRaises(github.GitHubError):
                github.repo_name(bad)

    def test_branch_names_are_safe_refs(self):
        self.assertEqual(github.slug('Hesap makinesi: toplama & ÇIKARMA!'), 'hesap-makinesi-toplama-cikarma')
        self.assertEqual(github.slug('ışık'), 'isik')
        self.assertEqual(github.slug('--- ~^:?*[\\ .lock'), 'lock')
        self.assertEqual(github.slug('@{'), 'calisma')
        for title in ('-x', '..', 'a/b', 'HEAD', 'çalışma' * 20, '.lock'):
            name = github.branch_for(title, 'ABCDEF123')
            self.assertTrue(name.startswith('conductor/') and name.endswith('-abcdef'), name)
            self.assertEqual(subprocess.run(['git', 'check-ref-format', '--branch', name], capture_output=True).returncode, 0, name)

    def test_pr_body_carries_the_reviewer_outcome(self):   # TB-21 card §2
        self.assertEqual(github.pr_body('Hesap', 'SUCCEEDED', {'outcome': 'verified', 'current_turn': 2}),
                         'Conductor çalışması: Hesap\n\nSon yürütme: SUCCEEDED\nİnceleyici sonucu: İnceleyici hedefi doğruladı (tur 2)\n')
        self.assertIn('İnceleyici sonucu: henüz yok', github.pr_body('Hesap', 'RUNNING', {'outcome': None, 'current_turn': 1}))
        self.assertEqual(github.pr_body('Hesap', None, None), 'Conductor çalışması: Hesap\n\nSon yürütme: —\n')
        self.assertIn('İnceleyici sonucu: custom (tur ?)', github.pr_body('Hesap', 'FAILED', {'outcome': 'custom', 'current_turn': None}))   # Gemini P3

    def test_secret_and_remote_patterns(self):
        for p in ('.env', '.env.local', '.ENV', 'CERT.PEM', 'prod.env', '.netrc', '.npmrc', 'cert.p12', 'key.pfx',
                  'a/b/server.pem', 'id_rsa', 'deploy/id_ed25519.pub', 'credentials.json', 'x.key'):
            self.assertTrue(github.is_secret(p), p)
        for p in ('README.md', 'env.md', 'src/key.ts', 'docs/credential.md'):
            self.assertFalse(github.is_secret(p), p)
        self.assertEqual(github.remote_repo('https://github.com/o/n.git'), 'o/n')
        self.assertEqual(github.remote_repo('git@github.com:o/n.git'), 'o/n')
        for url in ('https://evil.com/o/n', 'https://github.com.evil.com/o/n', 'https://github.com/-x/n', 'https://github.com/o/n/x'):
            self.assertIsNone(github.remote_repo(url), url)

    def test_error_redacts_tokens_and_url_credentials(self):
        self.assertEqual(github._say('fatal: https://kullanici:parola@github.com/octo/demo.git/'),
                         'fatal: https://***@github.com/octo/demo.git/')
        self.assertEqual(github._say('fatal: https://belirteç@github.com/octo/demo.git/'),
                         'fatal: https://***@github.com/octo/demo.git/')
        self.assertEqual(github._say('bad ghp_SECRET123 and github_pat_ABC_9 in https://ghs_TOK@github.com/o/n'),
                         'bad ghp_*** and github_pat_*** in https://***@github.com/o/n')
        self.assertEqual(github._say('redirect https://github.com/o/n to git@github.com:o/n.git'),
                         'redirect https://github.com/o/n to git@github.com:o/n.git')

    def test_malformed_gh_json_in_status_and_repos(self):
        for bad in ('not-json', 'null', '[]', '{"hosts": null}', '{"hosts": []}', '{"hosts": {"github.com": null}}',
                    '{"hosts": {"github.com": {"active": true}}}', '{"hosts": {"github.com": [null, "x", 1]}}'):
            with self.subTest(status=bad), patch.object(github, 'gh', return_value=(0, bad)):
                self.assertEqual(github.status(), {'installed': True, 'logged_in': False, 'account': None})
        with patch.object(github, 'gh', return_value=(0, '{"hosts": {"github.com": [{"active": true, "state": "success", "login": 123}]}}')):
            self.assertEqual(github.status(), {'installed': True, 'logged_in': True, 'account': None})
        for bad in ('null', '{}', '"str"', '[null, 1, "x", {}, {"nameWithOwner": null}, {"nameWithOwner": 7}]'):
            with self.subTest(repos=bad), patch.object(github, 'gh', return_value=(0, bad)):
                self.assertEqual(github.repos(), [])
        mixed = '[null, 1, {"nameWithOwner": null}, {"nameWithOwner": 7}, {"nameWithOwner": "octo/demo", "isPrivate": false, "isArchived": false, "updatedAt": "t"}]'
        with patch.object(github, 'gh', return_value=(0, mixed)):
            self.assertEqual([r['nameWithOwner'] for r in github.repos()], ['octo/demo'])
        with patch.object(github, 'gh', return_value=(0, '{not-json')), self.assertRaises(github.GitHubError) as caught:
            github.repos()
        self.assertEqual((caught.exception.code, caught.exception.status), ('gh_failed', 409))

    def test_timeout_closes_pipes_if_post_kill_communicate_hangs(self):
        stdin, stdout, stderr = MagicMock(), MagicMock(), MagicMock()
        proc = MagicMock(pid=999999, stdin=stdin, stdout=stdout, stderr=stderr,
                         communicate=MagicMock(side_effect=subprocess.TimeoutExpired(['gh'], 1)))
        with patch.object(subprocess, 'Popen', return_value=proc), patch.object(os, 'killpg'):
            with self.assertRaises(github.GitHubError) as caught:
                github.run(['/usr/bin/gh', 'auth', 'status'], timeout=1)
        self.assertEqual((caught.exception.code, caught.exception.status), ('timeout', 504))
        self.assertEqual(proc.communicate.call_args_list[1].kwargs, {'timeout': 5})
        stdout.close.assert_called_once()
        stderr.close.assert_called_once()
        stdin.close.assert_called_once()

    def test_failed_clone_cleans_up_new_target_only(self):
        root = Path(__file__).resolve().parent
        target = root / 'half-clone-test-99'
        state = {'exists': False, 'symlink': False, 'dir': False}
        orig_exists, orig_symlink, orig_dir, orig_resolve = Path.exists, Path.is_symlink, Path.is_dir, Path.resolve
        def fake_exists(p): return state['exists'] if p == target else orig_exists(p)
        def fake_symlink(p): return state['symlink'] if p == target else orig_symlink(p)
        def fake_dir(p): return state['dir'] if p == target else orig_dir(p)
        def fail_half_clone(*args, **kw):
            state.update(exists=True, dir=True)
            raise github.GitHubError('timeout', 'gh zaman aşımına uğradı', 504)
        with (patch.object(github, 'clone_root', return_value=root),
              patch.object(Path, 'exists', fake_exists),
              patch.object(Path, 'is_symlink', fake_symlink),
              patch.object(Path, 'is_dir', fake_dir),
              patch.object(github.shutil, 'rmtree', side_effect=lambda p, **kw: state.update(exists=False, dir=False)) as rmtree):
            with patch.object(github, 'gh', side_effect=fail_half_clone), self.assertRaises(github.GitHubError) as caught:
                github.clone('db', 'octo/half-clone-test-99')
            self.assertEqual(caught.exception.code, 'timeout')
            rmtree.assert_called_once_with(target, ignore_errors=True)
            self.assertFalse(state['exists'])

            rmtree.reset_mock()
            with patch.object(github, 'gh', side_effect=github.GitHubError('gh_failed', 'boom', 409)), self.assertRaises(github.GitHubError):
                github.clone('db', 'octo/half-clone-test-99')
            rmtree.assert_not_called()

            def fail_symlink(*args, **kw):
                state.update(exists=True, symlink=True)
                raise github.GitHubError('gh_failed', 'boom', 409)
            with patch.object(github, 'gh', side_effect=fail_symlink), self.assertRaises(github.GitHubError):
                github.clone('db', 'octo/half-clone-test-99')
            rmtree.assert_not_called()

            state.update(exists=False, symlink=False, dir=False)
            with (patch.object(Path, 'resolve', lambda p, *a, **kw: root.parent / 'escaped' if p == target else orig_resolve(p, *a, **kw)),
                  patch.object(github, 'gh', side_effect=fail_half_clone),
                  self.assertRaises(github.GitHubError)):
                github.clone('db', 'octo/half-clone-test-99')
            rmtree.assert_not_called()

            state.update(exists=True, symlink=False, dir=True)
            with (patch.object(github, 'git', side_effect=github.GitHubError('git_failed', 'err', 409)),
                  patch.object(github, 'gh') as gh_mock,
                  self.assertRaises(github.GitHubError) as pre_existing):
                github.clone('db', 'octo/half-clone-test-99')
            self.assertEqual(pre_existing.exception.code, 'target_occupied')
            gh_mock.assert_not_called()
            rmtree.assert_not_called()


class Api(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        t = Path(tmp.name).resolve()
        self.home, self.root, remotes = t / 'home', t / 'conductor', t / 'remotes'
        (self.root / '.system').mkdir(parents=True); self.home.mkdir(); (self.home / 'code').mkdir()
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        bare = remotes / 'octo/demo.git'
        sh('git', 'init', '-q', '--bare', '-b', 'main', str(bare))
        seed = t / 'seed'; sh('git', 'init', '-q', '-b', 'main', str(seed))
        (seed / 'README.md').write_text('demo\n')
        sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'init', cwd=seed)
        sh('git', 'add', 'README.md', cwd=seed)
        sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'readme', cwd=seed)
        sh('git', 'push', '-q', str(bare), 'main', cwd=seed)
        self.bare, self.log = bare, t / 'gh.log'
        gh = t / 'bin/gh'; gh.parent.mkdir(); gh.write_text(FAKE_GH.format(python=sys.executable)); gh.chmod(0o755)
        for p in (patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root), 'CONDUCTOR_GH_BIN': str(gh),
                                          'FAKE_GH_LOG': str(self.log), 'FAKE_GITHUB': f'file://{remotes}/'}),
                  patch.object(Path, 'home', return_value=self.home), patch.object(github, 'GITHUB', f'file://{remotes}/')):
            p.start(); self.addCleanup(p.stop)
        self.db = self.root / '.system/state.db'
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        from app.api import create_app
        app = create_app(); app.state.runner_db = str(self.db)
        self.client = TestClient(app, base_url='http://127.0.0.1:8765')
        self.marker = t / 'hook-ran'

    def set_clone_dir(self):
        r = self.client.put('/api/settings', json={'github': {'clone_dir': str(self.home / 'code')}})
        self.assertEqual(r.status_code, 200, r.text)

    def clone_work(self):
        self.set_clone_dir()
        path = Path(self.client.post('/api/github/clone', json={'repo': 'octo/demo'}).json()['path'])
        sh('git', 'config', 'user.name', 'Test User', cwd=path); sh('git', 'config', 'user.email', 'b@example.com', cwd=path)
        for hook in ('pre-commit', 'commit-msg', 'pre-push', 'post-commit'):   # an agent can write these
            h = path / '.git/hooks' / hook; h.write_text(f'#!/bin/sh\ntouch {self.marker}\n'); h.chmod(0o755)
        wid = str(uuid4()); ws.create_work(self.conn, wid, 'Hesap makinesi', str(path))
        return wid, path

    def gh_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def get(self, url, code=200):
        r = self.client.get(url); self.assertEqual(r.status_code, code, r.text); return r.json()

    def post(self, url, body, code=200):
        r = self.client.post(url, json=body); self.assertEqual(r.status_code, code, r.text); return r.json()

    # ── status, repos, clone ──
    def test_status_reports_the_account_and_never_the_token(self):
        r = self.client.get('/api/github/status')
        self.assertEqual(r.json(), {'installed': True, 'logged_in': True, 'account': 'octo'})
        self.assertNotIn('SECRET', r.text)
        with patch.dict(os.environ, {'FAKE_GH_MODE': 'signed_out'}):
            self.assertEqual(self.get('/api/github/status')['logged_in'], False)
        with patch.dict(os.environ, {'CONDUCTOR_GH_BIN': '/nonexistent/gh'}):
            self.assertEqual(self.get('/api/github/status'), {'installed': False, 'logged_in': False, 'account': None})

    def test_repo_list_drops_unsafe_names(self):
        self.assertEqual([i['nameWithOwner'] for i in self.get('/api/github/repos')['items']], ['octo/demo'])
        self.assertEqual(self.client.get('/api/github/repos', params={'owner': '-evil'}).status_code, 422)
        with patch.object(github, 'gh', return_value=(0, '{not-json')):
            self.assertEqual(self.get('/api/github/repos', 409)['code'], 'gh_failed')

    def test_clone_needs_the_folder_then_clones_once(self):
        self.assertEqual(self.post('/api/github/clone', {'repo': 'octo/demo'}, 409)['code'], 'clone_dir_missing')
        self.set_clone_dir()
        first = self.post('/api/github/clone', {'repo': 'octo/demo'})
        self.assertEqual((first['cloned'], first['path']), (True, str(self.home / 'code/demo')))
        self.assertEqual(self.gh_calls()[-1]['argv'], ['repo', 'clone', 'octo/demo', str(self.home / 'code/demo')])   # no `--`
        self.assertEqual(self.post('/api/github/clone', {'repo': 'octo/demo'}), {'path': first['path'], 'cloned': False})
        self.assertEqual(self.post('/api/github/clone', {'repo': '-evil/demo'}, 422)['code'], 'invalid_repo')

    def test_clone_refuses_an_occupied_or_linked_target(self):
        self.set_clone_dir()
        (self.home / 'code/other').mkdir(); (self.home / 'code/other/x').write_text('mine')
        self.assertEqual(self.post('/api/github/clone', {'repo': 'octo/other'}, 409)['code'], 'target_occupied')
        (self.home / 'code/linked').symlink_to(self.home)
        self.assertEqual(self.post('/api/github/clone', {'repo': 'octo/linked'}, 409)['code'], 'target_exists')
        self.assertEqual(self.gh_calls(), [])

    def test_failed_clone_removes_half_cloned_target(self):
        self.set_clone_dir()
        target = self.home / 'code/demo'
        def broken_gh(*args, **kw):
            (target / '.git').mkdir(parents=True)
            (target / '.git/config').write_text(f'[remote "origin"]\n\turl = {github.GITHUB}octo/demo.git\n')
            raise github.GitHubError('timeout', 'gh zaman aşımına uğradı', 504)
        with patch.object(github, 'gh', side_effect=broken_gh):
            self.assertEqual(self.post('/api/github/clone', {'repo': 'octo/demo'}, 504)['code'], 'timeout')
        self.assertFalse(target.exists())
        self.assertEqual(self.post('/api/github/clone', {'repo': 'octo/demo'}), {'path': str(target), 'cloned': True})

    # ── commit → push → PR ──
    def test_commit_push_and_draft_pr(self):
        wid, path = self.clone_work()
        (path / 'app.py').write_text('print(1)\n'); (path / '.env').write_text('KEY=1\n')
        (path / 'other.py').write_text('x\n'); sh('git', 'add', 'other.py', cwd=path)   # staged by the agent, not chosen
        st = self.get(f'/api/github/works/{wid}')
        self.assertEqual((st['repo'], st['branch'], st['unsafe_config']), ('octo/demo', 'main', []))
        self.assertEqual({f['path']: f['secret'] for f in st['files']}, {'app.py': False, '.env': True, 'other.py': False})
        self.assertEqual(self.post(f'/api/github/works/{wid}/push', {'digest': '0' * 64}, 409)['code'], 'default_branch')

        done = self.post(f'/api/github/works/{wid}/commit', {'files': ['app.py'], 'message': '-m Add app\n\nbody', 'expected_head': st['head']})
        self.assertRegex(done['branch'], r'^conductor/\d{8}-\d{4}-hesap-makinesi-[0-9a-f]{6}$')
        self.assertEqual(sh('git', 'show', '--name-only', '--format=%s', 'HEAD', cwd=path).split(), ['-m', 'Add', 'app', 'app.py'])
        self.assertEqual(sh('git', 'rev-parse', 'main', cwd=path), sh('git', 'rev-parse', 'origin/main', cwd=path))   # main untouched
        self.assertIn('other.py', sh('git', 'diff', '--cached', '--name-only', cwd=path))

        view = self.get(f'/api/github/works/{wid}/push')
        self.assertEqual((view['repo'], view['branch'], view['base'], view['files'], view['workflow_files']),
                         ('octo/demo', done['branch'], 'main', 1, []))
        self.post(f'/api/github/works/{wid}/push', {'digest': view['digest']})
        self.assertEqual(sh('git', 'rev-parse', done['branch'], cwd=self.bare).strip(), done['commit'])
        self.assertEqual(sh('git', 'rev-parse', 'main', cwd=self.bare), sh('git', 'rev-parse', 'origin/main', cwd=path))

        pr = self.get(f'/api/github/works/{wid}/pr')
        self.assertEqual((pr['base'], pr['head'], pr['title']), ('main', done['branch'], '-m Add app'))
        out = self.post(f'/api/github/works/{wid}/pr', {'digest': pr['digest'], 'title': '--title x', 'body': 'Özet; $(rm -rf ~)'})
        self.assertEqual(out['url'], 'https://github.com/octo/demo/pull/7')
        call = self.gh_calls()[-1]
        self.assertEqual(call['argv'], ['pr', 'create', '--repo', 'octo/demo', '--base', 'main', '--head', done['branch'],
                                        '--title', '--title x', '--body-file', '-', '--draft', '--no-maintainer-edit'])
        self.assertEqual(call['stdin'], 'Özet; $(rm -rf ~)')
        kinds = [r[0] for r in self.conn.execute('SELECT type FROM work_events WHERE work_id=? ORDER BY seq', (wid,))]
        self.assertEqual(kinds[-3:], ['github_commit', 'github_push', 'github_pr'])
        self.assertFalse(self.marker.exists(), 'an agent-written git hook ran')

    def test_secret_files_and_workflows_need_their_own_confirmation(self):
        wid, path = self.clone_work()
        (path / '.env').write_text('KEY=1\n'); (path / '.github/workflows').mkdir(parents=True)
        (path / '.github/workflows/ci.yml').write_text('on: push\n')
        head = self.get(f'/api/github/works/{wid}')['head']
        body = {'files': ['.env'], 'message': 'env', 'expected_head': head}
        self.assertEqual(self.post(f'/api/github/works/{wid}/commit', body, 409)['code'], 'secrets_ack_required')
        self.post(f'/api/github/works/{wid}/commit', {**body, 'secrets_ack': True})
        head = self.get(f'/api/github/works/{wid}')['head']
        self.post(f'/api/github/works/{wid}/commit', {'files': ['.github/workflows/ci.yml'], 'message': 'ci', 'expected_head': head})
        view = self.get(f'/api/github/works/{wid}/push')
        self.assertEqual(view['workflow_files'], ['.github/workflows/ci.yml'])
        self.assertEqual(self.post(f'/api/github/works/{wid}/push', {'digest': view['digest']}, 409)['code'], 'workflows_ack_required')
        self.post(f'/api/github/works/{wid}/push', {'digest': view['digest'], 'workflows_ack': True})

    def test_a_changed_branch_invalidates_the_confirmation(self):
        wid, path = self.clone_work()
        (path / 'a.txt').write_text('a')
        head = self.get(f'/api/github/works/{wid}')['head']
        self.assertEqual(self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'a', 'expected_head': '0' * 40}, 409)['code'], 'stale')
        self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'a', 'expected_head': head})
        view, pr = self.get(f'/api/github/works/{wid}/push'), self.get(f'/api/github/works/{wid}/pr')
        (path / 'b.txt').write_text('b'); sh('git', 'add', 'b.txt', cwd=path); sh('git', 'commit', '-q', '-m', 'agent', cwd=path)
        self.assertEqual(self.post(f'/api/github/works/{wid}/push', {'digest': view['digest']}, 409)['code'], 'stale')
        self.assertEqual(self.post(f'/api/github/works/{wid}/pr', {'digest': pr['digest'], 'title': 't', 'body': ''}, 409)['code'], 'stale')
        self.assertFalse(any(c['argv'][:2] == ['pr', 'create'] for c in self.gh_calls()))

    def test_only_listed_files_and_known_repositories(self):
        wid, path = self.clone_work()
        (path / 'a.txt').write_text('a')
        head = self.get(f'/api/github/works/{wid}')['head']
        for files in (['missing.txt'], ['a.txt', 'a.txt'], ['--all']):
            self.assertEqual(self.post(f'/api/github/works/{wid}/commit', {'files': files, 'message': 'm', 'expected_head': head}, 422)['code'], 'invalid_files')
        self.assertEqual(self.client.post(f'/api/github/works/{wid}/commit', json={'files': ['a.txt'], 'message': 'a\0b'}).status_code, 422)
        sh('git', 'remote', 'set-url', 'origin', 'https://evil.example/octo/demo.git', cwd=path)
        self.assertEqual(self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'm', 'expected_head': head}, 409)['code'], 'not_github')

    def test_agent_written_git_config_is_refused(self):
        wid, path = self.clone_work()
        for key, value in (('credential.helper', f'!touch {self.marker}'), ('core.hooksPath', '/tmp'), ('core.fsmonitor', 'true'),
                           ('url.https://evil.example/.insteadOf', 'https://github.com/'), ('include.path', '/tmp/x'),
                           ('remote.origin.pushurl', 'https://evil.example/x.git'), ('filter.x.clean', f'touch {self.marker}')):
            with self.subTest(key=key):
                sh('git', 'config', key, value, cwd=path)
                (path / '.gitattributes').write_text('* filter=x\n')   # with a same-size edit of a tracked file,
                (path / 'README.md').write_text('DEMO\n')                # `git status` runs filter.x.clean
                self.assertIn(key.lower(), self.get(f'/api/github/works/{wid}')['unsafe_config'])
                (path / 'a.txt').write_text(key)
                r = self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'm', 'expected_head': None}, 409)
                self.assertEqual(r['code'], 'unsafe_git_config')
                sh('git', 'config', '--unset-all', key, cwd=path)
        self.assertFalse(self.marker.exists())

    def test_without_a_git_identity_the_noreply_address_is_used(self):   # DoD 4: no `<user>@<host>.local`
        wid, path = self.clone_work()
        sh('git', 'config', '--unset', 'user.name', cwd=path); sh('git', 'config', '--unset', 'user.email', cwd=path)
        (path / 'a.txt').write_text('a')
        head = self.get(f'/api/github/works/{wid}')['head']
        empty = Path(self.home); (empty / '.config').mkdir(exist_ok=True)
        with patch.dict(os.environ, {'HOME': str(empty), 'XDG_CONFIG_HOME': str(empty / '.config')}):   # no global identity
            self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'm', 'expected_head': head})
        self.assertEqual(sh('git', 'log', '-1', '--format=%an <%ae>', cwd=path).strip(), 'Octo Cat <42+octo@users.noreply.github.com>')
        (path / 'c.txt').write_text('c'); head = self.get(f'/api/github/works/{wid}')['head']   # a blank profile name → login
        with patch.dict(os.environ, {'HOME': str(empty), 'XDG_CONFIG_HOME': str(empty / '.config'), 'FAKE_GH_MODE': 'blank'}):
            self.post(f'/api/github/works/{wid}/commit', {'files': ['c.txt'], 'message': 'm', 'expected_head': head})
        self.assertEqual(sh('git', 'log', '-1', '--format=%an', cwd=path).strip(), 'octo')
        sh('git', 'config', 'user.email', 'me@example.com', cwd=path); (path / 'b.txt').write_text('b')   # an own address wins
        head = self.get(f'/api/github/works/{wid}')['head']
        self.post(f'/api/github/works/{wid}/commit', {'files': ['b.txt'], 'message': 'm', 'expected_head': head})
        self.assertEqual(sh('git', 'log', '-1', '--format=%ae', cwd=path).strip(), 'me@example.com')

    def test_an_empty_repository_gets_a_clear_message(self):   # found in the real GitHub check (DoD 4)
        wid, path = self.clone_work()
        (path / 'a.txt').write_text('a')
        head = self.get(f'/api/github/works/{wid}')['head']
        with patch.dict(os.environ, {'FAKE_GH_MODE': 'empty'}):
            r = self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'm', 'expected_head': head}, 409)
        self.assertEqual(r['code'], 'empty_repo')
        self.assertIn('README', r['detail'])

    def test_a_busy_workspace_is_left_alone(self):
        wid, path = self.clone_work()
        (path / 'a.txt').write_text('a')
        repo.create_session(self.conn, 's1', str(path), 'auto')
        self.assertEqual(self.post(f'/api/github/works/{wid}/commit', {'files': ['a.txt'], 'message': 'm', 'expected_head': None}, 409)['code'], 'workspace_busy')

    # ── boundaries ──
    def test_timeout_kills_the_process_group(self):
        with patch.dict(os.environ, {'FAKE_GH_MODE': 'sleep'}), patch.object(github, 'TIMEOUT', 1):
            started = time.monotonic()
            self.assertEqual(self.get('/api/github/status', 504)['code'], 'timeout')
            self.assertLess(time.monotonic() - started, 10)

    def test_mac_only(self):
        self.assertEqual(self.client.post('/api/github/clone', json={'repo': 'octo/demo'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/github/status', headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        with self.assertRaises(HTTPException) as caught:
            github.mac_only(SimpleNamespace(state=SimpleNamespace(conductor_mobile_device={'id': 'phone'})))
        self.assertEqual(caught.exception.status_code, 404)
        for method, path in (('GET', '/api/github/status'), ('POST', '/api/github/clone'), ('POST', '/api/github/works/x/push')):
            self.assertEqual(mobile._route(method, path), (False, None))   # the gateway does not route them


if __name__ == '__main__':
    unittest.main()
