import tempfile
import unittest
from pathlib import Path

from config_loader import load_name


class ConfigLoaderTests(unittest.TestCase):
    def test_loads_plain_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "name.txt"
            path.write_text("  Pony  ", encoding="utf-8")
            self.assertEqual(load_name(path), "Pony")


if __name__ == "__main__":
    unittest.main()
