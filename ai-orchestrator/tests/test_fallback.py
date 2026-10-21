import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from app import store, runner_store as repo
from app.runner import SessionRunner
from app.provider_policy import normalize, load_policy, digest
from app.provider_errors import codex_error, claude_error
from app.adapters.base import AdapterResult
from app.adapters.process import ProcessCleanupError, same_identity, stop_process
from app.handoff import snapshot, differences
from tests.test_runner import Fake, success

POLICY={'roles':{'author':['codex','claude'],'reviewer':['claude','codex']}}


def quota():
    return AdapterResult('partial work','old-session',None,'error',True,'failed',0,'QUOTA_EXCEEDED',{'code':'test-fixture'})


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ws=self.root/'workspace';self.ws.mkdir()
        self.db=self.root/'state.db';self.conn=store.connect(self.db);self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def create(self,roles=('author',),policy=None):
        frozen=normalize(policy or POLICY)
        with store.transaction(self.conn):
            repo.create_session(self.conn,'s',str(self.ws),'auto',policy=frozen)
            for i,role in enumerate(roles):
                repo.add_session_step(self.conn,f's{i}','s',i,frozen['roles'][role][0]['provider'],'goal',role=role)

    def attempts(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM session_attempts ORDER BY started_at')]

    async def test_partial_work_quota_handoff_locked_and_hash_evidence(self):
        self.create()
        (self.ws/'existing.txt').write_text('user content')
        test=self
        class Primary(Fake):
            async def run(self,prompt,workspace,provider_session_id=None,*,profile):
                (Path(workspace)/'new.txt').write_text('partial')
                async for e in super().run(prompt,workspace,provider_session_id,profile=profile):yield e
        class Backup(Fake):
            async def run(self,prompt,workspace,provider_session_id=None,*,profile):
                test.assertEqual(profile,'WRITE')
                test.assertIsNone(provider_session_id)
                test.assertEqual(test.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0],1)
                test.assertEqual((Path(workspace)/'new.txt').read_text(),'partial')
                test.assertIn('HANDOFF',prompt)
                (Path(workspace)/'new.txt').write_text('finished')
                async for e in super().run(prompt,workspace,provider_session_id,profile=profile):yield e
        primary,backup=Primary(quota()),Backup()
        await SessionRunner(self.db,adapter_factory=lambda p:primary if p=='codex' else backup).run_session('s')
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'SUCCEEDED')
        self.assertEqual([a['status'] for a in self.attempts()],['QUOTA','COMPLETED'])
        package=json.loads(self.conn.execute("SELECT payload FROM session_events WHERE kind='handoff'").fetchone()[0])
        self.assertEqual(package['from'],'codex');self.assertEqual(package['to'],'claude')
        self.assertEqual(package['package']['changed_files'][0]['path'],'new.txt')
        self.assertEqual(package['package']['test_results'],'unknown')
        self.assertEqual((self.ws/'existing.txt').read_text(),'user content')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_snapshots').fetchone()[0],4)

    async def test_chain_exhausted_no_repeated_provider(self):
        self.create()
        await SessionRunner(self.db,adapter_factory=lambda _:Fake(quota())).run_session('s')
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'WAITING_CAPACITY')
        self.assertEqual([a['provider'] for a in self.attempts()],['codex','claude'])

    async def test_unknown_error_with_quota_text_does_not_fallback(self):
        self.create()
        bad=AdapterResult('quota exceeded',None,None,'error',True,'api_error',1)
        await SessionRunner(self.db,adapter_factory=lambda _:Fake(bad)).run_session('s')
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'FAILED')
        self.assertEqual(len(self.attempts()),1)

    async def test_success_with_quota_text_does_not_fallback(self):
        self.create()
        await SessionRunner(self.db,adapter_factory=lambda _:Fake(success('quota discussion'))).run_session('s')
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'SUCCEEDED')
        self.assertEqual(len(self.attempts()),1)

    async def test_cleanup_error_never_launches_backup(self):
        self.create();calls=[]
        class Broken(Fake):
            async def run(self,*args,**kwargs):
                try:
                    async for e in super().run(*args,**kwargs):yield e
                finally:raise ProcessCleanupError('group remains')
        def factory(provider):calls.append(provider);return Broken(quota())
        await SessionRunner(self.db,adapter_factory=factory).run_session('s')
        self.assertEqual(calls,['codex'])
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'RECOVERY_REQUIRED')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0],1)

    async def test_missing_cleanup_receipt_never_launches_backup(self):
        self.create()
        class Unconfirmed:
            async def run(self,*args,**kwargs):yield {'type':'failed','result':quota()}
        await SessionRunner(self.db,adapter_factory=lambda _:Unconfirmed()).run_session('s')
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'RECOVERY_REQUIRED')
        self.assertEqual(len(self.attempts()),1)

    async def test_roles_do_not_share_provider_sessions(self):
        self.create(roles=('author','reviewer','author'),policy={'roles':{'author':['codex'],'reviewer':['codex']}})
        calls=[]
        class Recorder(Fake):
            async def run(self,p,w,sid=None,*,profile):
                calls.append((profile,sid))
                self.result=success('done',profile+'-session')
                async for e in super().run(p,w,sid,profile=profile):yield e
        await SessionRunner(self.db,adapter_factory=lambda _:Recorder()).run_session('s')
        self.assertEqual(calls,[('WRITE',None),('READ_ONLY',None),('WRITE','WRITE-session')])

    async def test_policy_frozen_despite_file_change(self):
        path=self.root/'providers.yaml';path.write_text('roles:\n  author: [codex, claude]\n  reviewer: [claude]\n')
        frozen=load_policy(path)
        self.create(policy={'roles':{r:v for r,v in frozen['roles'].items()}})
        path.write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n')
        calls=[]
        def factory(p):calls.append(p);return Fake(quota() if p=='codex' else success())
        await SessionRunner(self.db,adapter_factory=factory).run_session('s')
        self.assertEqual(calls,['codex','claude'])
        self.assertNotEqual(digest(repo.frozen_policy(self.conn,'s')),digest(load_policy(path)))
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE session_policies SET policy_json='{}'")

    async def test_policy_rejects_duplicates_wrong_profiles_and_approval(self):
        for data in [dict(POLICY,require_handover_approval=True),
                     {'roles':{'author':['codex','codex'],'reviewer':['claude']}},
                     {'roles':{'author':[{'provider':'codex','profile':'READ_ONLY'}],'reviewer':['claude']}}]:
            with self.assertRaises(ValueError):normalize(data)

    async def test_hash_ignores_mtime_and_tracks_delete_add_symlink(self):
        file=self.ws/'a';file.write_text('same')
        before=snapshot(self.ws);os.utime(file,(1,1))
        self.assertEqual(differences(before,snapshot(self.ws)),[])
        file.unlink();(self.ws/'b').symlink_to('/etc/passwd')
        changes=differences(before,snapshot(self.ws))
        self.assertEqual([c['path'] for c in changes],['a','b'])
        self.assertEqual(changes[1]['after']['kind'],'symlink')

    async def test_structured_error_mapping_only(self):
        self.assertEqual(codex_error({'codexErrorInfo':'usageLimitExceeded'}),'QUOTA_EXCEEDED')
        self.assertEqual(codex_error({'message':'usageLimitExceeded'}),'UNKNOWN')
        self.assertEqual(claude_error({'type':'assistant','error':'rate_limit'}),'QUOTA_EXCEEDED')
        self.assertEqual(claude_error({'type':'assistant','message':{'content':'rate_limit'}}),'UNKNOWN')

    async def test_reused_pid_refuses_signal(self):
        owner={'pid':123,'pgid':123,'start_time':1,'boot_time':1}
        reused=dict(owner,start_time=2)
        process=AsyncMock();process.pid=123
        with patch('app.adapters.process.group_members',return_value=[reused]),patch('app.adapters.process.identity',return_value=reused),patch('app.adapters.process.os.killpg') as kill:
            with self.assertRaises(ProcessCleanupError):await stop_process(process,owner,timeout=.01)
        kill.assert_not_called()
        self.assertFalse(same_identity(owner,reused))

    async def test_post_kill_group_still_alive_is_cleanup_error(self):
        owner={'pid':123,'pgid':123,'start_time':1,'boot_time':1}
        process=AsyncMock();process.pid=123
        with patch('app.adapters.process.group_members',return_value=[owner]),patch('app.adapters.process.identity',return_value=owner),patch('app.adapters.process.os.killpg'):
            with self.assertRaises(ProcessCleanupError):await stop_process(process,owner,timeout=.01)

    async def test_auth_does_not_use_backup(self):
        self.create()
        bad=AdapterResult('login needed',None,None,'error',True,'failed',1,'AUTH')
        await SessionRunner(self.db,adapter_factory=lambda _:Fake(bad)).run_session('s')
        self.assertEqual(repo.get_session(self.conn,'s')['status'],'WAITING_AUTH')
        self.assertEqual(len(self.attempts()),1)
