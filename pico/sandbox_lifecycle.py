"""Offline bundle lifecycle helpers, independent from runtime and CLI."""

from __future__ import annotations

import atexit
import hashlib
import io
import importlib.metadata
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import time

from .security import ensure_private_dir


_MANIFEST = ".pico-bundle-manifest.json"
_TOOLCHAIN_MARKER = ".pico-toolchain.json"
_MAX_IMPORT_BYTES = 512 * 1024 * 1024
_MAX_IMPORT_FILES = 100_000
_MAX_IMPORT_FILE_BYTES = 128 * 1024 * 1024
_MAX_IMPORT_DEPTH = 32
_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "identity",
        "platform",
        "arch",
        "node_version",
        "srt_version",
        "package_lock_sha256",
        "srt_capability",
        "tree_sha256",
        "total_size",
        "licenses",
        "files",
    }
)
_FILE_FIELDS = frozenset({"path", "type", "size", "mode", "sha256"})
_SYMLINK_FIELDS = _FILE_FIELDS | {"target"}
_SAFE_FILE_MODES = {0o400, 0o500}
_LICENSE_PREFIXES = ("license", "copying", "notice")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DOCTOR_CHECK_ORDER = (
    "platform",
    "private_permissions",
    "toolchain_identity",
    "os_capability",
    "policy_build",
    "minimal_smoke",
    "workspace_migration",
)
_MATRIX_STATUSES = {"verified", "failed", "unknown"}
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_RETIREMENT_FIELDS = frozenset(
    {
        "security_reason",
        "replacement",
        "compatibility_evidence",
        "rollback_window",
        "release_note",
    }
)
_TRASH_PREFIX = "prune-"
_MAX_PRUNE_DELETE_ENTRIES = 10_000
_RECENT_SECONDS = 24 * 60 * 60
_LEASE_SUFFIX = ".lease"


class UnknownIdentityError(ValueError):
    pass


class ArchiveValidationError(ValueError):
    def __init__(self, message, *, code="toolchain_archive_invalid"):
        super().__init__(message)
        self.code = code


def verified_bundle_inventory(root, *, inspector):
    """Return every candidate, preserving unknown entries as prune refusals."""
    root = Path(root)
    result = []
    if not root.exists():
        return result
    for path in sorted(root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_dir():
            inspected = {"verified": False, "identity": "", "reason": "unsafe_path"}
        else:
            try:
                inspected = inspector(path)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                inspected = {
                    "verified": False,
                    "identity": "",
                    "reason": type(exc).__name__,
                }
        item = dict(inspected or {})
        item.setdefault("verified", False)
        item.setdefault("identity", "")
        item.setdefault("reason", "identity_unknown")
        try:
            info = path.lstat()
            item.setdefault("device", info.st_dev)
            item.setdefault("inode", info.st_ino)
        except OSError:
            pass
        item.update(name=path.name, path=str(path))
        result.append(dict(sorted(item.items())))
    return result


def bundle_usage_state(bundle_path, *, referenced_identities=(), now=None):
    """Read conservative active/recent/reference evidence for one bundle."""
    bundle_path = Path(bundle_path)
    identity = bundle_path.name
    now = time.time() if now is None else float(now)
    toolchain_root = bundle_path.parent.parent
    references = toolchain_root / "references"
    persisted_references = set()
    if references.is_dir() and not references.is_symlink():
        for path in references.iterdir():
            try:
                if path.is_file() and not path.is_symlink():
                    persisted_references.add(path.read_text(encoding="ascii").strip())
            except OSError:
                persisted_references.add(identity)
    usage = toolchain_root / "usage" / identity
    try:
        timestamp = usage.stat().st_mtime if usage.is_file() else bundle_path.stat().st_mtime
        recent = now - timestamp <= _RECENT_SECONDS
    except OSError:
        recent = True
    leases = toolchain_root / "leases"
    active = False
    if leases.is_dir() and not leases.is_symlink():
        for lease in leases.glob(f"{identity}-*{_LEASE_SUFFIX}"):
            try:
                payload = lease.read_text(encoding="ascii").strip()
                pid = int(payload)
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except (OSError, ValueError):
                active = True
                break
            active = True
            break
    return {
        "active_lease": active,
        "recent": recent,
        "referenced": identity in set(referenced_identities) | persisted_references,
    }


def acquire_bundle_lease(root, identity):
    """Create a process lease that prune recognizes until process exit."""
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", identity):
        raise ValueError("bundle identity is required")
    root = Path(root)
    references = ensure_private_dir(root / "references")
    try:
        pico_version = importlib.metadata.version("pico")
    except importlib.metadata.PackageNotFoundError:
        pico_version = "source"
    reference = references / f"pico-{re.sub(r'[^A-Za-z0-9._-]', '_', pico_version)}"
    descriptor = os.open(
        reference,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, identity.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(references)
    usage = ensure_private_dir(root / "usage") / identity
    descriptor = os.open(
        usage,
        os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.utime(usage, None, follow_symlinks=False)
    leases = ensure_private_dir(root / "leases")
    path = leases / f"{identity}-{os.getpid()}{_LEASE_SUFFIX}"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        info = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or path.read_text(encoding="ascii").strip() != str(os.getpid())
        ):
            raise ValueError("bundle lease is unsafe") from None
        return path
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(leases)
    atexit.register(path.unlink, missing_ok=True)
    return path


def _retirement_deadline(retirement, *, pinned_identity):
    if (
        not isinstance(retirement, dict)
        or set(retirement) != _RETIREMENT_FIELDS
        or any(
            not isinstance(retirement[key], str) or not retirement[key].strip()
            for key in _RETIREMENT_FIELDS
        )
        or retirement["replacement"] != pinned_identity
    ):
        return None
    try:
        deadline = datetime.fromisoformat(
            retirement["rollback_window"].replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return deadline.astimezone(timezone.utc) if deadline.tzinfo is not None else None


def plan_prune(inventory, *, pinned_identity, now=None, include_quarantine=False):
    """Build a non-mutating keep/delete/refuse plan from trusted inventory."""
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError("prune time must be timezone-aware")
    keep, remove, refuse = [], [], []
    for raw in inventory:
        item = dict(raw)
        if not item.get("verified") or not item.get("identity"):
            item["decision"] = "refuse"
            item["reason"] = item.get("reason") or "identity_unknown"
            refuse.append(item)
        elif item["identity"] == pinned_identity:
            item["decision"] = "keep"
            item["reason"] = "pinned"
            keep.append(item)
        elif item.get("active_lease") or item.get("active_lock") or item.get("staging"):
            item["decision"] = "keep"
            item["reason"] = "active"
            keep.append(item)
        elif item.get("recent"):
            item["decision"] = "keep"
            item["reason"] = "recent"
            keep.append(item)
        elif item.get("referenced"):
            item["decision"] = "keep"
            item["reason"] = "referenced"
            keep.append(item)
        elif item.get("location") == "quarantine" and not include_quarantine:
            item["decision"] = "refuse"
            item["reason"] = "quarantine_not_included"
            refuse.append(item)
        else:
            deadline = _retirement_deadline(
                item.get("retirement"), pinned_identity=pinned_identity
            )
            if deadline is None:
                item["decision"] = "refuse"
                item["reason"] = "retirement_evidence_invalid"
                refuse.append(item)
            elif deadline > now.astimezone(timezone.utc):
                item["decision"] = "keep"
                item["reason"] = "rollback_window"
                keep.append(item)
            else:
                item["decision"] = "delete"
                item["reason"] = "retired"
                remove.append(item)
    return {
        "pinned_identity": pinned_identity,
        "keep": keep,
        "delete": remove,
        "refuse": refuse,
    }


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bounded_delete_tree(root, *, limit):
    """Delete at most ``limit`` filesystem entries without following links."""
    root = Path(root)
    removed = 0
    stack = [(root, None)]
    while stack and removed < limit:
        path, iterator = stack[-1]
        if iterator is None:
            try:
                path.lstat()
            except FileNotFoundError:
                stack.pop()
                continue
            if path.is_symlink() or not path.is_dir():
                path.unlink()
                removed += 1
                stack.pop()
                continue
            iterator = os.scandir(path)
            stack[-1] = (path, iterator)
        try:
            child = next(iterator)
        except StopIteration:
            iterator.close()
            path.rmdir()
            removed += 1
            stack.pop()
        else:
            stack.append((Path(child.path), None))
    for _, iterator in stack:
        if iterator is not None:
            iterator.close()
    return removed, not root.exists()


def resume_prune_trash(trash_root, *, max_entries=_MAX_PRUNE_DELETE_ENTRIES):
    """Continue bounded cleanup of previously authorized prune trash."""
    if type(max_entries) is not int or max_entries < 0:
        raise ValueError("prune delete budget must be a non-negative integer")
    trash_root = Path(trash_root)
    if not trash_root.exists():
        return {"deleted_entries": 0, "pending_cleanup": []}
    if trash_root.is_symlink() or not trash_root.is_dir():
        raise UnknownIdentityError("refusing unsafe trash root")
    remaining = max_entries
    pending = []
    for path in sorted(trash_root.iterdir()):
        if not path.name.startswith(_TRASH_PREFIX):
            continue
        removed, complete = _bounded_delete_tree(path, limit=remaining)
        remaining -= removed
        if not complete:
            pending.append(str(path))
        if remaining == 0:
            pending.extend(
                str(candidate)
                for candidate in sorted(trash_root.iterdir())
                if candidate.name.startswith(_TRASH_PREFIX)
                and str(candidate) not in pending
            )
            break
    _fsync_directory(trash_root)
    return {
        "deleted_entries": max_entries - remaining,
        "pending_cleanup": pending,
    }


def apply_prune_plan(
    plan,
    *,
    trash_root,
    allowed_roots,
    max_delete_entries=_MAX_PRUNE_DELETE_ENTRIES,
):
    """Rename authorized candidates into trash and clean it within a bound."""
    trash_root = ensure_private_dir(trash_root)
    cleanup = resume_prune_trash(trash_root, max_entries=max_delete_entries)
    remaining = max_delete_entries - cleanup["deleted_entries"]
    allowed = {Path(path).resolve(strict=True) for path in allowed_roots}
    if not allowed:
        raise ValueError("prune requires an allowed source root")
    trashed = []
    for item in plan.get("delete", ()):
        identity = item.get("identity")
        if not item.get("verified") or not identity:
            raise UnknownIdentityError("refusing to prune bundle with unknown identity")
        if identity == plan.get("pinned_identity"):
            raise ValueError("refusing to prune pinned bundle")
        source = Path(item["path"])
        if not source.is_dir() or source.is_symlink():
            raise UnknownIdentityError("refusing to prune unsafe bundle path")
        parent = source.parent.resolve(strict=True)
        info = source.lstat()
        if (
            parent not in allowed
            or (info.st_dev, info.st_ino)
            != (item.get("device"), item.get("inode"))
        ):
            raise UnknownIdentityError("refusing changed bundle identity")
        target = trash_root / f"{_TRASH_PREFIX}{next(tempfile._get_candidate_names())}"
        os.replace(source, target)
        _fsync_directory(source.parent)
        _fsync_directory(trash_root)
        trashed.append({**item, "trash_path": str(target)})
        removed, complete = _bounded_delete_tree(target, limit=remaining)
        cleanup["deleted_entries"] += removed
        remaining -= removed
        if not complete:
            cleanup["pending_cleanup"].append(str(target))
    _fsync_directory(trash_root)
    return {
        "trashed": trashed,
        "kept": list(plan.get("keep", ())),
        "refuse": list(plan.get("refuse", ())),
        **cleanup,
    }


def _stat_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_source_file(path, expected=None):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    try:
        before = path.lstat() if expected is None else expected
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        after_open = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > _MAX_IMPORT_FILE_BYTES
            or _stat_identity(before) != _stat_identity(opened)
            or _stat_identity(after_open) != _stat_identity(opened)
        ):
            raise ArchiveValidationError(f"source file changed or is unsafe: {path.name}")
        chunks = []
        remaining = _MAX_IMPORT_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            len(data) > _MAX_IMPORT_FILE_BYTES
            or len(data) != opened.st_size
            or _stat_identity(after_read) != _stat_identity(opened)
        ):
            raise ArchiveValidationError(f"source file changed or is unsafe: {path.name}")
        return data
    except ArchiveValidationError:
        raise
    except OSError as exc:
        raise ArchiveValidationError(f"source file is unsafe: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_source_link(path, root, expected=None):
    try:
        before = path.lstat() if expected is None else expected
        target = os.readlink(path)
        after = path.lstat()
    except OSError as exc:
        raise ArchiveValidationError(f"source link is unsafe: {path.name}") from exc
    if (
        not stat.S_ISLNK(before.st_mode)
        or _stat_identity(before) != _stat_identity(after)
        or not target
        or PurePosixPath(target).is_absolute()
        or not (path.parent / target).resolve(strict=False).is_relative_to(root)
    ):
        raise ArchiveValidationError(f"source link is unsafe: {path.name}")
    return target, f"symlink:{target}".encode()


def _file_inventory(root):
    files = []
    root = Path(root)
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ArchiveValidationError("bundle source is unsafe") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ArchiveValidationError("bundle source is unsafe")
    root = root.resolve(strict=True)
    total_size = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        try:
            info = path.lstat()
        except OSError as exc:
            raise ArchiveValidationError(f"unsafe source path: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            target, payload = _read_source_link(path, root, info)
            files.append({
                "path": relative, "type": "symlink", "target": target,
                "size": len(payload), "mode": 0,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
            total_size += len(payload)
        elif stat.S_ISREG(info.st_mode):
            data = _read_source_file(path, info)
            files.append({
                "path": relative,
                "type": "file",
                "size": len(data),
                "mode": 0o500 if info.st_mode & 0o111 else 0o400,
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            total_size += len(data)
        elif not stat.S_ISDIR(info.st_mode):
            raise ArchiveValidationError(f"unsafe source path: {relative}")
        if len(files) > _MAX_IMPORT_FILES:
            raise ArchiveValidationError("bundle source contains too many files")
        if total_size > _MAX_IMPORT_BYTES:
            raise ArchiveValidationError("bundle source exceeds total size limit")
    return files


def bundle_tree_hash(tree):
    encoded = json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def export_bundle(
    source,
    archive,
    *,
    identity,
    platform,
    arch,
    node_version="",
    srt_version="",
    package_lock_sha256="",
    srt_capability="",
):
    """Create a deterministic offline archive with a verified file manifest."""
    source, archive = Path(source), Path(archive)
    files = _file_inventory(source)
    tree = {
        item["path"]: item["sha256"]
        for item in files
        if item["path"] != _TOOLCHAIN_MARKER
    }
    manifest = {
        "version": 1,
        "identity": identity,
        "platform": platform,
        "arch": arch,
        "node_version": node_version,
        "srt_version": srt_version,
        "package_lock_sha256": package_lock_sha256,
        "srt_capability": srt_capability,
        "tree_sha256": bundle_tree_hash(tree),
        "total_size": sum(item["size"] for item in files),
        "licenses": [
            item["path"] for item in files
            if Path(item["path"]).name.casefold().startswith(_LICENSE_PREFIXES)
        ],
        "files": files,
    }
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{archive.name}.", dir=archive.parent)
    os.close(fd)
    temporary = Path(raw_temp)
    try:
        with tarfile.open(temporary, "w") as output:
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            info = tarfile.TarInfo(_MANIFEST)
            info.size, info.mode, info.mtime = len(encoded), 0o600, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            output.addfile(info, io.BytesIO(encoded))
            for entry in manifest["files"]:
                path = source / entry["path"]
                info = tarfile.TarInfo(entry["path"])
                info.mode, info.mtime = entry["mode"], 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if entry["type"] == "symlink":
                    target, payload = _read_source_link(path, source.resolve(strict=True))
                    if (
                        target != entry["target"]
                        or len(payload) != entry["size"]
                        or hashlib.sha256(payload).hexdigest() != entry["sha256"]
                    ):
                        raise ArchiveValidationError(
                            f"source link changed: {entry['path']}"
                        )
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    info.size = 0
                    output.addfile(info)
                else:
                    data = _read_source_file(path)
                    if (
                        len(data) != entry["size"]
                        or hashlib.sha256(data).hexdigest() != entry["sha256"]
                    ):
                        raise ArchiveValidationError(
                            f"source file changed: {entry['path']}"
                        )
                    info.type = tarfile.REGTYPE
                    info.size = len(data)
                    output.addfile(info, io.BytesIO(data))
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def _safe_name(name):
    path = PurePosixPath(name)
    return (
        bool(name)
        and path.as_posix() == name
        and not path.is_absolute()
        and ".." not in path.parts
        and "" not in path.parts
    )


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ArchiveValidationError("archive JSON contains duplicate keys")
        value[key] = item
    return value


def _validate_manifest(manifest):
    if (
        not isinstance(manifest, dict)
        or set(manifest) != _MANIFEST_FIELDS
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
    ):
        raise ArchiveValidationError("invalid bundle manifest")
    for key in ("identity", "platform", "arch"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise ArchiveValidationError("invalid bundle manifest")
    for key in ("node_version", "srt_version", "srt_capability"):
        if not isinstance(manifest[key], str):
            raise ArchiveValidationError("invalid bundle manifest")
    lock_hash = manifest["package_lock_sha256"]
    if not isinstance(lock_hash, str) or (lock_hash and not _SHA256_RE.fullmatch(lock_hash)):
        raise ArchiveValidationError("invalid package lock hash")
    if not isinstance(manifest["tree_sha256"], str) or not _SHA256_RE.fullmatch(
        manifest["tree_sha256"]
    ):
        raise ArchiveValidationError("invalid bundle tree hash")
    entries = manifest["files"]
    if not isinstance(entries, list) or len(entries) > _MAX_IMPORT_FILES:
        raise ArchiveValidationError("invalid bundle file manifest")
    expected = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") not in {"file", "symlink"}:
            raise ArchiveValidationError("invalid bundle file entry")
        fields = _SYMLINK_FIELDS if entry["type"] == "symlink" else _FILE_FIELDS
        name = entry.get("path")
        if set(entry) != fields or not isinstance(name, str) or not _safe_name(name):
            raise ArchiveValidationError("invalid bundle file entry")
        if name == _MANIFEST or len(PurePosixPath(name).parts) > _MAX_IMPORT_DEPTH:
            raise ArchiveValidationError("invalid bundle file path")
        if name in expected:
            raise ArchiveValidationError("duplicate manifest paths")
        if (
            type(entry.get("size")) is not int
            or entry["size"] < 0
            or entry["size"] > _MAX_IMPORT_FILE_BYTES
            or not isinstance(entry.get("sha256"), str)
            or not _SHA256_RE.fullmatch(entry["sha256"])
        ):
            raise ArchiveValidationError("invalid bundle file entry")
        mode = entry.get("mode")
        if entry["type"] == "file":
            if type(mode) is not int or mode not in _SAFE_FILE_MODES:
                raise ArchiveValidationError(f"invalid file mode: {name}")
        elif (
            type(mode) is not int
            or mode != 0
            or not isinstance(entry.get("target"), str)
        ):
            raise ArchiveValidationError("invalid bundle link entry")
        expected[name] = entry
    symlinks = {path for path, entry in expected.items() if entry["type"] == "symlink"}
    for name in expected:
        parts = PurePosixPath(name).parts
        if any(PurePosixPath(*parts[:index]).as_posix() in symlinks for index in range(1, len(parts))):
            raise ArchiveValidationError("archive link cannot be a member parent")
    declared_total = manifest["total_size"]
    if (
        type(declared_total) is not int
        or declared_total < 0
        or declared_total > _MAX_IMPORT_BYTES
        or sum(entry["size"] for entry in entries) != declared_total
    ):
        raise ArchiveValidationError("invalid bundle total size")
    licenses = manifest["licenses"]
    if (
        not isinstance(licenses, list)
        or len(licenses) != len(set(licenses))
        or any(not isinstance(path, str) for path in licenses)
    ):
        raise ArchiveValidationError("invalid license index")
    for path in licenses:
        entry = expected.get(path)
        if (
            entry is None
            or entry["type"] != "file"
            or not Path(path).name.casefold().startswith(_LICENSE_PREFIXES)
        ):
            raise ArchiveValidationError("invalid license index")
    tree = {
        path: entry["sha256"]
        for path, entry in expected.items()
        if path != _TOOLCHAIN_MARKER
    }
    if manifest["tree_sha256"] != bundle_tree_hash(tree):
        raise ArchiveValidationError("bundle tree hash mismatch")
    return expected


def _open_archive_descriptor(path):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise ArchiveValidationError("archive path is unsafe") from exc
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ArchiveValidationError("archive path is unsafe")
    if info.st_size > _MAX_IMPORT_BYTES:
        os.close(descriptor)
        raise ArchiveValidationError("archive exceeds total size limit")
    return descriptor, info


def _read_tar_member(source, member, *, limit, label):
    if type(member.size) is not int or member.size < 0 or member.size > limit:
        raise ArchiveValidationError(f"{label} exceeds size limit")
    stream = source.extractfile(member)
    if stream is None:
        raise ArchiveValidationError(f"{label} is unreadable")
    try:
        data = stream.read(member.size + 1)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    if len(data) != member.size:
        raise ArchiveValidationError(f"{label} size mismatch")
    return data


def import_bundle(archive, destination, *, expected_platform=None, expected_arch=None, importer=None):
    """Validate and stage an offline archive before atomic installation."""
    archive, destination = Path(archive), Path(destination)
    archive_descriptor, archive_info = _open_archive_descriptor(archive)
    try:
        parent = ensure_private_dir(destination.parent)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        os.chmod(staging, 0o700)
        try:
            try:
                with os.fdopen(os.dup(archive_descriptor), "rb") as archive_stream:
                    with tarfile.open(fileobj=archive_stream, mode="r:") as source:
                        members = []
                        names = set()
                        for member in source:
                            if len(members) >= _MAX_IMPORT_FILES + 1:
                                raise ArchiveValidationError(
                                    "archive contains too many members"
                                )
                            if (
                                not _safe_name(member.name)
                                or len(PurePosixPath(member.name).parts)
                                > _MAX_IMPORT_DEPTH
                                or not (member.isfile() or member.issym())
                            ):
                                raise ArchiveValidationError(
                                    "unsafe archive path or link"
                                )
                            if member.name in names:
                                raise ArchiveValidationError(
                                    "archive contains duplicate paths"
                                )
                            members.append(member)
                            names.add(member.name)
                        manifests = [m for m in members if m.name == _MANIFEST]
                        if len(manifests) != 1 or not manifests[0].isfile():
                            raise ArchiveValidationError(
                                "archive manifest missing or duplicated"
                            )
                        manifest_data = _read_tar_member(
                            source,
                            manifests[0],
                            limit=_MAX_IMPORT_FILE_BYTES,
                            label="archive manifest",
                        )
                        manifest = json.loads(
                            manifest_data.decode("utf-8"),
                            object_pairs_hook=_object_from_pairs,
                        )
                        expected = _validate_manifest(manifest)
                        actual_members = {
                            member.name: member
                            for member in members
                            if member.name != _MANIFEST
                        }
                        if set(actual_members) != set(expected):
                            raise ArchiveValidationError(
                                "archive paths do not match manifest"
                            )
                        extracted_total = 0
                        links = []
                        for name, member in actual_members.items():
                            entry = expected[name]
                            target = staging.joinpath(*PurePosixPath(name).parts)
                            if member.issym():
                                if (
                                    entry["type"] != "symlink"
                                    or member.size != 0
                                    or not member.linkname
                                    or member.linkname != entry["target"]
                                ):
                                    raise ArchiveValidationError(
                                        f"link mismatch: {name}"
                                    )
                                link = PurePosixPath(member.linkname)
                                resolved = (
                                    target.parent.joinpath(*link.parts).resolve(
                                        strict=False
                                    )
                                )
                                if link.is_absolute() or not resolved.is_relative_to(
                                    staging
                                ):
                                    raise ArchiveValidationError(f"unsafe link: {name}")
                                data = f"symlink:{member.linkname}".encode()
                                links.append((target, entry["target"]))
                            else:
                                if entry["type"] != "file":
                                    raise ArchiveValidationError(
                                        f"file type mismatch: {name}"
                                    )
                                if member.size != entry["size"]:
                                    raise ArchiveValidationError(
                                        f"size mismatch: {name}"
                                    )
                                data = _read_tar_member(
                                    source,
                                    member,
                                    limit=_MAX_IMPORT_FILE_BYTES,
                                    label=f"file: {name}",
                                )
                            if len(data) != entry["size"]:
                                raise ArchiveValidationError(f"size mismatch: {name}")
                            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                                raise ArchiveValidationError(f"hash mismatch: {name}")
                            extracted_total += len(data)
                            if extracted_total > manifest["total_size"]:
                                raise ArchiveValidationError(
                                    "bundle total size mismatch"
                                )
                            if entry["type"] == "file":
                                target.parent.mkdir(
                                    mode=0o700, parents=True, exist_ok=True
                                )
                                target.write_bytes(data)
                                os.chmod(target, entry["mode"])
                        if extracted_total != manifest["total_size"]:
                            raise ArchiveValidationError("bundle total size mismatch")
                        for target, linkname in links:
                            target.parent.mkdir(
                                mode=0o700, parents=True, exist_ok=True
                            )
                            if target.exists() or target.is_symlink():
                                raise ArchiveValidationError(
                                    f"unsafe link path: {target.name}"
                                )
                            resolved = (target.parent / linkname).resolve(strict=False)
                            if not resolved.exists():
                                raise ArchiveValidationError(
                                    f"unsafe link target: {target.name}"
                                )
                            os.symlink(linkname, target)
                        for path in staging.rglob("*"):
                            if path.is_dir() and not path.is_symlink():
                                os.chmod(path, 0o700)
                if _stat_identity(os.fstat(archive_descriptor)) != _stat_identity(
                    archive_info
                ):
                    raise ArchiveValidationError("archive changed during import")
            except (
                tarfile.TarError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                UnicodeError,
            ) as exc:
                if isinstance(exc, ArchiveValidationError):
                    raise
                raise ArchiveValidationError("invalid archive") from exc
            if expected_platform is not None and manifest.get("platform") != expected_platform:
                raise ArchiveValidationError("platform mismatch")
            if expected_arch is not None and manifest.get("arch") != expected_arch:
                raise ArchiveValidationError("arch mismatch")
            if importer is not None:
                importer(staging, manifest)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            os.replace(staging, destination)
            return manifest
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    finally:
        os.close(archive_descriptor)


def build_compatibility_payload(
    *,
    required,
    actual,
    verification_status="unknown",
    last_smoke_commit="",
):
    """Build one fixed, auditable compatibility-matrix row.

    A local version match is not release evidence.  Only an explicitly
    verified smoke commit can produce ``compatible=True``.
    """
    if verification_status not in _MATRIX_STATUSES:
        raise ValueError("invalid compatibility verification status")
    if last_smoke_commit and not (
        isinstance(last_smoke_commit, str) and _COMMIT_RE.fullmatch(last_smoke_commit)
    ):
        raise ValueError("invalid compatibility smoke commit")
    required = dict(required or {})
    actual = dict(actual or {})
    aliases = {
        "os": actual.get("os", actual.get("platform", "unknown")),
        "architecture": actual.get("architecture", actual.get("arch", "unknown")),
    }
    mismatches = {
        key: {"expected": expected, "actual": actual.get(key)}
        for key, expected in required.items()
        if actual.get(key, aliases.get(key)) != expected
    }
    evidence_values = (
        actual.get("pico_version"),
        actual.get("python_version"),
        actual.get("node_version"),
        actual.get("srt_version"),
        aliases["os"],
        aliases["architecture"],
        actual.get("kernel"),
        actual.get("bwrap"),
        actual.get("userns"),
        actual.get("seccomp"),
    )
    evidence_complete = bool(required) and all(
        value not in {None, "", "unknown"} for value in evidence_values
    )
    if mismatches:
        status = "failed"
        compatible = False
    elif verification_status == "failed":
        status = "failed"
        compatible = False
    elif verification_status == "verified" and last_smoke_commit and evidence_complete:
        status = "verified"
        compatible = True
    else:
        status = "unknown"
        compatible = None
    matrix = {
        "record_type": "sandbox_compatibility_matrix",
        "format_version": 1,
        "status": status,
        "compatible": compatible,
        "pico_version": actual.get("pico_version", "unknown"),
        "python_version": actual.get("python_version", "unknown"),
        "node_version": actual.get("node_version", "unknown"),
        "srt_version": actual.get("srt_version", "unknown"),
        "os": aliases["os"],
        "architecture": aliases["architecture"],
        "kernel": actual.get("kernel", "unknown"),
        "bwrap": actual.get("bwrap", "not_applicable"),
        "userns": actual.get("userns", "not_applicable"),
        "seccomp": actual.get("seccomp", "not_applicable"),
        "last_smoke_commit": last_smoke_commit,
        "required": required,
        "actual": actual,
        "mismatches": mismatches,
    }
    return matrix


def build_doctor_payload(*, compatibility, inventory, checks=None):
    """Build a fixed-order, dependency-aware doctor payload."""
    allowed = {"pass", "warn", "fail", "not_applicable", "unknown"}
    # Ignore legacy/foreign check names; the emitted contract remains fixed.
    provided = {
        key: value for key, value in dict(checks or {}).items()
        if key in DOCTOR_CHECK_ORDER
    }
    normalized = {}
    blocked_by = None
    for check_id in DOCTOR_CHECK_ORDER:
        value = provided.get(check_id)
        if isinstance(value, dict):
            item = dict(value)
            status = item.get("status", "unknown")
        elif value is True:
            item, status = {}, "pass"
        elif value is False:
            item, status = {}, "fail"
        else:
            item, status = {}, "unknown" if value is None else str(value)
        if status not in allowed:
            status = "fail"
            item["reason_code"] = "invalid_check_status"
        if blocked_by is not None:
            status = "unknown"
            item = {
                "reason_code": f"blocked_by_{blocked_by}",
                "remediation": normalized[blocked_by]["remediation"],
            }
        normalized[str(check_id)] = {
            "check_id": str(check_id),
            "status": status,
            "reason_code": str(item.get("reason_code", "check_not_run" if status == "unknown" else status)),
            "remediation": str(item.get("remediation", "")),
        }
        if blocked_by is None and status in {"fail", "unknown"}:
            blocked_by = check_id
    ok = compatibility.get("compatible") is True and all(
        item["status"] in {"pass", "not_applicable"} for item in normalized.values()
    )
    return {
        "ok": ok,
        "compatibility": dict(compatibility),
        "inventory": list(inventory),
        "checks": normalized,
    }
