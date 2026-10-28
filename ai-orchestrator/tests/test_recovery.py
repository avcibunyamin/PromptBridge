"""F2-03c recovery: reaper safety, endpoints, retry. Simulated + one real subprocess."""
import asyncio
import contextlib
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
from starlette.testclient import TestClient
from app import store, runner_store as repo
from app.api import create_app
from app.adapters import process as P
from app.adapters.process import reap_owned, spawn_owned, release_gate, group_exists, same_boot, same_identity


class Sim:
    """A controllable process world for reap_owned (no real signals)."""
    def __init__(self, boot, leader_identity, members, term_kills=True):
        self.boot = boot
        self.leader_identity = leader_identity   # identity(owner.pid) result, or None
        self.members = [dict(m) for m in members]
        self.term_kills = term_kills             # SIGTERM alone clears the group?
        self.calls = []

    def boot_time(self):
        return self.boot

    def identity(self, pid):
        if self.leader_identity and self.leader_identity['pid'] == pid:
            return self.leader_identity
        for m in self.members:
            if m['pid'] == pid:
                return m
        return None

    def group_members(self, pgid):
        return [dict(m) for m in self.members if m['pgid'] == pgid]

    def group_exists(self, pgid):
        return any(m['pgid'] == pgid for m in self.members)

    def killpg(self, pgid, sig):
        self.calls.append(('killpg', pgid, sig))
        if sig == signal.SIGKILL or self.term_kills:
            self.members = [m for m in self.members if m['pgid'] != pgid]

    def kill(self, pid, sig):
        self.calls.append(('kill', pid, sig))
        self.members = [m for m in self.members if m['pid'] != pid]


@contextlib.contextmanager
def world(sim):
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(P.psutil, 'boot_time', sim.boot_time))
        stack.enter_context(patch.object(P, 'identity', sim.identity))
        stack.enter_context(patch.object(P, 'group_members', sim.group_members))
        stack.enter_context(patch.object(P, 'group_exists', sim.group_exists))
        stack.enter_context(patch.object(P.os, 'killpg', sim.killpg))
        stack.enter_context(patch.object(P.os, 'kill', sim.kill))
        yield sim


def ident(pid, start=100.0, boot=5.0):
    return {'pid': pid, 'pgid': pid, 'start_time': start, 'boot_time': boot}


class ReaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_reboot_is_dead_without_signalling(self):  # DoD 1 — safe terminal
        owner = ident(700, boot=5.0)
        sim = Sim(boot=999.0, leader_identity=ident(700), members=[ident(700)])
        with world(sim):
            proof = await reap_owned(owner, [])
        self.assertEqual((proof['result'], proof['ok']), ('dead', True))
        self.assertEqual(proof['reason'], 'boot_time_changed')
        self.assertEqual(sim.calls, [])                       # no signal at all

    async def test_group_already_empty_is_dead(self):
        owner = ident(701)
        with world(Sim(boot=5.0, leader_identity=None, members=[])) as sim:
            proof = await reap_owned(owner, [])
        self.assertEqual((proof['result'], proof['ok']), ('dead', True))
        self.assertEqual(sim.calls, [])

    async def test_leader_alive_killpg_then_verified(self):  # DoD 3
        owner = ident(702)
        sim = Sim(boot=5.0, leader_identity=ident(702), members=[ident(702)])
        with world(sim):
            proof = await reap_owned(owner, [])
        self.assertEqual((proof['result'], proof['ok']), ('reaped', True))
        self.assertIn('SIGTERM', proof['signals'])
        self.assertEqual(sim.calls[0], ('killpg', 702, signal.SIGTERM))

    async def test_leader_alive_survives_sigterm_then_sigkill(self):
        owner = ident(703)
        sim = Sim(boot=5.0, leader_identity=ident(703), members=[ident(703)], term_kills=False)
        with world(sim):
            proof = await reap_owned(owner, [], timeout=0.2)
        self.assertEqual(proof['result'], 'reaped')
        self.assertEqual(proof['signals'], ['SIGTERM', 'SIGKILL'])

    async def test_ambiguous_member_keeps_lock(self):  # DoD 4 — lock retained, distinct output
        owner = ident(704)                              # leader dead...
        stranger = ident(9999)                          # ...but an unknown pid shares the pgid
        stranger['pgid'] = 704
        sim = Sim(boot=5.0, leader_identity=None, members=[stranger])
        with world(sim):
            proof = await reap_owned(owner, [])         # no trusted snapshot
        self.assertEqual((proof['result'], proof['ok']), ('blocked', False))
        self.assertEqual(proof['blocking'], [9999])
        self.assertEqual(sim.calls, [])                 # never signals unattributable

    async def test_attributable_orphans_reaped_by_pid(self):
        owner = ident(705)                              # leader dead
        child = {'pid': 800, 'pgid': 705, 'start_time': 50.0, 'boot_time': 5.0}
        sim = Sim(boot=5.0, leader_identity=None, members=[child])
        with world(sim):
            proof = await reap_owned(owner, [child])    # child in trusted snapshot
        self.assertEqual((proof['result'], proof['ok']), ('reaped', True))
        self.assertIn(('kill', 800, signal.SIGKILL), sim.calls)
        self.assertNotIn('killpg', [c[0] for c in sim.calls])  # per-pid, not group-wide

    async def test_pid_reuse_is_not_signalled(self):  # DoD 5
        owner = ident(706, start=10.0)                  # recorded start 10
        reused = ident(706, start=888.0)                # same pid, new start -> reused
        sim = Sim(boot=5.0, leader_identity=reused, members=[reused])
        with world(sim):
            proof = await reap_owned(owner, [])
        self.assertEqual((proof['result'], proof['ok']), ('blocked', False))
        self.assertEqual(proof['blocking'], [706])
        self.assertEqual(sim.calls, [])                 # reused pid never killed

    async def test_real_orphan_group_reaped(self):  # DoD 10 — real subprocess, no injection
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        proc, owner, gate = await spawn_owned([sys.executable, '-c', 'import time;time.sleep(30)'], tmp.name)
        release_gate(gate)
        await asyncio.sleep(0.2)
        self.assertTrue(group_exists(owner['pgid']))
        proof = await reap_owned({k: owner[k] for k in ('pid', 'pgid', 'start_time', 'boot_time')})
        self.assertEqual((proof['result'], proof['ok']), ('reaped', True))
        self.assertFalse(group_exists(owner['pgid']))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 2)

    def test_same_identity_boot_time_tolerance(self):
        base = ident(707, start=100.0, boot=1000.0)
        self.assertTrue(same_identity(base, ident(707, start=100.0, boot=1001.0)))
        self.assertFalse(same_identity(base, ident(707, start=100.0, boot=4600.0)))
        self.assertFalse(same_boot(None, 1000.0))
        self.assertFalse(same_boot(1000.0, None))

    async def test_real_orphan_reaped_with_1s_boot_time_drift(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        proc, owner, gate = await spawn_owned([sys.executable, '-c', 'import time;time.sleep(30)'], tmp.name)
        try:
            release_gate(gate)
            await asyncio.sleep(0.2)
            self.assertTrue(group_exists(owner['pgid']))
            shifted = {k: owner[k] for k in ('pid', 'pgid', 'start_time', 'boot_time')}
            shifted['boot_time'] -= 1.0
            proof = await reap_owned(shifted)
            self.assertNotEqual(proof.get('reason'), 'boot_time_changed')
            self.assertEqual((proof['result'], proof['ok']), ('reaped', True))
            self.assertFalse(group_exists(owner['pgid']))
        finally:
            with contextlib.suppress(Exception):
                os.killpg(owner['pgid'], signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)

    async def test_boot_time_3600s_drift_is_boot_time_changed(self):
        owner = ident(708, boot=1000.0)
        sim = Sim(boot=4600.0, leader_identity=ident(708, boot=4600.0), members=[ident(708, boot=4600.0)])
        with world(sim):
            proof = await reap_owned(owner, [])
        self.assertEqual((proof['result'], proof['reason'], proof['ok']), ('dead', 'boot_time_changed', True))
        self.assertEqual(sim.calls, [])


class RecoveryEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / '.system').mkdir()
        self.db = self.root / '.system/state.db'
        env = patch.dict(os.environ, {'AI_ORCHESTRATOR_ROOT': str(self.root)})
        env.start(); self.addCleanup(env.stop)
        self.app = create_app()

    def stuck(self, status='RECOVERY_REQUIRED', with_owner=True, stopped=False, crashed=False):
        conn = store.connect(self.db); store.migrate(conn)
        repo.create_session(conn, 's', str(self.root), 'auto')
        repo.add_session_step(conn, 's0', 's', 0, 'codex', 'goal')
        self.assertTrue(repo.claim_session(conn, 's'))       # acquires the workspace lock
        repo.update_step_status(conn, 's0', 'RUNNING')
        if with_owner:
            repo.begin_attempt(conn, 'a0', 's0', 1, 'codex', 'WRITE')
            owner = {'pid': 4242, 'pgid': 4242, 'start_time': 1.0, 'boot_time': 2.0}
            repo.record_owner(conn, 'a0', owner, stopped=stopped)
        if not crashed:  # crashed: server died mid-step -> step RUNNING, attempt still open
            repo.update_step_status(conn, 's0', 'FAILED', is_error=1, terminal_reason='crash')
        repo.update_session_status(conn, 's', status)
        conn.close()

    def locked(self):
        conn = store.connect(self.db)
        try:
            return conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0]
        finally:
            conn.close()

    def test_recover_requires_confirm(self):  # K4
        self.stuck()
        with TestClient(self.app) as client:
            self.assertEqual(client.post('/api/sessions/s/recover', json={}).status_code, 422)
            self.assertEqual(client.post('/api/sessions/s/recover', json={'confirm': False}).status_code, 422)
        self.assertEqual(self.locked(), 1)                   # nothing released

    def test_recover_non_recovery_state_is_409(self):
        self.stuck(status='WAITING_CONTEXT')
        with TestClient(self.app) as client:
            r = client.post('/api/sessions/s/recover', json={'confirm': True})
        self.assertEqual(r.status_code, 409)

    def test_recover_success_releases_lock_and_fails(self):
        self.stuck()
        proof = {'result': 'reaped', 'ok': True, 'signals': ['SIGTERM'], 'pid': 4242, 'pgid': 4242}
        with TestClient(self.app) as client:
            async def fake_reap(owner, trusted, **kw):
                return dict(proof)
            with patch('app.adapters.process.reap_owned', fake_reap):
                r = client.post('/api/sessions/s/recover', json={'confirm': True})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['terminal_reason'], 'recovered_by_human')
            data = client.get('/api/sessions/s').json()
            self.assertEqual(data['session']['status'], 'FAILED')
            self.assertEqual(data['steps'][0]['terminal_reason'], 'recovered_by_human')
            self.assertIn('recovery', client.get('/api/sessions/s/events').text)
        self.assertEqual(self.locked(), 0)                   # lock released only after proof

    def test_recover_after_real_crash_closes_running_step_and_attempt(self):
        # Realistic crash state (caught by the F2-05 live drill): step RUNNING, attempt open.
        self.stuck(crashed=True)
        with TestClient(self.app) as client:
            async def fake_reap(owner, trusted, **kw):
                return {'result': 'reaped', 'ok': True, 'signals': ['SIGTERM'], 'pid': 4242, 'pgid': 4242}
            with patch('app.adapters.process.reap_owned', fake_reap):
                self.assertEqual(client.post('/api/sessions/s/recover', json={'confirm': True}).status_code, 200)
            data = client.get('/api/sessions/s').json()
        self.assertEqual(data['session']['status'], 'FAILED')
        self.assertEqual((data['steps'][0]['status'], data['steps'][0]['terminal_reason']),
                         ('FAILED', 'recovered_by_human'))
        attempt = data['attempts'][0]
        self.assertEqual((attempt['status'], attempt['terminal_reason']), ('FAILED', 'recovered_by_human'))
        self.assertIsNotNone(attempt['ended_at'])
        self.assertEqual(self.locked(), 0)
        with TestClient(self.app) as client:   # K1: list exposes the real-data flag
            self.assertEqual(client.get('/api/sessions').json()[0]['recovered_by_human'], 1)

    def test_recover_blocked_keeps_lock(self):  # security order: no release without proof
        self.stuck()
        blocked = {'result': 'blocked', 'ok': False, 'blocking': [9999], 'signals': [], 'pid': 4242, 'pgid': 4242}
        with TestClient(self.app) as client:
            async def fake_reap(owner, trusted, **kw):
                return dict(blocked)
            with patch('app.adapters.process.reap_owned', fake_reap):
                r = client.post('/api/sessions/s/recover', json={'confirm': True})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['status'], 'RECOVERY_REQUIRED')
            self.assertEqual(r.json()['blocking'], [9999])
            self.assertEqual(client.get('/api/sessions/s').json()['session']['status'], 'RECOVERY_REQUIRED')
            self.assertEqual(client.get('/api/sessions').json()[0]['recovered_by_human'], 0)
        self.assertEqual(self.locked(), 1)                   # lock retained

    def test_recovery_detail_lists_owner_and_lock(self):
        self.stuck()
        with TestClient(self.app) as client:
            data = client.get('/api/sessions/s/recovery').json()
        self.assertTrue(data['lock_retained'])
        self.assertEqual(len(data['owners']), 1)
        self.assertEqual(data['owners'][0]['pid'], 4242)

    def test_retry_requeues_waiting_context(self):  # K2 — re-run the failed step
        self.stuck(status='WAITING_CONTEXT', with_owner=False)
        conn = store.connect(self.db)
        conn.execute("DELETE FROM workspace_locks"); conn.commit(); conn.close()  # WAITING_* freed it
        with TestClient(self.app) as client:
            async def noop(sid):
                return None
            with patch.object(self.app.state.runner, 'run_session', noop):
                r = client.post('/api/sessions/s/retry')
                self.assertEqual(r.status_code, 202)
                data = client.get('/api/sessions/s').json()
        self.assertEqual(data['session']['status'], 'QUEUED')
        self.assertEqual(data['steps'][0]['status'], 'PENDING')

    def test_retry_refused_on_recovery_required(self):
        self.stuck(status='RECOVERY_REQUIRED')
        with TestClient(self.app) as client:
            self.assertEqual(client.post('/api/sessions/s/retry').status_code, 409)


if __name__ == '__main__':
    unittest.main()
