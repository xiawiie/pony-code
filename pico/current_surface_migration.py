"""One-time Plan 3 transaction for the exact Plan 1 manifest.

This module is intentionally isolated from production imports and is deleted
after the single audited migration.
"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import secrets
import re

from pico import file_lock
from pico.checkpoint_store import (
    CheckpointStore,
    _validate_checkpoint_record,
    _validate_tool_change_record,
)
from pico.recovery_checkpoint_writer import validate_file_entry
from pico.recovery_manager import _rename_swap
from pico.recovery_models import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_RECORD_TYPE,
    TOOL_CHANGE_FORMAT_VERSION,
    TOOL_CHANGE_RECORD_TYPE,
)
from pico.session_store import (
    SESSION_FORMAT_VERSION,
    SESSION_RECORD_TYPE,
    SessionStore,
    _validate_payload as validate_current_session,
)
from pico.security import (
    _open_private_parent,
    _open_private_directory,
    _private_directory_flags,
    _write_all,
    ensure_private_dir,
    private_directory_identity,
    require_directory_no_symlink,
    require_regular_no_symlink,
    write_private_bytes_atomic,
)


class MigrationError(RuntimeError):
    """A content-free migration refusal."""


class MigrationInterrupted(BaseException):
    """Test-only crash boundary; deliberately bypasses ordinary rollback."""


_STAT_KEYS = ("device", "inode", "nlink", "mode", "mtime_ns", "size", "sha256")
_SUMMARY = {
    "total": 46,
    "transform": 8,
    "verify_only": 38,
    "sessions": 5,
    "runs": 36,
    "checkpoints": 5,
    "memory": 0,
    "user_memory": 0,
}


def _fault(label):
    if os.environ.get("PICO_MIGRATION_FAULT") == label:
        raise MigrationInterrupted(label)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _decode(raw, label="artifact"):
    def object_from_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MigrationError(f"duplicate {label} key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=object_from_pairs)
    except MigrationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MigrationError(f"invalid {label}") from None
    if not isinstance(value, dict):
        raise MigrationError(f"invalid {label}")
    return value


def _read_regular(
    path,
    *,
    private=False,
    max_bytes=16 * 1024 * 1024,
    trusted_root=None,
    trusted_root_identity=None,
):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_descriptor = -1
    descriptor = -1
    try:
        path, parent_descriptor = _open_private_parent(
            path,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except (OSError, ValueError):
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        raise MigrationError("unsafe artifact") from None
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or (private and stat.S_IMODE(opened.st_mode) != 0o600)
        ):
            raise MigrationError("unsafe artifact")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise MigrationError("artifact too large")
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
            or after.st_nlink != 1
            or current.st_nlink != 1
            or not stat.S_ISREG(after.st_mode)
        ):
            raise MigrationError("artifact changed")
        return raw, after
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _relative_path(value):
    if not isinstance(value, str):
        raise MigrationError("invalid manifest path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError("invalid manifest path")
    return path


def _manifest_path():
    value = os.environ.get("PICO_PLAN1_MANIFEST")
    if not value:
        raise MigrationError("manifest environment variable missing")
    path = Path(os.path.abspath(os.path.expanduser(value)))
    try:
        return require_regular_no_symlink(path)
    except (OSError, ValueError):
        raise MigrationError("unsafe manifest") from None


def _load_manifest(repo_root):
    path = _manifest_path()
    manifest_root = require_directory_no_symlink(path.parent)
    manifest_root_identity = private_directory_identity(manifest_root)
    raw, _ = _read_regular(
        path,
        private=True,
        trusted_root=manifest_root,
        trusted_root_identity=manifest_root_identity,
    )
    manifest = _decode(raw, "manifest")
    repo_hash = hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:16]
    if (
        set(manifest) != {
            "record_type", "created_at", "git_head", "repo_hash",
            "checkpoint_lock_mode_before", "summary", "entries",
        }
        or manifest.get("record_type") != "current_surface_preflight"
        or manifest.get("repo_hash") != repo_hash
        or not isinstance(manifest.get("created_at"), str)
        or not isinstance(manifest.get("git_head"), str)
        or len(manifest["git_head"]) != 40
        or manifest.get("checkpoint_lock_mode_before") not in {"0600", "0644"}
        or manifest.get("summary") != _SUMMARY
        or not isinstance(manifest.get("entries"), list)
        or len(manifest["entries"]) != _SUMMARY["total"]
    ):
        raise MigrationError("manifest identity mismatch")
    entries = []
    seen = set()
    for item in manifest["entries"]:
        if not isinstance(item, dict):
            raise MigrationError("invalid manifest entry")
        relative = _relative_path(item.get("path"))
        if relative in seen or item.get("role") not in {"transform", "verify_only"}:
            raise MigrationError("invalid manifest entry")
        if (
            set(item) != {"path", "role", *_STAT_KEYS}
            or any(type(item.get(key)) is not int for key in _STAT_KEYS[:-1])
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
        ):
            raise MigrationError("invalid manifest entry")
        seen.add(relative)
        entries.append({**item, "path": relative.as_posix()})
    def transform_path(item):
        parts = PurePosixPath(item["path"]).parts
        return (
            len(parts) == 2 and parts[0] == "sessions" and parts[1].endswith(".json")
        ) or (
            len(parts) == 3
            and parts[:2] in {("checkpoints", "records"), ("checkpoints", "tool_changes")}
            and parts[2].endswith(".json")
        )

    if any((item["role"] == "transform") != transform_path(item) for item in entries):
        raise MigrationError("manifest target mismatch")
    observed_summary = {
        "total": len(entries),
        "transform": sum(item["role"] == "transform" for item in entries),
        "verify_only": sum(item["role"] == "verify_only" for item in entries),
        "sessions": sum(item["path"].startswith("sessions/") for item in entries),
        "runs": sum(item["path"].startswith("runs/") for item in entries),
        "checkpoints": sum(item["path"].startswith("checkpoints/") for item in entries),
        "memory": sum(item["path"].startswith("memory/") for item in entries),
        "user_memory": 0,
    }
    if observed_summary != _SUMMARY:
        raise MigrationError("manifest target mismatch")
    targets = [item for item in entries if item["role"] == "transform"]
    families = [PurePosixPath(item["path"]).parts for item in targets]
    if (
        len(targets) != 8
        or sum(len(parts) == 2 and parts[0] == "sessions" and parts[1].endswith(".json") for parts in families) != 4
        or sum(len(parts) == 3 and parts[:2] == ("checkpoints", "records") and parts[2].endswith(".json") for parts in families) != 2
        or sum(len(parts) == 3 and parts[:2] == ("checkpoints", "tool_changes") and parts[2].endswith(".json") for parts in families) != 2
    ):
        raise MigrationError("manifest target mismatch")
    return {
        "path": path,
        "sha256": _sha256(raw),
        "repo_hash": repo_hash,
        "entries": entries,
        "pico_identity": private_directory_identity(repo_root / ".pico"),
    }


def _entry_path(repo_root, entry):
    path = repo_root / ".pico" / Path(*PurePosixPath(entry["path"]).parts)
    try:
        path.relative_to(repo_root / ".pico")
    except ValueError:
        raise MigrationError("manifest path escaped") from None
    return path


def _metadata(info, raw):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "nlink": info.st_nlink,
        "mode": stat.S_IMODE(info.st_mode),
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "sha256": _sha256(raw),
    }


def _read_live(repo_root, manifest, entry):
    return _read_regular(
        _entry_path(repo_root, entry),
        private=True,
        trusted_root=repo_root / ".pico",
        trusted_root_identity=manifest["pico_identity"],
    )


def _matches_manifest(repo_root, manifest, entry, *, exact_identity=True):
    raw, info = _read_live(repo_root, manifest, entry)
    observed = _metadata(info, raw)
    keys = _STAT_KEYS if exact_identity else ("nlink", "mode", "mtime_ns", "size", "sha256")
    if any(observed[key] != entry[key] for key in keys):
        raise MigrationError("manifest drift")
    return raw, observed


def _scan_tree(root, root_identity, *, include_dirs=False):
    files = {}
    descriptors = [_open_private_directory(root)]
    try:
        opened = os.fstat(descriptors[0])
        if (opened.st_dev, opened.st_ino) != tuple(root_identity):
            raise MigrationError("root changed")

        def visit(descriptor, prefix=()):
            with os.scandir(descriptor) as entries:
                entries = list(entries)
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                relative = PurePosixPath(*prefix, entry.name).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    if stat.S_IMODE(info.st_mode) != 0o700:
                        raise MigrationError("unsafe private directory")
                    if include_dirs:
                        files[relative] = info
                    child = os.open(entry.name, _private_directory_flags(), dir_fd=descriptor)
                    descriptors.append(child)
                    current = os.fstat(child)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise MigrationError("directory changed")
                    visit(child, (*prefix, entry.name))
                else:
                    files[relative] = info

        visit(descriptors[0])
        return files
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _live_temp_target(relative, transaction):
    if transaction is None:
        return None
    candidate = PurePosixPath(relative)
    for target in transaction["targets"]:
        live = PurePosixPath(target["path"])
        pattern = rf"^\.{re.escape(live.name)}\.pico-migration\.[0-9a-f]{{24}}\.tmp$"
        if candidate.parent == live.parent and re.fullmatch(pattern, candidate.name):
            return target
    return None


def _validate_path_set(repo_root, manifest, transaction=None):
    try:
        current = _scan_tree(repo_root / ".pico", manifest["pico_identity"])
        if any(
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            for info in current.values()
        ):
            raise MigrationError("unsafe artifact")
    except (OSError, ValueError):
        raise MigrationError("manifest path set drift") from None
    expected = {item["path"] for item in manifest["entries"]}
    extras = set(current) - expected
    if set(current) - extras != expected or any(
        _live_temp_target(relative, transaction) is None for relative in extras
    ):
        raise MigrationError("manifest path set drift")
    user_memory = Path.home() / ".pico" / "memory"
    try:
        user_info = user_memory.lstat()
    except FileNotFoundError:
        pass
    else:
        try:
            if not stat.S_ISDIR(user_info.st_mode):
                raise MigrationError("unexpected user memory data")
            require_directory_no_symlink(user_memory)
            if any(user_memory.iterdir()):
                raise MigrationError("unexpected user memory data")
        except (OSError, ValueError):
            raise MigrationError("unexpected user memory data") from None


def _validate_manifest(
    repo_root,
    manifest,
    *,
    transaction=None,
    require_transformed_metadata=False,
):
    _validate_path_set(repo_root, manifest, transaction)
    transformed = {}
    if transaction is not None:
        transformed = {item["path"]: item for item in transaction["targets"]}
    for entry in manifest["entries"]:
        if entry["role"] == "verify_only" or entry["path"] not in transformed:
            _matches_manifest(repo_root, manifest, entry)
            continue
        raw, info = _read_live(repo_root, manifest, entry)
        current_hash = _sha256(raw)
        target = transformed[entry["path"]]
        if current_hash not in {target["original"]["sha256"], target["transformed_sha256"]}:
            raise MigrationError("target drift")
        if require_transformed_metadata and (
            stat.S_IMODE(info.st_mode) != entry["mode"]
            or info.st_mtime_ns != entry["mtime_ns"]
        ):
            raise MigrationError("target metadata drift")


def _remove_prompt_cache(identity):
    if not isinstance(identity, dict):
        return
    flags = identity.get("feature_flags")
    if isinstance(flags, dict):
        flags.pop("prompt_cache", None)


def _transform_session(record, session_id):
    if type(record.get("schema_version")) is not int or record["schema_version"] not in {2, 3}:
        raise MigrationError("unsupported old session")
    if record["schema_version"] == 3 and "history" in record:
        raise MigrationError("invalid old session")
    messages = deepcopy(record.get("messages"))
    if not isinstance(messages, list):
        raise MigrationError("invalid old session")
    result = deepcopy(record)
    if any(not isinstance(record.get(key), str) for key in ("id", "created_at", "workspace_root")):
        raise MigrationError("invalid old session")
    if record["id"] != session_id:
        raise MigrationError("invalid old session")
    result.pop("schema_version", None)
    result.pop("history", None)
    result.update(record_type=SESSION_RECORD_TYPE, format_version=SESSION_FORMAT_VERSION)
    for key, empty in (
        ("working_memory", {}),
        ("memory", {}),
        ("recently_recalled", []),
        ("checkpoints", {}),
        ("resume_state", {}),
        ("recovery", {}),
        ("runtime_identity", {}),
    ):
        if key not in result:
            result[key] = deepcopy(empty)
        if not isinstance(result[key], type(empty)):
            raise MigrationError("invalid old session")
    result["messages"] = messages
    _remove_prompt_cache(result["runtime_identity"])
    items = result["checkpoints"].get("items", {})
    if not isinstance(items, dict):
        raise MigrationError("invalid old session")
    for checkpoint in items.values():
        if not isinstance(checkpoint, dict):
            raise MigrationError("invalid old session")
        checkpoint.pop("schema_version", None)
        _remove_prompt_cache(checkpoint.get("runtime_identity"))
    try:
        validate_current_session(result, session_id)
    except ValueError:
        raise MigrationError("invalid transformed session") from None
    if result["messages"] != messages:
        raise MigrationError("session transcript changed")
    return result


def _review_only_entry(entry):
    result = deepcopy(entry)
    if not isinstance(result, dict):
        raise MigrationError("invalid old file entry")
    missing_mode = any(
        result.get(prefix + "_exists") is True and not isinstance(result.get(prefix + "_mode"), int)
        for prefix in ("before", "after")
    )
    missing_sources = not isinstance(result.get("source_tool_change_ids"), list)
    if missing_mode or missing_sources:
        result["snapshot_eligible"] = False
        result["ineligible_reason"] = "mode_unknown"
        result["before_mode"] = None
        result["after_mode"] = None
        result["source_tool_change_ids"] = []
    if validate_file_entry(result):
        raise MigrationError("invalid old file entry")
    return result


def _transform_file_entries(record):
    if "file_entries" in record:
        if not isinstance(record["file_entries"], list):
            raise MigrationError("invalid old recovery record")
        record["file_entries"] = [_review_only_entry(item) for item in record["file_entries"]]
    if "prepared_file_entries" in record and not isinstance(record["prepared_file_entries"], list):
        raise MigrationError("invalid old recovery record")


def _transform_checkpoint(record):
    if record.get("schema_version") != "checkpoint-record-v1":
        raise MigrationError("unsupported old checkpoint")
    result = deepcopy(record)
    result.pop("schema_version", None)
    result.update(record_type=CHECKPOINT_RECORD_TYPE, format_version=CHECKPOINT_FORMAT_VERSION)
    for key in ("checkpoint_id", "checkpoint_type", "created_at", "workspace_root"):
        if not isinstance(result.get(key), str) or not result[key]:
            raise MigrationError("invalid old checkpoint")
    result.setdefault("status", "")
    for key in (
        "session_id", "run_id", "turn_id", "parent_checkpoint_id", "owner_id",
        "reviewed_at", "review_reason", "reviewed_by",
    ):
        result.setdefault(key, "")
    for key in (
        "tool_change_ids", "missing_tool_change_ids", "file_entries",
        "verification_evidence", "integrity_errors",
    ):
        result.setdefault(key, [])
    for key in ("git_review_context", "restore_provenance"):
        result.setdefault(key, {})
    for evidence in result.get("verification_evidence", []):
        if not isinstance(evidence, dict):
            raise MigrationError("invalid old verification evidence")
        evidence.pop("schema_version", None)
    _transform_file_entries(result)
    try:
        _validate_checkpoint_record(result, expected_id=result["checkpoint_id"])
    except ValueError:
        raise MigrationError("invalid transformed checkpoint") from None
    return result


def _transform_tool_change(record):
    if record.get("schema_version") != "tool-change-record-v1":
        raise MigrationError("unsupported old tool change")
    result = deepcopy(record)
    result.pop("schema_version", None)
    result.update(record_type=TOOL_CHANGE_RECORD_TYPE, format_version=TOOL_CHANGE_FORMAT_VERSION)
    for key in ("tool_change_id", "started_at", "tool_name", "effect_class"):
        if not isinstance(result.get(key), str) or not result[key]:
            raise MigrationError("invalid old tool change")
    result.setdefault("status", "pending")
    for key in (
        "checkpoint_id", "turn_id", "owner_id", "ended_at", "reviewed_at",
        "review_reason", "reviewed_by",
    ):
        result.setdefault(key, "")
    for key in (
        "affected_paths", "file_entries", "prepared_file_entries",
        "shell_side_effects", "trace_event_ids",
    ):
        result.setdefault(key, [])
    for key in ("input_summary", "recovery_context", "approval", "error"):
        result.setdefault(key, {})
    _transform_file_entries(result)
    try:
        _validate_tool_change_record(result, expected_id=result["tool_change_id"])
    except ValueError:
        raise MigrationError("invalid transformed tool change") from None
    return result


def _transform(entry, raw):
    record = _decode(raw)
    path = PurePosixPath(entry["path"])
    if path.parts[0] == "sessions":
        result = _transform_session(record, path.stem)
    elif path.parts[:2] == ("checkpoints", "records"):
        result = _transform_checkpoint(record)
    elif path.parts[:2] == ("checkpoints", "tool_changes"):
        result = _transform_tool_change(record)
    else:
        raise MigrationError("invalid transform target")
    return (json.dumps(result, indent=2) + "\n").encode("utf-8")


def _migration_commit(repo_root):
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        raise MigrationError("migration commit unavailable") from None
    if len(value) != 40:
        raise MigrationError("migration commit unavailable")
    return value


def _identity(repo_root, manifest):
    return {
        "repo_root": str(repo_root),
        "repo_hash": manifest["repo_hash"],
        "manifest_path": str(manifest["path"]),
        "manifest_sha256": manifest["sha256"],
        "migration_commit": _migration_commit(repo_root),
    }


def _private_dir(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        path = require_directory_no_symlink(path)
        info = path.lstat()
    except (OSError, ValueError):
        raise MigrationError("unsafe transaction directory") from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_nlink < 1:
        raise MigrationError("unsafe transaction directory")
    return path


def _fsync_dir(path, expected_identity=None):
    descriptor = _open_private_directory(path)
    try:
        opened = os.fstat(descriptor)
        if expected_identity is not None and (
            opened.st_dev, opened.st_ino
        ) != tuple(expected_identity):
            raise MigrationError("directory changed")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_atomic(root, root_identity, path, data, fault_label):
    def fsync_file(descriptor):
        os.fsync(descriptor)
        _fault(f"{fault_label}:file-fsync")

    def fsync_parent(descriptor):
        os.fsync(descriptor)
        _fault(f"{fault_label}:parent-fsync")

    write_private_bytes_atomic(
        path,
        data,
        trusted_root=root,
        trusted_root_identity=root_identity,
        error="migration atomic write failed",
        fsync_file=fsync_file,
        fsync_parent=fsync_parent,
    )


def _write_private_exclusive(root, root_identity, path, data, fault_label):
    path, parent = _open_private_parent(
        path,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    descriptor = -1
    identity = None
    completed = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        _write_all(descriptor, data)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _fault(f"{fault_label}:file-fsync")
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
            or (opened.st_dev, opened.st_ino) != identity
        ):
            raise MigrationError("backup changed")
        os.fsync(parent)
        _fault(f"{fault_label}:parent-fsync")
        completed = True
    except Exception:
        if descriptor >= 0:
            try:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
            except OSError:
                pass
        if identity is not None:
            try:
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == identity:
                    os.unlink(path.name, dir_fd=parent)
                    os.fsync(parent)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
    if not completed:
        raise MigrationError("backup write incomplete")


def _make_private_parents(root, root_identity, relative):
    relative = _relative_path(PurePosixPath(relative).as_posix())
    descriptor = _open_private_directory(root)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != tuple(root_identity):
            raise MigrationError("transaction root changed")
        for part in relative.parts:
            try:
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                _fault("backup:directory-fsync")
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise MigrationError("unsafe transaction directory")
            child = os.open(part, _private_directory_flags(), dir_fd=descriptor)
            current = os.fstat(child)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                os.close(child)
                raise MigrationError("transaction directory changed")
            os.close(descriptor)
            descriptor = child
        final = os.fstat(descriptor)
        return final.st_dev, final.st_ino
    finally:
        os.close(descriptor)


def _write_journal(root, root_identity, journal):
    _write_private_atomic(
        root,
        root_identity,
        root / "journal.json",
        (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        f"journal:{journal['status']}",
    )


def _update_journal(root, root_identity, journal, **updates):
    candidate = deepcopy(journal)
    candidate.update(updates)
    _write_journal(root, root_identity, candidate)
    journal.clear()
    journal.update(candidate)


def _read_journal(root, root_identity):
    raw, _ = _read_regular(
        root / "journal.json",
        private=True,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    journal = _decode(raw, "journal")
    if journal.get("status") not in {"prepared", "applying", "verified"}:
        raise MigrationError("invalid journal")
    return journal


_TRANSACTION_NAME = re.compile(r"^migration-[0-9]{8}T[0-9]{12}Z$")
_STAGING_NAME = re.compile(r"^\.migration-staging-[0-9a-f]{24}$")


def _validate_journal(
    root, root_identity, journal, identity, manifest, *, formal=True
):
    expected_top = {
        *identity,
        "transaction_name",
        "status",
        "targets",
        "applied_paths",
    }
    if set(journal) != expected_top:
        raise MigrationError("invalid journal")
    if any(journal.get(key) != value for key, value in identity.items()):
        raise MigrationError("journal identity mismatch")
    targets = journal.get("targets")
    applied = journal.get("applied_paths")
    if (
        not isinstance(targets, list)
        or len(targets) != 8
        or not isinstance(applied, list)
        or any(not isinstance(path, str) for path in applied)
        or len(applied) != len(set(applied))
        or not isinstance(journal.get("transaction_name"), str)
        or _TRANSACTION_NAME.fullmatch(journal["transaction_name"]) is None
        or (formal and root.name != journal["transaction_name"])
        or (not formal and _STAGING_NAME.fullmatch(root.name) is None)
    ):
        raise MigrationError("invalid journal")
    paths = [item.get("path") for item in targets if isinstance(item, dict)]
    expected_targets = {
        item["path"]: item for item in manifest["entries"] if item["role"] == "transform"
    }
    if (
        len(paths) != 8
        or any(not isinstance(path, str) for path in paths)
        or set(paths) != set(expected_targets)
        or any(path not in paths for path in applied)
        or (journal["status"] == "prepared" and applied)
        or (journal["status"] == "verified" and set(applied) != set(paths))
    ):
        raise MigrationError("invalid journal")
    for item in targets:
        if not isinstance(item, dict) or set(item) != {
            "path", "original", "backup_path", "backup_sha256", "transformed_sha256"
        }:
            raise MigrationError("invalid journal")
        relative = _relative_path(item["backup_path"])
        if relative.parts != ("original", *PurePosixPath(item["path"]).parts):
            raise MigrationError("invalid journal")
        backup = root / Path(*relative.parts)
        raw, _ = _read_regular(
            backup,
            private=True,
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        original = item.get("original")
        if (
            not isinstance(original, dict)
            or set(original) != set(_STAT_KEYS)
            or any(type(original.get(key)) is not int for key in _STAT_KEYS[:-1])
            or original != {key: expected_targets[item["path"]][key] for key in _STAT_KEYS}
            or _sha256(raw) != item.get("backup_sha256")
            or _sha256(raw) != original.get("sha256")
            or not isinstance(item.get("transformed_sha256"), str)
            or len(item["transformed_sha256"]) != 64
            or _sha256(_transform({"path": item["path"]}, raw)) != item["transformed_sha256"]
        ):
            raise MigrationError("backup verification failed")
    _validate_owned_tree(root, root_identity, targets)
    return journal


def _validate_owned_tree(root, root_identity, targets):
    allowed_files = {PurePosixPath("journal.json")}
    allowed_files.update(PurePosixPath(item["backup_path"]) for item in targets)
    allowed_dirs = {PurePosixPath("original")}
    for item in allowed_files:
        allowed_dirs.update(parent for parent in item.parents if parent != PurePosixPath("."))
    for relative_text, info in _scan_tree(
        root, root_identity, include_dirs=True
    ).items():
        relative = PurePosixPath(relative_text)
        if stat.S_ISDIR(info.st_mode):
            if relative not in allowed_dirs or stat.S_IMODE(info.st_mode) != 0o700:
                raise MigrationError("unsafe transaction content")
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise MigrationError("unsafe transaction content")
        leaf = relative.name
        temporary = (
            leaf.startswith(".journal.json.")
            and leaf.endswith(".tmp")
            and relative.parent == PurePosixPath(".")
        ) or any(
            leaf.startswith(f".{PurePosixPath(item).name}.")
            and leaf.endswith(".tmp")
            and relative.parent == PurePosixPath(item).parent
            for item in allowed_files
        )
        if relative not in allowed_files and not temporary:
            raise MigrationError("unsafe transaction content")


def _child_dir_identity(parent_root, parent_identity, name):
    parent = _open_private_directory(parent_root)
    child = -1
    try:
        opened_parent = os.fstat(parent)
        if (opened_parent.st_dev, opened_parent.st_ino) != tuple(parent_identity):
            raise MigrationError("backup root changed")
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o700:
            raise MigrationError("unsafe transaction directory")
        child = os.open(name, _private_directory_flags(), dir_fd=parent)
        opened = os.fstat(child)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MigrationError("transaction directory changed")
        return opened.st_dev, opened.st_ino
    finally:
        if child >= 0:
            os.close(child)
        os.close(parent)


def _remove_owned_tree(parent_root, parent_identity, root, root_identity):
    parent = _open_private_directory(parent_root)
    child = _open_private_directory(root)
    try:
        if (os.fstat(parent).st_dev, os.fstat(parent).st_ino) != tuple(parent_identity):
            raise MigrationError("backup root changed")
        if (os.fstat(child).st_dev, os.fstat(child).st_ino) != tuple(root_identity):
            raise MigrationError("transaction root changed")

        def remove_contents(descriptor):
            with os.scandir(descriptor) as scanned:
                entries = list(scanned)
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    nested = os.open(entry.name, _private_directory_flags(), dir_fd=descriptor)
                    try:
                        if (os.fstat(nested).st_dev, os.fstat(nested).st_ino) != (
                            info.st_dev, info.st_ino
                        ):
                            raise MigrationError("transaction directory changed")
                        remove_contents(nested)
                    finally:
                        os.close(nested)
                    os.rmdir(entry.name, dir_fd=descriptor)
                else:
                    current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise MigrationError("transaction content changed")
                    os.unlink(entry.name, dir_fd=descriptor)

        remove_contents(child)
        current = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != tuple(root_identity):
            raise MigrationError("transaction root changed")
        os.rmdir(root.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(child)
        os.close(parent)


def _safe_remove_staging(backup_root, backup_identity, root, root_identity, manifest):
    target_files = {
        PurePosixPath("original") / PurePosixPath(item["path"])
        for item in manifest["entries"] if item["role"] == "transform"
    }
    target_dirs = {PurePosixPath("original")}
    for target in target_files:
        target_dirs.update(target.parents)
    for relative_text, info in _scan_tree(
        root, root_identity, include_dirs=True
    ).items():
        if stat.S_ISLNK(info.st_mode) or (not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode)):
            raise MigrationError("unsafe staging directory")
        if stat.S_ISREG(info.st_mode) and (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise MigrationError("unsafe staging directory")
        if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) != 0o700:
            raise MigrationError("unsafe staging directory")
        pure = PurePosixPath(relative_text)
        allowed_temp = (
            pure.name.startswith(".journal.json.")
            or any(
                pure.name.startswith(f".{target.name}.")
                and pure.parent == target.parent
                for target in target_files
            )
        ) and pure.name.endswith(".tmp")
        if (
            stat.S_ISDIR(info.st_mode) and pure not in target_dirs
            or stat.S_ISREG(info.st_mode)
            and pure not in target_files
            and pure != PurePosixPath("journal.json")
            and not allowed_temp
        ):
            raise MigrationError("unsafe staging content")
    _remove_owned_tree(backup_root, backup_identity, root, root_identity)


def _direct_children(root, root_identity):
    descriptor = _open_private_directory(root)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != tuple(root_identity):
            raise MigrationError("backup root changed")
        with os.scandir(descriptor) as scanned:
            return {entry.name: entry.stat(follow_symlinks=False) for entry in scanned}
    finally:
        os.close(descriptor)


def _promote(backup_root, backup_identity, staging, staging_identity, formal_name):
    descriptor = _open_private_directory(backup_root)
    try:
        if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != tuple(backup_identity):
            raise MigrationError("backup root changed")
        before = os.stat(staging.name, dir_fd=descriptor, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != tuple(staging_identity):
            raise MigrationError("staging changed")
        try:
            os.stat(formal_name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise MigrationError("migration transaction already exists")
        os.rename(
            staging.name,
            formal_name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
        installed = os.stat(formal_name, dir_fd=descriptor, follow_symlinks=False)
        if (installed.st_dev, installed.st_ino) != tuple(staging_identity):
            raise MigrationError("promotion changed")
        os.fsync(descriptor)
        _fault("promotion:parent-fsync")
    finally:
        os.close(descriptor)


def _scan_transactions(backup_root, backup_identity, identity, repo_root, manifest):
    children = _direct_children(backup_root, backup_identity)
    formal = sorted(name for name in children if name.startswith("migration-"))
    staging = sorted(name for name in children if name.startswith(".migration-staging-"))
    if len(formal) > 1 or len(staging) > 1 or (formal and staging):
        raise MigrationError("ambiguous migration transaction")
    if formal:
        root = backup_root / formal[0]
        root_identity = _child_dir_identity(backup_root, backup_identity, formal[0])
        journal = _read_journal(root, root_identity)
        return root, root_identity, _validate_journal(
            root, root_identity, journal, identity, manifest
        )
    if staging:
        if _STAGING_NAME.fullmatch(staging[0]) is None:
            raise MigrationError("invalid staging name")
        root = backup_root / staging[0]
        root_identity = _child_dir_identity(backup_root, backup_identity, staging[0])
        contents = _scan_tree(root, root_identity)
        if "journal.json" not in contents:
            _validate_manifest(repo_root, manifest)
            _safe_remove_staging(
                backup_root, backup_identity, root, root_identity, manifest
            )
            return None, None, None
        journal = _read_journal(root, root_identity)
        _validate_journal(
            root, root_identity, journal, identity, manifest, formal=False
        )
        if journal["status"] != "prepared":
            raise MigrationError("invalid staging state")
        _promote(
            backup_root,
            backup_identity,
            root,
            root_identity,
            journal["transaction_name"],
        )
        return backup_root / journal["transaction_name"], root_identity, journal
    return None, None, None


def _create_transaction(backup_root, backup_identity, identity, repo_root, manifest):
    _validate_manifest(repo_root, manifest)
    descriptor = _open_private_directory(backup_root)
    try:
        if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != tuple(backup_identity):
            raise MigrationError("backup root changed")
        staging_name = f".migration-staging-{secrets.token_hex(12)}"
        os.mkdir(staging_name, 0o700, dir_fd=descriptor)
        staging_info = os.stat(staging_name, dir_fd=descriptor, follow_symlinks=False)
        os.fsync(descriptor)
        _fault("staging:parent-fsync")
    finally:
        os.close(descriptor)
    staging = backup_root / staging_name
    staging_identity = (staging_info.st_dev, staging_info.st_ino)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    transaction_name = f"migration-{timestamp}"
    targets = []
    try:
        original_identity = _make_private_parents(
            staging, staging_identity, PurePosixPath("original")
        )
        original_root = staging / "original"
        for index, entry in enumerate(item for item in manifest["entries"] if item["role"] == "transform"):
            raw, observed = _matches_manifest(repo_root, manifest, entry)
            transformed = _transform(entry, raw)
            relative = Path(*PurePosixPath(entry["path"]).parts)
            backup = original_root / relative
            _make_private_parents(staging, staging_identity, PurePosixPath("original") / relative.parent)
            _write_private_exclusive(
                staging, staging_identity, backup, raw, f"backup:{index}"
            )
            backup_raw, _ = _read_regular(
                backup,
                private=True,
                trusted_root=staging,
                trusted_root_identity=staging_identity,
            )
            if backup_raw != raw:
                raise MigrationError("backup verification failed")
            targets.append(
                {
                    "path": entry["path"],
                    "original": observed,
                    "backup_path": (Path("original") / relative).as_posix(),
                    "backup_sha256": _sha256(raw),
                    "transformed_sha256": _sha256(transformed),
                }
            )
            _fault(f"backup:{index}")
        _fsync_dir(original_root, original_identity)
        _fault("backup:root-fsync")
        journal = {
            **identity,
            "transaction_name": transaction_name,
            "status": "prepared",
            "targets": targets,
            "applied_paths": [],
        }
        _write_journal(staging, staging_identity, journal)
        _fault("prepared")
        root = backup_root / transaction_name
        _promote(
            backup_root,
            backup_identity,
            staging,
            staging_identity,
            transaction_name,
        )
        _fault("promoted")
        return root, staging_identity, journal
    except BaseException:
        # Crash simulation intentionally leaves staging for the next invocation.
        raise


def _read_leaf(parent_descriptor, name, max_bytes=16 * 1024 * 1024):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        info = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise MigrationError("unsafe artifact")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise MigrationError("artifact too large")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)
            or after.st_nlink != 1
        ):
            raise MigrationError("artifact changed")
        return data, after
    finally:
        os.close(descriptor)


def _replace_target(
    repo_root,
    manifest,
    entry,
    data,
    precondition,
    final_metadata=None,
    fault_prefix="replace",
):
    final_metadata = final_metadata or precondition
    path = _entry_path(repo_root, entry)
    current, info = _read_live(repo_root, manifest, entry)
    if any(_metadata(info, current)[key] != precondition[key] for key in _STAT_KEYS):
        raise MigrationError("target precondition failed")
    path, parent = _open_private_parent(
        path,
        trusted_root=repo_root / ".pico",
        trusted_root_identity=manifest["pico_identity"],
    )
    temp_name = f".{path.name}.pico-migration.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    temp_identity = None
    swapped = False
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent)
        temp_info = os.fstat(descriptor)
        temp_identity = (temp_info.st_dev, temp_info.st_ino)
        _write_all(descriptor, data)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _fault(f"{fault_prefix}:temp")
        temp_current = os.stat(temp_name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(temp_current.st_mode)
            or temp_current.st_nlink != 1
            or (temp_current.st_dev, temp_current.st_ino) != temp_identity
        ):
            raise MigrationError("target temp changed")
        old, old_info = _read_leaf(parent, path.name)
        if any(_metadata(old_info, old)[key] != precondition[key] for key in _STAT_KEYS):
            raise MigrationError("target precondition failed")
        _fault(f"{fault_prefix}:before-swap")
        _rename_swap(parent, temp_name, path.name)
        swapped = True
        _fault(f"{fault_prefix}:after-swap")
        displaced, displaced_info = _read_leaf(parent, temp_name)
        if any(
            _metadata(displaced_info, displaced)[key] != precondition[key]
            for key in _STAT_KEYS
        ):
            _rename_swap(parent, temp_name, path.name)
            swapped = False
            os.fsync(parent)
            raise MigrationError("target precondition failed")
        installed = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (installed.st_dev, installed.st_ino) != temp_identity or installed.st_nlink != 1:
            _rename_swap(parent, temp_name, path.name)
            swapped = False
            os.fsync(parent)
            restored, restored_info = _read_leaf(parent, path.name)
            if any(
                _metadata(restored_info, restored)[key] != precondition[key]
                for key in _STAT_KEYS
            ):
                raise MigrationError("target rollback failed")
            raise MigrationError("target changed")
        _fault(f"{fault_prefix}:before-metadata")
        os.fchmod(descriptor, final_metadata["mode"])
        os.utime(descriptor, ns=(final_metadata["mtime_ns"], final_metadata["mtime_ns"]))
        os.fsync(descriptor)
        _fault(f"{fault_prefix}:after-metadata")
        os.unlink(temp_name, dir_fd=parent)
        swapped = False
        os.fsync(parent)
        _fault(f"{fault_prefix}:after-cleanup")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not swapped and temp_identity is not None:
            try:
                current = os.stat(temp_name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == temp_identity:
                    os.unlink(temp_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _restore_metadata(repo_root, manifest, entry, expected_hash, original):
    path = _entry_path(repo_root, entry)
    raw, before = _read_live(repo_root, manifest, entry)
    if _sha256(raw) != expected_hash:
        raise MigrationError("target drift")
    path, parent = _open_private_parent(
        path,
        trusted_root=repo_root / ".pico",
        trusted_root_identity=manifest["pico_identity"],
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_nlink != 1
        ):
            raise MigrationError("target changed")
        os.fchmod(descriptor, original["mode"])
        os.utime(descriptor, ns=(original["mtime_ns"], original["mtime_ns"]))
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def _reconcile_live_temps(repo_root, manifest, journal):
    files = _scan_tree(repo_root / ".pico", manifest["pico_identity"])
    expected = {item["path"] for item in manifest["entries"]}
    for relative in set(files) - expected:
        target = _live_temp_target(relative, journal)
        if target is None:
            raise MigrationError("manifest path set drift")
        temp_entry = {"path": relative}
        temp_raw, temp_info = _read_live(repo_root, manifest, temp_entry)
        live_raw, _ = _read_live(repo_root, manifest, target)
        hashes = {_sha256(temp_raw), _sha256(live_raw)}
        allowed = {target["original"]["sha256"], target["transformed_sha256"]}
        if hashes != allowed or stat.S_IMODE(temp_info.st_mode) != 0o600:
            raise MigrationError("owned temp reconciliation failed")
        temp_path = _entry_path(repo_root, temp_entry)
        temp_path, parent = _open_private_parent(
            temp_path,
            trusted_root=repo_root / ".pico",
            trusted_root_identity=manifest["pico_identity"],
        )
        try:
            current = os.stat(temp_path.name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (temp_info.st_dev, temp_info.st_ino):
                raise MigrationError("owned temp changed")
            os.unlink(temp_path.name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)


def _transformed_bytes(root, root_identity, target):
    raw = _read_verified_backup(root, root_identity, target)
    return _transform({"path": target["path"]}, raw)


def _read_verified_backup(root, root_identity, target):
    raw, _ = _read_regular(
        root / Path(*PurePosixPath(target["backup_path"]).parts),
        private=True,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    digest = _sha256(raw)
    if (
        digest != target["backup_sha256"]
        or digest != target["original"]["sha256"]
    ):
        raise MigrationError("backup verification failed")
    return raw


def _strict_verify_locked(repo_root, manifest, root, root_identity, journal):
    _validate_manifest(
        repo_root,
        manifest,
        transaction=journal,
        require_transformed_metadata=True,
    )
    sessions = SessionStore(repo_root / ".pico" / "sessions")
    checkpoints = CheckpointStore(repo_root)
    embedded_count = 0
    family_counts = {"session": 0, "checkpoint": 0, "tool_change": 0}
    checkpoint_records = {}
    tool_records = {}
    for target in journal["targets"]:
        relative = PurePosixPath(target["path"])
        if relative.parts[0] == "sessions":
            current = sessions._load_unlocked(relative.stem)
            family_counts["session"] += 1
            items = current["checkpoints"].get("items", {})
            if not isinstance(items, dict):
                raise MigrationError("business invariant failed")
            embedded_count += len(items)
        elif relative.parts[:2] == ("checkpoints", "records"):
            loaded = checkpoints._load_checkpoint_record_unlocked(relative.stem)
            checkpoint_records[loaded["checkpoint_id"]] = loaded
            family_counts["checkpoint"] += 1
        else:
            loaded = checkpoints._load_tool_change_record_unlocked(relative.stem)
            tool_records[loaded["tool_change_id"]] = loaded
            family_counts["tool_change"] += 1
        backup, _ = _read_regular(
            root / Path(*PurePosixPath(target["backup_path"]).parts),
            private=True,
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        live, _ = _read_live(repo_root, manifest, target)
        original_record = _decode(backup)
        current_record = _decode(live)
        if current_record != _decode(_transform({"path": target["path"]}, backup)):
            raise MigrationError("business invariant failed")
        if relative.parts[0] == "sessions":
            old_items = original_record.get("checkpoints", {}).get("items", {})
            new_items = current_record["checkpoints"].get("items", {})
            if (
                original_record.get("messages") != current_record["messages"]
                or list(old_items) != list(new_items)
            ):
                raise MigrationError("business invariant failed")
    if family_counts != {"session": 4, "checkpoint": 2, "tool_change": 2} or embedded_count != 30:
        raise MigrationError("business invariant failed")
    checkpoint_refs = [
        tool_id
        for record in checkpoint_records.values()
        for tool_id in record["tool_change_ids"]
    ]
    tool_refs = [record["checkpoint_id"] for record in tool_records.values()]
    if (
        len(checkpoint_refs) != 2
        or len(tool_refs) != 2
        or any(tool_id not in tool_records for tool_id in checkpoint_refs)
        or any(checkpoint_id not in checkpoint_records for checkpoint_id in tool_refs)
    ):
        raise MigrationError("business invariant failed")
    for target in journal["targets"]:
        live, info = _read_live(repo_root, manifest, target)
        if (
            _sha256(live) != target["transformed_sha256"]
            or stat.S_IMODE(info.st_mode) != target["original"]["mode"]
            or info.st_mtime_ns != target["original"]["mtime_ns"]
        ):
            raise MigrationError("strict migration verification failed")


def _public_smoke(repo_root, journal):
    sessions = SessionStore(repo_root / ".pico" / "sessions")
    checkpoints = CheckpointStore(repo_root)
    for target in journal["targets"]:
        relative = PurePosixPath(target["path"])
        if relative.parts[0] == "sessions":
            sessions.load(relative.stem)
        elif relative.parts[:2] == ("checkpoints", "records"):
            checkpoints.load_checkpoint_record(relative.stem)
        else:
            checkpoints.load_tool_change_record(relative.stem)


def _rollback_locked(
    repo_root, manifest, root, root_identity, journal, identity
):
    if journal["status"] == "verified":
        raise MigrationError("verified transaction cannot be rolled back")
    _validate_journal(root, root_identity, journal, identity, manifest)
    _reconcile_live_temps(repo_root, manifest, journal)
    _validate_path_set(repo_root, manifest)
    for entry in manifest["entries"]:
        if entry["role"] == "verify_only":
            _matches_manifest(repo_root, manifest, entry)
    for target in journal["targets"]:
        live, _ = _read_live(repo_root, manifest, target)
        if _sha256(live) not in {target["original"]["sha256"], target["transformed_sha256"]}:
            raise MigrationError("rollback target drift")
    backups = {
        target["path"]: _read_verified_backup(root, root_identity, target)
        for target in journal["targets"]
    }
    failures = False
    for target in reversed(journal["targets"]):
        try:
            backup = backups[target["path"]]
            live, current_info = _read_live(repo_root, manifest, target)
            if _sha256(live) == target["original"]["sha256"]:
                if (
                    stat.S_IMODE(current_info.st_mode) != target["original"]["mode"]
                    or current_info.st_mtime_ns != target["original"]["mtime_ns"]
                ):
                    _restore_metadata(
                        repo_root,
                        manifest,
                        target,
                        target["original"]["sha256"],
                        target["original"],
                    )
                continue
            _replace_target(
                repo_root,
                manifest,
                target,
                backup,
                _metadata(current_info, live),
                target["original"],
                fault_prefix="rollback",
            )
        except Exception:
            failures = True
    for entry in manifest["entries"]:
        raw, info = _read_live(repo_root, manifest, entry)
        if entry["role"] == "verify_only":
            if any(_metadata(info, raw)[key] != entry[key] for key in _STAT_KEYS):
                failures = True
        else:
            observed = _metadata(info, raw)
            if any(
                observed[key] != entry[key]
                for key in ("nlink", "mode", "mtime_ns", "size", "sha256")
            ):
                failures = True
    if failures:
        raise MigrationError("rollback incomplete")
    _validate_path_set(repo_root, manifest)


def _locks(repo_root):
    stack = ExitStack()
    stack.enter_context(
        file_lock.locked_file(
            repo_root / ".pico" / "checkpoints" / ".checkpoint_store.lock",
            require_lock=True,
            require_existing=True,
        )
    )
    stack.enter_context(
        file_lock.locked_file(
            repo_root / ".pico" / "sessions" / ".session_store.lock",
            require_lock=True,
            require_existing=True,
        )
    )
    return stack


def _context():
    repo_root = Path.cwd().resolve(strict=True)
    manifest = _load_manifest(repo_root)
    identity = _identity(repo_root, manifest)
    backup_root = Path.home() / ".pico" / "backups" / manifest["repo_hash"]
    return repo_root, manifest, identity, backup_root


def check():
    repo_root, manifest, _, _ = _context()
    _validate_manifest(repo_root, manifest)
    print("check ok transformed=8 verify_only=38")


def apply():
    repo_root, manifest, identity, backup_root = _context()
    ensure_private_dir(backup_root)
    backup_identity = private_directory_identity(backup_root)
    mutex = backup_root / "migration.lock"
    with file_lock.locked_file(mutex, require_lock=True, blocking=False):
        root, root_identity, journal = _scan_transactions(
            backup_root, backup_identity, identity, repo_root, manifest
        )
        _validate_manifest(repo_root, manifest, transaction=journal)
        with _locks(repo_root):
            if root is None:
                root, root_identity, journal = _create_transaction(
                    backup_root, backup_identity, identity, repo_root, manifest
                )
            _validate_journal(
                root, root_identity, journal, identity, manifest
            )
            _reconcile_live_temps(repo_root, manifest, journal)
            _validate_manifest(repo_root, manifest, transaction=journal)
            if journal["status"] == "verified":
                _strict_verify_locked(
                    repo_root, manifest, root, root_identity, journal
                )
            else:
                try:
                    if journal["status"] == "prepared":
                        _update_journal(
                            root, root_identity, journal, status="applying"
                        )
                    for index, target in enumerate(journal["targets"]):
                        live, info = _read_live(repo_root, manifest, target)
                        current_hash = _sha256(live)
                        if current_hash == target["transformed_sha256"]:
                            if stat.S_IMODE(info.st_mode) != target["original"]["mode"] or info.st_mtime_ns != target["original"]["mtime_ns"]:
                                _restore_metadata(
                                    repo_root, manifest, target, current_hash, target["original"]
                                )
                            if target["path"] not in journal["applied_paths"]:
                                _update_journal(
                                    root,
                                    root_identity,
                                    journal,
                                    applied_paths=[
                                        *journal["applied_paths"], target["path"]
                                    ],
                                )
                            continue
                        if current_hash != target["original"]["sha256"]:
                            raise MigrationError("target drift")
                        observed = _metadata(info, live)
                        if any(
                            observed[key] != target["original"][key]
                            for key in (
                                "nlink",
                                "mode",
                                "mtime_ns",
                                "size",
                                "sha256",
                            )
                        ):
                            raise MigrationError("target metadata drift")
                        transformed = _transformed_bytes(root, root_identity, target)
                        _replace_target(
                            repo_root,
                            manifest,
                            target,
                            transformed,
                            observed,
                            target["original"],
                            fault_prefix=f"replace:{index}",
                        )
                        _fault(f"replace:{index}")
                        verified, verified_info = _read_live(repo_root, manifest, target)
                        if (
                            _sha256(verified) != target["transformed_sha256"]
                            or stat.S_IMODE(verified_info.st_mode) != target["original"]["mode"]
                            or verified_info.st_mtime_ns != target["original"]["mtime_ns"]
                        ):
                            raise MigrationError("target write verification failed")
                        if target["path"] not in journal["applied_paths"]:
                            _update_journal(
                                root,
                                root_identity,
                                journal,
                                applied_paths=[
                                    *journal["applied_paths"], target["path"]
                                ],
                            )
                    _strict_verify_locked(
                        repo_root, manifest, root, root_identity, journal
                    )
                    _update_journal(
                        root, root_identity, journal, status="verified"
                    )
                except Exception:
                    _rollback_locked(
                        repo_root,
                        manifest,
                        root,
                        root_identity,
                        journal,
                        identity,
                    )
                    raise
        _public_smoke(repo_root, journal)
    print("apply verified transformed=8 verify_only=38")


def verify():
    repo_root, manifest, identity, backup_root = _context()
    mutex = backup_root / "migration.lock"
    with file_lock.locked_file(mutex, require_lock=True, require_existing=True, blocking=False):
        backup_identity = private_directory_identity(backup_root)
        root, root_identity, journal = _scan_transactions(
            backup_root, backup_identity, identity, repo_root, manifest
        )
        if root is None or journal["status"] != "verified":
            raise MigrationError("verified transaction not found")
        with _locks(repo_root):
            _validate_journal(root, root_identity, journal, identity, manifest)
            _reconcile_live_temps(repo_root, manifest, journal)
            _strict_verify_locked(
                repo_root, manifest, root, root_identity, journal
            )
    print("verify ok transformed=8 verify_only=38")


def rollback():
    repo_root, manifest, identity, backup_root = _context()
    mutex = backup_root / "migration.lock"
    with file_lock.locked_file(mutex, require_lock=True, require_existing=True, blocking=False):
        backup_identity = private_directory_identity(backup_root)
        root, root_identity, journal = _scan_transactions(
            backup_root, backup_identity, identity, repo_root, manifest
        )
        if root is None:
            raise MigrationError("migration transaction not found")
        with _locks(repo_root):
            _rollback_locked(
                repo_root,
                manifest,
                root,
                root_identity,
                journal,
                identity,
            )
    print("rollback ok transformed=8 verify_only=38")


def verify_original():
    repo_root, manifest, identity, backup_root = _context()
    mutex = backup_root / "migration.lock"
    with file_lock.locked_file(mutex, require_lock=True, require_existing=True, blocking=False):
        backup_identity = private_directory_identity(backup_root)
        root, root_identity, journal = _scan_transactions(
            backup_root, backup_identity, identity, repo_root, manifest
        )
        if root is None or journal["status"] == "verified":
            raise MigrationError("original transaction state unavailable")
        with _locks(repo_root):
            _validate_journal(root, root_identity, journal, identity, manifest)
            _reconcile_live_temps(repo_root, manifest, journal)
            _validate_path_set(repo_root, manifest)
            for entry in manifest["entries"]:
                raw, info = _read_live(repo_root, manifest, entry)
                observed = _metadata(info, raw)
                keys = _STAT_KEYS if entry["role"] == "verify_only" else (
                    "nlink", "mode", "mtime_ns", "size", "sha256"
                )
                if any(observed[key] != entry[key] for key in keys):
                    raise MigrationError("original verification failed")
                if entry["role"] == "transform":
                    _transform(entry, raw)
    print("verify-original ok transformed=8 verify_only=38")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pico.current_surface_migration")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--verify", action="store_true")
    actions.add_argument("--rollback", action="store_true")
    actions.add_argument("--verify-original", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check:
            check()
        elif args.apply:
            apply()
        elif args.verify:
            verify()
        elif args.rollback:
            rollback()
        else:
            verify_original()
    except MigrationError as exc:
        parser.exit(1, f"migration refused: {exc}\n")
    except (OSError, RuntimeError, TypeError, ValueError):
        parser.exit(1, "migration refused: migration operation failed\n")


if __name__ == "__main__":  # pragma: no cover
    main()
