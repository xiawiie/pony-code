from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

import pytest

import pico.state.checkpoint_store as checkpoint_store_module
from pico.state.checkpoint_store import CheckpointStore
from pico.recovery.models import new_checkpoint_record, new_tool_change_record


def _entry(blob, path="note.txt", source_id="tc_1"):
    value = blob["blob_ref"]
    return {
        "path": path,
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": value,
        "before_hash": value,
        "before_mode": 0o644,
        "after_exists": True,
        "after_blob_ref": value,
        "after_hash": value,
        "after_mode": 0o644,
        "expected_current_hash": value,
        "source_tool_change_ids": [source_id] if source_id else [],
    }


def _prepared_entry(blob, path="note.txt"):
    return {
        "path": path,
        "before_exists": True,
        "before_blob_ref": blob["blob_ref"],
        "before_hash": blob["content_hash"],
        "before_mode": 0o644,
    }


def test_checkpoint_store_round_trips_records_tool_changes_and_blobs(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"hello\r\n", content_kind="text")

    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["file_entries"].append(_entry(blob, "README.md"))
    store.write_tool_change_record(tool_change)

    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "task_1", "", str(tmp_path))
    record["tool_change_ids"] = ["tc_1"]
    store.write_checkpoint_record(record)

    assert store.load_checkpoint_record("ckpt_1")["tool_change_ids"] == ["tc_1"]
    assert store.load_tool_change_record("tc_1")["tool_name"] == "write_file"
    assert store.read_blob(blob["blob_ref"]) == b"hello\r\n"


def test_checkpoint_store_blob_exists_is_metadata_only_and_fail_closed(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"cached")
    path = store._blob_path(blob["blob_ref"])

    assert store.blob_exists(blob["blob_ref"]) is True

    path.chmod(0o644)
    with pytest.raises(ValueError, match="blob file is unsafe"):
        store.blob_exists(blob["blob_ref"])

    path.unlink()
    assert store.blob_exists(blob["blob_ref"]) is False


def test_checkpoint_store_writes_use_file_lock(tmp_path, monkeypatch):
    calls = []

    @contextmanager
    def fake_lock(path):
        calls.append(Path(path).name)
        yield

    monkeypatch.setattr(checkpoint_store_module.file_lock, "locked_file", fake_lock)

    store = CheckpointStore(tmp_path)
    store.write_blob(b"hello", content_kind="text")
    store.write_tool_change_record(new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write"))
    store.write_checkpoint_record(new_checkpoint_record("ckpt_1", "turn", "s", "r", "task_1", "", str(tmp_path)))

    assert calls == [".checkpoint_store.lock", ".checkpoint_store.lock", ".checkpoint_store.lock"]


def test_prune_dry_run_scans_checkpoint_and_tool_change_blob_refs(tmp_path):
    store = CheckpointStore(tmp_path)
    referenced = store.write_blob(b"keep", "text")
    orphan = store.write_blob(b"remove", "text")
    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["file_entries"].append(_entry(referenced, "a.txt"))
    store.write_tool_change_record(tool_change)

    result = store.prune(dry_run=True)

    assert referenced["blob_ref"] not in result["unreferenced_blob_refs"]
    assert orphan["blob_ref"] in result["unreferenced_blob_refs"]


def test_prune_preserves_prepared_and_restore_intent_blobs(tmp_path):
    store = CheckpointStore(tmp_path)
    prepared = store.write_blob(b"prepared")
    intent_pre = store.write_blob(b"intent-pre")
    intent_post = store.write_blob(b"intent-post")
    tool = new_tool_change_record(
        "tc_refs", "", "turn", "write_file", "workspace_write", "owner"
    )
    tool["prepared_file_entries"] = [_prepared_entry(prepared, "a.txt")]
    store.write_tool_change_record(tool)
    checkpoint = new_checkpoint_record(
        "ckpt_refs", "restore", "", "", "", "", str(tmp_path.resolve())
    )
    checkpoint["status"] = "applying"
    checkpoint["restore_provenance"] = {
        "entries": [
            {
                "path": "a.txt",
                "pre_state": {
                    "exists": True,
                    "hash": intent_pre["content_hash"],
                    "blob_ref": intent_pre["blob_ref"],
                    "mode": 0o644,
                },
                "planned_post_state": {
                    "exists": True,
                    "hash": intent_post["content_hash"],
                    "blob_ref": intent_post["blob_ref"],
                    "mode": 0o644,
                },
                "outcome": "pending",
                "reason": "",
                "target_modified": False,
                "actual_post_state": {},
            }
        ]
    }
    store.write_checkpoint_record(checkpoint)
    result = store.prune(dry_run=True)
    assert prepared["blob_ref"] not in result["unreferenced_blob_refs"]
    assert intent_pre["blob_ref"] not in result["unreferenced_blob_refs"]
    assert intent_post["blob_ref"] not in result["unreferenced_blob_refs"]


def test_prune_holds_mutation_then_store_lock_through_scan_and_delete(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    orphan = store.write_blob(b"orphan")
    held = {"mutation": False, "store": False}

    @contextmanager
    def mutation_lock():
        assert held == {"mutation": False, "store": False}
        held["mutation"] = True
        try:
            yield
        finally:
            assert held["store"] is False
            held["mutation"] = False

    @contextmanager
    def store_lock(path, require_lock=False):
        assert require_lock is True
        assert held == {"mutation": True, "store": False}
        held["store"] = True
        try:
            yield
        finally:
            held["store"] = False

    real_scan = store._scan_records
    real_unlink = store._unlink_blob

    def scan(*args, **kwargs):
        assert held == {"mutation": True, "store": True}
        return real_scan(*args, **kwargs)

    def unlink(blob_ref):
        assert held == {"mutation": True, "store": True}
        return real_unlink(blob_ref)

    monkeypatch.setattr(store, "mutation_lock", mutation_lock)
    monkeypatch.setattr(checkpoint_store_module.file_lock, "locked_file", store_lock)
    monkeypatch.setattr(store, "_scan_records", scan)
    monkeypatch.setattr(store, "_unlink_blob", unlink)
    result = store.prune(dry_run=False)
    assert result["removed_blob_refs"] == [orphan["blob_ref"]]
    assert held == {"mutation": False, "store": False}


def test_prune_record_unlink_failure_never_sweeps_surviving_reference(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    kept = store.write_blob(b"must survive")
    record = new_checkpoint_record(
        "ckpt_old", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    record["created_at"] = "2000-01-01T00:00:00+00:00"
    record["file_entries"] = [_entry(kept, source_id="")]
    store.write_checkpoint_record(record)
    monkeypatch.setattr(
        store,
        "_unlink_store_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unlink failed")),
    )
    with pytest.raises(OSError, match="unlink failed"):
        store.prune(
            dry_run=False,
            older_than="1d",
            now=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
    assert store.load_checkpoint_record("ckpt_old")["checkpoint_id"] == "ckpt_old"
    assert store.read_blob(kept["blob_ref"]) == b"must survive"


def test_prune_retry_recovers_orphan_tool_after_mid_record_failure(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"tool history")
    tool = new_tool_change_record(
        "tc_old", "ckpt_old", "turn", "write_file", "workspace_write"
    )
    tool["status"] = "finalized"
    tool["ended_at"] = "2000-01-01T00:00:00+00:00"
    tool["file_entries"] = [_entry(blob, source_id="tc_old")]
    store.write_tool_change_record(tool)
    checkpoint = new_checkpoint_record(
        "ckpt_old", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    checkpoint["created_at"] = "2000-01-01T00:00:00+00:00"
    checkpoint["tool_change_ids"] = ["tc_old"]
    store.write_checkpoint_record(checkpoint)
    real_unlink = store._unlink_store_file
    failed = {"value": False}

    def fail_tool_once(directory, identity, name):
        if directory == store.tool_changes_dir and not failed["value"]:
            failed["value"] = True
            raise OSError("tool unlink failed")
        return real_unlink(directory, identity, name)

    monkeypatch.setattr(store, "_unlink_store_file", fail_tool_once)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    with pytest.raises(OSError, match="tool unlink failed"):
        store.prune(dry_run=False, older_than="1d", now=now)
    with pytest.raises(FileNotFoundError):
        store.load_checkpoint_record("ckpt_old")
    assert store.load_tool_change_record("tc_old")["status"] == "finalized"
    assert store.read_blob(blob["blob_ref"]) == b"tool history"

    retried = store.prune(dry_run=False, older_than="1d", now=now)
    assert retried["removed_tool_change_ids"] == ["tc_old"]
    assert blob["blob_ref"] in retried["removed_blob_refs"]


def test_prune_ignores_atomic_write_temp_files(tmp_path):
    store = CheckpointStore(tmp_path)
    temp_dir = store.blobs_dir / "ab"
    temp_dir.mkdir(parents=True)
    temp_file = temp_dir / ("a" * 64 + ".tmp")
    temp_file.write_bytes(b"in flight")

    result = store.prune(dry_run=False)

    assert temp_file.name not in result["unreferenced_blob_refs"]
    assert temp_file.name not in result["removed_blob_refs"]
    assert temp_file.exists()


def test_prune_fails_closed_when_record_enumeration_is_invalid(tmp_path):
    store = CheckpointStore(tmp_path)
    unique = store.write_blob(b"unique-reference", "text")
    (store.records_dir / "broken.json").write_text(
        '{"before_blob_ref":"' + unique["blob_ref"] + '"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid_record"):
        store.prune(dry_run=False)

    assert store.read_blob(unique["blob_ref"]) == b"unique-reference"


def test_prune_older_than_previews_and_applies_expired_records(tmp_path):
    store = CheckpointStore(tmp_path)
    old_blob = store.write_blob(b"old", "text")
    new_blob = store.write_blob(b"new", "text")

    old_tool_change = new_tool_change_record("tc_old", "", "task_old", "write_file", "workspace_write")
    old_tool_change["file_entries"].append(_entry(old_blob, "old.txt", "tc_old"))
    store.write_tool_change_record(old_tool_change)
    new_tool_change = new_tool_change_record("tc_new", "", "task_new", "write_file", "workspace_write")
    new_tool_change["file_entries"].append(_entry(new_blob, "new.txt", "tc_new"))
    store.write_tool_change_record(new_tool_change)

    old_record = new_checkpoint_record("ckpt_old", "turn", "s", "r", "task_old", "", str(tmp_path))
    old_record["created_at"] = "2026-06-01T00:00:00+00:00"
    old_record["tool_change_ids"] = ["tc_old"]
    old_record["file_entries"].append(_entry(old_blob, "old.txt", "tc_old"))
    store.write_checkpoint_record(old_record)
    new_record = new_checkpoint_record("ckpt_new", "turn", "s", "r", "task_new", "", str(tmp_path))
    new_record["created_at"] = "2026-07-01T00:00:00+00:00"
    new_record["tool_change_ids"] = ["tc_new"]
    new_record["file_entries"].append(_entry(new_blob, "new.txt", "tc_new"))
    store.write_checkpoint_record(new_record)

    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    preview = store.prune(dry_run=True, older_than="7d", now=now)

    assert preview["prunable_checkpoint_ids"] == ["ckpt_old"]
    assert preview["prunable_tool_change_ids"] == ["tc_old"]
    assert old_blob["blob_ref"] in preview["unreferenced_blob_refs"]
    assert new_blob["blob_ref"] not in preview["unreferenced_blob_refs"]
    assert store.load_checkpoint_record("ckpt_old")["checkpoint_id"] == "ckpt_old"
    assert store.load_tool_change_record("tc_old")["tool_change_id"] == "tc_old"
    assert store.has_blob(old_blob["blob_ref"]) is True

    applied = store.prune(dry_run=False, older_than="7d", now=now)

    assert applied["removed_checkpoint_ids"] == ["ckpt_old"]
    assert applied["removed_tool_change_ids"] == ["tc_old"]
    assert old_blob["blob_ref"] in applied["removed_blob_refs"]
    assert [item["checkpoint_id"] for item in store.list_checkpoint_records()] == ["ckpt_new"]
    assert [item["tool_change_id"] for item in store.list_tool_change_records()] == ["tc_new"]
    assert store.has_blob(old_blob["blob_ref"]) is False
    assert store.has_blob(new_blob["blob_ref"]) is True
