"""TB-26 / K-034: a work in its own git worktree; bring the result back to the source with a merge, or drop the copy.
Real git in temp folders; no provider and no network."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app import git_guard, mobile
from tests import test_tb12b_interrupt as tb12b
from tests.test_tb12b_interrupt import HOST, LOGIN

ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}


def sh(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout


def make_repo(path: Path, commit=True):
    path.mkdir(parents=True)
    sh(path, 'init', '-q', '-b', 'main')
    if commit:
        (path / 'app.txt').write_text('one\n')
        sh(path, 'add', '-A'); sh(path, 'commit', '-qm', 'first')
    return path


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        root = self.tmp / 'conductor'; (root / '.system').mkdir(parents=True)
        (root / '.system/providers.yaml').write_text('roles:\n  author: [claude]\n  reviewer: [claude]\n')
        p = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(root), 'CONDUCTOR_WORKTREES': str(self.tmp / 'copies'),
                                    'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'})
        p.start(); self.addCleanup(p.stop)
        from app.api import create_app
        self.app = create_app()
        self.c = TestClient(self.app, base_url='http://127.0.0.1:8765'); self.c.__enter__()
        self.addCleanup(self.c.__exit__, None, None, None)
        self.src = make_repo(self.tmp / 'proje')

    def create(self, path=None, wid=None, title='Karanlık tema'):
        return self.c.post('/api/works', json={'client_work_id': wid or str(uuid4()), 'title': title,
                                               'workspace_path': str(path or self.src), 'isolate': True})


class Isolation(Base):
    def test_create_runs_in_a_copy_and_a_replay_reuses_it(self):
        wid = str(uuid4())
        r = self.create(wid=wid); self.assertEqual(r.status_code, 201, r.text)
        copy = Path(r.json()['workspace_path'])
        self.assertEqual(copy.parent, self.tmp / 'copies')
        info = self.c.get(f'/api/works/{wid}').json()['isolation']
        self.assertEqual(info['source'], str(self.src)); self.assertTrue(info['branch'].startswith('conductor/'))
        self.assertEqual(sh(copy, 'branch', '--show-current').strip(), info['branch'])
        self.assertEqual(sh(self.src, 'branch', '--show-current').strip(), 'main')       # the source stays put
        again = self.create(wid=wid)
        self.assertEqual((again.status_code, again.json()['workspace_path']), (200, str(copy)))
        self.assertEqual(sh(self.src, 'worktree', 'list').count('\n'), 2)
        self.assertEqual(self.create(wid=wid, title='başka').status_code, 409)           # same id, other payload
        plain = self.c.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 't', 'workspace_path': str(self.src)})
        self.assertNotIn('isolation', [k for k, v in self.c.get(f"/api/works/{plain.json()['id']}").json().items() if v])

    def test_open_copies_per_source_are_capped(self):
        with patch('app.isolation.MAX_COPIES', 2):
            self.assertEqual(self.create().status_code, 201); self.assertEqual(self.create().status_code, 201)
            self.assertEqual(self.create().json()['code'], 'too_many_copies')

    def test_a_failed_add_leaves_no_branch_or_record(self):
        from app import isolation
        real, calls = isolation.git, []
        def failing(repo, *args, **kw):
            calls.append(args[0])
            if args[:2] == ('worktree', 'add'):
                real(repo, *args, **kw)                                                   # it really made them…
                raise isolation.GitHubError('git_failed', 'disk full')                    # …then failed
            return real(repo, *args, **kw)
        with patch('app.isolation.git', failing):
            self.assertEqual(self.create().json()['code'], 'git_failed')
        self.assertNotIn('conductor/', sh(self.src, 'branch', '--list'))
        self.assertEqual(sh(self.src, 'worktree', 'list').count('\n'), 1)

    def test_refusals(self):
        (self.tmp / 'plain').mkdir()
        self.assertEqual(self.create(self.tmp / 'plain').json()['code'], 'not_a_repo')
        inner = make_repo(self.tmp / 'plain' / 'inner'); (self.tmp / 'plain' / 'loose').mkdir()   # K-055: offers inner repos
        (self.tmp / 'plain' / 'link').symlink_to(inner)
        self.assertEqual(self.create(self.tmp / 'plain').json()['repos'], [str(inner.resolve())])
        self.assertEqual(self.create(self.src / '..' / 'proje' / '.git' / '..').status_code, 201)   # canonical root is fine
        self.assertEqual(self.create(make_repo(self.tmp / 'bos', commit=False)).json()['code'], 'empty_repo')
        sh(self.src, 'config', 'core.hooksPath', '/tmp/evil')
        self.assertEqual(self.create().json()['code'], 'unsafe_git_config')

    def preview(self, wid):
        r = self.c.get(f'/api/isolation/works/{wid}/apply'); self.assertEqual(r.status_code, 200, r.text); return r.json()

    def test_apply_merges_into_the_source_only_when_clean_and_unchanged(self):
        wid = str(uuid4()); copy = Path(self.create(wid=wid).json()['workspace_path'])
        (copy / 'new.txt').write_text('x\n'); (copy / '.gitattributes').write_text('* text\n'); (copy / 'sub').mkdir(); (copy / 'sub/.gitattributes').write_text('* text\n')
        v = self.preview(wid)
        self.assertEqual((v['target'], v['uncommitted'], v['source_dirty'], v['attention']),
                         ('main', ['.gitattributes', 'new.txt', 'sub/.gitattributes'], [], ['.gitattributes', 'sub/.gitattributes']))
        (self.src / 'app.txt').write_text('dirty\n')
        self.assertEqual(self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': v['digest']}).json()['code'], 'stale')
        r = self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': self.preview(wid)['digest']})
        self.assertEqual(r.json()['code'], 'source_dirty')
        sh(self.src, 'checkout', '--', 'app.txt')
        digest = self.preview(wid)['digest']
        from app import store
        conn = store.connect(self.app.state.runner_db); self.addCleanup(conn.close)
        conn.execute('PRAGMA foreign_keys=OFF')   # a lock row without building a whole session
        with store.transaction(conn):   # a run on the source folder itself
            conn.execute('INSERT INTO workspace_locks VALUES (?,?,?)', (str(self.src), 'sess_x', 'now'))
        self.assertEqual(self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': digest}).json()['code'], 'workspace_busy')
        with store.transaction(conn):
            conn.execute('DELETE FROM workspace_locks')
        r = self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': digest})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.src / 'new.txt').read_text(), 'x\n')
        self.assertIn('Conductor: Karanlık tema', sh(self.src, 'log', '-1', '--format=%s'))
        self.assertEqual(sh(self.src, 'status', '--porcelain'), '')
        r = self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': self.preview(wid)['digest']})
        self.assertEqual(r.json()['code'], 'nothing')

    def test_a_conflict_leaves_the_source_unchanged(self):
        wid = str(uuid4()); copy = Path(self.create(wid=wid).json()['workspace_path'])
        (copy / 'app.txt').write_text('copy\n')
        (self.src / 'app.txt').write_text('source\n'); sh(self.src, 'commit', '-qam', 'source side')
        head = sh(self.src, 'rev-parse', 'HEAD')
        r = self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': self.preview(wid)['digest']})
        self.assertEqual(r.json()['code'], 'merge_conflict')
        self.assertEqual((sh(self.src, 'rev-parse', 'HEAD'), sh(self.src, 'status', '--porcelain')), (head, ''))
        self.assertEqual((self.src / 'app.txt').read_text(), 'source\n')

    def test_apply_refuses_when_source_has_merge_head_without_committing_in_copy(self):
        wid = str(uuid4()); copy = Path(self.create(wid=wid).json()['workspace_path'])
        (copy / 'new.txt').write_text('x\n')
        copy_head = sh(copy, 'rev-parse', 'HEAD')
        (self.src / '.git/MERGE_HEAD').write_text(sh(self.src, 'rev-parse', 'HEAD'))
        r = self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': self.preview(wid)['digest']})
        self.assertEqual((r.status_code, r.json()['code']), (409, 'source_busy'))
        self.assertEqual(sh(copy, 'rev-parse', 'HEAD'), copy_head)

    def test_apply_does_not_overwrite_ignored_local_file_in_source(self):
        (self.src / '.gitignore').write_text('local.env\n')
        sh(self.src, 'add', '.gitignore'); sh(self.src, 'commit', '-qm', 'ignore local.env')
        (self.src / 'local.env').write_text('secret-local\n')
        wid = str(uuid4()); copy = Path(self.create(wid=wid).json()['workspace_path'])
        (copy / 'local.env').write_text('from-copy\n'); sh(copy, 'add', '-f', 'local.env'); sh(copy, 'commit', '-qm', 'track local.env')
        r = self.c.post(f'/api/isolation/works/{wid}/apply', json={'digest': self.preview(wid)['digest']})
        self.assertEqual((r.status_code, r.json()['code']), (409, 'source_untracked'))
        self.assertEqual((self.src / 'local.env').read_text(), 'secret-local\n')
        self.assertEqual(sh(self.src, 'status', '--porcelain'), '')

    def test_create_refuses_existing_branch_and_keeps_it(self):
        from app import isolation
        wid = str(uuid4())
        _, branch = isolation.plan(str(self.src), wid, 'Karanlık tema')
        sh(self.src, 'branch', branch)
        r = self.create(wid=wid, title='Karanlık tema')
        self.assertEqual((r.status_code, r.json()['code']), (409, 'branch_exists'))
        self.assertIn(branch, sh(self.src, 'branch', '--list', branch))

    def test_remove_needs_archive_and_a_clean_copy_and_keeps_the_branch(self):
        wid = str(uuid4()); r = self.create(wid=wid); copy = Path(r.json()['workspace_path'])
        branch = self.c.get(f'/api/works/{wid}').json()['isolation']['branch']
        self.assertEqual(self.c.post(f'/api/isolation/works/{wid}/remove').json()['code'], 'not_archived')
        self.c.patch(f'/api/works/{wid}', json={'expected_revision': 0, 'archived': True})
        (copy / 'wip.txt').write_text('w')
        self.assertEqual(self.c.post(f'/api/isolation/works/{wid}/remove').json()['code'], 'copy_dirty')
        (copy / 'wip.txt').unlink()
        r = self.c.post(f'/api/isolation/works/{wid}/remove')
        self.assertEqual((r.status_code, r.json()['branch_deleted']), (200, True))                 # nothing unmerged on it
        self.assertFalse(copy.exists())
        self.assertEqual(sh(self.src, 'branch', '--list', branch), '')
        self.assertTrue(self.c.get(f'/api/works/{wid}').json()['isolation']['removed'])
        self.assertEqual(self.c.get(f'/api/isolation/works/{wid}/apply').json()['code'], 'not_isolated')

    def test_a_copy_moved_to_another_branch_or_repo_is_refused(self):
        wid = str(uuid4()); copy = Path(self.create(wid=wid).json()['workspace_path'])
        sh(copy, 'switch', '-q', '-c', 'other')
        self.assertEqual(self.c.get(f'/api/isolation/works/{wid}/apply').json()['code'], 'copy_moved')
        branch = self.c.get(f'/api/works/{wid}').json()['isolation']['branch']
        sh(copy, 'switch', '-q', branch)
        self.assertEqual(self.c.get(f'/api/isolation/works/{wid}/apply').status_code, 200)
        fake = make_repo(self.tmp / 'fake'); sh(fake, 'branch', branch)
        (copy / '.git').write_text(f'gitdir: {fake}/.git\n')                          # points at another repo
        self.assertEqual(self.c.get(f'/api/isolation/works/{wid}/apply').json()['code'], 'copy_moved')

    def test_remove_keeps_an_unmerged_branch(self):
        wid = str(uuid4()); copy = Path(self.create(wid=wid).json()['workspace_path'])
        branch = self.c.get(f'/api/works/{wid}').json()['isolation']['branch']
        (copy / 'w.txt').write_text('w'); sh(copy, 'add', '-A'); sh(copy, 'commit', '-qm', 'unmerged work')
        self.c.patch(f'/api/works/{wid}', json={'expected_revision': 0, 'archived': True})
        r = self.c.post(f'/api/isolation/works/{wid}/remove')
        self.assertEqual((r.status_code, r.json()['branch_deleted']), (200, False))
        self.assertIn(branch, sh(self.src, 'branch', '--list', branch))

    def test_cross_site_is_refused(self):
        wid = str(uuid4()); self.create(wid=wid)
        r = self.c.post(f'/api/isolation/works/{wid}/remove', headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 403)

    def test_git_guard_sees_the_shared_hooks_of_a_copy(self):
        copy = Path(self.create().json()['workspace_path'])
        before = git_guard.fingerprint(copy)
        self.assertIn('.git', before)
        hook = self.src / '.git/hooks/pre-commit'; hook.write_text('#!/bin/sh\n'); hook.chmod(0o755)
        self.assertIn('common:hooks/pre-commit', git_guard.changes(before, git_guard.fingerprint(copy)))


class Phone(unittest.TestCase):
    setUp, scopes = tb12b.Interrupt.setUp, tb12b.Interrupt.scopes

    def send(self, method, path, **kw):
        return self.gw.request(method, path, headers={'Authorization': f'Bearer {self.token}', 'Tailscale-User-Login': LOGIN,
                                                      'Origin': f'https://{HOST}'}, **kw)

    def test_a_phone_may_start_a_copy_but_not_apply_or_remove(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        p = patch.dict(os.environ, {'CONDUCTOR_WORKTREES': tmp.name + '/copies', 'GIT_CONFIG_GLOBAL': '/dev/null'})
        p.start(); self.addCleanup(p.stop)
        n = patch('app.mobile.notify_mac'); n.start(); self.addCleanup(n.stop)
        src = str(make_repo(Path(tmp.name).resolve() / 'proje'))
        self.local.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'mac', 'workspace_path': src})
        self.scopes(['command'])
        wid = str(uuid4())
        r = self.send('POST', '/api/works', json={'client_work_id': wid, 'title': 'tel', 'workspace_path': src, 'isolate': True})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertNotEqual(r.json()['workspace_path'], src)
        for method, path in (('GET', f'/api/isolation/works/{wid}/apply'), ('POST', f'/api/isolation/works/{wid}/apply'),
                             ('POST', f'/api/isolation/works/{wid}/remove')):
            self.assertEqual((self.send(method, path).status_code), 404)                  # not on the gateway at all
        other = str(make_repo(Path(tmp.name).resolve() / 'other'))
        r = self.send('POST', '/api/works', json={'client_work_id': str(uuid4()), 'title': 't', 'workspace_path': other, 'isolate': True})
        self.assertEqual(r.json().get('code'), 'mobile_folder')                           # K-033/3 still applies
        plain = Path(tmp.name).resolve() / 'plain'; make_repo(plain / 'inner')            # K-055: no folder listing for a phone
        self.local.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'mac', 'workspace_path': str(plain)})
        r = self.send('POST', '/api/works', json={'client_work_id': str(uuid4()), 'title': 't', 'workspace_path': str(plain), 'isolate': True})
        self.assertEqual((r.json().get('code'), 'repos' in r.json()), ('not_a_repo', False))
        self.assertEqual(self.local.post('/api/works', json={'client_work_id': str(uuid4()), 'title': 'm', 'workspace_path': str(plain),
                                                             'isolate': True}).json()['repos'], [str(plain / 'inner')])


if __name__ == '__main__':
    unittest.main()
