"""Anchored, bounded, fail-closed workspace file I/O."""

import errno
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

from .private_files import (
    _open_private_directory,
    _private_directory_flags,
    _remove_owned_entry,
    _write_all,
)


class WorkspaceIOError(ValueError):
    """Stable fail-closed error raised by anchored workspace I/O."""

    def __init__(self, code, detail=""):
        self.code = str(code)
        message = self.code if not detail else f"{self.code}: {detail}"
        super().__init__(message)


def read_regular_bytes_anchored(
    workspace_root, raw_path, *, max_bytes, expected_root_identity=None
):
    """Read one relative regular file once through an anchored bounded fd."""
    parts = _workspace_relative_parts(raw_path)
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("invalid workspace file limit")
    parent = -1
    leaf = -1
    try:
        try:
            parent = _open_workspace_directory_anchored(
                workspace_root,
                parts[:-1],
                expected_root_identity=expected_root_identity,
            )
        except FileNotFoundError:
            return _missing_workspace_file()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            before = os.stat(
                parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
            _require_safe_workspace_file(before)
            leaf = os.open(
                parts[-1],
                flags,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return _missing_workspace_file()
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
                raise WorkspaceIOError(
                    "workspace_entry_unsafe",
                    "path is not a stable regular file",
                ) from None
            raise
        opened = os.fstat(leaf)
        _require_safe_workspace_file(opened)
        if opened.st_size > limit:
            raise _workspace_file_limit_error(opened)
        before_signature = _workspace_entry_signature(before)
        opened_signature = _workspace_entry_signature(opened)
        if opened_signature != before_signature:
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a stable regular file",
            )
        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_entry_unsafe",
        )
        chunks = []
        digest = hashlib.sha256()
        remaining = limit + 1
        while remaining:
            chunk = os.read(leaf, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise _workspace_file_limit_error(opened)
        try:
            current = os.stat(
                parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace file changed while it was read",
            ) from None
        after = os.fstat(leaf)
        if (
            _workspace_entry_signature(after) != opened_signature
            or _workspace_entry_signature(current) != opened_signature
        ):
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace file changed while it was read",
            )
        return {
            "exists": True,
            "data": data,
            "mode": stat.S_IMODE(opened.st_mode),
            "sha256": digest.hexdigest(),
            "identity": (opened.st_dev, opened.st_ino),
        }
    finally:
        if leaf >= 0:
            os.close(leaf)
        if parent >= 0:
            os.close(parent)


def list_directory_names_anchored(
    workspace_root,
    raw_path=".",
    *,
    max_entries,
    expected_root_identity=None,
):
    """List one directory without following or returning unsafe entries."""
    parts = _workspace_relative_parts(raw_path, allow_root=True)
    limit = int(max_entries)
    if limit < 1:
        raise ValueError("invalid workspace directory limit")
    descriptor = _open_workspace_directory_anchored(
        workspace_root,
        parts,
        expected_root_identity=expected_root_identity,
    )
    try:
        opened_identity = _workspace_inode_identity(os.fstat(descriptor))
        entries = []
        unsafe_count = 0
        scanned = 0
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > limit:
                    raise WorkspaceIOError(
                        "workspace_directory_limit_exceeded",
                        "workspace directory scan limit exceeded",
                    )
                try:
                    before = entry.stat(follow_symlinks=False)
                    current = os.stat(
                        entry.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except (FileNotFoundError, OSError):
                    unsafe_count += 1
                    continue
                safe = _workspace_entry_signature(before) == _workspace_entry_signature(
                    current
                ) and (
                    stat.S_ISDIR(current.st_mode)
                    or (stat.S_ISREG(current.st_mode) and current.st_nlink == 1)
                )
                if not safe:
                    unsafe_count += 1
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "mode": current.st_mode,
                        "size": current.st_size,
                    }
                )
        _require_current_workspace_directory(
            workspace_root,
            parts,
            opened_identity,
            expected_root_identity=expected_root_identity,
            error_code="workspace_entry_unsafe",
        )
        entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        return {
            "entries": tuple(entries),
            "unsafe_count": unsafe_count,
            "scanned": scanned,
        }
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _WorkspaceWriteRequest:
    workspace_root: object
    parts: tuple[str, ...]
    data: bytes
    limit: int
    expected_sha256: str | None
    expected_missing: bool
    mode: int | None
    expected_root_identity: object
    sync_file: object
    sync_parent: object


@dataclass(frozen=True)
class _WorkspaceTarget:
    descriptor: int
    signature: tuple | None
    digest: str | None
    mode: int


@dataclass(frozen=True)
class _WorkspaceTemp:
    descriptor: int
    name: str
    identity: tuple[int, int]


def _workspace_write_request(
    workspace_root,
    raw_path,
    data,
    *,
    max_bytes,
    expected_sha256,
    expected_missing,
    mode,
    expected_root_identity,
    fsync_file,
    fsync_parent,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("workspace atomic write requires bytes")
    rendered = bytes(data)
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("invalid workspace file limit")
    if len(rendered) > limit:
        raise WorkspaceIOError(
            "workspace_file_limit_exceeded",
            "workspace file exceeds the configured limit",
        )
    if expected_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_sha256)
    ):
        raise ValueError("invalid expected workspace digest")
    if expected_sha256 is not None and expected_missing:
        raise ValueError("workspace write cannot expect both content and absence")
    if type(expected_missing) is not bool:
        raise ValueError("invalid expected workspace absence")
    if mode is not None and (
        type(mode) is not int or mode < 0 or stat.S_IMODE(mode) != mode
    ):
        raise ValueError("invalid workspace file mode")
    return _WorkspaceWriteRequest(
        workspace_root=workspace_root,
        parts=_workspace_relative_parts(raw_path),
        data=rendered,
        limit=limit,
        expected_sha256=expected_sha256,
        expected_missing=expected_missing,
        mode=mode,
        expected_root_identity=expected_root_identity,
        sync_file=fsync_file or os.fsync,
        sync_parent=fsync_parent or os.fsync,
    )


def _require_workspace_write_parent(parent, request):
    _require_current_workspace_directory(
        request.workspace_root,
        request.parts[:-1],
        _workspace_inode_identity(os.fstat(parent)),
        expected_root_identity=request.expected_root_identity,
        error_code="workspace_changed_during_write",
    )


def _inspect_workspace_target(parent, request):
    try:
        existing = os.stat(request.parts[-1], dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is None:
        if request.expected_sha256 is not None:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file disappeared before write",
            )
        return _WorkspaceTarget(
            -1,
            None,
            None,
            request.mode if request.mode is not None else 0o644,
        )

    _require_safe_workspace_file(existing)
    if request.expected_missing:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace file appeared before write",
        )
    if request.expected_sha256 is not None and existing.st_size > request.limit:
        raise _workspace_file_limit_error(existing)
    descriptor = _open_workspace_regular_at(parent, request.parts[-1])
    try:
        opened = os.fstat(descriptor)
        signature = _workspace_entry_signature(opened)
        if signature != _workspace_entry_signature(existing):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file identity changed before write",
            )
        _require_workspace_write_parent(parent, request)
        digest = None
        if request.expected_sha256 is not None:
            digest = _workspace_descriptor_sha256(
                descriptor, opened.st_size, "workspace_changed_during_write"
            )
            if digest != request.expected_sha256:
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file content changed before write",
                )
        return _WorkspaceTarget(
            descriptor,
            signature,
            digest,
            request.mode if request.mode is not None else stat.S_IMODE(opened.st_mode),
        )
    except BaseException:
        os.close(descriptor)
        raise


def _create_workspace_temp(parent, request, target):
    name = f".{request.parts[-1]}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    identity = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, 0o600, dir_fd=parent)
        identity = _workspace_inode_identity(os.fstat(descriptor))
        os.fchmod(descriptor, target.mode)
        _require_workspace_write_parent(parent, request)
        _write_all(descriptor, request.data)
        request.sync_file(descriptor)
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _workspace_inode_identity(opened) != identity
            or _workspace_inode_identity(current) != identity
            or stat.S_IMODE(opened.st_mode) != target.mode
            or opened.st_size != len(request.data)
        ):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace temporary file changed",
            )
        return _WorkspaceTemp(descriptor, name, identity)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if identity is not None:
            try:
                _remove_owned_entry(parent, name, identity)
            except OSError:
                pass
        raise


def _revalidate_workspace_target(parent, request, target):
    _require_workspace_write_parent(parent, request)
    try:
        current = os.stat(request.parts[-1], dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if target.signature is None:
        if current is not None:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file appeared during write",
            )
        return
    if current is None or _workspace_entry_signature(current) != target.signature:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace file identity changed during write",
        )
    if target.digest is not None:
        current_digest = _workspace_descriptor_sha256(
            target.descriptor,
            target.signature[4],
            "workspace_changed_during_write",
        )
        if current_digest != target.digest:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file content changed during write",
            )
    if _workspace_entry_signature(os.fstat(target.descriptor)) != target.signature:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace file changed during write",
        )


def _install_workspace_temp(parent, request, target, temp):
    os.replace(
        temp.name,
        request.parts[-1],
        src_dir_fd=parent,
        dst_dir_fd=parent,
    )
    current = os.stat(request.parts[-1], dir_fd=parent, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or _workspace_inode_identity(current) != temp.identity
        or stat.S_IMODE(current.st_mode) != target.mode
    ):
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace replace result changed",
        )
    request.sync_parent(parent)
    _require_workspace_write_parent(parent, request)
    return {
        "mode": target.mode,
        "sha256": hashlib.sha256(request.data).hexdigest(),
        "created": target.signature is None,
    }


def write_regular_bytes_anchored_atomic(
    workspace_root,
    raw_path,
    data,
    *,
    max_bytes,
    expected_sha256=None,
    expected_missing=False,
    mode=None,
    expected_root_identity=None,
    fsync_file=None,
    fsync_parent=None,
):
    """CAS-check and atomically replace one workspace regular file."""
    request = _workspace_write_request(
        workspace_root,
        raw_path,
        data,
        max_bytes=max_bytes,
        expected_sha256=expected_sha256,
        expected_missing=expected_missing,
        mode=mode,
        expected_root_identity=expected_root_identity,
        fsync_file=fsync_file,
        fsync_parent=fsync_parent,
    )
    parent = _open_workspace_directory_anchored(
        request.workspace_root,
        request.parts[:-1],
        expected_root_identity=request.expected_root_identity,
        create=True,
    )
    target = None
    temp = None
    installed = False
    try:
        target = _inspect_workspace_target(parent, request)
        temp = _create_workspace_temp(parent, request, target)
        _revalidate_workspace_target(parent, request, target)
        result = _install_workspace_temp(parent, request, target, temp)
        installed = True
        return result
    finally:
        if temp is not None:
            os.close(temp.descriptor)
            if not installed:
                try:
                    _remove_owned_entry(parent, temp.name, temp.identity)
                except OSError:
                    pass
        if target is not None and target.descriptor >= 0:
            os.close(target.descriptor)
        os.close(parent)


def remove_regular_file_anchored(
    workspace_root,
    raw_path,
    *,
    max_bytes,
    expected_sha256,
    expected_root_identity=None,
    fsync_parent=None,
):
    """CAS-check and remove one anchored workspace regular file."""
    if re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256)) is None:
        raise ValueError("invalid expected workspace digest")
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("invalid workspace file limit")
    parts = _workspace_relative_parts(raw_path)
    parent = _open_workspace_directory_anchored(
        workspace_root,
        parts[:-1],
        expected_root_identity=expected_root_identity,
    )
    descriptor = -1
    try:
        try:
            before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file disappeared before remove",
            ) from None
        _require_safe_workspace_file(before)
        if before.st_size > limit:
            raise _workspace_file_limit_error(before)
        descriptor = _open_workspace_regular_at(parent, parts[-1])
        signature = _workspace_entry_signature(os.fstat(descriptor))
        if signature != _workspace_entry_signature(before):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file identity changed before remove",
            )
        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_changed_during_write",
        )
        digest = _workspace_descriptor_sha256(
            descriptor,
            before.st_size,
            "workspace_changed_during_write",
        )
        if digest != expected_sha256:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file content changed before remove",
            )
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (
            _workspace_entry_signature(current) != signature
            or _workspace_entry_signature(os.fstat(descriptor)) != signature
        ):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file changed before remove",
            )
        os.unlink(parts[-1], dir_fd=parent)
        (fsync_parent or os.fsync)(parent)
        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_changed_during_write",
        )
        return {"sha256": digest, "mode": stat.S_IMODE(before.st_mode)}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _missing_workspace_file():
    return {
        "exists": False,
        "data": None,
        "mode": None,
        "sha256": "",
        "identity": None,
    }


def _workspace_file_limit_error(value):
    error = WorkspaceIOError(
        "workspace_file_limit_exceeded",
        "workspace file exceeds the configured limit",
    )
    error.state = {
        "exists": True,
        "data": None,
        "mode": stat.S_IMODE(value.st_mode),
        "sha256": "",
        "identity": _workspace_inode_identity(value),
    }
    return error


def _workspace_relative_parts(raw_path, *, allow_root=False):
    raw = os.fsdecode(os.fspath(raw_path))
    if "\x00" in raw:
        raise ValueError("invalid relative path")
    relative = Path(raw)
    if relative.is_absolute():
        raise ValueError("invalid relative path")
    if allow_root and raw in {"", "."}:
        return ()
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid relative path")
    return tuple(relative.parts)


def _workspace_inode_identity(value):
    return value.st_dev, value.st_ino


def _workspace_entry_signature(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_safe_workspace_file(value):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "path is not a stable regular file",
        )


def _open_workspace_directory_anchored(
    workspace_root,
    parts,
    *,
    expected_root_identity=None,
    create=False,
):
    try:
        descriptor = _open_private_directory(workspace_root)
    except (OSError, ValueError) as exc:
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "workspace root is unsafe",
        ) from exc
    try:
        opened_root = os.fstat(descriptor)
        if expected_root_identity is not None and (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != tuple(expected_root_identity):
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace root changed",
            )
        directory_flags = _private_directory_flags()
        for component in parts:
            try:
                child = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise WorkspaceIOError(
                        "workspace_entry_unsafe",
                        "workspace parent could not be created safely",
                    ) from exc
                os.fsync(descriptor)
                try:
                    child = os.open(
                        component,
                        directory_flags,
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise WorkspaceIOError(
                        "workspace_entry_unsafe",
                        "workspace parent is unsafe",
                    ) from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise WorkspaceIOError(
                        "workspace_entry_unsafe",
                        "workspace parent is unsafe",
                    ) from None
                raise
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise WorkspaceIOError(
                    "workspace_entry_unsafe",
                    "workspace parent is not a directory",
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_current_workspace_directory(
    workspace_root,
    parts,
    expected_identity,
    *,
    expected_root_identity,
    error_code,
):
    current = -1
    try:
        try:
            current = _open_workspace_directory_anchored(
                workspace_root,
                parts,
                expected_root_identity=expected_root_identity,
            )
        except (FileNotFoundError, OSError, WorkspaceIOError) as exc:
            raise WorkspaceIOError(
                error_code,
                "workspace directory changed",
            ) from exc
        if _workspace_inode_identity(os.fstat(current)) != tuple(expected_identity):
            raise WorkspaceIOError(
                error_code,
                "workspace directory changed",
            )
    finally:
        if current >= 0:
            os.close(current)


def _open_workspace_regular_at(parent_descriptor, name):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a stable regular file",
            ) from None
        raise
    try:
        _require_safe_workspace_file(os.fstat(descriptor))
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _workspace_descriptor_sha256(descriptor, size, error_code):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = int(size)
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise WorkspaceIOError(
                error_code,
                "workspace file changed while hashing",
            )
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise WorkspaceIOError(
            error_code,
            "workspace file changed while hashing",
        )
    return digest.hexdigest()
