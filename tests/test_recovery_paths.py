import os

import pytest

from pico.recovery_paths import (
    hash_file_bytes,
    normalize_workspace_relative_path,
    resolve_workspace_relative_path,
    resolve_workspace_relative_path_no_symlinks,
)


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


def test_no_symlink_resolver_rejects_parent_and_leaf_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "linked")
    with pytest.raises(ValueError, match="symlink"):
        resolve_workspace_relative_path_no_symlinks(tmp_path, "linked/note.txt")

    target = tmp_path / "target.txt"
    target.write_text("value", encoding="utf-8")
    os.symlink(target, tmp_path / "leaf.txt")
    with pytest.raises(ValueError, match="symlink"):
        resolve_workspace_relative_path_no_symlinks(tmp_path, "leaf.txt")


def test_no_symlink_resolver_allows_only_a_missing_leaf(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    assert resolve_workspace_relative_path_no_symlinks(
        tmp_path, "parent/new.txt"
    ) == parent / "new.txt"
    with pytest.raises(ValueError, match="missing_parent"):
        resolve_workspace_relative_path_no_symlinks(
            tmp_path, "missing/new.txt"
        )


def test_no_symlink_resolver_maps_regular_parent_to_missing_parent(tmp_path):
    (tmp_path / "parent").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="missing_parent"):
        resolve_workspace_relative_path_no_symlinks(
            tmp_path, "parent/new.txt"
        )
