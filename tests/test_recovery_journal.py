import json
import stat
from contextlib import contextmanager

import pytest

from pony.state.checkpoint_store import CheckpointStore
from pony.recovery.checkpoint_writer import RecoveryCheckpointWriter
from pony.recovery.manager import RecoveryManager, RestoreMutationError
from pony.recovery import manager as recovery_manager_module
from pony.recovery.models import new_checkpoint_record, new_tool_change_record


def _modified_checkpoint(store, root, *, checkpoint_id="ckpt_source"):
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    target = root / "note.txt"
    target.write_bytes(b"after")
    target.chmod(0o640)
    record = new_checkpoint_record(
        checkpoint_id,
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(root.resolve()),
    )
    record["file_entries"] = [
        {
            "path": "note.txt",
            "change_kind": "modified",
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "before_exists": True,
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["content_hash"],
            "before_mode": 0o600,
            "after_exists": True,
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "after_mode": 0o640,
            "expected_current_hash": after["content_hash"],
            "source_tool_change_ids": [],
        }
    ]
    store.write_checkpoint_record(record)
    return target


def seed_unrelated_recovery_blocker(store, root, blocker_kind):
    if blocker_kind == "pending_tool":
        store.write_tool_change_record(
            new_tool_change_record(
                "tc_foreign_pending",
                "",
                "other-turn",
                "write_file",
                "workspace_write",
                "foreign",
            )
        )
        return
    if blocker_kind == "invalid_record":
        (store.tool_changes_dir / "foreign-invalid.json").write_bytes(b"{invalid")
        return
    record = new_checkpoint_record(
        "ckpt_foreign_" + blocker_kind,
        "restore",
        "session",
        "run",
        "other-turn",
        "",
        str(root.resolve()),
    )
    record["status"] = blocker_kind
    record["owner_id"] = "foreign"
    record["reviewed_at"] = ""
    store.write_checkpoint_record(record)


def _restorable_entry(
    store,
    path,
    before_bytes,
    after_bytes,
    *,
    before_mode=0o644,
    after_mode=0o644,
):
    before = store.write_blob(before_bytes)
    after = store.write_blob(after_bytes)
    return {
        "path": path,
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": before["blob_ref"],
        "before_hash": before["content_hash"],
        "before_mode": before_mode,
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "after_mode": after_mode,
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }


def build_three_file_restore(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    record = new_checkpoint_record(
        "ckpt_three_files",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    record["file_entries"] = [
        _restorable_entry(store, "first.txt", b"first-before", b"first-after"),
        _restorable_entry(store, "second.txt", b"second-before", b"second-after"),
        _restorable_entry(store, "third.txt", b"third-before", b"third-after"),
    ]
    for name in ("first", "second", "third"):
        (tmp_path / f"{name}.txt").write_bytes(f"{name}-after".encode())
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"]


def build_created_file_restore(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    target = tmp_path / "created.txt"
    target.write_bytes(b"created-after")
    after = store.write_blob(b"created-after")
    record = new_checkpoint_record(
        "ckpt_created",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    record["file_entries"] = [
        {
            "path": "created.txt",
            "change_kind": "created",
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "before_exists": False,
            "before_blob_ref": "",
            "before_hash": "",
            "before_mode": None,
            "after_exists": True,
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["content_hash"],
            "after_mode": 0o644,
            "expected_current_hash": after["content_hash"],
            "source_tool_change_ids": [],
        }
    ]
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"], target


def build_applying_journal(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    target = tmp_path / "note.txt"
    target.write_bytes(b"current-after")
    target.chmod(0o640)
    pre_blob = store.write_blob(b"current-after")
    post_blob = store.write_blob(b"restored-before")
    pre_state = {
        "exists": True,
        "hash": pre_blob["content_hash"],
        "blob_ref": pre_blob["blob_ref"],
        "mode": 0o640,
    }
    post_state = {
        "exists": True,
        "hash": post_blob["content_hash"],
        "blob_ref": post_blob["blob_ref"],
        "mode": 0o600,
    }
    record = new_checkpoint_record(
        "ckpt_applying",
        "restore",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    record["status"] = "applying"
    record["owner_id"] = "owner-crashed"
    record["restore_provenance"] = {
        "source_checkpoint_id": "ckpt_source",
        "plan_id": "plan_crashed",
        "entries": [
            {
                "path": "note.txt",
                "pre_state": pre_state,
                "planned_post_state": post_state,
                "outcome": "pending",
                "reason": "",
                "target_modified": False,
                "actual_post_state": {},
            }
        ],
    }
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"], target, post_state


def build_partial_restore_with_one_proven_entry(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    entry = _restorable_entry(
        store, "note.txt", b"current-c", b"restored-a"
    )
    target = tmp_path / "note.txt"
    target.write_bytes(b"restored-a")
    record = new_checkpoint_record(
        "ckpt_partial",
        "restore",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    record["status"] = "partial"
    record["reviewed_at"] = "2026-07-10T00:00:00+00:00"
    record["reviewed_by"] = "cli"
    record["review_reason"] = "explicit_cli_resolution"
    record["file_entries"] = [entry]
    record["restore_provenance"] = {"entries": []}
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"], target


def test_applying_journal_contains_all_intents_before_first_mutation(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(
        store,
        tmp_path,
        checkpoint_writer=RecoveryCheckpointWriter(store, tmp_path),
    )
    observed = []

    def inspect_before_apply(restore_checkpoint_id, intent):
        journal = store.load_checkpoint_record(restore_checkpoint_id)
        observed.append(
            (journal["status"], journal["restore_provenance"]["entries"])
        )
        target.write_bytes(store.read_blob(intent["planned_post_state"]["blob_ref"]))
        target.chmod(intent["planned_post_state"]["mode"])
        return {
            "outcome": "applied",
            "actual_post_state": intent["planned_post_state"],
        }

    monkeypatch.setattr(manager, "_apply_intent", inspect_before_apply)
    manager.apply_restore("ckpt_source")
    assert observed[0][0] == "applying"
    assert observed[0][1][0]["outcome"] == "pending"


def test_prestate_blob_failure_causes_zero_target_mutations(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    original = target.read_bytes()
    monkeypatch.setattr(
        store,
        "write_blob",
        lambda data, content_kind="text": (_ for _ in ()).throw(
            OSError("fsync failed")
        ),
    )
    with pytest.raises(OSError, match="fsync failed"):
        manager.apply_restore("ckpt_source")
    assert target.read_bytes() == original
    assert not [
        record
        for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
    ]


def test_target_parent_fsync_precedes_applied_outcome(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    events = []
    real_parent_fsync = manager._fsync_target_parent
    real_update = store.update_checkpoint_record

    def fsync_parent(path):
        events.append("target-parent-fsync")
        return real_parent_fsync(path)

    def update(checkpoint_id, transform, *, expected_status=None):
        result = real_update(
            checkpoint_id, transform, expected_status=expected_status
        )
        if any(
            entry.get("outcome") == "applied"
            for entry in result.get("restore_provenance", {}).get("entries", [])
        ):
            events.append("applied-outcome")
        return result

    monkeypatch.setattr(manager, "_fsync_target_parent", fsync_parent)
    monkeypatch.setattr(store, "update_checkpoint_record", update)
    manager.apply_restore("ckpt_source")
    assert events.index("target-parent-fsync") < events.index("applied-outcome")


def test_successful_restore_applies_source_before_mode(tmp_path):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_source")
    assert result["status"] == "applied"
    assert target.read_bytes() == b"before"
    assert target.stat().st_mode & 0o777 == 0o600


def test_apply_lock_covers_plan_rebuild_journal_mutation_and_terminal_rmw(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    events = []
    held = {"value": False}
    real_plan = manager._preview_restore_locked
    real_apply = manager._apply_intent
    real_update = store.update_checkpoint_record

    @contextmanager
    def lock():
        held["value"] = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            held["value"] = False

    def plan(checkpoint_id):
        assert held["value"] is True
        events.append("strict-plan")
        return real_plan(checkpoint_id)

    def apply_intent(restore_checkpoint_id, intent):
        assert held["value"] is True
        events.append("target-mutation")
        return real_apply(restore_checkpoint_id, intent)

    def update(checkpoint_id, transform, *, expected_status=None):
        assert held["value"] is True
        result = real_update(
            checkpoint_id, transform, expected_status=expected_status
        )
        if result.get("status") in {
            "applied",
            "blocked",
            "failed",
            "partial",
            "noop",
        }:
            events.append("terminal-rmw")
        return result

    monkeypatch.setattr(store, "mutation_lock", lock)
    monkeypatch.setattr(manager, "_preview_restore_locked", plan)
    monkeypatch.setattr(manager, "_apply_intent", apply_intent)
    monkeypatch.setattr(store, "update_checkpoint_record", update)
    manager.apply_restore("ckpt_source")
    assert events[0] == "lock-enter"
    assert events.index("strict-plan") < events.index("target-mutation")
    assert events.index("target-mutation") < events.index("terminal-rmw")
    assert events[-1] == "lock-exit"


def test_apply_rebuilds_plan_and_blocks_post_preview_change(tmp_path):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    assert manager.preview_restore("ckpt_source")["status"] == "ready"
    target.write_bytes(b"external-after-preview")
    result = manager.apply_restore("ckpt_source")
    audit = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert result["status"] == "blocked"
    assert audit["status"] == "blocked"
    assert target.read_bytes() == b"external-after-preview"


def test_noop_apply_writes_successful_noop_audit(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_noop",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )
    store.write_checkpoint_record(record)
    result = RecoveryManager(store, tmp_path).apply_restore(record["checkpoint_id"])
    audit = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert result["status"] == "noop"
    assert audit["status"] == "noop"


def test_restore_journal_metadata_uses_store_redactor(tmp_path):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    sentinel = "sk-journal-owner-sentinel"

    def redact(value):
        return json.loads(json.dumps(value).replace(sentinel, "<redacted>"))

    store.set_redactor(redact)
    manager = RecoveryManager(store, tmp_path)
    manager.owner_id = sentinel
    result = manager.apply_restore("ckpt_source")
    raw = store._record_path(result["restore_checkpoint_id"]).read_bytes()
    assert sentinel.encode() not in raw


def test_execution_uses_the_canonical_persisted_journal_intent(tmp_path):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)

    def redact(value):
        return json.loads(json.dumps(value).replace("note.txt", "redacted.txt"))

    store.set_redactor(redact)
    result = RecoveryManager(store, tmp_path).apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert result["status"] == "failed"
    assert target.read_bytes() == b"after"
    assert journal["restore_provenance"]["entries"][0]["path"] == "redacted.txt"


@pytest.mark.parametrize("fail_update", [1, 2])
def test_outcome_or_terminal_rmw_failure_leaves_parseable_applying_journal(
    tmp_path, monkeypatch, fail_update
):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    real_update = store.update_checkpoint_record
    calls = {"count": 0}

    def update(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == fail_update:
            raise OSError("durable RMW failed")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_checkpoint_record", update)
    with pytest.raises(OSError, match="durable RMW failed"):
        manager.apply_restore("ckpt_source")
    journal = next(
        item
        for item in store.list_checkpoint_records(strict=True)
        if item.get("checkpoint_type") == "restore"
    )
    assert journal["status"] == "applying"
    assert journal["restore_provenance"]["entries"][0]["outcome"] in {
        "pending",
        "applied",
    }


def test_target_parent_fsync_failure_after_swap_leaves_pending_applying(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    real_swap = recovery_manager_module._rename_swap
    real_fsync = recovery_manager_module.os.fsync
    swapped = {"value": False}

    def swap(*args):
        result = real_swap(*args)
        swapped["value"] = True
        return result

    def fsync(descriptor):
        if swapped["value"] and stat.S_ISDIR(recovery_manager_module.os.fstat(descriptor).st_mode):
            raise OSError("target parent fsync failed")
        return real_fsync(descriptor)

    monkeypatch.setattr(recovery_manager_module, "_rename_swap", swap)
    monkeypatch.setattr(recovery_manager_module.os, "fsync", fsync)
    with pytest.raises(OSError, match="mutation_durability_unknown"):
        manager.apply_restore("ckpt_source")
    journal = next(
        item
        for item in store.list_checkpoint_records(strict=True)
        if item.get("checkpoint_type") == "restore"
    )
    assert target.read_bytes() == b"before"
    assert target.stat().st_mode & 0o777 == 0o600
    assert journal["status"] == "applying"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "pending"


def test_delete_rename_then_read_failure_leaves_pending_applying(
    tmp_path, monkeypatch
):
    store, manager, source_id, target = build_created_file_restore(tmp_path)
    real_read = recovery_manager_module._read_restore_leaf

    def read(parent_descriptor, name, **kwargs):
        if ".restore-delete." in name:
            raise OSError("moved read failed")
        return real_read(parent_descriptor, name, **kwargs)

    monkeypatch.setattr(recovery_manager_module, "_read_restore_leaf", read)
    with pytest.raises(OSError, match="mutation_durability_unknown"):
        manager.apply_restore(source_id)
    journal = next(
        item
        for item in store.list_checkpoint_records(strict=True)
        if item.get("checkpoint_type") == "restore"
    )
    assert not target.exists()
    assert journal["status"] == "applying"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "pending"


def test_second_entry_failure_is_partial_with_not_attempted_tail(
    tmp_path, monkeypatch
):
    store, manager, checkpoint_id = build_three_file_restore(tmp_path)
    real_apply = manager._apply_intent
    calls = {"count": 0}

    def fail_second(restore_checkpoint_id, intent):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("second write failed")
        return real_apply(restore_checkpoint_id, intent)

    monkeypatch.setattr(manager, "_apply_intent", fail_second)
    result = manager.apply_restore(checkpoint_id)
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert result["status"] == "partial"
    assert [
        entry["outcome"]
        for entry in journal["restore_provenance"]["entries"]
    ] == ["applied", "failed", "not_attempted"]
    assert len(journal["file_entries"]) == 1


def test_replace_success_then_outcome_crash_stays_applying_and_reconciles(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    monkeypatch.setattr(
        manager,
        "_write_intent_outcome",
        lambda *args: (_ for _ in ()).throw(
            KeyboardInterrupt("crash before outcome RMW")
        ),
    )
    with pytest.raises(KeyboardInterrupt, match="outcome RMW"):
        manager.apply_restore("ckpt_source")
    journal = next(
        record
        for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
    )
    assert target.read_bytes() == b"before"
    assert journal["status"] == "applying"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "pending"
    preview = manager.preview_restore_journal_resolution(journal["checkpoint_id"])
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"


def test_unlink_success_then_outcome_crash_reconciles_absent_post_state(
    tmp_path, monkeypatch
):
    store, manager, source_id, target = build_created_file_restore(tmp_path)
    monkeypatch.setattr(
        manager,
        "_write_intent_outcome",
        lambda *args: (_ for _ in ()).throw(
            KeyboardInterrupt("crash after unlink")
        ),
    )
    with pytest.raises(KeyboardInterrupt, match="after unlink"):
        manager.apply_restore(source_id)
    journal = next(
        record
        for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
    )
    assert not target.exists()
    preview = manager.preview_restore_journal_resolution(journal["checkpoint_id"])
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"


def test_delete_reread_detects_reappeared_target_as_uncertain_partial(
    tmp_path, monkeypatch
):
    store, manager, source_id, target = build_created_file_restore(tmp_path)
    real_fsync = manager._fsync_target_parent

    def recreate_after_fsync(path):
        real_fsync(path)
        target.write_bytes(b"external-recreated")

    monkeypatch.setattr(manager, "_fsync_target_parent", recreate_after_fsync)
    result = manager.apply_restore(source_id)
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert result["status"] == "partial"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "uncertain"
    assert journal["restore_provenance"]["entries"][0]["target_modified"] is True


def test_outer_post_mutation_fsync_error_is_not_recorded_as_unmodified(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    monkeypatch.setattr(
        manager,
        "_fsync_target_parent",
        lambda path: (_ for _ in ()).throw(OSError("outer fsync failed")),
    )
    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert target.read_bytes() == b"before"
    assert result["status"] == "applied"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "applied"
    assert journal["restore_provenance"]["entries"][0]["target_modified"] is True


def test_target_modified_sensitive_actual_post_is_not_blobbed(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    sentinel = b"sk-sensitive-actual-post-value"

    def corrupt_then_fail(*args, **kwargs):
        (tmp_path / "note.txt").write_bytes(sentinel)
        raise RestoreMutationError("reread_hash_mismatch", target_modified=True)

    monkeypatch.setattr(
        recovery_manager_module,
        "_mutate_workspace_bytes_if_unchanged",
        corrupt_then_fail,
    )
    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])
    intent = journal["restore_provenance"]["entries"][0]
    assert result["status"] == "partial"
    assert intent["outcome"] == "uncertain"
    assert intent["reason"] == "manual_recovery_required"
    assert intent["actual_post_state"] == {}
    assert all(
        sentinel not in path.read_bytes()
        for path in store.blobs_dir.rglob("*")
        if path.is_file()
    )


def test_applying_journal_reconciles_actual_tuple_without_writing(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    target.write_bytes(store.read_blob(post_state["blob_ref"]))
    target.chmod(post_state["mode"])
    before = store.load_checkpoint_record(restore_id)
    preview = manager.preview_restore_journal_resolution(restore_id)
    after = store.load_checkpoint_record(restore_id)
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"
    assert before == after


def test_resolve_applied_unconfirmed_generates_proven_undo_entry(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    target.write_bytes(store.read_blob(post_state["blob_ref"]))
    target.chmod(post_state["mode"])
    preview = manager.preview_restore_journal_resolution(restore_id)
    resolved = manager.resolve_restore_journal(
        restore_id,
        expected_record_hash=preview["record_hash"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )
    assert resolved["status"] == "applied"
    assert resolved["restore_provenance"]["entries"][0]["outcome"] == "applied"
    assert len(resolved["file_entries"]) == 1
    assert resolved["file_entries"][0]["before_hash"] == resolved[
        "restore_provenance"
    ]["entries"][0]["pre_state"]["hash"]


def test_resolve_not_applied_is_failed_without_undo_entry(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    del target, post_state
    preview = manager.preview_restore_journal_resolution(restore_id)
    assert preview["entries"][0]["classification"] == "not_applied"
    resolved = manager.resolve_restore_journal(
        restore_id,
        expected_record_hash=preview["record_hash"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )
    assert resolved["status"] == "failed"
    assert resolved["file_entries"] == []


def test_resolve_unknown_tuple_is_partial_manual_recovery(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    del post_state
    target.write_bytes(b"external-value")
    preview = manager.preview_restore_journal_resolution(restore_id)
    resolved = manager.resolve_restore_journal(
        restore_id,
        expected_record_hash=preview["record_hash"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )
    assert resolved["status"] == "partial"
    assert resolved["restore_provenance"]["entries"][0]["outcome"] == "uncertain"
    assert (
        resolved["restore_provenance"]["entries"][0]["reason"]
        == "manual_recovery_required"
    )


def test_partial_checkpoint_undoes_only_proven_file_entries(tmp_path):
    store, manager, partial_id, target = (
        build_partial_restore_with_one_proven_entry(tmp_path)
    )
    partial = store.load_checkpoint_record(partial_id)
    assert partial["status"] == "partial"
    assert len(partial["file_entries"]) == 1
    undo = manager.apply_restore(partial_id)
    assert undo["status"] == "applied"
    assert target.read_bytes() == b"current-c"


def test_partial_requires_explicit_preview_then_apply_acceptance(tmp_path):
    store, manager, partial_id, target = (
        build_partial_restore_with_one_proven_entry(tmp_path)
    )
    record = store.load_checkpoint_record(partial_id)
    record["reviewed_at"] = ""
    record["reviewed_by"] = ""
    record["review_reason"] = ""
    store.write_checkpoint_record(record)
    before = store.load_checkpoint_record(partial_id)
    preview = manager.preview_restore_journal_resolution(partial_id)
    assert preview["status"] == "partial_review_required"
    assert store.load_checkpoint_record(partial_id) == before
    accepted = manager.resolve_restore_journal(
        partial_id,
        expected_record_hash=preview["record_hash"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )
    assert accepted["status"] == "partial"
    assert accepted["reviewed_at"]
    assert target.read_bytes() == b"restored-a"


def test_resolution_rejects_changed_record_without_writing(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    del target, post_state
    preview = manager.preview_restore_journal_resolution(restore_id)
    record = store.load_checkpoint_record(restore_id)
    record["review_reason"] = "changed"
    store.write_checkpoint_record(record)
    with pytest.raises(ValueError, match="record_changed"):
        manager.resolve_restore_journal(
            restore_id,
            expected_record_hash=preview["record_hash"],
            reviewed_by="cli",
            review_reason="explicit_cli_resolution",
        )
    assert store.load_checkpoint_record(restore_id)["review_reason"] == "changed"


def test_resolution_rejects_replaced_workspace_root_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(tmp_path / "metadata")
    manager = RecoveryManager(store, workspace)
    target = workspace / "note.txt"
    target.write_bytes(b"current-after")
    target.chmod(0o640)
    pre = store.write_blob(b"current-after")
    post = store.write_blob(b"restored-before")
    record = new_checkpoint_record(
        "ckpt_root_swap_journal",
        "restore",
        "session",
        "run",
        "turn",
        "",
        str(workspace.resolve()),
    )
    record["status"] = "applying"
    record["restore_provenance"] = {
        "entries": [
            {
                "path": "note.txt",
                "pre_state": {
                    "exists": True,
                    "hash": pre["content_hash"],
                    "blob_ref": pre["blob_ref"],
                    "mode": 0o640,
                },
                "planned_post_state": {
                    "exists": True,
                    "hash": post["content_hash"],
                    "blob_ref": post["blob_ref"],
                    "mode": 0o600,
                },
                "outcome": "pending",
                "reason": "",
                "target_modified": False,
                "actual_post_state": {},
            }
        ]
    }
    store.write_checkpoint_record(record)
    workspace.rename(tmp_path / "workspace-old")
    workspace.mkdir()
    replacement = workspace / "note.txt"
    replacement.write_bytes(b"restored-before")
    replacement.chmod(0o600)
    with pytest.raises(ValueError, match="workspace root changed"):
        manager.preview_restore_journal_resolution(record["checkpoint_id"])


@pytest.mark.parametrize(
    "blocker_kind", ("pending_tool", "invalid_record", "applying", "partial")
)
def test_apply_restore_global_review_guard_blocks_before_plan_and_target(
    tmp_path, blocker_kind, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    seed_unrelated_recovery_blocker(store, tmp_path, blocker_kind)
    manager = RecoveryManager(store, tmp_path)
    calls = []
    monkeypatch.setattr(manager, "_apply_intent", lambda *args: calls.append(args))
    result = manager.apply_restore("ckpt_source")
    assert result["status"] == "blocked"
    assert target.read_bytes() == b"after"
    assert calls == []


def test_capture_blocked_result_never_creates_applying_or_mutates(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    monkeypatch.setattr(
        manager,
        "_capture_restore_intents",
        lambda plan: {
            "status": "blocked",
            "reason": "sensitive_content",
            "intents": [],
        },
    )
    monkeypatch.setattr(
        manager,
        "_write_applying_journal",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("applying journal written")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_apply_all_intents",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("target mutation attempted")
        ),
    )
    result = manager.apply_restore("ckpt_source")
    assert result["status"] == "blocked"
    assert target.read_bytes() == b"after"
    audit = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert audit["status"] == "blocked"
    assert not any(
        item.get("status") == "applying"
        for item in store.list_checkpoint_records(strict=True)
    )
