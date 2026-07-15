import os

import pytest

from pico import recovery_manager as recovery_manager_module
from pico.checkpoint_store import CheckpointStore
from pico.recovery_manager import RecoveryManager
from pico.recovery_models import new_checkpoint_record, new_tool_change_record
from pico.recovery_paths import hash_bytes


def test_rename_swap_uses_linux_exchange_when_macos_api_is_unavailable(
    monkeypatch,
):
    calls = []

    def renameat2(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(recovery_manager_module, "_RENAMEATX_NP", None)
    monkeypatch.setattr(recovery_manager_module, "_RENAMEAT2", renameat2)

    recovery_manager_module._rename_swap(7, "first", "second")

    assert calls == [(7, b"first", 7, b"second", 2)]


def test_rename_noreplace_uses_distinct_linux_directory_descriptors(
    monkeypatch,
):
    calls = []

    def renameat2(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(recovery_manager_module, "_RENAMEATX_NP", None)
    monkeypatch.setattr(recovery_manager_module, "_RENAMEAT2", renameat2)

    recovery_manager_module._rename_noreplace(7, "source", 9, "destination")

    assert calls == [(7, b"source", 9, b"destination", 1)]


def _complete_modified_entry(store, root, path="note.txt"):
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    (root / path).write_bytes(b"after")
    return {
        "path": path,
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": before["blob_ref"],
        "before_hash": before["content_hash"],
        "before_mode": 0o644,
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "after_mode": 0o644,
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }


def _strict_entry(store, root, entry):
    entry = dict(entry)
    kind = entry["change_kind"]
    before_exists = kind in {"modified", "deleted"}
    after_exists = kind in {"modified", "created"}
    entry["before_exists"] = before_exists
    entry["after_exists"] = after_exists
    entry["before_mode"] = 0o644 if before_exists else None
    entry["after_mode"] = (
        (root / entry["path"]).stat().st_mode & 0o7777
        if after_exists
        else None
    )
    entry["source_tool_change_ids"] = []
    if not before_exists:
        entry["before_blob_ref"] = ""
        entry["before_hash"] = ""
    if after_exists:
        current = (root / entry["path"]).read_bytes()
        current_hash = hash_bytes(current)["content_hash"]
        entry.setdefault("after_hash", current_hash)
        entry.setdefault("expected_current_hash", entry["after_hash"])
        if entry["snapshot_eligible"] and not entry.get("after_blob_ref"):
            entry["after_blob_ref"] = store.write_blob(current)["blob_ref"]
        else:
            entry.setdefault("after_blob_ref", "")
    else:
        entry["after_blob_ref"] = ""
        entry["after_hash"] = ""
        entry["after_mode"] = None
        entry["expected_current_hash"] = ""
    return entry


def test_workspace_mismatch_is_invalid(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_wrong", "turn", "", "", "", "", str(other)
    )
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_wrong")

    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "workspace_mismatch"


def test_parent_symlink_is_invalid(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "linked")
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_link", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    record["file_entries"] = [
        {
            "path": "linked/note.txt",
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "before_exists": False,
            "before_blob_ref": "",
            "before_hash": "",
            "before_mode": None,
            "after_exists": False,
            "after_blob_ref": "",
            "after_hash": "",
            "after_mode": None,
            "expected_current_hash": "",
            "change_kind": "modified",
            "source_tool_change_ids": [],
        }
    ]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_link")

    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "symlink"


def test_plan_status_precedence_is_stable(tmp_path):
    manager = RecoveryManager(CheckpointStore(tmp_path), tmp_path)
    assert manager._plan_status(["restore"]) == "ready"
    assert manager._plan_status([]) == "noop"
    assert manager._plan_status(["review", "restore"]) == "review_required"
    assert manager._plan_status(["conflict", "review"]) == "conflicted"
    assert manager._plan_status(["error", "conflict"]) == "invalid"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda entry: entry.update(before_blob_ref="f" * 64), "blob_ref_hash_mismatch"),
        (
            lambda entry: entry.update(expected_current_hash="f" * 64),
            "expected_after_hash_mismatch",
        ),
        (lambda entry: entry.update(change_kind="created"), "change_kind_exists_mismatch"),
    ],
)
def test_plan_revalidates_untrusted_file_entry_consistency(
    tmp_path, mutation, reason
):
    del reason
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_tampered", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    entry = _complete_modified_entry(store, tmp_path)
    mutation(entry)
    record["file_entries"] = [entry]
    with pytest.raises(ValueError, match="invalid_file_entry"):
        store.write_checkpoint_record(record)


def test_turn_preview_rejects_pending_source_history(tmp_path):
    store = CheckpointStore(tmp_path)
    pending = new_tool_change_record(
        "tc_pending", "", "turn", "write_file", "workspace_write"
    )
    store.write_tool_change_record(pending)
    record = new_checkpoint_record(
        "ckpt_pending_source", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    record["tool_change_ids"] = ["tc_pending"]
    store.write_checkpoint_record(record)
    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])
    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "incomplete_tool_change_history"


def test_turn_preview_rejects_malformed_or_unknown_entry_sources(tmp_path):
    store = CheckpointStore(tmp_path)
    source = new_tool_change_record(
        "tc_done", "", "turn", "write_file", "workspace_write"
    )
    source["status"] = "finalized"
    store.write_tool_change_record(source)
    record = new_checkpoint_record(
        "ckpt_bad_source", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    entry = _complete_modified_entry(store, tmp_path)
    entry["source_tool_change_ids"] = [{"unhashable": True}]
    record["tool_change_ids"] = ["tc_done"]
    record["file_entries"] = [entry]
    with pytest.raises(ValueError, match="invalid_file_entry"):
        store.write_checkpoint_record(record)


def test_turn_preview_rejects_checkpoint_that_borrows_unrelated_source(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint_id = "ckpt_forged_provenance"
    source = new_tool_change_record(
        "tc_done", checkpoint_id, "turn", "write_file", "workspace_write"
    )
    source["status"] = "finalized"
    other = _complete_modified_entry(store, tmp_path, path="other.txt")
    other["source_tool_change_ids"] = ["tc_done"]
    source["file_entries"] = [other]
    store.write_tool_change_record(source)
    record = new_checkpoint_record(
        checkpoint_id, "turn", "", "", "", "", str(tmp_path.resolve())
    )
    claimed = _complete_modified_entry(store, tmp_path, path="note.txt")
    claimed["source_tool_change_ids"] = ["tc_done"]
    record["tool_change_ids"] = ["tc_done"]
    record["file_entries"] = [claimed]
    store.write_checkpoint_record(record)
    plan = RecoveryManager(store, tmp_path).preview_restore(checkpoint_id)
    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "incomplete_tool_change_history"


def test_preview_compares_current_mode_and_performs_no_writes(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_mode", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    record["file_entries"] = [_complete_modified_entry(store, tmp_path)]
    store.write_checkpoint_record(record)
    (tmp_path / "note.txt").chmod(0o600)
    for name in ("write_blob", "write_checkpoint_record", "write_tool_change_record"):
        monkeypatch.setattr(
            store,
            name,
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("preview attempted a write")
            ),
        )
    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])
    assert plan["status"] == "conflicted"
    assert plan["entries"][0]["reason"] == "mode_mismatch"


def test_preview_rejects_replaced_workspace_root_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(tmp_path / "metadata")
    record = new_checkpoint_record(
        "ckpt_root_identity", "turn", "", "", "", "", str(workspace.resolve())
    )
    record["file_entries"] = [_complete_modified_entry(store, workspace)]
    store.write_checkpoint_record(record)
    manager = RecoveryManager(store, workspace)
    workspace.rename(tmp_path / "workspace-old")
    workspace.mkdir()
    (workspace / "note.txt").write_bytes(b"after")
    plan = manager.preview_restore(record["checkpoint_id"])
    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "workspace_mismatch"


def test_preview_distinguishes_corrupt_blob_from_missing_blob(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_corrupt_blob", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    entry = _complete_modified_entry(store, tmp_path)
    record["file_entries"] = [entry]
    store.write_checkpoint_record(record)
    store._blob_path(entry["before_blob_ref"]).write_bytes(b"tampered")
    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])
    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "before_blob_hash_mismatch"


@pytest.mark.parametrize(
    ("status", "reviewed_at", "expected"),
    [
        ("applying", "", "review_required"),
        ("blocked", "", "noop"),
        ("failed", "", "noop"),
        ("noop", "", "noop"),
        ("partial", "2026-01-01T00:00:00+00:00", "noop"),
        ("applied", "", "noop"),
    ],
)
def test_restore_checkpoint_status_controls_preview(
    tmp_path, status, reviewed_at, expected
):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_restore_status",
        "restore",
        "",
        "",
        "",
        "",
        str(tmp_path.resolve()),
    )
    record["status"] = status
    record["reviewed_at"] = reviewed_at
    store.write_checkpoint_record(record)
    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])
    assert plan["status"] == expected


def test_unreviewed_partial_and_mode_unknown_are_not_ready(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_legacy_partial", "restore", "", "", "", "", str(tmp_path.resolve())
    )
    record["status"] = "partial"
    entry = _complete_modified_entry(store, tmp_path)
    entry.update(
        snapshot_eligible=False,
        ineligible_reason="mode_unknown",
        before_mode=None,
        after_mode=None,
        source_tool_change_ids=[],
    )
    record["file_entries"] = [entry]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])

    assert plan["status"] == "review_required"
    assert {entry["reason"] for entry in plan["entries"]} == {
        "mode_unknown",
        "partial_review_required",
    }


def test_store_rejects_old_entries_missing_current_fields(tmp_path):
    store = CheckpointStore(tmp_path)
    a = store.write_blob(b"a")
    b = store.write_blob(b"b")
    c = store.write_blob(b"c")
    (tmp_path / "note.txt").write_bytes(b"c")
    record = new_checkpoint_record(
        "ckpt_legacy_duplicates", "turn", "", "", "", "", str(tmp_path.resolve())
    )

    def legacy_entry(before, after):
        return {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "before_exists": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "after_exists": True,
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "expected_current_hash": after["content_hash"],
        }

    record["file_entries"] = [legacy_entry(a, b), legacy_entry(b, c)]
    with pytest.raises(ValueError, match="invalid_file_entry"):
        store.write_checkpoint_record(record)


def test_current_entry_cannot_hide_a_symlink_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "linked")
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_legacy_link", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    entry = _complete_modified_entry(store, tmp_path)
    entry["path"] = "linked/note.txt"
    record["file_entries"] = [entry]
    store.write_checkpoint_record(record)
    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])
    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "symlink"


def test_restore_preview_conflicts_when_current_hash_changed(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before\n", "text")
    after = store.write_blob(b"after\n", "text")
    (tmp_path / "note.txt").write_text("user edit\n", encoding="utf-8")
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
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
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
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_1")

    assert plan["entries"][0]["decision"] == "conflict"
    assert plan["entries"][0]["reason"] == "unexpected_file_present"


def test_store_rejects_entry_when_before_blob_unavailable(tmp_path):
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
    with pytest.raises(ValueError, match="invalid_file_entry"):
        store.write_checkpoint_record(record)


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
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_binary")
    entry = plan["entries"][0]

    assert entry["decision"] == "review"
    assert entry["reason"] == "discontinuous_history"
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
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    # 手动把 before-blob 从 store 里弄丢，模拟被外部删掉的场景。
    (store.blobs_dir / before["blob_ref"][:2] / before["blob_ref"]).unlink()

    manager = RecoveryManager(store, tmp_path)
    plan = manager.preview_restore("ckpt_1")
    result = manager.apply_restore("ckpt_1")

    assert result["restored_paths"] == []
    assert result["skipped_entries"] == [
        {"path": "note.txt", "reason": "before_blob_missing"}
    ]
    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "before_blob_missing"
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
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    monkeypatch.setattr(
        recovery_manager_module,
        "_write_restore_bytes",
        lambda descriptor, data: recovery_manager_module.os.write(
            descriptor, bytes(data)[:3]
        ),
    )

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_1")

    assert result["restored_paths"] == []
    assert result["skipped_entries"][0]["reason"] == "post_write_hash_mismatch"
    assert note_path.read_text(encoding="utf-8") == "after\n"


def test_restore_never_snapshots_legacy_sensitive_path(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"safe-before\n", "text")
    current = b"safe-after\n"
    target = tmp_path / ".env"
    target.write_bytes(current)
    record = new_checkpoint_record(
        "ckpt_sensitive_path", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": ".env",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    blobs_before = {path for path in store.blobs_dir.rglob("*") if path.is_file()}

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_sensitive_path")
    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_sensitive_path")

    assert plan["entries"][0]["decision"] == "review"
    assert plan["entries"][0]["reason"] == "sensitive_path"
    assert result["restored_paths"] == []
    assert target.read_bytes() == current
    assert {path for path in store.blobs_dir.rglob("*") if path.is_file()} == blobs_before


def test_restore_never_snapshots_sensitive_current_bytes(
    tmp_path, monkeypatch
):
    sentinel = "sk-legacy-current-secret"
    monkeypatch.setenv("PICO_OPENAI_API_KEY", sentinel)
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"safe-before\n", "text")
    current = b"safe-after\n"
    sensitive_current = f'KEY = "{sentinel}"\n'.encode()
    target = tmp_path / "note.txt"
    target.write_bytes(current)
    record = new_checkpoint_record(
        "ckpt_sensitive_current", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    blobs_before = {path for path in store.blobs_dir.rglob("*") if path.is_file()}
    manager = RecoveryManager(store, tmp_path)
    real_preview = manager.preview_restore

    def preview_then_replace(checkpoint_id):
        plan = real_preview(checkpoint_id)
        target.write_bytes(sensitive_current)
        return plan

    monkeypatch.setattr(manager, "preview_restore", preview_then_replace)

    result = manager.apply_restore("ckpt_sensitive_current")

    assert result["restored_paths"] == []
    assert result["skipped_entries"] == []
    assert target.read_bytes() == sensitive_current
    assert {path for path in store.blobs_dir.rglob("*") if path.is_file()} == blobs_before


def test_restore_rechecks_current_bytes_after_pre_blob_write(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"safe-before\n", "text")
    current = b"safe-after\n"
    sensitive_replacement = b"ghp_" + b"x" * 24
    target = tmp_path / "note.txt"
    target.write_bytes(current)
    record = new_checkpoint_record(
        "ckpt_recheck", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    real_write_blob = store.write_blob

    def replace_target_then_write(data, content_kind="text"):
        target.write_bytes(sensitive_replacement)
        return real_write_blob(data, content_kind)

    monkeypatch.setattr(store, "write_blob", replace_target_then_write)

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_recheck")

    assert result["restored_paths"] == []
    assert result["skipped_entries"][0]["reason"] == "current_state_changed"
    assert target.read_bytes() == sensitive_replacement


def test_restore_rejects_replaced_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(tmp_path / "metadata")
    before = store.write_blob(b"safe-before\n", "text")
    current = b"safe-after\n"
    (workspace / "note.txt").write_bytes(current)
    record = new_checkpoint_record(
        "ckpt_root_swap", "turn", "s", "r", "t", "", str(workspace)
    )
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, workspace, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    manager = RecoveryManager(store, workspace)
    moved = tmp_path / "workspace-original"
    workspace.rename(moved)
    workspace.mkdir()
    replacement = workspace / "note.txt"
    replacement.write_bytes(current)

    result = manager.apply_restore("ckpt_root_swap")

    assert result["restored_paths"] == []
    assert replacement.read_bytes() == current


def test_restore_rejects_replaced_intermediate_parent(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    parent = tmp_path / "sub"
    parent.mkdir()
    target = parent / "note.txt"
    current = b"safe-after\n"
    target.write_bytes(current)
    before = store.write_blob(b"safe-before\n", "text")
    record = new_checkpoint_record(
        "ckpt_parent_swap", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": "sub/note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    real_write_blob = store.write_blob
    swapped = False

    def swap_parent_then_write(data, content_kind="text"):
        nonlocal swapped
        if not swapped and data == current:
            moved = tmp_path / "sub-original"
            parent.rename(moved)
            parent.mkdir()
            (parent / "note.txt").write_bytes(current)
            swapped = True
        return real_write_blob(data, content_kind)

    monkeypatch.setattr(store, "write_blob", swap_parent_then_write)

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_parent_swap")

    assert result["restored_paths"] == []
    assert (parent / "note.txt").read_bytes() == current


def test_restore_atomic_swap_preserves_last_moment_replacement(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    current = b"safe-after\n"
    sensitive = b"ghp_" + b"z" * 24
    target = tmp_path / "note.txt"
    target.write_bytes(current)
    before = store.write_blob(b"safe-before\n", "text")
    record = new_checkpoint_record(
        "ckpt_atomic_swap", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    real_swap = recovery_manager_module._rename_swap
    raced = False

    def swap_after_final_compare(parent_descriptor, source, destination):
        nonlocal raced
        if destination == "note.txt" and not raced:
            raced = True
            target.write_bytes(sensitive)
        return real_swap(parent_descriptor, source, destination)

    monkeypatch.setattr(recovery_manager_module, "_rename_swap", swap_after_final_compare)

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_atomic_swap")

    assert result["restored_paths"] == []
    assert target.read_bytes() == sensitive


def test_restore_delete_does_not_claim_concurrently_recreated_target(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    current = b"created-after\n"
    sensitive = b"ghp_" + b"q" * 24
    target = tmp_path / "note.txt"
    target.write_bytes(current)
    record = new_checkpoint_record(
        "ckpt_delete_race", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "created",
            "snapshot_eligible": True,
            "before_blob_ref": "",
            "before_hash": "",
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    real_rename = recovery_manager_module.os.rename

    def rename_then_recreate(source, destination, **kwargs):
        result = real_rename(source, destination, **kwargs)
        if source == "note.txt":
            target.write_bytes(sensitive)
        return result

    monkeypatch.setattr(recovery_manager_module.os, "rename", rename_then_recreate)

    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_delete_race")

    assert result["restored_paths"] == []
    assert target.read_bytes() == sensitive


def test_restore_never_mutates_from_sensitive_source_blob(tmp_path, monkeypatch):
    sentinel = "sk-legacy-source-secret"
    monkeypatch.setenv("PICO_OPENAI_API_KEY", sentinel)
    store = CheckpointStore(tmp_path)
    sensitive_before = store.write_blob(sentinel.encode(), "text")
    current = b"safe-after\n"
    target = tmp_path / "note.txt"
    target.write_bytes(current)
    record = new_checkpoint_record(
        "ckpt_sensitive_source", "turn", "s", "r", "t", "", str(tmp_path)
    )
    record["file_entries"].append(
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "before_blob_ref": sensitive_before["blob_ref"],
            "before_hash": sensitive_before["content_hash"],
            "expected_current_hash": hash_bytes(current)["content_hash"],
            "content_kind": "text",
            "ineligible_reason": "",
        }
    )
    record["file_entries"] = [
        _strict_entry(store, tmp_path, record["file_entries"][0])
    ]
    store.write_checkpoint_record(record)
    blobs_before = {path for path in store.blobs_dir.rglob("*") if path.is_file()}

    manager = RecoveryManager(store, tmp_path)
    plan = manager.preview_restore("ckpt_sensitive_source")
    result = manager.apply_restore("ckpt_sensitive_source")

    assert result["restored_paths"] == []
    assert result["skipped_entries"] == [
        {"path": "note.txt", "reason": "sensitive_content"}
    ]
    assert plan["status"] == "review_required"
    assert plan["entries"][0]["reason"] == "sensitive_content"
    assert target.read_bytes() == current
    assert {path for path in store.blobs_dir.rglob("*") if path.is_file()} == blobs_before
