import io
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tarfile

import pytest

from pico.sandbox_lifecycle import (
    ArchiveValidationError,
    acquire_bundle_lease,
    apply_prune_plan,
    bundle_usage_state,
    build_compatibility_payload,
    build_doctor_payload,
    export_bundle,
    import_bundle,
    plan_prune,
    resume_prune_trash,
    verified_bundle_inventory,
)


def _bundle(root, name, identity):
    path = root / name
    path.mkdir()
    (path / "bin").mkdir()
    (path / "bin" / "tool").write_bytes(identity.encode())
    return path


def _inspect(path):
    marker = path / "bin" / "tool"
    if not marker.exists():
        return {"verified": False, "reason": "marker missing"}
    return {"verified": True, "identity": marker.read_text(), "platform": "linux", "arch": "x86_64"}


def _retirement(replacement="id-current", rollback_window="2026-01-01T00:00:00Z"):
    return {
        "security_reason": "security retirement",
        "replacement": replacement,
        "compatibility_evidence": "artifact://compatibility/id-old",
        "rollback_window": rollback_window,
        "release_note": "release-notes.md#id-old",
    }


def test_inventory_preserves_unknown_bundles_as_refusals(tmp_path):
    _bundle(tmp_path, "one", "id-1")
    (tmp_path / "broken").mkdir()
    (tmp_path / ".trash-old").mkdir()

    inventory = verified_bundle_inventory(tmp_path, inspector=_inspect)

    assert inventory[1] == {
        "arch": "x86_64", "identity": "id-1", "name": "one",
        "path": str(tmp_path / "one"), "platform": "linux", "verified": True,
        "reason": "identity_unknown",
        "device": (tmp_path / "one").stat().st_dev,
        "inode": (tmp_path / "one").stat().st_ino,
    }
    assert inventory[0]["name"] == "broken"
    assert inventory[0]["verified"] is False


def test_prune_dry_run_keeps_current_and_apply_renames_to_trash(tmp_path):
    current = _bundle(tmp_path, "current", "id-current")
    old = _bundle(tmp_path, "old", "id-old")
    inventory = verified_bundle_inventory(tmp_path, inspector=_inspect)
    inventory[1]["retirement"] = _retirement()

    plan = plan_prune(
        inventory,
        pinned_identity="id-current",
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    assert [item["identity"] for item in plan["keep"]] == ["id-current"]
    assert [item["identity"] for item in plan["delete"]] == ["id-old"]
    assert current.exists() and old.exists()

    result = apply_prune_plan(
        plan, trash_root=tmp_path / ".trash", allowed_roots=(tmp_path,)
    )
    assert current.exists() and not old.exists()
    assert not Path(result["trashed"][0]["trash_path"]).exists()


def test_prune_returns_unknown_identity_as_refuse():
    plan = plan_prune(
        [{"path": "/tmp/x", "verified": True}], pinned_identity="known"
    )

    assert plan["delete"] == []
    assert plan["refuse"][0]["decision"] == "refuse"


def test_prune_requires_complete_retirement_and_expired_rollback_window():
    base = {"path": "/tmp/x", "identity": "old", "verified": True}
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)

    missing = plan_prune([base], pinned_identity="current", now=now)
    future = plan_prune(
        [{**base, "retirement": _retirement("current", "2027-01-01T00:00:00Z")}],
        pinned_identity="current",
        now=now,
    )

    assert missing["refuse"][0]["reason"] == "retirement_evidence_invalid"
    assert future["keep"][0]["reason"] == "rollback_window"


def test_resume_prune_trash_is_bounded_and_retries(tmp_path):
    trash = tmp_path / "trash"
    pending = trash / "prune-interrupted"
    pending.mkdir(parents=True)
    (pending / "one").write_text("1")
    (pending / "two").write_text("2")

    first = resume_prune_trash(trash, max_entries=1)
    second = resume_prune_trash(trash, max_entries=10)

    assert first["deleted_entries"] == 1
    assert first["pending_cleanup"] == [str(pending)]
    assert second["pending_cleanup"] == []
    assert not pending.exists()


def test_bundle_lease_persists_current_pico_reference(tmp_path):
    root = tmp_path / "toolchain"
    bundle = root / "bundles" / "bundle-v1"
    bundle.mkdir(parents=True)

    lease = acquire_bundle_lease(root, "bundle-v1")
    state = bundle_usage_state(bundle, now=bundle.stat().st_mtime + 100_000)

    assert lease.read_text(encoding="ascii") == str(os.getpid())
    assert state["active_lease"] is True
    assert state["referenced"] is True


def test_bundle_usage_keeps_unknown_lease_state_fail_closed(tmp_path):
    root = tmp_path / "toolchain"
    bundle = root / "bundles" / "bundle-v1"
    bundle.mkdir(parents=True)
    leases = root / "leases"
    leases.mkdir()
    (leases / "bundle-v1-unknown.lease").write_text("not-a-pid")

    assert bundle_usage_state(bundle)["active_lease"] is True


def test_export_import_round_trip_uses_staging_and_importer(tmp_path):
    source = _bundle(tmp_path, "source", "id-1")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id-1", platform="linux", arch="x86_64")
    called = []

    def importer(staging, manifest):
        called.append((staging.name, manifest["identity"]))
        assert staging.name.startswith(".staging-")

    destination = tmp_path / "installed"
    result = import_bundle(
        archive, destination, expected_platform="linux", expected_arch="x86_64",
        importer=importer,
    )
    assert result["identity"] == "id-1"
    assert (destination / "bin" / "tool").read_text() == "id-1"
    assert called[0][1] == "id-1"


def test_import_rejects_links_traversal_hash_size_and_target_mismatch(tmp_path):
    source = _bundle(tmp_path, "source", "id-1")
    archive = tmp_path / "bundle.tar"
    export_bundle(source, archive, identity="id-1", platform="linux", arch="x86_64")
    raw = bytearray(archive.read_bytes())
    payload_offset = raw.rfind(b"id-1")
    assert payload_offset >= 0
    raw[payload_offset] = ord("X")
    corrupt = tmp_path / "corrupt.tar"
    corrupt.write_bytes(raw)
    with pytest.raises(ArchiveValidationError):
        import_bundle(corrupt, tmp_path / "bad", expected_platform="linux", expected_arch="x86_64")
    with pytest.raises(ArchiveValidationError, match="platform"):
        import_bundle(archive, tmp_path / "bad2", expected_platform="darwin", expected_arch="x86_64")

    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(unsafe, "w") as tf:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ArchiveValidationError, match="unsafe"):
        import_bundle(unsafe, tmp_path / "bad3")


def test_payload_builders_are_pure_structured_data_without_telemetry():
    compatibility = build_compatibility_payload(
        required={"platform": "linux", "arch": "x86_64", "identity": "id-1"},
        actual={"platform": "linux", "arch": "x86_64", "identity": "id-2"},
    )
    assert compatibility["compatible"] is False
    assert compatibility["mismatches"] == {"identity": {"expected": "id-1", "actual": "id-2"}}
    doctor = build_doctor_payload(compatibility=compatibility, inventory=[], checks={"storage": True})
    assert doctor["ok"] is False
    assert "telemetry" not in json.dumps(doctor).lower()
