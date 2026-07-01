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
