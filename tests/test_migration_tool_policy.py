import json
import subprocess
from types import SimpleNamespace

import pytest

from pony.state.checkpoint_store import CheckpointStore
from pony.cli.migration import _build_tool_changes, _identity, _migration
from pony.recovery.migration import ABSENT, CANDIDATE_READY, PREPARING
from pony.recovery.models import new_checkpoint_record, new_tool_change_record
from pony.tools.change_converter import convert_tool_change_v1
from pony.workspace.context import WorkspaceContext


def _file_entry(blob_ref, *, source_id="tc_1", path="note.txt"):
    return {
        "path": path,
        "change_kind": "created",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": False,
        "before_blob_ref": "",
        "before_hash": "",
        "before_mode": None,
        "after_exists": True,
        "after_blob_ref": blob_ref,
        "after_hash": blob_ref,
        "after_mode": 0o644,
        "expected_current_hash": blob_ref,
        "source_tool_change_ids": [source_id] if source_id else [],
    }


def _legacy_tool_change(status="pending"):
    record = new_tool_change_record(
        "tc_1", "ckpt_1", "turn_1", "write_file", "workspace_write"
    )
    record["format_version"] = 1
    record["status"] = status
    record.pop("policy")
    record.pop("sandbox")
    return record


def _migration_graph(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"after\n")
    tool_change = new_tool_change_record(
        "tc_1", "ckpt_1", "turn_1", "write_file", "workspace_write"
    )
    tool_change["status"] = "finalized"
    tool_change["file_entries"] = [_file_entry(blob["blob_ref"])]
    store.write_tool_change_record(tool_change)

    checkpoint = new_checkpoint_record(
        "ckpt_1", "turn", "session_1", "run_1", "turn_1", "", str(tmp_path)
    )
    checkpoint["tool_change_ids"] = ["tc_1"]
    checkpoint["file_entries"] = [dict(tool_change["file_entries"][0])]
    store.write_checkpoint_record(checkpoint)

    legacy = dict(tool_change)
    legacy["format_version"] = 1
    legacy.pop("policy")
    legacy.pop("sandbox")
    tool_path = store.tool_changes_dir / "tc_1.json"
    tool_path.write_text(json.dumps(legacy), encoding="utf-8")
    workspace = SimpleNamespace(repo_root=str(tmp_path), status="clean")
    migration, _ = _migration(workspace, "tool_changes")
    return store, checkpoint, legacy, tool_path, blob["blob_ref"], migration


def _apply(migration):
    return migration.apply(
        lambda source, candidate: _build_tool_changes(source, candidate)
    )


@pytest.mark.parametrize(
    "status", ("pending", "finalized", "error", "partial_success", "interrupted")
)
def test_tool_change_migration_marks_all_valid_v1_evidence_incomplete(status):
    record = _legacy_tool_change(status)
    record["approval"] = {"outcome": "approved"}
    converted = convert_tool_change_v1(record)

    assert converted["format_version"] == 2
    assert converted["status"] == "legacy_migrated"
    assert converted["policy"]["decision"] == "allow"
    assert converted["policy"]["evidence_complete"] is False
    assert converted["approval"] == record["approval"]


@pytest.mark.parametrize(
    "corruption", ("invalid_status", "missing_field", "extra_field")
)
def test_tool_change_migration_rejects_invalid_v1_without_washing_status(corruption):
    record = _legacy_tool_change()
    if corruption == "invalid_status":
        record["status"] = "corrupt"
    elif corruption == "missing_field":
        record.pop("approval")
    else:
        record["unexpected"] = True

    with pytest.raises(ValueError):
        convert_tool_change_v1(record)


def test_tool_change_migration_rejects_duplicate_record_keys_before_rename(
    tmp_path, monkeypatch
):
    _, _, legacy, tool_path, _, migration = _migration_graph(tmp_path)
    payload = json.dumps(legacy)
    tool_path.write_text(payload[:-1] + ',"status":"finalized"}', encoding="utf-8")
    monkeypatch.setattr(
        migration,
        "_rename",
        lambda *args: pytest.fail("duplicate JSON reached migration rename"),
    )

    with pytest.raises(ValueError, match="duplicate"):
        _apply(migration)

    assert migration.status()["state"] == PREPARING


def test_tool_change_migration_validates_complete_reference_graph(tmp_path):
    store, checkpoint, _, tool_path, blob_ref, migration = _migration_graph(tmp_path)
    parent = new_checkpoint_record(
        "ckpt_parent", "turn", "session_1", "run_1", "turn_0", "", str(tmp_path)
    )
    store.write_checkpoint_record(parent)
    pending = new_tool_change_record(
        "tc_pending", "ckpt_parent", "turn_1", "write_file", "workspace_write"
    )
    store.write_tool_change_record(pending)
    pending["format_version"] = 1
    pending.pop("policy")
    pending.pop("sandbox")
    (store.tool_changes_dir / "tc_pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    checkpoint["missing_tool_change_ids"] = [
        "tc_pending",
        "tc_explicitly_missing",
    ]
    checkpoint["integrity_errors"] = [
        {
            "reason": "incomplete_tool_change_history",
            "tool_change_ids": ["tc_pending", "tc_explicitly_missing"],
        }
    ]
    store.write_checkpoint_record(checkpoint)

    assert _apply(migration) == ABSENT
    migrated = json.loads(tool_path.read_text(encoding="utf-8"))
    assert migrated["format_version"] == 2
    assert migrated["status"] == "legacy_migrated"
    assert store.read_blob(blob_ref) == b"after\n"


@pytest.mark.parametrize("committed", (False, True))
def test_migration_identity_binds_repo_inode_and_head_without_path(tmp_path, committed):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    expected_commit = ""
    if committed:
        (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Pony Test",
                "-c",
                "user.email=pony@example.invalid",
                "commit",
                "-q",
                "-m",
                "initial",
            ],
            check=True,
        )
        expected_commit = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    workspace = WorkspaceContext.build(tmp_path)
    identity = _identity(workspace)
    root_info = tmp_path.stat()

    assert identity["repo_device"] == root_info.st_dev
    assert identity["repo_inode"] == root_info.st_ino
    assert identity["repo_commit"] == expected_commit
    assert str(tmp_path) not in json.dumps(identity)


@pytest.mark.parametrize(
    "corruption",
    (
        "dangling_tool_change",
        "source_mismatch",
        "checkpoint_history_mismatch",
        "declared_missing_present",
        "missing_blob",
        "blob_hash_mismatch",
        "file_hash_mismatch",
    ),
)
def test_tool_change_migration_rejects_invalid_graph_before_cutover(
    tmp_path, monkeypatch, corruption
):
    store, checkpoint, legacy, tool_path, blob_ref, migration = _migration_graph(
        tmp_path
    )
    if corruption == "dangling_tool_change":
        checkpoint["tool_change_ids"] = ["tc_missing"]
        checkpoint["file_entries"][0]["source_tool_change_ids"] = ["tc_missing"]
        store.write_checkpoint_record(checkpoint)
    elif corruption == "source_mismatch":
        checkpoint["file_entries"][0]["source_tool_change_ids"] = ["tc_other"]
        store.write_checkpoint_record(checkpoint)
    elif corruption == "checkpoint_history_mismatch":
        checkpoint["file_entries"][0]["path"] = "other.txt"
        store.write_checkpoint_record(checkpoint)
    elif corruption == "declared_missing_present":
        other = new_tool_change_record(
            "tc_other", "", "turn_1", "write_file", "workspace_write"
        )
        other["status"] = "finalized"
        store.write_tool_change_record(other)
        checkpoint["missing_tool_change_ids"] = ["tc_other"]
        store.write_checkpoint_record(checkpoint)
    elif corruption == "missing_blob":
        store._blob_path(blob_ref).unlink()
    elif corruption == "blob_hash_mismatch":
        store._blob_path(blob_ref).write_bytes(b"tampered\n")
    else:
        legacy["file_entries"][0]["after_hash"] = "0" * 64
        tool_path.write_text(json.dumps(legacy), encoding="utf-8")

    renames = []
    monkeypatch.setattr(
        migration,
        "_rename",
        lambda source, destination: renames.append((source, destination)),
    )

    with pytest.raises((OSError, ValueError)):
        _apply(migration)

    assert renames == []
    expected_state = (
        PREPARING if corruption == "file_hash_mismatch" else CANDIDATE_READY
    )
    assert migration.status()["state"] == expected_state
    assert json.loads(tool_path.read_text(encoding="utf-8"))["format_version"] == 1


@pytest.mark.parametrize("location", ("file_entries", "restore_provenance"))
@pytest.mark.parametrize("corruption", ("missing", "hash_mismatch"))
def test_tool_change_migration_validates_checkpoint_blob_refs_before_cutover(
    tmp_path, monkeypatch, location, corruption
):
    store, _, _, _, _, migration = _migration_graph(tmp_path)
    blob = store.write_blob(b"checkpoint-only\n")
    checkpoint_type = "restore" if location == "restore_provenance" else "manual"
    checkpoint = new_checkpoint_record(
        "ckpt_extra", checkpoint_type, "session_1", "run_1", "turn_2", "", str(tmp_path)
    )
    if location == "file_entries":
        checkpoint["file_entries"] = [
            _file_entry(blob["blob_ref"], source_id="", path="extra.txt")
        ]
    else:
        checkpoint["status"] = "applying"
        checkpoint["restore_provenance"] = {
            "entries": [{"pre_state": {"blob_ref": blob["blob_ref"]}}]
        }
    store.write_checkpoint_record(checkpoint)
    if corruption == "missing":
        store._blob_path(blob["blob_ref"]).unlink()
    else:
        store._blob_path(blob["blob_ref"]).write_bytes(b"tampered\n")

    renames = []
    monkeypatch.setattr(
        migration,
        "_rename",
        lambda source, destination: renames.append((source, destination)),
    )

    with pytest.raises((OSError, ValueError)):
        _apply(migration)

    assert renames == []
