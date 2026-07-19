import os
from pathlib import Path
from unittest.mock import patch

def test_explicit_home_override_remains_visible(tmp_path, isolated_home):
    explicit_home = tmp_path / "explicit-home"
    explicit_home.mkdir()

    with patch.dict(os.environ, {"HOME": str(explicit_home)}, clear=True):
        assert Path.home() == explicit_home
    with patch.dict(os.environ, {}, clear=True):
        assert Path.home() == isolated_home
