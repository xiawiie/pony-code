import tempfile
import unittest
from pathlib import Path

from workspace_paths import resolve_workspace_path


class WorkspacePathTests(unittest.TestCase):
    def test_relative_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(resolve_workspace_path(root, "src/app.py"), root / "src/app.py")


if __name__ == "__main__":
    unittest.main()
