from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import stat

import pytest

from pony.security.workspace_files import WorkspaceIOError
from pony.state.edit_checkpoint_store import EditCheckpointStore


def test_capture_keeps_first_before_image_and_latest_post(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "note.txt"
    target.write_bytes(b"before")
    state_root = root / ".pony" / "edit-checkpoints"
    store = EditCheckpointStore(state_root, root, max_file_bytes=1024)

    before = store.capture_before("turn-1", "note.txt")
    target.write_bytes(b"intermediate")
    assert store.capture_before("turn-1", "note.txt") == before
    store.record_post("turn-1", "note.txt")
    target.write_bytes(b"latest")
    post = store.record_post("turn-1", "note.txt")

    manifest_path = next((state_root / "turns").iterdir())
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    assert manifest_path.suffix == ".json"
    assert manifest["paths"]["note.txt"]["post"] == post
    assert post["sha256"] == hashlib.sha256(b"latest").hexdigest()
    assert store.read_before("turn-1", "note.txt") == b"before"
    if os.name == "posix":
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
        blob = state_root / "blobs" / before["blob_ref"][:2] / before["blob_ref"]
        assert stat.S_IMODE(blob.stat().st_mode) == 0o600


def test_assess_restore_classifies_created_modified_deleted_and_restored(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "modified.txt").write_bytes(b"before")
    (root / "deleted.txt").write_bytes(b"deleted")
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)

    store.capture_before("turn-2", "modified.txt")
    (root / "modified.txt").write_bytes(b"after")
    store.record_post("turn-2", "modified.txt")
    store.capture_before("turn-2", "created.txt")
    (root / "created.txt").write_bytes(b"created")
    store.record_post("turn-2", "created.txt")
    store.capture_before("turn-2", "deleted.txt")
    (root / "deleted.txt").unlink()
    store.record_post("turn-2", "deleted.txt")

    assert set(store.assess_restore("turn-2")["eligible"]) == {
        "modified.txt",
        "created.txt",
        "deleted.txt",
    }
    (root / "modified.txt").write_bytes(b"before")
    assessment = store.assess_restore("turn-2")
    assert assessment["already_restored"] == ("modified.txt",)
    assert set(assessment["eligible"]) == {"created.txt", "deleted.txt"}


def test_assess_restore_is_read_only_and_reports_conflict_and_incomplete(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "conflict.txt").write_bytes(b"before")
    (root / "incomplete.txt").write_bytes(b"before")
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)
    store.capture_before("turn-3", "conflict.txt")
    (root / "conflict.txt").write_bytes(b"after")
    store.record_post("turn-3", "conflict.txt")
    store.capture_before("turn-3", "incomplete.txt")
    (root / "conflict.txt").write_bytes(b"external")
    before = {path.name: path.read_bytes() for path in root.glob("*.txt")}

    assessment = store.assess_restore("turn-3")

    assert assessment["conflicts"] == ("conflict.txt",)
    assert assessment["incomplete"] == ("incomplete.txt",)
    assert {path.name: path.read_bytes() for path in root.glob("*.txt")} == before


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
        store.capture_before("turn-unsafe", "unsafe")

    assert exc_info.value.code == "workspace_entry_unsafe"
    assert outside.read_bytes() == b"outside"


def test_root_drift_and_traversal_fail_before_state_write(tmp_path):
    root = tmp_path / "repo"
    detached = tmp_path / "detached"
    root.mkdir()
    (root / "note.txt").write_bytes(b"before")
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)
    store.capture_before("turn-root", "note.txt")
    with pytest.raises(ValueError, match="invalid relative path"):
        store.capture_before("turn-root", "../outside.txt")
    root.rename(detached)
    root.mkdir()
    (root / "note.txt").write_bytes(b"external")

    with pytest.raises(WorkspaceIOError, match="workspace_entry_unsafe"):
        store.assess_restore("turn-root")
    assert (root / "note.txt").read_bytes() == b"external"
    assert not (root / ".pony").exists()


def test_concurrent_assessment_is_serialized_and_read_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "note.txt"
    target.write_bytes(b"before")
    state_root = root / ".pony" / "edit-checkpoints"
    first = EditCheckpointStore(state_root, root, max_file_bytes=1024)
    second = EditCheckpointStore(state_root, root, max_file_bytes=1024)
    first.capture_before("turn-concurrent", "note.txt")
    target.write_bytes(b"after")
    first.record_post("turn-concurrent", "note.txt")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda store: store.assess_restore("turn-concurrent"),
                (first, second),
            )
        )

    assert results[0] == results[1]
    assert target.read_bytes() == b"after"
