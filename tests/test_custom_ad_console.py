"""啟動時印出的客製化服務資訊：在繁中 Windows 主控台（cp950，沒有「衞」）也不能讓啟動當掉。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestCustomAdConsole(unittest.TestCase):
    def test_start_prints_with_replace(self):
        src = (ROOT / "tools/start.py").read_text(encoding="utf-8")
        self.assertIn('print(CUSTOM_AD.encode(enc, "replace").decode(enc))', src)

    def test_serve_line_print_is_guarded(self):
        src = (ROOT / "tools/serve_line.py").read_text(encoding="utf-8")
        block = src[src.index("署名詳見 NOTICE") - 400: src.index("署名詳見 NOTICE") + 300]
        self.assertRegex(block, r"try:[\s\S]*print\([\s\S]*except Exception:")


if __name__ == "__main__":
    unittest.main()
