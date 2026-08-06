"""Windows implementation of Pony's private-state file contracts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets

from . import windows_native as native


class AtomicWriteAmbiguous(RuntimeError):
    """A Windows atomic write could not prove rollback or commit."""


def ensure_private_dir(path):
    return native.ensure_directory(path)


def _open_file(
    path,
    *,
    trusted_root=None,
    trusted_root_identity=None,
    access=native.FILE_READ_ACCESS,
):
    path, parent = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        handle, _created = native.open_relative(
            parent,
            path.name,
            directory=False,
            desired_access=access,
        )
        return path, parent, handle
    except Exception:
        parent.close()
        raise


def private_directory_identity(path, identity_type):
    with native.open_path(path, directory=True) as handle:
        return identity_type(*native.identity(handle))


def _harden_private(handle):
    try:
        native.require_private(handle)
    except ValueError:
        native.make_private(handle)
        native.require_private(handle)


def ensure_private_file(path, *, trusted_root=None, trusted_root_identity=None):
    path, parent, handle = _open_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        access=native.FILE_ALL_ACCESS,
    )
    try:
        _harden_private(handle)
        return path
    finally:
        handle.close()
        parent.close()


def private_file_signature(
    path,
    signature_type,
    *,
    trusted_root=None,
    trusted_root_identity=None,
):
    _path, parent, handle = _open_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        value = native.require_kind(handle, directory=False)
        protection = native.protection_identity(handle)
        try:
            native.require_private(handle)
        except ValueError:
            is_private = False
        else:
            is_private = True
        return signature_type(
            filesystem_id=value.filesystem_id,
            file_id=value.file_id,
            size=value.size,
            modified_ns=value.modified_ns,
            changed_ns=value.changed_ns,
            link_count=value.link_count,
            protection_identity=protection,
            is_private=is_private,
        )
    finally:
        handle.close()
        parent.close()


def read_private_bytes(
    path,
    *,
    trusted_root=None,
    trusted_root_identity=None,
    max_bytes=None,
    harden=True,
    allow_insecure_mode=False,
):
    access = native.FILE_ALL_ACCESS if harden else native.FILE_READ_ACCESS
    _path, parent, handle = _open_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        access=access,
    )
    try:
        if harden:
            _harden_private(handle)
        elif allow_insecure_mode:
            native.require_current_owner(handle)
        else:
            native.require_private(handle)
        return native.read_bytes(handle, max_bytes=max_bytes)
    finally:
        handle.close()
        parent.close()


def read_private_text(
    path,
    *,
    encoding="utf-8",
    errors="strict",
    trusted_root=None,
    trusted_root_identity=None,
    max_bytes=None,
    harden=True,
    allow_insecure_mode=False,
):
    return read_private_bytes(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        max_bytes=max_bytes,
        harden=harden,
        allow_insecure_mode=allow_insecure_mode,
    ).decode(encoding, errors=errors)


def _existing(parent, name):
    try:
        handle, _created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_WRITE_ACCESS,
        )
    except FileNotFoundError:
        return None
    try:
        native.require_private(handle)
        return handle
    except Exception:
        handle.close()
        raise


def _same_parent(path, original, *, trusted_root, trusted_root_identity):
    _path, current = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        if native.identity(current) != native.identity(original):
            raise ValueError("private root changed")
    finally:
        current.close()


def _same_target(parent, name, expected):
    try:
        current, _created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_READ_ACCESS,
        )
    except FileNotFoundError:
        if expected is None:
            return
        raise ValueError("private file changed") from None
    try:
        if expected is None or native.identity(current) != expected:
            raise ValueError("private file changed")
        native.require_private(current)
    finally:
        current.close()


def _create_temp(parent, name, data):
    with native.private_security_descriptor() as descriptor:
        handle, created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_WRITE_ACCESS,
            disposition=native.FILE_CREATE,
            security_descriptor=descriptor,
        )
    if not created:
        handle.close()
        raise ValueError("private temp changed")
    try:
        native.write_bytes(handle, data)
        native.require_private(handle)
        return handle
    except Exception:
        handle.close()
        raise


def _installed_target(parent, name, expected_identity):
    handle, _created = native.open_relative(
        parent,
        name,
        directory=False,
        desired_access=native.FILE_READ_ACCESS,
    )
    try:
        if native.identity(handle) != expected_identity:
            raise ValueError("private temp changed")
        native.require_private(handle)
    finally:
        handle.close()


def _open_owned_target(parent, name, expected_identity):
    handle, _created = native.open_relative(
        parent,
        name,
        directory=False,
        desired_access=native.FILE_WRITE_ACCESS,
        single_link=False,
    )
    try:
        if native.identity(handle) != expected_identity:
            raise ValueError("private file changed")
        native.require_private(handle)
        return handle
    except Exception:
        handle.close()
        raise


def _rollback(
    path,
    parent,
    installed_identity,
    backup,
    *,
    existing_identity,
):
    installed = None
    try:
        installed = _open_owned_target(parent, path.name, installed_identity)
        native.truncate(installed, 0)
        if existing_identity is None:
            native.delete_handle(installed)
            installed.close()
            installed = None
            _same_target(parent, path.name, None)
        else:
            restored = _open_owned_target(parent, backup.name, existing_identity)
            try:
                native.rename_handle(
                    restored,
                    parent,
                    path.name,
                    replace=True,
                )
            finally:
                restored.close()
            installed.close()
            installed = None
            _same_target(parent, path.name, existing_identity)
    except Exception as exc:
        raise AtomicWriteAmbiguous("private atomic write rollback failed") from exc
    finally:
        if installed is not None:
            installed.close()


def _cleanup_file(path, *, primary):
    try:
        native.delete_file(path, missing_ok=True)
    except Exception:
        if primary is None:
            raise


def _cleanup_owned_target(parent, name, expected_identity, *, primary):
    if expected_identity is None:
        return
    handle = None
    try:
        handle = _open_owned_target(parent, name, expected_identity)
        native.truncate(handle, 0)
        native.delete_handle(handle)
    except FileNotFoundError:
        return
    except Exception:
        if primary is None:
            raise
    finally:
        if handle is not None:
            handle.close()


def _rollback_promotion(
    source,
    destination,
    parent,
    source_identity,
    destination_identity,
    backup,
):
    try:
        _same_target(parent, destination.name, source_identity)
        if destination_identity is None:
            native.move_file(destination, source)
        else:
            native.replace_file(destination, backup, source)
        _same_target(parent, source.name, source_identity)
        _same_target(parent, destination.name, destination_identity)
    except Exception as exc:
        raise AtomicWriteAmbiguous("private file promotion rollback failed") from exc


def promote_private_file(
    source,
    destination,
    *,
    trusted_root,
    trusted_root_identity,
    expected_source_identity,
    expected_destination_identity=None,
):
    source, parent = native.open_parent(
        source,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    destination = native.lexical_absolute(destination)
    if destination.parent != source.parent or destination.name == source.name:
        parent.close()
        raise ValueError("private promotion requires distinct sibling files")
    source_handle = None
    backup = destination.with_name(
        f".{destination.name}.{secrets.token_hex(12)}.bak"
    )
    installed = False
    committed = False
    primary = None
    try:
        source_handle, _created = native.open_relative(
            parent,
            source.name,
            directory=False,
            desired_access=native.FILE_READ_ACCESS,
        )
        native.require_private(source_handle)
        if native.identity(source_handle) != tuple(expected_source_identity):
            raise ValueError("private file changed")
        _same_target(parent, destination.name, expected_destination_identity)
        _same_target(parent, backup.name, None)
        _same_parent(
            source,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        source_handle.close()
        source_handle = None
        try:
            if expected_destination_identity is None:
                native.move_file(source, destination)
            else:
                native.replace_file(destination, source, backup)
        except OSError:
            try:
                _same_target(parent, destination.name, tuple(expected_source_identity))
            except (OSError, ValueError):
                pass
            else:
                installed = True
            raise
        installed = True
        _same_target(parent, destination.name, tuple(expected_source_identity))
        _same_target(parent, source.name, None)
        _same_parent(
            source,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        if expected_destination_identity is not None:
            _same_target(
                parent, backup.name, tuple(expected_destination_identity)
            )
            native.delete_file(backup)
        committed = True
        return destination
    except BaseException as exc:
        primary = exc
        if installed and not committed:
            _rollback_promotion(
                source,
                destination,
                parent,
                tuple(expected_source_identity),
                (
                    None
                    if expected_destination_identity is None
                    else tuple(expected_destination_identity)
                ),
                backup,
            )
            installed = False
        raise
    finally:
        if source_handle is not None:
            source_handle.close()
        if not installed:
            _cleanup_file(backup, primary=primary)
        parent.close()


def remove_private_file(
    path,
    *,
    trusted_root,
    trusted_root_identity,
    expected_identity,
):
    path, parent = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    handle = None
    try:
        handle, _created = native.open_relative(
            parent,
            path.name,
            directory=False,
            desired_access=native.FILE_DELETE_ACCESS,
        )
        native.require_private(handle)
        if native.identity(handle) != tuple(expected_identity):
            raise ValueError("private file changed")
        _same_parent(
            path,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        native.delete_handle(handle)
        handle.close()
        handle = None
        _same_target(parent, path.name, None)
        _same_parent(
            path,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
    finally:
        if handle is not None:
            handle.close()
        parent.close()


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
    path, parent = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    existing = None
    temp = None
    temp_identity = None
    existing_identity = None
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    backup = path.with_name(f".{path.name}.{secrets.token_hex(12)}.bak")
    installed = False
    committed = False
    primary = None
    try:
        existing = _existing(parent, path.name)
        existing_identity = native.identity(existing) if existing is not None else None
        if require_absent and existing is not None:
            raise ValueError(error)
        if (
            existing is not None
            and max_existing_bytes is not None
            and native.facts(existing).size > int(max_existing_bytes)
        ):
            raise ValueError("private file too large")
        temp = _create_temp(parent, temp_path.name, bytes(data))
        temp_identity = native.identity(temp)
        if fsync_file is not None:
            fsync_file(temp.value)
        _same_parent(
            path,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        _same_target(parent, path.name, existing_identity)
        if validate_commit is not None:
            validate_commit()
        temp.close()
        temp = None
        if existing is None:
            native.move_file(temp_path, path)
        else:
            existing.close()
            existing = None
            native.replace_file(path, temp_path, backup)
        installed = True
        _installed_target(parent, path.name, temp_identity)
        _same_parent(
            path,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        if fsync_parent is not None:
            fsync_parent(parent.value)
        if validate_commit is not None:
            validate_commit()
        committed = True
        if existing_identity is not None:
            native.delete_file(backup)
        return path
    except BaseException as exc:
        primary = exc
        if installed and not committed:
            _rollback(
                path,
                parent,
                temp_identity,
                backup,
                existing_identity=existing_identity,
            )
            installed = False
        raise
    finally:
        if temp is not None:
            _cleanup_owned_target(
                parent,
                temp_path.name,
                temp_identity,
                primary=primary,
            )
            temp.close()
        if existing is not None:
            existing.close()
        if not installed:
            _cleanup_owned_target(
                parent,
                temp_path.name,
                temp_identity,
                primary=primary,
            )
            _cleanup_owned_target(
                parent,
                backup.name,
                existing_identity,
                primary=primary,
            )
        parent.close()


def _create_append_file(parent, name):
    with native.private_security_descriptor() as descriptor:
        handle, created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_WRITE_ACCESS,
            disposition=native.FILE_CREATE,
            security_descriptor=descriptor,
        )
    if not created:
        handle.close()
        raise ValueError("private file changed")
    return handle


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
    path, parent = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    created = False
    try:
        handle = _existing(parent, path.name)
        if handle is None:
            handle = _create_append_file(parent, path.name)
            created = True
        try:
            identity = native.identity(handle)
            if expected_identity is not None and identity != tuple(expected_identity):
                raise ValueError("private file changed")
            original_size = native.facts(handle).size
            if (
                max_total_bytes is not None
                and original_size + len(data) > int(max_total_bytes)
            ):
                raise ValueError("private file too large")
            try:
                native.write_bytes(handle, bytes(data), append=True)
                _same_target(parent, path.name, identity)
            except BaseException:
                native.truncate(handle, original_size)
                raise
            return path
        finally:
            handle.close()
    except BaseException:
        if created:
            native.delete_file(path, missing_ok=True)
        raise
    finally:
        parent.close()


def harden_private_tree(path):
    root = ensure_private_dir(path)
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                child = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    with native.open_path(
                        child,
                        directory=True,
                        desired_access=native.FILE_ALL_ACCESS,
                    ) as handle:
                        native.require_kind(handle, directory=True)
                        _harden_private(handle)
                    pending.append(child)
                elif entry.is_file(follow_symlinks=False):
                    ensure_private_file(child)
                else:
                    raise ValueError("private tree has unsafe entry")
    return root


def descriptor_digest(handle, size, error):
    if native.facts(handle).size != size:
        raise ValueError(error)
    return hashlib.sha256(native.read_bytes(handle, max_bytes=size)).hexdigest()
