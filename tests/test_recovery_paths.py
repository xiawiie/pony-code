import pytest

from pico.recovery_paths import hash_file_bytes, normalize_workspace_relative_path, resolve_workspace_relative_path


def test_normalize_rejects_absolute_and_traversal_paths():
    assert normalize_workspace_relative_path("src\\app.py") == "src/app.py"

    with pytest.raises(ValueError, match="absolute"):
        normalize_workspace_relative_path("/tmp/app.py")

    with pytest.raises(ValueError, match="traversal"):
        normalize_workspace_relative_path("../outside.py")


def test_resolve_keeps_target_inside_workspace(tmp_path):
    assert resolve_workspace_relative_path(tmp_path, "src/app.py") == tmp_path / "src" / "app.py"


def test_hash_file_bytes_preserves_raw_bytes(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"line\r\n")

    result = hash_file_bytes(path)

    assert result["hash_algorithm"] == "sha256"
    assert result["size_bytes"] == 6
    assert result["content_hash"]
