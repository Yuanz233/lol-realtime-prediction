import tempfile
import unittest
from pathlib import Path

from preview import build_preview_db
from storage import game, lists


class PreviewTest(unittest.TestCase):
    def test_preview_adds_live_game_without_touching_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = str(Path(folder) / "source.sqlite3")
            preview = str(Path(folder) / "preview.sqlite3")
            build_preview_db(source, preview, include_live=True)
            self.assertFalse(Path(source).exists())
            matches = lists(preview)
            self.assertEqual(len(matches["live"]), 1)
            self.assertEqual(matches["live"][0]["game_id"], "preview-live-001")
            self.assertEqual(len(game(preview, "preview-live-001")["points"]), 10)

    def test_preview_can_render_empty_live_state(self):
        with tempfile.TemporaryDirectory() as folder:
            preview = str(Path(folder) / "preview.sqlite3")
            build_preview_db(str(Path(folder) / "missing.sqlite3"), preview, include_live=False)
            self.assertEqual(lists(preview)["live"], [])


if __name__ == "__main__":
    unittest.main()
