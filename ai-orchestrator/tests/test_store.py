"""Real SQLite contract tests for G2; no delivery or network operations."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import tempfile
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import patch

from app import store as s


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.conn = s.connect(self.path)
        self.addCleanup(self.conn.close)
        s.migrate(self.conn)

    def capture(self, eid='msg_a', content=b'original\xff\n'):
        return s.insert_captured(self.conn, {
            'id': eid, 'source_agent': 'chatgpt', 'target_agent': 'claude',
            'origin_ref': 'agents/chatgpt/outbox/output.md'}, content)[1]

    def pending(self, eid='msg_a'):
        vid = self.capture(eid)
        s.set_state(self.conn, eid, 'CAPTURED', 'PENDING_APPROVAL', 'enqueue', 'system')
        return vid

    def delivering(self, eid='msg_a'):
        vid = self.pending(eid)
        s.set_approved(self.conn, eid, vid, 'operator')
        s.record_delivery_intent(self.conn, eid, vid, f'claude/inbox/{eid}.md')
        return vid

    def failed(self):
        vid = self.delivering()
        s.set_state(self.conn, 'msg_a', 'DELIVERING', 'FAILED', 'deliver_err', 'system')
        return vid

    def snapshot(self):
        return {table: [tuple(row) for row in self.conn.execute(f'SELECT * FROM {table}')]
                for table in ('envelopes', 'versions', 'history')}

    def test_happy_path_and_audit(self):
        captured = self.pending()
        edited = s.add_edited_version(self.conn, 'msg_a', 'düzenlenmiş\n'.encode(), actor='operator')
        s.set_approved(self.conn, 'msg_a', edited, 'operator')
        s.record_delivery_intent(self.conn, 'msg_a', edited, 'claude/inbox/msg_a.md')
        sha = hashlib.sha256(s.get_content(self.conn, edited)).hexdigest()
        s.record_delivered(self.conn, 'msg_a', sha)
        row = s.get_envelope(self.conn, 'msg_a')
        self.assertEqual(row['state'], 'DELIVERED')
        self.assertEqual(row['approved_version_id'], edited)
        self.assertEqual(row['delivered_version_id'], edited)
        self.assertEqual(row['delivered_sha256'], sha)
        self.assertTrue(row['delivered_at'])
        self.assertEqual(s.get_content(self.conn, captured), b'original\xff\n')
        history = s.get_history(self.conn, 'msg_a')
        self.assertEqual([h['event'] for h in history],
                         ['capture', 'enqueue', 'edited', 'human_approve', 'deliver_intent', 'deliver_ok'])
        self.assertEqual(history[2]['from_state'], 'PENDING_APPROVAL')
        self.assertEqual(history[2]['to_state'], 'PENDING_APPROVAL')
        self.assertEqual(history[3]['actor'], 'operator')
        self.assertEqual(len(s.list_by_state(self.conn, 'DELIVERED')), 1)

    def test_preview_and_immutable_versions(self):
        vid = self.capture()
        edited = s.add_edited_version(self.conn, 'msg_a', b'edited\xfe\n')
        self.assertIsNone(s.get_envelope(self.conn, 'msg_a')['approved_version_id'])
        self.assertEqual(s.get_content(self.conn, vid), b'original\xff\n')
        self.assertEqual(s.get_content(self.conn, edited), b'edited\xfe\n')
        self.assertTrue(s.get_history(self.conn, 'msg_a')[-1]['detail']['diff_base64'])
        before = self.snapshot()
        for table in ('versions', 'history'):
            for sql in (f'DELETE FROM {table}', f'UPDATE {table} SET envelope_id=envelope_id'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql)
        self.assertEqual(self.snapshot(), before)

    def test_migration_idempotent_and_foreign_keys(self):
        self.capture()
        before = self.snapshot()
        s.migrate(self.conn)
        s.migrate(self.conn)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertFalse(self.conn.execute('PRAGMA foreign_key_list(envelopes)').fetchall())
        self.assertEqual(self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], s.SCHEMA_VERSION)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO history(envelope_id,at,to_state) VALUES ('missing','now','CAPTURED')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("""INSERT INTO versions(version_id,envelope_id,kind,content,sha256,bytes,created_at)
                VALUES ('orphan','missing','captured',x'00','hash',1,'now')""")

    def test_unknown_migration_version_preserved(self):
        self.conn.execute("UPDATE schema_meta SET value='future' WHERE key='schema_version'")
        with self.assertRaises(ValueError):
            s.migrate(self.conn)
        self.assertEqual(self.conn.execute('SELECT value FROM schema_meta').fetchone()[0], 'future')

    def test_capture_version_failure_rolls_back_envelope(self):
        self.conn.execute("""CREATE TEMP TRIGGER fail_version BEFORE INSERT ON versions
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.capture()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_approval_ownership_and_double_approval(self):
        own = self.pending()
        other = self.pending('msg_other')
        for vid in (other, 'missing'):
            before = self.snapshot()
            with self.assertRaises(s.VersionMismatch):
                s.set_approved(self.conn, 'msg_a', vid, 'operator')
            self.assertEqual(self.snapshot(), before)
        s.set_approved(self.conn, 'msg_a', own, 'operator')
        before = self.snapshot()
        with self.assertRaises(s.InvalidTransition):
            s.set_approved(self.conn, 'msg_a', own, 'operator')
        self.assertEqual(self.snapshot(), before)

    def test_delivery_requires_approved_version(self):
        original = self.pending()
        edited = s.add_edited_version(self.conn, 'msg_a', b'edit')
        before = self.snapshot()
        with self.assertRaises(s.VersionMismatch):
            s.record_delivery_intent(self.conn, 'msg_a', original, 'target.md')
        self.assertEqual(self.snapshot(), before)
        s.set_approved(self.conn, 'msg_a', edited, 'operator')
        before = self.snapshot()
        with self.assertRaises(s.VersionMismatch):
            s.record_delivery_intent(self.conn, 'msg_a', original, 'target.md')
        self.assertEqual(self.snapshot(), before)

    def test_hash_mismatch_keeps_delivering_then_correct_hash_completes(self):
        vid = self.delivering()
        before = self.snapshot()
        with self.assertRaises(s.DeliveredHashMismatch):
            s.record_delivered(self.conn, 'msg_a', '0' * 64)
        self.assertEqual(self.snapshot(), before)
        s.record_delivered(self.conn, 'msg_a', s.get_version(self.conn, vid)['sha256'])
        self.assertEqual(s.get_envelope(self.conn, 'msg_a')['state'], 'DELIVERED')

    def test_reopen_delivery_intent_before_delivered_hash_exists(self):
        vid = self.delivering()
        self.conn.close()
        self.conn = s.connect(self.path)
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        env = s.get_envelope(self.conn, 'msg_a')
        self.assertEqual(env['state'], 'DELIVERING')
        self.assertEqual(env['delivered_path'], 'claude/inbox/msg_a.md')
        self.assertIsNone(env['delivered_sha256'])
        expected = s.get_version(self.conn, env['delivered_version_id'])['sha256']
        self.assertEqual(expected, hashlib.sha256(s.get_content(self.conn, vid)).hexdigest())
        s.record_delivered(self.conn, 'msg_a', expected)

    def test_reset_preserves_history_and_rejects_old_approval(self):
        vid = self.failed()
        before = s.get_envelope(self.conn, 'msg_a')
        history = s.get_history(self.conn, 'msg_a')
        reset = s.reset_for_reapproval(self.conn, 'msg_a', actor='operator', reason='review again')
        self.assertEqual(reset['state'], 'PENDING_APPROVAL')
        self.assertTrue(all(reset[field] is None for field in s.RESET_FIELDS))
        self.assertEqual(reset['captured_version_id'], vid)
        self.assertEqual(reset['origin_ref'], before['origin_ref'])
        after = s.get_history(self.conn, 'msg_a')
        self.assertEqual(after[:-1], history)
        self.assertEqual(after[-1]['detail']['previous'], {k: before[k] for k in s.RESET_FIELDS})
        self.assertEqual(s.get_content(self.conn, vid), b'original\xff\n')
        unchanged = self.snapshot()
        with self.assertRaises(s.VersionMismatch):
            s.record_delivery_intent(self.conn, 'msg_a', vid, 'target.md')
        with self.assertRaises(s.InvalidTransition):
            s.reset_for_reapproval(self.conn, 'msg_a', actor='operator', reason='twice')
        self.assertEqual(self.snapshot(), unchanged)

    def test_reset_wrong_state_and_missing_message(self):
        self.capture()
        for eid in ('msg_a', 'missing'):
            before = self.snapshot()
            with self.assertRaises(s.InvalidTransition):
                s.reset_for_reapproval(self.conn, eid, actor='operator', reason='invalid')
            self.assertEqual(self.snapshot(), before)

    def test_retry_preserves_intent(self):
        vid = self.failed()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            s.record_delivery_intent(self.conn, 'msg_a', vid, 'changed.md', expected_from='FAILED')
        self.assertEqual(self.snapshot(), before)
        s.record_delivery_intent(self.conn, 'msg_a', vid, 'claude/inbox/msg_a.md', expected_from='FAILED')
        self.assertEqual(s.get_envelope(self.conn, 'msg_a')['state'], 'DELIVERING')

    def test_history_failure_rolls_back_all_writes(self):
        for action in ('capture', 'edit', 'state', 'approve', 'intent', 'delivered', 'reset'):
            with self.subTest(action=action):
                eid = 'rollback_' + action
                vid = None
                if action in ('delivered', 'reset'):
                    vid = self.delivering(eid)
                elif action != 'capture':
                    vid = self.pending(eid)
                if action == 'intent':
                    s.set_approved(self.conn, eid, vid, 'operator')
                if action == 'reset':
                    s.set_state(self.conn, eid, 'DELIVERING', 'FAILED', 'deliver_err', 'system')
                calls = {
                    'capture': lambda: self.capture(eid),
                    'edit': lambda: s.add_edited_version(self.conn, eid, b'new'),
                    'state': lambda: s.set_state(self.conn, eid, 'PENDING_APPROVAL', 'REJECTED', 'reject', 'human'),
                    'approve': lambda: s.set_approved(self.conn, eid, vid, 'operator'),
                    'intent': lambda: s.record_delivery_intent(self.conn, eid, vid, 'target.md'),
                    'delivered': lambda: s.record_delivered(self.conn, eid, s.get_version(self.conn, vid)['sha256']),
                    'reset': lambda: s.reset_for_reapproval(self.conn, eid, actor='operator', reason='retry'),
                }
                before = self.snapshot()
                with patch.object(s, '_history', side_effect=sqlite3.IntegrityError('audit injected')):
                    with self.assertRaises(sqlite3.IntegrityError):
                        calls[action]()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.conn.in_transaction)

    def test_outer_transaction_is_not_committed_by_nested_daos(self):
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            with s.transaction(self.conn):
                vid = self.pending()
                s.set_approved(self.conn, 'msg_a', vid, 'operator')
                raise RuntimeError('abort outer operation')
        self.assertEqual(self.snapshot(), before)

    def test_nested_failure_rolls_back_savepoint_only(self):
        with s.transaction(self.conn):
            self.capture('keep')
            with patch.object(s, '_history', side_effect=RuntimeError('fail')):
                with self.assertRaises(RuntimeError):
                    self.capture('discard')
            self.assertTrue(self.conn.in_transaction)
        self.assertIsNotNone(s.get_envelope(self.conn, 'keep'))
        self.assertIsNone(s.get_envelope(self.conn, 'discard'))

    def test_concurrent_approvals_have_one_winner(self):
        vid = self.pending()
        gate = Barrier(2)

        def approve(actor):
            conn = s.connect(self.path)
            try:
                gate.wait(timeout=5)
                try:
                    s.set_approved(conn, 'msg_a', vid, actor)
                    return 'approved'
                except s.InvalidTransition:
                    return 'stale'
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(approve, actor) for actor in ('reviewer_a', 'reviewer_b')]
            results = [f.result(timeout=10) for f in futures]
        self.assertCountEqual(results, ['approved', 'stale'])
        self.assertEqual(len([h for h in s.get_history(self.conn, 'msg_a') if h['event'] == 'human_approve']), 1)

    def test_lock_timeout_is_bounded_and_no_write_is_committed(self):
        vid = self.pending()
        other = s.connect(self.path, timeout=0.01)
        self.addCleanup(other.close)
        before = self.snapshot()
        with s.transaction(self.conn):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                s.set_approved(other, 'msg_a', vid, 'operator')
        self.assertFalse(other.in_transaction)
        self.assertEqual(self.snapshot(), before)
        s.set_approved(other, 'msg_a', vid, 'operator')

    def test_missing_reads_and_raw_content_contract(self):
        self.assertIsNone(s.get_envelope(self.conn, 'missing'))
        self.assertIsNone(s.get_version(self.conn, 'missing'))
        self.assertEqual(s.get_history(self.conn, 'missing'), [])
        with self.assertRaises(KeyError):
            s.get_content(self.conn, 'missing')
        with self.assertRaises(TypeError):
            self.capture(content='not bytes')
        self.assertFalse(s.list_by_state(self.conn, 'CAPTURED'))


if __name__ == '__main__':
    unittest.main()
