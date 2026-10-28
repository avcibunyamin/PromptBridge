"""F3-04: opt-in bounded quota wait (card Rev 5, K-012) — plans, scheduler, WAITING_HUMAN, API."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from starlette.testclient import TestClient
from app import store, runner_store as repo, works_store as ws, wait_plans
from app.api import create_app
from app.provider_policy import normalize, digest
from app.runner import SessionRunner, snapshot_exclusions
from app.wait_plans import RetryScheduler, utcnow, iso
from tests.test_retry_generation import Script, ok, err

WAIT = {'deadline_hours': 4, 'max_attempts': 3}


def quota():
    return err('QUOTA_EXCEEDED')


def writing(name, result):
    """An attempt that changes the folder and then ends with `result` (the agent's own work)."""
    class Adapter:
        async def run(self, prompt, workspace, provider_session_id=None, *, profile):
            (Path(workspace) / name).write_text('agent work')
            owner = {'pid': 900, 'pgid': 900, 'start_time': 1.0, 'boot_time': 1.0}
            yield {'type': 'process_started', 'ownership': owner}
            yield {'type': 'started', 'provider_session_id': None}
            yield {'type': 'process_stopped', 'ownership': owner}
            yield {'type': 'completed' if result.successful else 'failed', 'result': result}
    return lambda provider: Adapter()


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.ws = Path(self.tmp.name) / 'ws'; self.ws.mkdir(); (self.ws / 'readme.txt').write_text('v1')
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.started = []

    def create(self, sid='s', wait=WAIT, workspace=None):
        policy = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}})
        if wait:
            policy['wait'] = dict(wait)
        repo.create_session(self.conn, sid, str(workspace or self.ws), 'auto', policy=policy)
        for i, agent in enumerate(('codex', 'claude', 'codex')):
            repo.add_session_step(self.conn, f'{sid}_{i}', sid, i, agent, f'task {i}')
        return sid

    async def execute(self, factory, sid='s'):
        await SessionRunner(self.db, adapter_factory=factory).run_session(sid)
        return self.status(sid)

    def status(self, sid='s'):
        return repo.get_session(self.conn, sid)['status']

    def plans(self, sid='s'):
        return [dict(r) for r in self.conn.execute('SELECT * FROM retry_plans WHERE session_id=? ORDER BY created_at', (sid,))]

    def events(self, kind, sid='s'):
        return [json.loads(r[0]) for r in self.conn.execute(
            'SELECT payload FROM session_events WHERE session_id=? AND kind=? ORDER BY seq', (sid, kind))]

    def scheduler(self, ahead):
        return RetryScheduler(self.db, self.started.append, excluded=snapshot_exclusions(self.db),
                              clock=lambda: utcnow() + ahead)


class Planning(Base):
    async def test_opt_in_quota_creates_one_plan_from_the_folder_as_left(self):
        self.create()
        before = utcnow()
        self.assertEqual(await self.execute(Script(quota())), 'WAITING_CAPACITY')
        [plan] = self.plans()
        self.assertEqual((plan['status'], plan['attempts_so_far'], plan['max_attempts'], plan['reason']),
                         ('PENDING', 0, 3, 'QUOTA_EXCEEDED'))
        self.assertEqual(self.conn.execute('SELECT kind FROM workspace_snapshots WHERE id=?',
                                           (plan['baseline_snapshot_id'],)).fetchone()[0], 'wait_start')
        self.assertTrue(iso(before + timedelta(hours=4)) <= plan['deadline'] <= iso(utcnow() + timedelta(hours=4)))
        self.assertTrue(iso(before + timedelta(minutes=15)) <= plan['next_retry_at'] <= iso(utcnow() + timedelta(minutes=15)))
        self.assertEqual(len(self.events('wait_planned')), 1)

    async def test_default_is_unchanged_and_other_failures_never_plan(self):
        for sid, result, expected in (('off', quota(), 'WAITING_CAPACITY'), ('auth', err('AUTH'), 'WAITING_AUTH'),
                                      ('perm', err('PERMISSION'), 'WAITING_APPROVAL'), ('other', err('BROKEN'), 'FAILED')):
            with self.subTest(sid=sid):
                (self.ws / sid).mkdir()
                self.create(sid, wait=None if sid == 'off' else WAIT, workspace=self.ws / sid)
                self.assertEqual(await self.execute(Script(result), sid), expected)
                self.assertEqual(self.plans(sid), [])
        self.assertEqual(self.scheduler(timedelta(days=2)).tick(), [])

    async def test_quota_without_confirmed_cleanup_is_recovery_not_a_plan(self):
        self.create()

        class Unstopped:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                yield {'type': 'process_started', 'ownership': {'pid': 7, 'pgid': 7, 'start_time': 1.0, 'boot_time': 1.0}}
                yield {'type': 'failed', 'result': quota()}
        self.assertEqual(await self.execute(lambda p: Unstopped()), 'RECOVERY_REQUIRED')
        self.assertEqual(self.plans(), [])


class Firing(Base):
    async def test_not_due_waits_and_due_fires_through_the_human_retry_path(self):
        self.create(); await self.execute(Script(quota()))
        self.assertEqual(self.scheduler(timedelta(minutes=1)).tick(), [])
        self.assertEqual(self.scheduler(timedelta(minutes=16)).tick(), [('s', 'queued')])
        self.assertEqual((self.started, self.status()), (['s'], 'QUEUED'))
        [plan] = self.plans()
        self.assertEqual((plan['status'], plan['attempts_so_far']), ('FIRED', 1))
        self.assertEqual(repo.get_session_steps(self.conn, 's')[0]['retry_generation'], 1)
        self.assertEqual([e['attempt'] for e in self.events('wait_fired')], [1])
        self.assertEqual(await self.execute(Script()), 'SUCCEEDED')

    async def test_retries_accumulate_on_one_row_then_fail(self):
        self.create()
        self.assertEqual(await self.execute(Script(quota())), 'WAITING_CAPACITY')
        for attempt in (1, 2, 3):
            self.assertEqual(self.scheduler(timedelta(hours=2)).tick(), [('s', 'queued')])
            self.assertEqual(self.plans()[0]['attempts_so_far'], attempt)   # counted once, at the fire
            await self.execute(Script(quota()))
        self.assertEqual(self.status(), 'FAILED')
        [plan] = self.plans()
        self.assertEqual((plan['status'], plan['closed_reason'], plan['attempts_so_far']),
                         ('EXPIRED', 'max_retries_exceeded', 3))
        self.assertEqual([e['renewed'] for e in self.events('wait_planned')], [False, True, True])
        self.assertEqual(self.scheduler(timedelta(days=2)).tick(), [])

    async def test_deadline_expires_the_plan_and_fails_the_session(self):
        self.create(wait={'deadline_hours': 1, 'max_attempts': 5}); await self.execute(Script(quota()))
        self.assertEqual(self.scheduler(timedelta(hours=2)).tick(), [('s', 'expired')])
        self.assertEqual(self.status(), 'FAILED')
        self.assertEqual(self.plans()[0]['closed_reason'], 'retry_deadline_exceeded')
        self.assertEqual(self.started, [])

    async def test_agent_changes_during_a_retry_are_not_outside_changes(self):
        """Negative test for the refreshed base: the retried attempt edits the folder, hits quota again."""
        self.create(); await self.execute(Script(quota()))
        first_base = self.plans()[0]['baseline_snapshot_id']
        self.assertEqual(self.scheduler(timedelta(hours=1)).tick(), [('s', 'queued')])
        self.assertEqual(await self.execute(writing('agent.txt', quota())), 'WAITING_CAPACITY')
        [plan] = self.plans()
        self.assertEqual(plan['status'], 'PENDING'); self.assertNotEqual(plan['baseline_snapshot_id'], first_base)
        self.assertEqual(self.scheduler(timedelta(hours=2)).tick(), [('s', 'queued')])   # not WAITING_HUMAN
        self.assertEqual(self.events('wait_workspace_changed'), [])


class Guards(Base):
    async def test_outside_change_stops_at_waiting_human(self):
        self.create(); await self.execute(Script(quota()))
        (self.ws / 'readme.txt').write_text('edited by someone else')
        self.assertEqual(self.scheduler(timedelta(hours=1)).tick(), [('s', 'waiting_human')])
        self.assertEqual((self.status(), self.started), ('WAITING_HUMAN', []))
        [plan] = self.plans()
        self.assertEqual((plan['status'], plan['closed_reason']), ('CANCELLED', 'workspace_changed'))
        self.assertEqual(self.events('wait_workspace_changed')[0]['paths'], ['readme.txt'])
        repo.retry_session(self.conn, 's')                                     # the human decides to continue
        self.assertEqual(self.status(), 'QUEUED')

    async def test_human_retry_supersedes_a_pending_plan(self):
        self.create(); await self.execute(Script(quota()))
        repo.retry_session(self.conn, 's')
        self.assertEqual((self.plans()[0]['status'], self.plans()[0]['closed_reason']), ('CANCELLED', 'human_retry'))
        self.assertEqual(self.scheduler(timedelta(hours=5)).tick(), [])
        self.assertEqual(self.status(), 'QUEUED')

    async def test_expiry_never_overwrites_a_session_that_left_waiting(self):
        self.create(); await self.execute(Script(quota()))
        self.conn.execute("UPDATE sessions SET status='QUEUED' WHERE id='s'")   # moved on by another path
        self.assertEqual(self.scheduler(timedelta(hours=5)).tick(), [])       # past the deadline
        self.assertEqual(self.status(), 'QUEUED')
        self.assertEqual(self.plans()[0]['closed_reason'], 'session_not_waiting')

    async def test_busy_folder_reschedules_without_counting(self):
        self.create(); await self.execute(Script(quota()))
        repo.create_session(self.conn, 'other', str(self.ws), 'auto')          # QUEUED on the same folder
        self.assertEqual(self.scheduler(timedelta(minutes=30)).tick(), [('s', 'rescheduled')])
        [plan] = self.plans()
        self.assertEqual((plan['status'], plan['attempts_so_far']), ('PENDING', 0))
        self.assertTrue(plan['next_retry_at'] > iso(utcnow() + timedelta(minutes=40)))
        self.assertEqual(self.events('wait_rescheduled')[0]['reason'], 'workspace_busy')

    async def test_unresolved_cleanup_cancels_the_plan_and_keeps_the_state(self):
        self.create(); await self.execute(Script(quota()))
        self.conn.execute("UPDATE session_attempts SET ended_at=NULL")
        self.assertEqual(self.scheduler(timedelta(hours=1)).tick(), [('s', 'cancelled')])
        self.assertEqual(self.status(), 'WAITING_CAPACITY')
        self.assertEqual(self.plans()[0]['closed_reason'], 'recovery_required')

    async def test_two_schedulers_fire_a_plan_once(self):
        self.create(); await self.execute(Script(quota()))
        pid, now = self.plans()[0]['id'], utcnow() + timedelta(hours=1)

        def claim(_):
            conn = store.connect(self.db)
            try:
                return wait_plans.claim(conn, pid, now)
            finally:
                conn.close()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(claim, range(2)), key=str), [None, 'fired'])

    async def test_restart_reconciles_plans_and_never_starts_a_session(self):
        self.create(); await self.execute(Script(quota()))
        self.create('q', workspace=self.tmp.name); await self.execute(Script(quota()), 'q')
        now = utcnow() + timedelta(hours=1)
        self.assertEqual(wait_plans.claim(self.conn, self.plans()[0]['id'], now), 'fired')          # crash before fire
        self.assertEqual(wait_plans.claim(self.conn, self.plans('q')[0]['id'], now), 'fired')
        self.assertEqual(wait_plans.fire(self.conn, self.plans('q')[0], snapshot_exclusions(self.db), now), 'queued')
        self.conn.execute("UPDATE sessions SET status='RECOVERY_REQUIRED' WHERE status IN ('QUEUED','RUNNING')")   # startup rule
        wait_plans.reconcile(self.conn)
        self.assertEqual((self.plans()[0]['status'], self.status()), ('PENDING', 'WAITING_CAPACITY'))
        self.assertEqual((self.plans('q')[0]['status'], self.plans('q')[0]['closed_reason'], self.status('q')),
                         ('CANCELLED', 'recovery_required', 'RECOVERY_REQUIRED'))

    async def test_fire_failure_reschedules_plan_continues_batch_and_expires_at_deadline(self):
        ws2 = Path(self.tmp.name) / 'ws2'; ws2.mkdir()
        self.create('s'); await self.execute(Script(quota()), 's')
        self.create('q', workspace=ws2); await self.execute(Script(quota()), 'q')
        initial_retry_at = self.plans('s')[0]['next_retry_at']
        real_fire, changed = wait_plans.fire, []

        def flaky_fire(conn, plan, excluded, now):
            if plan['session_id'] == 's':
                raise RuntimeError('corrupt snapshot')
            return real_fire(conn, plan, excluded, now)

        sched = RetryScheduler(self.db, self.started.append, excluded=snapshot_exclusions(self.db),
                               clock=lambda: utcnow() + timedelta(minutes=30), on_change=lambda: changed.append(True))
        with patch('app.wait_plans.fire', side_effect=flaky_fire), self.assertLogs('app.wait_plans', level='ERROR'):
            self.assertEqual(sched.tick(), [('q', 'queued')])
        self.assertEqual((self.started, self.status('q'), changed), (['q'], 'QUEUED', [True]))
        [plan_s] = self.plans('s')
        self.assertEqual((plan_s['status'], plan_s['attempts_so_far'], self.status('s')),
                         ('PENDING', 0, 'WAITING_CAPACITY'))
        self.assertGreater(plan_s['next_retry_at'], initial_retry_at)
        self.assertGreater(plan_s['next_retry_at'], iso(utcnow() + timedelta(minutes=34)))
        self.assertEqual(self.events('wait_rescheduled', 's'),
                         [{'plan_id': plan_s['id'], 'reason': 'fire_failed', 'next_retry_at': plan_s['next_retry_at']}])
        self.assertEqual(self.scheduler(timedelta(hours=5)).tick(), [('s', 'expired')])
        [plan_s] = self.plans('s')
        self.assertEqual((plan_s['status'], plan_s['closed_reason'], self.status('s')),
                         ('EXPIRED', 'retry_deadline_exceeded', 'FAILED'))


class WorkQueue(Base):
    async def test_waiting_human_holds_the_work_queue(self):
        work, _ = ws.create_work(self.conn, str(uuid4()), 'w', str(self.ws))
        policy = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}})
        ws.add_message(self.conn, work['id'], str(uuid4()), 'goal', digest(policy), dict(policy, wait=WAIT), digest(policy), None, WAIT)
        ws.add_message(self.conn, work['id'], str(uuid4()), 'next', digest(policy), policy, digest(policy))
        sid, reason = ws.dispatch_next(self.conn, work['id'])
        self.assertIsNone(reason)
        self.assertEqual(await self.execute(Script(quota()), sid), 'WAITING_CAPACITY')
        (self.ws / 'readme.txt').write_text('changed')
        self.assertEqual(self.scheduler(timedelta(hours=1)).tick(), [(sid, 'waiting_human')])
        self.assertEqual(ws.blocker(self.conn, work['id']), 'run_waiting')
        self.assertEqual(ws.dispatch_next(self.conn, work['id']), (None, 'run_waiting'))


class Migration(unittest.TestCase):
    def test_1_6_database_gains_retry_plans_losslessly(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        conn = store.connect(Path(tmp.name) / 'state.db'); self.addCleanup(conn.close); store.migrate(conn)
        repo.create_session(conn, 'old', tmp.name, 'auto'); repo.update_session_status(conn, 'old', 'WAITING_CAPACITY')
        conn.execute('DROP TABLE retry_plans'); conn.execute("UPDATE schema_meta SET value='1.6'")
        before = repo.get_session(conn, 'old')
        store.migrate(conn); store.migrate(conn)                                # idempotent
        self.assertEqual(conn.execute("SELECT value FROM schema_meta").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(repo.get_session(conn, 'old'), before)
        self.assertEqual(conn.execute('SELECT count(*) FROM retry_plans').fetchone()[0], 0)
        self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.system').mkdir(); (self.root / 'ws').mkdir()
        (self.root / '.system/providers.yaml').write_text('roles:\n  author: [codex]\n  reviewer: [claude]\n')
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)}); env.start(); self.addCleanup(env.stop)
        installed = patch('app.session_api.installed_providers', return_value={'codex': True, 'claude': True})
        installed.start(); self.addCleanup(installed.stop)
        self.app = create_app(); self.c = TestClient(self.app); self.c.__enter__(); self.addCleanup(self.c.__exit__, None, None, None)
        self.app.state.runner.adapter_factory = Script(quota())
        self.wid = str(uuid4())
        self.assertEqual(self.c.post('/api/works', json={'client_work_id': self.wid, 'title': 't',
                                                         'workspace_path': str(self.root / 'ws')}).status_code, 201)
        self.digest = self.c.get('/api/sessions/providers').json()['digest']

    def send(self, cmid=None, **wait):
        return self.c.post(f'/api/works/{self.wid}/messages',
                           json=dict({'client_message_id': cmid or str(uuid4()), 'text': 'goal', 'policy_digest': self.digest}, **wait))

    def wait_status(self, sid, status):
        for _ in range(200):
            if self.c.get(f'/api/sessions/{sid}').json()['session']['status'] == status:
                return self.c.get(f'/api/sessions/{sid}').json()
            time.sleep(0.02)
        self.fail(f'{sid} never reached {status}')

    def test_message_freezes_wait_and_hashes_it(self):
        cmid = str(uuid4())
        r = self.send(cmid, wait_on_quota=True, wait_deadline_hours=8, wait_max_attempts=2)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()['wait'], {'deadline_hours': 8, 'max_attempts': 2})
        self.assertEqual(self.send(cmid, wait_on_quota=True, wait_deadline_hours=8, wait_max_attempts=2).status_code, 200)
        clash = self.send(cmid, wait_on_quota=True, wait_deadline_hours=9, wait_max_attempts=2)
        self.assertEqual((clash.status_code, clash.json()['code']), (409, 'IDEMPOTENCY_CONFLICT'))
        for bad in ({'wait_deadline_hours': 25}, {'wait_max_attempts': 0}, {'wait_on_quota': 'true'}, {'wait_max_attempts': '3'}):
            with self.subTest(bad=bad):
                self.assertEqual(self.send(**bad).status_code, 422)
        detail = self.wait_status(self._sid(), 'WAITING_CAPACITY')
        self.assertEqual(detail['policy']['wait'], {'deadline_hours': 8, 'max_attempts': 2})
        self.assertEqual(detail['wait_plan']['max_attempts'], 2)
        self.assertIsNone(detail['wait_plan']['resets_at'])

    def _sid(self):
        for _ in range(200):
            items = self.c.get(f'/api/works/{self.wid}/messages').json()['items']
            if items and items[0]['session']:
                return items[0]['session']['id']
            time.sleep(0.02)
        self.fail('message was never dispatched')

    def test_cancel_wait_and_waiting_human(self):
        self.assertEqual(self.send(wait_on_quota=True).status_code, 201)
        sid = self._sid()
        self.wait_status(sid, 'WAITING_CAPACITY')
        self.assertEqual(self.c.delete(f'/api/sessions/{sid}/wait', headers={'Origin': 'http://evil.example'}).status_code, 403)
        self.assertEqual(self.c.delete(f'/api/sessions/{sid}/wait').json(), {'status': 'CANCELLED'})
        self.assertEqual(self.c.get(f'/api/sessions/{sid}').json()['wait_plan']['status'], 'CANCELLED')
        again = self.c.delete(f'/api/sessions/{sid}/wait')
        self.assertEqual((again.status_code, again.json()['code']), (409, 'no_wait_plan'))
        conn = store.connect(self.root / '.system/state.db'); self.addCleanup(conn.close)
        conn.execute("UPDATE sessions SET status='WAITING_HUMAN' WHERE id=?", (sid,))
        self.assertEqual(self.c.post(f'/api/sessions/{sid}/cancel').json(), {'status': 'CANCELLED'})

    def test_fired_plan_cannot_be_cancelled(self):
        self.assertEqual(self.send(wait_on_quota=True).status_code, 201)
        sid = self._sid(); self.wait_status(sid, 'WAITING_CAPACITY')
        conn = store.connect(self.root / '.system/state.db'); self.addCleanup(conn.close)
        conn.execute("UPDATE retry_plans SET status='FIRED'")
        r = self.c.delete(f'/api/sessions/{sid}/wait')
        self.assertEqual((r.status_code, r.json()['code']), (409, 'wait_already_fired'))


if __name__ == '__main__':
    unittest.main()
