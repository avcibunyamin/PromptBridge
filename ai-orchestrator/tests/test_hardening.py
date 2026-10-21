"""F2-04: measured WAL concurrency, guarded mutations and persistent versions."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from starlette.testclient import TestClient
from app import store, delivery
from app.api import create_app
from app.security import ALLOWED_ORIGINS
from app.transitions import transition


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'.system').mkdir()
        self.db=self.root/'.system/state.db'
        self.conn=store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        env=patch.dict(os.environ,{'AI_ORCHESTRATOR_ROOT':str(self.root)})
        env.start(); self.addCleanup(env.stop)

    def capture(self):
        eid,vid=store.insert_captured(self.conn,{'id':'e','source_agent':'chatgpt','target_agent':'claude'},b'original')
        transition(self.conn,eid,'enqueue',expected_state='CAPTURED',actor='test',payload={})
        return eid,vid

    def test_wal_pragmas_persist_memory_remains_valid(self):
        for pragma,expected in [('journal_mode','wal'),('synchronous',1),('busy_timeout',5000),('foreign_keys',1)]:
            self.assertEqual(self.conn.execute('PRAGMA '+pragma).fetchone()[0],expected)
        c=store.connect(':memory:')
        try:
            store.migrate(c)
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'memory')
        finally:c.close()

    def test_wal_reader_sees_committed_snapshot_while_writer_holds_transaction(self):
        self.conn.execute('CREATE TABLE proof(value INTEGER)')
        self.conn.execute('INSERT INTO proof VALUES (1)')
        def read_during_write():
            c=store.connect(self.db)
            try:return c.execute('SELECT value FROM proof').fetchone()[0]
            finally:c.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store.transaction(self.conn):
                self.conn.execute('UPDATE proof SET value=2')
                self.assertEqual(pool.submit(read_during_write).result(timeout=2),1)
            self.assertEqual(pool.submit(read_during_write).result(timeout=2),2)

    def test_every_mutation_refuses_cross_site_before_state_changes(self):
        eid,_=self.capture()
        paths=[f'/api/envelopes/{eid}/{action}' for action in ('approve','reject','edit')]+[
            '/api/reconcile','/api/sessions/start','/api/sessions/missing/cancel',
            '/api/sessions/missing/recover','/api/sessions/missing/retry']
        with TestClient(create_app()) as client:
            for headers in ({'origin':'https://evil.invalid'},{'sec-fetch-site':'cross-site'},
                            {'origin':'http://localhost:8765','sec-fetch-site':'cross-site'}):
                for path in paths:
                    with self.subTest(path=path,headers=headers):
                        r=client.post(path,json={},headers=headers)
                        self.assertEqual(r.status_code,403,r.text)
                        self.assertEqual(r.json()['code'],'untrusted_origin')
            self.assertEqual(store.get_envelope(self.conn,eid)['state'],'PENDING_APPROVAL')
            self.assertEqual(self.conn.execute('SELECT count(*) FROM versions').fetchone()[0],1)
            self.assertEqual(client.get('/api/envelopes',headers={'origin':'https://evil.invalid'}).status_code,200)
            self.assertEqual(client.get('/ui/').status_code,200)

    def test_local_and_same_origin_accepted_cors_uses_same_policy(self):
        with TestClient(create_app(),base_url='http://localhost:8891') as client:
            for origin in (*ALLOWED_ORIGINS,'http://localhost:8891'):
                self.assertEqual(client.post('/api/reconcile',headers={'origin':origin}).status_code,200)
            for origin in ALLOWED_ORIGINS:
                r=client.options('/api/reconcile',headers={'origin':origin,'access-control-request-method':'POST'})
                self.assertEqual(r.headers['access-control-allow-origin'],origin)

    def test_errors_flat_409_422_404_500_without_internal_secrets(self):
        app=create_app()
        with TestClient(app,raise_server_exceptions=False) as client:
            eid,_=self.capture()
            self.assertEqual(client.post(f'/api/envelopes/{eid}/reject',json={}).status_code,200)
            responses=[client.post(f'/api/envelopes/{eid}/approve',json={}),
                       client.post('/api/sessions/start',json={}),client.get('/api/envelopes/missing')]
            with patch('app.api._get_conn',side_effect=RuntimeError('secret-private-value')):
                responses.append(client.get('/api/envelopes'))
            self.assertEqual([r.status_code for r in responses],[409,422,404,500])
            for r in responses:
                self.assertEqual(set(r.json()),{'detail','code'})
                self.assertIsInstance(r.json()['detail'],str)
                self.assertNotIn('secret-private-value',r.text)

    def test_edited_version_survives_publish_crash_reopen_reconcile(self):
        eid,captured=self.capture(); content='Düzenlenmiş içerik 🙂'.encode()
        edited=store.add_edited_version(self.conn,eid,content)
        transition(self.conn,eid,'human_approve',expected_state='PENDING_APPROVAL',actor='human',payload={'version_id':edited})
        real_transition=delivery.transition
        def crash(conn,eid,event,**kwargs):
            if str(event.value if hasattr(event,'value') else event)=='deliver_ok':
                raise KeyboardInterrupt('simulated crash after publication before DB completion')
            return real_transition(conn,eid,event,**kwargs)
        inbox=self.root/'inbox'
        with patch('app.delivery.transition',side_effect=crash), self.assertRaises(KeyboardInterrupt):
            delivery.deliver(self.conn,eid,inbox_dir=inbox)
        self.assertEqual(store.get_envelope(self.conn,eid)['state'],'DELIVERING')
        self.conn.close(); self.conn=store.connect(self.db); self.addCleanup(self.conn.close)
        result=delivery.reconcile(self.conn,inbox_dir=inbox)
        self.assertEqual(result[0]['action'],'recovered_delivered')
        env=store.get_envelope(self.conn,eid)
        self.assertEqual((env['captured_version_id'],env['approved_version_id'],env['delivered_version_id']),(captured,edited,edited))
        self.assertEqual(Path(env['delivered_path']).read_bytes(),content)
        self.assertEqual(env['delivered_sha256'],hashlib.sha256(content).hexdigest())
        self.assertEqual(store.get_content(self.conn,captured),b'original')
        self.assertEqual(delivery.reconcile(self.conn,inbox_dir=inbox),[])
