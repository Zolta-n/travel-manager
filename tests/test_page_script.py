"""The page is one script: two functions with the same name silently replace each other."""
from collections import Counter
from pathlib import Path
import re
import unittest

PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"


class PageScriptTests(unittest.TestCase):
    def test_no_function_is_declared_twice(self):
        names = Counter(re.findall(r"(?:^|[\s;{}])(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", PAGE.read_text()))
        self.assertEqual([n for n, c in names.items() if c > 1], [])


if __name__ == "__main__":
    unittest.main()
