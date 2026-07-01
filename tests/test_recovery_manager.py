from pico.checkpoint_store import CheckpointStore
from pico.recovery_manager import RecoveryManager
from pico.recovery_models import new_checkpoint_record


def test_restore_preview_conflicts_when_current_hash_changed(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("user edit\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "expected_current_hash": after["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_1")

    assert plan["entries"][0]["decision"] == "conflict"
