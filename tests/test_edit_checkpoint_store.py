import os
import stat

import pytest

from pony.security.workspace_files import WorkspaceIOError
from pony.state.edit_checkpoint_store import EditCheckpointError, EditCheckpointStore


def test_checkpoint_restores_created_modified_and_deleted_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    modified = root / "modified.txt"
    deleted = root / "deleted.sh"
    modified.write_bytes(b"before\n")
    deleted.write_bytes(b"#!/bin/sh\n")
    deleted.chmod(0o755)
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)

    before = store.capture_before("turn-1", "modified.txt")
    modified.write_bytes(b"intermediate\n")
    assert store.capture_before("turn-1", "modified.txt") == before
    modified.write_bytes(b"after\n")
    store.record_post("turn-1", "modified.txt")

    store.capture_before("turn-1", "created.txt")
    (root / "created.txt").write_bytes(b"created\n")
    store.record_post("turn-1", "created.txt")

    store.capture_before("turn-1", "deleted.sh")
    deleted.unlink()
    store.record_post("turn-1", "deleted.sh")

    result = store.restore("turn-1")

    assert result["paths"] == ("modified.txt", "created.txt", "deleted.sh")
    assert modified.read_bytes() == b"before\n"
    assert not (root / "created.txt").exists()
    assert deleted.read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE(deleted.stat().st_mode) == 0o755


def test_restore_preflights_every_path_before_writing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a-before")
    (root / "b.txt").write_bytes(b"b-before")
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)

    for name in ("a.txt", "b.txt"):
        store.capture_before("turn-2", name)
        (root / name).write_bytes(f"{name}-after".encode())
        store.record_post("turn-2", name)
    (root / "b.txt").write_bytes(b"external-change")

    with pytest.raises(EditCheckpointError) as exc_info:
        store.restore("turn-2")

    assert exc_info.value.code == "edit_checkpoint_conflict"
    assert exc_info.value.paths == ("b.txt",)
    assert (root / "a.txt").read_bytes() == b"a.txt-after"
    assert (root / "b.txt").read_bytes() == b"external-change"


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo"))
def test_capture_rejects_unsafe_workspace_entries(tmp_path, kind):
    root = tmp_path / "repo"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_bytes(b"outside")
    target = root / "unsafe"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target, 0o600)
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)

    with pytest.raises(WorkspaceIOError) as exc_info:
        store.capture_before("turn-3", "unsafe")

    assert exc_info.value.code == "workspace_entry_unsafe"
    assert outside.read_bytes() == b"outside"


def test_checkpoint_state_is_private_and_traversal_is_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "note.txt").write_bytes(b"private before-image")
    state_root = root / ".pony" / "edit-checkpoints"
    store = EditCheckpointStore(state_root, root, max_file_bytes=1024)

    event = store.capture_before("turn-private", "note.txt")

    ledger = next((state_root / "turns").iterdir())
    blob = state_root / "blobs" / event["blob_ref"][:2] / event["blob_ref"]
    if os.name == "posix":
        assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
        assert stat.S_IMODE(blob.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="invalid relative path"):
        store.capture_before("turn-private", "../outside.txt")
