import json
import os

import pytest

from pony.runtime.legacy import LegacySandboxResumeError, preflight_legacy_sandbox_resume
from pony.state.legacy_artifacts import LegacyArtifactError, LegacyCheckpointReader


def _private_directory(path):
    path.mkdir(parents=True)
    path.chmod(0o700)
    return path


def _sidecar(root, *, session_id="session-1"):
    directory = _private_directory(root / ".pony" / "sandbox_sessions")
    source = root.lstat()
    sandbox_id = "sandbox_" + "a" * 32
    path = directory / f"{sandbox_id}.json"
    path.write_text(
        json.dumps(
            {
                "record_type": "docker_sandbox_session_pointer",
                "format_version": 1,
                "pony_session_id": session_id,
                "sandbox_id": sandbox_id,
                "source_root": str(root),
                "source_device": source.st_dev,
                "source_inode": source.st_ino,
                "state_root": "/retired/state",
                "state_device": 1,
                "state_inode": 2,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_legacy_sandbox_preflight_allows_absent_sidecars_without_creating_state(tmp_path):
    before = tmp_path.stat()

    preflight_legacy_sandbox_resume(tmp_path, "session-1")

    assert not (tmp_path / ".pony").exists()
    assert tmp_path.stat() == before


def test_legacy_sandbox_preflight_rejects_exact_bound_session_without_writing(tmp_path):
    sidecar = _sidecar(tmp_path)
    before = sidecar.read_bytes()

    with pytest.raises(
        LegacySandboxResumeError, match="legacy_sandbox_session_unsupported"
    ):
        preflight_legacy_sandbox_resume(tmp_path, "session-1")

    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "overflow"))
def test_legacy_sandbox_preflight_fails_closed_for_untrusted_sidecars(tmp_path, kind):
    path = _sidecar(tmp_path)
    directory = path.parent
    if kind == "symlink":
        path.unlink()
        path.symlink_to("/dev/null")
    elif kind == "hardlink":
        os.link(path, directory / ("sandbox_" + "b" * 32 + ".json"))
    else:
        for index in range(128):
            copy = directory / f"sandbox_{index:032x}.json"
            copy.write_bytes(path.read_bytes())
            copy.chmod(0o600)

    with pytest.raises(LegacySandboxResumeError, match="sandbox_state_invalid"):
        preflight_legacy_sandbox_resume(tmp_path, "other-session")


@pytest.mark.parametrize(
    "field,value",
    (
        ("format_version", True),
        ("state_root", "relative/state"),
        ("state_device", False),
        ("state_inode", 0),
    ),
)
def test_legacy_sandbox_preflight_rejects_malformed_sidecar_metadata(
    tmp_path, field, value
):
    path = _sidecar(tmp_path, session_id="other-session")
    pointer = json.loads(path.read_text(encoding="utf-8"))
    pointer[field] = value
    path.write_text(json.dumps(pointer), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(LegacySandboxResumeError, match="sandbox_state_invalid"):
        preflight_legacy_sandbox_resume(tmp_path, "session-1")


def test_legacy_checkpoint_reader_projects_safe_fields_without_writing(tmp_path):
    checkpoints = _private_directory(tmp_path / ".pony" / "checkpoints")
    records = _private_directory(checkpoints / "records")
    raw = {
        "record_type": "checkpoint",
        "format_version": 1,
        "checkpoint_id": "checkpoint-1",
        "checkpoint_type": "turn",
        "created_at": "now",
        "status": "",
        "owner_id": "",
        "reviewed_at": "",
        "private_payload": "must not be exposed",
    }
    path = records / "checkpoint-1.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o600)
    before = path.read_bytes()

    reader = LegacyCheckpointReader(tmp_path)

    assert reader.list_checkpoint_records(strict=True) == [
        {
            "checkpoint_id": "checkpoint-1",
            "checkpoint_type": "turn",
            "created_at": "now",
            "status": "",
            "owner_id": "",
            "reviewed_at": "",
        }
    ]
    assert reader.load_checkpoint_record("checkpoint-1") == reader.list_checkpoint_records(
        strict=True
    )[0]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    (
        ("format_version", 99),
        ("checkpoint_type", "unknown"),
        ("status", "pending"),
    ),
)
def test_legacy_checkpoint_reader_rejects_unknown_record_contract(tmp_path, field, value):
    checkpoints = _private_directory(tmp_path / ".pony" / "checkpoints")
    records = _private_directory(checkpoints / "records")
    record = {
        "record_type": "checkpoint",
        "format_version": 1,
        "checkpoint_id": "checkpoint-1",
        "checkpoint_type": "turn",
        "created_at": "now",
        "status": "",
        "owner_id": "",
        "reviewed_at": "",
    }
    record[field] = value
    path = records / "checkpoint-1.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(LegacyArtifactError):
        LegacyCheckpointReader(tmp_path).list_checkpoint_records(strict=True)


def test_legacy_checkpoint_reader_rejects_non_object_json(tmp_path):
    checkpoints = _private_directory(tmp_path / ".pony" / "checkpoints")
    records = _private_directory(checkpoints / "records")
    path = records / "checkpoint-1.json"
    path.write_text("[]", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(LegacyArtifactError):
        LegacyCheckpointReader(tmp_path).list_checkpoint_records(strict=True)


@pytest.mark.parametrize(
    ("directory_name", "record", "missing"),
    (
        (
            "records",
            {
                "record_type": "checkpoint",
                "format_version": 1,
                "checkpoint_id": "checkpoint-1",
                "checkpoint_type": "turn",
                "created_at": "now",
                "status": "",
                "owner_id": "",
                "reviewed_at": "",
            },
            "created_at",
        ),
        (
            "tool_changes",
            {
                "record_type": "tool_change",
                "format_version": 2,
                "tool_change_id": "change-1",
                "status": "pending",
                "owner_id": "",
                "tool_name": "write_file",
                "effect_class": "workspace_write",
                "started_at": "now",
                "reviewed_at": "",
            },
            "tool_name",
        ),
    ),
)
def test_legacy_checkpoint_reader_rejects_truncated_projected_record(
    tmp_path, directory_name, record, missing
):
    checkpoints = _private_directory(tmp_path / ".pony" / "checkpoints")
    directory = _private_directory(checkpoints / directory_name)
    record.pop(missing)
    record_id = record.get("checkpoint_id", record.get("tool_change_id", "change-1"))
    path = directory / f"{record_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    reader = LegacyCheckpointReader(tmp_path)

    with pytest.raises(LegacyArtifactError):
        (
            reader.list_checkpoint_records(strict=True)
            if directory_name == "records"
            else reader.list_tool_change_records(strict=True)
        )


def test_legacy_checkpoint_reader_rejects_checkpoint_root_replacement(tmp_path):
    checkpoints = _private_directory(tmp_path / ".pony" / "checkpoints")
    records = _private_directory(checkpoints / "records")
    path = records / "checkpoint-1.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    reader = LegacyCheckpointReader(tmp_path)
    replacement = tmp_path / "replacement"
    _private_directory(replacement / "records")
    checkpoints.rename(tmp_path / "retired-checkpoints")
    replacement.rename(checkpoints)

    with pytest.raises(LegacyArtifactError):
        reader.list_checkpoint_records(strict=True)


def test_legacy_checkpoint_reader_rejects_unsafe_record_without_writing(tmp_path):
    checkpoints = _private_directory(tmp_path / ".pony" / "checkpoints")
    records = _private_directory(checkpoints / "records")
    path = records / "checkpoint-1.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    before = path.read_bytes()

    with pytest.raises(LegacyArtifactError):
        LegacyCheckpointReader(tmp_path).list_checkpoint_records(strict=True)

    assert path.read_bytes() == before
