import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from app.api import create_app
from app import store, runner_store as repo
from tests.test_runner import Fake


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.system').mkdir()
        self.db = self.root / '.system/state.db'
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)})
        env.start(); self.addCleanup(env.stop)
        self.app = create_app()

    def start(self, client, **overrides):
        return client.post('/api/sessions/start', json={
            'workspace_path': str(self.root), 'task_prompt': 'make it',
            'policy_digest': client.get('/api/sessions/providers').json()['digest'], **overrides})

    def test_start_get_cancel_and_workspace_conflict(self):
        with TestClient(self.app) as client:
            self.app.state.runner.adapter_factory = lambda _: Fake(delay=30)
            response = self.start(client)
            self.assertEqual(response.status_code, 202)
            sid = response.json()['session_id']
            self.assertEqual(self.start(client).status_code, 409)
            self.assertEqual(len(client.get('/api/sessions').json()), 1)
            self.assertEqual(client.post(f'/api/sessions/{sid}/cancel').status_code, 202)
            for _ in range(100):
                data = client.get(f'/api/sessions/{sid}').json()
                if data['session']['status'] == 'CANCELLED':break
                time.sleep(.01)
            self.assertEqual(data['session']['status'], 'CANCELLED')

    def test_invalid_requests_and_cross_site_rejected(self):
        with TestClient(self.app) as client:
            self.assertEqual(self.start(client, agents=[]).status_code, 422)
            self.assertEqual(self.start(client, mode='manual').status_code, 422)
            self.assertEqual(self.start(client, task_prompt=' ').status_code, 422)
            self.assertEqual(self.start(client, workspace_path=str(self.root/'absent')).status_code, 422)
            response = client.post('/api/sessions/start', json={
                'workspace_path': str(self.root), 'task_prompt':'x',
                'policy_digest':client.get('/api/sessions/providers').json()['digest']}, headers={'origin':'https://untrusted.example'})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(client.get('/api/sessions').json(), [])
            with patch('app.session_api.provider_auth.check', return_value=('authenticated', 'now')) as check:
                cross = client.get('/api/sessions/providers?check_auth=true', headers={'sec-fetch-site': 'cross-site'})
                self.assertEqual((cross.status_code, cross.json()['code']), (403, 'untrusted_origin'))
                check.assert_not_called()
                self.assertEqual(client.get('/api/sessions/providers?check_auth=true').status_code, 200)

    def test_restart_requires_recovery_and_retains_lock(self):
        conn = store.connect(self.db);store.migrate(conn)
        repo.create_session(conn, 'interrupted', str(self.root), 'auto')
        self.assertTrue(repo.claim_session(conn, 'interrupted')); conn.close()
        with TestClient(self.app) as client:
            data = client.get('/api/sessions/interrupted').json()
            self.assertEqual(data['session']['status'], 'RECOVERY_REQUIRED')
            self.assertEqual(self.start(client).status_code, 409)
            conn=store.connect(self.db)
            self.assertEqual(conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0],1)
            conn.close()

    def test_terminal_events_replayed_by_cursor(self):
        conn=store.connect(self.db);store.migrate(conn)
        repo.create_session(conn,'done',str(self.root),'auto')
        repo.checkpoint(conn,'done',None,'output_delta',{'text':'first'})
        repo.checkpoint(conn,'done',None,'output_delta',{'text':'second'})
        repo.update_session_status(conn,'done','SUCCEEDED');conn.close()
        with TestClient(self.app) as client:
            response=client.get('/api/sessions/done/events?after=1')
            self.assertEqual(response.status_code,200)
            self.assertNotIn('first',response.text)
            self.assertIn('second',response.text)
            self.assertIn('SUCCEEDED',response.text)

    def test_prepared_or_started_continuation_requires_recovery_no_replay(self):
        from tests.test_context import context
        for started in (False, True):
            sid = 'started' if started else 'prepared'
            workspace=self.root/sid; workspace.mkdir()
            conn=store.connect(self.db); store.migrate(conn)
            repo.create_session(conn,sid,str(workspace),'auto')
            repo.add_session_step(conn,sid+'_step',sid,0,'codex','goal')
            repo.claim_session(conn,sid); repo.update_step_status(conn,sid+'_step','RUNNING')
            parent=sid+'_parent'; child=sid+'_child'
            repo.begin_attempt(conn,parent,sid+'_step',1,'codex','WRITE')
            repo.end_attempt(conn,parent,'CONTEXT_EXHAUSTED',context())
            owner={'pid':1,'pgid':1,'start_time':1,'boot_time':1}
            repo.record_owner(conn,parent,owner); repo.record_owner(conn,parent,owner,stopped=True)
            repo.prepare_continuation(conn,child,parent,sid+'_step','codex','WRITE',{'evidence':'fixture'})
            if started:
                repo.checkpoint(conn,sid,sid+'_step','started',{'attempt_id':child,'provider_session_id':'fresh'})
            conn.close()
            with TestClient(self.app) as client:
                data=client.get(f'/api/sessions/{sid}').json()
                self.assertEqual(data['session']['status'],'RECOVERY_REQUIRED')
                self.assertEqual(len(data['attempts']),2)
                self.assertEqual(self.app.state.runner_jobs,{})
                events=client.get(f'/api/sessions/{sid}/events').text
                self.assertIn('context_continuation',events)
                self.assertIn('to_attempt_id',events)
                # Last-Event-ID suppresses previously seen preparation on reconnect.
                conn=store.connect(self.db)
                seq=conn.execute('SELECT max(seq) FROM session_events WHERE session_id=?',(sid,)).fetchone()[0]
                conn.close()
                replay=client.get(f'/api/sessions/{sid}/events',headers={'Last-Event-ID':str(seq)}).text
                self.assertNotIn('context_continuation',replay)

    def test_context_policy_limit_is_present_and_changes_digest(self):
        with TestClient(self.app) as client:
            old=client.get('/api/sessions/providers').json()
            self.assertEqual(old['policy']['max_context_continuations'],3)
            (self.root/'.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\nmax_context_continuations: 0\n')
            self.assertEqual(self.start(client,policy_digest=old['digest']).status_code,409)

    def test_policy_changed_or_unsupported_approval_is_rejected(self):
        with TestClient(self.app) as client:
            old = client.get('/api/sessions/providers').json()['digest']
            (self.root/'.system/providers.yaml').write_text(
                'roles:\n  author: [codex, claude]\n  reviewer: [claude, codex]\n')
            self.assertEqual(self.start(client, policy_digest=old).status_code, 409)
            self.assertEqual(self.start(client, require_handover_approval=True).status_code, 422)
            self.assertEqual(client.get('/api/sessions').json(), [])

    def test_start_persists_accepted_policy_and_roles(self):
        with TestClient(self.app) as client:
            self.app.state.runner.adapter_factory = lambda _: Fake(delay=30)
            sid = self.start(client).json()['session_id']
            (self.root/'.system/providers.yaml').write_text(
                'roles:\n  author: [claude]\n  reviewer: [codex]\n')
            detail = client.get(f'/api/sessions/{sid}').json()
            self.assertEqual(detail['policy']['roles']['author'][0]['provider'], 'codex')
            self.assertEqual([s['role'] for s in detail['steps']], ['author','reviewer','author'])
            client.post(f'/api/sessions/{sid}/cancel')

    def test_cancel_waiting_states_and_preserve_waiting_capacity(self):
        with TestClient(self.app) as client:
            for status in ('WAITING_APPROVAL', 'WAITING_AUTH', 'WAITING_CONTEXT'):
                sid = f'sess_{status.lower()}'
                conn = store.connect(self.db)
                repo.create_session(conn, sid, str(self.root), 'auto')
                repo.update_session_status(conn, sid, status)
                conn.close()
                response = client.post(f'/api/sessions/{sid}/cancel')
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json(), {'status': 'CANCELLED'})
                self.assertEqual(client.get(f'/api/sessions/{sid}').json()['session']['status'], 'CANCELLED')
            cap_sid = 'sess_waiting_capacity'
            conn = store.connect(self.db)
            repo.create_session(conn, cap_sid, str(self.root), 'auto')
            repo.update_session_status(conn, cap_sid, 'WAITING_CAPACITY')
            conn.close()
            response = client.post(f'/api/sessions/{cap_sid}/cancel')
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), {'status': 'WAITING_CAPACITY'})
            self.assertEqual(client.get(f'/api/sessions/{cap_sid}').json()['session']['status'], 'WAITING_CAPACITY')

