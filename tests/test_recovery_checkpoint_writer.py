from pico.checkpoint_store import CheckpointStore
from pico.recovery_checkpoint_writer import RecoveryCheckpointWriter
from pico.recovery_models import new_tool_change_record


def test_turn_checkpoint_links_real_tool_changes_and_file_entries(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"after\n", "text")
    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["status"] = "finalized"
    tool_change["affected_paths"] = ["note.txt"]
    tool_change["file_entries"] = [
        {
            "path": "note.txt",
            "change_kind": "created",
            "snapshot_eligible": True,
            "after_blob_ref": blob["blob_ref"],
            "after_hash": blob["content_hash"],
            "expected_current_hash": blob["content_hash"],
        }
    ]
    store.write_tool_change_record(tool_change)

    writer = RecoveryCheckpointWriter(store, tmp_path)
    checkpoint = writer.create_turn_checkpoint(
        session_id="session_1",
        run_id="run_1",
        turn_id="task_1",
        parent_checkpoint_id="",
        tool_change_ids=["tc_1"],
        verification_evidence=[],
    )

    loaded = store.load_checkpoint_record(checkpoint["checkpoint_id"])
    assert loaded["tool_change_ids"] == ["tc_1"]
    assert loaded["file_entries"][0]["path"] == "note.txt"


def test_turn_checkpoint_records_missing_tool_changes_without_aborting(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"after\n", "text")
    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["status"] = "finalized"
    tool_change["file_entries"] = [{"path": "note.txt", "after_blob_ref": blob["blob_ref"]}]
    store.write_tool_change_record(tool_change)

    writer = RecoveryCheckpointWriter(store, tmp_path)
    checkpoint = writer.create_turn_checkpoint(
        session_id="session_1",
        run_id="run_1",
        turn_id="task_1",
        parent_checkpoint_id="",
        tool_change_ids=["tc_1", "missing_tc"],
        verification_evidence=[],
    )

    loaded = store.load_checkpoint_record(checkpoint["checkpoint_id"])
    assert loaded["tool_change_ids"] == ["tc_1"]
    assert loaded["missing_tool_change_ids"] == ["missing_tc"]
    assert loaded["file_entries"][0]["path"] == "note.txt"
