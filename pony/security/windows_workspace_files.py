"""Native Windows implementation of anchored workspace file I/O."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

from . import windows_native as native
from .workspace_files import (
    WorkspaceIOError,
    _missing_workspace_file,
    _workspace_relative_parts,
)


_FILE_MODE = stat.S_IFREG
_DIRECTORY_MODE = stat.S_IFDIR
_MISSING_ERRORS = {2, 3}


@dataclass
class _DirectoryChain:
    path: Path
    handles: list
    root_index: int

    @property
    def handle(self):
        return self.handles[-1]

    @property
    def root(self):
        return self.handles[self.root_index]

    def close(self):
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()


@dataclass
class _Target:
    handle: object | None
    signature: tuple | None
    digest: str | None


@dataclass
class _Temp:
    handle: object
    path: Path
    identity: tuple


def _is_missing(exc):
    return native.error_code(exc) in _MISSING_ERRORS


def _facts_signature(value):
    return (
        value.filesystem_id,
        value.file_id,
        value.link_count,
        value.size,
        value.modified_ns,
        value.changed_ns,
    )


def _file_limit_error(value):
    error = WorkspaceIOError(
        "workspace_file_limit_exceeded",
        "workspace file exceeds the configured limit",
    )
    error.state = {
        "exists": True,
        "data": None,
        "mode": _FILE_MODE,
        "size": value.size,
        "modified_ns": value.modified_ns,
        "changed_ns": value.changed_ns,
        "sha256": "",
        "identity": (value.filesystem_id, value.file_id),
    }
    return error


def _open_directory_chain(
    workspace_root,
    parts,
    *,
    expected_root_identity,
    create=False,
):
    root = native.lexical_absolute(workspace_root)
    handles = []
    root_index = len(root.parts) - 1
    path = Path(root.anchor)
    try:
        handles.append(
            native.open_path(
                path,
                directory=True,
                share_access=native.FILE_SHARE_READ_WRITE,
            )
        )
        components = [*root.parts[1:], *parts]
        for index, component in enumerate(components):
            path /= component
            in_workspace_parent = index >= len(root.parts) - 1
            try:
                child, _created = native.open_relative(
                    handles[-1],
                    component,
                    directory=True,
                    desired_access=native.FILE_DIRECTORY_ACCESS,
                    share_access=native.FILE_SHARE_READ_WRITE,
                )
            except OSError as exc:
                if not in_workspace_parent or not _is_missing(exc):
                    raise
                if not create:
                    for handle in reversed(handles):
                        handle.close()
                    raise FileNotFoundError(2, "workspace directory missing") from None
                try:
                    child, _created = native.open_relative(
                        handles[-1],
                        component,
                        directory=True,
                        desired_access=native.FILE_DIRECTORY_ACCESS,
                        disposition=native.FILE_CREATE,
                        share_access=native.FILE_SHARE_READ_WRITE,
                    )
                except OSError as create_exc:
                    if native.error_code(create_exc) not in {80, 183}:
                        raise WorkspaceIOError(
                            "workspace_entry_unsafe",
                            "workspace parent could not be created safely",
                        ) from create_exc
                    try:
                        child, _created = native.open_relative(
                            handles[-1],
                            component,
                            directory=True,
                            desired_access=native.FILE_DIRECTORY_ACCESS,
                            share_access=native.FILE_SHARE_READ_WRITE,
                        )
                    except Exception as reopen_exc:
                        raise WorkspaceIOError(
                            "workspace_entry_unsafe",
                            "workspace parent could not be created safely",
                        ) from reopen_exc
            handles.append(child)
        chain = _DirectoryChain(path, handles, root_index)
        if expected_root_identity is not None and native.identity(chain.root) != tuple(
            expected_root_identity
        ):
            raise WorkspaceIOError("workspace_entry_unsafe", "workspace root changed")
        return chain
    except FileNotFoundError:
        for handle in reversed(handles):
            handle.close()
        raise
    except WorkspaceIOError:
        for handle in reversed(handles):
            handle.close()
        raise
    except (OSError, ValueError) as exc:
        for handle in reversed(handles):
            handle.close()
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "workspace root or parent is unsafe",
        ) from exc


def _open_leaf(parent, name, *, access=native.FILE_READ_ACCESS):
    try:
        handle, _created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=access,
            share_access=native.FILE_SHARE_ALL,
        )
        return handle
    except OSError as exc:
        if _is_missing(exc):
            raise
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "path is not a stable regular file",
        ) from exc
    except ValueError as exc:
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "path is not a stable regular file",
        ) from exc


def _read_digest(handle, size, *, code):
    try:
        data = native.read_bytes(handle, max_bytes=size)
    except ValueError as exc:
        raise WorkspaceIOError(code, "workspace file changed while hashing") from exc
    if len(data) != size:
        raise WorkspaceIOError(code, "workspace file changed while hashing")
    return hashlib.sha256(data).hexdigest()


def read_regular_bytes_anchored(
    workspace_root,
    raw_path,
    *,
    max_bytes,
    expected_root_identity=None,
):
    parts = _workspace_relative_parts(raw_path)
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("invalid workspace file limit")
    try:
        chain = _open_directory_chain(
            workspace_root,
            parts[:-1],
            expected_root_identity=expected_root_identity,
        )
    except FileNotFoundError:
        return _missing_workspace_file()
    handle = None
    try:
        try:
            handle = _open_leaf(chain.handle, parts[-1])
        except OSError as exc:
            if _is_missing(exc):
                return _missing_workspace_file()
            raise
        opened = native.facts(handle)
        if opened.size > limit:
            raise _file_limit_error(opened)
        try:
            data = native.read_bytes(handle, max_bytes=limit)
        except ValueError as exc:
            changed = native.facts(handle)
            if changed.size > limit:
                raise _file_limit_error(changed) from exc
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace file changed while it was read",
            ) from exc
        after = native.facts(handle)
        if _facts_signature(after) != _facts_signature(opened):
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace file changed while it was read",
            )
        current = _open_leaf(chain.handle, parts[-1])
        try:
            if _facts_signature(native.facts(current)) != _facts_signature(opened):
                raise WorkspaceIOError(
                    "workspace_entry_unsafe",
                    "workspace file changed while it was read",
                )
        finally:
            current.close()
        return {
            "exists": True,
            "data": data,
            "mode": _FILE_MODE,
            "size": opened.size,
            "modified_ns": opened.modified_ns,
            "changed_ns": opened.changed_ns,
            "sha256": hashlib.sha256(data).hexdigest(),
            "identity": native.identity(handle),
        }
    finally:
        if handle is not None:
            handle.close()
        chain.close()


def list_directory_names_anchored(
    workspace_root,
    raw_path=".",
    *,
    max_entries,
    expected_root_identity=None,
):
    parts = _workspace_relative_parts(raw_path, allow_root=True)
    limit = int(max_entries)
    if limit < 1:
        raise ValueError("invalid workspace directory limit")
    chain = _open_directory_chain(
        workspace_root,
        parts,
        expected_root_identity=expected_root_identity,
    )
    try:
        opened_identity = native.identity(chain.handle)
        entries = []
        unsafe_count = 0
        scanned = 0
        with os.scandir(native.win32_path(chain.path)) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > limit:
                    raise WorkspaceIOError(
                        "workspace_directory_limit_exceeded",
                        "workspace directory scan limit exceeded",
                    )
                handle = None
                try:
                    name = native.lexical_component(entry.name)
                    directory = entry.is_dir(follow_symlinks=False)
                    handle, _created = native.open_relative(
                        chain.handle,
                        name,
                        directory=directory,
                        desired_access=native.FILE_DIRECTORY_ACCESS
                        if directory
                        else native.FILE_READ_ACCESS,
                        share_access=native.FILE_SHARE_READ_WRITE,
                    )
                    value = native.facts(handle)
                    if value.directory != directory or value.reparse_tag:
                        raise ValueError("workspace entry changed")
                    entries.append(
                        {
                            "name": name,
                            "mode": _DIRECTORY_MODE if directory else _FILE_MODE,
                            "size": value.size,
                            "modified_ns": value.modified_ns,
                            "changed_ns": value.changed_ns,
                            "identity": (value.filesystem_id, value.file_id),
                        }
                    )
                except (OSError, ValueError):
                    unsafe_count += 1
                finally:
                    if handle is not None:
                        handle.close()
        entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        return {
            "entries": tuple(entries),
            "unsafe_count": unsafe_count,
            "scanned": scanned,
            "identity": opened_identity,
        }
    finally:
        chain.close()


def _validate_write(data, max_bytes, expected_sha256):
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
    return rendered, limit


def _inspect_target(parent, name, *, expected_sha256, limit):
    try:
        handle = _open_leaf(parent, name)
    except OSError as exc:
        if not _is_missing(exc):
            raise
        if expected_sha256 is not None:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file disappeared before write",
            )
        return _Target(None, None, None)
    try:
        value = native.facts(handle)
        if expected_sha256 is not None and value.size > limit:
            raise _file_limit_error(value)
        signature = _facts_signature(value)
        digest = None
        if expected_sha256 is not None:
            digest = _read_digest(
                handle,
                value.size,
                code="workspace_changed_during_write",
            )
            if digest != expected_sha256:
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file content changed before write",
                )
        return _Target(handle, signature, digest)
    except BaseException:
        handle.close()
        raise


def _create_temp(parent, path, data, sync_file):
    name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    temp_path = path.with_name(name)
    handle = None
    created = False
    try:
        handle, created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_REPLACE_ACCESS,
            disposition=native.FILE_CREATE,
            share_access=native.FILE_SHARE_ALL,
        )
        if not created:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace temporary file changed",
            )
        native.write_bytes(handle, data)
        if sync_file is not None:
            sync_file(handle.value)
        value = native.facts(handle)
        identity = native.identity(handle)
        if value.size != len(data) or value.link_count != 1:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace temporary file changed",
            )
        return _Temp(handle, temp_path, identity)
    except BaseException:
        if handle is not None:
            if created:
                try:
                    native.delete_handle(handle)
                except OSError:
                    pass
            handle.close()
        raise


def _identity_at(parent, name):
    try:
        handle = _open_leaf(parent, name)
    except OSError as exc:
        if _is_missing(exc):
            return None
        raise
    try:
        return native.identity(handle)
    finally:
        handle.close()


def _same_target(parent, name, signature):
    try:
        current = _open_leaf(parent, name)
    except OSError as exc:
        if _is_missing(exc):
            current = None
        else:
            raise
    if signature is None:
        if current is not None:
            current.close()
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file appeared during write",
            )
        return
    if current is None:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace file disappeared during write",
        )
    try:
        if _facts_signature(native.facts(current)) != signature:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace file identity changed during write",
            )
    finally:
        current.close()


def _revalidate_target(parent, name, target):
    _same_target(parent, name, target.signature)
    if target.handle is None:
        return
    value = native.facts(target.handle)
    if _facts_signature(value) != target.signature:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace file changed during write",
        )
    if target.digest is not None and _read_digest(
        target.handle,
        value.size,
        code="workspace_changed_during_write",
    ) != target.digest:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace file content changed during write",
        )


def _copy_restore(parent, path, target):
    name = f".{path.name}.{secrets.token_hex(12)}.restore"
    restore_path = path.with_name(name)
    handle = None
    created = False
    try:
        handle, created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_REPLACE_ACCESS,
            disposition=native.FILE_CREATE,
            share_access=native.FILE_SHARE_ALL,
        )
        if not created:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace restore file changed",
            )
        identity = native.identity(handle)
        digest = hashlib.sha256()
        total = 0
        expected_size = target.signature[3]
        for chunk in native.read_chunks(target.handle):
            total += len(chunk)
            if total > expected_size:
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file changed while preparing rollback",
                )
            native.write_bytes(handle, chunk, append=total != len(chunk))
            digest.update(chunk)
        if total == 0:
            native.write_bytes(handle, b"")
        value = native.facts(handle)
        if (
            total != expected_size
            or value.size != expected_size
            or value.link_count != 1
            or native.identity(handle) != identity
        ):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace restore file changed",
            )
        target.digest = digest.hexdigest()
        if _read_digest(
            handle,
            expected_size,
            code="workspace_changed_during_write",
        ) != target.digest:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace restore file content changed",
            )
        return _Temp(handle, restore_path, identity)
    except BaseException:
        if handle is not None:
            if created:
                try:
                    native.delete_handle(handle)
                except OSError:
                    pass
            handle.close()
        raise


def _verify_owned(parent, owned, size, digest, message):
    if owned is None or owned.handle is None:
        raise WorkspaceIOError("workspace_changed_during_write", message)
    value = native.facts(owned.handle)
    if (
        native.identity(owned.handle) != owned.identity
        or value.link_count != 1
        or value.size != size
        or _identity_at(parent, owned.path.name) != owned.identity
        or _read_digest(
            owned.handle,
            size,
            code="workspace_changed_during_write",
        )
        != digest
    ):
        raise WorkspaceIOError("workspace_changed_during_write", message)


def _open_owned(parent, owned, message):
    handle = _open_leaf(parent, owned.path.name, access=native.FILE_REPLACE_ACCESS)
    try:
        if native.identity(handle) != owned.identity:
            raise WorkspaceIOError("workspace_changed_during_write", message)
        owned.handle = handle
    except BaseException:
        handle.close()
        raise


def _installed(parent, name, identity, size, digest):
    handle = _open_leaf(parent, name)
    try:
        value = native.facts(handle)
        if (
            native.identity(handle) != identity
            or value.link_count != 1
            or value.size != size
            or _read_digest(
                handle,
                size,
                code="workspace_changed_during_write",
            )
            != digest
        ):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace replace result changed",
            )
    finally:
        handle.close()


def _rollback_atomic(
    parent,
    path,
    temp,
    target,
    restore,
    rendered_size,
    rendered_digest,
):
    try:
        _installed(
            parent,
            path.name,
            temp.identity,
            rendered_size,
            rendered_digest,
        )
        if target.signature is None:
            native.delete_handle(temp.handle)
            temp.handle.close()
            temp.handle = None
            if _identity_at(parent, path.name) is not None:
                raise ValueError("workspace rollback did not remove installed file")
            return False

        _verify_owned(
            parent,
            restore,
            target.signature[3],
            target.digest,
            "workspace restore file changed",
        )
        temp.handle.close()
        temp.handle = None
        try:
            native.rename_handle(
                restore.handle,
                parent,
                path.name,
                replace=True,
            )
        except OSError:
            _installed(
                parent,
                path.name,
                restore.identity,
                target.signature[3],
                target.digest,
            )
        else:
            _installed(
                parent,
                path.name,
                restore.identity,
                target.signature[3],
                target.digest,
            )
        if _identity_at(parent, restore.path.name) is not None:
            raise ValueError("workspace restore binding remained after rollback")
        return True
    except Exception as exc:
        raise WorkspaceIOError(
            "workspace_changed_during_write",
            "workspace atomic rollback failed",
        ) from exc


def write_regular_bytes_anchored_atomic(
    workspace_root,
    raw_path,
    data,
    *,
    max_bytes,
    expected_sha256=None,
    expected_root_identity=None,
    fsync_file=None,
    fsync_parent=None,
):
    rendered, limit = _validate_write(data, max_bytes, expected_sha256)
    rendered_digest = hashlib.sha256(rendered).hexdigest()
    parts = _workspace_relative_parts(raw_path)
    chain = _open_directory_chain(
        workspace_root,
        parts[:-1],
        expected_root_identity=expected_root_identity,
        create=True,
    )
    path = chain.path / parts[-1]
    target = None
    temp = None
    restore = None
    installed = False
    committed = False
    restore_installed = False
    preserve_restore = False
    try:
        target = _inspect_target(
            chain.handle,
            path.name,
            expected_sha256=expected_sha256,
            limit=limit,
        )
        temp = _create_temp(chain.handle, path, rendered, fsync_file)
        if target.signature is not None:
            restore = _copy_restore(chain.handle, path, target)
        _revalidate_target(chain.handle, path.name, target)
        _verify_owned(
            chain.handle,
            temp,
            len(rendered),
            rendered_digest,
            "workspace temporary file changed",
        )
        if restore is not None:
            _verify_owned(
                chain.handle,
                restore,
                target.signature[3],
                target.digest,
                "workspace restore file changed",
            )
        if target.handle is not None:
            target.handle.close()
            target.handle = None
        try:
            native.rename_handle(
                temp.handle,
                chain.handle,
                path.name,
                replace=target.signature is not None,
            )
        except OSError:
            if _identity_at(chain.handle, path.name) == temp.identity:
                installed = True
            else:
                try:
                    _verify_owned(
                        chain.handle,
                        temp,
                        len(rendered),
                        rendered_digest,
                        "workspace temporary file changed",
                    )
                    _same_target(chain.handle, path.name, target.signature)
                except Exception:
                    preserve_restore = restore is not None
                    raise WorkspaceIOError(
                        "workspace_changed_during_write",
                        "workspace replace result is ambiguous",
                    ) from None
            raise
        installed = True
        _installed(
            chain.handle,
            path.name,
            temp.identity,
            len(rendered),
            rendered_digest,
        )
        if _identity_at(chain.handle, temp.path.name) is not None:
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace temporary binding remained after replace",
            )
        if restore is not None:
            _verify_owned(
                chain.handle,
                restore,
                target.signature[3],
                target.digest,
                "workspace restore file changed",
            )
        if fsync_parent is not None:
            fsync_parent(chain.handle.value)
        if restore is not None:
            try:
                native.delete_handle(restore.handle)
            except BaseException as cleanup_error:
                restore.handle.close()
                restore.handle = None
                current = _identity_at(chain.handle, restore.path.name)
                if current is not None:
                    if current != restore.identity:
                        preserve_restore = True
                        raise WorkspaceIOError(
                            "workspace_changed_during_write",
                            "workspace restore file changed",
                        ) from cleanup_error
                    _open_owned(
                        chain.handle,
                        restore,
                        "workspace restore file changed",
                    )
                    raise cleanup_error
            else:
                restore.handle.close()
                restore.handle = None
                if _identity_at(chain.handle, restore.path.name) is not None:
                    raise WorkspaceIOError(
                        "workspace_changed_during_write",
                        "workspace restore cleanup failed",
                    )
        committed = True
        return {
            "mode": _FILE_MODE,
            "sha256": rendered_digest,
            "created": target.signature is None,
        }
    except BaseException:
        if installed and not committed:
            try:
                restore_installed = _rollback_atomic(
                    chain.handle,
                    path,
                    temp,
                    target,
                    restore,
                    len(rendered),
                    rendered_digest,
                )
            except Exception:
                preserve_restore = restore is not None
                raise
            installed = False
        raise
    finally:
        if target is not None and target.handle is not None:
            target.handle.close()
        if temp is not None and temp.handle is not None:
            if not committed and not installed:
                try:
                    native.delete_handle(temp.handle)
                except OSError:
                    pass
            temp.handle.close()
        if restore is not None and restore.handle is not None:
            if (
                not committed
                and not installed
                and not restore_installed
                and not preserve_restore
            ):
                try:
                    native.delete_handle(restore.handle)
                except OSError:
                    pass
            restore.handle.close()
        chain.close()
