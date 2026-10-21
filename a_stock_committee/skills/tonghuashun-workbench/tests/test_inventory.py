from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inventory.py"
spec = importlib.util.spec_from_file_location("ths_inventory", SCRIPT)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def test_utf8_and_gbk_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.xml"
            for encoding in ("utf-8-sig", "gb18030"):
                path.write_bytes('<root><name>行情</name></root>'.encode(encoding))
                self.assertEqual(inventory.xml_root(path).findtext("name"), "行情")

    def test_nested_html_and_context_dependent_keys(self):
        parser = inventory.ShortcutTable()
        parser.feed('<table><tr><td>F4</td><td><b>Index</b> &amp; quote</td></tr>'
                    '<tr><td>F4</td><td>Account query</td></tr></table>')
        self.assertEqual(parser.rows, [["F4", "Index & quote"], ["F4", "Account query"]])

    def test_output_cannot_modify_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output in (root, root / "nested"):
                with self.subTest(output=str(output)):
                    argv = [str(SCRIPT), "--root", str(root), "--output", str(output)]
                    with patch("sys.argv", argv), redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as exc:
                            inventory.main()
                    self.assertEqual(exc.exception.code, 2)
                    self.assertFalse((output / "catalog.json").exists())

    def test_markdown_escapes_cells(self):
        data = {"created_at": "test", "toolbar": [], "shortcuts": [],
                "function_entries": [], "drawing": [], "quote_fields": ["a|b\nc"],
                "formula_functions": []}
        result = inventory.markdown(data)
        self.assertIn("a\\|b c", result)
        self.assertIn("快捷键", result)


if __name__ == "__main__":
    unittest.main()
