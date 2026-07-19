"""Application-owned private state file I/O and atomic transactions."""

import errno
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import stat

from .paths import _lexical_absolute


_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())


class PrivateAtomicWriteError(RuntimeError):
    """An atomic write failed after its committed state became ambiguous."""

    committed = True


def ensure_private_dir(path):
    path = _lexical_absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        created = False
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                created = True
            mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("private directory has symlink component")
        if not stat.S_ISDIR(mode):
            raise ValueError("private directory has unsafe component")
        if created:
            current.chmod(0o700, follow_symlinks=False)
    path.chmod(0o700, follow_symlinks=False)
    return path


def ensure_private_file(path, *, trusted_root=None, trusted_root_identity=None):
    path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return path


def read_private_text(
    path,
    *,
    encoding="utf-8",
    errors="strict",
    trusted_root=None,
    trusted_root_identity=None,
    max_bytes=None,
):
    return read_private_bytes(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        max_bytes=max_bytes,
    ).decode(encoding, errors=errors)


def read_private_bytes(
    path,
    *,
    trusted_root=None,
    trusted_root_identity=None,
    max_bytes=None,
    harden=True,
):
    path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        opened = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else opened.st_uid
        if harden and stat.S_IMODE(opened.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        elif opened.st_uid != uid or stat.S_IMODE(opened.st_mode) != 0o600:
            raise ValueError("private file permissions are unsafe")
        chunks = []
        remaining = None if max_bytes is None else int(max_bytes) + 1
        while remaining is None or remaining > 0:
            size = 64 * 1024 if remaining is None else min(64 * 1024, remaining)
            chunk = os.read(descriptor, size)
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        data = b"".join(chunks)
        if max_bytes is not None and len(data) > int(max_bytes):
            raise ValueError("private file too large")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_directory_flags():
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not _OPEN_SUPPORTS_DIR_FD or not directory_flag or not nofollow_flag:
        raise RuntimeError("private file descriptor traversal unavailable")
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | directory_flag | nofollow_flag


def _open_private_directory(path):
    path = _lexical_absolute(path)
    directory_flags = _private_directory_flags()
    descriptor = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:]:
            current = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("refusing symlink component")
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError("private file has unsafe parent")
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("refusing symlink component") from None
                raise
            try:
                if not stat.S_ISDIR(os.fstat(child_descriptor).st_mode):
                    raise ValueError("private file has unsafe parent")
            except Exception:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def private_directory_identity(path):
    descriptor = _open_private_directory(path)
    try:
        opened = os.fstat(descriptor)
        return opened.st_dev, opened.st_ino
    finally:
        os.close(descriptor)


def private_file_signature(path, *, trusted_root=None, trusted_root_identity=None):
    """Return a no-follow identity/version signature for a private file."""
    _path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        opened = os.fstat(descriptor)
        return (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
            stat.S_IMODE(opened.st_mode),
            opened.st_uid,
        )
    finally:
        os.close(descriptor)


def _open_private_parent(path, *, trusted_root=None, trusted_root_identity=None):
    path = _lexical_absolute(path)
    if trusted_root is None:
        return path, _open_private_directory(path.parent)
    root = _lexical_absolute(trusted_root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("private path escapes trusted root") from exc
    if not relative.parts:
        raise ValueError("private path must name a file")
    descriptor = _open_private_directory(root)
    try:
        opened = os.fstat(descriptor)
        if trusted_root_identity is None or (
            opened.st_dev,
            opened.st_ino,
        ) != tuple(trusted_root_identity):
            raise ValueError("private root changed")
        directory_flags = _private_directory_flags()
        for component in relative.parts[:-1]:
            current = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("refusing symlink component")
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError("private file has unsafe parent")
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("refusing symlink component") from None
                raise
            try:
                if not stat.S_ISDIR(os.fstat(child_descriptor).st_mode):
                    raise ValueError("private file has unsafe parent")
            except Exception:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
        return path, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_file(path, *, trusted_root=None, trusted_root_identity=None):
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(current.st_mode):
            raise ValueError("refusing symlink component")
        if not stat.S_ISREG(current.st_mode):
            raise ValueError("path is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)

    try:
        opened = os.fstat(descriptor)
        path_current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (path_current.st_dev, path_current.st_ino)
        ):
            raise ValueError("private file changed or has multiple links")
    except Exception:
        os.close(descriptor)
        raise
    return path, descriptor


def _write_all(descriptor, data):
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("private write failed")
        view = view[written:]


def _private_entry_stat(parent_descriptor, name):
    return os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )


def _remove_owned_entry(parent_descriptor, name, identity):
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        return True
    if (current.st_dev, current.st_ino) == identity:
        os.unlink(name, dir_fd=parent_descriptor)
        return True
    return False


def _private_entry_signature(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _descriptor_digest(descriptor, size, error):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = int(size)
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise ValueError(error)
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError(error)
    return digest.digest()


def _validate_private_backup(
    parent_descriptor,
    name,
    descriptor,
    signature,
    digest,
    error,
):
    opened = os.fstat(descriptor)
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        raise ValueError(error) from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or _private_entry_signature(opened) != signature
        or _private_entry_signature(current) != signature
        or current.st_nlink != 1
    ):
        raise ValueError(error)
    if _descriptor_digest(descriptor, signature[2], error) != digest:
        raise ValueError(error)
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        raise ValueError(error) from None
    if (
        _private_entry_signature(os.fstat(descriptor)) != signature
        or _private_entry_signature(current) != signature
    ):
        raise ValueError(error)


def _validate_open_backup(descriptor, signature, digest, error):
    opened = os.fstat(descriptor)
    opened_signature = _private_entry_signature(opened)
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink not in {0, 1}
        or opened_signature[:4] != signature[:4]
        or _descriptor_digest(descriptor, signature[2], error) != digest
    ):
        raise ValueError(error)
    after = os.fstat(descriptor)
    if (
        _private_entry_signature(after) != opened_signature
        or after.st_nlink != opened.st_nlink
    ):
        raise ValueError(error)


def _restore_backup_from_descriptor(
    parent_descriptor,
    canonical_name,
    backup_descriptor,
    backup_signature,
    backup_digest,
    writer_identity,
    existing_signature,
    error,
    sync_file,
    sync_parent,
):
    _validate_open_backup(
        backup_descriptor,
        backup_signature,
        backup_digest,
        error,
    )
    restore_name = f".{canonical_name}.{secrets.token_hex(12)}.restore"
    restore_descriptor = -1
    restore_identity = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        restore_descriptor = os.open(
            restore_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(restore_descriptor)
        restore_identity = (opened.st_dev, opened.st_ino)
        os.fchmod(restore_descriptor, 0o600)
        os.lseek(backup_descriptor, 0, os.SEEK_SET)
        remaining = backup_signature[2]
        while remaining:
            chunk = os.read(backup_descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError(error)
            _write_all(restore_descriptor, chunk)
            remaining -= len(chunk)
        if os.read(backup_descriptor, 1):
            raise ValueError(error)
        sync_file(restore_descriptor)
        restored = os.fstat(restore_descriptor)
        current = _private_entry_stat(parent_descriptor, restore_name)
        if (
            not stat.S_ISREG(restored.st_mode)
            or restored.st_nlink != 1
            or stat.S_IMODE(restored.st_mode) != 0o600
            or (restored.st_dev, restored.st_ino) != restore_identity
            or (current.st_dev, current.st_ino) != restore_identity
            or _descriptor_digest(
                restore_descriptor,
                backup_signature[2],
                error,
            )
            != backup_digest
        ):
            raise ValueError(error)
        _validate_open_backup(
            backup_descriptor,
            backup_signature,
            backup_digest,
            error,
        )
        if _canonical_state(
            parent_descriptor,
            canonical_name,
            writer_identity,
            existing_signature,
        ) not in {"writer", "missing"}:
            raise ValueError(error)
        os.replace(
            restore_name,
            canonical_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        restored = _private_entry_stat(parent_descriptor, canonical_name)
        if (
            restored.st_dev,
            restored.st_ino,
        ) != restore_identity or restored.st_nlink != 1:
            raise ValueError(error)
        sync_parent(parent_descriptor)
    finally:
        if restore_descriptor >= 0:
            os.close(restore_descriptor)
        if restore_identity is not None:
            try:
                _remove_owned_entry(
                    parent_descriptor,
                    restore_name,
                    restore_identity,
                )
            except OSError:
                pass


def _canonical_state(parent_descriptor, name, identity, existing_signature):
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        return "missing"
    if (current.st_dev, current.st_ino) == identity:
        return "writer"
    if (
        existing_signature is not None
        and stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and _private_entry_signature(current) == existing_signature
    ):
        return "original"
    return "unknown"


def _require_current_private_parent(
    path,
    parent_descriptor,
    *,
    trusted_root,
    trusted_root_identity,
):
    current_descriptor = -1
    try:
        try:
            _, current_descriptor = _open_private_parent(
                path,
                trusted_root=trusted_root,
                trusted_root_identity=trusted_root_identity,
            )
        except FileNotFoundError as exc:
            raise ValueError("private root changed") from exc
        opened = os.fstat(parent_descriptor)
        current = os.fstat(current_descriptor)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("private root changed")
    finally:
        if current_descriptor >= 0:
            os.close(current_descriptor)


@dataclass
class _PrivateAtomicWriteState:
    path: Path
    data: bytes
    parent_descriptor: int
    trusted_root: object
    trusted_root_identity: object
    error: str
    sync_file: object
    sync_parent: object
    max_existing_bytes: object
    require_absent: bool
    temp_name: str
    descriptor: int = -1
    backup_descriptor: int = -1
    identity: object = None
    backup_name: object = None
    backup_identity: object = None
    backup_signature: object = None
    backup_digest: object = None
    existing: object = None
    existing_signature: object = None
    backup_preserved: bool = False
    preserve_new: bool = False
    replace_started: bool = False
    committed: bool = False


def _begin_private_atomic_write(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
    error,
    fsync_file,
    fsync_parent,
    max_existing_bytes,
    require_absent,
):
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    return _PrivateAtomicWriteState(
        path=path,
        data=bytes(data),
        parent_descriptor=parent_descriptor,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        error=error,
        sync_file=fsync_file or os.fsync,
        sync_parent=fsync_parent or os.fsync,
        max_existing_bytes=max_existing_bytes,
        require_absent=bool(require_absent),
        temp_name=f".{path.name}.{secrets.token_hex(12)}.tmp",
    )


def _inspect_existing_private_entry(state):
    try:
        existing = _private_entry_stat(state.parent_descriptor, state.path.name)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
    ):
        raise ValueError(state.error)
    if state.require_absent and existing is not None:
        raise ValueError(state.error)
    if (
        existing is not None
        and state.max_existing_bytes is not None
        and existing.st_size > int(state.max_existing_bytes)
    ):
        raise ValueError("private file too large")
    state.existing = existing
    state.existing_signature = (
        _private_entry_signature(existing) if existing is not None else None
    )


def _require_valid_private_temp(state):
    opened = os.fstat(state.descriptor)
    current = _private_entry_stat(state.parent_descriptor, state.temp_name)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != state.identity
        or (current.st_dev, current.st_ino) != state.identity
        or current.st_nlink != 1
    ):
        raise ValueError(state.error)


def _create_private_temp(state):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    state.descriptor = os.open(
        state.temp_name,
        flags,
        0o600,
        dir_fd=state.parent_descriptor,
    )
    opened = os.fstat(state.descriptor)
    state.identity = (opened.st_dev, opened.st_ino)
    os.fchmod(state.descriptor, 0o600)
    _write_all(state.descriptor, state.data)
    state.sync_file(state.descriptor)
    _require_valid_private_temp(state)


def _open_validated_private_source(state):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(
        state.path.name,
        flags,
        dir_fd=state.parent_descriptor,
    )
    opened = os.fstat(descriptor)
    current = _private_entry_stat(state.parent_descriptor, state.path.name)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or _private_entry_signature(opened) != state.existing_signature
        or _private_entry_signature(current) != state.existing_signature
    ):
        os.close(descriptor)
        raise ValueError(state.error)
    return descriptor


def _copy_private_backup(state, source_descriptor):
    state.backup_name = f".{state.path.name}.{secrets.token_hex(12)}.bak"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    state.backup_descriptor = os.open(
        state.backup_name,
        flags,
        0o600,
        dir_fd=state.parent_descriptor,
    )
    backup_opened = os.fstat(state.backup_descriptor)
    state.backup_identity = (backup_opened.st_dev, backup_opened.st_ino)
    os.fchmod(state.backup_descriptor, 0o600)

    source_opened = os.fstat(source_descriptor)
    source_digest = hashlib.sha256()
    remaining = source_opened.st_size
    while remaining:
        chunk = os.read(source_descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise ValueError(state.error)
        source_digest.update(chunk)
        _write_all(state.backup_descriptor, chunk)
        remaining -= len(chunk)
    if os.read(source_descriptor, 1):
        raise ValueError(state.error)
    state.sync_file(state.backup_descriptor)
    state.backup_digest = source_digest.digest()


def _validate_private_backup_copy(state, source_descriptor):
    source_opened = os.fstat(source_descriptor)
    source_current = _private_entry_stat(state.parent_descriptor, state.path.name)
    backup_opened = os.fstat(state.backup_descriptor)
    backup_current = _private_entry_stat(state.parent_descriptor, state.backup_name)
    state.backup_signature = _private_entry_signature(backup_opened)
    if (
        _private_entry_signature(source_opened) != state.existing_signature
        or _private_entry_signature(source_current) != state.existing_signature
        or not stat.S_ISREG(backup_opened.st_mode)
        or backup_opened.st_nlink != 1
        or stat.S_IMODE(backup_opened.st_mode) != 0o600
        or backup_opened.st_size != source_opened.st_size
        or (backup_opened.st_dev, backup_opened.st_ino) != state.backup_identity
        or (backup_current.st_dev, backup_current.st_ino) != state.backup_identity
        or backup_current.st_nlink != 1
        or _private_entry_signature(backup_current) != state.backup_signature
    ):
        raise ValueError(state.error)
    _validate_private_backup(
        state.parent_descriptor,
        state.backup_name,
        state.backup_descriptor,
        state.backup_signature,
        state.backup_digest,
        state.error,
    )


def _backup_existing_private_entry(state):
    if state.existing is None:
        return
    source_descriptor = _open_validated_private_source(state)
    try:
        _copy_private_backup(state, source_descriptor)
        _validate_private_backup_copy(state, source_descriptor)
    finally:
        os.close(source_descriptor)
    state.sync_parent(state.parent_descriptor)


def _require_current_private_write_parent(state):
    _require_current_private_parent(
        state.path,
        state.parent_descriptor,
        trusted_root=state.trusted_root,
        trusted_root_identity=state.trusted_root_identity,
    )


def _require_unchanged_private_target(state):
    _require_current_private_write_parent(state)
    _require_valid_private_temp(state)
    try:
        current = _private_entry_stat(state.parent_descriptor, state.path.name)
    except FileNotFoundError:
        current = None
    target_created = state.existing_signature is None and current is not None
    target_changed = state.existing_signature is not None and (
        current is None
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or _private_entry_signature(current) != state.existing_signature
    )
    if target_created or target_changed:
        raise ValueError(state.error)


def _install_private_temp(state):
    state.replace_started = True
    if state.require_absent:
        try:
            os.link(
                state.temp_name,
                state.path.name,
                src_dir_fd=state.parent_descriptor,
                dst_dir_fd=state.parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ValueError(state.error) from exc
        linked = _private_entry_stat(state.parent_descriptor, state.path.name)
        temp = _private_entry_stat(state.parent_descriptor, state.temp_name)
        opened = os.fstat(state.descriptor)
        if (
            (linked.st_dev, linked.st_ino) != state.identity
            or (temp.st_dev, temp.st_ino) != state.identity
            or (opened.st_dev, opened.st_ino) != state.identity
            or linked.st_nlink != 2
            or temp.st_nlink != 2
            or opened.st_nlink != 2
        ):
            raise ValueError(state.error)
        os.unlink(state.temp_name, dir_fd=state.parent_descriptor)
    else:
        os.replace(
            state.temp_name,
            state.path.name,
            src_dir_fd=state.parent_descriptor,
            dst_dir_fd=state.parent_descriptor,
        )
    current = _private_entry_stat(state.parent_descriptor, state.path.name)
    opened = os.fstat(state.descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or stat.S_IMODE(current.st_mode) != 0o600
        or (current.st_dev, current.st_ino) != state.identity
        or opened.st_nlink != 1
    ):
        raise ValueError(state.error)
    _require_current_private_write_parent(state)
    state.sync_parent(state.parent_descriptor)
    _require_current_private_write_parent(state)
    state.committed = True


def _restore_private_backup(state):
    _validate_private_backup(
        state.parent_descriptor,
        state.backup_name,
        state.backup_descriptor,
        state.backup_signature,
        state.backup_digest,
        state.error,
    )
    canonical_state = _canonical_state(
        state.parent_descriptor,
        state.path.name,
        state.identity,
        state.existing_signature,
    )
    if canonical_state == "unknown":
        raise ValueError(state.error)
    if canonical_state == "original":
        return
    os.replace(
        state.backup_name,
        state.path.name,
        src_dir_fd=state.parent_descriptor,
        dst_dir_fd=state.parent_descriptor,
    )
    state.backup_name = None
    restored = _private_entry_stat(state.parent_descriptor, state.path.name)
    if (
        not stat.S_ISREG(restored.st_mode)
        or restored.st_nlink != 1
        or stat.S_IMODE(restored.st_mode) != 0o600
        or (restored.st_dev, restored.st_ino) != state.backup_identity
        or _descriptor_digest(
            state.backup_descriptor,
            state.backup_signature[2],
            state.error,
        )
        != state.backup_digest
    ):
        raise ValueError(state.error)
    state.sync_parent(state.parent_descriptor)


def _rollback_private_write(state, primary):
    if not state.replace_started:
        return
    try:
        canonical_state = _canonical_state(
            state.parent_descriptor,
            state.path.name,
            state.identity,
            state.existing_signature,
        )
        if canonical_state == "unknown":
            state.backup_preserved = state.backup_name is not None
            raise ValueError(state.error)
        if state.existing_signature is None:
            if canonical_state == "writer":
                if not _remove_owned_entry(
                    state.parent_descriptor,
                    state.path.name,
                    state.identity,
                ):
                    raise ValueError(state.error)
                state.sync_parent(state.parent_descriptor)
        elif canonical_state != "original":
            try:
                _restore_private_backup(state)
            except BaseException:
                state.backup_preserved = state.backup_name is not None
                state.preserve_new = state.backup_name is not None
                raise
        if state.descriptor >= 0 and state.identity is not None:
            os.ftruncate(state.descriptor, 0)
            os.fsync(state.descriptor)
    except BaseException as rollback_error:
        state.backup_preserved = state.backup_preserved or state.backup_name is not None
        raise rollback_error from primary


def _wipe_descriptor(descriptor):
    try:
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
    except OSError:
        pass


def _cleanup_private_temp(state):
    if (
        state.descriptor >= 0
        and state.identity is not None
        and not state.committed
        and not state.preserve_new
    ):
        _wipe_descriptor(state.descriptor)
    if state.identity is not None and not state.committed and not state.preserve_new:
        try:
            _remove_owned_entry(
                state.parent_descriptor,
                state.temp_name,
                state.identity,
            )
        except OSError:
            pass


def _remove_committed_private_backup(state):
    current = _private_entry_stat(state.parent_descriptor, state.backup_name)
    if (current.st_dev, current.st_ino) != state.backup_identity:
        raise ValueError(state.error)
    os.unlink(state.backup_name, dir_fd=state.parent_descriptor)
    state.sync_parent(state.parent_descriptor)


def _restore_after_backup_cleanup_failure(state):
    _restore_backup_from_descriptor(
        state.parent_descriptor,
        state.path.name,
        state.backup_descriptor,
        state.backup_signature,
        state.backup_digest,
        state.identity,
        state.existing_signature,
        state.error,
        state.sync_file,
        state.sync_parent,
    )
    state.committed = False
    _wipe_descriptor(state.descriptor)


def _discard_uncommitted_private_backup(state):
    try:
        _wipe_descriptor(state.backup_descriptor)
        if _remove_owned_entry(
            state.parent_descriptor,
            state.backup_name,
            state.backup_identity,
        ):
            state.sync_parent(state.parent_descriptor)
    except (OSError, ValueError):
        pass


def _cleanup_private_backup(state):
    if (
        state.backup_descriptor < 0
        or state.backup_identity is None
        or state.backup_name is None
        or state.backup_preserved
    ):
        return None, None, None
    if not state.committed:
        _discard_uncommitted_private_backup(state)
        return None, None, None

    cleanup_error = None
    try:
        _remove_committed_private_backup(state)
    except BaseException as exc:
        cleanup_error = exc
    else:
        _wipe_descriptor(state.backup_descriptor)
        return None, None, None

    try:
        _restore_after_backup_cleanup_failure(state)
    except BaseException as rollback_error:
        return cleanup_error, PrivateAtomicWriteError(state.error), rollback_error
    return cleanup_error, None, None


def _close_private_write(state):
    if state.descriptor >= 0:
        os.close(state.descriptor)
    if state.backup_descriptor >= 0:
        os.close(state.backup_descriptor)
    os.close(state.parent_descriptor)


def _cleanup_private_write(state):
    _cleanup_private_temp(state)
    cleanup_error, committed_error, committed_cause = _cleanup_private_backup(state)
    _close_private_write(state)
    if committed_error is not None:
        raise committed_error from committed_cause
    if cleanup_error is not None:
        raise cleanup_error


def write_private_bytes_atomic(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
    error="private temp changed",
    fsync_file=None,
    fsync_parent=None,
    max_existing_bytes=None,
    require_absent=False,
    validate_commit=None,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("private atomic write requires bytes")
    state = _begin_private_atomic_write(
        path,
        data,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        error=error,
        fsync_file=fsync_file,
        fsync_parent=fsync_parent,
        max_existing_bytes=max_existing_bytes,
        require_absent=require_absent,
    )
    try:
        _inspect_existing_private_entry(state)
        _create_private_temp(state)
        _require_current_private_write_parent(state)
        _backup_existing_private_entry(state)
        _require_unchanged_private_target(state)
        if validate_commit is not None:
            validate_commit()
        _install_private_temp(state)
        if validate_commit is not None:
            validate_commit()
    except BaseException as primary:
        _rollback_private_write(state, primary)
        raise
    finally:
        _cleanup_private_write(state)
    return state.path


def append_private_bytes(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
    max_total_bytes=None,
    expected_identity=None,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("private append requires bytes")
    if max_total_bytes is not None and len(data) > int(max_total_bytes):
        raise ValueError("private file too large")
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    descriptor = -1
    original_size = None
    write_started = False
    completed = False
    try:
        try:
            before = _private_entry_stat(parent_descriptor, path.name)
        except FileNotFoundError:
            before = None
        if expected_identity is not None and (
            before is None or (before.st_dev, before.st_ino) != tuple(expected_identity)
        ):
            raise ValueError("private file changed")
        if before is not None:
            if stat.S_ISLNK(before.st_mode):
                raise ValueError("refusing symlink component")
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("private file has multiple links")
        flags = os.O_APPEND | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_CREAT | (os.O_EXCL if before is None else 0)
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        original_size = opened.st_size
        current = _private_entry_stat(parent_descriptor, path.name)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
            or before is not None
            and (before.st_dev, before.st_ino) != identity
        ):
            raise ValueError("private file changed")
        if max_total_bytes is not None and opened.st_size + len(data) > int(
            max_total_bytes
        ):
            raise ValueError("private file too large")
        os.fchmod(descriptor, 0o600)
        write_started = True
        _write_all(descriptor, bytes(data))
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = _private_entry_stat(parent_descriptor, path.name)
        if (
            after.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
            or current.st_nlink != 1
        ):
            raise ValueError("private file changed")
        os.fsync(parent_descriptor)
        completed = True
        return path
    finally:
        if (
            descriptor >= 0
            and original_size is not None
            and write_started
            and not completed
        ):
            try:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def harden_private_tree(path):
    """Repair modes below one application-owned tree without following links."""
    root = ensure_private_dir(path)
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                child = Path(entry.path)
                if stat.S_ISDIR(mode):
                    pending.append(ensure_private_dir(child))
                elif stat.S_ISREG(mode):
                    ensure_private_file(child)
                else:
                    raise ValueError("private tree has unsafe entry")
    return root
