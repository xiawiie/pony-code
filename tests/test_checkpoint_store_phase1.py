from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

import pico.checkpoint_store as checkpoint_store_module
from pico.checkpoint_store import CheckpointStore
from pico.recovery_models import new_checkpoint_record, new_tool_change_record


def test_checkpoint_store_round_trips_records_tool_changes_and_blobs(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"hello\r\n", content_kind="text")

    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["file_entries"].append({"path": "README.md", "after_blob_ref": blob["blob_ref"]})
    store.write_tool_change_record(tool_change)

    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "task_1", "", str(tmp_path))
    record["tool_change_ids"] = ["tc_1"]
    store.write_checkpoint_record(record)

    assert store.load_checkpoint_record("ckpt_1")["tool_change_ids"] == ["tc_1"]
    assert store.load_tool_change_record("tc_1")["tool_name"] == "write_file"
    assert store.read_blob(blob["blob_ref"]) == b"hello\r\n"


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
    tool_change["file_entries"].append({"path": "a.txt", "after_blob_ref": referenced["blob_ref"]})
    store.write_tool_change_record(tool_change)

    result = store.prune(dry_run=True)

    assert referenced["blob_ref"] not in result["unreferenced_blob_refs"]
    assert orphan["blob_ref"] in result["unreferenced_blob_refs"]


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


def test_prune_older_than_previews_and_applies_expired_records(tmp_path):
    store = CheckpointStore(tmp_path)
    old_blob = store.write_blob(b"old", "text")
    new_blob = store.write_blob(b"new", "text")

    old_tool_change = new_tool_change_record("tc_old", "", "task_old", "write_file", "workspace_write")
    old_tool_change["file_entries"].append({"path": "old.txt", "after_blob_ref": old_blob["blob_ref"]})
    store.write_tool_change_record(old_tool_change)
    new_tool_change = new_tool_change_record("tc_new", "", "task_new", "write_file", "workspace_write")
    new_tool_change["file_entries"].append({"path": "new.txt", "after_blob_ref": new_blob["blob_ref"]})
    store.write_tool_change_record(new_tool_change)

    old_record = new_checkpoint_record("ckpt_old", "turn", "s", "r", "task_old", "", str(tmp_path))
    old_record["created_at"] = "2026-06-01T00:00:00+00:00"
    old_record["tool_change_ids"] = ["tc_old"]
    old_record["file_entries"].append({"path": "old.txt", "after_blob_ref": old_blob["blob_ref"]})
    store.write_checkpoint_record(old_record)
    new_record = new_checkpoint_record("ckpt_new", "turn", "s", "r", "task_new", "", str(tmp_path))
    new_record["created_at"] = "2026-07-01T00:00:00+00:00"
    new_record["tool_change_ids"] = ["tc_new"]
    new_record["file_entries"].append({"path": "new.txt", "after_blob_ref": new_blob["blob_ref"]})
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
