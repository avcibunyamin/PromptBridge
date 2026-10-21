"""F3-01b fix review (Codex, 2026-09-29): chain acceptance must see the newest message even in long works.

The panel loads at most 2000 messages (oldest first), so the newest message's digest comes from the
work summary instead of the capped list.
"""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app import store, works_store as ws
from app.provider_policy import DEFAULT, normalize, digest

PANEL_MESSAGE_CAP = 2000   # ui/src/api.ts listAllMessages


class LatestPolicyDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        ws_dir = Path(self.tmp.name) / 'ws'; ws_dir.mkdir()
        self.conn = store.connect(Path(self.tmp.name) / 'state.db'); self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'long', str(ws_dir))

    def add(self, policy, text='m'):
        ws.add_message(self.conn, self.wid, str(uuid4()), text, digest(policy), policy, digest(policy))

    def test_no_message_means_no_accepted_digest(self):
        self.assertIsNone(ws.work_summary(self.conn, self.wid)['latest_policy_digest'])

    def test_newest_digest_beyond_the_panel_message_cap(self):
        old = normalize(DEFAULT)
        new = normalize({'roles': {'author': ['claude'], 'reviewer': ['codex']}})
        self.assertNotEqual(digest(old), digest(new))
        for i in range(PANEL_MESSAGE_CAP + 1):
            self.add(old, f'm{i}')
        self.add(new, 'newest')
        # What the panel sees in its capped, oldest-first list: only the old digest.
        seen, cursor = [], None
        while len(seen) < PANEL_MESSAGE_CAP:
            page = ws.list_messages(self.conn, self.wid, cursor=cursor, limit=100)
            seen += page['items']; cursor = page['next_cursor']
            if not cursor:
                break
        self.assertEqual({m['policy_digest'] for m in seen[:PANEL_MESSAGE_CAP]}, {digest(old)})
        # The summary still reports the newest message's digest.
        self.assertEqual(ws.work_summary(self.conn, self.wid)['latest_policy_digest'], digest(new))


if __name__ == '__main__':
    unittest.main()
