import os
import tempfile
import unittest
from pathlib import Path

from a_stock_committee.cli import _load_news_items, _load_symbols, _normalize_symbol_codes


class CliInputTests(unittest.TestCase):
    def test_load_news_items_from_inbox_dir_sorted_by_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp)
            old_path = inbox / "old_note.txt"
            new_path = inbox / "new_note.md"
            ignored_path = inbox / "ignore.csv"
            old_path.write_text("旧NVL72资料", encoding="utf-8")
            new_path.write_text("新摩根士丹利NVL72 BOM资料", encoding="utf-8")
            ignored_path.write_text("ignore", encoding="utf-8")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))

            items = _load_news_items("", "", str(inbox))

            self.assertEqual([item.title for item in items], ["new_note.md", "old_note.txt"])
            self.assertEqual(items[0].source, "inbox")
            self.assertIn("摩根士丹利", items[0].summary)
            self.assertTrue(items[0].published_at)

    def test_symbol_file_inputs_are_normalized_for_risk_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.txt"
            path.write_text("600745.SSE\n# comment\n002342\n", encoding="utf-8")

            symbols = _load_symbols("SH600519", str(path))
            codes = _normalize_symbol_codes(symbols)

            self.assertEqual(codes, ["600519", "600745", "002342"])


if __name__ == "__main__":
    unittest.main()
