"""K-064: writing authors keep a progress note the next model continues from; read-only roles only read it."""
import unittest

from app import work_prompt as wp


class NotesRuleTest(unittest.TestCase):
    def test_writing_roles_keep_the_note(self):
        for role in ('author', 'correction'):
            self.assertIn(f'Keep a short progress note in {wp.NOTES_FILE}', wp.RULES[role])

    def test_read_only_roles_never_write_it(self):
        for role in ('reviewer', 'author_read_only', 'correction_read_only'):
            self.assertNotIn('Keep a short progress note', wp.RULES[role])
        self.assertIn(wp.NOTES_FILE, wp.RULES['reviewer'])
        self.assertIn(wp.NOTES_FILE, wp.RULES['author_read_only'])
        self.assertIn(wp.NOTES_FILE, wp.RULES['correction_read_only'])


if __name__ == '__main__':
    unittest.main()


class SignedInFromCatalogTest(unittest.TestCase):
    """K-064: the accounts list tells the panel which accounts the last catalog saw signed in."""

    def setUp(self):
        from app import catalog
        self.catalog, self.saved = catalog, dict(catalog._cache)
        self.addCleanup(lambda: catalog._cache.update(self.saved))

    def test_states(self):
        live = {'provider': 'agy', 'account': 'a1', 'source': 'live', 'identity': 'agy@a1/m'}
        self.catalog._cache.update(entries=None, errors={})
        self.assertIsNone(self.catalog.signed_in('agy', 'a1'))            # never refreshed
        self.catalog._cache.update(entries=[live], errors={'agy@a2': 'unauthenticated', 'agy@a3': 'timeout'})
        self.assertIs(self.catalog.signed_in('agy', 'a1'), True)
        self.assertIs(self.catalog.signed_in('agy', 'a2'), False)
        self.assertIsNone(self.catalog.signed_in('agy', 'a3'))            # an error that is not a sign-out
        self.assertIsNone(self.catalog.signed_in('agy', 'a4'))            # not listed
