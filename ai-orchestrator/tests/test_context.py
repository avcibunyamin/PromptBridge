import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from app import store, runner_store as repo
from app.runner import SessionRunner
from app.provider_policy import normalize, digest
from app.adapters.base import AdapterResult
from app.context_digest import build, encode, INSTRUCTION, PromptTooLarge
from tests.test_runner import Fake, success


def context():
    return AdapterResult('partial', 'full-thread', None, 'error', True, 'failed', 0,
                         'CONTEXT_EXHAUSTED', {'codexErrorInfo':'contextWindowExceeded'})


def quota():
    result = context(); result.error_code = 'QUOTA_EXCEEDED'
    return result


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.ws = self.root/'work'; self.ws.mkdir()
        self.db = self.root/'db'; self.conn = store.connect(self.db); self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def create(self, roles=('author',), limit=3, legacy=False, goal='finish existing work'):
        policy = normalize({'roles':{'author':['codex','claude'],'reviewer':['claude','codex']},'max_context_continuations':limit})
        if legacy:
            policy.pop('max_context_continuations'); policy.pop('context_continuation_providers'); policy['version']=1
        repo.create_session(self.conn,'s',str(self.ws),'auto',policy=policy)
        for i, role in enumerate(roles):
            repo.add_session_step(self.conn,f's{i}','s',i,policy['roles'][role][0]['provider'],goal,role=role)

    def attempts(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM session_attempts ORDER BY started_at,attempt_no')]

    def status(self):
        return repo.get_session(self.conn,'s')['status']

    async def run_results(self, results, **kwargs):
        calls=[]
        def factory(provider):
            fake=Fake(results[len(calls)]); calls.append((provider,fake)); return fake
        await SessionRunner(self.db,adapter_factory=factory,**kwargs).run_session('s')
        return calls

    async def test_fresh_context_resume_new_id_later_role(self):
        self.create(('author','reviewer','author'))
        calls=await self.run_results([context(),success('done','new-thread'),success('review','review-thread'),success()])
        self.assertEqual(self.status(),'SUCCEEDED')
        self.assertEqual([p for p,_ in calls],['codex','codex','claude','codex'])
        self.assertIsNone(calls[1][1].calls[0][1])
        self.assertEqual(calls[3][1].calls[0][1],'new-thread')
        self.assertIn(INSTRUCTION,calls[1][1].calls[0][0])
        attempts=self.attempts()
        self.assertEqual(attempts[1]['continuation_of'],attempts[0]['id'])
        self.assertEqual(attempts[0]['status'],'CONTEXT_EXHAUSTED')
        self.assertEqual(attempts[0]['provider_session_id'],'full-thread')

    async def test_three_extra_then_wait_and_release(self):
        self.create(); calls=await self.run_results([context() for _ in range(4)])
        self.assertEqual(len(calls),4); self.assertEqual(self.status(),'WAITING_CONTEXT')
        self.assertEqual([a['continuation_no'] for a in self.attempts()],[0,1,2,3])
        self.assertEqual(repo.get_session_steps(self.conn,'s')[0]['terminal_reason'],'context_continuation_exhausted')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0],0)
        prompts=[f.calls[0][0] for _,f in calls[1:]]
        self.assertTrue(all(p.count('CONTINUATION:')==1 for p in prompts))

    async def test_disabled(self):
        self.create(limit=0); calls=await self.run_results([context()])
        self.assertEqual(len(calls),1); self.assertEqual(self.status(),'WAITING_CONTEXT')
        self.assertEqual(repo.get_session_steps(self.conn,'s')[0]['terminal_reason'],'context_continuation_disabled')

    async def test_legacy_policy_unchanged_effective_zero(self):
        self.create(legacy=True)
        before=tuple(self.conn.execute('SELECT * FROM session_policies').fetchone())
        await self.run_results([context()]); self.assertEqual(self.status(),'WAITING_CONTEXT')
        self.assertEqual(tuple(self.conn.execute('SELECT * FROM session_policies').fetchone()),before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE session_policies SET policy_json='{}'")

    async def test_context_then_quota_then_backup(self):
        self.create(); calls=await self.run_results([context(),quota(),success()])
        self.assertEqual([p for p,_ in calls],['codex','codex','claude'])
        self.assertEqual([a['attempt_no'] for a in self.attempts()],[1,2,3])
        self.assertEqual(repo.continuation_count(self.conn,'s0'),1)
        self.assertEqual(self.status(),'SUCCEEDED')

    async def test_reviewer_quota_then_context_shared_cap(self):
        self.create(('reviewer',))
        calls=await self.run_results([quota()]+[context() for _ in range(4)])
        self.assertEqual([p for p,_ in calls],['claude','codex','codex','codex','codex'])
        self.assertEqual(self.status(),'WAITING_CONTEXT')
        self.assertTrue(all(a['profile']=='READ_ONLY' for a in self.attempts()))
        self.assertEqual([a['attempt_no'] for a in self.attempts()],[1,2,3,4,5])

    async def test_unsupported_provider_no_continuation(self):
        self.create(('reviewer',)); await self.run_results([context()])
        self.assertEqual(self.status(),'FAILED'); self.assertEqual(len(self.attempts()),1)

    async def test_context_cleanup_missing_retains_lock(self):
        self.create()
        class Unclean:
            async def run(self,*a,**kw): yield {'type':'failed','result':context()}
        await SessionRunner(self.db,adapter_factory=lambda _:Unclean()).run_session('s')
        self.assertEqual(self.status(),'RECOVERY_REQUIRED'); self.assertEqual(len(self.attempts()),1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0],1)

    async def test_checkpoint_failure_rolls_back_prepared_attempt(self):
        self.create()
        self.conn.execute("CREATE TRIGGER break_digest BEFORE INSERT ON session_events WHEN NEW.kind='context_continuation' BEGIN SELECT RAISE(ABORT,'injected'); END")
        await self.run_results([context()])
        self.assertEqual(self.status(),'FAILED'); self.assertEqual(len(self.attempts()),1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_events WHERE kind='context_digest'").fetchone()[0],0)

    async def test_deadline_not_reset(self):
        self.create(); calls=[]
        def factory(p):
            f=Fake(context() if not calls else success(),delay=.3); calls.append(f); return f
        await SessionRunner(self.db,adapter_factory=factory,step_timeout=.45).run_session('s')
        self.assertEqual(self.status(),'WAITING_HUMAN'); self.assertEqual(len(calls),2)   # K-061: resumable
        self.assertEqual(repo.get_session_steps(self.conn,'s')[0]['terminal_reason'],'step_timeout')

    async def test_cancel_during_digest_prevents_next_process(self):
        self.create(); runner=SessionRunner(self.db,adapter_factory=lambda _:Fake(context()))
        from app.context_digest import build as real_build
        def cancel(*args):
            value=real_build(*args); runner.cancel_session('s'); return value
        with patch('app.runner.build_digest',side_effect=cancel): await runner.run_session('s')
        self.assertEqual(self.status(),'CANCELLED'); self.assertEqual(len(self.attempts()),1)

    async def test_mandatory_goal_never_truncated(self):
        self.create(goal='ğ'*70000); await self.run_results([context()])
        self.assertEqual(self.status(),'WAITING_CONTEXT'); self.assertEqual(len(self.attempts()),1)
        self.assertEqual(repo.get_session_steps(self.conn,'s')[0]['terminal_reason'],'context_prompt_too_large')

    async def test_frozen_limit_not_reloaded(self):
        self.create(limit=1)
        (self.root/'providers.yaml').write_text('max_context_continuations: 3')
        await self.run_results([context(),context()])
        self.assertEqual(len(self.attempts()),2); self.assertEqual(self.status(),'WAITING_CONTEXT')

    async def test_digest_is_attempt_scoped_utf8_bounded_with_evidence(self):
        self.create(); repo.update_step_status(self.conn,'s0','RUNNING')
        repo.begin_attempt(self.conn,'a','s0',1,'codex','WRITE')
        repo.checkpoint(self.conn,'s','s0','output_delta',{'attempt_id':'other','text':'SECRET_OTHER_ATTEMPT'})
        for i in range(5):
            repo.checkpoint(self.conn,'s','s0','output_delta',{'attempt_id':'a','text':'ğ🙂'*10000})
        for i in range(100):
            repo.checkpoint(self.conn,'s','s0','command_result',{'attempt_id':'a','command':str(i)+'x'*1000,'exit_code':0})
        parent=dict(self.conn.execute('SELECT * FROM session_attempts').fetchone())
        step=repo.get_session_steps(self.conn,'s')[0]
        snap={'files':{},'unknown':[],'complete':True}
        package,prompt=build(self.conn,'s',step,parent,'goal',snap,snap,snap,{'session':'s','attempt':'a','after':'z'})
        self.assertLessEqual(len(encode(package).encode()),65536)
        self.assertLessEqual(len(prompt.encode()),131072)
        self.assertNotIn('SECRET_OTHER_ATTEMPT',prompt); self.assertIn('🙂',prompt)
        self.assertTrue(package['truncated']); self.assertGreater(package['omitted_count'],0)
        self.assertLessEqual(len(package['output']['tail'].encode()),16384)
        self.assertEqual(package['command_results'][0]['purpose'],'unknown')
        self.assertIn('session_events:',package['command_results'][0]['log_ref'])

    async def test_policy_integer_range_and_digest(self):
        base={'roles':{'author':['codex'],'reviewer':['claude']}}
        for n in (True,False,-1,4,1.5,'2',None):
            with self.subTest(n=n),self.assertRaises(ValueError):normalize(dict(base,max_context_continuations=n))
        self.assertNotEqual(digest(normalize(base)),digest(normalize(dict(base,max_context_continuations=0))))


class ParentGuardTests(unittest.TestCase):
    setUp = ContextTests.setUp
    create = ContextTests.create

    def parent(self):
        self.create(); repo.update_step_status(self.conn,'s0','RUNNING')
        repo.begin_attempt(self.conn,'parent','s0',1,'codex','WRITE')
        repo.end_attempt(self.conn,'parent','CONTEXT_EXHAUSTED',context())
        owner={'pid':1,'pgid':1,'start_time':1,'boot_time':1}
        repo.record_owner(self.conn,'parent',owner); repo.record_owner(self.conn,'parent',owner,stopped=True)

    def test_parent_constraints_and_race_one_child_one_checkpoint(self):
        self.parent()
        for parent,step,provider,profile in [('missing','s0','codex','WRITE'),('parent','other','codex','WRITE'),('parent','s0','claude','WRITE'),('parent','s0','codex','READ_ONLY')]:
            with self.assertRaises(ValueError):repo.prepare_continuation(self.conn,'bad',parent,step,provider,profile,{})
        self.conn.execute("UPDATE session_attempts SET error_code='QUOTA_EXCEEDED'")
        with self.assertRaises(ValueError):repo.prepare_continuation(self.conn,'bad','parent','s0','codex','WRITE',{})
        self.conn.execute("UPDATE session_attempts SET error_code='CONTEXT_EXHAUSTED'")
        barrier=threading.Barrier(2)
        def race(aid):
            c=store.connect(self.db)
            try:
                barrier.wait()
                repo.prepare_continuation(c,aid,'parent','s0','codex','WRITE',{})
                return True
            except sqlite3.IntegrityError:return False
            finally:c.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(race,['a','b'])),[False,True])
        child=self.conn.execute("SELECT * FROM session_attempts WHERE continuation_of='parent'").fetchone()
        self.assertEqual(child['continuation_no'],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_events WHERE kind='context_continuation'").fetchone()[0],1)


class MigrationTests(unittest.TestCase):
    def test_early_1_1_without_attempt_table_installs_new_tables(self):
        c=store.connect(':memory:')
        try:
            schema=Path(store.__file__).with_name('schema.sql').read_text()
            c.executescript(schema[:schema.index('CREATE TABLE IF NOT EXISTS session_policies')])
            c.execute("INSERT INTO schema_meta VALUES ('schema_version','1.1')")
            eid,vid=store.insert_captured(c,{'id':'old','source_agent':'chatgpt','target_agent':'claude'},b'old bytes')
            store.migrate(c)
            self.assertEqual(store.get_content(c,vid),b'old bytes')
            self.assertEqual(c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],store.SCHEMA_VERSION)
            self.assertEqual(c.execute('SELECT count(*) FROM session_attempts').fetchone()[0],0)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
        finally:c.close()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.db=self.root/'old.db'
        self.conn=store.connect(self.db); self.addCleanup(self.conn.close)
        schema=Path(store.__file__).with_name('schema.sql').read_text()
        start=schema.index('  continuation_no INTEGER')
        end=schema.index('\n);',start)
        schema=schema[:start]+'  UNIQUE(step_id,attempt_no),\n  UNIQUE(step_id,provider)'+schema[end:]
        self.conn.executescript(schema)
        self.conn.execute("INSERT INTO schema_meta VALUES ('schema_version','1.1')")
        repo.create_session(self.conn,'s',str(self.root),'auto')
        repo.add_session_step(self.conn,'step','s',0,'codex','goal')
        repo.begin_attempt(self.conn,'a','step',1,'codex','WRITE')
        repo.record_owner(self.conn,'a',{'pid':1,'pgid':1,'start_time':1,'boot_time':1})
        repo.save_snapshot(self.conn,'s','a','before',{'proof':True})
        repo.checkpoint(self.conn,'s','step','existing',{'proof':True})
        self.conn.execute('CREATE INDEX custom_attempt_status ON session_attempts(status)')

    def test_rebuild_preserves_rows_fk_indexes_and_idempotence(self):
        tables=['session_attempts','process_owners','workspace_snapshots','session_policies','session_events']
        before={t:[dict(r) for r in self.conn.execute('SELECT * FROM '+t)] for t in tables}
        store.migrate(self.conn); store.migrate(self.conn)
        for table in tables:
            rows=[dict(r) for r in self.conn.execute('SELECT * FROM '+table)]
            if table=='session_attempts':
                self.assertEqual(rows[0].pop('continuation_no'),0); self.assertIsNone(rows[0].pop('continuation_of'))
            self.assertEqual(rows,before[table])
        self.assertFalse(self.conn.execute('PRAGMA foreign_key_check').fetchall())
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertIsNotNone(self.conn.execute("SELECT sql FROM sqlite_master WHERE name='custom_attempt_status'").fetchone())
        # Same provider now allowed only as a self-referenced continuation.
        self.conn.execute("INSERT INTO session_attempts(id,step_id,attempt_no,provider,profile,started_at,status,continuation_no,continuation_of) VALUES ('b','step',2,'codex','WRITE','now','RUNNING',1,'a')")
        self.assertFalse(self.conn.execute('PRAGMA foreign_key_check').fetchall())

    def test_failure_rolls_back_old_schema_and_restores_fk(self):
        before=list(self.conn.iterdump())
        self.conn.execute("CREATE TRIGGER inject_version BEFORE UPDATE ON schema_meta BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):store.migrate(self.conn)
        self.conn.execute('DROP TRIGGER inject_version')
        self.assertEqual(list(self.conn.iterdump()),before)
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0],1)

    def test_unknown_version_no_writes_and_active_owner_blocks_upgrade(self):
        import fcntl
        with open(self.db.with_suffix('.runner.lock'),'a') as owner:
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):store.migrate(self.conn)
        self.conn.execute("UPDATE schema_meta SET value='future'")
        before=list(self.conn.iterdump())
        with self.assertRaises(ValueError):store.migrate(self.conn)
        self.assertEqual(list(self.conn.iterdump()),before)
