"""可恢复编辑的落盘层。

目录约定（相对 workspace_root）：
    .pony/checkpoints/records/          # Checkpoint Record，一份 turn/restore/manual 的元数据
    .pony/checkpoints/tool_changes/     # Tool Change Record，逐次工具执行的元数据
    .pony/checkpoints/blobs/            # 原始字节内容，按 sha256 前两位分桶

所有写入都走原子 replace，防止在崩溃时留下半截 JSON。
"""

import base64
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
import re
import stat
from pathlib import Path

from pony.state import file_lock
from pony.security import private_files as private_files
from pony.security import workspace_files as workspace_files
from pony.recovery.models import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_RECORD_TYPE,
    TOOL_CHANGE_FORMAT_VERSION,
    TOOL_CHANGE_RECORD_TYPE,
)
from pony.recovery.paths import hash_bytes
from pony.security.private_files import (
    ensure_private_dir,
    harden_private_tree,
    private_directory_identity,
    read_private_bytes,
    write_private_bytes_atomic,
)
from pony.security.paths import require_regular_no_symlink


def _identity(value):
    return value


@contextmanager
def _null_lock():
    yield


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
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
_SOURCE_MUTATION_GUARD_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "journal_id",
        "sandbox_id",
        "diff_digest",
    }
)
_APPLY_ID = re.compile(r"apply_[0-9a-f]{32}\Z")
_SANDBOX_ID = re.compile(r"sandbox_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_MUTATION_GUARD_RELATIVE = ".pony/checkpoints/source-apply-guard.json"
_CHECKPOINT_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "checkpoint_id",
        "checkpoint_type",
        "created_at",
        "session_id",
        "run_id",
        "turn_id",
        "parent_checkpoint_id",
        "workspace_root",
        "tool_change_ids",
        "missing_tool_change_ids",
        "git_review_context",
        "file_entries",
        "verification_evidence",
        "restore_provenance",
        "status",
        "owner_id",
        "reviewed_at",
        "review_reason",
        "reviewed_by",
        "integrity_errors",
    }
)
_TOOL_CHANGE_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "tool_change_id",
        "checkpoint_id",
        "turn_id",
        "owner_id",
        "tool_name",
        "effect_class",
        "status",
        "started_at",
        "ended_at",
        "input_summary",
        "affected_paths",
        "file_entries",
        "prepared_file_entries",
        "recovery_context",
        "shell_side_effects",
        "policy",
        "sandbox",
        "approval",
        "error",
        "trace_event_ids",
        "reviewed_at",
        "review_reason",
        "reviewed_by",
    }
)
_VERIFICATION_FIELDS = frozenset(
    {
        "verification_id",
        "created_at",
        "execution_mode",
        "command",
        "risk_class",
        "status",
        "stdout_tail",
        "stderr_tail",
        "affected_checkpoint_id",
        "trace_event_id",
        "argv",
        "runner_executed",
        "exit_code",
    }
)


class CheckpointStoreError(ValueError):
    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(self.code + ": " + str(message))


def _safe_id(value, label):
    text = value if isinstance(value, str) else ""
    if text in {"", ".", ".."} or _SAFE_ID.fullmatch(text) is None:
        raise CheckpointStoreError("invalid_record_id", f"invalid {label}")
    return text


def _decode_json(raw):
    def object_from_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CheckpointStoreError("duplicate_key", "duplicate record key")
            value[key] = item
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_from_pairs)
    except CheckpointStoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CheckpointStoreError("invalid_json", "invalid record JSON") from None


def _validate_checkpoint_record(record, expected_id=None):
    if not isinstance(record, dict):
        raise CheckpointStoreError(
            "invalid_record", "checkpoint record must be an object"
        )
    if record.keys() != _CHECKPOINT_FIELDS:
        raise CheckpointStoreError("invalid_record_shape", "invalid checkpoint fields")
    if record.get("record_type") != CHECKPOINT_RECORD_TYPE:
        raise CheckpointStoreError(
            "unsupported_record_type", "unsupported checkpoint record type"
        )
    if (
        type(record.get("format_version")) is not int
        or record["format_version"] != CHECKPOINT_FORMAT_VERSION
    ):
        raise CheckpointStoreError(
            "unsupported_format", "unsupported checkpoint format"
        )
    checkpoint_id = _safe_id(record.get("checkpoint_id"), "checkpoint id")
    if expected_id is not None and checkpoint_id != expected_id:
        raise CheckpointStoreError(
            "internal_id_mismatch", "checkpoint internal id mismatch"
        )
    checkpoint_type = record.get("checkpoint_type")
    if checkpoint_type not in _CHECKPOINT_TYPES:
        raise CheckpointStoreError("invalid_checkpoint_type", "invalid checkpoint type")
    status = record.get("status")
    if checkpoint_type == "restore" and status not in _RESTORE_STATUSES:
        raise CheckpointStoreError("invalid_status", "invalid restore status")
    if checkpoint_type != "restore" and status != "":
        raise CheckpointStoreError(
            "invalid_status", "non-restore checkpoint has status"
        )
    string_fields = (
        "status",
        "created_at",
        "session_id",
        "run_id",
        "turn_id",
        "parent_checkpoint_id",
        "workspace_root",
        "owner_id",
        "reviewed_at",
        "review_reason",
        "reviewed_by",
    )
    list_fields = (
        "tool_change_ids",
        "missing_tool_change_ids",
        "file_entries",
        "verification_evidence",
        "integrity_errors",
    )
    dict_fields = ("git_review_context", "restore_provenance")
    if any(not isinstance(record.get(key), str) for key in string_fields):
        raise CheckpointStoreError(
            "invalid_record_shape", "invalid checkpoint string field"
        )
    if any(not isinstance(record.get(key), list) for key in list_fields):
        raise CheckpointStoreError(
            "invalid_record_shape", "invalid checkpoint list field"
        )
    if any(not isinstance(record.get(key), dict) for key in dict_fields):
        raise CheckpointStoreError(
            "invalid_record_shape", "invalid checkpoint object field"
        )
    from pony.recovery.checkpoint_writer import validate_file_entry

    if any(validate_file_entry(entry) for entry in record["file_entries"]):
        raise CheckpointStoreError(
            "invalid_file_entry", "invalid checkpoint file entry"
        )
    if any(
        not _valid_verification_evidence(item)
        for item in record["verification_evidence"]
    ):
        raise CheckpointStoreError(
            "invalid_verification", "invalid verification evidence"
        )
    for tool_change_id in record["tool_change_ids"] + record["missing_tool_change_ids"]:
        _safe_id(tool_change_id, "tool change id")
    return record


def _validate_tool_change_record(record, expected_id=None):
    if not isinstance(record, dict):
        raise CheckpointStoreError(
            "invalid_record", "tool change record must be an object"
        )
    if record.keys() != _TOOL_CHANGE_FIELDS:
        raise CheckpointStoreError("invalid_record_shape", "invalid tool change fields")
    if record.get("record_type") != TOOL_CHANGE_RECORD_TYPE:
        raise CheckpointStoreError(
            "unsupported_record_type", "unsupported tool change record type"
        )
    if (
        type(record.get("format_version")) is not int
        or record["format_version"] != TOOL_CHANGE_FORMAT_VERSION
    ):
        raise CheckpointStoreError(
            "unsupported_format", "unsupported tool change format"
        )
    tool_change_id = _safe_id(record.get("tool_change_id"), "tool change id")
    if expected_id is not None and tool_change_id != expected_id:
        raise CheckpointStoreError(
            "internal_id_mismatch", "tool change internal id mismatch"
        )
    if record.get("status") not in _TOOL_CHANGE_STATUSES:
        raise CheckpointStoreError("invalid_status", "invalid tool change status")
    string_fields = (
        "checkpoint_id",
        "turn_id",
        "owner_id",
        "tool_name",
        "effect_class",
        "started_at",
        "ended_at",
        "reviewed_at",
        "review_reason",
        "reviewed_by",
    )
    list_fields = (
        "affected_paths",
        "file_entries",
        "prepared_file_entries",
        "shell_side_effects",
        "trace_event_ids",
    )
    dict_fields = (
        "input_summary",
        "recovery_context",
        "policy",
        "sandbox",
        "approval",
        "error",
    )
    if any(not isinstance(record.get(key), str) for key in string_fields):
        raise CheckpointStoreError(
            "invalid_record_shape", "invalid tool change string field"
        )
    if any(not isinstance(record.get(key), list) for key in list_fields):
        raise CheckpointStoreError(
            "invalid_record_shape", "invalid tool change list field"
        )
    if any(not isinstance(record.get(key), dict) for key in dict_fields):
        raise CheckpointStoreError(
            "invalid_record_shape", "invalid tool change object field"
        )
    from pony.recovery.checkpoint_writer import validate_file_entry
    from pony.recovery.paths import normalize_workspace_relative_path

    if any(validate_file_entry(entry) for entry in record["file_entries"]):
        raise CheckpointStoreError(
            "invalid_file_entry", "invalid tool change file entry"
        )
    if any(
        not _valid_prepared_file_entry(entry)
        for entry in record["prepared_file_entries"]
    ):
        raise CheckpointStoreError("invalid_file_entry", "invalid prepared file entry")
    try:
        affected_paths = [
            normalize_workspace_relative_path(path)
            for path in record["affected_paths"]
            if isinstance(path, str)
        ]
    except ValueError:
        raise CheckpointStoreError("invalid_path", "invalid affected path") from None
    if affected_paths != record["affected_paths"]:
        raise CheckpointStoreError("invalid_path", "invalid affected path")
    return record


def source_apply_guard_present(workspace_root):
    """Fail closed on an existing source Apply guard without creating state."""
    root = Path(workspace_root)
    info = root.lstat()
    state = workspace_files.read_regular_bytes_anchored(
        root,
        _SOURCE_MUTATION_GUARD_RELATIVE,
        max_bytes=CheckpointStore.MAX_RECORD_BYTES,
        expected_root_identity=(info.st_dev, info.st_ino),
    )
    if not state["exists"]:
        return False
    if len(state["data"]) > CheckpointStore.MAX_RECORD_BYTES:
        raise CheckpointStoreError(
            "source_apply_guard_invalid", "source apply guard is invalid"
        )
    try:
        value = _decode_json(state["data"])
    except CheckpointStoreError as exc:
        raise CheckpointStoreError(
            "source_apply_guard_invalid", "source apply guard is invalid"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.keys() != _SOURCE_MUTATION_GUARD_FIELDS
        or value["record_type"] != "docker_sandbox_source_apply_guard"
        or value["format_version"] != 1
        or _APPLY_ID.fullmatch(str(value["journal_id"])) is None
        or _SANDBOX_ID.fullmatch(str(value["sandbox_id"])) is None
        or _SHA256.fullmatch(str(value["diff_digest"])) is None
    ):
        raise CheckpointStoreError(
            "source_apply_guard_invalid", "source apply guard is invalid"
        )
    return True


def _valid_verification_evidence(item):
    if not isinstance(item, dict) or item.keys() != _VERIFICATION_FIELDS:
        return False
    string_fields = (
        "verification_id",
        "created_at",
        "execution_mode",
        "command",
        "risk_class",
        "status",
        "stdout_tail",
        "stderr_tail",
        "affected_checkpoint_id",
        "trace_event_id",
    )
    return (
        all(isinstance(item.get(key), str) for key in string_fields)
        and isinstance(item.get("argv"), list)
        and all(isinstance(value, str) for value in item["argv"])
        and item.get("runner_executed") is True
        and type(item.get("exit_code")) is int
        and item.get("execution_mode") == "argv"
        and item.get("status") in {"passed", "failed"}
    )


def _valid_prepared_file_entry(item):
    if not isinstance(item, dict):
        return False
    required = {
        "path",
        "before_exists",
        "before_blob_ref",
        "before_hash",
        "before_mode",
    }
    if not required <= item.keys() or type(item["before_exists"]) is not bool:
        return False
    from pony.recovery.paths import normalize_workspace_relative_path

    try:
        if normalize_workspace_relative_path(item["path"]) != item["path"]:
            return False
    except (TypeError, ValueError):
        return False
    if not item["before_exists"]:
        return (
            item["before_blob_ref"] == ""
            and item["before_hash"] == ""
            and item["before_mode"] is None
        )
    mode = item["before_mode"]
    hashes_valid = item["before_hash"] == item["before_blob_ref"] == "" or (
            _looks_like_blob_ref(item["before_hash"])
            and item["before_blob_ref"] == item["before_hash"]
        )
    return (
        hashes_valid and type(mode) is int and mode >= 0 and stat.S_IMODE(mode) == mode
    )


class CheckpointStore:
    MAX_BLOB_BYTES = 8 * 1024 * 1024
    MAX_RECORD_BYTES = 8 * 1024 * 1024

    def __init__(
        self,
        workspace_root,
        redactor=None,
        *,
        source_apply_authority=None,
        source_apply_control_lock=None,
        read_only=False,
    ):
        # workspace_root 通常就是 Pony 的 repo 根。真实存储放在 .pony/checkpoints 下。
        # 如果传入路径已经是 .pony/checkpoints，直接用；否则加子目录。
        workspace_root = Path(workspace_root)
        if (
            workspace_root.name == "checkpoints"
            and workspace_root.parent.name == ".pony"
        ):
            self.root = workspace_root
        else:
            self.root = workspace_root / ".pony" / "checkpoints"
        self._read_only = bool(read_only)
        self._missing = False
        if self._read_only:
            try:
                self.root.lstat()
            except FileNotFoundError:
                self._missing = True
        else:
            self.root = ensure_private_dir(self.root)
        self.records_dir = self.root / "records"
        self.tool_changes_dir = self.root / "tool_changes"
        self.blobs_dir = self.root / "blobs"
        self.quarantine_dir = self.root / "quarantine"
        self.quarantine_checkpoint_dir = self.quarantine_dir / "checkpoint"
        self.quarantine_tool_change_dir = self.quarantine_dir / "tool_change"
        self.lock_path = self.root / ".checkpoint_store.lock"
        self.mutation_lock_path = self.root.parent / ".source-mutation.lock"
        self.source_mutation_guard_path = self.root / "source-apply-guard.json"
        self._redactor = redactor or _identity
        self._source_apply_authority = source_apply_authority
        self._source_apply_control_lock = source_apply_control_lock
        if self._missing:
            self._root_identity = None
            self._records_identity = None
            self._tool_changes_identity = None
            self._blobs_identity = None
            self._quarantine_identities = {
                "checkpoint": None,
                "tool_change": None,
            }
            return
        read_only_identities = {}
        if self._read_only:
            self._root_identity = self._validate_existing_private_directory(self.root)
        for directory in (
            self.records_dir,
            self.tool_changes_dir,
            self.blobs_dir,
            self.quarantine_dir,
            self.quarantine_checkpoint_dir,
            self.quarantine_tool_change_dir,
        ):
            if self._read_only:
                try:
                    read_only_identities[directory] = (
                        self._validate_existing_private_directory(directory)
                    )
                except FileNotFoundError:
                    read_only_identities[directory] = None
            else:
                ensure_private_dir(directory)
        for directory in (self.records_dir, self.tool_changes_dir):
            if self._read_only and read_only_identities[directory] is None:
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        if info.st_nlink != 1:
                            raise ValueError("private file has unsafe link count")
                        require_regular_no_symlink(Path(entry.path))
        if not self._read_only:
            harden_private_tree(self.blobs_dir)
        else:
            self._records_identity = read_only_identities[self.records_dir]
            self._tool_changes_identity = read_only_identities[self.tool_changes_dir]
            self._blobs_identity = read_only_identities[self.blobs_dir]
            self._quarantine_identities = {
                "checkpoint": read_only_identities[self.quarantine_checkpoint_dir],
                "tool_change": read_only_identities[self.quarantine_tool_change_dir],
            }
            return
        self._root_identity = private_directory_identity(self.root)
        self._records_identity = private_directory_identity(self.records_dir)
        self._tool_changes_identity = private_directory_identity(self.tool_changes_dir)
        self._blobs_identity = private_directory_identity(self.blobs_dir)
        self._quarantine_identities = {
            "checkpoint": private_directory_identity(self.quarantine_checkpoint_dir),
            "tool_change": private_directory_identity(self.quarantine_tool_change_dir),
        }

    @staticmethod
    def _validate_existing_private_directory(path):
        path = Path(path)
        info = path.lstat()
        uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
        if (
            path.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or private_directory_identity(path) != (info.st_dev, info.st_ino)
        ):
            raise ValueError("private directory permissions are unsafe")
        return info.st_dev, info.st_ino

    @contextmanager
    def _read_lock(self):
        if self._read_only:
            yield
        else:
            with file_lock.locked_file(self.lock_path):
                yield

    def _read_private_bytes(
        self,
        path,
        *,
        trusted_root,
        trusted_root_identity,
        max_bytes,
    ):
        return read_private_bytes(
            path,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
            max_bytes=max_bytes,
            harden=not self._read_only,
        )

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity

    @contextmanager
    def mutation_lock(self, *, source_apply_journal_id=None):
        if self._read_only:
            raise CheckpointStoreError("read_only_store", "store is read-only")
        if file_lock.lock_is_active(self.lock_path):
            raise RuntimeError("lock order violation")
        external_lock = _null_lock()
        if self._source_apply_control_lock is not None and not file_lock.lock_is_active(
            self._source_apply_control_lock
        ):
            external_lock = file_lock.locked_file(
                self._source_apply_control_lock,
                require_lock=True,
            )
        with external_lock:
            with file_lock.locked_file(self.mutation_lock_path, require_lock=True):
                if (
                    self._source_apply_authority is not None
                    and self._source_apply_authority() is not None
                ):
                    raise CheckpointStoreError(
                        "source_apply_review_required",
                        "source apply must be reconciled before another mutation",
                    )
                guard = self._load_source_mutation_guard()
                if guard is not None and guard["journal_id"] != str(
                    source_apply_journal_id or ""
                ):
                    raise CheckpointStoreError(
                        "source_apply_review_required",
                        "source apply must be reconciled before another mutation",
                    )
                yield

    def _load_source_mutation_guard(self):
        try:
            raw = self._read_private_bytes(
                self.source_mutation_guard_path,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=self.MAX_RECORD_BYTES,
            )
        except FileNotFoundError:
            return None
        try:
            value = _decode_json(raw)
        except CheckpointStoreError as exc:
            raise CheckpointStoreError(
                "source_apply_guard_invalid", "source apply guard is invalid"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.keys() != _SOURCE_MUTATION_GUARD_FIELDS
            or value["record_type"] != "docker_sandbox_source_apply_guard"
            or value["format_version"] != 1
            or _APPLY_ID.fullmatch(str(value["journal_id"])) is None
            or _SANDBOX_ID.fullmatch(str(value["sandbox_id"])) is None
            or _SHA256.fullmatch(str(value["diff_digest"])) is None
        ):
            raise CheckpointStoreError(
                "source_apply_guard_invalid", "source apply guard is invalid"
            )
        return value

    def begin_source_apply_guard(self, *, journal_id, sandbox_id, diff_digest):
        if not file_lock.lock_is_active(self.mutation_lock_path):
            raise RuntimeError("source apply guard requires mutation lock")
        value = {
            "record_type": "docker_sandbox_source_apply_guard",
            "format_version": 1,
            "journal_id": str(journal_id),
            "sandbox_id": str(sandbox_id),
            "diff_digest": str(diff_digest),
        }
        if (
            _APPLY_ID.fullmatch(value["journal_id"]) is None
            or _SANDBOX_ID.fullmatch(value["sandbox_id"]) is None
            or _SHA256.fullmatch(value["diff_digest"]) is None
        ):
            raise CheckpointStoreError(
                "source_apply_guard_invalid", "source apply guard is invalid"
            )
        current = self._load_source_mutation_guard()
        if current is not None and current != value:
            raise CheckpointStoreError(
                "source_apply_review_required", "another source apply is unresolved"
            )
        if current is None:
            write_private_bytes_atomic(
                self.source_mutation_guard_path,
                json.dumps(
                    value,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii"),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                fsync_file=self._fsync_file,
                fsync_parent=self._fsync_parent,
                max_existing_bytes=self.MAX_RECORD_BYTES,
            )
        return value

    def source_apply_guard(self):
        value = self._load_source_mutation_guard()
        return None if value is None else deepcopy(value)

    def finish_source_apply_guard(self, *, journal_id, allow_missing=False):
        if not file_lock.lock_is_active(self.mutation_lock_path):
            raise RuntimeError("source apply guard requires mutation lock")
        current = self._load_source_mutation_guard()
        if current is None and allow_missing:
            return False
        if current is None or current["journal_id"] != str(journal_id):
            raise CheckpointStoreError(
                "source_apply_guard_invalid", "source apply guard does not match"
            )
        path, parent = private_files._open_private_parent(
            self.source_mutation_guard_path,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        try:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise CheckpointStoreError(
                    "source_apply_guard_invalid", "source apply guard is invalid"
                )
            os.unlink(path.name, dir_fd=parent)
            self._fsync_parent(parent)
        finally:
            os.close(parent)
        return True

    # -- blob 存取 --------------------------------------------------------
    def _blob_path(self, content_hash):
        if not _looks_like_blob_ref(content_hash):
            raise ValueError("invalid_blob_ref")
        return self.blobs_dir / content_hash[:2] / content_hash

    def write_blob(self, data, content_kind="text"):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("write_blob requires bytes-like data")
        if len(data) > self.MAX_BLOB_BYTES:
            raise ValueError("blob_too_large")
        info = hash_bytes(bytes(data))
        blob_ref = info["content_hash"]
        blob_path = self._blob_path(blob_ref)
        with file_lock.locked_file(self.lock_path):
            ensure_private_dir(blob_path.parent)
            checked_path = require_regular_no_symlink(blob_path, allow_missing=True)
            try:
                checked_path.lstat()
            except FileNotFoundError:
                self._write_bytes_atomic(checked_path, bytes(data))
            else:
                if self.read_blob(blob_ref) != bytes(data):
                    raise ValueError("blob_hash_mismatch")
        return {
            "blob_ref": blob_ref,
            "content_hash": blob_ref,
            "hash_algorithm": info["hash_algorithm"],
            "size_bytes": info["size_bytes"],
            "content_kind": content_kind,
        }

    def read_blob(self, blob_ref):
        try:
            data = self._read_private_bytes(
                self._blob_path(str(blob_ref)),
                trusted_root=self.blobs_dir,
                trusted_root_identity=self._blobs_identity,
                max_bytes=self.MAX_BLOB_BYTES,
            )
        except ValueError as exc:
            if str(exc) == "private file too large":
                raise ValueError("blob_too_large") from None
            raise
        if hashlib.sha256(data).hexdigest() != str(blob_ref):
            raise ValueError("blob_hash_mismatch")
        return data

    def has_blob(self, blob_ref):
        try:
            self.read_blob(blob_ref)
        except FileNotFoundError:
            return False
        return True

    def blob_exists(self, blob_ref):
        """Check that an immutable blob is still a safe regular file."""
        if self._missing or self._blobs_identity is None:
            return False
        path = self._blob_path(str(blob_ref))
        with self._read_lock():
            try:
                path, parent = private_files._open_private_parent(
                    path,
                    trusted_root=self.blobs_dir,
                    trusted_root_identity=self._blobs_identity,
                )
            except FileNotFoundError:
                return False
            try:
                try:
                    info = os.stat(
                        path.name,
                        dir_fd=parent,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return False
                uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != uid
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > self.MAX_BLOB_BYTES
                ):
                    raise ValueError("blob file is unsafe")
                return True
            finally:
                os.close(parent)

    # -- checkpoint record ------------------------------------------------
    def _record_path(self, checkpoint_id):
        return self.records_dir / (_safe_id(checkpoint_id, "checkpoint id") + ".json")

    def write_checkpoint_record(self, record):
        self.create_checkpoint_record(record)
        return self._record_path(record["checkpoint_id"])

    def create_checkpoint_record(self, record):
        checkpoint_id = record["checkpoint_id"]
        path = self._record_path(checkpoint_id)
        with file_lock.locked_file(self.lock_path):
            return self._write_json_atomic(
                path, record, kind="checkpoint", expected_id=checkpoint_id
            )

    def load_checkpoint_record(self, checkpoint_id):
        with self._read_lock():
            return self._load_checkpoint_record_unlocked(checkpoint_id)

    def _load_checkpoint_record_unlocked(self, checkpoint_id):
        record, _ = self._load_checkpoint_record_snapshot_unlocked(checkpoint_id)
        return record

    def load_checkpoint_record_snapshot(self, checkpoint_id):
        with self._read_lock():
            return self._load_checkpoint_record_snapshot_unlocked(checkpoint_id)

    def _load_checkpoint_record_snapshot_unlocked(self, checkpoint_id):
        data = self._read_private_bytes(
            self._record_path(checkpoint_id),
            trusted_root=self.records_dir,
            trusted_root_identity=self._records_identity,
            max_bytes=self.MAX_RECORD_BYTES,
        )
        record = _decode_json(data)
        record = _validate_checkpoint_record(
            record, expected_id=_safe_id(checkpoint_id, "checkpoint id")
        )
        return record, data

    def list_checkpoint_records(self, strict=False):
        if self._records_identity is None:
            return []
        with self._read_lock():
            return self._list_checkpoint_records_unlocked(strict=strict)

    def _list_checkpoint_records_unlocked(self, strict=False):
        records = self._list_records(
            self.records_dir,
            self._records_identity,
            kind="checkpoint",
            strict=strict,
        )
        records.sort(key=lambda item: item.get("created_at", ""))
        return records

    # -- tool change record ----------------------------------------------
    def _tool_change_path(self, tool_change_id):
        return self.tool_changes_dir / (
            _safe_id(tool_change_id, "tool change id") + ".json"
        )

    def write_tool_change_record(self, record):
        self.create_tool_change_record(record)
        return self._tool_change_path(record["tool_change_id"])

    def create_tool_change_record(self, record):
        tool_change_id = record["tool_change_id"]
        path = self._tool_change_path(tool_change_id)
        with file_lock.locked_file(self.lock_path):
            return self._write_json_atomic(
                path, record, kind="tool_change", expected_id=tool_change_id
            )

    def load_tool_change_record(self, tool_change_id):
        with self._read_lock():
            return self._load_tool_change_record_unlocked(tool_change_id)

    def _load_tool_change_record_unlocked(self, tool_change_id):
        record, _ = self._load_tool_change_record_snapshot_unlocked(tool_change_id)
        return record

    def load_tool_change_record_snapshot(self, tool_change_id):
        with self._read_lock():
            return self._load_tool_change_record_snapshot_unlocked(tool_change_id)

    def _load_tool_change_record_snapshot_unlocked(self, tool_change_id):
        data = self._read_private_bytes(
            self._tool_change_path(tool_change_id),
            trusted_root=self.tool_changes_dir,
            trusted_root_identity=self._tool_changes_identity,
            max_bytes=self.MAX_RECORD_BYTES,
        )
        record = _decode_json(data)
        record = _validate_tool_change_record(
            record,
            expected_id=_safe_id(tool_change_id, "tool change id"),
        )
        return record, data

    def update_checkpoint_record(
        self, checkpoint_id, transform, *, expected_status=None
    ):
        checkpoint_id = _safe_id(checkpoint_id, "checkpoint id")
        with file_lock.locked_file(self.lock_path, require_lock=True):
            record = self._load_checkpoint_record_unlocked(checkpoint_id)
            if expected_status is not None and record.get("status") != expected_status:
                raise CheckpointStoreError(
                    "status_conflict", "checkpoint status conflict"
                )
            updated = transform(deepcopy(record))
            return self._write_json_atomic(
                self._record_path(checkpoint_id),
                updated,
                kind="checkpoint",
                expected_id=checkpoint_id,
            )

    def update_checkpoint_record_if_hash(
        self,
        checkpoint_id,
        expected_record_hash,
        transform,
        *,
        expected_status=None,
    ):
        checkpoint_id = _safe_id(checkpoint_id, "checkpoint id")
        with file_lock.locked_file(self.lock_path, require_lock=True):
            record, data = self._load_checkpoint_record_snapshot_unlocked(checkpoint_id)
            if hashlib.sha256(data).hexdigest() != expected_record_hash:
                raise CheckpointStoreError("record_changed", "record changed")
            if expected_status is not None and record.get("status") != expected_status:
                raise CheckpointStoreError(
                    "status_conflict", "checkpoint status conflict"
                )
            updated = transform(deepcopy(record))
            return self._write_json_atomic(
                self._record_path(checkpoint_id),
                updated,
                kind="checkpoint",
                expected_id=checkpoint_id,
            )

    def update_tool_change_record(
        self, tool_change_id, transform, *, expected_status=None
    ):
        tool_change_id = _safe_id(tool_change_id, "tool change id")
        with file_lock.locked_file(self.lock_path, require_lock=True):
            record = self._load_tool_change_record_unlocked(tool_change_id)
            if expected_status is not None and record.get("status") != expected_status:
                raise CheckpointStoreError(
                    "status_conflict", "tool change status conflict"
                )
            updated = transform(deepcopy(record))
            return self._write_json_atomic(
                self._tool_change_path(tool_change_id),
                updated,
                kind="tool_change",
                expected_id=tool_change_id,
            )

    def update_tool_change_record_if_hash(
        self,
        tool_change_id,
        expected_record_hash,
        transform,
        *,
        expected_status=None,
    ):
        tool_change_id = _safe_id(tool_change_id, "tool change id")
        with file_lock.locked_file(self.lock_path, require_lock=True):
            record, data = self._load_tool_change_record_snapshot_unlocked(
                tool_change_id
            )
            if hashlib.sha256(data).hexdigest() != expected_record_hash:
                raise CheckpointStoreError("record_changed", "record changed")
            if expected_status is not None and record.get("status") != expected_status:
                raise CheckpointStoreError(
                    "status_conflict", "tool change status conflict"
                )
            updated = transform(deepcopy(record))
            return self._write_json_atomic(
                self._tool_change_path(tool_change_id),
                updated,
                kind="tool_change",
                expected_id=tool_change_id,
            )

    def list_tool_change_records(self, strict=False):
        if self._tool_changes_identity is None:
            return []
        with self._read_lock():
            return self._list_tool_change_records_unlocked(strict=strict)

    def _list_tool_change_records_unlocked(self, strict=False):
        records = self._list_records(
            self.tool_changes_dir,
            self._tool_changes_identity,
            kind="tool_change",
            strict=strict,
        )
        records.sort(key=lambda item: item.get("started_at", ""))
        return records

    def validate_tool_change_reference_graph(self, tool_changes_dir=None):
        """Validate Checkpoint -> Tool Change -> Blob references without writes."""
        directory = Path(tool_changes_dir or self.tool_changes_dir)
        with file_lock.locked_file(self.lock_path, require_lock=True):
            checkpoints = self._list_checkpoint_records_unlocked(strict=True)
            identity = (
                self._tool_changes_identity
                if directory == self.tool_changes_dir
                else private_directory_identity(directory)
            )
            tool_changes = self._list_records(
                directory,
                identity,
                kind="tool_change",
                strict=True,
            )
            tool_changes_by_id = {
                record["tool_change_id"]: record for record in tool_changes
            }

            for tool_change_id, record in tool_changes_by_id.items():
                for entry in record["file_entries"]:
                    if entry["source_tool_change_ids"] not in (
                        [],
                        [tool_change_id],
                    ):
                        raise CheckpointStoreError(
                            "reference_graph_mismatch",
                            "tool change file entry has a foreign source",
                        )

            from pony.recovery.checkpoint_writer import coalesce_file_entries

            for checkpoint in checkpoints:
                tool_change_ids = checkpoint["tool_change_ids"]
                missing_ids = checkpoint["missing_tool_change_ids"]
                if (
                    len(set(tool_change_ids)) != len(tool_change_ids)
                    or len(set(missing_ids)) != len(missing_ids)
                    or set(tool_change_ids) & set(missing_ids)
                ):
                    raise CheckpointStoreError(
                        "reference_graph_mismatch",
                        "checkpoint tool change references are ambiguous",
                    )
                linked = []
                for tool_change_id in tool_change_ids:
                    tool_change = tool_changes_by_id.get(tool_change_id)
                    if tool_change is None:
                        raise CheckpointStoreError(
                            "dangling_reference",
                            "checkpoint tool change reference does not resolve",
                        )
                    if tool_change["checkpoint_id"] != checkpoint["checkpoint_id"]:
                        raise CheckpointStoreError(
                            "reference_graph_mismatch",
                            "checkpoint and tool change links disagree",
                        )
                    linked.extend(tool_change["file_entries"])
                known_sources = set(tool_change_ids)
                if any(
                    source_id not in known_sources
                    for entry in checkpoint["file_entries"]
                    for source_id in entry["source_tool_change_ids"]
                ):
                    raise CheckpointStoreError(
                        "reference_graph_mismatch",
                        "checkpoint file entry has an unknown source",
                    )
                if tool_change_ids and (
                    coalesce_file_entries(linked) != checkpoint["file_entries"]
                ):
                    raise CheckpointStoreError(
                        "reference_graph_mismatch",
                        "checkpoint file entries do not match tool changes",
                    )
                if any(
                    tool_changes_by_id[tool_change_id]["status"]
                    not in {"pending", "legacy_migrated"}
                    for tool_change_id in missing_ids
                    if tool_change_id in tool_changes_by_id
                ):
                    raise CheckpointStoreError(
                        "reference_graph_mismatch",
                        "available tool change is declared missing",
                    )

            for blob_ref in _referenced_blob_refs(checkpoints, tool_changes):
                try:
                    self.read_blob(blob_ref)
                except FileNotFoundError:
                    raise CheckpointStoreError(
                        "dangling_reference", "referenced blob does not exist"
                    ) from None
            return len(tool_changes)

    # -- pruning ----------------------------------------------------------
    def prune(self, dry_run=True, older_than=None, now=None):
        if self._read_only:
            if not dry_run:
                raise CheckpointStoreError("read_only_store", "store is read-only")
            if self._missing:
                cutoff = _cutoff_datetime(older_than, now=now)
                return {
                    "dry_run": True,
                    "older_than": str(older_than or ""),
                    "cutoff_created_before": (
                        cutoff.isoformat() if cutoff is not None else ""
                    ),
                    "prunable_checkpoint_ids": [],
                    "prunable_tool_change_ids": [],
                    "removed_checkpoint_ids": [],
                    "removed_tool_change_ids": [],
                    "referenced_count": 0,
                    "unreferenced_blob_refs": [],
                    "removed_blob_refs": [],
                }
            return self._prune_locked(
                dry_run=True,
                older_than=older_than,
                now=now,
            )
        with self.mutation_lock():
            with file_lock.locked_file(self.lock_path, require_lock=True):
                return self._prune_locked(
                    dry_run=dry_run, older_than=older_than, now=now
                )

    def _prune_locked(self, dry_run=True, older_than=None, now=None):
        """扫描所有 blob 引用，返回未被引用的 blob。dry_run=False 时才真的删除。

        引用来源必须囊括：
          - checkpoint record 的 file_entries
          - checkpoint record 的 restore_provenance.pre_restore_file_states 与 post_...
          - tool change record 的 file_entries
        任何一处漏扫，都会误删仍被引用的 blob。
        """
        checkpoint_records = self._list_checkpoint_records_unlocked(strict=True)
        tool_change_records = self._list_tool_change_records_unlocked(strict=True)
        cutoff = _cutoff_datetime(older_than, now=now)
        prunable_checkpoint_ids = _prunable_checkpoint_ids(checkpoint_records, cutoff)
        prunable_checkpoint_id_set = set(prunable_checkpoint_ids)
        retained_checkpoint_records = [
            record
            for record in checkpoint_records
            if record.get("checkpoint_id") not in prunable_checkpoint_id_set
        ]
        retained_tool_change_ids = {
            tool_change_id
            for record in retained_checkpoint_records
            for tool_change_id in (record.get("tool_change_ids", []) or [])
            if tool_change_id
        }
        candidate_tool_change_ids = {
            tool_change_id
            for record in checkpoint_records
            if record.get("checkpoint_id") in prunable_checkpoint_id_set
            for tool_change_id in (record.get("tool_change_ids", []) or [])
            if tool_change_id
        }
        existing_checkpoint_ids = {
            record.get("checkpoint_id") for record in checkpoint_records
        }
        orphan_tool_change_ids = {
            record.get("tool_change_id")
            for record in tool_change_records
            if record.get("checkpoint_id")
            and record.get("checkpoint_id") not in existing_checkpoint_ids
            and record.get("status") != "pending"
            and cutoff is not None
            and (
                _parse_created_at(record.get("ended_at") or record.get("started_at"))
                or datetime.max.replace(tzinfo=timezone.utc)
            )
            < cutoff
        }
        candidate_tool_change_ids.update(orphan_tool_change_ids)
        prunable_tool_change_ids = sorted(
            candidate_tool_change_ids - retained_tool_change_ids
        )
        prunable_tool_change_id_set = set(prunable_tool_change_ids)
        retained_tool_change_records = [
            record
            for record in tool_change_records
            if record.get("tool_change_id") not in prunable_tool_change_id_set
        ]

        referenced = _referenced_blob_refs(
            retained_checkpoint_records, retained_tool_change_records
        )

        unreferenced = []
        for blob_ref in self._list_blob_refs():
            if blob_ref in referenced:
                continue
            unreferenced.append(blob_ref)

        removed_checkpoint_ids = []
        removed_tool_change_ids = []
        removed = []
        if not dry_run:
            for checkpoint_id in prunable_checkpoint_ids:
                self._unlink_store_file(
                    self.records_dir,
                    self._records_identity,
                    _safe_id(checkpoint_id, "checkpoint id") + ".json",
                )
                removed_checkpoint_ids.append(checkpoint_id)
            for tool_change_id in prunable_tool_change_ids:
                self._unlink_store_file(
                    self.tool_changes_dir,
                    self._tool_changes_identity,
                    _safe_id(tool_change_id, "tool change id") + ".json",
                )
                removed_tool_change_ids.append(tool_change_id)
            for blob_ref in unreferenced:
                try:
                    self._unlink_blob(blob_ref)
                    removed.append(blob_ref)
                except OSError:
                    continue

        return {
            "dry_run": bool(dry_run),
            "older_than": str(older_than or ""),
            "cutoff_created_before": cutoff.isoformat() if cutoff is not None else "",
            "prunable_checkpoint_ids": prunable_checkpoint_ids,
            "prunable_tool_change_ids": prunable_tool_change_ids,
            "removed_checkpoint_ids": removed_checkpoint_ids,
            "removed_tool_change_ids": removed_tool_change_ids,
            "referenced_count": len(referenced),
            "unreferenced_blob_refs": unreferenced,
            "removed_blob_refs": removed,
        }

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _fsync_file(descriptor):
        os.fsync(descriptor)

    @staticmethod
    def _fsync_parent(descriptor):
        os.fsync(descriptor)

    @staticmethod
    def _open_store_directory(directory, expected_identity):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != tuple(expected_identity):
                raise ValueError("private root changed")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _list_records(self, directory, identity, *, kind, strict):
        records, _ = self._scan_records(
            directory,
            identity,
            kind=kind,
            strict=strict,
        )
        return records

    @staticmethod
    def _opaque_invalid_id(kind, relative_path, evidence):
        body = (
            kind.encode("ascii")
            + b"\0"
            + relative_path.as_posix().encode("utf-8")
            + b"\0"
            + evidence
        )
        return "invalid_" + hashlib.sha256(body).hexdigest()

    @staticmethod
    def _non_regular_evidence(parent_descriptor, name, info):
        payload = {
            "file_type": stat.S_IFMT(info.st_mode),
            "mode": info.st_mode,
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
            "link_target_b64": "",
        }
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=parent_descriptor)
            payload["link_target_b64"] = base64.b64encode(os.fsencode(target)).decode(
                "ascii"
            )
        after = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (info.st_dev, info.st_ino, info.st_mode) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
        ):
            raise CheckpointStoreError("record_changed", "record inode changed")
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _read_record_candidate(self, parent_descriptor, name):
        before = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        identity = (before.st_dev, before.st_ino, before.st_mode)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            evidence = self._non_regular_evidence(parent_descriptor, name, before)
            return None, evidence, identity, "inode"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            current = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise CheckpointStoreError("record_changed", "record inode changed")
            chunks = []
            remaining = self.MAX_RECORD_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > self.MAX_RECORD_BYTES:
                evidence = self._non_regular_evidence(parent_descriptor, name, current)
                return None, evidence, identity, "inode"
            return raw, raw, identity, "raw"
        finally:
            os.close(descriptor)

    def _scan_records(self, directory, identity, *, kind, strict):
        descriptor = self._open_store_directory(directory, identity)
        try:
            with os.scandir(descriptor) as entries:
                names = sorted(
                    entry.name for entry in entries if entry.name.endswith(".json")
                )
            records = []
            invalid = []
            strict_invalid = False
            for name in names:
                relative = Path(directory.name) / name
                raw = None
                evidence = b""
                candidate_identity = None
                evidence_kind = "inode"
                try:
                    raw, evidence, candidate_identity, evidence_kind = (
                        self._read_record_candidate(descriptor, name)
                    )
                    if raw is None:
                        raise CheckpointStoreError(
                            "invalid_record_type", "invalid record type"
                        )
                    expected_id = _safe_id(name[:-5], f"{kind} id")
                    record = _decode_json(raw)
                    validator = (
                        _validate_checkpoint_record
                        if kind == "checkpoint"
                        else _validate_tool_change_record
                    )
                    records.append(validator(record, expected_id=expected_id))
                except (
                    OSError,
                    UnicodeDecodeError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    if strict:
                        strict_invalid = True
                        break
                    if not evidence:
                        try:
                            info = os.stat(
                                name,
                                dir_fd=descriptor,
                                follow_symlinks=False,
                            )
                            evidence = self._non_regular_evidence(
                                descriptor, name, info
                            )
                            candidate_identity = (
                                info.st_dev,
                                info.st_ino,
                                info.st_mode,
                            )
                            evidence_kind = "inode"
                        except OSError:
                            continue
                    raw_hash = hashlib.sha256(evidence).hexdigest()
                    opaque_id = self._opaque_invalid_id(kind, relative, evidence)
                    placeholder = {
                        "opaque_id": opaque_id,
                        "record_kind": kind,
                        "status": "invalid_record",
                        "raw_hash": raw_hash,
                        "quarantinable": True,
                    }
                    records.append(placeholder)
                    invalid.append(
                        {
                            **placeholder,
                            "name": name,
                            "identity": candidate_identity,
                            "evidence_kind": evidence_kind,
                        }
                    )
            if strict_invalid:
                raise CheckpointStoreError("invalid_record", "invalid record") from None
            return records, invalid
        finally:
            os.close(descriptor)

    def quarantine_invalid_record(self, opaque_id, *, expected_raw_hash):
        opaque_id = str(opaque_id)
        if not re.fullmatch(r"invalid_[0-9a-f]{64}", opaque_id):
            raise CheckpointStoreError(
                "invalid_record_changed", "invalid record changed"
            )
        with self.mutation_lock():
            with file_lock.locked_file(self.lock_path, require_lock=True):
                found = []
                for kind, directory, identity in (
                    ("checkpoint", self.records_dir, self._records_identity),
                    (
                        "tool_change",
                        self.tool_changes_dir,
                        self._tool_changes_identity,
                    ),
                ):
                    _, candidates = self._scan_records(
                        directory,
                        identity,
                        kind=kind,
                        strict=False,
                    )
                    found.extend(
                        (directory, identity, item)
                        for item in candidates
                        if item["opaque_id"] == opaque_id
                        and item["raw_hash"] == str(expected_raw_hash)
                    )
                if len(found) != 1:
                    raise CheckpointStoreError(
                        "invalid_record_changed", "invalid record changed"
                    )
                source_dir, source_identity, item = found[0]
                kind = item["record_kind"]
                quarantine_dir = (
                    self.quarantine_checkpoint_dir
                    if kind == "checkpoint"
                    else self.quarantine_tool_change_dir
                )
                quarantine_identity = self._quarantine_identities[kind]
                suffix = ".raw" if item["evidence_kind"] == "raw" else ".inode"
                evidence_name = opaque_id + suffix
                self._move_quarantine_candidate(
                    source_dir,
                    source_identity,
                    item["name"],
                    item["identity"],
                    quarantine_dir,
                    quarantine_identity,
                    evidence_name,
                    regular=item["evidence_kind"] == "raw",
                    expected_raw_hash=item["raw_hash"],
                )
                relative_base = Path("quarantine") / kind
                metadata = {
                    "opaque_id": opaque_id,
                    "record_kind": kind,
                    "status": "quarantined",
                    "evidence_kind": item["evidence_kind"],
                    "raw_hash": item["raw_hash"],
                    "quarantined_at": datetime.now(timezone.utc).isoformat(),
                    "quarantine_metadata_path": str(
                        relative_base / (opaque_id + ".json")
                    ),
                }
                key = (
                    "quarantine_raw_path"
                    if item["evidence_kind"] == "raw"
                    else "quarantine_evidence_path"
                )
                metadata[key] = str(relative_base / evidence_name)
                return self._write_json_atomic(
                    quarantine_dir / (opaque_id + ".json"), metadata
                )

    def _move_quarantine_candidate(
        self,
        source_dir,
        source_identity,
        source_name,
        expected_identity,
        destination_dir,
        destination_identity,
        destination_name,
        *,
        regular,
        expected_raw_hash,
    ):
        source_descriptor = self._open_store_directory(source_dir, source_identity)
        destination_descriptor = self._open_store_directory(
            destination_dir, destination_identity
        )
        try:
            current = os.stat(
                source_name,
                dir_fd=source_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino, current.st_mode) != tuple(
                expected_identity
            ):
                raise CheckpointStoreError(
                    "invalid_record_changed", "invalid record changed"
                )
            if regular:
                raw, _, _, evidence_kind = self._read_record_candidate(
                    source_descriptor, source_name
                )
                if (
                    evidence_kind != "raw"
                    or raw is None
                    or hashlib.sha256(raw).hexdigest() != expected_raw_hash
                ):
                    raise CheckpointStoreError(
                        "invalid_record_changed", "invalid record changed"
                    )
            try:
                os.stat(
                    destination_name,
                    dir_fd=destination_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise CheckpointStoreError(
                    "quarantine_exists", "quarantine evidence exists"
                )
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_descriptor,
                dst_dir_fd=destination_descriptor,
            )
            installed = os.stat(
                destination_name,
                dir_fd=destination_descriptor,
                follow_symlinks=False,
            )
            if (installed.st_dev, installed.st_ino, installed.st_mode) != tuple(
                expected_identity
            ):
                raise CheckpointStoreError("record_changed", "quarantine inode changed")
            if regular:
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                evidence_descriptor = os.open(
                    destination_name,
                    flags,
                    dir_fd=destination_descriptor,
                )
                try:
                    opened = os.fstat(evidence_descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino, opened.st_mode)
                        != tuple(expected_identity)
                    ):
                        raise CheckpointStoreError(
                            "invalid_record_changed", "invalid record changed"
                        )
                    chunks = []
                    remaining = self.MAX_RECORD_BYTES + 1
                    while remaining:
                        chunk = os.read(
                            evidence_descriptor,
                            min(65536, remaining),
                        )
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    if (
                        len(raw) > self.MAX_RECORD_BYTES
                        or hashlib.sha256(raw).hexdigest() != expected_raw_hash
                    ):
                        raise CheckpointStoreError(
                            "invalid_record_changed", "invalid record changed"
                        )
                    os.fchmod(evidence_descriptor, 0o600)
                finally:
                    os.close(evidence_descriptor)
            os.fsync(source_descriptor)
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
            os.close(source_descriptor)

    def list_quarantined_records(self):
        if self._missing:
            return []
        records = []
        for kind, directory in (
            ("checkpoint", self.quarantine_checkpoint_dir),
            ("tool_change", self.quarantine_tool_change_dir),
        ):
            identity = self._quarantine_identities[kind]
            if identity is None:
                continue
            descriptor = self._open_store_directory(directory, identity)
            try:
                with os.scandir(descriptor) as entries:
                    names = sorted(
                        entry.name for entry in entries if entry.name.endswith(".json")
                    )
            finally:
                os.close(descriptor)
            for name in names:
                try:
                    data = self._read_private_bytes(
                        directory / name,
                        trusted_root=directory,
                        trusted_root_identity=identity,
                        max_bytes=self.MAX_RECORD_BYTES,
                    )
                    record = json.loads(data.decode("utf-8"))
                    safe = self._validated_quarantine_metadata(
                        record, kind=kind, name=name
                    )
                    if safe is None:
                        raise ValueError("invalid quarantine metadata")
                    records.append(safe)
                except (
                    OSError,
                    TypeError,
                    UnicodeDecodeError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    continue
        return sorted(records, key=lambda item: item.get("quarantined_at", ""))

    @staticmethod
    def _validated_quarantine_metadata(record, *, kind, name):
        if not isinstance(record, dict):
            return None
        opaque_id = record.get("opaque_id")
        raw_hash = record.get("raw_hash")
        evidence_kind = record.get("evidence_kind")
        if (
            not isinstance(opaque_id, str)
            or re.fullmatch(r"invalid_[0-9a-f]{64}", opaque_id) is None
            or name != opaque_id + ".json"
            or record.get("record_kind") != kind
            or record.get("status") != "quarantined"
            or evidence_kind not in {"raw", "inode"}
            or not isinstance(raw_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", raw_hash) is None
            or not isinstance(record.get("quarantined_at"), str)
        ):
            return None
        base = Path("quarantine") / kind
        metadata_path = str(base / (opaque_id + ".json"))
        evidence_key = (
            "quarantine_raw_path"
            if evidence_kind == "raw"
            else "quarantine_evidence_path"
        )
        evidence_suffix = ".raw" if evidence_kind == "raw" else ".inode"
        evidence_path = str(base / (opaque_id + evidence_suffix))
        if (
            record.get("quarantine_metadata_path") != metadata_path
            or record.get(evidence_key) != evidence_path
        ):
            return None
        return {
            "opaque_id": opaque_id,
            "record_kind": kind,
            "status": "quarantined",
            "evidence_kind": evidence_kind,
            "raw_hash": raw_hash,
            "quarantined_at": record["quarantined_at"],
            "quarantine_metadata_path": metadata_path,
            evidence_key: evidence_path,
        }

    def _list_blob_refs(self):
        root_descriptor = self._open_store_directory(
            self.blobs_dir, self._blobs_identity
        )
        refs = []
        try:
            with os.scandir(root_descriptor) as entries:
                buckets = sorted(
                    entry.name
                    for entry in entries
                    if len(entry.name) == 2
                    and all(char in "0123456789abcdef" for char in entry.name)
                )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            for bucket in buckets:
                try:
                    descriptor = os.open(bucket, flags, dir_fd=root_descriptor)
                except OSError:
                    continue
                try:
                    with os.scandir(descriptor) as entries:
                        names = sorted(entry.name for entry in entries)
                    for name in names:
                        if not _looks_like_blob_ref(name) or not name.startswith(
                            bucket
                        ):
                            continue
                        try:
                            self.read_blob(name)
                        except (OSError, ValueError):
                            continue
                        refs.append(name)
                finally:
                    os.close(descriptor)
        finally:
            os.close(root_descriptor)
        return refs

    def _unlink_store_file(self, directory, identity, name):
        descriptor = self._open_store_directory(directory, identity)
        try:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise ValueError("unsafe store file")
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _unlink_blob(self, blob_ref):
        blob_ref = str(blob_ref)
        if not _looks_like_blob_ref(blob_ref):
            raise ValueError("invalid_blob_ref")
        root_descriptor = self._open_store_directory(
            self.blobs_dir, self._blobs_identity
        )
        bucket_descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            bucket_descriptor = os.open(blob_ref[:2], flags, dir_fd=root_descriptor)
            current = os.stat(
                blob_ref,
                dir_fd=bucket_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise ValueError("unsafe blob")
            os.unlink(blob_ref, dir_fd=bucket_descriptor)
            os.fsync(bucket_descriptor)
        finally:
            if bucket_descriptor >= 0:
                os.close(bucket_descriptor)
            os.close(root_descriptor)

    def _write_json_atomic(self, path, payload, *, kind=None, expected_id=None):
        if kind == "checkpoint":
            _validate_checkpoint_record(payload, expected_id=expected_id)
        elif kind == "tool_change":
            _validate_tool_change_record(payload, expected_id=expected_id)
        safe_payload = self._redactor(deepcopy(payload))
        if kind == "checkpoint":
            _validate_checkpoint_record(safe_payload, expected_id=expected_id)
        elif kind == "tool_change":
            _validate_tool_change_record(safe_payload, expected_id=expected_id)
        data = (json.dumps(safe_payload, indent=2, sort_keys=True) + "\n").encode()
        if len(data) > self.MAX_RECORD_BYTES:
            raise ValueError("private file too large")
        canonical_payload = _decode_json(data)
        if kind == "checkpoint":
            _validate_checkpoint_record(canonical_payload, expected_id=expected_id)
        elif kind == "tool_change":
            _validate_tool_change_record(canonical_payload, expected_id=expected_id)
        if path.parent == self.records_dir:
            root, identity = self.records_dir, self._records_identity
        elif path.parent == self.tool_changes_dir:
            root, identity = self.tool_changes_dir, self._tool_changes_identity
        else:
            root, identity = self.root, self._root_identity
        write_private_bytes_atomic(
            path,
            data,
            trusted_root=root,
            trusted_root_identity=identity,
            error="unsafe checkpoint temp changed",
            fsync_file=self._fsync_file,
            fsync_parent=self._fsync_parent,
            max_existing_bytes=self.MAX_RECORD_BYTES,
        )
        return canonical_payload

    def _write_bytes_atomic(self, path, data):
        write_private_bytes_atomic(
            path,
            data,
            trusted_root=self.blobs_dir,
            trusted_root_identity=self._blobs_identity,
            error="unsafe checkpoint temp changed",
            fsync_file=self._fsync_file,
            fsync_parent=self._fsync_parent,
            max_existing_bytes=self.MAX_BLOB_BYTES,
        )

    @staticmethod
    def _verify_temp(path, identity):
        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
        ):
            raise ValueError("checkpoint temp changed")

    @staticmethod
    def _verify_installed(path, identity):
        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
        ):
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) == identity
            ):
                path.unlink()
            raise ValueError("checkpoint temp changed")

    @staticmethod
    def _remove_temp(path, identity):
        if path is None or identity is None:
            return
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()


def _collect_blob_refs(entry, sink):
    if not isinstance(entry, dict):
        return
    for key in ("before_blob_ref", "after_blob_ref", "blob_ref"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            sink.add(value)


def _referenced_blob_refs(checkpoint_records, tool_change_records):
    referenced = set()
    for record in checkpoint_records:
        for entry in record.get("file_entries", []) or []:
            _collect_blob_refs(entry, referenced)
        provenance = record.get("restore_provenance") or {}
        for entry in provenance.get("pre_restore_file_states", []) or []:
            _collect_blob_refs(entry, referenced)
        for entry in provenance.get("post_restore_file_states", []) or []:
            _collect_blob_refs(entry, referenced)
        for intent in provenance.get("entries", []) or []:
            for key in ("pre_state", "planned_post_state", "actual_post_state"):
                _collect_state_blob_ref(intent.get(key), referenced)
    for record in tool_change_records:
        for entry in record.get("file_entries", []) or []:
            _collect_blob_refs(entry, referenced)
        for entry in record.get("prepared_file_entries", []) or []:
            _collect_blob_refs(entry, referenced)
    return referenced


def _collect_state_blob_ref(state, sink):
    if not isinstance(state, dict):
        return
    value = state.get("blob_ref")
    if isinstance(value, str) and value:
        sink.add(value)


def _cutoff_datetime(older_than, now=None):
    if not older_than:
        return None
    duration = _parse_duration(older_than)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - duration


def _parse_duration(value):
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", str(value or "").strip())
    if not match:
        raise ValueError("older_than must use a duration like 7d, 12h, or 30m")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(weeks=amount)


def _prunable_checkpoint_ids(checkpoint_records, cutoff):
    if cutoff is None:
        return []
    parent_refs = {
        str(record.get("parent_checkpoint_id") or "")
        for record in checkpoint_records
        if record.get("parent_checkpoint_id")
    }
    prunable = []
    for record in checkpoint_records:
        checkpoint_id = str(record.get("checkpoint_id") or "")
        if not checkpoint_id or checkpoint_id in parent_refs:
            continue
        created_at = _parse_created_at(record.get("created_at", ""))
        if created_at is not None and created_at < cutoff:
            prunable.append(checkpoint_id)
    return prunable


def _parse_created_at(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _looks_like_blob_ref(value):
    text = str(value)
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)
