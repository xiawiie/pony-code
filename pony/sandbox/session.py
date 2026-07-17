"""Docker Sandbox session state, filtered staging, and workspace mapping."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import unicodedata

from pony.state import file_lock
from pony.tools.subprocess import run_hardened_git
from pony.security.private_files import (
    ensure_private_dir,
    private_directory_identity,
    write_private_bytes_atomic,
)
from pony.security.paths import is_allowed_env_template_leaf, sensitive_path_reason
from pony.security.redaction import contains_secret_material
from pony.workspace.context import now


FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_BASELINE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ENV_TEMPLATE_BYTES = 1024 * 1024
STAGING_CHUNK_BYTES = 1024 * 1024
MAX_LOGICAL_BYTES = 1024 * 1024 * 1024
MAX_ALLOCATED_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_DEPTH = 32
MAX_CLEANUP_DELETE_ENTRIES = 10_000
MAX_CLEANUP_ARTIFACTS_BYTES = 64 * 1024
LOGICAL_ROOT = PurePosixPath("/workspace")
_WORKSPACE_TRASH_NAME = "trash-workspace"
_CONTENT_BLOBS_TRASH_NAME = "trash-content-blobs"
_CLEANUP_ARTIFACTS_NAME = "cleanup-artifacts.json"
_STAGING_BLOBS_RELATIVE = Path("recovery/.pony/checkpoints/blobs")
_SOURCE_APPLY_CONTROL_NAME = ".source-apply-control"
_SOURCE_APPLY_AUTHORITY_NAME = "active.json"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SANDBOX_ID_RE = re.compile(r"^sandbox_[0-9a-f]{32}$")
_WORKSPACE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_APPLY_ID_RE = re.compile(r"^apply_[0-9a-f]{32}$")
_SOURCE_APPLY_AUTHORITY_FIELDS = {
    "record_type",
    "format_version",
    "source_root",
    "source_device",
    "source_inode",
    "sandbox_id",
    "state_root",
    "state_device",
    "state_inode",
    "control_device",
    "control_inode",
    "journal_id",
    "diff_digest",
}
_MANIFEST_FIELDS = {
    "record_type",
    "format_version",
    "sandbox_id",
    "pony_session_id",
    "state",
    "created_at",
    "updated_at",
    "source",
    "execution",
    "engine",
    "image",
    "policy",
    "lease",
    "active_call",
    "diff",
    "apply",
    "cleanup",
    "sidecar",
}
_ENGINE_FIELDS = {
    "endpoint_hash",
    "client_version",
    "server_version",
    "api_version",
    "profile",
    "security_digest",
}
_IMAGE_FIELDS = {"image_digest", "image_id", "platform"}
_POLICY_FIELDS = {
    "version",
    "digest",
    "network",
    "mount_digest",
    "resource_digest",
}
_STATES = {
    "creating",
    "ready",
    "running",
    "pending_review",
    "applying",
    "discarding",
    "applied",
    "discarded",
    "cleanup_pending",
    "review_required",
    "failed",
}
_GENERATED_DIRS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_AGENT_DIRS = {".claude", ".superpowers"}


class SandboxSessionError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SandboxSessionError("sandbox_manifest_invalid")
        value[key] = item
    return value


def _decode_json(raw):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SandboxSessionError("sandbox_manifest_invalid")
            ),
        )
    except SandboxSessionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxSessionError("sandbox_manifest_invalid") from exc
    if not isinstance(value, dict):
        raise SandboxSessionError("sandbox_manifest_invalid")
    return value


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _staged_mode(source_mode):
    return 0o755 if source_mode & stat.S_IXUSR else 0o644


def _read_strict_file(path, *, max_bytes, parent_descriptor=None):
    descriptor = -1
    target = Path(path).name if parent_descriptor is not None else path
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else before.st_uid
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > max_bytes
        ):
            raise SandboxSessionError("sandbox_manifest_invalid")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(
            target,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            len(raw) > max_bytes
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
        ):
            raise SandboxSessionError("sandbox_manifest_invalid")
        return raw
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("sandbox_manifest_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _atomic_json(path, root, value, *, max_bytes=MAX_MANIFEST_BYTES):
    raw = _canonical_json(value)
    if len(raw) > max_bytes:
        raise SandboxSessionError("sandbox_manifest_invalid")
    write_private_bytes_atomic(
        path,
        raw,
        trusted_root=root,
        trusted_root_identity=private_directory_identity(root),
        max_existing_bytes=max_bytes,
    )


def _is_integer(value, *, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_absolute_path(value):
    return isinstance(value, str) and bool(value) and Path(value).is_absolute()


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _validate_identity_metadata(engine, image, policy):
    if (
        not isinstance(engine, dict)
        or set(engine) != _ENGINE_FIELDS
        or not _matches(_SHA256_RE, engine["endpoint_hash"])
        or not _matches(_SHA256_RE, engine["security_digest"])
        or not isinstance(engine["profile"], str)
        or engine["profile"] not in {"desktop_vm", "linux_rootless"}
        or not isinstance(engine["api_version"], str)
        or re.fullmatch(r"[0-9]+\.[0-9]+", engine["api_version"] or "") is None
        or any(
            not isinstance(engine[name], str)
            or not engine[name]
            or len(engine[name]) > 100
            or _CONTROL_RE.search(engine[name])
            for name in ("client_version", "server_version")
        )
        or not isinstance(image, dict)
        or set(image) != _IMAGE_FIELDS
        or not _matches(_SHA256_RE, image["image_digest"])
        or not _matches(_SHA256_RE, image["image_id"])
        or not isinstance(image["platform"], str)
        or image["platform"] not in {"linux/arm64", "linux/amd64"}
        or not isinstance(policy, dict)
        or set(policy) != _POLICY_FIELDS
        or type(policy["version"]) is not int
        or policy["version"] != 1
        or policy["network"] != "none"
        or any(
            not _matches(_SHA256_RE, policy[name])
            for name in ("digest", "mount_digest", "resource_digest")
        )
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    return dict(engine), dict(image), dict(policy)


def _validate_manifest(value):
    if set(value) != _MANIFEST_FIELDS:
        raise SandboxSessionError("sandbox_manifest_invalid")
    if (
        value["record_type"] != "docker_sandbox_session"
        or value["format_version"] != FORMAT_VERSION
        or not _matches(_SANDBOX_ID_RE, value["sandbox_id"])
        or not isinstance(value["state"], str)
        or value["state"] not in _STATES
        or not isinstance(value["pony_session_id"], str)
        or not isinstance(value["created_at"], str)
        or not isinstance(value["updated_at"], str)
        or not isinstance(value["source"], dict)
        or not isinstance(value["execution"], dict)
        or not isinstance(value["engine"], dict)
        or not isinstance(value["image"], dict)
        or not isinstance(value["policy"], dict)
        or value["active_call"] is not None
        and not isinstance(value["active_call"], dict)
        or value["lease"] is not None
        and not isinstance(value["lease"], dict)
        or not isinstance(value["diff"], dict)
        or not isinstance(value["apply"], dict)
        or not isinstance(value["cleanup"], dict)
        or value["sidecar"] is not None
        and not isinstance(value["sidecar"], dict)
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    source = value["source"]
    execution = value["execution"]
    _validate_identity_metadata(value["engine"], value["image"], value["policy"])
    if set(source) != {
        "root",
        "device",
        "inode",
        "baseline_digest",
        "branch",
        "head",
    } or set(execution) != {
        "root",
        "device",
        "inode",
        "tree_digest",
        "file_count",
        "logical_bytes",
        "allocated_bytes",
        "synthetic_git_commit",
    }:
        raise SandboxSessionError("sandbox_manifest_invalid")
    for name in (source["baseline_digest"], execution["tree_digest"]):
        if name and not _matches(_SHA256_RE, name):
            raise SandboxSessionError("sandbox_manifest_invalid")
    if (
        not _is_absolute_path(source["root"])
        or not _is_integer(source["device"], minimum=1)
        or not _is_integer(source["inode"], minimum=1)
        or not isinstance(source["branch"], str)
        or not isinstance(source["head"], str)
        or not _is_absolute_path(execution["root"])
        or not all(
            _is_integer(execution[name])
            for name in (
                "device",
                "inode",
                "file_count",
                "logical_bytes",
                "allocated_bytes",
            )
        )
        or not isinstance(execution["synthetic_git_commit"], str)
        or execution["synthetic_git_commit"]
        and re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}",
            execution["synthetic_git_commit"],
        )
        is None
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    lease = value["lease"]
    if lease is not None and (
        set(lease) != {"owner_pid", "owner_start", "owner_nonce", "acquired_at"}
        or not _is_integer(lease["owner_pid"], minimum=1)
        or not isinstance(lease["owner_start"], str)
        or not lease["owner_start"]
        or not isinstance(lease["owner_nonce"], str)
        or re.fullmatch(r"[0-9a-f]{64}", lease["owner_nonce"]) is None
        or not isinstance(lease["acquired_at"], str)
        or not lease["acquired_at"]
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    active = value["active_call"]
    if active is not None and (
        set(active)
        != {
            "call_id",
            "reconciliation_token",
            "container_name",
            "expected_labels",
            "plan_digest",
            "container_id",
            "return_state",
            "reconciliation",
        }
        or not isinstance(active["call_id"], str)
        or not active["call_id"]
        or not isinstance(active["container_name"], str)
        or not active["container_name"]
        or not isinstance(active["reconciliation_token"], str)
        or re.fullmatch(r"[0-9a-f]{64}", active["reconciliation_token"]) is None
        or not _matches(_SHA256_RE, active["plan_digest"])
        or not isinstance(active["container_id"], str)
        or active["container_id"]
        and re.fullmatch(r"[0-9a-f]{64}", active["container_id"]) is None
        or not isinstance(active["expected_labels"], dict)
        or not active["expected_labels"]
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in active["expected_labels"].items()
        )
        or not isinstance(active["return_state"], str)
        or active["return_state"] not in {"creating", "ready"}
        or not isinstance(active["reconciliation"], dict)
        or set(active["reconciliation"])
        != {"status", "target_started", "cleanup_status", "error_code"}
        or active["reconciliation"]["status"] not in {"not_started", "review_required"}
        or active["reconciliation"]["target_started"] is not None
        and type(active["reconciliation"]["target_started"]) is not bool
        or active["reconciliation"]["cleanup_status"]
        not in {"not_started", "not_attempted", "pending", "completed", "failed"}
        or not isinstance(active["reconciliation"]["error_code"], str)
        or active["reconciliation"]["status"] == "not_started"
        and active["reconciliation"]
        != {
            "status": "not_started",
            "target_started": None,
            "cleanup_status": "not_started",
            "error_code": "",
        }
        or active["reconciliation"]["status"] == "review_required"
        and (
            not active["reconciliation"]["error_code"]
            or active["reconciliation"]["cleanup_status"] == "not_started"
        )
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    diff = value["diff"]
    apply = value["apply"]
    cleanup = value["cleanup"]
    if (
        set(diff) != {"digest", "status", "candidate_count", "blocked_count"}
        or not isinstance(diff["digest"], str)
        or diff["digest"]
        and not _matches(_SHA256_RE, diff["digest"])
        or not isinstance(diff["status"], str)
        or diff["status"] not in {"not_generated", "diff_ready", "diff_blocked"}
        or not _is_integer(diff["candidate_count"])
        or not _is_integer(diff["blocked_count"])
        or set(apply) != {"journal_id", "status"}
        or not isinstance(apply["journal_id"], str)
        or not isinstance(apply["status"], str)
        or apply["status"]
        not in {
            "not_started",
            "applying",
            "apply_conflicted",
            "apply_applied",
            "apply_failed_rolled_back",
            "apply_review_required",
        }
        or set(cleanup) != {"status", "last_error_code"}
        or not isinstance(cleanup["status"], str)
        or not isinstance(cleanup["last_error_code"], str)
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    if (
        diff["status"] == "not_generated"
        and (diff["digest"] or diff["candidate_count"] or diff["blocked_count"])
        or diff["status"] == "diff_ready"
        and (not diff["digest"] or diff["blocked_count"])
        or diff["status"] == "diff_blocked"
        and (not diff["digest"] or not diff["blocked_count"])
        or apply["status"] in {"not_started", "apply_conflicted"}
        and apply["journal_id"]
        or apply["status"]
        in {
            "applying",
            "apply_applied",
            "apply_failed_rolled_back",
            "apply_review_required",
        }
        and _APPLY_ID_RE.fullmatch(apply["journal_id"]) is None
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    sidecar = value["sidecar"]
    if sidecar is not None and (
        set(sidecar) != {"path", "parent_device", "parent_inode"}
        or not _is_absolute_path(sidecar["path"])
        or not _is_integer(sidecar["parent_device"], minimum=1)
        or not _is_integer(sidecar["parent_inode"], minimum=1)
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    state = value["state"]
    if (
        state == "running"
        and active is None
        or state == "ready"
        and active is not None
        or state in {"applied", "discarded"}
        and (active is not None or lease is not None)
        or state not in {"creating", "failed"}
        and (
            not source["baseline_digest"]
            or not execution["tree_digest"]
            or not _is_integer(execution["device"], minimum=1)
            or not _is_integer(execution["inode"], minimum=1)
        )
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    return value


def _source_apply_authority_path(parent, source_root):
    workspace = Path(parent) / SandboxSessionStore._workspace_id(source_root)
    return workspace / _SOURCE_APPLY_CONTROL_NAME / _SOURCE_APPLY_AUTHORITY_NAME


def source_apply_control_lock_path(parent, source_root):
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    workspace = Path(parent) / SandboxSessionStore._workspace_id(source_root)
    return workspace / _SOURCE_APPLY_CONTROL_NAME / ".lock"


@contextmanager
def source_mutation_authority(parent, source_root):
    """Serialize host source mutations with unresolved Sandbox Apply."""
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    lock_path = source_apply_control_lock_path(parent, source_root)
    if file_lock.lock_is_active(lock_path):
        if read_source_apply_authority(parent, source_root) is not None:
            raise SandboxSessionError("source_apply_review_required")
        yield
        return
    stack = ExitStack()
    try:
        stack.enter_context(file_lock.locked_file(lock_path, require_lock=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    with stack:
        if read_source_apply_authority(parent, source_root) is not None:
            raise SandboxSessionError("source_apply_review_required")
        yield


def _validate_source_apply_authority(
    value,
    *,
    parent,
    source_root,
    control_identity=None,
):
    if (
        not isinstance(value, dict)
        or set(value) != _SOURCE_APPLY_AUTHORITY_FIELDS
        or value["record_type"] != "docker_sandbox_source_apply_authority"
        or value["format_version"] != FORMAT_VERSION
        or value["source_root"] != str(source_root)
        or not _is_integer(value["source_device"], minimum=1)
        or not _is_integer(value["source_inode"], minimum=1)
        or not _matches(_SANDBOX_ID_RE, value["sandbox_id"])
        or not _is_absolute_path(value["state_root"])
        or not _is_integer(value["state_device"], minimum=1)
        or not _is_integer(value["state_inode"], minimum=1)
        or not _is_integer(value["control_device"], minimum=1)
        or not _is_integer(value["control_inode"], minimum=1)
        or not _matches(_APPLY_ID_RE, value["journal_id"])
        or not _matches(_SHA256_RE, value["diff_digest"])
    ):
        raise SandboxSessionError("sandbox_state_invalid")
    state_root = Path(value["state_root"])
    expected = Path(parent) / SandboxSessionStore._workspace_id(source_root)
    control_root = _source_apply_authority_path(parent, source_root).parent
    try:
        state_info = state_root.lstat()
        state_identity = private_directory_identity(state_root)
        if control_identity is None:
            control_identity = private_directory_identity(control_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    if (
        state_root.parent != expected
        or state_root.name != value["sandbox_id"]
        or not stat.S_ISDIR(state_info.st_mode)
        or state_root.is_symlink()
        or (state_info.st_dev, state_info.st_ino)
        != (value["state_device"], value["state_inode"])
        or state_identity != (state_info.st_dev, state_info.st_ino)
        or tuple(control_identity) != (value["control_device"], value["control_inode"])
    ):
        raise SandboxSessionError("sandbox_state_invalid")
    return value


def read_source_apply_authority(parent, source_root):
    """Read the external unresolved-Apply authority without creating state."""
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    path = _source_apply_authority_path(parent, source_root)
    try:
        raw = _read_strict_file(path, max_bytes=MAX_MANIFEST_BYTES)
    except SandboxSessionError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            pass
        raise SandboxSessionError("sandbox_state_invalid") from exc
    try:
        return _validate_source_apply_authority(
            _decode_json(raw),
            parent=parent,
            source_root=source_root,
        )
    except SandboxSessionError as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc


def write_source_apply_authority(
    parent,
    source_root,
    *,
    source_device,
    source_inode,
    state_root,
    sandbox_id,
    journal_id,
    diff_digest,
):
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    state_root = Path(os.path.abspath(os.fspath(state_root)))
    path = _source_apply_authority_path(parent, source_root)
    try:
        workspace = ensure_private_dir(path.parent)
        value = make_source_apply_authority(
            parent,
            source_root,
            source_device=source_device,
            source_inode=source_inode,
            state_root=state_root,
            sandbox_id=sandbox_id,
            journal_id=journal_id,
            diff_digest=diff_digest,
        )
        existing = read_source_apply_authority(parent, source_root)
        if existing is not None and existing != value:
            raise SandboxSessionError("sandbox_state_invalid")
        if existing is None:
            _atomic_json(path, workspace, value)
        return value
    except SandboxSessionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc


def make_source_apply_authority(
    parent,
    source_root,
    *,
    source_device,
    source_inode,
    state_root,
    sandbox_id,
    journal_id,
    diff_digest,
):
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    state_root = Path(os.path.abspath(os.fspath(state_root)))
    path = _source_apply_authority_path(parent, source_root)
    try:
        state_info = state_root.lstat()
        control_info = path.parent.lstat()
        control_identity = private_directory_identity(path.parent)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    if (
        not stat.S_ISDIR(control_info.st_mode)
        or (control_info.st_dev, control_info.st_ino) != control_identity
    ):
        raise SandboxSessionError("sandbox_state_invalid")
    return _validate_source_apply_authority(
        {
            "record_type": "docker_sandbox_source_apply_authority",
            "format_version": FORMAT_VERSION,
            "source_root": str(source_root),
            "source_device": source_device,
            "source_inode": source_inode,
            "sandbox_id": str(sandbox_id),
            "state_root": str(state_root),
            "state_device": state_info.st_dev,
            "state_inode": state_info.st_ino,
            "control_device": control_info.st_dev,
            "control_inode": control_info.st_ino,
            "journal_id": str(journal_id),
            "diff_digest": str(diff_digest),
        },
        parent=parent,
        source_root=source_root,
    )


def clear_source_apply_authority(
    parent,
    source_root,
    *,
    expected_authority,
):
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    current = read_source_apply_authority(parent, source_root)
    if current is None or current != expected_authority:
        raise SandboxSessionError("sandbox_state_invalid")
    path = _source_apply_authority_path(parent, source_root)
    parent_descriptor = -1
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_parent = os.fstat(parent_descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else opened_parent.st_uid
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != uid
            or stat.S_IMODE(opened_parent.st_mode) != 0o700
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (current["control_device"], current["control_inode"])
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SandboxSessionError("sandbox_state_invalid")
        verified = _validate_source_apply_authority(
            _decode_json(
                _read_strict_file(
                    path.name,
                    max_bytes=MAX_MANIFEST_BYTES,
                    parent_descriptor=parent_descriptor,
                )
            ),
            parent=parent,
            source_root=source_root,
            control_identity=(opened_parent.st_dev, opened_parent.st_ino),
        )
        current_after = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            verified != current
            or _identity(current_after) != _identity(before)
            or private_directory_identity(path.parent)
            != (opened_parent.st_dev, opened_parent.st_ino)
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        try:
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise SandboxSessionError("sandbox_state_invalid")
        if private_directory_identity(path.parent) != (
            opened_parent.st_dev,
            opened_parent.st_ino,
        ):
            raise SandboxSessionError("sandbox_state_invalid")
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    return True


def _validate_cleanup_artifacts(value, sandbox_id):
    if (
        not isinstance(value, dict)
        or set(value)
        != {"record_type", "format_version", "sandbox_id", "staging_blobs"}
        or value["record_type"] != "docker_sandbox_cleanup_artifacts"
        or value["format_version"] != FORMAT_VERSION
        or value["sandbox_id"] != sandbox_id
        or not isinstance(value["staging_blobs"], dict)
        or set(value["staging_blobs"]) != {"status", "device", "inode"}
    ):
        raise SandboxSessionError("sandbox_cleanup_failed")
    blobs = value["staging_blobs"]
    if (
        blobs["status"] not in {"absent", "planned"}
        or type(blobs["device"]) is not int
        or type(blobs["inode"]) is not int
        or blobs["status"] == "absent"
        and (blobs["device"] != 0 or blobs["inode"] != 0)
        or blobs["status"] == "planned"
        and (blobs["device"] <= 0 or blobs["inode"] <= 0)
    ):
        raise SandboxSessionError("sandbox_cleanup_failed")
    return value


def _validate_baseline(value, sandbox_id):
    if set(value) != {
        "record_type",
        "format_version",
        "sandbox_id",
        "tree_digest",
        "entries",
        "tracked_paths",
        "untracked_paths",
        "excluded_counts",
    } or (
        value["record_type"] != "docker_sandbox_baseline"
        or value["format_version"] != FORMAT_VERSION
        or value["sandbox_id"] != sandbox_id
        or not _matches(_SHA256_RE, value["tree_digest"])
        or not isinstance(value["entries"], list)
        or not isinstance(value["tracked_paths"], list)
        or not isinstance(value["untracked_paths"], list)
        or not isinstance(value["excluded_counts"], dict)
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    paths = []
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "sha256",
            "size",
            "mode",
            "uid",
            "gid",
        }:
            raise SandboxSessionError("sandbox_manifest_invalid")
        path = _validated_relative(entry["path"]).as_posix()
        if (
            path != entry["path"]
            or not _matches(_SHA256_RE, entry["sha256"])
            or not _is_integer(entry["size"])
            or not _is_integer(entry["mode"])
            or entry["mode"] > 0o7777
            or not _is_integer(entry["uid"])
            or not _is_integer(entry["gid"])
        ):
            raise SandboxSessionError("sandbox_manifest_invalid")
        paths.append(path)
    tracked = value["tracked_paths"]
    untracked = value["untracked_paths"]
    if any(not isinstance(path, str) for path in tracked + untracked):
        raise SandboxSessionError("sandbox_manifest_invalid")
    if (
        paths != sorted(set(paths))
        or tracked != sorted(set(tracked))
        or untracked != sorted(set(untracked))
        or set(tracked) & set(untracked)
        or set(tracked) | set(untracked) != set(paths)
        or any(path not in paths for path in tracked + untracked)
        or any(
            not isinstance(key, str) or not _is_integer(item)
            for key, item in value["excluded_counts"].items()
        )
        or value["tree_digest"] != _sha256(_canonical_json(value["entries"]))
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    return value


def _validate_sidecar_shape(value):
    if not isinstance(value, dict) or set(value) != {
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
    }:
        raise SandboxSessionError("sandbox_manifest_invalid")
    if (
        value["record_type"] != "docker_sandbox_session_pointer"
        or value["format_version"] != FORMAT_VERSION
        or not isinstance(value["pony_session_id"], str)
        or not _matches(_SANDBOX_ID_RE, value["sandbox_id"])
        or not _is_absolute_path(value["source_root"])
        or not _is_integer(value["source_device"], minimum=1)
        or not _is_integer(value["source_inode"], minimum=1)
        or not _is_absolute_path(value["state_root"])
        or not _is_integer(value["state_device"], minimum=1)
        or not _is_integer(value["state_inode"], minimum=1)
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    return value


def _validate_sidecar(
    value,
    manifest,
    state_root,
    *,
    require_live_source=True,
):
    _validate_sidecar_shape(value)
    try:
        source_root = Path(value["source_root"])
        root_info = state_root.lstat()
        state_identity = private_directory_identity(state_root)
        if require_live_source:
            source_info = source_root.lstat()
            source_identity = private_directory_identity(source_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_manifest_invalid") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or state_root.is_symlink()
        or state_identity != (root_info.st_dev, root_info.st_ino)
        or value["pony_session_id"] != manifest["pony_session_id"]
        or value["sandbox_id"] != manifest["sandbox_id"]
        or value["source_root"] != manifest["source"]["root"]
        or (value["source_device"], value["source_inode"])
        != (manifest["source"]["device"], manifest["source"]["inode"])
        or value["state_root"] != str(state_root)
        or (value["state_device"], value["state_inode"])
        != (root_info.st_dev, root_info.st_ino)
        or require_live_source
        and (
            not stat.S_ISDIR(source_info.st_mode)
            or source_root.is_symlink()
            or source_identity != (source_info.st_dev, source_info.st_ino)
            or (value["source_device"], value["source_inode"])
            != (source_info.st_dev, source_info.st_ino)
        )
    ):
        raise SandboxSessionError("sandbox_manifest_invalid")
    return value


def _sidecar_metadata(project_state_root, sandbox_id):
    if project_state_root is None:
        return None
    parent = ensure_private_dir(Path(project_state_root) / "sandbox_sessions")
    info = parent.lstat()
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISDIR(info.st_mode)
        or parent.is_symlink()
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SandboxSessionError("sandbox_state_invalid")
    return {
        "path": str(parent / f"{sandbox_id}.json"),
        "parent_device": info.st_dev,
        "parent_inode": info.st_ino,
    }


def _sidecar_location(manifest):
    sidecar = manifest["sidecar"]
    if sidecar is None:
        return None
    sidecar_path = Path(sidecar["path"])
    parent = sidecar_path.parent
    try:
        parent_info = parent.lstat()
        parent_identity = private_directory_identity(parent)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else parent_info.st_uid
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent.is_symlink()
        or parent_info.st_uid != uid
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or (parent_info.st_dev, parent_info.st_ino)
        != (sidecar["parent_device"], sidecar["parent_inode"])
        or sidecar_path.name != f"{manifest['sandbox_id']}.json"
    ):
        raise SandboxSessionError("sandbox_state_invalid")
    if parent_identity != (parent_info.st_dev, parent_info.st_ino):
        raise SandboxSessionError("sandbox_state_invalid")
    return sidecar_path, parent


def _sidecar_pointer(manifest, state_root):
    source = manifest["source"]
    state_info = state_root.lstat()
    return {
        "record_type": "docker_sandbox_session_pointer",
        "format_version": FORMAT_VERSION,
        "pony_session_id": manifest["pony_session_id"],
        "sandbox_id": manifest["sandbox_id"],
        "source_root": source["root"],
        "source_device": source["device"],
        "source_inode": source["inode"],
        "state_root": str(state_root),
        "state_device": state_info.st_dev,
        "state_inode": state_info.st_ino,
    }


def _missing_sidecar_after_source_replacement(manifest, state_root):
    sidecar = manifest["sidecar"]
    if sidecar is None:
        return False
    source_root = Path(manifest["source"]["root"])
    expected_parent = source_root / ".pony" / "sandbox_sessions"
    if Path(sidecar["path"]) != expected_parent / f"{manifest['sandbox_id']}.json":
        return False
    try:
        source_info = source_root.lstat()
        state_info = state_root.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(source_info.st_mode)
        or source_root.is_symlink()
        or (source_info.st_dev, source_info.st_ino)
        == (manifest["source"]["device"], manifest["source"]["inode"])
        or not stat.S_ISDIR(state_info.st_mode)
        or state_root.is_symlink()
    ):
        return False
    try:
        (source_root / ".pony").lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _read_bound_sidecar(
    manifest,
    state_root,
    *,
    require_live_source=True,
    allow_missing_after_source_replacement=False,
):
    try:
        location = _sidecar_location(manifest)
    except SandboxSessionError:
        if (
            allow_missing_after_source_replacement
            and _missing_sidecar_after_source_replacement(manifest, state_root)
        ):
            return None
        raise
    if location is None:
        return None
    sidecar_path, parent = location
    try:
        before = private_directory_identity(parent)
        value = _validate_sidecar(
            _decode_json(
                _read_strict_file(
                    sidecar_path,
                    max_bytes=MAX_MANIFEST_BYTES,
                )
            ),
            manifest,
            state_root,
            require_live_source=require_live_source,
        )
        if private_directory_identity(parent) != before:
            raise SandboxSessionError("sandbox_manifest_invalid")
        return value
    except SandboxSessionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_manifest_invalid") from exc


def _create_session_sidecar(manifest, state_root):
    location = _sidecar_location(manifest)
    if location is None:
        return
    sidecar_path, parent = location
    pointer = _validate_sidecar(
        _sidecar_pointer(manifest, state_root),
        manifest,
        state_root,
    )
    temp_path = parent / f".{manifest['sandbox_id']}.{secrets.token_hex(12)}.tmp"
    parent_descriptor = -1
    temp_identity = None
    try:
        _atomic_json(temp_path, parent, pointer)
        temp_info = temp_path.lstat()
        temp_identity = (temp_info.st_dev, temp_info.st_ino)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_descriptor = os.open(parent, flags)
        parent_info = os.fstat(parent_descriptor)
        current_temp = os.stat(
            temp_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (parent_info.st_dev, parent_info.st_ino)
            != tuple(private_directory_identity(parent))
            or not stat.S_ISREG(temp_info.st_mode)
            or temp_info.st_nlink != 1
            or stat.S_IMODE(temp_info.st_mode) != 0o600
            or (current_temp.st_dev, current_temp.st_ino) != temp_identity
            or current_temp.st_nlink != 1
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        from pony.recovery.manager import _rename_noreplace

        _rename_noreplace(
            parent_descriptor,
            temp_path.name,
            parent_descriptor,
            sidecar_path.name,
        )
        temp_identity = None
        os.fsync(parent_descriptor)
        published = os.stat(
            sidecar_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
            or (published.st_dev, published.st_ino)
            != (temp_info.st_dev, temp_info.st_ino)
            or tuple(private_directory_identity(parent))
            != (parent_info.st_dev, parent_info.st_ino)
        ):
            raise SandboxSessionError("sandbox_state_invalid")
    except SandboxSessionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    finally:
        if parent_descriptor >= 0:
            if temp_identity is not None:
                try:
                    current = os.stat(
                        temp_path.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) == temp_identity:
                        os.unlink(temp_path.name, dir_fd=parent_descriptor)
                        os.fsync(parent_descriptor)
                except OSError:
                    pass
            os.close(parent_descriptor)


def _write_session_manifest(
    state_root,
    manifest,
    *,
    create_sidecar=False,
    require_live_source=True,
    allow_missing_sidecar_after_source_replacement=False,
):
    _validate_manifest(manifest)
    if create_sidecar:
        _create_session_sidecar(manifest, state_root)
    else:
        _read_bound_sidecar(
            manifest,
            state_root,
            require_live_source=require_live_source,
            allow_missing_after_source_replacement=(
                allow_missing_sidecar_after_source_replacement
            ),
        )
    _atomic_json(state_root / "manifest.json", state_root, manifest)


def _reconcile_creation_sidecar_orphans(store, project_state_root, source_root):
    sidecar_parent = Path(project_state_root) / "sandbox_sessions"
    try:
        parent_info = sidecar_parent.lstat()
    except FileNotFoundError:
        return
    uid = os.geteuid() if hasattr(os, "geteuid") else parent_info.st_uid
    try:
        parent_identity = private_directory_identity(sidecar_parent)
        source_info = source_root.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or sidecar_parent.is_symlink()
        or parent_info.st_uid != uid
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or parent_identity != (parent_info.st_dev, parent_info.st_ino)
        or not stat.S_ISDIR(source_info.st_mode)
        or source_root.is_symlink()
    ):
        raise SandboxSessionError("sandbox_state_invalid")
    expected_workspace = store.parent / store._workspace_id(source_root)
    for sidecar_path in sorted(sidecar_parent.iterdir(), key=lambda item: item.name):
        match = re.fullmatch(r"(sandbox_[0-9a-f]{32})\.json", sidecar_path.name)
        if match is None:
            raise SandboxSessionError("sandbox_state_invalid")
        sidecar_info = sidecar_path.lstat()
        raw = _read_strict_file(sidecar_path, max_bytes=MAX_MANIFEST_BYTES)
        pointer = _validate_sidecar_shape(_decode_json(raw))
        expected_state_root = expected_workspace / pointer["sandbox_id"]
        if (
            pointer["sandbox_id"] != match.group(1)
            or pointer["source_root"] != str(source_root)
            or (pointer["source_device"], pointer["source_inode"])
            != (source_info.st_dev, source_info.st_ino)
            or Path(pointer["state_root"]) != expected_state_root
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        manifest_path = expected_state_root / "manifest.json"
        try:
            manifest_path.lstat()
        except FileNotFoundError:
            pass
        else:
            manifest = _validate_manifest(
                _decode_json(
                    _read_strict_file(
                        manifest_path,
                        max_bytes=MAX_MANIFEST_BYTES,
                    )
                )
            )
            _validate_sidecar(pointer, manifest, expected_state_root)
            continue
        try:
            state_info = expected_state_root.lstat()
        except FileNotFoundError:
            state_info = None
        if state_info is not None:
            try:
                state_identity = private_directory_identity(expected_state_root)
                entries = list(expected_state_root.iterdir())
            except (OSError, RuntimeError, ValueError) as exc:
                raise SandboxSessionError("sandbox_state_invalid") from exc
            if (
                not stat.S_ISDIR(state_info.st_mode)
                or expected_state_root.is_symlink()
                or state_info.st_uid != uid
                or stat.S_IMODE(state_info.st_mode) != 0o700
                or state_identity != (state_info.st_dev, state_info.st_ino)
                or state_identity != (pointer["state_device"], pointer["state_inode"])
                or entries
            ):
                raise SandboxSessionError("sandbox_state_invalid")
            workspace_descriptor = -1
            try:
                workspace_descriptor = os.open(
                    expected_workspace,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                current = os.stat(
                    expected_state_root.name,
                    dir_fd=workspace_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != state_identity:
                    raise SandboxSessionError("sandbox_state_invalid")
                os.rmdir(expected_state_root.name, dir_fd=workspace_descriptor)
                os.fsync(workspace_descriptor)
            except SandboxSessionError:
                raise
            except OSError as exc:
                raise SandboxSessionError("sandbox_state_invalid") from exc
            finally:
                if workspace_descriptor >= 0:
                    os.close(workspace_descriptor)
        parent_descriptor = -1
        try:
            parent_descriptor = os.open(
                sidecar_parent,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            current = os.stat(
                sidecar_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _identity(current) != _identity(sidecar_info)
                or _read_strict_file(sidecar_path, max_bytes=MAX_MANIFEST_BYTES) != raw
            ):
                raise SandboxSessionError("sandbox_state_invalid")
            os.unlink(sidecar_path.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except SandboxSessionError:
            raise
        except OSError as exc:
            raise SandboxSessionError("sandbox_state_invalid") from exc
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
    try:
        current_parent = sidecar_parent.lstat()
        if (
            current_parent.st_dev,
            current_parent.st_ino,
        ) != parent_identity or private_directory_identity(
            sidecar_parent
        ) != parent_identity:
            raise SandboxSessionError("sandbox_state_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc


def _process_start(pid):
    executable = shutil.which("ps", path="/usr/bin:/bin")
    if not executable:
        raise SandboxSessionError("sandbox_lease_identity_unavailable")
    try:
        result = subprocess.run(
            [executable, "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SandboxSessionError("sandbox_lease_identity_unavailable") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise SandboxSessionError("sandbox_lease_identity_unavailable")
    return value


def _lease_is_live(lease):
    try:
        pid = int(lease["owner_pid"])
    except (ValueError, KeyError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise SandboxSessionError("sandbox_lease_identity_unavailable") from exc
    return _process_start(pid) == lease["owner_start"]


def _validated_relative(raw_path):
    if not isinstance(raw_path, str) or _CONTROL_RE.search(raw_path):
        raise SandboxSessionError("workspace_path_invalid")
    path = PurePosixPath(raw_path)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SandboxSessionError("workspace_path_invalid")
    if len(path.parts) > MAX_DEPTH:
        raise SandboxSessionError("workspace_capacity_exceeded")
    try:
        raw_path.encode("utf-8").decode("utf-8")
    except UnicodeError as exc:
        raise SandboxSessionError("workspace_path_invalid") from exc
    return path


def _filter_reason(raw_path):
    path = _validated_relative(raw_path)
    folded = tuple(part.casefold() for part in path.parts)
    if ".git" in folded:
        return "excluded_git"
    if ".pony" in folded:
        return "excluded_pony_state"
    if any(part in _AGENT_DIRS for part in folded):
        return "excluded_agent_control"
    if any(part in _GENERATED_DIRS for part in folded):
        return "excluded_generated"
    return sensitive_path_reason(path.as_posix())


def _open_source_root(source):
    descriptor = -1
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        current = source.lstat()
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise SandboxSessionError("workspace_invalid")
        return descriptor, opened
    except SandboxSessionError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise SandboxSessionError("workspace_invalid") from exc


def _open_child_directory(parent_descriptor, name, *, root_device):
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if before.st_dev != root_device:
        raise SandboxSessionError("workspace_mount_boundary")
    if not stat.S_ISDIR(before.st_mode):
        raise SandboxSessionError("unsupported_workspace_entry")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    opened = os.fstat(descriptor)
    if (
        opened.st_dev != root_device
        or not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        os.close(descriptor)
        raise SandboxSessionError("workspace_changed_during_stage")
    return descriptor


def _walk_inventory(source, *, root_descriptor=None, root_device=None):
    result = set()
    excluded = {}
    owned_descriptor = root_descriptor is None
    if owned_descriptor:
        root_descriptor, root_info = _open_source_root(source)
        root_device = root_info.st_dev

    def visit(directory_descriptor, prefix=()):
        for name in sorted(os.listdir(directory_descriptor)):
            relative = PurePosixPath(*prefix, name).as_posix()
            _validated_relative(relative)
            info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if info.st_dev != root_device or os.path.ismount(source / relative):
                raise SandboxSessionError("workspace_mount_boundary")
            reason = _filter_reason(relative)
            if stat.S_ISDIR(info.st_mode):
                if reason:
                    excluded[reason] = excluded.get(reason, 0) + 1
                else:
                    child_descriptor = _open_child_directory(
                        directory_descriptor,
                        name,
                        root_device=root_device,
                    )
                    try:
                        visit(child_descriptor, (*prefix, name))
                    finally:
                        os.close(child_descriptor)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SandboxSessionError("unsupported_workspace_entry")
            result.add(relative)

    try:
        visit(root_descriptor)
        return result, set(), excluded
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("workspace_changed_during_stage") from exc
    finally:
        if owned_descriptor:
            os.close(root_descriptor)


def _git_inventory(source, git_executable):
    try:
        candidates = run_hardened_git(
            git_executable,
            ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=source,
            timeout=30,
        )
        tracked = run_hardened_git(
            git_executable,
            ["ls-files", "--cached", "-z"],
            cwd=source,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SandboxSessionError("git_inventory_failed") from exc
    if candidates.returncode != 0 or tracked.returncode != 0:
        raise SandboxSessionError("git_inventory_failed")

    def decode(raw):
        try:
            return {
                item for item in bytes(raw or b"").decode("utf-8").split("\0") if item
            }
        except UnicodeDecodeError as exc:
            raise SandboxSessionError("workspace_path_invalid") from exc

    candidate_paths = {
        path
        for path in decode(candidates.stdout)
        if (source / path).exists() or (source / path).is_symlink()
    }
    return candidate_paths, decode(tracked.stdout), {}


def _git_audit(source, git_executable):
    try:
        branch = run_hardened_git(
            git_executable,
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=source,
            timeout=30,
        )
        head = run_hardened_git(
            git_executable,
            ["rev-parse", "--verify", "HEAD"],
            cwd=source,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SandboxSessionError("git_inventory_failed") from exc

    def decode(result, pattern):
        if result.returncode != 0:
            return ""
        try:
            value = bytes(result.stdout or b"").decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise SandboxSessionError("git_inventory_failed") from exc
        if not value or pattern.fullmatch(value) is None:
            raise SandboxSessionError("git_inventory_failed")
        return value

    return (
        decode(branch, re.compile(r"[^\x00-\x1f\x7f]{1,255}")),
        decode(head, re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")),
    )


def _read_source_file(
    source,
    relative,
    root_device,
    *,
    root_descriptor=None,
    chunk_consumer=None,
):
    path = source.joinpath(*relative.parts)
    descriptor = -1
    parent_descriptor = -1
    owned_root = root_descriptor is None
    try:
        if owned_root:
            root_descriptor, root_info = _open_source_root(source)
            if root_info.st_dev != root_device:
                raise SandboxSessionError("workspace_mount_boundary")
        root_before = os.fstat(root_descriptor)
        parent_descriptor = os.dup(root_descriptor)
        for index, part in enumerate(relative.parts[:-1], start=1):
            parent_path = source.joinpath(*relative.parts[:index])
            if os.path.ismount(parent_path):
                raise SandboxSessionError("workspace_mount_boundary")
            child_descriptor = _open_child_directory(
                parent_descriptor,
                part,
                root_device=root_device,
            )
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
        parent_before = os.fstat(parent_descriptor)
        info = os.stat(
            relative.parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if info.st_dev != root_device or os.path.ismount(path):
            raise SandboxSessionError("workspace_mount_boundary")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxSessionError("unsupported_workspace_entry")
        if info.st_size > MAX_FILE_BYTES:
            raise SandboxSessionError("workspace_capacity_exceeded")
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(info)
        ):
            raise SandboxSessionError("workspace_changed_during_stage")
        digest = hashlib.sha256()
        size = 0
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(STAGING_CHUNK_BYTES, remaining))
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
            if chunk_consumer is not None:
                chunk_consumer(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        current = os.stat(
            relative.parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        resolved_parent = os.dup(root_descriptor)
        try:
            for part in relative.parts[:-1]:
                child_descriptor = _open_child_directory(
                    resolved_parent,
                    part,
                    root_device=root_device,
                )
                os.close(resolved_parent)
                resolved_parent = child_descriptor
            resolved_parent_info = os.fstat(resolved_parent)
            resolved_current = os.stat(
                relative.parts[-1],
                dir_fd=resolved_parent,
                follow_symlinks=False,
            )
        finally:
            os.close(resolved_parent)
        root_after = os.fstat(root_descriptor)
        source_after = source.lstat()
        if (
            _identity(opened) != _identity(after)
            or _identity(after) != _identity(current)
            or _identity(parent_before) != _identity(resolved_parent_info)
            or _identity(after) != _identity(resolved_current)
            or _identity(root_before) != _identity(root_after)
            or (root_after.st_dev, root_after.st_ino)
            != (source_after.st_dev, source_after.st_ino)
        ):
            raise SandboxSessionError("workspace_changed_during_stage")
        if size > MAX_FILE_BYTES:
            raise SandboxSessionError("workspace_capacity_exceeded")
        return {
            "identity": _identity(after),
            "sha256": "sha256:" + digest.hexdigest(),
            "size": size,
            "allocated": int(getattr(after, "st_blocks", 0)) * 512,
            "mode": _staged_mode(after.st_mode),
            "source_mode": stat.S_IMODE(after.st_mode),
            "uid": after.st_uid,
            "gid": after.st_gid,
        }
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("workspace_changed_during_stage") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if owned_root and root_descriptor is not None:
            os.close(root_descriptor)


def snapshot_source_tree(root):
    """Return a stable digest of an unfiltered ordinary source tree."""
    root = Path(os.path.abspath(os.fspath(root)))
    root_descriptor, root_info = _open_source_root(root)

    def capture():
        entries = []
        logical = 0
        allocated = 0

        def visit(directory_descriptor, directory_info, prefix=()):
            nonlocal logical, allocated
            names = sorted(os.listdir(directory_descriptor))
            for name in names:
                relative = PurePosixPath(*prefix, name)
                _validated_relative(relative.as_posix())
                info = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if info.st_dev != root_info.st_dev or os.path.ismount(root / relative):
                    raise SandboxSessionError("workspace_mount_boundary")
                if len(entries) >= MAX_ENTRIES:
                    raise SandboxSessionError("workspace_capacity_exceeded")
                if stat.S_ISDIR(info.st_mode):
                    child = _open_child_directory(
                        directory_descriptor,
                        name,
                        root_device=root_info.st_dev,
                    )
                    try:
                        opened = os.fstat(child)
                        if _identity(opened) != _identity(info):
                            raise SandboxSessionError("workspace_changed_during_stage")
                        entries.append(
                            {
                                "path": relative.as_posix(),
                                "kind": "directory",
                                "identity": list(_identity(opened)),
                                "sha256": "",
                            }
                        )
                        visit(child, opened, (*prefix, name))
                        if os.path.ismount(root / relative):
                            raise SandboxSessionError("workspace_mount_boundary")
                        current = os.stat(
                            name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                        if _identity(current) != _identity(opened):
                            raise SandboxSessionError("workspace_changed_during_stage")
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SandboxSessionError("unsupported_workspace_entry")
                entry = _read_source_file(
                    root,
                    relative,
                    root_info.st_dev,
                    root_descriptor=root_descriptor,
                )
                if entry["identity"] != _identity(info):
                    raise SandboxSessionError("workspace_changed_during_stage")
                logical += entry["size"]
                allocated += entry["allocated"]
                if logical > MAX_LOGICAL_BYTES or allocated > MAX_ALLOCATED_BYTES:
                    raise SandboxSessionError("workspace_capacity_exceeded")
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "kind": "file",
                        "identity": list(entry["identity"]),
                        "sha256": entry["sha256"],
                    }
                )
            if names != sorted(os.listdir(directory_descriptor)) or _identity(
                os.fstat(directory_descriptor)
            ) != _identity(directory_info):
                raise SandboxSessionError("workspace_changed_during_stage")

        visit(root_descriptor, root_info)
        current = root.lstat()
        opened = os.fstat(root_descriptor)
        if _identity(opened) != _identity(root_info) or _identity(current) != _identity(
            root_info
        ):
            raise SandboxSessionError("workspace_changed_during_stage")
        return {
            "record_type": "docker_sandbox_source_snapshot",
            "format_version": FORMAT_VERSION,
            "root_identity": list(_identity(root_info)),
            "entries": entries,
        }

    try:
        first = capture()
        second = capture()
        if first != second:
            raise SandboxSessionError("workspace_changed_during_stage")
        return _sha256(_canonical_json(first))
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("workspace_changed_during_stage") from exc
    finally:
        os.close(root_descriptor)


def _prepare_staging_parent(destination, relative):
    target = destination.joinpath(*relative.parts)
    current = destination
    created = []
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current.mkdir(mode=0o755)
            created.append(current)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or current.is_symlink():
            raise SandboxSessionError("staging_write_failed")
        current.chmod(0o755)
    return target, created


def _remove_empty_staging_directories(created):
    for path in reversed(created):
        try:
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SandboxSessionError("staging_write_failed") from exc


def _write_staging_chunk(descriptor, chunk):
    view = memoryview(chunk)
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError as exc:
            raise SandboxSessionError("staging_write_failed") from exc
        if written <= 0:
            raise SandboxSessionError("staging_write_failed")
        view = view[written:]


def _publish_file(
    destination,
    relative,
    source,
    root_device,
    *,
    root_descriptor,
    known_secrets,
):
    target, created = _prepare_staging_parent(destination, relative)
    parent_descriptor = -1
    descriptor = -1
    temp_name = ".pony-stage-" + secrets.token_hex(12)
    published = False
    exclusion_reason = ""
    secrets_to_scan = tuple(
        bytes(secret)
        for secret in known_secrets
        if isinstance(secret, (bytes, bytearray)) and 4 <= len(secret) <= MAX_FILE_BYTES
    )
    carry_size = max((len(secret) for secret in secrets_to_scan), default=1) - 1
    carry = b""
    found_known_secret = False
    is_env_template = is_allowed_env_template_leaf(relative.as_posix())
    env_buffer = bytearray() if is_env_template else None

    def consume(chunk):
        nonlocal carry, env_buffer, found_known_secret
        _write_staging_chunk(descriptor, chunk)
        if secrets_to_scan and not found_known_secret:
            window = carry + chunk
            found_known_secret = any(secret in window for secret in secrets_to_scan)
            carry = window[-carry_size:] if carry_size else b""
        if env_buffer is not None:
            if len(env_buffer) + len(chunk) <= MAX_ENV_TEMPLATE_BYTES:
                env_buffer.extend(chunk)
            else:
                env_buffer = None

    try:
        parent_descriptor = os.open(
            target.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_info = os.fstat(parent_descriptor)
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        entry = _read_source_file(
            source,
            relative,
            root_device,
            root_descriptor=root_descriptor,
            chunk_consumer=consume,
        )
        if is_env_template and entry["size"] > MAX_ENV_TEMPLATE_BYTES:
            exclusion_reason = "env_template_too_large"
        elif found_known_secret:
            exclusion_reason = "known_secret_content"
        elif env_buffer is not None and contains_secret_material(
            env_buffer.decode("utf-8", errors="replace"),
            env={},
        ):
            exclusion_reason = "high_confidence_secret"
        if exclusion_reason:
            os.close(descriptor)
            descriptor = -1
            os.unlink(temp_name, dir_fd=parent_descriptor)
            _remove_empty_staging_directories(created)
            return entry, exclusion_reason
        os.fchmod(descriptor, entry["mode"])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        current_parent = target.parent.lstat()
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or target.parent.is_symlink()
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_info.st_dev, parent_info.st_ino)
        ):
            raise SandboxSessionError("staging_write_failed")
        os.replace(
            temp_name,
            target.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        published = True
        os.fsync(parent_descriptor)
        return entry, ""
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("staging_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            if not published:
                try:
                    os.unlink(temp_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            os.close(parent_descriptor)


def stage_source(
    source,
    destination,
    *,
    git_executable=None,
    known_secrets=(),
):
    source = Path(os.path.abspath(os.fspath(source)))
    root_descriptor, source_info = _open_source_root(source)
    try:
        audit_before = (
            _git_audit(source, git_executable) if git_executable else ("", "")
        )

        def inventory():
            return (
                _git_inventory(source, git_executable)
                if git_executable
                else _walk_inventory(
                    source,
                    root_descriptor=root_descriptor,
                    root_device=source_info.st_dev,
                )
            )

        candidate_paths, tracked_paths, excluded = inventory()
        inventory_excluded = dict(excluded)
        if len(candidate_paths) > MAX_ENTRIES:
            raise SandboxSessionError("workspace_capacity_exceeded")
        collisions = {}
        accepted_paths = []
        for raw_path in sorted(candidate_paths):
            relative = _validated_relative(raw_path)
            collision = unicodedata.normalize("NFC", relative.as_posix()).casefold()
            if collision in collisions and collisions[collision] != relative.as_posix():
                raise SandboxSessionError("workspace_path_collision")
            collisions[collision] = relative.as_posix()
            reason = _filter_reason(relative.as_posix())
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            accepted_paths.append(relative)
        destination = Path(destination)
        destination_created = False
        try:
            destination.mkdir(mode=0o700, parents=False)
            destination_created = True
            destination.chmod(0o700)
        except Exception:
            if destination_created:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        accepted = []
        logical = 0
        allocated = 0
        entries = []
        try:
            for relative in accepted_paths:
                entry, reason = _publish_file(
                    destination,
                    relative,
                    source,
                    source_info.st_dev,
                    root_descriptor=root_descriptor,
                    known_secrets=known_secrets,
                )
                if reason:
                    excluded[reason] = excluded.get(reason, 0) + 1
                    continue
                logical += entry["size"]
                allocated += entry["allocated"]
                if logical > MAX_LOGICAL_BYTES or allocated > MAX_ALLOCATED_BYTES:
                    raise SandboxSessionError("workspace_capacity_exceeded")
                accepted.append((relative, entry))
            after_paths, after_tracked, after_excluded = inventory()
            audit_after = (
                _git_audit(source, git_executable) if git_executable else ("", "")
            )
            if (
                after_paths != candidate_paths
                or after_tracked != tracked_paths
                or after_excluded != inventory_excluded
                or audit_after != audit_before
            ):
                raise SandboxSessionError("workspace_changed_during_stage")
            for relative, entry in accepted:
                current = _read_source_file(
                    source,
                    relative,
                    source_info.st_dev,
                    root_descriptor=root_descriptor,
                )
                if (
                    current["identity"] != entry["identity"]
                    or current["sha256"] != entry["sha256"]
                    or current["source_mode"] != entry["source_mode"]
                ):
                    raise SandboxSessionError("workspace_changed_during_stage")
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": entry["sha256"],
                        "size": entry["size"],
                        "mode": entry["source_mode"],
                        "uid": entry["uid"],
                        "gid": entry["gid"],
                    }
                )
            source_after = source.lstat()
            opened_after = os.fstat(root_descriptor)
            if (source_after.st_dev, source_after.st_ino) != (
                source_info.st_dev,
                source_info.st_ino,
            ) or (opened_after.st_dev, opened_after.st_ino) != (
                source_info.st_dev,
                source_info.st_ino,
            ):
                raise SandboxSessionError("workspace_changed_during_stage")
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        raw_entries = json.dumps(
            entries,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        accepted_paths = {entry["path"] for entry in entries}
        return {
            "tree_digest": _sha256(raw_entries),
            "file_count": len(entries),
            "logical_bytes": logical,
            "allocated_bytes": allocated,
            "entries": entries,
            "tracked_paths": sorted(accepted_paths & tracked_paths),
            "untracked_paths": sorted(accepted_paths - tracked_paths),
            "excluded_counts": dict(sorted(excluded.items())),
            "source_device": source_info.st_dev,
            "source_inode": source_info.st_ino,
            "source_branch": audit_before[0],
            "source_head": audit_before[1],
        }
    finally:
        os.close(root_descriptor)


def _capture_execution_entries(root):
    root = Path(root)
    root_descriptor, root_info = _open_source_root(root)
    paths = []
    collisions = set()

    def visit(directory_descriptor, prefix=()):
        for name in sorted(os.listdir(directory_descriptor)):
            relative = PurePosixPath(*prefix, name)
            if relative.parts[0] == ".git":
                continue
            _validated_relative(relative.as_posix())
            collision = unicodedata.normalize("NFC", relative.as_posix()).casefold()
            if collision in collisions:
                raise SandboxSessionError("workspace_path_collision")
            collisions.add(collision)
            info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if info.st_dev != root_info.st_dev or os.path.ismount(root / relative):
                raise SandboxSessionError("workspace_mount_boundary")
            if stat.S_ISDIR(info.st_mode):
                child_descriptor = _open_child_directory(
                    directory_descriptor,
                    name,
                    root_device=root_info.st_dev,
                )
                try:
                    visit(child_descriptor, (*prefix, name))
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SandboxSessionError("unsupported_workspace_entry")
            if len(paths) >= MAX_ENTRIES:
                raise SandboxSessionError("workspace_capacity_exceeded")
            entry = _read_source_file(
                root,
                relative,
                root_info.st_dev,
                root_descriptor=root_descriptor,
            )
            paths.append(
                {
                    "path": relative.as_posix(),
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                    "mode": entry["source_mode"],
                }
            )

    try:
        visit(root_descriptor)
        return paths
    finally:
        os.close(root_descriptor)


def _remove_generated_directory(path, root, expected_identity):
    path = Path(path)
    root = Path(root)
    if path.parent != root:
        return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if (
        expected_identity is None
        or not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or (info.st_dev, info.st_ino) != expected_identity
    ):
        return False
    trash = root / ("trash-" + secrets.token_hex(12))
    try:
        os.replace(path, trash)
        moved = trash.lstat()
        if (moved.st_dev, moved.st_ino) != expected_identity:
            return False
        shutil.rmtree(trash)
    except OSError:
        return False
    return True


def _fsync_directory(path):
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_delete_directory(path, *, expected_identity, max_entries):
    if type(max_entries) is not int or max_entries < 0:
        raise SandboxSessionError("sandbox_cleanup_failed")
    path = Path(path)
    parent_descriptor = -1
    removed = 0
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            root_info = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return 0, True
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or (root_info.st_dev, root_info.st_ino) != expected_identity
        ):
            raise SandboxSessionError("sandbox_cleanup_failed")

        def remove(parent, name, depth):
            nonlocal removed
            if removed >= max_entries or depth > MAX_DEPTH:
                return False
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(name, dir_fd=parent)
                removed += 1
                return True
            if info.st_dev != root_info.st_dev:
                raise SandboxSessionError("sandbox_cleanup_failed")
            child = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise SandboxSessionError("sandbox_cleanup_failed")
                with os.scandir(child) as entries:
                    for entry in entries:
                        if not remove(child, entry.name, depth + 1):
                            return False
            finally:
                os.close(child)
            if removed >= max_entries:
                return False
            os.rmdir(name, dir_fd=parent)
            removed += 1
            return True

        complete = remove(parent_descriptor, path.name, 0)
        return removed, complete
    except SandboxSessionError:
        raise
    except OSError as exc:
        raise SandboxSessionError("sandbox_cleanup_failed") from exc
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _private_cleanup_directory(path):
    path = Path(path)
    try:
        identity = private_directory_identity(path)
        info = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SandboxSessionError("sandbox_cleanup_failed") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o700
        or (info.st_dev, info.st_ino) != identity
    ):
        raise SandboxSessionError("sandbox_cleanup_failed")
    return identity


def _cleanup_artifacts_record(state_root, manifest):
    state_root = Path(state_root)
    path = state_root / _CLEANUP_ARTIFACTS_NAME
    try:
        path.lstat()
    except FileNotFoundError:
        blobs = state_root / _STAGING_BLOBS_RELATIVE
        trash = state_root / _CONTENT_BLOBS_TRASH_NAME
        blob_identity = _private_cleanup_directory(blobs)
        if _private_cleanup_directory(trash) is not None:
            raise SandboxSessionError("sandbox_cleanup_failed")
        value = {
            "record_type": "docker_sandbox_cleanup_artifacts",
            "format_version": FORMAT_VERSION,
            "sandbox_id": manifest["sandbox_id"],
            "staging_blobs": {
                "status": "planned" if blob_identity is not None else "absent",
                "device": blob_identity[0] if blob_identity is not None else 0,
                "inode": blob_identity[1] if blob_identity is not None else 0,
            },
        }
        _atomic_json(
            path,
            state_root,
            value,
            max_bytes=MAX_CLEANUP_ARTIFACTS_BYTES,
        )
        return _validate_cleanup_artifacts(value, manifest["sandbox_id"])
    raw = _read_strict_file(path, max_bytes=MAX_CLEANUP_ARTIFACTS_BYTES)
    return _validate_cleanup_artifacts(
        _decode_json(raw),
        manifest["sandbox_id"],
    )


def _cleanup_workspace(state_root, manifest, *, max_delete_entries):
    state_root = Path(state_root)
    workspace = Path(manifest["execution"]["root"])
    trash = state_root / _WORKSPACE_TRASH_NAME
    expected_identity = (
        manifest["execution"]["device"],
        manifest["execution"]["inode"],
    )
    try:
        workspace_info = workspace.lstat()
    except FileNotFoundError:
        workspace_info = None
    try:
        trash_info = trash.lstat()
    except FileNotFoundError:
        trash_info = None
    if workspace_info is not None:
        if (
            trash_info is not None
            or workspace.parent != state_root
            or not stat.S_ISDIR(workspace_info.st_mode)
            or workspace.is_symlink()
            or (workspace_info.st_dev, workspace_info.st_ino) != expected_identity
        ):
            raise SandboxSessionError("sandbox_cleanup_failed")
        os.replace(workspace, trash)
        _fsync_directory(state_root)
    elif trash_info is not None and (
        not stat.S_ISDIR(trash_info.st_mode)
        or trash.is_symlink()
        or (trash_info.st_dev, trash_info.st_ino) != expected_identity
    ):
        raise SandboxSessionError("sandbox_cleanup_failed")
    removed, complete = _bounded_delete_directory(
        trash,
        expected_identity=expected_identity,
        max_entries=max_delete_entries,
    )
    _fsync_directory(state_root)
    return removed, complete


def _cleanup_staging_blobs(
    state_root,
    manifest,
    record,
    *,
    max_delete_entries,
):
    state_root = Path(state_root)
    blobs = state_root / _STAGING_BLOBS_RELATIVE
    trash = state_root / _CONTENT_BLOBS_TRASH_NAME
    expected = (
        record["staging_blobs"]["device"],
        record["staging_blobs"]["inode"],
    )
    blob_identity = _private_cleanup_directory(blobs)
    trash_identity = _private_cleanup_directory(trash)
    if record["staging_blobs"]["status"] == "absent":
        if blob_identity is not None or trash_identity is not None:
            raise SandboxSessionError("sandbox_cleanup_failed")
        return 0, True
    if (
        blob_identity is not None
        and trash_identity is not None
        or blob_identity is not None
        and blob_identity != expected
        or trash_identity is not None
        and trash_identity != expected
    ):
        raise SandboxSessionError("sandbox_cleanup_failed")
    if blob_identity is not None:
        os.replace(blobs, trash)
        _fsync_directory(blobs.parent)
        _fsync_directory(state_root)
        if _private_cleanup_directory(trash) != expected:
            raise SandboxSessionError("sandbox_cleanup_failed")
        trash_identity = expected
    if trash_identity is None:
        return 0, True
    removed, complete = _bounded_delete_directory(
        trash,
        expected_identity=expected,
        max_entries=max_delete_entries,
    )
    _fsync_directory(state_root)
    return removed, complete


def _cleanup_terminal_artifacts(state_root, manifest, *, max_delete_entries):
    if type(max_delete_entries) is not int or max_delete_entries < 0:
        raise SandboxSessionError("sandbox_cleanup_failed")
    record = _cleanup_artifacts_record(state_root, manifest)
    removed, workspace_complete = _cleanup_workspace(
        state_root,
        manifest,
        max_delete_entries=max_delete_entries,
    )
    if not workspace_complete:
        return False
    _removed, blobs_complete = _cleanup_staging_blobs(
        state_root,
        manifest,
        record,
        max_delete_entries=max_delete_entries - removed,
    )
    return blobs_complete


def _require_owned_lease(manifest):
    lease = manifest.get("lease")
    if (
        lease is None
        or lease["owner_pid"] != os.getpid()
        or lease["owner_start"] != _process_start(os.getpid())
        or not _lease_is_live(lease)
    ):
        raise SandboxSessionError("sandbox_lease_mismatch")


@dataclass(frozen=True)
class WorkspaceView:
    physical_root: Path
    logical_root: PurePosixPath = LOGICAL_ROOT

    def __post_init__(self):
        root = Path(self.physical_root)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or root.is_symlink():
            raise SandboxSessionError("execution_root_invalid")
        object.__setattr__(self, "physical_root", root)
        object.__setattr__(self, "_identity", (info.st_dev, info.st_ino))

    def verify(self):
        info = self.physical_root.lstat()
        if (info.st_dev, info.st_ino) != self._identity or not stat.S_ISDIR(
            info.st_mode
        ):
            raise SandboxSessionError("execution_root_changed")

    def physical_path(self, raw_path, *, allow_missing=True):
        self.verify()
        raw = os.fspath(raw_path).replace("\\", "/")
        if _CONTROL_RE.search(raw):
            raise SandboxSessionError("workspace_path_invalid")
        logical = PurePosixPath(raw)
        if logical.is_absolute():
            try:
                relative = logical.relative_to(self.logical_root)
            except ValueError as exc:
                raise SandboxSessionError("workspace_path_invalid") from exc
        else:
            relative = logical
        if not relative.parts:
            return self.physical_root
        normalized = _validated_relative(relative.as_posix())
        target = self.physical_root.joinpath(*normalized.parts)
        current = self.physical_root
        for index, part in enumerate(normalized.parts):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if allow_missing and index == len(normalized.parts) - 1:
                    return target
                raise SandboxSessionError("workspace_path_invalid") from None
            if stat.S_ISLNK(mode):
                raise SandboxSessionError("workspace_path_invalid")
            if index < len(normalized.parts) - 1 and not stat.S_ISDIR(mode):
                raise SandboxSessionError("workspace_path_invalid")
        return target

    def logical_path(self, physical_path):
        self.verify()
        path = Path(os.path.abspath(os.fspath(physical_path)))
        try:
            relative = path.relative_to(self.physical_root)
        except ValueError as exc:
            raise SandboxSessionError("workspace_path_invalid") from exc
        return (self.logical_root / PurePosixPath(relative.as_posix())).as_posix()


@dataclass
class SandboxSession:
    state_root: Path
    manifest: dict

    @property
    def sandbox_id(self):
        return self.manifest["sandbox_id"]

    @property
    def state(self):
        return self.manifest["state"]

    @property
    def workspace_view(self):
        if self.state not in {"ready", "running", "pending_review"}:
            raise SandboxSessionError("sandbox_session_not_active")
        return WorkspaceView(Path(self.manifest["execution"]["root"]))


@dataclass(frozen=True)
class SyntheticGitBootstrapRequest:
    sandbox_id: str
    state_root: Path
    workspace_view: WorkspaceView
    tracked_paths: tuple[str, ...]


@dataclass
class _SandboxSessionCreation:
    source_root: Path
    sandbox_id: str
    state_root: Path
    candidate: Path
    workspace: Path
    manifest: dict
    candidate_identity: object = None
    workspace_identity: object = None


class SandboxSessionStore:
    def __init__(self, parent):
        self.parent = Path(os.path.abspath(os.fspath(parent)))

    @staticmethod
    def _workspace_id(source_root):
        return hashlib.sha256(str(source_root).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _creation_manifest(
        source_root,
        source_info,
        sandbox_id,
        pony_session_id,
        workspace,
        engine,
        image,
        policy,
        sidecar,
    ):
        timestamp = now()
        return {
            "record_type": "docker_sandbox_session",
            "format_version": FORMAT_VERSION,
            "sandbox_id": sandbox_id,
            "pony_session_id": str(pony_session_id),
            "state": "creating",
            "created_at": timestamp,
            "updated_at": timestamp,
            "source": {
                "root": str(source_root),
                "device": source_info.st_dev,
                "inode": source_info.st_ino,
                "baseline_digest": "",
                "branch": "",
                "head": "",
            },
            "execution": {
                "root": str(workspace),
                "device": 0,
                "inode": 0,
                "tree_digest": "",
                "file_count": 0,
                "logical_bytes": 0,
                "allocated_bytes": 0,
                "synthetic_git_commit": "",
            },
            "engine": engine,
            "image": image,
            "policy": policy,
            "lease": {
                "owner_pid": os.getpid(),
                "owner_start": _process_start(os.getpid()),
                "owner_nonce": secrets.token_hex(32),
                "acquired_at": timestamp,
            },
            "active_call": None,
            "diff": {
                "digest": "",
                "status": "not_generated",
                "candidate_count": 0,
                "blocked_count": 0,
            },
            "apply": {"journal_id": "", "status": "not_started"},
            "cleanup": {"status": "not_started", "last_error_code": ""},
            "sidecar": sidecar,
        }

    def _begin_session_creation(
        self,
        source_root,
        pony_session_id,
        engine,
        image,
        policy,
        project_state_root,
    ):
        source_root = Path(os.path.abspath(os.fspath(source_root)))
        source_info = source_root.lstat()
        if not stat.S_ISDIR(source_info.st_mode) or source_root.is_symlink():
            raise SandboxSessionError("workspace_invalid")
        sandbox_id = "sandbox_" + secrets.token_hex(16)
        workspace_parent = ensure_private_dir(
            self.parent / self._workspace_id(source_root)
        )
        state_root = ensure_private_dir(workspace_parent / sandbox_id)
        sidecar = _sidecar_metadata(project_state_root, sandbox_id)
        candidate = state_root / "workspace.candidate"
        workspace = state_root / "workspace"
        manifest = self._creation_manifest(
            source_root,
            source_info,
            sandbox_id,
            pony_session_id,
            workspace,
            engine,
            image,
            policy,
            sidecar,
        )
        creation = _SandboxSessionCreation(
            source_root=source_root,
            sandbox_id=sandbox_id,
            state_root=state_root,
            candidate=candidate,
            workspace=workspace,
            manifest=manifest,
        )
        self._write_initial_session_manifest(creation)
        return creation

    def _write_initial_session_manifest(self, creation):
        sidecar = creation.manifest["sidecar"]
        if sidecar is None:
            _write_session_manifest(
                creation.state_root,
                creation.manifest,
                create_sidecar=True,
            )
            return
        project_state_root = Path(sidecar["path"]).parent.parent
        with file_lock.locked_file(project_state_root / ".sandbox-session-create.lock"):
            _reconcile_creation_sidecar_orphans(
                self,
                project_state_root,
                creation.source_root,
            )
            _write_session_manifest(
                creation.state_root,
                creation.manifest,
                create_sidecar=True,
            )

    @staticmethod
    def _baseline_from_staging(creation, staged):
        return {
            "record_type": "docker_sandbox_baseline",
            "format_version": FORMAT_VERSION,
            "sandbox_id": creation.sandbox_id,
            "tree_digest": staged["tree_digest"],
            "entries": staged["entries"],
            "tracked_paths": staged["tracked_paths"],
            "untracked_paths": staged["untracked_paths"],
            "excluded_counts": staged["excluded_counts"],
        }

    def _stage_session_workspace(self, creation, git_executable, known_secrets):
        staged = stage_source(
            creation.source_root,
            creation.candidate,
            git_executable=git_executable,
            known_secrets=known_secrets,
        )
        candidate_info = creation.candidate.lstat()
        creation.candidate_identity = (
            candidate_info.st_dev,
            candidate_info.st_ino,
        )
        baseline = self._baseline_from_staging(creation, staged)
        _atomic_json(
            creation.state_root / "baseline.json",
            creation.state_root,
            baseline,
            max_bytes=MAX_BASELINE_BYTES,
        )
        baseline_digest = _sha256(_canonical_json(baseline))
        os.replace(creation.candidate, creation.workspace)
        creation.workspace_identity = creation.candidate_identity
        creation.candidate_identity = None
        creation.workspace.chmod(0o755)
        workspace_info = creation.workspace.lstat()
        creation.manifest["source"].update(
            baseline_digest=baseline_digest,
            branch=staged["source_branch"],
            head=staged["source_head"],
        )
        creation.manifest["execution"].update(
            root=str(creation.workspace),
            device=workspace_info.st_dev,
            inode=workspace_info.st_ino,
            tree_digest=staged["tree_digest"],
            file_count=staged["file_count"],
            logical_bytes=staged["logical_bytes"],
            allocated_bytes=staged["allocated_bytes"],
        )
        creation.manifest["updated_at"] = now()
        _write_session_manifest(creation.state_root, creation.manifest)
        return staged, WorkspaceView(creation.workspace)

    @staticmethod
    def _expected_staged_entries(staged):
        return [
            {
                "path": entry["path"],
                "sha256": entry["sha256"],
                "size": entry["size"],
                "mode": _staged_mode(entry["mode"]),
            }
            for entry in staged["entries"]
        ]

    def _complete_session_creation(self, creation, staged, view, bootstrap_git):
        request = SyntheticGitBootstrapRequest(
            sandbox_id=creation.sandbox_id,
            state_root=creation.state_root,
            workspace_view=view,
            tracked_paths=tuple(staged["tracked_paths"]),
        )
        commit = str(bootstrap_git(request) or "")
        latest = self.inspect(creation.state_root)
        if latest.state != "creating" or latest.manifest["active_call"] is not None:
            raise SandboxSessionError("synthetic_git_bootstrap_incomplete")
        creation.manifest = latest.manifest
        captured = _capture_execution_entries(creation.workspace)
        if captured != self._expected_staged_entries(staged):
            raise SandboxSessionError("synthetic_git_modified_workspace")
        creation.manifest["execution"]["synthetic_git_commit"] = commit
        creation.manifest["state"] = "ready"
        creation.manifest["updated_at"] = now()
        _write_session_manifest(creation.state_root, creation.manifest)
        return SandboxSession(creation.state_root, creation.manifest)

    def _record_session_creation_failure(self, creation):
        try:
            creation.manifest = self.inspect(creation.state_root).manifest
        except SandboxSessionError:
            pass
        manifest = creation.manifest
        if (
            manifest.get("active_call") is not None
            or manifest["state"] == "review_required"
        ):
            manifest["state"] = "review_required"
            manifest["updated_at"] = now()
            manifest["cleanup"] = {
                "status": "pending",
                "last_error_code": "sandbox_create_failed",
            }
            _write_session_manifest(creation.state_root, manifest)
            return
        manifest["state"] = "failed"
        manifest["updated_at"] = now()
        candidate_clean = _remove_generated_directory(
            creation.candidate,
            creation.state_root,
            creation.candidate_identity,
        )
        workspace_clean = _remove_generated_directory(
            creation.workspace,
            creation.state_root,
            creation.workspace_identity,
        )
        cleanup_complete = candidate_clean and workspace_clean
        manifest["cleanup"] = {
            "status": "complete" if cleanup_complete else "pending",
            "last_error_code": "" if cleanup_complete else "sandbox_create_failed",
        }
        _write_session_manifest(creation.state_root, manifest)

    def create(
        self,
        source_root,
        *,
        pony_session_id,
        bootstrap_git,
        git_executable=None,
        known_secrets=(),
        engine=None,
        image=None,
        policy=None,
        project_state_root=None,
    ):
        if not callable(bootstrap_git):
            raise SandboxSessionError("synthetic_git_bootstrap_required")
        engine, image, policy = _validate_identity_metadata(engine, image, policy)
        creation = self._begin_session_creation(
            source_root,
            pony_session_id,
            engine,
            image,
            policy,
            project_state_root,
        )
        try:
            staged, view = self._stage_session_workspace(
                creation,
                git_executable,
                known_secrets,
            )
            return self._complete_session_creation(
                creation,
                staged,
                view,
                bootstrap_git,
            )
        except Exception:
            self._record_session_creation_failure(creation)
            raise

    def inspect(self, state_root):
        return self._inspect(state_root, require_live_source=True)

    def reconcile_source_apply_authority(
        self,
        source_root,
        authority,
        *,
        journal_status,
    ):
        source_root = Path(os.path.abspath(os.fspath(source_root)))
        control_lock = source_apply_control_lock_path(self.parent, source_root)
        if not file_lock.lock_is_active(control_lock):
            raise RuntimeError("source apply reconciliation requires control lock")
        lock_info = control_lock.lstat()
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_nlink != 1
            or stat.S_IMODE(lock_info.st_mode) != 0o600
            or _read_strict_file(control_lock, max_bytes=0) != b""
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        authority = _validate_source_apply_authority(
            authority,
            parent=self.parent,
            source_root=source_root,
        )
        if journal_status not in {"applying", "apply_review_required"}:
            raise SandboxSessionError("sandbox_state_invalid")
        state_root = Path(authority["state_root"])
        with file_lock.locked_file(state_root / ".lease.lock"):
            session = self._inspect(
                state_root,
                require_live_source=False,
                allow_missing_sidecar_after_source_replacement=True,
            )
            manifest = session.manifest
            source = manifest["source"]
            lease = manifest["lease"]
            current_start = _process_start(os.getpid())
            if (
                lease is not None
                and _lease_is_live(lease)
                and (
                    lease["owner_pid"] != os.getpid()
                    or lease["owner_start"] != current_start
                )
            ):
                raise SandboxSessionError("sandbox_session_busy")
            if (
                manifest["sandbox_id"] != authority["sandbox_id"]
                or source["root"] != authority["source_root"]
                or (source["device"], source["inode"])
                != (authority["source_device"], authority["source_inode"])
                or manifest["diff"]["digest"] != authority["diff_digest"]
                or manifest["active_call"] is not None
            ):
                raise SandboxSessionError("sandbox_state_invalid")
            expected = {
                "journal_id": authority["journal_id"],
                "status": "applying",
            }
            if manifest["state"] == "pending_review" and manifest["apply"] == {
                "journal_id": "",
                "status": "not_started",
            }:
                pass
            elif manifest["state"] == "applying" and manifest["apply"] == expected:
                pass
            elif manifest["state"] == "review_required" and manifest["apply"] == {
                "journal_id": authority["journal_id"],
                "status": "apply_review_required",
            }:
                if lease is None:
                    return session
            else:
                raise SandboxSessionError("sandbox_state_invalid")
            if manifest["state"] != "review_required":
                manifest["state"] = "review_required"
                manifest["apply"] = {
                    "journal_id": authority["journal_id"],
                    "status": "apply_review_required",
                }
            manifest["lease"] = None
            manifest["updated_at"] = now()
            _write_session_manifest(
                state_root,
                manifest,
                require_live_source=False,
                allow_missing_sidecar_after_source_replacement=True,
            )
            return SandboxSession(state_root, manifest)

    def _inspect(
        self,
        state_root,
        *,
        require_live_source,
        allow_missing_sidecar_after_source_replacement=False,
    ):
        state_root = Path(os.path.abspath(os.fspath(state_root)))
        try:
            relative = state_root.relative_to(self.parent)
            info = state_root.lstat()
            workspace_info = state_root.parent.lstat()
        except (FileNotFoundError, ValueError) as exc:
            raise SandboxSessionError("sandbox_state_invalid") from exc
        uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
        if (
            len(relative.parts) != 2
            or _WORKSPACE_ID_RE.fullmatch(relative.parts[0]) is None
            or _SANDBOX_ID_RE.fullmatch(relative.parts[1]) is None
            or not stat.S_ISDIR(info.st_mode)
            or state_root.is_symlink()
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or not stat.S_ISDIR(workspace_info.st_mode)
            or state_root.parent.is_symlink()
            or workspace_info.st_uid != uid
            or stat.S_IMODE(workspace_info.st_mode) != 0o700
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        manifest_path = state_root / "manifest.json"
        manifest_raw = _read_strict_file(
            manifest_path,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        manifest = _validate_manifest(_decode_json(manifest_raw))
        if (
            not require_live_source
            and not allow_missing_sidecar_after_source_replacement
        ):
            raise SandboxSessionError("sandbox_manifest_invalid")
        if require_live_source:
            try:
                live_source = Path(manifest["source"]["root"])
                source_info = live_source.lstat()
                source_identity = private_directory_identity(live_source)
            except (OSError, RuntimeError, ValueError) as exc:
                raise SandboxSessionError("sandbox_manifest_invalid") from exc
            if (
                not stat.S_ISDIR(source_info.st_mode)
                or live_source.is_symlink()
                or source_identity != (source_info.st_dev, source_info.st_ino)
                or (source_info.st_dev, source_info.st_ino)
                != (
                    manifest["source"]["device"],
                    manifest["source"]["inode"],
                )
            ):
                raise SandboxSessionError("sandbox_manifest_invalid")
        if (
            state_root.name != manifest["sandbox_id"]
            or state_root.parent.name
            != self._workspace_id(Path(manifest["source"]["root"]))
            or Path(manifest["execution"]["root"]) != state_root / "workspace"
        ):
            raise SandboxSessionError("sandbox_manifest_invalid")
        if manifest["source"]["baseline_digest"]:
            baseline_raw = _read_strict_file(
                state_root / "baseline.json",
                max_bytes=MAX_BASELINE_BYTES,
            )
            baseline = _validate_baseline(
                _decode_json(baseline_raw),
                manifest["sandbox_id"],
            )
            if (
                _sha256(baseline_raw) != manifest["source"]["baseline_digest"]
                or baseline["tree_digest"] != manifest["execution"]["tree_digest"]
                or len(baseline["entries"]) != manifest["execution"]["file_count"]
            ):
                raise SandboxSessionError("sandbox_manifest_invalid")
        _read_bound_sidecar(
            manifest,
            state_root,
            require_live_source=require_live_source,
            allow_missing_after_source_replacement=(
                allow_missing_sidecar_after_source_replacement
            ),
        )
        if (
            manifest["state"]
            in {
                "ready",
                "running",
                "pending_review",
                "applying",
                "discarding",
                "review_required",
            }
            or manifest["state"] == "creating"
            and manifest["execution"]["device"]
        ):
            workspace = Path(manifest["execution"]["root"])
            try:
                workspace_info = workspace.lstat()
            except FileNotFoundError as exc:
                raise SandboxSessionError("execution_root_changed") from exc
            if (
                not stat.S_ISDIR(workspace_info.st_mode)
                or workspace.is_symlink()
                or stat.S_IMODE(workspace_info.st_mode) != 0o755
                or (workspace_info.st_dev, workspace_info.st_ino)
                != (
                    manifest["execution"]["device"],
                    manifest["execution"]["inode"],
                )
            ):
                raise SandboxSessionError("execution_root_changed")
        return SandboxSession(state_root, manifest)

    def list(self):
        inventory = self.inventory()
        if inventory["unknown_count"]:
            raise SandboxSessionError("sandbox_state_invalid")
        return inventory["manifests"]

    def inventory(self):
        try:
            parent_info = self.parent.lstat()
        except FileNotFoundError:
            return {"manifests": [], "unknown_count": 0}
        uid = os.geteuid() if hasattr(os, "geteuid") else parent_info.st_uid
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or self.parent.is_symlink()
            or parent_info.st_uid != uid
            or stat.S_IMODE(parent_info.st_mode) != 0o700
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        sessions = []
        unknown_count = 0
        for workspace_dir in sorted(self.parent.iterdir(), key=lambda item: item.name):
            try:
                workspace_info = workspace_dir.lstat()
                valid_workspace = (
                    _WORKSPACE_ID_RE.fullmatch(workspace_dir.name) is not None
                    and not workspace_dir.is_symlink()
                    and stat.S_ISDIR(workspace_info.st_mode)
                    and workspace_info.st_uid == uid
                    and stat.S_IMODE(workspace_info.st_mode) == 0o700
                )
            except OSError:
                valid_workspace = False
            if not valid_workspace:
                unknown_count += 1
                continue
            state_roots = sorted(workspace_dir.iterdir(), key=lambda item: item.name)
            control = [
                item for item in state_roots if item.name == _SOURCE_APPLY_CONTROL_NAME
            ]
            inactive_control_only = False
            control_unknown = False
            if control:
                try:
                    control_info = control[0].lstat()
                    control_entries = sorted(
                        control[0].iterdir(), key=lambda item: item.name
                    )
                    valid_control_files = True
                    for item in control_entries:
                        item_info = item.lstat()
                        if (
                            item.is_symlink()
                            or not stat.S_ISREG(item_info.st_mode)
                            or item_info.st_uid != uid
                            or item_info.st_nlink != 1
                            or stat.S_IMODE(item_info.st_mode) != 0o600
                        ):
                            valid_control_files = False
                            break
                    control_names = {item.name for item in control_entries}
                    valid_control = (
                        not control[0].is_symlink()
                        and stat.S_ISDIR(control_info.st_mode)
                        and control_info.st_uid == uid
                        and stat.S_IMODE(control_info.st_mode) == 0o700
                        and ".lock" in control_names
                        and control_names <= {".lock", _SOURCE_APPLY_AUTHORITY_NAME}
                        and valid_control_files
                        and _read_strict_file(
                            control[0] / ".lock",
                            max_bytes=0,
                        )
                        == b""
                    )
                    if not valid_control:
                        raise SandboxSessionError("sandbox_state_invalid")
                    inactive_control_only = control_names == {".lock"}
                    if not inactive_control_only:
                        active = control[0] / _SOURCE_APPLY_AUTHORITY_NAME
                        authority = read_source_apply_authority(
                            self.parent,
                            Path(
                                _decode_json(
                                    _read_strict_file(
                                        active,
                                        max_bytes=MAX_MANIFEST_BYTES,
                                    )
                                )["source_root"]
                            ),
                        )
                        if Path(authority["state_root"]).parent != workspace_dir:
                            raise SandboxSessionError("sandbox_state_invalid")
                except (KeyError, OSError, SandboxSessionError):
                    control_unknown = True
                    unknown_count += 1
            state_roots = [
                item for item in state_roots if item.name != _SOURCE_APPLY_CONTROL_NAME
            ]
            if not state_roots:
                if not inactive_control_only and not control_unknown:
                    unknown_count += 1
                continue
            for state_root in state_roots:
                if _SANDBOX_ID_RE.fullmatch(state_root.name) is None:
                    unknown_count += 1
                    continue
                try:
                    sessions.append(self.inspect(state_root).manifest)
                except SandboxSessionError:
                    unknown_count += 1
        return {"manifests": sessions, "unknown_count": unknown_count}

    def find(self, sandbox_id):
        if _SANDBOX_ID_RE.fullmatch(str(sandbox_id)) is None:
            raise SandboxSessionError("sandbox_session_not_found")
        matches = [
            manifest for manifest in self.list() if manifest["sandbox_id"] == sandbox_id
        ]
        if len(matches) != 1:
            raise SandboxSessionError(
                "sandbox_session_not_found" if not matches else "sandbox_state_invalid"
            )
        return self.inspect(Path(matches[0]["execution"]["root"]).parent)

    def discard(
        self,
        state_root,
        *,
        max_delete_entries=MAX_CLEANUP_DELETE_ENTRIES,
    ):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if manifest["active_call"] is not None or manifest["state"] not in {
            "ready",
            "pending_review",
            "failed",
            "review_required",
        }:
            raise SandboxSessionError("sandbox_discard_not_allowed")
        root = session.state_root
        manifest["state"] = "discarding"
        manifest["updated_at"] = now()
        _write_session_manifest(root, manifest)
        try:
            complete = _cleanup_terminal_artifacts(
                root,
                manifest,
                max_delete_entries=max_delete_entries,
            )
        except SandboxSessionError:
            complete = False
        manifest["state"] = "discarded" if complete else "cleanup_pending"
        manifest["lease"] = None
        manifest["updated_at"] = now()
        manifest["cleanup"] = {
            "status": "complete" if complete else "pending",
            "last_error_code": "" if complete else "sandbox_cleanup_pending",
        }
        _write_session_manifest(root, manifest)
        return SandboxSession(root, manifest)

    def cleanup_applied(
        self,
        state_root,
        *,
        max_delete_entries=MAX_CLEANUP_DELETE_ENTRIES,
    ):
        state_root = Path(state_root)
        with file_lock.locked_file(state_root / ".lease.lock"):
            session = self.inspect(state_root)
            manifest = session.manifest
            if (
                manifest["state"] != "applied"
                or manifest["lease"] is not None
                or manifest["active_call"] is not None
            ):
                raise SandboxSessionError("sandbox_cleanup_not_allowed")
            try:
                cleaned = _cleanup_terminal_artifacts(
                    state_root,
                    manifest,
                    max_delete_entries=max_delete_entries,
                )
            except SandboxSessionError:
                cleaned = False
            manifest["state"] = "applied" if cleaned else "cleanup_pending"
            manifest["cleanup"] = {
                "status": "complete" if cleaned else "pending",
                "last_error_code": "" if cleaned else "sandbox_cleanup_pending",
            }
            manifest["updated_at"] = now()
            _write_session_manifest(state_root, manifest)
            if not cleaned:
                raise SandboxSessionError("sandbox_cleanup_failed")
            return SandboxSession(state_root, manifest)

    def resume_cleanup(
        self,
        state_root,
        *,
        max_delete_entries=MAX_CLEANUP_DELETE_ENTRIES,
    ):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if (
            manifest["state"] != "cleanup_pending"
            or manifest["active_call"] is not None
        ):
            raise SandboxSessionError("sandbox_cleanup_not_allowed")
        try:
            cleaned = _cleanup_terminal_artifacts(
                session.state_root,
                manifest,
                max_delete_entries=max_delete_entries,
            )
        except SandboxSessionError:
            cleaned = False
        if cleaned:
            manifest["state"] = (
                "applied"
                if manifest["apply"]["status"] == "apply_applied"
                else "discarded"
            )
            manifest["lease"] = None
        manifest["cleanup"] = {
            "status": "complete" if cleaned else "pending",
            "last_error_code": "" if cleaned else "sandbox_cleanup_pending",
        }
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        if not cleaned:
            raise SandboxSessionError("sandbox_cleanup_pending")
        return SandboxSession(session.state_root, manifest)

    def acquire(self, state_root):
        state_root = Path(state_root)
        with file_lock.locked_file(state_root / ".lease.lock"):
            session = self.inspect(state_root)
            manifest = session.manifest
            lease = manifest["lease"]
            current_start = _process_start(os.getpid())
            if lease is not None and _lease_is_live(lease):
                if (
                    lease["owner_pid"] != os.getpid()
                    or lease["owner_start"] != current_start
                ):
                    raise SandboxSessionError("sandbox_session_busy")
                return session
            manifest["lease"] = {
                "owner_pid": os.getpid(),
                "owner_start": current_start,
                "owner_nonce": secrets.token_hex(32),
                "acquired_at": now(),
            }
            manifest["updated_at"] = now()
            _write_session_manifest(state_root, manifest)
            return SandboxSession(state_root, manifest)

    def release(self, state_root, owner_nonce):
        state_root = Path(state_root)
        with file_lock.locked_file(state_root / ".lease.lock"):
            session = self.inspect(state_root)
            manifest = session.manifest
            lease = manifest["lease"]
            if (
                lease is None
                or lease["owner_pid"] != os.getpid()
                or lease["owner_start"] != _process_start(os.getpid())
                or not secrets.compare_digest(lease["owner_nonce"], str(owner_nonce))
            ):
                raise SandboxSessionError("sandbox_lease_mismatch")
            manifest["lease"] = None
            manifest["updated_at"] = now()
            _write_session_manifest(state_root, manifest)
            return SandboxSession(state_root, manifest)

    def begin_call(
        self,
        state_root,
        *,
        call_id,
        reconciliation_token,
        container_name,
        expected_labels,
        plan_digest,
        return_state=None,
    ):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        current_state = manifest["state"]
        expected_return = str(return_state or current_state)
        if (
            current_state not in {"creating", "ready"}
            or expected_return != current_state
            or manifest["active_call"] is not None
        ):
            raise SandboxSessionError("sandbox_call_not_allowed")
        if (
            not isinstance(expected_labels, dict)
            or not expected_labels
            or _SHA256_RE.fullmatch(str(plan_digest)) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(reconciliation_token)) is None
        ):
            raise SandboxSessionError("sandbox_call_invalid")
        manifest["state"] = "running"
        manifest["active_call"] = {
            "call_id": str(call_id),
            "reconciliation_token": str(reconciliation_token),
            "container_name": str(container_name),
            "expected_labels": dict(expected_labels),
            "plan_digest": str(plan_digest),
            "container_id": "",
            "return_state": expected_return,
            "reconciliation": {
                "status": "not_started",
                "target_started": None,
                "cleanup_status": "not_started",
                "error_code": "",
            },
        }
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def record_container_id(self, state_root, container_id):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        active = manifest["active_call"]
        if (
            manifest["state"] != "running"
            or active is None
            or active["container_id"]
            or re.fullmatch(r"[0-9a-f]{64}", str(container_id)) is None
        ):
            raise SandboxSessionError("sandbox_call_invalid")
        active["container_id"] = str(container_id)
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def reconcile_active_call(
        self,
        state_root,
        find_containers,
        *,
        confirm_container_absent=None,
        preserve_absent=False,
    ):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        active = manifest["active_call"]
        if manifest["state"] not in {"running", "review_required"} or active is None:
            return session
        matches = list(find_containers(dict(active)))
        if len(matches) > 1:
            manifest["state"] = "review_required"
            manifest["updated_at"] = now()
            _write_session_manifest(session.state_root, manifest)
            return SandboxSession(session.state_root, manifest)
        if not matches:
            absent = bool(
                active["container_id"]
                and confirm_container_absent is not None
                and confirm_container_absent(active["container_id"]) is True
            )
            if active["container_id"] and (preserve_absent or not absent):
                manifest["state"] = "review_required"
            else:
                manifest["state"] = active["return_state"]
                manifest["active_call"] = None
            manifest["updated_at"] = now()
            _write_session_manifest(session.state_root, manifest)
            return SandboxSession(session.state_root, manifest)
        match = matches[0]
        if (
            not isinstance(match, dict)
            or match.get("name") != active["container_name"]
            or match.get("labels") != active["expected_labels"]
            or match.get("contract_matches") is not True
            or re.fullmatch(r"[0-9a-f]{64}", str(match.get("id", ""))) is None
            or active["container_id"]
            and match.get("id") != active["container_id"]
        ):
            manifest["state"] = "review_required"
        else:
            active["container_id"] = match["id"]
            manifest["state"] = "running"
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def finish_call(self, state_root, *, review_required=False):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if manifest["state"] != "running" or manifest["active_call"] is None:
            raise SandboxSessionError("sandbox_call_not_active")
        manifest["state"] = (
            "review_required"
            if review_required
            else manifest["active_call"]["return_state"]
        )
        if not review_required:
            manifest["active_call"] = None
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def record_call_reconciliation(
        self,
        state_root,
        *,
        target_started,
        cleanup_status,
        error_code,
    ):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if (
            manifest["state"] not in {"running", "review_required"}
            or manifest["active_call"] is None
            or target_started is not None
            and type(target_started) is not bool
            or cleanup_status not in {"not_attempted", "pending", "completed", "failed"}
            or not isinstance(error_code, str)
            or not error_code
        ):
            raise SandboxSessionError("sandbox_call_invalid")
        manifest["state"] = "review_required"
        manifest["active_call"]["reconciliation"] = {
            "status": "review_required",
            "target_started": target_started,
            "cleanup_status": str(cleanup_status),
            "error_code": error_code,
        }
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def mark_review_required(self, state_root, *, error_code):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if manifest["active_call"] is not None or manifest["state"] not in {
            "ready",
            "pending_review",
            "applying",
        }:
            raise SandboxSessionError("sandbox_review_not_allowed")
        manifest["state"] = "review_required"
        manifest["cleanup"] = {
            "status": "pending",
            "last_error_code": str(error_code),
        }
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def record_diff(
        self,
        state_root,
        *,
        diff_digest,
        candidate_count,
        blocked_count,
    ):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if (
            manifest["state"] != "ready"
            or manifest["active_call"] is not None
            or manifest["diff"]
            != {
                "digest": "",
                "status": "not_generated",
                "candidate_count": 0,
                "blocked_count": 0,
            }
            or not _matches(_SHA256_RE, diff_digest)
            or not _is_integer(candidate_count)
            or not _is_integer(blocked_count)
        ):
            raise SandboxSessionError("sandbox_diff_not_allowed")
        status = "diff_blocked" if blocked_count else "diff_ready"
        manifest["diff"] = {
            "digest": diff_digest,
            "status": status,
            "candidate_count": candidate_count,
            "blocked_count": blocked_count,
        }
        manifest["state"] = "pending_review"
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def record_apply_conflict(self, state_root, *, diff_digest):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if (
            manifest["state"] != "pending_review"
            or manifest["active_call"] is not None
            or manifest["diff"]["status"] != "diff_ready"
            or manifest["diff"]["digest"] != diff_digest
            or manifest["apply"] != {"journal_id": "", "status": "not_started"}
        ):
            raise SandboxSessionError("sandbox_apply_not_allowed")
        manifest["apply"] = {"journal_id": "", "status": "apply_conflicted"}
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def begin_apply(self, state_root, *, diff_digest, journal_id):
        session = self.inspect(state_root)
        manifest = session.manifest
        _require_owned_lease(manifest)
        if (
            manifest["state"] != "pending_review"
            or manifest["active_call"] is not None
            or manifest["diff"]["status"] != "diff_ready"
            or manifest["diff"]["digest"] != diff_digest
            or manifest["apply"] != {"journal_id": "", "status": "not_started"}
            or _APPLY_ID_RE.fullmatch(str(journal_id)) is None
        ):
            raise SandboxSessionError("sandbox_apply_not_allowed")
        manifest["state"] = "applying"
        manifest["apply"] = {"journal_id": str(journal_id), "status": "applying"}
        manifest["updated_at"] = now()
        _write_session_manifest(session.state_root, manifest)
        return SandboxSession(session.state_root, manifest)

    def finish_apply(self, state_root, *, journal_id, outcome):
        if outcome == "apply_review_required":
            try:
                session = self.inspect(state_root)
                relaxed_source_identity = False
            except SandboxSessionError:
                session = self._inspect(
                    state_root,
                    require_live_source=False,
                    allow_missing_sidecar_after_source_replacement=True,
                )
                relaxed_source_identity = True
        else:
            session = self.inspect(state_root)
            relaxed_source_identity = False
        manifest = session.manifest
        _require_owned_lease(manifest)
        if (
            manifest["state"] != "applying"
            or manifest["active_call"] is not None
            or manifest["apply"]
            != {"journal_id": str(journal_id), "status": "applying"}
            or outcome
            not in {
                "apply_applied",
                "apply_failed_rolled_back",
                "apply_review_required",
            }
        ):
            raise SandboxSessionError("sandbox_apply_not_allowed")
        manifest["apply"]["status"] = outcome
        if outcome == "apply_applied":
            manifest["state"] = "applied"
            manifest["lease"] = None
        elif outcome == "apply_failed_rolled_back":
            manifest["state"] = "pending_review"
        else:
            manifest["state"] = "review_required"
        manifest["updated_at"] = now()
        _write_session_manifest(
            session.state_root,
            manifest,
            require_live_source=not relaxed_source_identity,
            allow_missing_sidecar_after_source_replacement=(relaxed_source_identity),
        )
        return SandboxSession(session.state_root, manifest)


def find_project_sandbox_session(
    project_state_root,
    source_root,
    pony_session_id,
):
    """Return the unique Session bound by an immutable project sidecar."""
    if not isinstance(pony_session_id, str):
        raise SandboxSessionError("sandbox_state_invalid")
    try:
        project_state_root = Path(os.path.abspath(os.fspath(project_state_root)))
        source_root = Path(os.path.abspath(os.fspath(source_root)))
    except (TypeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    sidecar_parent = project_state_root / "sandbox_sessions"
    try:
        before = sidecar_parent.lstat()
    except FileNotFoundError:
        return None
    try:
        uid = os.geteuid() if hasattr(os, "geteuid") else before.st_uid
        parent_identity = private_directory_identity(sidecar_parent)
        source_info = source_root.lstat()
        if (
            not stat.S_ISDIR(before.st_mode)
            or sidecar_parent.is_symlink()
            or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) != 0o700
            or parent_identity != (before.st_dev, before.st_ino)
            or not stat.S_ISDIR(source_info.st_mode)
            or source_root.is_symlink()
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        matches = []
        for sidecar_path in sorted(
            sidecar_parent.iterdir(),
            key=lambda item: item.name,
        ):
            name = re.fullmatch(r"(sandbox_[0-9a-f]{32})\.json", sidecar_path.name)
            if name is None:
                raise SandboxSessionError("sandbox_state_invalid")
            raw = _read_strict_file(
                sidecar_path,
                max_bytes=MAX_MANIFEST_BYTES,
            )
            pointer = _validate_sidecar_shape(_decode_json(raw))
            if pointer["sandbox_id"] != name.group(1):
                raise SandboxSessionError("sandbox_state_invalid")
            state_root = Path(pointer["state_root"])
            session = SandboxSessionStore(state_root.parent.parent).inspect(state_root)
            sidecar = session.manifest["sidecar"]
            if (
                sidecar is None
                or Path(sidecar["path"]) != sidecar_path
                or _read_strict_file(
                    sidecar_path,
                    max_bytes=MAX_MANIFEST_BYTES,
                )
                != raw
            ):
                raise SandboxSessionError("sandbox_state_invalid")
            if (
                pointer["pony_session_id"] == pony_session_id
                and pointer["source_root"] == str(source_root)
                and (pointer["source_device"], pointer["source_inode"])
                == (source_info.st_dev, source_info.st_ino)
            ):
                matches.append(session)
        after = sidecar_parent.lstat()
        if (
            _identity(after) != _identity(before)
            or private_directory_identity(sidecar_parent) != parent_identity
            or len(matches) > 1
        ):
            raise SandboxSessionError("sandbox_state_invalid")
        return matches[0] if matches else None
    except SandboxSessionError as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxSessionError("sandbox_state_invalid") from exc
