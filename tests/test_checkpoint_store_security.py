import hashlib
import json
import os
import stat

import pytest

from pony.state import checkpoint_store as checkpoint_store_module
from pony.state.checkpoint_store import CheckpointStore
from pony.recovery.models import new_checkpoint_record, new_tool_change_record
from pony.agent.verification import new_verification_record


def _checkpoint(tmp_path, checkpoint_id="ckpt_safe"):
    return new_checkpoint_record(
        checkpoint_id,
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )


def test_load_checkpoint_does_not_reopen_path_after_validation(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(_checkpoint(tmp_path))
    replacement = _checkpoint(tmp_path)
    replacement["session_id"] = "replacement"
    real_read = checkpoint_store_module.read_private_bytes
    swapped = False

    def swap_after_read(path, **kwargs):
        nonlocal swapped
        result = real_read(path, **kwargs)
        if not swapped and path == store._record_path("ckpt_safe"):
            swapped = True
            path.unlink()
            path.write_text(json.dumps(replacement), encoding="utf-8")
        return result

    monkeypatch.setattr(checkpoint_store_module, "read_private_bytes", swap_after_read)

    loaded = store.load_checkpoint_record("ckpt_safe")

    assert swapped is True
    assert loaded["session_id"] == "session"


def test_blob_read_is_bounded(tmp_path):
    store = CheckpointStore(tmp_path)
    blob_ref = "a" * 64
    path = store._blob_path(blob_ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (store.MAX_BLOB_BYTES + 1))

    with pytest.raises(ValueError, match="blob_too_large"):
        store.read_blob(blob_ref)


def test_blob_write_is_bounded(tmp_path):
    store = CheckpointStore(tmp_path)
    store.MAX_BLOB_BYTES = 8

    with pytest.raises(ValueError, match="blob_too_large"):
        store.write_blob(b"x" * 9)

    assert not any(path.is_file() for path in store.blobs_dir.rglob("*"))


def test_checkpoint_record_write_is_bounded(tmp_path):
    store = CheckpointStore(tmp_path)
    store.MAX_RECORD_BYTES = 256
    record = _checkpoint(tmp_path, "ckpt_oversized")
    record["review_reason"] = "x" * 1024

    with pytest.raises(ValueError, match="private file too large"):
        store.write_checkpoint_record(record)

    assert not store._record_path("ckpt_oversized").exists()


def test_checkpoint_record_rejects_oversized_existing_artifact_before_backup(tmp_path):
    store = CheckpointStore(tmp_path)
    store.MAX_RECORD_BYTES = 256
    record = _checkpoint(tmp_path, "ckpt_existing_oversized")
    path = store._record_path("ckpt_existing_oversized")
    path.write_bytes(b"x" * 257)

    with pytest.raises(ValueError, match="private file too large"):
        store.write_checkpoint_record(record)

    assert path.read_bytes() == b"x" * 257
    assert not list(store.records_dir.glob(".*.bak"))


@pytest.mark.parametrize("record_id", ["", ".", "..", "../escape", "a/b", "a\\b"])
def test_record_ids_reject_unsafe_names(tmp_path, record_id):
    store = CheckpointStore(tmp_path)
    record = _checkpoint(tmp_path)
    record["checkpoint_id"] = record_id
    with pytest.raises(ValueError):
        store.write_checkpoint_record(record)


@pytest.mark.parametrize("blob_ref", ["abc", "A" * 64, "g" * 64, "../" + "a" * 64])
def test_blob_refs_require_lowercase_sha256(tmp_path, blob_ref):
    store = CheckpointStore(tmp_path)
    with pytest.raises(ValueError):
        store.read_blob(blob_ref)


def test_read_blob_rejects_hash_mismatch(tmp_path):
    store = CheckpointStore(tmp_path)
    info = store.write_blob(b"trusted")
    store._blob_path(info["blob_ref"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="blob_hash_mismatch"):
        store.read_blob(info["blob_ref"])


def test_write_blob_rejects_existing_hash_named_corrupt_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    data = b"trusted"
    blob_ref = hashlib.sha256(data).hexdigest()
    path = store._blob_path(blob_ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="blob_hash_mismatch"):
        store.write_blob(data)


def test_list_checkpoint_records_is_bounded_and_schema_validating(tmp_path):
    store = CheckpointStore(tmp_path)
    (store.records_dir / "broken.json").write_bytes(
        b"x" * (store.MAX_RECORD_BYTES + 1)
    )

    with pytest.raises(ValueError, match="invalid_record"):
        store.list_checkpoint_records(strict=True)
    [invalid] = store.list_checkpoint_records(strict=False)
    assert invalid["status"] == "invalid_record"
    assert invalid["opaque_id"].startswith("invalid_")


def test_store_rejects_symlinked_records_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / ".pony" / "checkpoints"
    root.mkdir(parents=True)
    os.symlink(outside, root / "records")
    with pytest.raises(ValueError, match="symlink"):
        CheckpointStore(tmp_path)


def test_store_layout_uses_private_modes(tmp_path):
    store = CheckpointStore(tmp_path)
    path = store.write_checkpoint_record(_checkpoint(tmp_path, "ckpt_private"))
    blob = store.write_blob(b"private")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store._blob_path(blob["blob_ref"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("record_type", "wrong", "unsupported_record_type"),
        ("format_version", True, "unsupported_format"),
        ("checkpoint_type", "unknown", "invalid_checkpoint_type"),
        ("status", "mystery", "invalid_status"),
    ],
)
def test_checkpoint_record_rejects_invalid_schema_type_and_status(
    tmp_path, field, value, code
):
    store = CheckpointStore(tmp_path)
    record = _checkpoint(tmp_path, "ckpt_invalid")
    record[field] = value
    with pytest.raises(ValueError, match=code):
        store.write_checkpoint_record(record)


def test_load_rejects_internal_id_mismatch(tmp_path):
    store = CheckpointStore(tmp_path)
    path = store.records_dir / "ckpt_requested.json"
    path.write_text(json.dumps(_checkpoint(tmp_path, "ckpt_internal")), encoding="utf-8")
    with pytest.raises(ValueError, match="internal_id_mismatch"):
        store.load_checkpoint_record("ckpt_requested")


def test_tool_change_record_requires_internal_id(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_tool_change_record(
        "tc_internal", "", "turn", "write_file", "workspace_write", "owner"
    )
    path = store.tool_changes_dir / "tc_requested.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="internal_id_mismatch"):
        store.load_tool_change_record("tc_requested")


def test_record_schema_rejects_wrong_container_and_scalar_types(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint = _checkpoint(tmp_path, "ckpt_shape")
    checkpoint["file_entries"] = {}
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.write_checkpoint_record(checkpoint)
    tool_change = new_tool_change_record(
        "tc_shape", "", "turn", "write_file", "workspace_write", "owner"
    )
    tool_change["owner_id"] = []
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.write_tool_change_record(tool_change)


def test_prepared_existing_entry_allows_mode_only_when_snapshot_unavailable(tmp_path):
    store = CheckpointStore(tmp_path)
    tool_change = new_tool_change_record(
        "tc_mode_only", "", "turn", "write_file", "workspace_write", "owner"
    )
    tool_change["prepared_file_entries"] = [{
        "path": "large.bin",
        "before_exists": True,
        "before_blob_ref": "",
        "before_hash": "",
        "before_mode": 0o600,
    }]

    store.write_tool_change_record(tool_change)

    assert store.load_tool_change_record("tc_mode_only")["prepared_file_entries"] == tool_change["prepared_file_entries"]


def test_json_is_redacted_but_blob_bytes_remain_exact(tmp_path):
    sentinel = "sk-checkpoint-redactor-sentinel"

    def redact(value):
        return json.loads(json.dumps(value).replace(sentinel, "<redacted>"))

    store = CheckpointStore(tmp_path, redactor=redact)
    record = _checkpoint(tmp_path, "ckpt_redacted")
    record["verification_evidence"] = [new_verification_record(
        argv=["pytest"],
        risk_class="read_only",
        runner_executed=True,
        execution_mode="argv",
        exit_code=0,
        stdout=sentinel,
        stderr="",
    )]
    path = store.write_checkpoint_record(record)
    blob = store.write_blob(sentinel.encode())
    assert sentinel.encode() not in path.read_bytes()
    assert store.read_blob(blob["blob_ref"]) == sentinel.encode()
    store.set_redactor(lambda value: value)
    loaded = store.load_checkpoint_record("ckpt_redacted")
    assert loaded["verification_evidence"][0]["stdout_tail"] == "<redacted>"


def test_current_records_reject_missing_required_fields(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint = _checkpoint(tmp_path, "ckpt_legacy")
    for key in (
        "status", "owner_id", "reviewed_at", "review_reason", "reviewed_by",
        "integrity_errors",
    ):
        checkpoint.pop(key, None)
    (store.records_dir / "ckpt_legacy.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )
    tool = new_tool_change_record(
        "tc_legacy", "", "turn", "write_file", "workspace_write", "owner"
    )
    for key in (
        "prepared_file_entries", "recovery_context", "reviewed_at",
        "review_reason", "reviewed_by",
    ):
        tool.pop(key, None)
    (store.tool_changes_dir / "tc_legacy.json").write_text(json.dumps(tool), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_record_shape|invalid_status"):
        store.load_checkpoint_record("ckpt_legacy")
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.load_tool_change_record("tc_legacy")


def test_restore_without_status_is_rejected(tmp_path):
    store = CheckpointStore(tmp_path)
    record = _checkpoint(tmp_path, "ckpt_restore_legacy")
    record["checkpoint_type"] = "restore"
    record.pop("status", None)
    (store.records_dir / "ckpt_restore_legacy.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.load_checkpoint_record("ckpt_restore_legacy")


def test_additive_defaulting_never_overwrites_present_wrong_type(tmp_path):
    store = CheckpointStore(tmp_path)
    record = _checkpoint(tmp_path, "ckpt_wrong_default")
    record["integrity_errors"] = {}
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.write_checkpoint_record(record)
