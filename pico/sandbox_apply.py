"""Trusted Docker Sandbox captures, immutable diffs, and source apply."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import ctypes
import difflib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import struct
import sys
import unicodedata

from . import file_lock
from . import security as securitylib
from .checkpoint_store import CheckpointStore, CheckpointStoreError
from .recovery_policy import DEFAULT_MAX_BLOB_SIZE
from .recovery_manager import _rename_noreplace, _rename_swap
from .sandbox_session import (
    _AGENT_DIRS,
    _fsync_directory,
    _GENERATED_DIRS,
    _identity,
    _open_child_directory,
    _open_source_root,
    _staged_mode,
    _validated_relative,
    _validate_baseline,
    clear_source_apply_authority,
    make_source_apply_authority,
    MAX_BASELINE_BYTES,
    MAX_ENTRIES,
    MAX_LOGICAL_BYTES,
    read_source_apply_authority,
    SandboxSessionError,
    source_apply_control_lock_path,
    write_source_apply_authority,
)
from .workspace import now


FORMAT_VERSION = 1
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_DIFF_BYTES = 16 * 1024 * 1024
MAX_RENDERED_DIFF_CHARS = 1024 * 1024
MAX_RENDERED_FILE_BYTES = 256 * 1024
MAX_APPLY_PATHS = 10_000
MAX_APPLY_BYTES = 64 * 1024 * 1024
MAX_APPLY_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_APPLY_JOURNALS = 10_000
# Counts payload leaves; removing the fixed empty trash directories is constant work.
MAX_APPLY_CLEANUP_ENTRIES = 10_000
_APPLY_BLOB_TRASH_NAME = "trash-blobs"
_APPLY_QUARANTINE_NAME = "source-apply-quarantine"
_DARWIN_PROVENANCE_XATTR = b"com.apple.provenance"
_MAX_XATTR_NAME_BYTES = 64 * 1024
_LINUX_FS_IOC_GETFLAGS = 0x80086601
_LINUX_DEFAULT_FILE_FLAGS = 0x00080000  # FS_EXTENT_FL
_LINUX_DEFAULT_DIRECTORY_FLAGS = 0x00081000  # FS_INDEX_FL | FS_EXTENT_FL

_CAPTURE_FIELDS = {
    "record_type",
    "format_version",
    "sandbox_id",
    "capture_kind",
    "tree_digest",
    "entries",
    "ignored_counts",
}
_ENTRY_FIELDS = {
    "path",
    "kind",
    "mode",
    "size",
    "sha256",
    "snapshot_eligible",
    "ineligible_reason",
    "blob_ref",
    "classification",
}
_DIFF_FIELDS = {
    "record_type",
    "format_version",
    "sandbox_id",
    "baseline_capture_digest",
    "final_capture_digest",
    "entries",
    "counts",
    "candidate_bytes",
    "rendered",
}
_DIFF_ENTRY_FIELDS = {
    "path",
    "change_kind",
    "classification",
    "before",
    "after",
}
_STATE_FIELDS = {
    "exists",
    "kind",
    "mode",
    "size",
    "sha256",
    "snapshot_eligible",
    "ineligible_reason",
    "blob_ref",
}
_BLOCKED_CLASSIFICATIONS = {
    "blocked_sensitive",
    "blocked_size",
    "blocked_type",
}
_COUNT_NAMES = (
    "candidate",
    "high_risk_candidate",
    "blocked_sensitive",
    "blocked_size",
    "blocked_type",
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOB_REF_RE = re.compile(r"^[0-9a-f]{64}$")
_SANDBOX_ID_RE = re.compile(r"^sandbox_[0-9a-f]{32}$")
_APPLY_ID_RE = re.compile(r"^apply_[0-9a-f]{32}$")
_TEMP_NAME_RE = re.compile(r"^\.pico-apply-[0-9a-f]{32}-[0-9a-f]{16}\.tmp$")
_KINDS = {
    "regular",
    "directory",
    "symlink",
    "hardlink",
    "fifo",
    "socket",
    "char_device",
    "block_device",
    "unknown",
}
_HIGH_RISK_NAMES = {
    "dockerfile",
    "makefile",
    "pico.toml",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "uv.lock",
}
_JOURNAL_FIELDS = {
    "record_type",
    "format_version",
    "journal_id",
    "sandbox_id",
    "diff_digest",
    "source",
    "status",
    "created_at",
    "updated_at",
    "entries",
    "created_dirs",
    "error_code",
}
_JOURNAL_ENTRY_FIELDS = {
    "path",
    "change_kind",
    "before",
    "before_identity",
    "after_identity",
    "prepared_identity",
    "after",
    "before_blob_ref",
    "parent_identities",
    "temp_name",
    "status",
}
_APPLY_STATE_FIELDS = {"exists", "sha256", "mode", "uid", "gid"}
_CREATED_DIR_FIELDS = {
    "path",
    "status",
    "device",
    "inode",
    "mode",
    "uid",
    "gid",
}
_PARENT_IDENTITY_FIELDS = {"path", "device", "inode"}
_LEAF_IDENTITY_FIELDS = {"device", "inode"}
_APPLY_OUTCOMES = {
    "apply_applied",
    "apply_failed_rolled_back",
    "apply_review_required",
}


class SandboxApplyError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def _matches_re(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_apply_sensitive_path(path):
    parts = PurePosixPath(path).parts
    return bool(
        (parts and parts[0].casefold() == ".pico")
        or securitylib.is_sensitive_path(path)
    )


def _apply_state_valid(value):
    if not isinstance(value, dict) or set(value) != _APPLY_STATE_FIELDS:
        return False
    if (
        type(value["exists"]) is not bool
        or not isinstance(value["sha256"], str)
        or value["mode"] is not None
        and (type(value["mode"]) is not int or not 0 <= value["mode"] <= 0o7777)
        or value["uid"] is not None
        and (type(value["uid"]) is not int or value["uid"] < 0)
        or value["gid"] is not None
        and (type(value["gid"]) is not int or value["gid"] < 0)
    ):
        return False
    if value["exists"]:
        return bool(
            _matches_re(_SHA256_RE, value["sha256"])
            and value["mode"] is not None
            and value["uid"] is not None
            and value["gid"] is not None
        )
    return value == {
        "exists": False,
        "sha256": "",
        "mode": None,
        "uid": None,
        "gid": None,
    }


def _apply_temp_name(journal_id, path):
    return (
        f".pico-apply-{journal_id[6:]}-"
        + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        + ".tmp"
    )


def _apply_directory_temp_name(path):
    return (
        ".pico-apply-directory-"
        + hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
        + ".tmp"
    )


def _ensure_apply_quarantine(checkpoint_store, journal_id):
    if not _matches_re(_APPLY_ID_RE, journal_id):
        raise SandboxApplyError("sandbox_apply_journal_invalid")
    descriptor = -1
    try:
        descriptor = securitylib._open_private_directory(checkpoint_store.root)
        root_info = os.fstat(descriptor)
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
        if (
            (root_info.st_dev, root_info.st_ino)
            != tuple(checkpoint_store._root_identity)
            or stat.S_IMODE(root_info.st_mode) != 0o700
            or root_info.st_uid != expected_uid
        ):
            raise SandboxApplyError("source_apply_unsupported")
        _SourceTree._validate_directory_metadata(descriptor)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        path = checkpoint_store.root
        for component in (_APPLY_QUARANTINE_NAME, journal_id):
            created = False
            try:
                current = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                current = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                created = True
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if created:
                os.fchmod(child, 0o700)
                os.fsync(child)
                opened = os.fstat(child)
            if (
                not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (opened.st_dev, opened.st_ino)
                or opened.st_dev != root_info.st_dev
                or stat.S_IMODE(opened.st_mode) != 0o700
                or opened.st_uid != expected_uid
            ):
                os.close(child)
                raise SandboxApplyError("source_apply_unsupported")
            try:
                _SourceTree._validate_directory_metadata(child)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            path /= component
        final_info = os.fstat(descriptor)
        return path, (final_info.st_dev, final_info.st_ino)
    except (OSError, ValueError) as exc:
        raise SandboxApplyError("source_apply_unsupported") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_apply_journal(value, *, journal_id=None, sandbox_id=None):
    if not isinstance(value, dict) or set(value) != _JOURNAL_FIELDS:
        raise SandboxApplyError("sandbox_apply_journal_invalid")
    source = value["source"]
    if (
        value["record_type"] != "docker_sandbox_apply_journal"
        or value["format_version"] != FORMAT_VERSION
        or not _matches_re(_APPLY_ID_RE, value["journal_id"])
        or journal_id is not None
        and value["journal_id"] != journal_id
        or not _matches_re(_SANDBOX_ID_RE, value["sandbox_id"])
        or sandbox_id is not None
        and value["sandbox_id"] != sandbox_id
        or not _matches_re(_SHA256_RE, value["diff_digest"])
        or not isinstance(source, dict)
        or set(source) != {"root", "device", "inode"}
        or not isinstance(source["root"], str)
        or not Path(source["root"]).is_absolute()
        or type(source["device"]) is not int
        or source["device"] < 1
        or type(source["inode"]) is not int
        or source["inode"] < 1
        or not isinstance(value["status"], str)
        or value["status"] not in {"applying", *_APPLY_OUTCOMES}
        or not isinstance(value["created_at"], str)
        or not isinstance(value["updated_at"], str)
        or not isinstance(value["entries"], list)
        or not isinstance(value["created_dirs"], list)
        or not isinstance(value["error_code"], str)
    ):
        raise SandboxApplyError("sandbox_apply_journal_invalid")
    paths = []
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != _JOURNAL_ENTRY_FIELDS:
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        try:
            path = _validated_relative(entry["path"]).as_posix()
        except (SandboxSessionError, TypeError) as exc:
            raise SandboxApplyError("sandbox_apply_journal_invalid") from exc
        if (
            path != entry["path"]
            or not isinstance(entry["change_kind"], str)
            or entry["change_kind"] not in {"created", "modified", "deleted"}
            or not _apply_state_valid(entry["before"])
            or not _apply_state_valid(entry["after"])
            or not isinstance(entry["before_identity"], dict)
            or set(entry["before_identity"]) != _LEAF_IDENTITY_FIELDS
            or not isinstance(entry["prepared_identity"], dict)
            or set(entry["prepared_identity"]) != _LEAF_IDENTITY_FIELDS
            or not isinstance(entry["after_identity"], dict)
            or set(entry["after_identity"]) != _LEAF_IDENTITY_FIELDS
            or any(
                type(entry["before_identity"][name]) is not int
                or entry["before_identity"][name] < 0
                for name in _LEAF_IDENTITY_FIELDS
            )
            or any(
                type(entry["prepared_identity"][name]) is not int
                or entry["prepared_identity"][name] < 0
                for name in _LEAF_IDENTITY_FIELDS
            )
            or any(
                type(entry["after_identity"][name]) is not int
                or entry["after_identity"][name] < 0
                for name in _LEAF_IDENTITY_FIELDS
            )
            or entry["before"]["exists"]
            and any(entry["before_identity"][name] < 1 for name in _LEAF_IDENTITY_FIELDS)
            or not entry["before"]["exists"]
            and any(entry["before_identity"].values())
            or any(entry["prepared_identity"].values())
            and any(
                entry["prepared_identity"][name] < 1
                for name in _LEAF_IDENTITY_FIELDS
            )
            or not entry["after"]["exists"]
            and any(entry["prepared_identity"].values())
            or any(entry["after_identity"].values())
            and any(
                entry["after_identity"][name] < 1
                for name in _LEAF_IDENTITY_FIELDS
            )
            or not entry["after"]["exists"]
            and any(entry["after_identity"].values())
            or entry["status"] == "applied"
            and entry["after"]["exists"]
            and any(
                entry["after_identity"][name] < 1
                for name in _LEAF_IDENTITY_FIELDS
            )
            or not isinstance(entry["before_blob_ref"], str)
            or entry["before"]["exists"]
            and not _matches_re(_BLOB_REF_RE, entry["before_blob_ref"])
            or not entry["before"]["exists"]
            and entry["before_blob_ref"]
            or not isinstance(entry["temp_name"], str)
            or not _matches_re(_TEMP_NAME_RE, entry["temp_name"])
            or entry["temp_name"]
            != _apply_temp_name(value["journal_id"], path)
            or not isinstance(entry["status"], str)
            or entry["status"] not in {"pending", "applied", "rolled_back"}
            or not isinstance(entry["parent_identities"], list)
        ):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        expected_change = (
            "created"
            if not entry["before"]["exists"] and entry["after"]["exists"]
            else "deleted"
            if entry["before"]["exists"] and not entry["after"]["exists"]
            else "modified"
            if entry["before"]["exists"] and entry["after"]["exists"]
            else ""
        )
        if (
            entry["change_kind"] != expected_change
            or _is_apply_sensitive_path(path)
            or entry["before"]["exists"]
            and "sha256:" + entry["before_blob_ref"]
            != entry["before"]["sha256"]
        ):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        parent_paths = []
        for parent in entry["parent_identities"]:
            if not isinstance(parent, dict) or set(parent) != _PARENT_IDENTITY_FIELDS:
                raise SandboxApplyError("sandbox_apply_journal_invalid")
            try:
                parent_path = _validated_relative(parent["path"]).as_posix()
            except (SandboxSessionError, TypeError) as exc:
                raise SandboxApplyError("sandbox_apply_journal_invalid") from exc
            if (
                parent_path != parent["path"]
                or type(parent["device"]) is not int
                or parent["device"] < 1
                or type(parent["inode"]) is not int
                or parent["inode"] < 1
            ):
                raise SandboxApplyError("sandbox_apply_journal_invalid")
            parent_paths.append(parent_path)
            candidate = PurePosixPath(path)
            parent_candidate = PurePosixPath(parent_path)
            if candidate.parts[: len(parent_candidate.parts)] != parent_candidate.parts:
                raise SandboxApplyError("sandbox_apply_journal_invalid")
        if parent_paths != sorted(set(parent_paths), key=lambda item: (item.count("/"), item)):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        paths.append(path)
    directories = []
    for item in value["created_dirs"]:
        if not isinstance(item, dict) or set(item) != _CREATED_DIR_FIELDS:
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        try:
            path = _validated_relative(item["path"]).as_posix()
        except (SandboxSessionError, TypeError) as exc:
            raise SandboxApplyError("sandbox_apply_journal_invalid") from exc
        if (
            path != item["path"]
            or not isinstance(item["status"], str)
            or item["status"] not in {"planned", "created", "removed"}
            or any(type(item[name]) is not int or item[name] < 0 for name in ("device", "inode", "mode", "uid", "gid"))
            or item["status"] == "planned"
            and any(item[name] for name in ("device", "inode", "mode", "uid", "gid"))
        ):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        directories.append(path)
    if (
        paths != sorted(set(paths))
        or directories
        != sorted(set(directories), key=lambda item: (item.count("/"), item))
        or any(
            not any(
                PurePosixPath(path).parts[: len(PurePosixPath(directory).parts)]
                == PurePosixPath(directory).parts
                for path in paths
            )
            for directory in directories
        )
        or value["error_code"]
        not in {"", "source_apply_failed", "source_apply_uncertain"}
        or value["status"] == "apply_applied"
        and any(entry["status"] != "applied" for entry in value["entries"])
        or value["status"] == "apply_failed_rolled_back"
        and any(entry["status"] != "rolled_back" for entry in value["entries"])
    ):
        raise SandboxApplyError("sandbox_apply_journal_invalid")
    return value


class SourceApplyStore:
    def __init__(self, project_state_root):
        self.root = securitylib.ensure_private_dir(
            Path(project_state_root) / "sandbox_apply"
        )
        self.journals = securitylib.ensure_private_dir(self.root / "journals")
        self.blobs = securitylib.ensure_private_dir(self.root / "blobs")

    def write_blob(self, data):
        if not isinstance(data, (bytes, bytearray)) or len(data) > DEFAULT_MAX_BLOB_SIZE:
            raise SandboxApplyError("sandbox_apply_blob_invalid")
        raw = bytes(data)
        digest = hashlib.sha256(raw).hexdigest()
        parent = securitylib.ensure_private_dir(self.blobs / digest[:2])
        path = parent / digest
        try:
            existing = securitylib.read_private_bytes(
                path,
                trusted_root=self.root,
                trusted_root_identity=securitylib.private_directory_identity(self.root),
                max_bytes=DEFAULT_MAX_BLOB_SIZE,
            )
        except FileNotFoundError:
            securitylib.write_private_bytes_atomic(
                path,
                raw,
                trusted_root=self.root,
                trusted_root_identity=securitylib.private_directory_identity(self.root),
                max_existing_bytes=DEFAULT_MAX_BLOB_SIZE,
            )
        else:
            if existing != raw:
                raise SandboxApplyError("sandbox_apply_blob_invalid")
        return digest

    def read_blob(self, digest):
        if not _matches_re(_BLOB_REF_RE, digest):
            raise SandboxApplyError("sandbox_apply_blob_invalid")
        data = securitylib.read_private_bytes(
            self.blobs / digest[:2] / digest,
            trusted_root=self.root,
            trusted_root_identity=securitylib.private_directory_identity(self.root),
            max_bytes=DEFAULT_MAX_BLOB_SIZE,
        )
        if hashlib.sha256(data).hexdigest() != digest:
            raise SandboxApplyError("sandbox_apply_blob_invalid")
        return data

    def write_journal(self, value):
        _validate_apply_journal(value)
        return _write_json(
            self.journals / f"{value['journal_id']}.json",
            self.root,
            value,
            max_bytes=MAX_APPLY_JOURNAL_BYTES,
        )

    def require_review(self, journal_id, *, sandbox_id):
        journal = self.load_journal(journal_id, sandbox_id=sandbox_id)
        if journal["status"] == "applying":
            journal["status"] = "apply_review_required"
            journal["error_code"] = "source_apply_uncertain"
            journal["updated_at"] = now()
            self.write_journal(journal)
        elif journal["status"] != "apply_review_required":
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        return journal

    def load_journal(self, journal_id, *, sandbox_id=None):
        if not _matches_re(_APPLY_ID_RE, journal_id):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        try:
            value, _raw = _read_json(
                self.journals / f"{journal_id}.json",
                self.root,
                max_bytes=MAX_APPLY_JOURNAL_BYTES,
            )
            value = _validate_apply_journal(
                value,
                journal_id=journal_id,
                sandbox_id=sandbox_id,
            )
            if value["status"] in {"applying", "apply_review_required"}:
                for entry in value["entries"]:
                    if entry["before"]["exists"]:
                        data = self.read_blob(entry["before_blob_ref"])
                        if _sha256(data) != entry["before"]["sha256"]:
                            raise SandboxApplyError("sandbox_apply_journal_invalid")
            return value
        except SandboxApplyError as exc:
            if exc.code in {
                "sandbox_apply_journal_invalid",
                "sandbox_apply_blob_invalid",
            }:
                raise SandboxApplyError("sandbox_apply_journal_invalid") from exc
            raise

    def load_unclaimed_journal(self, *, sandbox_id, diff_digest, source):
        try:
            journals = self._journals_snapshot()
        except SandboxApplyError as exc:
            raise SandboxApplyError("sandbox_apply_journal_invalid") from exc
        matches = [item for item in journals if item["status"] == "applying"]
        if not matches:
            return None
        if (
            len(matches) != 1
            or matches[0]["diff_digest"] != diff_digest
            or matches[0]["source"] != source
        ):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        journal = self.load_journal(matches[0]["journal_id"], sandbox_id=sandbox_id)
        if any(item["status"] != "pending" for item in journal["entries"]) or any(
            item["status"] != "planned" for item in journal["created_dirs"]
        ):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        return journal


    @staticmethod
    def _journal_blob_refs(journal):
        return {
            entry["before_blob_ref"]
            for entry in journal["entries"]
            if entry["before_blob_ref"]
        }

    def _journals_snapshot(self):
        try:
            before = securitylib.private_directory_identity(self.journals)
            paths = sorted(self.journals.iterdir(), key=lambda item: item.name)
            after = securitylib.private_directory_identity(self.journals)
        except (OSError, ValueError) as exc:
            raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        if before != after or len(paths) > MAX_APPLY_JOURNALS:
            raise SandboxApplyError("sandbox_apply_cleanup_failed")
        journals = []
        for path in paths:
            match = re.fullmatch(r"(apply_[0-9a-f]{32})\.json", path.name)
            if match is None:
                raise SandboxApplyError("sandbox_apply_cleanup_failed")
            try:
                value, _raw = _read_json(
                    path,
                    self.root,
                    max_bytes=MAX_APPLY_JOURNAL_BYTES,
                )
                journals.append(
                    _validate_apply_journal(value, journal_id=match.group(1))
                )
            except SandboxApplyError as exc:
                raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        return journals

    def _read_optional_blob(self, path, digest):
        try:
            data = securitylib.read_private_bytes(
                path,
                trusted_root=self.root,
                trusted_root_identity=securitylib.private_directory_identity(
                    self.root
                ),
                max_bytes=DEFAULT_MAX_BLOB_SIZE,
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise SandboxApplyError("sandbox_apply_cleanup_failed")
        return data

    def _trash_blob_refs(self, trash):
        try:
            trash.lstat()
        except FileNotFoundError:
            return set()
        try:
            before = securitylib.private_directory_identity(trash)
            paths = sorted(trash.iterdir(), key=lambda item: item.name)
            after = securitylib.private_directory_identity(trash)
        except (OSError, ValueError) as exc:
            raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        refs = {path.name for path in paths}
        if (
            before != after
            or len(refs) > MAX_APPLY_PATHS
            or any(_BLOB_REF_RE.fullmatch(item) is None for item in refs)
        ):
            raise SandboxApplyError("sandbox_apply_cleanup_failed")
        return refs

    def cleanup_terminal_blobs(
        self,
        journal_id,
        *,
        max_entries=MAX_APPLY_CLEANUP_ENTRIES,
    ):
        if type(max_entries) is not int or max_entries < 0:
            raise SandboxApplyError("sandbox_apply_cleanup_failed")
        journals = self._journals_snapshot()
        matches = [item for item in journals if item["journal_id"] == journal_id]
        if len(matches) != 1 or matches[0]["status"] not in {
            "apply_applied",
            "apply_failed_rolled_back",
        }:
            raise SandboxApplyError("sandbox_apply_cleanup_failed")
        target = matches[0]
        target_refs = self._journal_blob_refs(target)
        protected = set()
        for journal in journals:
            if journal["status"] in {"applying", "apply_review_required"}:
                for ref in self._journal_blob_refs(journal):
                    try:
                        self.read_blob(ref)
                    except (FileNotFoundError, OSError, ValueError) as exc:
                        raise SandboxApplyError(
                            "sandbox_apply_cleanup_failed"
                        ) from exc
                    protected.add(ref)
        removed = 0
        mutation_store = CheckpointStore(Path(target["source"]["root"]))
        quarantine, quarantine_identity = _ensure_apply_quarantine(
            mutation_store,
            journal_id,
        )
        tree = _SourceTree(
            Path(target["source"]["root"]),
            (target["source"]["device"], target["source"]["inode"]),
        )
        try:
            tree.attach_quarantine(quarantine, quarantine_identity)
            created = {
                item["path"]: (item["device"], item["inode"])
                for item in target["created_dirs"]
                if item["status"] == "created"
            }
            missing_dirs = [item["path"] for item in target["created_dirs"]]
            for entry in target["entries"]:
                if (
                    tree._quarantine_leaf(entry["temp_name"])["exists"]
                    and removed >= max_entries
                ):
                    return {
                        "complete": False,
                        "removed_count": removed,
                        "protected_count": len(target_refs & protected),
                    }
                removed += tree.reconcile_temp(
                    entry,
                    planned_missing=missing_dirs,
                    created=created,
                    terminal=target["status"],
                )
                if target["status"] == "apply_applied":
                    if (
                        tree._quarantine_leaf(entry["temp_name"])["exists"]
                        and removed >= max_entries
                    ):
                        return {
                            "complete": False,
                            "removed_count": removed,
                            "protected_count": len(target_refs & protected),
                        }
                    removed += tree.cleanup_delete_tombstone(
                        entry,
                        planned_missing=missing_dirs,
                        created=created,
                    )
            with os.scandir(tree._require_quarantine()) as entries:
                if next(entries, None) is not None:
                    raise SandboxApplyError("sandbox_apply_cleanup_failed")
        except SandboxApplyError as exc:
            raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        finally:
            tree.close()
        try:
            quarantine.rmdir()
            _fsync_directory(quarantine.parent)
        except OSError as exc:
            raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        trash = self.root / _APPLY_BLOB_TRASH_NAME
        trash_refs = self._trash_blob_refs(trash)
        candidates = sorted((target_refs - protected) | trash_refs)
        for digest in candidates:
            canonical = self.blobs / digest[:2] / digest
            trashed = trash / digest
            canonical_data = self._read_optional_blob(canonical, digest)
            trash_data = self._read_optional_blob(trashed, digest)
            if canonical_data is None and trash_data is None:
                continue
            if trash_data is not None:
                if removed >= max_entries:
                    return {
                        "complete": False,
                        "removed_count": removed,
                        "protected_count": len(target_refs & protected),
                    }
                try:
                    before = trashed.lstat()
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise SandboxApplyError("sandbox_apply_cleanup_failed")
                    current = trashed.lstat()
                    if _identity(before) != _identity(current):
                        raise SandboxApplyError("sandbox_apply_cleanup_failed")
                    trashed.unlink()
                    _fsync_directory(trash)
                except OSError as exc:
                    raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
                removed += 1
            if canonical_data is not None and digest not in protected:
                if removed >= max_entries:
                    return {
                        "complete": False,
                        "removed_count": removed,
                        "protected_count": len(target_refs & protected),
                    }
                trash = securitylib.ensure_private_dir(trash)
                try:
                    os.replace(canonical, trashed)
                    _fsync_directory(canonical.parent)
                    _fsync_directory(trash)
                except OSError as exc:
                    raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
                trash_data = self._read_optional_blob(trashed, digest)
                if trash_data != canonical_data:
                    raise SandboxApplyError("sandbox_apply_cleanup_failed")
                try:
                    before = trashed.lstat()
                    current = trashed.lstat()
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or _identity(before) != _identity(current)
                    ):
                        raise SandboxApplyError("sandbox_apply_cleanup_failed")
                    trashed.unlink()
                    _fsync_directory(trash)
                except OSError as exc:
                    raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
                removed += 1
        if self._trash_blob_refs(trash):
            return {
                "complete": False,
                "removed_count": removed,
                "protected_count": len(target_refs & protected),
            }
        try:
            trash.rmdir()
            _fsync_directory(self.root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SandboxApplyError("sandbox_apply_cleanup_failed") from exc
        return {
            "complete": True,
            "removed_count": removed,
            "protected_count": len(target_refs & protected),
        }


def _empty_apply_state():
    return {
        "exists": False,
        "sha256": "",
        "mode": None,
        "uid": None,
        "gid": None,
    }


def _descriptor_xattrs(descriptor):
    listxattr = getattr(os, "listxattr", None)
    if listxattr is not None:
        try:
            return {os.fsencode(name) for name in listxattr(descriptor)}
        except (OSError, TypeError, ValueError) as exc:
            raise SandboxApplyError("source_apply_metadata_unsupported") from exc
    if sys.platform != "darwin":
        raise SandboxApplyError("source_apply_metadata_unsupported")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        flistxattr = libc.flistxattr
        flistxattr.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        )
        flistxattr.restype = ctypes.c_ssize_t
        size = flistxattr(descriptor, None, 0, 0)
        if size < 0 or size > _MAX_XATTR_NAME_BYTES:
            raise OSError(ctypes.get_errno(), "flistxattr failed")
        if size == 0:
            return set()
        buffer = ctypes.create_string_buffer(size)
        read = flistxattr(descriptor, buffer, size, 0)
        if read != size:
            raise OSError(ctypes.get_errno(), "flistxattr changed")
        raw = buffer.raw[:read]
        if not raw.endswith(b"\0"):
            raise OSError(errno.EINVAL, "invalid flistxattr result")
        return set(raw[:-1].split(b"\0"))
    except (AttributeError, OSError) as exc:
        raise SandboxApplyError("source_apply_metadata_unsupported") from exc


def _descriptor_xattr(descriptor, name):
    getxattr = getattr(os, "getxattr", None)
    if getxattr is not None:
        try:
            return getxattr(descriptor, os.fsdecode(name))
        except (OSError, TypeError, ValueError) as exc:
            raise SandboxApplyError("source_apply_metadata_unsupported") from exc
    if sys.platform != "darwin":
        raise SandboxApplyError("source_apply_metadata_unsupported")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fgetxattr = libc.fgetxattr
        fgetxattr.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        )
        fgetxattr.restype = ctypes.c_ssize_t
        size = fgetxattr(descriptor, name, None, 0, 0, 0)
        if size < 0 or size > DEFAULT_MAX_BLOB_SIZE:
            raise OSError(ctypes.get_errno(), "fgetxattr failed")
        buffer = ctypes.create_string_buffer(size)
        read = fgetxattr(descriptor, name, buffer, size, 0, 0)
        if read != size:
            raise OSError(ctypes.get_errno(), "fgetxattr changed")
        return buffer.raw[:read]
    except (AttributeError, OSError) as exc:
        raise SandboxApplyError("source_apply_metadata_unsupported") from exc


def _descriptor_has_extended_acl(descriptor):
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        acl_get_fd_np = libc.acl_get_fd_np
        acl_get_fd_np.argtypes = (ctypes.c_int, ctypes.c_int)
        acl_get_fd_np.restype = ctypes.c_void_p
        acl_get_entry = libc.acl_get_entry
        acl_get_entry.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        acl_get_entry.restype = ctypes.c_int
        acl_free = libc.acl_free
        acl_free.argtypes = (ctypes.c_void_p,)
        acl_free.restype = ctypes.c_int
        ctypes.set_errno(0)
        acl = acl_get_fd_np(descriptor, 0x00000100)  # ACL_TYPE_EXTENDED
        if not acl:
            error = ctypes.get_errno()
            if error in {errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP}:
                return False
            raise OSError(error, "acl_get_fd_np failed")
        try:
            entry = ctypes.c_void_p()
            result = acl_get_entry(acl, 0, ctypes.byref(entry))  # ACL_FIRST_ENTRY
            if result < 0:
                raise OSError(ctypes.get_errno(), "acl_get_entry failed")
            return entry.value is not None
        finally:
            acl_free(acl)
    except (AttributeError, OSError) as exc:
        raise SandboxApplyError("source_apply_metadata_unsupported") from exc


def _descriptor_file_flags(descriptor, info):
    flags = getattr(info, "st_flags", 0)
    if sys.platform.startswith("linux"):
        try:
            raw = bytearray(struct.pack("I", 0))
            fcntl.ioctl(descriptor, _LINUX_FS_IOC_GETFLAGS, raw, True)
            allowed = (
                _LINUX_DEFAULT_DIRECTORY_FLAGS
                if stat.S_ISDIR(info.st_mode)
                else _LINUX_DEFAULT_FILE_FLAGS
            )
            flags |= struct.unpack("I", raw)[0] & ~allowed
        except OSError as exc:
            raise SandboxApplyError("source_apply_metadata_unsupported") from exc
    return flags


def _validate_descriptor_metadata(descriptor, info):
    xattrs = _descriptor_xattrs(descriptor)
    allowed_xattrs = (
        {_DARWIN_PROVENANCE_XATTR} if sys.platform == "darwin" else set()
    )
    if (
        xattrs - allowed_xattrs
        or _DARWIN_PROVENANCE_XATTR in xattrs
        and (
            len(provenance := _descriptor_xattr(
                descriptor,
                _DARWIN_PROVENANCE_XATTR,
            ))
            != 11
            or provenance[:3] != b"\x01\x02\x00"
        )
        or _descriptor_has_extended_acl(descriptor)
        or _descriptor_file_flags(descriptor, info)
    ):
        raise SandboxApplyError("source_apply_metadata_unsupported")


def _journal_state(observed):
    if not observed["exists"]:
        return _empty_apply_state()
    return {
        "exists": True,
        "sha256": observed["sha256"],
        "mode": observed["mode"],
        "uid": observed["uid"],
        "gid": observed["gid"],
    }


def _observed_matches(observed, expected):
    return _journal_state(observed) == expected


class _SourceTree:
    def __init__(self, root, expected_identity):
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.expected_identity = tuple(expected_identity)
        self.flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self.quarantine_descriptor = -1
        self.quarantine_path = None
        self.quarantine_identity = None
        if (
            not getattr(os, "O_DIRECTORY", 0)
            or not getattr(os, "O_NOFOLLOW", 0)
            or os.open not in getattr(os, "supports_dir_fd", ())
        ):
            raise SandboxApplyError("source_apply_unsupported")
        try:
            self.descriptor = os.open(self.root, self.flags)
            info = os.fstat(self.descriptor)
            current = self.root.lstat()
        except OSError as exc:
            if getattr(self, "descriptor", -1) >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            raise SandboxApplyError("source_apply_conflicted") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or self.root.is_symlink()
            or (info.st_dev, info.st_ino) != self.expected_identity
            or (current.st_dev, current.st_ino) != self.expected_identity
        ):
            os.close(self.descriptor)
            raise SandboxApplyError("source_apply_conflicted")
        self.root_device = info.st_dev

    def close(self):
        if self.quarantine_descriptor >= 0:
            os.close(self.quarantine_descriptor)
            self.quarantine_descriptor = -1
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def attach_quarantine(self, path, expected_identity):
        if self.quarantine_descriptor >= 0:
            raise SandboxApplyError("source_apply_uncertain")
        path = Path(os.path.abspath(os.fspath(path)))
        descriptor = -1
        try:
            descriptor = os.open(path, self.flags)
            opened = os.fstat(descriptor)
            current = path.lstat()
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise SandboxApplyError("source_apply_unsupported") from exc
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or opened.st_dev != self.root_device
            or (current.st_dev, current.st_ino)
            != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino) != tuple(expected_identity)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != expected_uid
        ):
            os.close(descriptor)
            raise SandboxApplyError("source_apply_unsupported")
        try:
            self._validate_directory_metadata(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            os.close(descriptor)
            raise SandboxApplyError("source_apply_unsupported")
        self.quarantine_descriptor = descriptor
        self.quarantine_path = path
        self.quarantine_identity = (opened.st_dev, opened.st_ino)

    def _require_quarantine(self):
        if self.quarantine_descriptor < 0 or self.quarantine_path is None:
            raise SandboxApplyError("source_apply_unsupported")
        opened = os.fstat(self.quarantine_descriptor)
        current = self.quarantine_path.lstat()
        expected_uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != self.quarantine_identity
            or (current.st_dev, current.st_ino) != self.quarantine_identity
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != expected_uid
        ):
            raise SandboxApplyError("source_apply_uncertain")
        self._validate_directory_metadata(self.quarantine_descriptor)
        return self.quarantine_descriptor

    def _verify_root(self):
        opened = os.fstat(self.descriptor)
        current = self.root.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != self.expected_identity
            or (current.st_dev, current.st_ino) != self.expected_identity
        ):
            raise SandboxApplyError("source_apply_conflicted")

    @staticmethod
    def _validate_directory_metadata(descriptor):
        info = os.fstat(descriptor)
        linux_xattrs = (
            _descriptor_xattrs(descriptor)
            if sys.platform.startswith("linux")
            else set()
        )
        if (
            linux_xattrs
            or _descriptor_has_extended_acl(descriptor)
            or _descriptor_file_flags(descriptor, info)
        ):
            raise SandboxApplyError("source_apply_metadata_unsupported")

    def _open_parent(
        self,
        raw_path,
        *,
        create=False,
        expected_parents=(),
        planned_missing=(),
        created=None,
        on_created=None,
    ):
        relative = _validated_relative(raw_path)
        descriptors = [os.dup(self.descriptor)]
        expected = {
            item["path"]: (item["device"], item["inode"])
            for item in expected_parents
        }
        planned = set(planned_missing)
        prefix = []
        identities = []
        missing = []
        try:
            self._verify_root()
            self._validate_directory_metadata(descriptors[-1])
            for index, part in enumerate(relative.parts[:-1]):
                prefix.append(part)
                path = PurePosixPath(*prefix).as_posix()
                try:
                    before = os.stat(
                        part,
                        dir_fd=descriptors[-1],
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not create:
                        missing = [
                            PurePosixPath(*relative.parts[: item + 1]).as_posix()
                            for item in range(index, len(relative.parts) - 1)
                        ]
                        return descriptors, relative, identities, missing
                    if path not in planned:
                        raise SandboxApplyError("source_apply_conflicted") from None
                    os.mkdir(part, 0o700, dir_fd=descriptors[-1])
                    os.fsync(descriptors[-1])
                    before = os.stat(
                        part,
                        dir_fd=descriptors[-1],
                        follow_symlinks=False,
                    )
                    identity = (before.st_dev, before.st_ino)
                    if created is not None:
                        created[path] = identity
                    if on_created is not None:
                        on_created(path, before)
                else:
                    if path in planned and (
                        created is None or path not in created
                    ):
                        raise SandboxApplyError("source_apply_conflicted")
                if (
                    not stat.S_ISDIR(before.st_mode)
                    or stat.S_ISLNK(before.st_mode)
                    or before.st_dev != self.root_device
                ):
                    raise SandboxApplyError("source_apply_conflicted")
                child = os.open(part, self.flags, dir_fd=descriptors[-1])
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    os.close(child)
                    raise SandboxApplyError("source_apply_conflicted")
                if path in expected and expected[path] != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    os.close(child)
                    raise SandboxApplyError("source_apply_conflicted")
                if created is not None and path in created and created[path] != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    os.close(child)
                    raise SandboxApplyError("source_apply_conflicted")
                self._validate_directory_metadata(child)
                descriptors.append(child)
                identities.append(
                    {"path": path, "device": opened.st_dev, "inode": opened.st_ino}
                )
            return descriptors, relative, identities, missing
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @staticmethod
    def _close_descriptors(descriptors):
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    def _quarantine_leaf(self, name, *, allowed_links=(1,)):
        return self._inspect_leaf(
            self._require_quarantine(),
            name,
            allowed_links=allowed_links,
        )

    def _retire_quarantine_leaf(
        self,
        name,
        expected=None,
        identity=None,
        *,
        allowed_links=(1,),
    ):
        parent = self._require_quarantine()
        observed = self._inspect_leaf(
            parent,
            name,
            allowed_links=allowed_links,
        )
        if not observed["exists"]:
            return False
        if (
            expected is not None
            and not _observed_matches(observed, expected)
            or identity is not None
            and observed["identity"] != tuple(identity)
        ):
            raise SandboxApplyError("source_apply_uncertain")
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
        return True

    def _restore_moved_name(self, parent, name, quarantine_name, identity):
        quarantine = self._require_quarantine()
        try:
            _rename_noreplace(quarantine, quarantine_name, parent, name)
        except OSError as exc:
            raise SandboxApplyError("source_apply_uncertain") from exc
        os.fsync(quarantine)
        os.fsync(parent)
        restored = self._inspect_leaf(parent, name)
        remaining = self._quarantine_leaf(quarantine_name)
        if (
            remaining["exists"]
            or not restored["exists"]
            or restored["identity"] != tuple(identity)
        ):
            raise SandboxApplyError("source_apply_uncertain")

    def _move_to_quarantine(
        self,
        parent,
        name,
        quarantine_name,
        expected,
        expected_identity,
    ):
        current = self._inspect_leaf(parent, name)
        if (
            not _observed_matches(current, expected)
            or not current["exists"]
            or current["identity"] != tuple(expected_identity)
        ):
            raise SandboxApplyError("source_apply_conflicted")
        if self._quarantine_leaf(quarantine_name)["exists"]:
            raise SandboxApplyError("source_apply_conflicted")
        quarantine = self._require_quarantine()
        try:
            _rename_noreplace(parent, name, quarantine, quarantine_name)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise SandboxApplyError("source_apply_conflicted") from exc
            if exc.errno in {
                errno.ENOTSUP,
                errno.EXDEV,
                errno.EINVAL,
                getattr(errno, "ENOSYS", -1),
            }:
                raise SandboxApplyError("source_apply_unsupported") from exc
            raise SandboxApplyError("source_apply_failed") from exc
        moved = self._quarantine_leaf(quarantine_name)
        target = self._inspect_leaf(parent, name)
        if (
            target["exists"]
            or not moved["exists"]
            or moved["identity"] != tuple(expected_identity)
            or not _observed_matches(moved, expected)
        ):
            if moved["exists"] and not target["exists"]:
                self._restore_moved_name(
                    parent,
                    name,
                    quarantine_name,
                    moved["identity"],
                )
                raise SandboxApplyError("source_apply_conflicted")
            raise SandboxApplyError("source_apply_uncertain")
        return moved

    def _restore_known_tombstone(
        self,
        parent,
        name,
        temp_name,
        identity,
        expected,
    ):
        current = self._inspect_leaf(parent, name)
        tombstone = self._quarantine_leaf(temp_name)
        if (
            current["exists"]
            or not tombstone["exists"]
            or tombstone["identity"] != tuple(identity)
            or not _observed_matches(tombstone, expected)
        ):
            raise SandboxApplyError("source_apply_uncertain")
        try:
            _rename_noreplace(
                self._require_quarantine(),
                temp_name,
                parent,
                name,
            )
        except OSError as exc:
            raise SandboxApplyError("source_apply_uncertain") from exc
        os.fsync(self._require_quarantine())
        os.fsync(parent)
        restored = self._inspect_leaf(parent, name)
        if (
            self._quarantine_leaf(temp_name)["exists"]
            or not restored["exists"]
            or restored["identity"] != tuple(identity)
            or not _observed_matches(restored, expected)
        ):
            raise SandboxApplyError("source_apply_uncertain")

    def _inspect_leaf(
        self,
        parent,
        name,
        *,
        max_bytes=DEFAULT_MAX_BLOB_SIZE,
        allowed_links=(1,),
    ):
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return {"exists": False, "data": None, **_empty_apply_state()}
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in allowed_links
            or before.st_dev != self.root_device
            or before.st_size > max_bytes
        ):
            raise SandboxApplyError("source_apply_conflicted")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        except OSError as exc:
            raise SandboxApplyError("source_apply_conflicted") from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise SandboxApplyError("source_apply_conflicted")
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            _validate_descriptor_metadata(descriptor, after)
            after_metadata = os.fstat(descriptor)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                len(data) > max_bytes
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or _identity(after_metadata) != _identity(after)
                or (current.st_dev, current.st_ino)
                != (opened.st_dev, opened.st_ino)
                or after.st_size != len(data)
                or after.st_mode != opened.st_mode
                or after.st_uid != opened.st_uid
                or after.st_gid != opened.st_gid
                or after.st_nlink not in allowed_links
                or _identity(after) != _identity(opened)
                or _identity(current) != _identity(opened)
            ):
                raise SandboxApplyError("source_apply_conflicted")
            return {
                "exists": True,
                "data": data,
                "sha256": _sha256(data),
                "mode": stat.S_IMODE(after.st_mode),
                "uid": after.st_uid,
                "gid": after.st_gid,
                "identity": (after.st_dev, after.st_ino),
            }
        finally:
            os.close(descriptor)

    def inspect(self, raw_path):
        descriptors, relative, identities, missing = self._open_parent(raw_path)
        try:
            if missing:
                observed = {"exists": False, "data": None, **_empty_apply_state()}
            else:
                observed = self._inspect_leaf(descriptors[-1], relative.parts[-1])
            observed["parent_identities"] = identities
            observed["missing_dirs"] = missing
            return observed
        finally:
            self._close_descriptors(descriptors)

    def mutate(
        self,
        raw_path,
        *,
        expected,
        expected_identity,
        replacement,
        replacement_data,
        temp_name,
        expected_parents,
        planned_missing,
        created,
        on_created,
        on_prepared=None,
        keep_delete_tombstone=True,
        on_step=None,
    ):
        descriptors, relative, _identities, missing = self._open_parent(
            raw_path,
            create=replacement["exists"],
            expected_parents=expected_parents,
            planned_missing=planned_missing,
            created=created,
            on_created=on_created,
        )
        target_modified = False
        temp_created = False
        temp_identity = None
        try:
            if missing:
                raise SandboxApplyError("source_apply_conflicted")
            parent = descriptors[-1]
            name = relative.parts[-1]
            current = self._inspect_leaf(parent, name)
            expected_identity = tuple(expected_identity)
            current_identity = (
                current["identity"] if current["exists"] else (0, 0)
            )
            if (
                not _observed_matches(current, expected)
                or current_identity != expected_identity
            ):
                raise SandboxApplyError("source_apply_conflicted")
            if not replacement["exists"]:
                if current["exists"]:
                    if on_step is not None:
                        on_step("before_delete_commit")
                    moved = self._move_to_quarantine(
                        parent,
                        name,
                        temp_name,
                        expected,
                        expected_identity,
                    )
                    target_modified = True
                    temp_created = True
                    if on_step is not None:
                        on_step("after_replace")
                    os.fsync(self._require_quarantine())
                    os.fsync(parent)
                    if not keep_delete_tombstone:
                        self._retire_quarantine_leaf(
                            temp_name,
                            expected,
                            moved["identity"],
                        )
                        temp_created = False
                if self._inspect_leaf(parent, name)["exists"]:
                    raise SandboxApplyError("source_apply_uncertain")
                if on_step is not None:
                    on_step("after_parent_fsync")
                return
            quarantine = self._require_quarantine()
            if self._quarantine_leaf(temp_name)["exists"]:
                raise SandboxApplyError("source_apply_conflicted")
            descriptor = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=quarantine,
            )
            temp_created = True
            try:
                opened = os.fstat(descriptor)
                temp_identity = (opened.st_dev, opened.st_ino)
                view = memoryview(replacement_data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("source apply write failed")
                    view = view[written:]
                os.fchmod(descriptor, replacement["mode"])
                opened = os.fstat(descriptor)
                if (opened.st_uid, opened.st_gid) != (
                    replacement["uid"],
                    replacement["gid"],
                ):
                    if not hasattr(os, "fchown"):
                        raise SandboxApplyError("source_apply_metadata_unsupported")
                    os.fchown(descriptor, replacement["uid"], replacement["gid"])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            staged = self._quarantine_leaf(temp_name)
            if (
                not _observed_matches(staged, replacement)
                or staged["identity"] != temp_identity
            ):
                raise SandboxApplyError("source_apply_prepare_failed")
            os.fsync(quarantine)
            if on_prepared is not None:
                on_prepared(staged["identity"])
            if on_step is not None:
                on_step("after_prepare")
            current = self._inspect_leaf(parent, name)
            current_identity = (
                current["identity"] if current["exists"] else (0, 0)
            )
            if (
                not _observed_matches(current, expected)
                or current_identity != expected_identity
            ):
                raise SandboxApplyError("source_apply_conflicted")
            if current["exists"]:
                try:
                    _rename_swap(
                        quarantine,
                        temp_name,
                        name,
                        second_parent_descriptor=parent,
                    )
                except OSError as exc:
                    raise SandboxApplyError("source_apply_unsupported") from exc
                target_modified = True
                if on_step is not None:
                    on_step("after_replace")
                swapped = self._quarantine_leaf(temp_name)
                applied = self._inspect_leaf(parent, name)
                if (
                    not _observed_matches(swapped, expected)
                    or swapped["identity"] != expected_identity
                    or not _observed_matches(applied, replacement)
                    or applied["identity"] != temp_identity
                ):
                    try:
                        _rename_swap(
                            quarantine,
                            temp_name,
                            name,
                            second_parent_descriptor=parent,
                        )
                    except OSError as exc:
                        raise SandboxApplyError("source_apply_uncertain") from exc
                    returned = self._quarantine_leaf(temp_name)
                    target_modified = not (
                        returned["exists"]
                        and returned["identity"] == temp_identity
                        and _observed_matches(returned, replacement)
                    )
                    if not target_modified:
                        self._retire_quarantine_leaf(
                            temp_name,
                            replacement,
                            temp_identity,
                        )
                        temp_created = False
                        raise SandboxApplyError("source_apply_conflicted")
                    raise SandboxApplyError("source_apply_uncertain")
                self._retire_quarantine_leaf(
                    temp_name,
                    expected,
                    expected_identity,
                )
                temp_created = False
            else:
                try:
                    _rename_noreplace(
                        quarantine,
                        temp_name,
                        parent,
                        name,
                    )
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise SandboxApplyError("source_apply_conflicted") from exc
                    if exc.errno in {
                        errno.ENOTSUP,
                        errno.EXDEV,
                        errno.EINVAL,
                        getattr(errno, "ENOSYS", -1),
                    }:
                        raise SandboxApplyError("source_apply_unsupported") from exc
                    raise SandboxApplyError("source_apply_failed") from exc
                target_modified = True
                temp_created = False
                if on_step is not None:
                    on_step("after_replace")
                applied = self._inspect_leaf(parent, name)
                if (
                    not _observed_matches(applied, replacement)
                    or applied["identity"] != temp_identity
                ):
                    raise SandboxApplyError("source_apply_uncertain")
            os.fsync(quarantine)
            os.fsync(parent)
            if on_step is not None:
                on_step("after_parent_fsync")
            if not _observed_matches(self._inspect_leaf(parent, name), replacement):
                raise SandboxApplyError("source_apply_uncertain")
        except SandboxApplyError:
            if target_modified:
                raise SandboxApplyError("source_apply_uncertain") from None
            raise
        except OSError as exc:
            if target_modified:
                raise SandboxApplyError("source_apply_uncertain") from exc
            raise SandboxApplyError("source_apply_failed") from exc
        finally:
            if temp_created and not target_modified:
                try:
                    self._retire_quarantine_leaf(
                        temp_name,
                        identity=temp_identity,
                    )
                except (OSError, SandboxApplyError):
                    pass
            self._close_descriptors(descriptors)

    def reconcile_temp(
        self,
        entry,
        *,
        planned_missing,
        created,
        terminal=None,
    ):
        if (
            terminal == "apply_applied"
            and entry["before"]["exists"]
            and not entry["after"]["exists"]
        ):
            temp = self._quarantine_leaf(
                entry["temp_name"],
                allowed_links=(1, 2),
            )
            before_identity = (
                entry["before_identity"]["device"],
                entry["before_identity"]["inode"],
            )
            if temp["exists"] and (
                temp["identity"] != before_identity
                or not _observed_matches(temp, entry["before"])
            ):
                raise SandboxApplyError("source_apply_uncertain")
            return 0
        descriptors, relative, _identities, missing = self._open_parent(
            entry["path"],
            expected_parents=entry["parent_identities"],
            planned_missing=planned_missing,
            created=created,
        )
        try:
            temp = self._quarantine_leaf(
                entry["temp_name"],
                allowed_links=(1, 2),
            )
            if missing:
                if temp["exists"]:
                    if (
                        entry["before"]["exists"]
                        or not _observed_matches(temp, entry["after"])
                    ):
                        raise SandboxApplyError("source_apply_uncertain")
                    self._retire_quarantine_leaf(
                        entry["temp_name"],
                        entry["after"],
                        temp["identity"],
                        allowed_links=(1, 2),
                    )
                    return 1
                if entry["before"]["exists"]:
                    raise SandboxApplyError("source_apply_uncertain")
                return 0
            parent = descriptors[-1]
            name = relative.parts[-1]
            target = self._inspect_leaf(parent, name, allowed_links=(1, 2))
            before_identity = (
                entry["before_identity"]["device"],
                entry["before_identity"]["inode"],
            )
            after_identity = (
                entry["after_identity"]["device"],
                entry["after_identity"]["inode"],
            )
            prepared_identity = (
                entry["prepared_identity"]["device"],
                entry["prepared_identity"]["inode"],
            )
            published_identity = (
                after_identity
                if after_identity != (0, 0)
                else prepared_identity
            )
            if not temp["exists"]:
                if (
                    entry["before"]["exists"]
                    and not entry["after"]["exists"]
                    and not target["exists"]
                    and terminal is None
                ):
                    raise SandboxApplyError("source_apply_uncertain")
                return 0
            if not entry["before"]["exists"]:
                if not _observed_matches(temp, entry["after"]):
                    raise SandboxApplyError("source_apply_uncertain")
                if (
                    published_identity != (0, 0)
                    and temp["identity"] != published_identity
                ):
                    raise SandboxApplyError("source_apply_uncertain")
                self._retire_quarantine_leaf(
                    entry["temp_name"],
                    entry["after"],
                    temp["identity"],
                    allowed_links=(1, 2),
                )
                return 1
            elif not entry["after"]["exists"]:
                if (
                    temp["identity"] != before_identity
                    or not _observed_matches(temp, entry["before"])
                ):
                    raise SandboxApplyError("source_apply_uncertain")
                if not target["exists"]:
                    return 0
                if not (
                    target["exists"]
                    and target["identity"] == before_identity
                    and target["identity"] == temp["identity"]
                    and _observed_matches(target, entry["before"])
                ):
                    raise SandboxApplyError("source_apply_uncertain")
                self._retire_quarantine_leaf(
                    entry["temp_name"],
                    entry["before"],
                    before_identity,
                    allowed_links=(1, 2),
                )
                return 1
            else:
                temp_before = bool(
                    temp["identity"] in {before_identity, prepared_identity}
                    and _observed_matches(temp, entry["before"])
                )
                temp_after = bool(
                    _observed_matches(temp, entry["after"])
                    and (
                        published_identity == (0, 0)
                        or temp["identity"] == published_identity
                    )
                )
                target_after = bool(
                    _observed_matches(target, entry["after"])
                    and published_identity != (0, 0)
                    and target["identity"] == published_identity
                )
                if temp_before and target_after:
                    expected = entry["before"]
                    identity = temp["identity"]
                elif temp_after:
                    expected = entry["after"]
                    identity = temp["identity"]
                else:
                    raise SandboxApplyError("source_apply_uncertain")
                self._retire_quarantine_leaf(
                    entry["temp_name"],
                    expected,
                    identity,
                    allowed_links=(1, 2),
                )
                return 1
        except OSError as exc:
            raise SandboxApplyError("source_apply_uncertain") from exc
        finally:
            self._close_descriptors(descriptors)

    def restore_delete_tombstone(
        self,
        entry,
        *,
        planned_missing,
        created,
    ):
        if entry["after"]["exists"]:
            return False
        descriptors, relative, _identities, missing = self._open_parent(
            entry["path"],
            expected_parents=entry["parent_identities"],
            planned_missing=planned_missing,
            created=created,
        )
        try:
            if missing:
                raise SandboxApplyError("source_apply_uncertain")
            parent = descriptors[-1]
            name = relative.parts[-1]
            tombstone = self._quarantine_leaf(entry["temp_name"])
            if not tombstone["exists"]:
                return False
            target = self._inspect_leaf(parent, name)
            before_identity = (
                entry["before_identity"]["device"],
                entry["before_identity"]["inode"],
            )
            if (
                target["exists"]
                or tombstone["identity"] != before_identity
                or not _observed_matches(tombstone, entry["before"])
            ):
                raise SandboxApplyError("source_apply_uncertain")
            self._restore_known_tombstone(
                parent,
                name,
                entry["temp_name"],
                tombstone["identity"],
                entry["before"],
            )
            return True
        except OSError as exc:
            raise SandboxApplyError("source_apply_uncertain") from exc
        finally:
            self._close_descriptors(descriptors)

    def cleanup_delete_tombstone(
        self,
        entry,
        *,
        planned_missing,
        created,
    ):
        if entry["after"]["exists"]:
            return 0
        del planned_missing, created
        try:
            tombstone = self._quarantine_leaf(entry["temp_name"])
            if not tombstone["exists"]:
                return 0
            if (
                tombstone["identity"]
                != (
                    entry["before_identity"]["device"],
                    entry["before_identity"]["inode"],
                )
                or not _observed_matches(tombstone, entry["before"])
            ):
                raise SandboxApplyError("source_apply_uncertain")
            self._retire_quarantine_leaf(
                entry["temp_name"],
                entry["before"],
                tombstone["identity"],
            )
            return 1
        except OSError as exc:
            raise SandboxApplyError("source_apply_uncertain") from exc

    def remove_created_dir(self, item):
        self._verify_root()
        relative = _validated_relative(item["path"])
        if len(relative.parts) == 1:
            descriptors = [os.dup(self.descriptor)]
        else:
            descriptors, _target, _identities, missing = self._open_parent(
                relative.as_posix()
            )
            if missing:
                self._close_descriptors(descriptors)
                return
        try:
            parent = descriptors[-1]
            name = relative.parts[-1]
            quarantine = self._require_quarantine()
            quarantine_name = _apply_directory_temp_name(item["path"])

            def inspect_quarantine():
                try:
                    info = os.stat(
                        quarantine_name,
                        dir_fd=quarantine,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return None
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino)
                    != (item["device"], item["inode"])
                ):
                    raise SandboxApplyError("source_apply_uncertain")
                child = os.open(quarantine_name, self.flags, dir_fd=quarantine)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        item["device"],
                        item["inode"],
                    ):
                        raise SandboxApplyError("source_apply_uncertain")
                    self._validate_directory_metadata(child)
                    with os.scandir(child) as entries:
                        if next(entries, None) is not None:
                            raise SandboxApplyError("source_apply_uncertain")
                finally:
                    os.close(child)
                return info

            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            quarantined = inspect_quarantine()
            if info is None:
                if quarantined is None:
                    return
                os.rmdir(quarantine_name, dir_fd=quarantine)
                os.fsync(quarantine)
                return
            if (
                not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != (item["device"], item["inode"])
                or quarantined is not None
            ):
                raise SandboxApplyError("source_apply_uncertain")
            child = os.open(name, self.flags, dir_fd=parent)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (
                    item["device"],
                    item["inode"],
                ):
                    raise SandboxApplyError("source_apply_uncertain")
                self._validate_directory_metadata(child)
                with os.scandir(child) as entries:
                    if next(entries, None) is not None:
                        raise SandboxApplyError("source_apply_uncertain")
            finally:
                os.close(child)
            try:
                _rename_noreplace(
                    parent,
                    name,
                    quarantine,
                    quarantine_name,
                )
            except OSError as exc:
                raise SandboxApplyError("source_apply_uncertain") from exc
            try:
                inspect_quarantine()
            except SandboxApplyError:
                try:
                    _rename_noreplace(
                        quarantine,
                        quarantine_name,
                        parent,
                        name,
                    )
                    os.fsync(quarantine)
                    os.fsync(parent)
                except OSError:
                    pass
                raise
            os.fsync(parent)
            os.fsync(quarantine)
            os.rmdir(quarantine_name, dir_fd=quarantine)
            os.fsync(quarantine)
        except OSError as exc:
            raise SandboxApplyError("source_apply_uncertain") from exc
        finally:
            self._close_descriptors(descriptors)


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SandboxApplyError("sandbox_apply_artifact_invalid")
        value[key] = item
    return value


def _decode_json(raw):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SandboxApplyError("sandbox_apply_artifact_invalid")
            ),
        )
    except SandboxApplyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxApplyError("sandbox_apply_artifact_invalid") from exc
    if not isinstance(value, dict):
        raise SandboxApplyError("sandbox_apply_artifact_invalid")
    return value


def _write_json(path, root, value, *, max_bytes):
    raw = _canonical_json(value)
    if len(raw) > max_bytes:
        raise SandboxApplyError("sandbox_apply_artifact_too_large")
    securitylib.write_private_bytes_atomic(
        path,
        raw,
        trusted_root=root,
        trusted_root_identity=securitylib.private_directory_identity(root),
        max_existing_bytes=max_bytes,
    )
    return raw


def _read_json(path, root, *, max_bytes, harden=True):
    raw = securitylib.read_private_bytes(
        path,
        trusted_root=root,
        trusted_root_identity=securitylib.private_directory_identity(root),
        max_bytes=max_bytes,
        harden=harden,
    )
    return _decode_json(raw), raw


def _kind(mode):
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "unknown"


def _high_risk(path, mode):
    candidate = PurePosixPath(path)
    parts = tuple(part.casefold() for part in candidate.parts)
    return bool(
        mode & stat.S_IXUSR
        or candidate.name.casefold() in _HIGH_RISK_NAMES
        or parts[:2] == (".github", "workflows")
        or parts and parts[0] in {"scripts", ".circleci"}
    )


def _contains_known_secret(data, redaction_env, secret_env_names):
    text = data.decode("utf-8", errors="replace")
    return securitylib.contains_secret_material(
        text,
        env=redaction_env,
        secret_env_names=secret_env_names,
    )


def _hash_capture_file(
    root,
    relative,
    initial,
    *,
    root_descriptor,
    root_device,
):
    parent = os.dup(root_descriptor)
    descriptor = -1
    try:
        for index, part in enumerate(relative.parts[:-1], start=1):
            if os.path.ismount(root.joinpath(*relative.parts[:index])):
                raise SandboxApplyError("workspace_mount_boundary")
            child = _open_child_directory(
                parent,
                part,
                root_device=root_device,
            )
            os.close(parent)
            parent = child
        current = os.stat(
            relative.parts[-1],
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (
            current.st_dev != root_device
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or _identity(current) != _identity(initial)
            or current.st_size > MAX_LOGICAL_BYTES
        ):
            raise SandboxApplyError("sandbox_capture_failed")
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(current):
            raise SandboxApplyError("sandbox_capture_failed")
        digest = hashlib.sha256()
        retained = [] if opened.st_size <= DEFAULT_MAX_BLOB_SIZE else None
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_LOGICAL_BYTES:
                raise SandboxApplyError("sandbox_capture_capacity_exceeded")
            digest.update(chunk)
            if retained is not None:
                retained.append(chunk)
        after = os.fstat(descriptor)
        live = os.stat(
            relative.parts[-1],
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (
            total != opened.st_size
            or _identity(after) != _identity(opened)
            or _identity(live) != _identity(opened)
        ):
            raise SandboxApplyError("sandbox_capture_failed")
        return {
            "data": b"".join(retained) if retained is not None else None,
            "sha256": "sha256:" + digest.hexdigest(),
            "size": total,
            "source_mode": stat.S_IMODE(after.st_mode),
        }
    except SandboxApplyError:
        raise
    except (OSError, SandboxSessionError) as exc:
        raise SandboxApplyError("sandbox_capture_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _capture_entry(
    root,
    relative,
    info,
    *,
    root_descriptor,
    root_device,
    blob_store,
    redaction_env,
    secret_env_names,
):
    path = relative.as_posix()
    kind = _kind(info.st_mode)
    sensitive_path = _is_apply_sensitive_path(path)
    if kind != "regular" or info.st_nlink != 1:
        return {
            "path": path,
            "kind": kind if info.st_nlink == 1 else "hardlink",
            "mode": stat.S_IMODE(info.st_mode),
            "size": info.st_size,
            "sha256": "",
            "snapshot_eligible": False,
            "ineligible_reason": (
                "sensitive_path" if sensitive_path else "unsupported_type"
            ),
            "blob_ref": "",
            "classification": (
                "blocked_sensitive" if sensitive_path else "blocked_type"
            ),
        }
    entry = _hash_capture_file(
        root,
        relative,
        info,
        root_descriptor=root_descriptor,
        root_device=root_device,
    )
    mode = 0o755 if entry["source_mode"] & stat.S_IXUSR else 0o644
    too_large = entry["size"] > DEFAULT_MAX_BLOB_SIZE
    sensitive_content = bool(
        not sensitive_path
        and not too_large
        and _contains_known_secret(
            entry["data"],
            redaction_env,
            secret_env_names,
        )
    )
    if sensitive_path:
        classification = "blocked_sensitive"
        reason = "sensitive_path"
    elif too_large:
        classification = "blocked_size"
        reason = "file_too_large"
    elif sensitive_content:
        classification = "blocked_sensitive"
        reason = "sensitive_content"
    else:
        classification = "high_risk_candidate" if _high_risk(path, mode) else "candidate"
        reason = ""
    eligible = not reason
    blob_ref = ""
    if eligible and blob_store is not None:
        blob_ref = blob_store.write_blob(
            entry["data"],
            "text" if b"\x00" not in entry["data"] else "binary",
        )["blob_ref"]
    return {
        "path": path,
        "kind": "regular",
        "mode": mode,
        "size": entry["size"],
        "sha256": entry["sha256"],
        "snapshot_eligible": eligible,
        "ineligible_reason": reason,
        "blob_ref": blob_ref,
        "classification": classification,
    }


def capture_staging(
    root,
    sandbox_id,
    capture_kind,
    *,
    blob_store=None,
    redaction_env=None,
    secret_env_names=(),
):
    root = Path(root)
    root_descriptor, root_info = _open_source_root(root)
    entries = []
    collisions = set()
    ignored = {}

    def visit(directory_descriptor, prefix=()):
        for name in sorted(os.listdir(directory_descriptor)):
            relative = PurePosixPath(*prefix, name)
            normalized = _validated_relative(relative.as_posix()).as_posix()
            collision = unicodedata.normalize("NFC", normalized).casefold()
            if collision in collisions:
                raise SandboxApplyError("workspace_path_collision")
            collisions.add(collision)
            if len(collisions) > MAX_ENTRIES:
                raise SandboxApplyError("sandbox_capture_capacity_exceeded")
            first = relative.parts[0].casefold()
            if first == ".git" or any(
                part.casefold() in _GENERATED_DIRS | _AGENT_DIRS
                for part in relative.parts
            ):
                ignored["ignored_generated"] = ignored.get("ignored_generated", 0) + 1
                continue
            info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if info.st_dev != root_info.st_dev or os.path.ismount(root / relative):
                raise SandboxApplyError("workspace_mount_boundary")
            if stat.S_ISDIR(info.st_mode):
                if _is_apply_sensitive_path(normalized):
                    entries.append(
                        {
                            "path": normalized,
                            "kind": "directory",
                            "mode": stat.S_IMODE(info.st_mode),
                            "size": 0,
                            "sha256": "",
                            "snapshot_eligible": False,
                            "ineligible_reason": "sensitive_path",
                            "blob_ref": "",
                            "classification": "blocked_sensitive",
                        }
                    )
                    continue
                child = _open_child_directory(
                    directory_descriptor,
                    name,
                    root_device=root_info.st_dev,
                )
                try:
                    visit(child, (*prefix, name))
                finally:
                    os.close(child)
                continue
            entries.append(
                _capture_entry(
                    root,
                    relative,
                    info,
                    root_descriptor=root_descriptor,
                    root_device=root_info.st_dev,
                    blob_store=blob_store,
                    redaction_env=redaction_env,
                    secret_env_names=secret_env_names,
                )
            )

    try:
        visit(root_descriptor)
    except SandboxApplyError:
        raise
    except (OSError, SandboxSessionError) as exc:
        raise SandboxApplyError("sandbox_capture_failed") from exc
    finally:
        os.close(root_descriptor)
    entries.sort(key=lambda item: item["path"])
    if sum(item["size"] for item in entries) > MAX_LOGICAL_BYTES:
        raise SandboxApplyError("sandbox_capture_capacity_exceeded")
    record = {
        "record_type": "docker_sandbox_capture",
        "format_version": FORMAT_VERSION,
        "sandbox_id": str(sandbox_id),
        "capture_kind": str(capture_kind),
        "tree_digest": _sha256(_canonical_json(entries)),
        "entries": entries,
        "ignored_counts": dict(sorted(ignored.items())),
    }
    _validate_capture(record, sandbox_id=sandbox_id, capture_kind=capture_kind)
    return record


def _validate_state(state):
    if (
        not isinstance(state, dict)
        or set(state) != _STATE_FIELDS
        or type(state["exists"]) is not bool
        or state["mode"] is not None
        and (type(state["mode"]) is not int or not 0 <= state["mode"] <= 0o7777)
        or type(state["size"]) is not int
        or state["size"] < 0
        or type(state["snapshot_eligible"]) is not bool
        or not isinstance(state["kind"], str)
        or not isinstance(state["sha256"], str)
        or not isinstance(state["ineligible_reason"], str)
        or not isinstance(state["blob_ref"], str)
    ):
        return False
    if not state["exists"]:
        return state == {
            "exists": False,
            "kind": "",
            "mode": None,
            "size": 0,
            "sha256": "",
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "blob_ref": "",
        }
    if state["kind"] not in _KINDS or state["mode"] is None:
        return False
    if state["kind"] == "regular":
        if not _matches_re(_SHA256_RE, state["sha256"]):
            return False
        if state["snapshot_eligible"]:
            return bool(
                not state["ineligible_reason"]
                and _matches_re(_BLOB_REF_RE, state["blob_ref"])
                and "sha256:" + state["blob_ref"] == state["sha256"]
            )
        return bool(
            not state["blob_ref"]
            and state["ineligible_reason"]
            in {"sensitive_path", "sensitive_content", "file_too_large"}
        )
    return bool(
        not state["sha256"]
        and not state["snapshot_eligible"]
        and not state["blob_ref"]
        and state["ineligible_reason"] in {"unsupported_type", "sensitive_path"}
    )


def _validate_capture(value, *, sandbox_id=None, capture_kind=None):
    if not isinstance(value, dict) or set(value) != _CAPTURE_FIELDS:
        raise SandboxApplyError("sandbox_capture_invalid")
    if (
        value["record_type"] != "docker_sandbox_capture"
        or value["format_version"] != FORMAT_VERSION
        or not _matches_re(_SANDBOX_ID_RE, value["sandbox_id"])
        or sandbox_id is not None and value["sandbox_id"] != sandbox_id
        or not isinstance(value["capture_kind"], str)
        or value["capture_kind"] not in {"baseline", "final", "call"}
        or capture_kind is not None and value["capture_kind"] != capture_kind
        or not isinstance(value["entries"], list)
        or not isinstance(value["ignored_counts"], dict)
        or set(value["ignored_counts"]) - {"ignored_generated"}
        or not _matches_re(_SHA256_RE, value["tree_digest"])
        or any(type(item) is not int or item < 0 for item in value["ignored_counts"].values())
    ):
        raise SandboxApplyError("sandbox_capture_invalid")
    paths = []
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise SandboxApplyError("sandbox_capture_invalid")
        try:
            path = _validated_relative(entry["path"]).as_posix()
        except (SandboxSessionError, TypeError) as exc:
            raise SandboxApplyError("sandbox_capture_invalid") from exc
        if (
            path != entry["path"]
            or not isinstance(entry["kind"], str)
            or entry["kind"] not in _KINDS
            or type(entry["mode"]) is not int
            or not 0 <= entry["mode"] <= 0o7777
            or type(entry["size"]) is not int
            or entry["size"] < 0
            or not isinstance(entry["sha256"], str)
            or type(entry["snapshot_eligible"]) is not bool
            or not isinstance(entry["ineligible_reason"], str)
            or not isinstance(entry["blob_ref"], str)
            or not isinstance(entry["classification"], str)
            or entry["classification"] not in {
                "candidate",
                "high_risk_candidate",
                "blocked_sensitive",
                "blocked_size",
                "blocked_type",
            }
        ):
            raise SandboxApplyError("sandbox_capture_invalid")
        state = _state(entry)
        if not _validate_state(state):
            raise SandboxApplyError("sandbox_capture_invalid")
        if _is_apply_sensitive_path(path) and (
            entry["snapshot_eligible"]
            or entry["ineligible_reason"] != "sensitive_path"
            or entry["classification"] != "blocked_sensitive"
        ):
            raise SandboxApplyError("sandbox_capture_invalid")
        if entry["kind"] == "regular":
            expected = entry["classification"]
            if entry["snapshot_eligible"]:
                expected = (
                    "high_risk_candidate"
                    if _high_risk(path, entry["mode"])
                    else "candidate"
                )
            elif entry["ineligible_reason"] in {
                "sensitive_path",
                "sensitive_content",
            }:
                expected = "blocked_sensitive"
            elif entry["ineligible_reason"] == "file_too_large":
                expected = "blocked_size"
            if entry["classification"] != expected:
                raise SandboxApplyError("sandbox_capture_invalid")
        elif entry["classification"] not in {
            "blocked_sensitive",
            "blocked_type",
        }:
            raise SandboxApplyError("sandbox_capture_invalid")
        paths.append(path)
    if (
        paths != sorted(set(paths))
        or value["tree_digest"] != _sha256(_canonical_json(value["entries"]))
    ):
        raise SandboxApplyError("sandbox_capture_invalid")
    return value


def _state(entry):
    if entry is None:
        return {
            "exists": False,
            "kind": "",
            "mode": None,
            "size": 0,
            "sha256": "",
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "blob_ref": "",
        }
    return {
        "exists": True,
        "kind": entry["kind"],
        "mode": entry["mode"],
        "size": entry["size"],
        "sha256": entry["sha256"],
        "snapshot_eligible": entry["snapshot_eligible"],
        "ineligible_reason": entry["ineligible_reason"],
        "blob_ref": entry["blob_ref"],
    }


def _change_kind(before, after):
    if before is None:
        return "created"
    if after is None:
        return "deleted"
    if before["kind"] != after["kind"]:
        return "type_changed"
    return "modified"


def _classification(before, after):
    values = [item for item in (before, after) if item is not None]
    classifications = {item["classification"] for item in values}
    if "blocked_sensitive" in classifications:
        return "blocked_sensitive"
    if "blocked_type" in classifications or any(
        item["kind"] != "regular" for item in values
    ):
        return "blocked_type"
    if "blocked_size" in classifications:
        return "blocked_size"
    if "high_risk_candidate" in classifications:
        return "high_risk_candidate"
    return "candidate"


def _classification_from_states(path, before, after):
    values = [item for item in (before, after) if item["exists"]]
    reasons = {item["ineligible_reason"] for item in values}
    if _is_apply_sensitive_path(path) or reasons & {
        "sensitive_path",
        "sensitive_content",
    }:
        return "blocked_sensitive"
    if any(item["kind"] != "regular" for item in values) or (
        before["exists"]
        and after["exists"]
        and before["kind"] != after["kind"]
    ):
        return "blocked_type"
    if "file_too_large" in reasons:
        return "blocked_size"
    if any(_high_risk(path, item["mode"]) for item in values):
        return "high_risk_candidate"
    return "candidate"


def _render_entry(entry, blob_store, redact_text):
    if entry["classification"] in _BLOCKED_CLASSIFICATIONS:
        return f"{entry['classification']}:{entry['change_kind']}:{entry['path']}"
    before = entry["before"]
    after = entry["after"]
    before_data = blob_store.read_blob(before["blob_ref"]) if before["exists"] else b""
    after_data = blob_store.read_blob(after["blob_ref"]) if after["exists"] else b""
    if (
        len(before_data) > MAX_RENDERED_FILE_BYTES
        or len(after_data) > MAX_RENDERED_FILE_BYTES
        or b"\x00" in before_data
        or b"\x00" in after_data
    ):
        return f"binary:{entry['change_kind']}:{entry['path']}"
    try:
        before_text = before_data.decode("utf-8").splitlines(keepends=True)
        after_text = after_data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return f"binary:{entry['change_kind']}:{entry['path']}"
    rendered = "".join(
        difflib.unified_diff(
            before_text,
            after_text,
            fromfile="a/" + entry["path"],
            tofile="b/" + entry["path"],
        )
    )
    return str(redact_text(rendered))


def build_diff(baseline, final, *, blob_store, redact_text):
    _validate_capture(baseline, capture_kind="baseline")
    _validate_capture(final, sandbox_id=baseline["sandbox_id"], capture_kind="final")
    before_map = {item["path"]: item for item in baseline["entries"]}
    after_map = {item["path"]: item for item in final["entries"]}
    entries = []
    candidate_bytes = 0
    for path in sorted(set(before_map) | set(after_map)):
        before = before_map.get(path)
        after = after_map.get(path)
        if before == after:
            continue
        classification = _classification(before, after)
        if classification not in {"blocked_sensitive", "blocked_type"}:
            candidate_bytes += (after or before)["size"]
        entries.append(
            {
                "path": path,
                "change_kind": _change_kind(before, after),
                "classification": classification,
                "before": _state(before),
                "after": _state(after),
            }
        )
    if len(entries) > MAX_APPLY_PATHS or candidate_bytes > MAX_APPLY_BYTES:
        for entry in entries:
            if entry["classification"] not in _BLOCKED_CLASSIFICATIONS:
                entry["classification"] = "blocked_size"
    counts = {
        name: sum(item["classification"] == name for item in entries)
        for name in _COUNT_NAMES
    }
    rendered_parts = []
    for entry in entries:
        part = _render_entry(entry, blob_store, redact_text)
        if sum(len(item) for item in rendered_parts) + len(part) > MAX_RENDERED_DIFF_CHARS:
            rendered_parts.append("diff_truncated")
            break
        rendered_parts.append(part)
    artifact = {
        "record_type": "docker_sandbox_diff",
        "format_version": FORMAT_VERSION,
        "sandbox_id": baseline["sandbox_id"],
        "baseline_capture_digest": _sha256(_canonical_json(baseline)),
        "final_capture_digest": _sha256(_canonical_json(final)),
        "entries": entries,
        "counts": counts,
        "candidate_bytes": candidate_bytes,
        "rendered": "\n".join(rendered_parts),
    }
    _validate_diff(artifact, sandbox_id=baseline["sandbox_id"])
    return artifact


def _validate_diff(value, *, sandbox_id=None):
    if not isinstance(value, dict) or set(value) != _DIFF_FIELDS:
        raise SandboxApplyError("sandbox_diff_invalid")
    if (
        value["record_type"] != "docker_sandbox_diff"
        or value["format_version"] != FORMAT_VERSION
        or not _matches_re(_SANDBOX_ID_RE, value["sandbox_id"])
        or sandbox_id is not None and value["sandbox_id"] != sandbox_id
        or not _matches_re(_SHA256_RE, value["baseline_capture_digest"])
        or not _matches_re(_SHA256_RE, value["final_capture_digest"])
        or not isinstance(value["entries"], list)
        or not isinstance(value["counts"], dict)
        or set(value["counts"]) != set(_COUNT_NAMES)
        or any(type(item) is not int or item < 0 for item in value["counts"].values())
        or type(value["candidate_bytes"]) is not int
        or value["candidate_bytes"] < 0
        or not isinstance(value["rendered"], str)
        or len(value["rendered"]) > MAX_RENDERED_DIFF_CHARS + 32
    ):
        raise SandboxApplyError("sandbox_diff_invalid")
    paths = []
    candidate_bytes = 0
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != _DIFF_ENTRY_FIELDS:
            raise SandboxApplyError("sandbox_diff_invalid")
        if (
            not isinstance(entry["change_kind"], str)
            or entry["change_kind"] not in {
                "created",
                "modified",
                "deleted",
                "type_changed",
            }
            or not isinstance(entry["classification"], str)
            or entry["classification"] not in {
                "candidate",
                "high_risk_candidate",
                *_BLOCKED_CLASSIFICATIONS,
            }
            or not _validate_state(entry["before"])
            or not _validate_state(entry["after"])
        ):
            raise SandboxApplyError("sandbox_diff_invalid")
        try:
            path = _validated_relative(entry["path"]).as_posix()
        except (SandboxSessionError, TypeError) as exc:
            raise SandboxApplyError("sandbox_diff_invalid") from exc
        before = entry["before"]
        after = entry["after"]
        expected_change = (
            "created"
            if not before["exists"] and after["exists"]
            else "deleted"
            if before["exists"] and not after["exists"]
            else "type_changed"
            if before["exists"]
            and after["exists"]
            and before["kind"] != after["kind"]
            else "modified"
        )
        if (
            path != entry["path"]
            or not before["exists"]
            and not after["exists"]
            or before == after
            or entry["change_kind"] != expected_change
            or _is_apply_sensitive_path(path)
            and any(
                item["snapshot_eligible"]
                or item["ineligible_reason"] != "sensitive_path"
                for item in (before, after)
                if item["exists"]
            )
        ):
            raise SandboxApplyError("sandbox_diff_invalid")
        expected_classification = _classification_from_states(path, before, after)
        if expected_classification not in {"blocked_sensitive", "blocked_type"}:
            candidate_bytes += (after if after["exists"] else before)["size"]
        paths.append(path)
    over_limit = len(value["entries"]) > MAX_APPLY_PATHS or candidate_bytes > MAX_APPLY_BYTES
    for entry in value["entries"]:
        expected_classification = _classification_from_states(
            entry["path"],
            entry["before"],
            entry["after"],
        )
        if over_limit and expected_classification not in {
            "blocked_sensitive",
            "blocked_type",
        }:
            expected_classification = "blocked_size"
        if entry["classification"] != expected_classification:
            raise SandboxApplyError("sandbox_diff_invalid")
    expected_counts = {
        name: sum(item["classification"] == name for item in value["entries"])
        for name in _COUNT_NAMES
    }
    if (
        paths != sorted(set(paths))
        or expected_counts != value["counts"]
        or candidate_bytes != value["candidate_bytes"]
    ):
        raise SandboxApplyError("sandbox_diff_invalid")
    return value


class StagingObserver:
    def __init__(
        self,
        context,
        blob_store,
        *,
        redaction_env=None,
        secret_env_names=(),
    ):
        self.context = context
        self.root = context.execution_root
        self.blob_store = blob_store
        self.redaction_env = redaction_env
        self.secret_env_names = tuple(secret_env_names)
        self.recovery_root = securitylib.ensure_private_dir(
            context.sandbox_state_root / "recovery"
        )
        self.baseline_path = self.recovery_root / "baseline-capture.json"
        self.final_path = self.recovery_root / "final-capture.json"
        self.diff_path = self.recovery_root / "diff.json"
        self.trusted_executables = {}

    def _capture(self, kind):
        return capture_staging(
            self.root,
            self.context.sandbox_session.sandbox_id,
            kind,
            blob_store=self.blob_store,
            redaction_env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def capture(self):
        record = self._capture("call")
        paths = {
            entry["path"]: _sha256(_canonical_json(entry))
            for entry in record["entries"]
        }
        file_states = {
            entry["path"]: {
                "before_mode": entry["mode"],
                **(
                    {
                        "before_hash": entry["blob_ref"],
                        "before_blob_ref": entry["blob_ref"],
                    }
                    if entry["snapshot_eligible"]
                    else {}
                ),
            }
            for entry in record["entries"]
        }
        return {
            "mode": "staging",
            "paths": paths,
            "detail": paths,
            "summaries": [],
            "capture": record,
            "file_states": file_states,
            "complete": True,
        }

    @staticmethod
    def diff(before, after):
        before_paths = before.get("paths", {})
        after_paths = after.get("paths", {})
        changed = sorted(
            path
            for path in set(before_paths) | set(after_paths)
            if before_paths.get(path) != after_paths.get(path)
        )
        summaries = []
        for path in changed:
            if path not in before_paths:
                kind = "created"
            elif path not in after_paths:
                kind = "deleted"
            else:
                kind = "modified"
            summaries.append(f"{kind}:{path}")
        return {
            "mode": "staging",
            "changed_paths": changed,
            "summaries": summaries,
        }

    def ensure_baseline(self, *, resumed=False):
        if resumed:
            value, _ = _read_json(
                self.baseline_path,
                self.recovery_root,
                max_bytes=MAX_CAPTURE_BYTES,
            )
            return _validate_capture(
                value,
                sandbox_id=self.context.sandbox_session.sandbox_id,
                capture_kind="baseline",
            )
        if self.baseline_path.exists():
            raise SandboxApplyError("sandbox_baseline_already_exists")
        value = self._capture("baseline")
        source_baseline, _ = _read_json(
            self.context.sandbox_state_root / "baseline.json",
            self.context.sandbox_state_root,
            max_bytes=MAX_CAPTURE_BYTES,
        )
        expected = {
            entry["path"]: {
                "sha256": entry["sha256"],
                "size": entry["size"],
                "mode": 0o755 if entry["mode"] & stat.S_IXUSR else 0o644,
            }
            for entry in source_baseline["entries"]
        }
        actual = {
            entry["path"]: {
                "sha256": entry["sha256"],
                "size": entry["size"],
                "mode": entry["mode"],
            }
            for entry in value["entries"]
        }
        if actual != expected:
            raise SandboxApplyError("sandbox_baseline_mismatch")
        _write_json(
            self.baseline_path,
            self.recovery_root,
            value,
            max_bytes=MAX_CAPTURE_BYTES,
        )
        return value

    def load_baseline(self):
        value, _ = _read_json(
            self.baseline_path,
            self.recovery_root,
            max_bytes=MAX_CAPTURE_BYTES,
        )
        return _validate_capture(
            value,
            sandbox_id=self.context.sandbox_session.sandbox_id,
            capture_kind="baseline",
        )

    def finalize_diff(self, redact_text):
        with self.blob_store.mutation_lock():
            return self._finalize_diff_locked(redact_text)

    def _finalize_diff_locked(self, redact_text):
        current = self.context.current_session()
        if current.state != "ready" or current.manifest["active_call"] is not None:
            raise SandboxApplyError("sandbox_diff_not_allowed")
        baseline = self.load_baseline()
        final_exists = self.final_path.exists()
        diff_exists = self.diff_path.exists()
        if diff_exists and not final_exists:
            raise SandboxApplyError("sandbox_diff_identity_mismatch")
        if final_exists:
            final, final_raw = _read_json(
                self.final_path,
                self.recovery_root,
                max_bytes=MAX_CAPTURE_BYTES,
            )
            _validate_capture(
                final,
                sandbox_id=current.sandbox_id,
                capture_kind="final",
            )
            live = self._capture("final")
            if live["tree_digest"] != final["tree_digest"]:
                raise SandboxApplyError("sandbox_final_tree_changed")
        else:
            final = self._capture("final")
            final_raw = _write_json(
                self.final_path,
                self.recovery_root,
                final,
                max_bytes=MAX_CAPTURE_BYTES,
            )
        artifact = build_diff(
            baseline,
            final,
            blob_store=self.blob_store,
            redact_text=redact_text,
        )
        if artifact["final_capture_digest"] != _sha256(final_raw):
            raise SandboxApplyError("sandbox_diff_identity_mismatch")
        if diff_exists:
            persisted, diff_raw = _read_json(
                self.diff_path,
                self.recovery_root,
                max_bytes=MAX_DIFF_BYTES,
            )
            _validate_diff(persisted, sandbox_id=current.sandbox_id)
            if persisted != artifact:
                raise SandboxApplyError("sandbox_diff_identity_mismatch")
        else:
            diff_raw = _write_json(
                self.diff_path,
                self.recovery_root,
                artifact,
                max_bytes=MAX_DIFF_BYTES,
            )
        diff_digest = _sha256(diff_raw)
        blocked = sum(
            artifact["counts"].get(name, 0)
            for name in _BLOCKED_CLASSIFICATIONS
        )
        candidates = len(artifact["entries"]) - blocked
        try:
            self.context.runner.session_store.record_diff(
                current.state_root,
                diff_digest=diff_digest,
                candidate_count=candidates,
                blocked_count=blocked,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc
        return {
            "status": "diff_blocked" if blocked else "diff_ready",
            "diff_digest": diff_digest,
            "artifact": artifact,
            "generated_count": max(
                0,
                final["ignored_counts"].get("ignored_generated", 0)
                - baseline["ignored_counts"].get("ignored_generated", 0),
            ),
        }

    def load_finalized_diff(self, expected_digest=None):
        current = self.context.current_session()
        artifact, raw = _read_json(
            self.diff_path,
            self.recovery_root,
            max_bytes=MAX_DIFF_BYTES,
        )
        _validate_diff(artifact, sandbox_id=current.sandbox_id)
        digest = _sha256(raw)
        if (
            current.manifest["diff"]["digest"] != digest
            or expected_digest is not None
            and expected_digest != digest
        ):
            raise SandboxApplyError("sandbox_diff_identity_mismatch")
        final, final_raw = _read_json(
            self.final_path,
            self.recovery_root,
            max_bytes=MAX_CAPTURE_BYTES,
        )
        _validate_capture(final, sandbox_id=current.sandbox_id, capture_kind="final")
        if _sha256(final_raw) != artifact["final_capture_digest"]:
            raise SandboxApplyError("sandbox_diff_identity_mismatch")
        live = self._capture("final")
        if live["tree_digest"] != final["tree_digest"]:
            raise SandboxApplyError("sandbox_final_tree_changed")
        return artifact, final, digest


def load_finalized_diff_artifact(session):
    recovery_root = session.state_root / "recovery"
    artifact, raw = _read_json(
        recovery_root / "diff.json",
        recovery_root,
        max_bytes=MAX_DIFF_BYTES,
        harden=False,
    )
    _validate_diff(artifact, sandbox_id=session.sandbox_id)
    digest = _sha256(raw)
    if session.manifest["diff"]["digest"] != digest:
        raise SandboxApplyError("sandbox_diff_identity_mismatch")
    return artifact, digest


class _MaintenanceRunner:
    def __init__(self, session_store):
        self.session_store = session_store


class SandboxMaintenanceContext:
    def __init__(self, session_store, session):
        manifest = session.manifest
        sidecar = manifest["sidecar"]
        if sidecar is None:
            raise SandboxApplyError("sandbox_state_invalid")
        self.source_root = Path(manifest["source"]["root"])
        self.execution_root = Path(manifest["execution"]["root"])
        self.project_state_root = Path(sidecar["path"]).parent.parent
        self.sandbox_state_root = session.state_root
        self.source_apply_state_root = session.state_root
        self.sandbox_session = session
        self.runner = _MaintenanceRunner(session_store)

    def current_session(self):
        try:
            return self.runner.session_store.inspect(self.sandbox_state_root)
        except SandboxSessionError as exc:
            raise SandboxApplyError("sandbox_state_invalid") from exc

    def observer(self):
        checkpoint_root = (
            self.sandbox_state_root
            / "recovery"
            / ".pico"
            / "checkpoints"
        )
        if not checkpoint_root.is_dir() or checkpoint_root.is_symlink():
            raise SandboxApplyError("sandbox_recovery_state_invalid")
        checkpoint_store = CheckpointStore(checkpoint_root)
        return StagingObserver(self, checkpoint_store)


class SourceApplier:
    def __init__(self, context, observer, *, fault_injector=None):
        self.context = context
        self.observer = observer
        self.session_store = context.runner.session_store
        self.store = SourceApplyStore(context.source_apply_state_root)
        self.fault_injector = fault_injector

    def _fault(self, stage, path=""):
        if self.fault_injector is not None:
            self.fault_injector(stage, path)

    def _reserve_external_authority(self, current, journal_id, diff_digest):
        source = current.manifest["source"]
        try:
            return write_source_apply_authority(
                self.session_store.parent,
                self.context.source_root,
                source_device=source["device"],
                source_inode=source["inode"],
                state_root=self.context.sandbox_state_root,
                sandbox_id=current.sandbox_id,
                journal_id=journal_id,
                diff_digest=diff_digest,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc

    def _external_authority(self):
        try:
            return read_source_apply_authority(
                self.session_store.parent,
                self.context.source_root,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc

    def _require_external_authority(self, journal):
        authority = self._external_authority()
        source = journal["source"]
        if authority is None or any(
            authority[name] != value
            for name, value in {
                "source_root": source["root"],
                "source_device": source["device"],
                "source_inode": source["inode"],
                "sandbox_id": journal["sandbox_id"],
                "state_root": str(self.context.sandbox_state_root),
                "journal_id": journal["journal_id"],
                "diff_digest": journal["diff_digest"],
            }.items()
        ):
            raise SandboxApplyError("sandbox_state_invalid")
        return authority

    def _clear_external_authority(self, journal, *, expected_authority=None):
        expected_authority = (
            self._require_external_authority(journal)
            if expected_authority is None
            else expected_authority
        )
        try:
            clear_source_apply_authority(
                self.session_store.parent,
                self.context.source_root,
                expected_authority=expected_authority,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc

    def _record_reservation_conflict(self, diff_digest):
        current = self.context.current_session()
        source = current.manifest["source"]
        with self._external_mutation_store() as mutation_store:
            authority = self._external_authority()
            if authority is None:
                return self._record_conflict(diff_digest)
            if any(
                authority[name] != value
                for name, value in {
                    "source_root": source["root"],
                    "source_device": source["device"],
                    "source_inode": source["inode"],
                    "sandbox_id": current.sandbox_id,
                    "state_root": str(self.context.sandbox_state_root),
                    "diff_digest": diff_digest,
                }.items()
            ):
                raise SandboxApplyError("source_apply_review_required")
            with mutation_store.mutation_lock(
                source_apply_journal_id=authority["journal_id"]
            ):
                if mutation_store.source_apply_guard() is not None:
                    raise SandboxApplyError("source_apply_review_required")
                try:
                    self.store.load_journal(
                        authority["journal_id"],
                        sandbox_id=current.sandbox_id,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise SandboxApplyError("source_apply_review_required")
                current = self.context.current_session()
                if current.state != "pending_review" or current.manifest[
                    "apply"
                ] != {"journal_id": "", "status": "not_started"}:
                    raise SandboxApplyError("sandbox_apply_not_allowed")
                clear_source_apply_authority(
                    self.session_store.parent,
                    self.context.source_root,
                    expected_authority=authority,
                )
        return self._record_conflict(diff_digest)

    @contextmanager
    def _external_control_lock(self):
        try:
            lock_path = source_apply_control_lock_path(
                self.session_store.parent,
                self.context.source_root,
            )
            lock = (
                nullcontext()
                if file_lock.lock_is_active(lock_path)
                else file_lock.locked_file(lock_path, require_lock=True)
            )
            with lock:
                yield
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc

    @contextmanager
    def _external_mutation_store(self):
        with self._external_control_lock():
            yield CheckpointStore(self.context.source_root)

    @contextmanager
    def _source_mutation_lock(self, journal_id=None):
        try:
            with self._external_mutation_store() as store:
                with store.mutation_lock(source_apply_journal_id=journal_id):
                    yield store
        except CheckpointStoreError as exc:
            raise SandboxApplyError(exc.code) from exc

    def _load_source_baseline(self):
        try:
            value, _raw = _read_json(
                self.context.sandbox_state_root / "baseline.json",
                self.context.sandbox_state_root,
                max_bytes=MAX_BASELINE_BYTES,
            )
            return _validate_baseline(
                value,
                self.context.sandbox_session.sandbox_id,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError("sandbox_source_baseline_invalid") from exc

    @staticmethod
    def _baseline_state(entry):
        if entry is None:
            return _empty_apply_state()
        return {
            "exists": True,
            "sha256": entry["sha256"],
            "mode": entry["mode"],
            "uid": entry["uid"],
            "gid": entry["gid"],
        }

    @staticmethod
    def _planned_after(before, reviewed_after):
        if not reviewed_after["exists"]:
            return _empty_apply_state()
        executable = bool(reviewed_after["mode"] & stat.S_IXUSR)
        if before["exists"]:
            mode = (before["mode"] & 0o666) | (0o111 if executable else 0)
            uid = before["uid"]
            gid = before["gid"]
        else:
            mode = 0o700 if executable else 0o600
            uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
            gid = os.getegid() if hasattr(os, "getegid") else os.getgid()
        return {
            "exists": True,
            "sha256": reviewed_after["sha256"],
            "mode": mode,
            "uid": uid,
            "gid": gid,
        }

    def _record_conflict(self, diff_digest):
        try:
            self.session_store.record_apply_conflict(
                self.context.sandbox_state_root,
                diff_digest=diff_digest,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc
        return {
            "status": "apply_conflicted",
            "diff_digest": diff_digest,
            "journal_id": "",
        }

    def _preflight(self, artifact, final, source_tree, staging_tree):
        baseline = self._load_source_baseline()
        baseline_map = {entry["path"]: entry for entry in baseline["entries"]}
        final_map = {entry["path"]: entry for entry in final["entries"]}
        intents = []
        missing_dirs = set()
        for diff_entry in artifact["entries"]:
            if diff_entry["classification"] not in {
                "candidate",
                "high_risk_candidate",
            }:
                continue
            path = diff_entry["path"]
            baseline_entry = baseline_map.get(path)
            baseline_state = self._baseline_state(baseline_entry)
            before = diff_entry["before"]
            if before["exists"] != baseline_state["exists"]:
                raise SandboxApplyError("source_apply_conflicted")
            if before["exists"] and (
                before["sha256"] != baseline_state["sha256"]
                or before["mode"] != _staged_mode(baseline_state["mode"])
            ):
                raise SandboxApplyError("source_apply_conflicted")
            source_current = source_tree.inspect(path)
            if not _observed_matches(source_current, baseline_state):
                raise SandboxApplyError("source_apply_conflicted")
            reviewed_after = diff_entry["after"]
            staged = staging_tree.inspect(path)
            if reviewed_after["exists"]:
                final_entry = final_map.get(path)
                if (
                    final_entry is None
                    or not staged["exists"]
                    or staged["sha256"] != reviewed_after["sha256"]
                    or staged["mode"] != reviewed_after["mode"]
                ):
                    raise SandboxApplyError("source_apply_conflicted")
            elif staged["exists"]:
                raise SandboxApplyError("source_apply_conflicted")
            planned_after = self._planned_after(baseline_state, reviewed_after)
            missing_dirs.update(source_current["missing_dirs"])
            intents.append(
                {
                    "path": path,
                    "change_kind": diff_entry["change_kind"],
                    "before": baseline_state,
                    "before_identity": (
                        {
                            "device": source_current["identity"][0],
                            "inode": source_current["identity"][1],
                        }
                        if source_current["exists"]
                        else {"device": 0, "inode": 0}
                    ),
                    "before_data": source_current["data"],
                    "after": planned_after,
                    "after_data": staged["data"] if staged["exists"] else None,
                    "parent_identities": source_current["parent_identities"],
                }
            )
        return intents, sorted(missing_dirs, key=lambda item: (item.count("/"), item))

    def _new_journal(self, diff_digest, intents, missing_dirs, *, journal_id=None):
        journal_id = str(journal_id or "apply_" + secrets.token_hex(16))
        timestamp = now()
        entries = []
        for intent in intents:
            before_blob_ref = (
                self.store.write_blob(intent["before_data"])
                if intent["before"]["exists"]
                else ""
            )
            entries.append(
                {
                    "path": intent["path"],
                    "change_kind": intent["change_kind"],
                    "before": intent["before"],
                    "before_identity": intent["before_identity"],
                    "after_identity": {"device": 0, "inode": 0},
                    "prepared_identity": {"device": 0, "inode": 0},
                    "after": intent["after"],
                    "before_blob_ref": before_blob_ref,
                    "parent_identities": intent["parent_identities"],
                    "temp_name": _apply_temp_name(journal_id, intent["path"]),
                    "status": "pending",
                }
            )
        source = self.context.current_session().manifest["source"]
        return {
            "record_type": "docker_sandbox_apply_journal",
            "format_version": FORMAT_VERSION,
            "journal_id": journal_id,
            "sandbox_id": self.context.sandbox_session.sandbox_id,
            "diff_digest": diff_digest,
            "source": {
                "root": source["root"],
                "device": source["device"],
                "inode": source["inode"],
            },
            "status": "applying",
            "created_at": timestamp,
            "updated_at": timestamp,
            "entries": entries,
            "created_dirs": [
                {
                    "path": path,
                    "status": "planned",
                    "device": 0,
                    "inode": 0,
                    "mode": 0,
                    "uid": 0,
                    "gid": 0,
                }
                for path in missing_dirs
            ],
            "error_code": "",
        }

    def _write_journal(self, journal):
        journal["updated_at"] = now()
        self.store.write_journal(journal)

    def _created_callback(self, journal, created):
        def record(path, info):
            created[path] = (info.st_dev, info.st_ino)
            item = next(
                value for value in journal["created_dirs"] if value["path"] == path
            )
            item.update(
                status="created",
                device=info.st_dev,
                inode=info.st_ino,
                mode=stat.S_IMODE(info.st_mode),
                uid=info.st_uid,
                gid=info.st_gid,
            )
            self._write_journal(journal)
            self._fault("after_mkdir", path)

        return record

    def _prepared_callback(self, journal, entry):
        def record(identity):
            entry["prepared_identity"] = {
                "device": identity[0],
                "inode": identity[1],
            }
            self._write_journal(journal)

        return record

    def _apply_intents(self, tree, journal, intents, missing_dirs, created):
        on_created = self._created_callback(journal, created)
        by_path = {item["path"]: item for item in journal["entries"]}
        for intent in intents:
            entry = by_path[intent["path"]]
            tree.mutate(
                intent["path"],
                expected=intent["before"],
                expected_identity=(
                    entry["before_identity"]["device"],
                    entry["before_identity"]["inode"],
                ),
                replacement=intent["after"],
                replacement_data=intent["after_data"],
                temp_name=entry["temp_name"],
                expected_parents=entry["parent_identities"],
                planned_missing=missing_dirs,
                created=created,
                on_created=on_created,
                on_prepared=self._prepared_callback(journal, entry),
                on_step=lambda stage, path=intent["path"]: self._fault(
                    stage,
                    path,
                ),
            )
            if entry["after"]["exists"]:
                entry["after_identity"] = dict(entry["prepared_identity"])
            entry["status"] = "applied"
            self._write_journal(journal)
            self._fault("after_mutation", intent["path"])

    def _rollback(self, tree, journal, missing_dirs, created):
        self._fault("before_rollback", "")
        for entry in reversed(journal["entries"]):
            tree.reconcile_temp(
                entry,
                planned_missing=missing_dirs,
                created=created,
            )
            if tree.restore_delete_tombstone(
                entry,
                planned_missing=missing_dirs,
                created=created,
            ):
                entry["status"] = "rolled_back"
                self._write_journal(journal)
                continue
            current = tree.inspect(entry["path"])
            current_parents = {
                item["path"]: (item["device"], item["inode"])
                for item in current["parent_identities"]
            }
            if any(
                current_parents.get(item["path"])
                != (item["device"], item["inode"])
                for item in entry["parent_identities"]
            ):
                raise SandboxApplyError("source_apply_uncertain")
            if _observed_matches(current, entry["before"]):
                before_identity = (
                    entry["before_identity"]["device"],
                    entry["before_identity"]["inode"],
                )
                prepared_identity = (
                    entry["prepared_identity"]["device"],
                    entry["prepared_identity"]["inode"],
                )
                if current["exists"] and current["identity"] not in {
                    before_identity,
                    prepared_identity,
                }:
                    raise SandboxApplyError("source_apply_uncertain")
                entry["status"] = "rolled_back"
                self._write_journal(journal)
                continue
            if not _observed_matches(current, entry["after"]):
                raise SandboxApplyError("source_apply_uncertain")
            before_data = (
                self.store.read_blob(entry["before_blob_ref"])
                if entry["before"]["exists"]
                else None
            )
            after_identity = (
                entry["after_identity"]["device"],
                entry["after_identity"]["inode"],
            )
            prepared_identity = (
                entry["prepared_identity"]["device"],
                entry["prepared_identity"]["inode"],
            )
            published_identity = (
                after_identity
                if after_identity != (0, 0)
                else prepared_identity
            )
            if not current["exists"] or published_identity == (0, 0):
                raise SandboxApplyError("source_apply_uncertain")
            if current["identity"] != published_identity:
                raise SandboxApplyError("source_apply_uncertain")
            tree.mutate(
                entry["path"],
                expected=entry["after"],
                expected_identity=published_identity,
                replacement=entry["before"],
                replacement_data=before_data,
                temp_name=entry["temp_name"],
                expected_parents=entry["parent_identities"],
                planned_missing=missing_dirs,
                created=created,
                on_created=self._created_callback(journal, created),
                on_prepared=self._prepared_callback(journal, entry),
                keep_delete_tombstone=False,
                on_step=lambda stage, path=entry["path"]: self._fault(
                    "rollback_" + stage,
                    path,
                ),
            )
            entry["status"] = "rolled_back"
            self._write_journal(journal)
        for item in reversed(journal["created_dirs"]):
            if item["status"] != "created":
                continue
            tree.remove_created_dir(item)
            item["status"] = "removed"
            self._write_journal(journal)

    def _finish(self, journal, outcome, mutation_store, *, error_code=""):
        self._fault("before_terminalize", "")
        journal["status"] = outcome
        journal["error_code"] = error_code
        self._write_journal(journal)
        try:
            self.session_store.finish_apply(
                self.context.sandbox_state_root,
                journal_id=journal["journal_id"],
                outcome=outcome,
            )
        except SandboxSessionError as exc:
            raise SandboxApplyError(exc.code) from exc
        self._fault("after_terminalize", "")
        result = {
            "status": outcome,
            "diff_digest": journal["diff_digest"],
            "journal_id": journal["journal_id"],
        }
        if outcome in {"apply_applied", "apply_failed_rolled_back"}:
            try:
                cleanup = self.store.cleanup_terminal_blobs(journal["journal_id"])
                if not cleanup["complete"]:
                    raise SandboxApplyError("sandbox_apply_cleanup_failed")
                authority = self._require_external_authority(journal)
                mutation_store.finish_source_apply_guard(
                    journal_id=journal["journal_id"]
                )
                self._clear_external_authority(
                    journal,
                    expected_authority=authority,
                )
                if outcome == "apply_applied":
                    self.session_store.cleanup_applied(
                        self.context.sandbox_state_root
                    )
            except (
                OSError,
                CheckpointStoreError,
                SandboxApplyError,
                SandboxSessionError,
            ):
                if outcome == "apply_applied":
                    result["status"] = "applied_cleanup_pending"
                else:
                    result["cleanup_status"] = "pending"
        return result

    def apply(self, expected_diff_digest):
        expected_diff_digest = str(expected_diff_digest)
        try:
            artifact, final, diff_digest = self.observer.load_finalized_diff(
                expected_diff_digest
            )
        except SandboxApplyError as exc:
            if exc.code == "sandbox_final_tree_changed":
                return self._record_reservation_conflict(expected_diff_digest)
            raise
        current = self.context.current_session()
        if current.state != "pending_review":
            raise SandboxApplyError("sandbox_apply_not_allowed")
        if current.manifest["diff"]["status"] == "diff_blocked":
            return {
                "status": "diff_blocked",
                "diff_digest": diff_digest,
                "journal_id": "",
            }
        if current.manifest["diff"]["status"] != "diff_ready":
            raise SandboxApplyError("sandbox_apply_not_allowed")
        if current.manifest["apply"] != {
            "journal_id": "",
            "status": "not_started",
        }:
            raise SandboxApplyError("sandbox_apply_not_allowed")

        source_binding = {
            "root": current.manifest["source"]["root"],
            "device": current.manifest["source"]["device"],
            "inode": current.manifest["source"]["inode"],
        }
        with self._external_mutation_store() as mutation_store:
            guard = mutation_store.source_apply_guard()
            authority = self._external_authority()
            orphan = self.store.load_unclaimed_journal(
                sandbox_id=current.sandbox_id,
                diff_digest=diff_digest,
                source=source_binding,
            )
        expected_authority_fields = {
            "source_root": source_binding["root"],
            "source_device": source_binding["device"],
            "source_inode": source_binding["inode"],
            "sandbox_id": current.sandbox_id,
            "state_root": str(self.context.sandbox_state_root),
            "diff_digest": diff_digest,
        }
        if authority is not None and any(
            authority[name] != value
            for name, value in expected_authority_fields.items()
        ):
            raise SandboxApplyError("source_apply_review_required")
        if guard is not None and (
            guard["sandbox_id"] != current.sandbox_id
            or guard["diff_digest"] != diff_digest
        ):
            raise SandboxApplyError("source_apply_review_required")
        source_identity = (
            current.manifest["source"]["device"],
            current.manifest["source"]["inode"],
        )
        staging_identity = (
            current.manifest["execution"]["device"],
            current.manifest["execution"]["inode"],
        )
        if orphan is not None and (
            guard is not None and orphan["journal_id"] != guard["journal_id"]
            or authority is None
            or orphan["journal_id"] != authority["journal_id"]
        ):
            raise SandboxApplyError("source_apply_review_required")
        if orphan is not None:
            with self._source_mutation_lock(orphan["journal_id"]) as mutation_store:
                orphan = self.store.load_unclaimed_journal(
                    sandbox_id=current.sandbox_id,
                    diff_digest=diff_digest,
                    source=source_binding,
                )
                expected_authority = make_source_apply_authority(
                    self.session_store.parent,
                    self.context.source_root,
                    source_device=orphan["source"]["device"],
                    source_inode=orphan["source"]["inode"],
                    state_root=self.context.sandbox_state_root,
                    sandbox_id=orphan["sandbox_id"],
                    journal_id=orphan["journal_id"],
                    diff_digest=orphan["diff_digest"],
                )
                existing = self._external_authority()
                if existing is not None and existing != expected_authority:
                    raise SandboxApplyError("source_apply_review_required")
                mutation_store.begin_source_apply_guard(
                    journal_id=orphan["journal_id"],
                    sandbox_id=current.sandbox_id,
                    diff_digest=diff_digest,
                )
                try:
                    self.session_store.begin_apply(
                        self.context.sandbox_state_root,
                        diff_digest=diff_digest,
                        journal_id=orphan["journal_id"],
                    )
                except SandboxSessionError as exc:
                    raise SandboxApplyError(exc.code) from exc
            return self.reconcile()
        reserved_journal_id = (
            authority["journal_id"] if authority is not None else None
        )
        if guard is not None and guard["journal_id"] != reserved_journal_id:
            raise SandboxApplyError("source_apply_review_required")
        if reserved_journal_id is not None:
            try:
                stale = self.store.load_journal(
                    reserved_journal_id,
                    sandbox_id=current.sandbox_id,
                )
            except FileNotFoundError:
                stale = None
            if stale is not None:
                raise SandboxApplyError("source_apply_review_required")
            if guard is not None:
                raise SandboxApplyError("source_apply_review_required")
        with self._source_mutation_lock(reserved_journal_id) as mutation_store:
            if self.store.load_unclaimed_journal(
                sandbox_id=current.sandbox_id,
                diff_digest=diff_digest,
                source=source_binding,
            ) is not None:
                raise SandboxApplyError("sandbox_apply_not_allowed")
            current = self.context.current_session()
            if current.state != "pending_review" or current.manifest["apply"] != {
                "journal_id": "",
                "status": "not_started",
            }:
                raise SandboxApplyError("sandbox_apply_not_allowed")
            self._fault("after_lock", "")
            source_tree = _SourceTree(self.context.source_root, source_identity)
            staging_tree = _SourceTree(self.context.execution_root, staging_identity)
            try:
                try:
                    intents, missing_dirs = self._preflight(
                        artifact,
                        final,
                        source_tree,
                        staging_tree,
                    )
                    live = self.observer._capture("final")
                    if live["tree_digest"] != final["tree_digest"]:
                        raise SandboxApplyError("source_apply_conflicted")
                except SandboxApplyError as exc:
                    if exc.code == "source_apply_conflicted":
                        if reserved_journal_id is not None:
                            authority = self._external_authority()
                            if authority is None:
                                raise SandboxApplyError(
                                    "source_apply_review_required"
                                )
                            clear_source_apply_authority(
                                self.session_store.parent,
                                self.context.source_root,
                                expected_authority=authority,
                            )
                        return self._record_conflict(diff_digest)
                    raise
                journal_id = reserved_journal_id or (
                    "apply_" + secrets.token_hex(16)
                )
                expected_authority = make_source_apply_authority(
                    self.session_store.parent,
                    self.context.source_root,
                    source_device=source_binding["device"],
                    source_inode=source_binding["inode"],
                    state_root=self.context.sandbox_state_root,
                    sandbox_id=current.sandbox_id,
                    journal_id=journal_id,
                    diff_digest=diff_digest,
                )
                existing = self._external_authority()
                if existing is not None and existing != expected_authority:
                    raise SandboxApplyError("source_apply_review_required")
                self._reserve_external_authority(
                    current,
                    journal_id,
                    diff_digest,
                )
                self._fault("after_reservation", "")
                journal = self._new_journal(
                    diff_digest,
                    intents,
                    missing_dirs,
                    journal_id=journal_id,
                )
                self._write_journal(journal)
                self._fault("after_journal_before_guard", "")
                mutation_store.begin_source_apply_guard(
                    journal_id=journal["journal_id"],
                    sandbox_id=current.sandbox_id,
                    diff_digest=diff_digest,
                )
                self._fault("after_guard", "")
                try:
                    self.session_store.begin_apply(
                        self.context.sandbox_state_root,
                        diff_digest=diff_digest,
                        journal_id=journal["journal_id"],
                    )
                except SandboxSessionError as exc:
                    raise SandboxApplyError(exc.code) from exc
                created = {}
                try:
                    source_tree.attach_quarantine(
                        *_ensure_apply_quarantine(
                            mutation_store,
                            journal["journal_id"],
                        )
                    )
                    self._fault("after_journal", "")
                    self._apply_intents(
                        source_tree,
                        journal,
                        intents,
                        missing_dirs,
                        created,
                    )
                    by_path = {
                        item["path"]: item for item in journal["entries"]
                    }
                    for intent in intents:
                        observed = source_tree.inspect(intent["path"])
                        entry = by_path[intent["path"]]
                        after_identity = (
                            entry["after_identity"]["device"],
                            entry["after_identity"]["inode"],
                        )
                        prepared_identity = (
                            entry["prepared_identity"]["device"],
                            entry["prepared_identity"]["inode"],
                        )
                        published_identity = (
                            after_identity
                            if after_identity != (0, 0)
                            else prepared_identity
                        )
                        if (
                            not _observed_matches(observed, intent["after"])
                            or observed["exists"]
                            and (
                                published_identity == (0, 0)
                                or observed["identity"] != published_identity
                            )
                        ):
                            raise SandboxApplyError("source_apply_uncertain")
                except Exception:
                    try:
                        self._rollback(
                            source_tree,
                            journal,
                            missing_dirs,
                            created,
                        )
                    except Exception:
                        return self._finish(
                            journal,
                            "apply_review_required",
                            mutation_store,
                            error_code="source_apply_uncertain",
                        )
                    return self._finish(
                        journal,
                        "apply_failed_rolled_back",
                        mutation_store,
                        error_code="source_apply_failed",
                    )
                return self._finish(journal, "apply_applied", mutation_store)
            finally:
                staging_tree.close()
                source_tree.close()

    def reconcile(self):
        current = self.context.current_session()
        apply_state = current.manifest["apply"]
        if current.state != "applying" or apply_state["status"] != "applying":
            raise SandboxApplyError("sandbox_apply_not_reconcilable")
        journal = self.store.load_journal(
            apply_state["journal_id"],
            sandbox_id=current.sandbox_id,
        )
        if (
            journal["diff_digest"] != current.manifest["diff"]["digest"]
            or journal["source"]
            != {
                "root": current.manifest["source"]["root"],
                "device": current.manifest["source"]["device"],
                "inode": current.manifest["source"]["inode"],
            }
        ):
            raise SandboxApplyError("sandbox_apply_journal_invalid")
        with self._source_mutation_lock(apply_state["journal_id"]) as mutation_store:
            try:
                mutation_store.begin_source_apply_guard(
                    journal_id=journal["journal_id"],
                    sandbox_id=current.sandbox_id,
                    diff_digest=journal["diff_digest"],
                )
            except CheckpointStoreError as exc:
                raise SandboxApplyError(exc.code) from exc
            tree = _SourceTree(
                self.context.source_root,
                (journal["source"]["device"], journal["source"]["inode"]),
            )
            try:
                try:
                    tree.attach_quarantine(
                        *_ensure_apply_quarantine(
                            mutation_store,
                            journal["journal_id"],
                        )
                    )
                except SandboxApplyError:
                    return self._finish(
                        journal,
                        "apply_review_required",
                        mutation_store,
                        error_code="source_apply_uncertain",
                    )
                created = {
                    item["path"]: (item["device"], item["inode"])
                    for item in journal["created_dirs"]
                    if item["status"] == "created"
                }
                missing_dirs = [item["path"] for item in journal["created_dirs"]]
                classifications = []
                rollback_started = False
                for entry in journal["entries"]:
                    try:
                        tree.reconcile_temp(
                            entry,
                            planned_missing=missing_dirs,
                            created=created,
                        )
                    except SandboxApplyError:
                        return self._finish(
                            journal,
                            "apply_review_required",
                            mutation_store,
                            error_code="source_apply_uncertain",
                        )
                    observed = tree.inspect(entry["path"])
                    current_parents = {
                        item["path"]: (item["device"], item["inode"])
                        for item in observed["parent_identities"]
                    }
                    if any(
                        current_parents.get(item["path"])
                        != (item["device"], item["inode"])
                        for item in entry["parent_identities"]
                    ):
                        classifications.append("unknown")
                    else:
                        before_identity = (
                            entry["before_identity"]["device"],
                            entry["before_identity"]["inode"],
                        )
                        after_identity = (
                            entry["after_identity"]["device"],
                            entry["after_identity"]["inode"],
                        )
                        prepared_identity = (
                            entry["prepared_identity"]["device"],
                            entry["prepared_identity"]["inode"],
                        )
                        published_identity = (
                            after_identity
                            if after_identity != (0, 0)
                            else prepared_identity
                        )
                        rollback_started = rollback_started or bool(
                            after_identity != (0, 0)
                            and prepared_identity != after_identity
                        )
                        before_matches = bool(
                            _observed_matches(observed, entry["before"])
                            and (
                                not observed["exists"]
                                or observed["identity"]
                                in {before_identity, prepared_identity}
                            )
                        )
                        after_matches = bool(
                            _observed_matches(observed, entry["after"])
                            and (
                                not observed["exists"]
                                or published_identity != (0, 0)
                                and observed["identity"] == published_identity
                            )
                        )
                        if before_matches:
                            classifications.append("before")
                        elif after_matches:
                            classifications.append("after")
                        else:
                            classifications.append("unknown")
                if "unknown" in classifications:
                    return self._finish(
                        journal,
                        "apply_review_required",
                        mutation_store,
                        error_code="source_apply_uncertain",
                    )
                if not rollback_started and classifications and all(
                    item == "after" for item in classifications
                ):
                    for entry in journal["entries"]:
                        if (
                            entry["after"]["exists"]
                            and not any(entry["after_identity"].values())
                        ):
                            entry["after_identity"] = dict(
                                entry["prepared_identity"]
                            )
                        entry["status"] = "applied"
                    return self._finish(journal, "apply_applied", mutation_store)
                try:
                    self._rollback(
                        tree,
                        journal,
                        missing_dirs,
                        created,
                    )
                except Exception:
                    return self._finish(
                        journal,
                        "apply_review_required",
                        mutation_store,
                        error_code="source_apply_uncertain",
                    )
                return self._finish(
                    journal,
                    "apply_failed_rolled_back",
                    mutation_store,
                    error_code="source_apply_failed",
                )
            finally:
                tree.close()
