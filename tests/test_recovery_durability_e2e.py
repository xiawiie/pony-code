import json
import os

import pytest

from pony.state.checkpoint_store import CheckpointStore
from pony.cli.app import main
from pony.recovery.checkpoint_writer import RecoveryCheckpointWriter
from pony.recovery.manager import RecoveryManager
from pony.recovery.models import new_checkpoint_record


def _source_checkpoint(store, root, names=("one.txt",)):
    entries = []
    targets = []
    for index, name in enumerate(names):
        before_bytes = f"before-{index}".encode()
        after_bytes = f"after-{index}".encode()
        before = store.write_blob(before_bytes)
        after = store.write_blob(after_bytes)
        target = root / name
        target.write_bytes(after_bytes)
        target.chmod(0o640)
        targets.append(target)
        entries.append(
            {
                "path": name,
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
        )
    record = new_checkpoint_record(
        "ckpt_source",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(root.resolve()),
    )
    record["file_entries"] = entries
    store.write_checkpoint_record(record)
    return targets


def _manager(store, root):
    return RecoveryManager(
        store,
        root,
        checkpoint_writer=RecoveryCheckpointWriter(store, root),
    )


def _applying_journal(store):
    records = [
        record
        for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
        and record.get("status") == "applying"
    ]
    assert len(records) == 1
    return records[0]


def test_replace_then_crash_before_outcome_reconciles_applied_unconfirmed(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _source_checkpoint(store, tmp_path)[0]
    manager = _manager(store, tmp_path)
    monkeypatch.setattr(
        manager,
        "_write_intent_outcome",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        manager.apply_restore("ckpt_source")

    journal = _applying_journal(store)
    assert target.read_bytes() == b"before-0"
    preview = manager.preview_restore_journal_resolution(journal["checkpoint_id"])
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"
    assert store.load_checkpoint_record(journal["checkpoint_id"]) == journal


def test_outer_parent_fsync_failure_uses_proven_post_state(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _source_checkpoint(store, tmp_path)[0]
    manager = _manager(store, tmp_path)
    monkeypatch.setattr(
        manager,
        "_fsync_target_parent",
        lambda path: (_ for _ in ()).throw(OSError("outer fsync failed")),
    )

    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])

    assert target.read_bytes() == b"before-0"
    assert result["status"] == "applied"
    assert journal["status"] == "applied"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "applied"


def test_second_file_failure_records_partial_and_proven_undo(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    targets = _source_checkpoint(
        store,
        tmp_path,
        names=("one.txt", "two.txt"),
    )
    manager = _manager(store, tmp_path)
    real_apply = manager._apply_intent
    calls = {"count": 0}

    def fail_second(restore_checkpoint_id, intent):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("second target failed")
        return real_apply(restore_checkpoint_id, intent)

    monkeypatch.setattr(manager, "_apply_intent", fail_second)
    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])

    assert result["status"] == "partial"
    assert targets[0].read_bytes() == b"before-0"
    assert targets[1].read_bytes() == b"after-1"
    assert len(journal["file_entries"]) == 1
    review_preview = manager.preview_restore(journal["checkpoint_id"])
    assert review_preview["status"] == "review_required"
    assert any(
        entry["reason"] == "partial_review_required"
        for entry in review_preview["entries"]
    )
    blocked = manager.apply_restore(journal["checkpoint_id"])
    assert blocked["status"] == "blocked"
    resolution = manager.preview_restore_journal_resolution(
        journal["checkpoint_id"]
    )
    manager.resolve_restore_journal(
        journal["checkpoint_id"],
        expected_record_hash=resolution["record_hash"],
        reviewed_by="test",
        review_reason="durability_e2e",
    )
    undo = manager.apply_restore(journal["checkpoint_id"])
    assert undo["status"] == "applied"
    assert targets[0].read_bytes() == b"after-0"


def test_mode_round_trip_survives_restore_and_undo(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode assertion")
    store = CheckpointStore(tmp_path)
    target = _source_checkpoint(store, tmp_path)[0]
    manager = _manager(store, tmp_path)

    restored = manager.apply_restore("ckpt_source")
    assert target.stat().st_mode & 0o777 == 0o600
    manager.apply_restore(restored["restore_checkpoint_id"])
    assert target.stat().st_mode & 0o777 == 0o640


def test_invalid_mutation_record_is_previewed_then_privately_quarantined(
    tmp_path, capsys
):
    store = CheckpointStore(tmp_path)
    raw = b"{private-invalid-evidence"
    source = store.tool_changes_dir / "tc_invalid.json"
    source.write_bytes(raw)

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--format",
                "json",
                "checkpoints",
                "pending",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    invalid_records = payload["data"]["invalid_records"]
    assert len(invalid_records) == 1
    assert invalid_records[0]["status"] == "invalid_record"
    assert "private-invalid-evidence" not in json.dumps(payload)
    invalid_id = invalid_records[0]["opaque_id"]
    assert invalid_id.startswith("invalid_")
    assert "tc_invalid" not in json.dumps(invalid_records)

    store.quarantine_invalid_record(
        invalid_id,
        expected_raw_hash=invalid_records[0]["raw_hash"],
    )
    quarantined = store.list_quarantined_records()
    assert len(quarantined) == 1
    assert quarantined[0]["raw_hash"]
    assert raw not in json.dumps(quarantined).encode()
    assert not source.exists()
    raw_paths = list((store.root / "quarantine").rglob("*.raw"))
    assert len(raw_paths) == 1
    assert raw_paths[0].read_bytes() == raw
    if os.name == "posix":
        assert raw_paths[0].stat().st_mode & 0o777 == 0o600
