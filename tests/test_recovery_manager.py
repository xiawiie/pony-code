from pathlib import Path

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


def test_restore_preview_conflicts_when_expected_empty_but_file_exists(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    (tmp_path / "note.txt").write_text("user recreated\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "deleted",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "after_blob_ref": "",
            "after_hash": "",
            "expected_current_hash": "",
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_1")

    assert plan["entries"][0]["decision"] == "conflict"
    assert plan["entries"][0]["reason"] == "unexpected_file_present"


def test_apply_restore_skips_entry_when_before_blob_missing(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
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
    # 手动把 before-blob 从 store 里弄丢，模拟被外部删掉的场景。
    (store.blobs_dir / before["blob_ref"][:2] / before["blob_ref"]).unlink()

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_1")

    assert result["restored_paths"] == []
    assert result["skipped_entries"][0]["path"] == "note.txt"
    assert result["skipped_entries"][0]["reason"] == "before_blob_missing"
    # 磁盘状态未被破坏
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"


def test_apply_restore_verifies_temp_write_before_replacing_target(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    note_path = tmp_path / "note.txt"
    note_path.write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
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
    original_write_bytes = Path.write_bytes

    def truncated_write(self, data):
        if self.parent == tmp_path:
            return original_write_bytes(self, bytes(data)[:3])
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", truncated_write)

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_1")

    assert result["restored_paths"] == []
    assert result["skipped_entries"][0]["reason"] == "post_write_hash_mismatch"
    assert note_path.read_text(encoding="utf-8") == "after\n"
