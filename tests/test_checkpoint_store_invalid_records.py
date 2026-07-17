import hashlib
import json
import os
import stat

import pytest

from pico.state.checkpoint_store import CheckpointStore


def test_strict_enumeration_rejects_malformed_record(tmp_path):
    store = CheckpointStore(tmp_path)
    (store.records_dir / "ckpt_broken.json").write_bytes(b"{broken")
    with pytest.raises(ValueError, match="invalid_record"):
        store.list_checkpoint_records(strict=True)


def test_inspection_returns_opaque_invalid_without_raw_name_or_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{broken-secret-value"
    filename = "github_pat_secret_filename.json"
    (store.tool_changes_dir / filename).write_bytes(raw)
    identity = b"tool_change\0tool_changes/" + filename.encode() + b"\0" + raw

    records = store.list_tool_change_records(strict=False)

    assert records == [
        {
            "opaque_id": "invalid_" + hashlib.sha256(identity).hexdigest(),
            "record_kind": "tool_change",
            "status": "invalid_record",
            "raw_hash": hashlib.sha256(raw).hexdigest(),
            "quarantinable": True,
        }
    ]
    rendered = json.dumps(records)
    assert "broken-secret-value" not in rendered
    assert "github_pat_secret_filename" not in rendered


def test_quarantine_preserves_exact_private_raw_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{invalid-evidence"
    source = store.tool_changes_dir / "secret-token-filename.json"
    source.write_bytes(raw)
    [preview] = store.list_tool_change_records(strict=False)

    result = store.quarantine_invalid_record(
        preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
    )

    raw_path = store.root / result["quarantine_raw_path"]
    metadata_path = store.root / result["quarantine_metadata_path"]
    assert raw_path.read_bytes() == raw
    assert stat.S_IMODE(raw_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert not source.exists()
    inspected = store.list_quarantined_records()
    assert inspected[0]["opaque_id"] == preview["opaque_id"]
    assert "secret-token-filename" not in json.dumps(inspected)


def test_quarantine_reenumerates_and_rejects_replaced_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    source = store.records_dir / "secret-filename.json"
    source.write_bytes(b"{first-invalid-bytes")
    [preview] = store.list_checkpoint_records(strict=False)
    source.write_bytes(b"{replacement-invalid-bytes")

    with pytest.raises(ValueError, match="invalid_record_changed"):
        store.quarantine_invalid_record(
            preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
        )

    assert source.read_bytes() == b"{replacement-invalid-bytes"
    assert store.list_quarantined_records() == []


def test_identical_invalid_bytes_at_two_paths_have_distinct_ids(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{same-invalid-bytes"
    (store.tool_changes_dir / "first.json").write_bytes(raw)
    (store.tool_changes_dir / "second.json").write_bytes(raw)
    previews = store.list_tool_change_records(strict=False)
    assert len({item["opaque_id"] for item in previews}) == 2


def test_non_regular_records_are_opaque_and_quarantinable_without_following(tmp_path):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-evidence"
    outside.write_bytes(b"must-not-be-read")
    linked = store.records_dir / "linked.json"
    linked.symlink_to(outside)

    [preview] = store.list_checkpoint_records(strict=False)
    assert preview["quarantinable"] is True
    assert "must-not-be-read" not in json.dumps(preview)
    result = store.quarantine_invalid_record(
        preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
    )
    evidence = store.root / result["quarantine_evidence_path"]
    assert not os.path.lexists(linked)
    assert evidence.is_symlink()
    assert os.readlink(evidence) == str(outside)
    assert outside.read_bytes() == b"must-not-be-read"


def test_fifo_record_listing_and_quarantine_never_blocks(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    store = CheckpointStore(tmp_path)
    fifo = store.tool_changes_dir / "blocked.json"
    os.mkfifo(fifo, 0o600)

    [preview] = store.list_tool_change_records(strict=False)
    result = store.quarantine_invalid_record(
        preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
    )

    evidence = store.root / result["quarantine_evidence_path"]
    assert not os.path.lexists(fifo)
    assert stat.S_ISFIFO(evidence.lstat().st_mode)


def test_regular_to_fifo_swap_at_open_is_nonblocking(tmp_path, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    from pico.state import checkpoint_store as checkpoint_store_module

    store = CheckpointStore(tmp_path)
    source = store.records_dir / "raced.json"
    source.write_bytes(b"{invalid")
    real_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            os.fspath(path) == source.name
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            swapped = True
            assert flags & getattr(os, "O_NONBLOCK", 0)
            source.unlink()
            os.mkfifo(source, 0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checkpoint_store_module.os, "open", racing_open)

    [preview] = store.list_checkpoint_records(strict=False)
    assert swapped is True
    assert preview["status"] == "invalid_record"


def test_quarantine_regular_evidence_reopen_is_nonblocking_and_inode_checked(
    tmp_path, monkeypatch
):
    from pico.state import checkpoint_store as checkpoint_store_module

    store = CheckpointStore(tmp_path)
    source = store.records_dir / "broken.json"
    source.write_bytes(b"{invalid")
    [preview] = store.list_checkpoint_records(strict=False)
    real_open = os.open
    raced = False

    def swap_raw_to_fifo(path, flags, *args, **kwargs):
        nonlocal raced
        if str(path).endswith(".raw") and kwargs.get("dir_fd") is not None:
            raced = True
            assert flags & getattr(os, "O_NONBLOCK", 0)
            os.unlink(path, dir_fd=kwargs["dir_fd"])
            os.mkfifo(path, 0o600, dir_fd=kwargs["dir_fd"])
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checkpoint_store_module.os, "open", swap_raw_to_fifo)

    with pytest.raises((OSError, ValueError)):
        store.quarantine_invalid_record(
            preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
        )
    assert raced is True


def test_quarantine_rechecks_exact_regular_bytes_at_move_boundary(
    tmp_path, monkeypatch
):
    from pico.state import checkpoint_store as checkpoint_store_module

    store = CheckpointStore(tmp_path)
    source = store.records_dir / "broken.json"
    source.write_bytes(b"{first-invalid")
    [preview] = store.list_checkpoint_records(strict=False)
    real_rename = os.rename

    def mutate_then_rename(source_name, destination_name, **kwargs):
        if source_name == source.name:
            descriptor = os.open(
                source_name,
                os.O_WRONLY | os.O_TRUNC,
                dir_fd=kwargs["src_dir_fd"],
            )
            try:
                os.write(descriptor, b"{changed-invalid")
            finally:
                os.close(descriptor)
        return real_rename(source_name, destination_name, **kwargs)

    monkeypatch.setattr(checkpoint_store_module.os, "rename", mutate_then_rename)

    with pytest.raises(ValueError, match="invalid_record_changed"):
        store.quarantine_invalid_record(
            preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
        )
    assert store.list_quarantined_records() == []


def test_strict_invalid_record_error_chain_does_not_retain_raw_secret(tmp_path):
    store = CheckpointStore(tmp_path)
    secret = "github_pat_STRICT_SECRET_123456789"
    (store.records_dir / "broken.json").write_text(
        "{" + secret, encoding="utf-8"
    )

    with pytest.raises(ValueError, match="invalid_record") as exc_info:
        store.list_checkpoint_records(strict=True)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_quarantine_listing_whitelists_metadata_and_skips_wrong_types(tmp_path):
    store = CheckpointStore(tmp_path)
    opaque = "invalid_" + "a" * 64
    base = {
        "opaque_id": opaque,
        "record_kind": "checkpoint",
        "status": "quarantined",
        "evidence_kind": "raw",
        "raw_hash": "b" * 64,
        "quarantined_at": "2026-07-11T00:00:00+00:00",
        "quarantine_metadata_path": f"quarantine/checkpoint/{opaque}.json",
        "quarantine_raw_path": f"quarantine/checkpoint/{opaque}.raw",
        "raw_secret": "must-not-leak",
    }
    (store.quarantine_checkpoint_dir / f"{opaque}.json").write_text(
        json.dumps(base), encoding="utf-8"
    )
    (store.quarantine_checkpoint_dir / "invalid_bad.json").write_text(
        json.dumps({**base, "opaque_id": None}), encoding="utf-8"
    )

    records = store.list_quarantined_records()

    assert len(records) == 1
    assert "raw_secret" not in records[0]
    assert "must-not-leak" not in json.dumps(records)
