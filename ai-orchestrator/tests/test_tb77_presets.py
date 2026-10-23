"""Tests for provider policy presets."""
from pathlib import Path
import unittest

from app.provider_policy import effective, load_config

PRESET_FILES = [
    'providers.example.yaml',
    'providers.claude-only.yaml',
    'providers.codex-only.yaml',
    'providers.ollama-local.yaml',
]


class PresetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent

    def _verify_preset(self, filename):
        path = self.root_dir / filename
        self.assertTrue(path.is_file(), f"Preset file missing: {path}")

        config = load_config(path)
        self.assertIsInstance(config, dict)

        eff = effective(config)
        self.assertIsInstance(eff, dict)
        roles = eff.get('roles', {})
        self.assertIn('author', roles)
        self.assertIn('reviewer', roles)

        author_chain = roles['author']
        reviewer_chain = roles['reviewer']

        self.assertGreaterEqual(len(author_chain), 1, f"No author links in {filename}")
        self.assertGreaterEqual(len(reviewer_chain), 1, f"No reviewer links in {filename}")

        for link in author_chain:
            self.assertEqual(link.get('profile'), 'WRITE', f"Author link {link} profile is not WRITE in {filename}")

        for link in reviewer_chain:
            self.assertEqual(link.get('profile'), 'READ_ONLY', f"Reviewer link {link} profile is not READ_ONLY in {filename}")

        return eff

    def test_all_presets_valid(self):
        for filename in PRESET_FILES:
            with self.subTest(preset=filename):
                self._verify_preset(filename)

    def test_example_preset(self):
        eff = self._verify_preset('providers.example.yaml')
        self.assertEqual(eff['roles']['author'][0]['provider'], 'codex')
        self.assertEqual(eff['roles']['reviewer'][0]['provider'], 'claude')

    def test_claude_only_preset(self):
        eff = self._verify_preset('providers.claude-only.yaml')
        self.assertEqual([link['provider'] for link in eff['roles']['author']], ['claude'])
        self.assertEqual([link['provider'] for link in eff['roles']['reviewer']], ['claude'])
        self.assertEqual(eff.get('max_context_continuations'), 0)
        self.assertFalse(eff.get('require_handover_approval'))

    def test_codex_only_preset(self):
        eff = self._verify_preset('providers.codex-only.yaml')
        self.assertEqual([link['provider'] for link in eff['roles']['author']], ['codex'])
        self.assertEqual([link['provider'] for link in eff['roles']['reviewer']], ['codex'])
        self.assertEqual(eff.get('max_context_continuations'), 3)
        self.assertFalse(eff.get('require_handover_approval'))

    def test_ollama_local_preset(self):
        eff = self._verify_preset('providers.ollama-local.yaml')
        self.assertEqual([link['provider'] for link in eff['roles']['author']], ['ollama/qwen2.5-coder:7b'])
        self.assertEqual([link['provider'] for link in eff['roles']['reviewer']], ['ollama/qwen2.5-coder:7b'])
        self.assertEqual(eff.get('max_context_continuations'), 0)
        self.assertFalse(eff.get('require_handover_approval'))


if __name__ == '__main__':
    unittest.main()
