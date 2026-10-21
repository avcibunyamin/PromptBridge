"""F3-02 §3: loop budgets are opt-in policy version 3; version 2 stays byte-identical."""
import unittest
from app.provider_policy import normalize, digest, loop_budget, DEFAULT

ROLES = {'author': ['codex', 'claude'], 'reviewer': ['claude']}


class LoopPolicy(unittest.TestCase):
    def test_without_loop_version_2_digest_unchanged(self):
        # Pinned digest of the default policy before F3-02: queued messages must not go stale.
        self.assertEqual(normalize(DEFAULT)['version'], 2)
        self.assertNotIn('loop', normalize(DEFAULT))
        self.assertEqual(digest(normalize(DEFAULT)), digest({'roles': {'author': [{'provider': 'codex', 'profile': 'WRITE'}],
            'reviewer': [{'provider': 'claude', 'profile': 'READ_ONLY'}]}, 'require_handover_approval': False,
            'version': 2, 'max_context_continuations': 3, 'context_continuation_providers': ['codex']}))
        self.assertIsNone(loop_budget(normalize(DEFAULT)))

    def test_loop_defaults_and_values(self):
        for raw, expected in ((None, {'max_turns': 3, 'max_active_minutes': 30}), ({}, {'max_turns': 3, 'max_active_minutes': 30}),
                              ({'max_turns': 5}, {'max_turns': 5, 'max_active_minutes': 30}),
                              ({'max_turns': 1, 'max_active_minutes': 120}, {'max_turns': 1, 'max_active_minutes': 120})):
            with self.subTest(raw=raw):
                p = normalize({'roles': ROLES, 'loop': raw})
                self.assertEqual((p['version'], p['loop'], loop_budget(p)), (3, expected, expected))

    def test_loop_changes_digest(self):
        self.assertNotEqual(digest(normalize({'roles': ROLES})), digest(normalize({'roles': ROLES, 'loop': {}})))
        self.assertNotEqual(digest(normalize({'roles': ROLES, 'loop': {'max_turns': 2}})),
                            digest(normalize({'roles': ROLES, 'loop': {'max_turns': 3}})))

    def test_invalid_loop_rejected(self):
        for bad in ([], 3, {'max_turns': 0}, {'max_turns': 6}, {'max_turns': True}, {'max_turns': 2.0},
                    {'max_active_minutes': 4}, {'max_active_minutes': 121}, {'max_active_minutes': '30'}, {'turns': 3}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize({'roles': ROLES, 'loop': bad})


if __name__ == '__main__':
    unittest.main()
