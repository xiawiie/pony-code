from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import stat

import pytest

from pony.security import private_files
from pony.security.workspace_files import WorkspaceIOError
from pony.state.edit_checkpoint_store import EditCheckpointError, EditCheckpointStore


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
    if os.name == "posix":
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
        blob = state_root / "blobs" / before["blob_ref"]
        assert stat.S_IMODE(blob.stat().st_mode) == 0o600
        assert tuple((state_root / "blobs").iterdir()) == (blob,)


def test_classify_turn_compares_created_modified_deleted_and_before(tmp_path):
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

    assert set(store.classify_turn("turn-2")["matches_post"]) == {
        "modified.txt",
        "created.txt",
        "deleted.txt",
    }
    (root / "modified.txt").write_bytes(b"before")
    classification = store.classify_turn("turn-2")
    assert classification["matches_before"] == ("modified.txt",)
    assert set(classification["matches_post"]) == {"created.txt", "deleted.txt"}


def test_classify_turn_is_read_only_and_reports_conflict_and_incomplete(tmp_path):
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

    classification = store.classify_turn("turn-3")

    assert classification["conflicts"] == ("conflict.txt",)
    assert classification["incomplete"] == ("incomplete.txt",)
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
        store.classify_turn("turn-root")
    assert (root / "note.txt").read_bytes() == b"external"
    assert not (root / ".pony").exists()


def test_concurrent_classification_is_serialized_and_read_only(tmp_path):
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
                lambda store: store.classify_turn("turn-concurrent"),
                (first, second),
            )
        )

    assert results[0] == results[1]
    assert target.read_bytes() == b"after"


def test_replacement_root_with_preexisting_lock_is_zero_write(tmp_path):
    root = tmp_path / "repo"
    detached = tmp_path / "detached"
    root.mkdir()
    (root / "note.txt").write_bytes(b"before")
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)
    store.capture_before("turn-replaced", "note.txt")
    root.rename(detached)
    replacement_state = root / ".pony" / "edit-checkpoints"
    replacement_state.mkdir(parents=True, mode=0o700)
    lock = replacement_state / ".store.lock"
    lock.write_bytes(b"external-lock")
    lock.chmod(0o600)
    (root / "note.txt").write_bytes(b"external")
    before = (lock.read_bytes(), lock.stat().st_mtime_ns, tuple(replacement_state.iterdir()))

    with pytest.raises(WorkspaceIOError, match="workspace_entry_unsafe"):
        store.classify_turn("turn-replaced")

    assert (lock.read_bytes(), lock.stat().st_mtime_ns, tuple(replacement_state.iterdir())) == before
    assert (root / "note.txt").read_bytes() == b"external"


def test_root_swap_after_lock_is_caught_before_state_access(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    detached = tmp_path / "detached"
    root.mkdir()
    target = root / "note.txt"
    target.write_bytes(b"before")
    store = EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)
    store.capture_before("turn-inside-lock", "note.txt")
    target.write_bytes(b"after")
    store.record_post("turn-inside-lock", "note.txt")
    require_roots = store._require_roots
    calls = 0

    def swap_on_second_check():
        nonlocal calls
        calls += 1
        if calls == 2:
            root.rename(detached)
            root.mkdir()
            (root / "note.txt").write_bytes(b"external")
        require_roots()

    monkeypatch.setattr(store, "_require_roots", swap_on_second_check)
    with pytest.raises(WorkspaceIOError, match="workspace_entry_unsafe"):
        store.classify_turn("turn-inside-lock")
    assert calls == 2
    assert (root / "note.txt").read_bytes() == b"external"
    assert not (root / ".pony").exists()


def test_missing_precreated_lock_is_not_recreated(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "note.txt"
    target.write_bytes(b"before")
    state = root / ".pony" / "edit-checkpoints"
    store = EditCheckpointStore(state, root, max_file_bytes=1024)
    store.capture_before("turn-lock", "note.txt")
    target.write_bytes(b"after")
    store.record_post("turn-lock", "note.txt")
    lock = state / ".store.lock"
    lock.unlink()
    manifest = next((state / "turns").iterdir())
    before = manifest.read_bytes()

    with pytest.raises(FileNotFoundError, match="lock file missing"):
        store.classify_turn("turn-lock")

    assert not lock.exists()
    assert manifest.read_bytes() == before


def test_missing_before_blob_has_stable_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "note.txt"
    target.write_bytes(b"before")
    state = root / ".pony" / "edit-checkpoints"
    store = EditCheckpointStore(state, root, max_file_bytes=1024)
    before = store.capture_before("turn-blob", "note.txt")
    target.write_bytes(b"after")
    store.record_post("turn-blob", "note.txt")
    (state / "blobs" / before["blob_ref"]).unlink()

    with pytest.raises(EditCheckpointError) as exc_info:
        store.classify_turn("turn-blob")

    assert exc_info.value.code == "edit_checkpoint_blob_invalid"


def test_init_root_swap_before_lock_creation_is_zero_write(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    detached = tmp_path / "detached"
    root.mkdir()
    append = private_files.append_private_bytes

    def swap_before_append(*args, **kwargs):
        root.rename(detached)
        replacement = root / ".pony" / "edit-checkpoints"
        replacement.mkdir(parents=True)
        (replacement / "sentinel").write_bytes(b"external")
        return append(*args, **kwargs)

    monkeypatch.setattr(private_files, "append_private_bytes", swap_before_append)
    with pytest.raises(ValueError, match="private root changed"):
        EditCheckpointStore(root / ".pony" / "edit-checkpoints", root, max_file_bytes=1024)

    replacement = root / ".pony" / "edit-checkpoints"
    assert not (replacement / ".store.lock").exists()
    assert (replacement / "sentinel").read_bytes() == b"external"


@pytest.mark.parametrize(("exists", "create_file"), ((0, False), (1, True)))
def test_manifest_rejects_integer_exists_values(tmp_path, exists, create_file):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "note.txt"
    if create_file:
        target.write_bytes(b"before")
    state = root / ".pony" / "edit-checkpoints"
    store = EditCheckpointStore(state, root, max_file_bytes=1024)
    store.capture_before("turn-bool", "note.txt")
    manifest_path = next((state / "turns").iterdir())
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["paths"]["note.txt"]["before"]["exists"] = exists
    manifest_path.write_text(json.dumps(manifest), encoding="ascii")

    with pytest.raises(EditCheckpointError) as exc_info:
        store.classify_turn("turn-bool")

    assert exc_info.value.code == "edit_checkpoint_invalid"
