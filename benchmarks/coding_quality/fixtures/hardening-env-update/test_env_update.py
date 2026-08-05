import tempfile
import unittest
from pathlib import Path

from env_update import update_env


class EnvUpdateTests(unittest.TestCase):
    def test_valid_update_preserves_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# provider\nMODEL=old\n", encoding="utf-8")
            update_env(path, ["MODEL=new"])
            self.assertEqual(path.read_text(encoding="utf-8"), "# provider\nMODEL=new\n")


if __name__ == "__main__":
    unittest.main()
