"""Bounded, read-only inspection of retired recovery artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from pony.security.private_files import private_directory_identity, read_private_bytes


MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_SIDECAR_BYTES = 8 * 1024 * 1024
MAX_SIDECARS = 128
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SANDBOX_ID = re.compile(r"sandbox_[0-9a-f]{32}\Z")
_CHECKPOINT_TYPES = {"turn", "restore", "manual"}
_RESTORE_STATUSES = {"applying", "applied", "blocked", "failed", "partial", "noop"}
_TOOL_CHANGE_STATUSES = {
    "pending",
    "finalized",
    "error",
    "partial_success",
    "interrupted",
    "legacy_migrated",
}


class LegacyArtifactError(ValueError):
    """A retired artifact cannot be safely inspected."""

    def __init__(self, code="legacy_artifact_invalid"):
        self.code = code
        super().__init__(code)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise LegacyArtifactError()
        value[key] = item
    return value


def _directory(path, *, missing_ok=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LegacyArtifactError() from None
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    try:
        identity = private_directory_identity(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LegacyArtifactError() from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o700
        or identity != (info.st_dev, info.st_ino)
    ):
        raise LegacyArtifactError()
    return path, identity


def _read_json(path, *, root, identity, limit=MAX_RECORD_BYTES):
    try:
        raw = read_private_bytes(
            path,
            trusted_root=root,
            trusted_root_identity=identity,
            max_bytes=limit,
            harden=False,
        )
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object), raw
    except LegacyArtifactError:
        raise
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise LegacyArtifactError() from exc


def _safe_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _string(value):
    if not isinstance(value, str):
        raise LegacyArtifactError()
    return value


def _positive_int(value):
    return type(value) is int and value >= 1


def _checkpoint_projection(value, checkpoint_id):
    if not isinstance(value, dict):
        raise LegacyArtifactError()
    if (
        value.get("record_type") != "checkpoint"
        or type(value.get("format_version")) is not int
        or value["format_version"] != 1
        or value.get("checkpoint_id") != checkpoint_id
        or not _safe_id(checkpoint_id)
        or value.get("checkpoint_type") not in _CHECKPOINT_TYPES
        or not isinstance(value.get("status"), str)
        or value["checkpoint_type"] == "restore"
        and value["status"] not in _RESTORE_STATUSES
        or value["checkpoint_type"] != "restore" and value["status"] != ""
    ):
        raise LegacyArtifactError()
    return {
        key: _string(value.get(key))
        for key in (
            "checkpoint_id",
            "checkpoint_type",
            "created_at",
            "status",
            "owner_id",
            "reviewed_at",
        )
    }


def _tool_change_projection(value, tool_change_id):
    if not isinstance(value, dict):
        raise LegacyArtifactError()
    if (
        value.get("record_type") != "tool_change"
        or type(value.get("format_version")) is not int
        or value["format_version"] != 2
        or value.get("tool_change_id") != tool_change_id
        or not _safe_id(tool_change_id)
        or value.get("status") not in _TOOL_CHANGE_STATUSES
    ):
        raise LegacyArtifactError()
    return {
        key: _string(value.get(key))
        for key in (
            "tool_change_id",
            "status",
            "owner_id",
            "tool_name",
            "effect_class",
            "started_at",
            "reviewed_at",
        )
    }


def _record_projection(record_type, value, record_id):
    if record_type == "checkpoint":
        return _checkpoint_projection(value, record_id)
    return _tool_change_projection(value, record_id)


class LegacyCheckpointReader:
    """Inspect old `.pony/checkpoints` data without creating or repairing it."""

    def __init__(self, workspace_root):
        root = Path(workspace_root)
        if root.name == "checkpoints" and root.parent.name == ".pony":
            root = root
        else:
            root = root / ".pony" / "checkpoints"
        checked = _directory(root, missing_ok=True)
        self.root = root
        self.identity = None if checked is None else checked[1]

    def _root_directory(self):
        if self.identity is None:
            return None
        checked = _directory(self.root)
        if checked[1] != self.identity:
            raise LegacyArtifactError()
        return checked

    def _records_directory(self, name):
        if self._root_directory() is None:
            return None
        checked = _directory(self.root / name, missing_ok=True)
        return None if checked is None else checked

    def _scan(self, directory_name, record_type, *, strict):
        directory = self._records_directory(directory_name)
        if directory is None:
            return []
        root, identity = directory
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise LegacyArtifactError() from exc
        if len(entries) > MAX_RECORDS:
            raise LegacyArtifactError()
        records = []
        for path in entries:
            if path.suffix != ".json":
                continue
            try:
                info = path.lstat()
                if (
                    path.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > MAX_RECORD_BYTES
                ):
                    raise LegacyArtifactError()
                value, _raw = _read_json(path, root=root, identity=identity)
                key = "checkpoint_id" if record_type == "checkpoint" else "tool_change_id"
                if not isinstance(value, dict) or value.get(key) != path.stem:
                    raise LegacyArtifactError()
                records.append(_record_projection(record_type, value, path.stem))
            except LegacyArtifactError:
                if strict:
                    raise
                digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()
                records.append(
                    {
                        "opaque_id": f"invalid_{digest}",
                        "record_kind": record_type,
                        "status": "invalid_record",
                    }
                )
        if private_directory_identity(root) != identity:
            raise LegacyArtifactError()
        self._root_directory()
        return records

    def list_checkpoint_records(self, *, strict=False):
        records = self._scan("records", "checkpoint", strict=strict)
        return sorted(records, key=lambda item: str(item.get("created_at", "")))

    def list_tool_change_records(self, *, strict=False):
        return self._scan("tool_changes", "tool_change", strict=strict)

    def load_checkpoint_record(self, checkpoint_id):
        if not _safe_id(checkpoint_id):
            raise FileNotFoundError(checkpoint_id)
        directory = self._records_directory("records")
        if directory is None:
            raise FileNotFoundError(checkpoint_id)
        value, _raw = _read_json(
            directory[0] / f"{checkpoint_id}.json",
            root=directory[0],
            identity=directory[1],
        )
        record = _checkpoint_projection(value, checkpoint_id)
        if private_directory_identity(directory[0]) != directory[1]:
            raise LegacyArtifactError()
        self._root_directory()
        return record

    def review_items(self):
        tool_changes = []
        restore_journals = []
        invalid_records = []
        for item in self.list_tool_change_records(strict=False):
            if item.get("status") == "invalid_record":
                invalid_records.append(item)
            elif item.get("status") == "pending" or (
                item.get("status") == "interrupted" and not item.get("reviewed_at")
            ):
                tool_changes.append(
                    {
                        key: item.get(key, "")
                        for key in (
                            "tool_change_id",
                            "status",
                            "owner_id",
                            "tool_name",
                            "effect_class",
                            "started_at",
                        )
                    }
                )
        for item in self.list_checkpoint_records(strict=False):
            if item.get("status") == "invalid_record":
                invalid_records.append(item)
            elif item.get("checkpoint_type") == "restore" and (
                item.get("status") == "applying"
                or (item.get("status") == "partial" and not item.get("reviewed_at"))
            ):
                restore_journals.append(
                    {
                        key: item.get(key, "")
                        for key in ("checkpoint_id", "status", "owner_id", "created_at")
                    }
                )
        return {
            "tool_changes": tool_changes,
            "restore_journals": restore_journals,
            "invalid_records": invalid_records,
        }


def legacy_sandbox_session_bound(workspace_root, pony_session_id):
    """Return whether a validated old Sandbox sidecar binds this Session."""
    root = Path(workspace_root)
    try:
        source_info = root.lstat()
    except OSError as exc:
        raise LegacyArtifactError("sandbox_state_invalid") from exc
    if root.is_symlink() or not stat.S_ISDIR(source_info.st_mode):
        raise LegacyArtifactError("sandbox_state_invalid")
    checked = _directory(root / ".pony" / "sandbox_sessions", missing_ok=True)
    if checked is None:
        return False
    sidecars, identity = checked
    try:
        paths = sorted(sidecars.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise LegacyArtifactError("sandbox_state_invalid") from exc
    if len(paths) > MAX_SIDECARS:
        raise LegacyArtifactError("sandbox_state_invalid")
    total = 0
    matches = 0
    for path in paths:
        if re.fullmatch(r"sandbox_[0-9a-f]{32}\.json", path.name) is None:
            raise LegacyArtifactError("sandbox_state_invalid")
        try:
            info = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise LegacyArtifactError("sandbox_state_invalid")
            total += info.st_size
            if total > MAX_SIDECAR_BYTES:
                raise LegacyArtifactError("sandbox_state_invalid")
            pointer, _raw = _read_json(
                path, root=sidecars, identity=identity, limit=MAX_RECORD_BYTES
            )
        except LegacyArtifactError as exc:
            raise LegacyArtifactError("sandbox_state_invalid") from exc
        required = {
            "record_type",
            "format_version",
            "pony_session_id",
            "sandbox_id",
            "source_root",
            "source_device",
            "source_inode",
            "state_root",
            "state_device",
            "state_inode",
        }
        if (
            set(pointer) != required
            or pointer.get("record_type") != "docker_sandbox_session_pointer"
            or type(pointer.get("format_version")) is not int
            or pointer["format_version"] != 1
            or _SANDBOX_ID.fullmatch(str(pointer.get("sandbox_id", ""))) is None
            or path.name != f"{pointer['sandbox_id']}.json"
            or pointer.get("source_root") != str(root)
            or not _positive_int(pointer.get("source_device"))
            or not _positive_int(pointer.get("source_inode"))
            or not isinstance(pointer.get("state_root"), str)
            or not os.path.isabs(pointer["state_root"])
            or not _positive_int(pointer.get("state_device"))
            or not _positive_int(pointer.get("state_inode"))
            or (pointer["source_device"], pointer["source_inode"])
            != (source_info.st_dev, source_info.st_ino)
            or not isinstance(pointer.get("pony_session_id"), str)
        ):
            raise LegacyArtifactError("sandbox_state_invalid")
        if pointer["pony_session_id"] == pony_session_id:
            matches += 1
    try:
        current_source = root.lstat()
    except OSError as exc:
        raise LegacyArtifactError("sandbox_state_invalid") from exc
    if (
        root.is_symlink()
        or not stat.S_ISDIR(current_source.st_mode)
        or (current_source.st_dev, current_source.st_ino)
        != (source_info.st_dev, source_info.st_ino)
        or private_directory_identity(sidecars) != identity
        or matches > 1
    ):
        raise LegacyArtifactError("sandbox_state_invalid")
    return matches == 1
