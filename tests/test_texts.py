import json
import tempfile
import unittest
from pathlib import Path

from video_pls_bot import texts


class TextsTests(unittest.TestCase):
    def test_defaults_without_file(self):
        self.assertEqual(texts.load(""), texts.DEFAULTS)

    def test_override_replaces_known_keys_and_ignores_the_rest(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "texts.json"
            path.write_text(json.dumps({"downloading": "Fetching", "unknown": "x", "welcome": 5,
                                        "digest.videos": ["{count} a", "{count} b", "{count} c"],
                                        "caption": ["not", "a string"]}))
            loaded = texts.load(str(path))
        self.assertEqual(loaded["downloading"], "Fetching")
        self.assertNotIn("unknown", loaded)
        self.assertEqual(loaded["welcome"], texts.DEFAULTS["welcome"])
        self.assertEqual(loaded["caption"], texts.DEFAULTS["caption"])
        self.assertEqual(len(loaded["digest.videos"]), 3)

    def test_unreadable_file_falls_back_to_defaults(self):
        self.assertEqual(texts.load("/nonexistent/texts.json"), texts.DEFAULTS)

    def test_every_alert_code_has_a_text(self):
        from video_pls_bot.alerts import REASONS, STAGES
        self.assertIn("download", STAGES)
        self.assertIn("process_restarted", REASONS)


if __name__ == "__main__":
    unittest.main()
