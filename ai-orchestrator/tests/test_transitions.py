from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from app import store as s
from app.transitions import (State, Event, TRANSITIONS, transition, EnvelopeNotFound,
                             InvalidEvent, InvalidTransition, VersionMismatch,
                             DeliveredHashMismatch)


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.conn = s.connect(self.path)
        self.addCleanup(self.conn.close)
        s.migrate(self.conn)

    def capture(self, eid='a'):
        return s.insert_captured(self.conn, {'id': eid, 'source_agent': 'chatgpt',
                                            'target_agent': 'claude'}, b'original')[1]

    def go(self, event, expected, payload=None, eid='a', actor='tester'):
        return transition(self.conn, eid, event, expected_state=expected, actor=actor,
                          payload={} if payload is None else payload)

    def setup_state(self, state, eid='a'):
        vid = self.capture(eid)
        if state != State.CAPTURED:
            self.go('enqueue', 'CAPTURED', eid=eid)
        if state in (State.APPROVED, State.DELIVERING, State.DELIVERED, State.FAILED):
            self.go('human_approve', 'PENDING_APPROVAL', {'version_id': vid}, eid=eid)
        if state in (State.DELIVERING, State.DELIVERED, State.FAILED):
            self.go('route', 'APPROVED', {'version_id': vid, 'dest_path': f'{eid}.md'}, eid=eid)
        if state == State.DELIVERED:
            self.go('deliver_ok', 'DELIVERING', {'actual_sha256': s.get_version(self.conn, vid)['sha256']}, eid=eid)
        if state == State.FAILED:
            self.go('deliver_err', 'DELIVERING', {'reason': 'disk'}, eid=eid)
        if state == State.REJECTED:
            self.go('human_reject', 'PENDING_APPROVAL', eid=eid)
        if state == State.EXPIRED:
            self.go('timeout', 'PENDING_APPROVAL', {'reason': 'caller policy'}, eid=eid)
        if state in (State.DETECTED, State.CONSUMED, State.ACK_TIMEOUT):
            # Only fixtures create future/non-service states.
            s.set_state(self.conn, eid, 'PENDING_APPROVAL', state, 'fixture', 'test')
        return vid

    def payload(self, event, vid, eid):
        if event == Event.HUMAN_APPROVE:
            return {'version_id': vid}
        if event in (Event.ROUTE, Event.RETRY):
            return {'version_id': vid, 'dest_path': f'{eid}.md'}
        if event == Event.DELIVER_OK:
            return {'actual_sha256': s.get_version(self.conn, vid)['sha256']}
        if event in (Event.TIMEOUT, Event.DELIVER_ERR, Event.ESCALATE):
            return {'reason': 'test'}
        return {}

    def snapshot(self, eid='a'):
        return s.get_envelope(self.conn, eid), s.get_history(self.conn, eid)

    def test_entire_matrix_and_exactly_one_audit_per_transition(self):
        for state in State:
            for event in Event:
                eid = f'{state}_{event}'
                with self.subTest(state=state, event=event):
                    vid = self.setup_state(state, eid)
                    before = self.snapshot(eid)
                    payload = self.payload(event, vid, eid)
                    target = TRANSITIONS.get((state, event))
                    if target is None:
                        with self.assertRaises(InvalidTransition):
                            self.go(event, state, payload, eid)
                        self.assertEqual(self.snapshot(eid), before)
                    else:
                        result = self.go(event, state, payload, eid)
                        self.assertEqual(result['state'], target)
                        history = s.get_history(self.conn, eid)
                        self.assertEqual(len(history), len(before[1]) + 1)
                        self.assertEqual(history[-1]['actor'], 'tester')
                        self.assertEqual(history[-1]['event'], event)
                        self.assertEqual(history[-1]['from_state'], state)
                        self.assertEqual(history[-1]['to_state'], target)

    def test_edited_version_happy_path_and_wrong_hash(self):
        original = self.setup_state(State.PENDING_APPROVAL)
        edited = s.add_edited_version(self.conn, 'a', b'changed')
        self.go('human_approve', 'PENDING_APPROVAL', {'version_id': edited})
        self.go('route', 'APPROVED', {'version_id': edited, 'dest_path': 'a.md'})
        before = self.snapshot()
        with self.assertRaises(DeliveredHashMismatch):
            self.go('deliver_ok', 'DELIVERING', {'actual_sha256': s.get_version(self.conn, original)['sha256']})
        self.assertEqual(self.snapshot(), before)
        self.go('deliver_ok', 'DELIVERING', {'actual_sha256': s.get_version(self.conn, edited)['sha256']})
        self.assertEqual(s.get_content(self.conn, original), b'original')

    def test_ownership_and_wrong_delivery_version(self):
        own = self.setup_state(State.PENDING_APPROVAL)
        other = self.capture('other')
        before = self.snapshot()
        with self.assertRaises(VersionMismatch):
            self.go('human_approve', 'PENDING_APPROVAL', {'version_id': other})
        self.assertEqual(self.snapshot(), before)
        self.go('human_approve', 'PENDING_APPROVAL', {'version_id': own})
        before = self.snapshot()
        with self.assertRaises(VersionMismatch):
            self.go('route', 'APPROVED', {'version_id': other, 'dest_path': 'a.md'})
        self.assertEqual(self.snapshot(), before)

    def test_retry_and_reapproval(self):
        vid = self.setup_state(State.FAILED)
        before = self.snapshot()
        for payload, error in (({'version_id': vid, 'dest_path': 'new.md'}, InvalidEvent),
                               ({'version_id': 'other', 'dest_path': 'a.md'}, VersionMismatch)):
            with self.assertRaises(error):
                self.go('retry', 'FAILED', payload)
            self.assertEqual(self.snapshot(), before)
        self.go('retry', 'FAILED', {'version_id': vid, 'dest_path': 'a.md'})
        self.go('deliver_err', 'DELIVERING', {'reason': 'again'})
        row = self.go('escalate', 'FAILED', {'reason': 'review again'})
        self.assertTrue(all(row[k] is None for k in s.RESET_FIELDS))
        before = self.snapshot()
        with self.assertRaises(InvalidTransition):
            self.go('route', 'PENDING_APPROVAL', {'version_id': vid, 'dest_path': 'a.md'})
        self.assertEqual(self.snapshot(), before)
        edited = s.add_edited_version(self.conn, 'a', b'revised')
        self.go('human_approve', 'PENDING_APPROVAL', {'version_id': edited})
        row = self.go('route', 'APPROVED', {'version_id': edited, 'dest_path': 'a.md'})
        self.assertEqual(row['delivered_version_id'], edited)

    def test_bad_inputs_and_stale_state_have_no_effect(self):
        vid = self.setup_state(State.PENDING_APPROVAL)
        before = self.snapshot()
        for event, state, payload, actor in (
            ('unknown', 'PENDING_APPROVAL', {}, 'tester'),
            ('human_approve', 'PENDING_APPROVAL', {}, 'tester'),
            ('human_approve', 'PENDING_APPROVAL', {'version_id': vid, 'extra': 'bad'}, 'tester'),
            ('human_approve', 'PENDING_APPROVAL', {'version_id': False}, 'tester'),
            ('human_approve', 'PENDING_APPROVAL', {'version_id': vid}, ''),
            ('human_approve', 'INVALID', {'version_id': vid}, 'tester'),
            ('human_approve', 'PENDING_APPROVAL', [], 'tester'),
            ('timeout', 'PENDING_APPROVAL', {'reason': ''}, 'tester'),
        ):
            with self.subTest(event=event, payload=payload), self.assertRaises(InvalidEvent):
                self.go(event, state, payload, actor=actor)
            self.assertEqual(self.snapshot(), before)
        with self.assertRaises(InvalidTransition):
            self.go('human_approve', 'CAPTURED', {'version_id': vid})
        with self.assertRaises(EnvelopeNotFound):
            self.go('enqueue', 'CAPTURED', eid='missing')
        self.assertEqual(self.snapshot(), before)
        self.go('human_approve', 'PENDING_APPROVAL', {'version_id': vid})
        approved = self.snapshot()
        with self.assertRaises(InvalidTransition):
            self.go('human_approve', 'PENDING_APPROVAL', {'version_id': vid})
        self.assertEqual(self.snapshot(), approved)

    def test_wrong_mode_or_trigger_cannot_enter_service(self):
        self.capture()
        for field, value in (('mode', 'auto'), ('trigger', 'agent-poll')):
            with self.subTest(field=field):
                self.conn.execute(f'UPDATE envelopes SET {field}=? WHERE id=?', (value, 'a'))
                before = self.snapshot()
                with self.assertRaises(InvalidTransition):
                    self.go('enqueue', 'CAPTURED')
                self.assertEqual(self.snapshot(), before)
                self.conn.execute("UPDATE envelopes SET mode='manual', trigger='human' WHERE id='a'")

    def test_rollback_of_special_methods(self):
        for state, event in ((State.PENDING_APPROVAL, Event.HUMAN_APPROVE),
                             (State.APPROVED, Event.ROUTE), (State.DELIVERING, Event.DELIVER_OK),
                             (State.FAILED, Event.ESCALATE)):
            eid = 'rollback_' + event
            vid = self.setup_state(state, eid)
            before = self.snapshot(eid)
            with self.subTest(event=event), patch.object(s, '_history', side_effect=sqlite3.IntegrityError('injected')):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.go(event, state, self.payload(event, vid, eid), eid)
            self.assertEqual(self.snapshot(eid), before)
            self.assertFalse(self.conn.in_transaction)

    def test_approve_reject_race(self):
        vid = self.setup_state(State.PENDING_APPROVAL)
        gate = Barrier(2)
        def run(event):
            conn = s.connect(self.path)
            try:
                gate.wait(timeout=5)
                try:
                    transition(conn, 'a', event, expected_state='PENDING_APPROVAL', actor='reviewer',
                               payload={'version_id': vid} if event == 'human_approve' else {})
                    return 'success'
                except InvalidTransition:
                    return 'stale'
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, e) for e in ('human_approve', 'human_reject')]
            self.assertCountEqual([f.result(timeout=10) for f in futures], ['success', 'stale'])
        self.assertEqual(len(s.get_history(self.conn, 'a')), 3)

    def test_lock_error_and_reopen(self):
        vid = self.setup_state(State.DELIVERING)
        other = s.connect(self.path, timeout=0.01)
        self.addCleanup(other.close)
        sha = s.get_version(self.conn, vid)['sha256']
        before = self.snapshot()
        with s.transaction(other):
            self.conn.execute('PRAGMA busy_timeout=10')
            with self.assertRaises(sqlite3.OperationalError):
                self.go('deliver_ok', 'DELIVERING', {'actual_sha256': sha})
        self.assertEqual(self.snapshot(), before)
        self.conn.close()
        self.conn = s.connect(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.go('deliver_ok', 'DELIVERING', {'actual_sha256': sha})['state'], 'DELIVERED')


if __name__ == '__main__':
    unittest.main()
