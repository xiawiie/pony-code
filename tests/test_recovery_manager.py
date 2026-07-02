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


def test_preview_flags_review_when_before_blob_unavailable(tmp_path):
    # 直接构造一份 file_entry: snapshot_eligible=True, change_kind=modified, 但没有 before_blob_ref。
    # _build_file_entries 平时会把 snapshot_eligible flip 掉；这里保留原样是为了独立
    # 验证 _plan_entry 里的“before_blob_unavailable”防御分支不会静默漏掉。
    store = CheckpointStore(tmp_path)
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("after\n", encoding="utf-8")
    record = new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": "",
            "before_hash": "",
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "expected_current_hash": after["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_1")

    assert plan["entries"][0]["decision"] == "review"
    assert plan["entries"][0]["reason"] == "before_blob_unavailable"


def test_preview_explains_ineligible_binary_without_claiming_snapshot(tmp_path):
    store = CheckpointStore(tmp_path)
    (tmp_path / "image.bin").write_bytes(b"\x00\x01after")
    record = new_checkpoint_record("ckpt_binary", "turn", "s", "r", "t", "", str(tmp_path))
    record["file_entries"].append(
        {
            "path": "image.bin",
            "change_kind": "modified",
            "snapshot_eligible": False,
            "before_blob_ref": "",
            "before_hash": "",
            "after_blob_ref": "",
            "after_hash": "",
            "expected_current_hash": "",
            "content_kind": "binary",
            "ineligible_reason": "binary_file",
        }
    )
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_binary")
    entry = plan["entries"][0]

    assert entry["decision"] == "review"
    assert entry["reason"] == "binary_file"
    assert entry["restore_available"] is False
    assert entry["captured_before_state"] is False
    assert "no restorable before-state snapshot" in entry["recovery_note"]


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
