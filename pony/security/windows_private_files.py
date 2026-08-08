"""Windows implementation of Pony's private-state file contracts."""

from __future__ import annotations

import hashlib
import os
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
            share_access=native.FILE_SHARE_READ,
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
            single_link=False,
        )
    except FileNotFoundError:
        if expected is None:
            return
        raise ValueError("private file changed") from None
    try:
        value = native.facts(current)
        current_identity = value.filesystem_id, value.file_id
        if expected is None or current_identity != expected or value.link_count != 1:
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


def _installed_target(parent, name, expected_identity, size, digest, error):
    try:
        handle, _created = native.open_relative(
            parent,
            name,
            directory=False,
            desired_access=native.FILE_READ_ACCESS,
        )
        try:
            if native.identity(handle) != expected_identity:
                raise ValueError(error)
            native.require_private(handle)
            if descriptor_digest(handle, size, error) != digest:
                raise ValueError(error)
        finally:
            handle.close()
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(error) from exc


def _require_temp_binding(parent, name, expected_identity, error):
    try:
        _same_target(parent, name, expected_identity)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(error) from exc


def _open_owned_target(parent, name, expected_identity):
    handle, _created = native.open_relative(
        parent,
        name,
        directory=False,
        desired_access=native.FILE_WRITE_ACCESS,
        share_access=native.FILE_SHARE_READ,
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


def _copy_restore(parent, canonical_name, source, size, digest, error):
    name = f".{canonical_name}.{secrets.token_hex(12)}.restore"
    handle = None
    identity = None
    try:
        with native.private_security_descriptor() as descriptor:
            handle, created = native.open_relative(
                parent,
                name,
                directory=False,
                desired_access=native.FILE_WRITE_ACCESS,
                disposition=native.FILE_CREATE,
                security_descriptor=descriptor,
                share_access=native.FILE_SHARE_ALL,
            )
        if not created:
            raise ValueError(error)
        identity = native.identity(handle)
        copied = hashlib.sha256()
        total = 0
        for chunk in native.read_chunks(source):
            total += len(chunk)
            if total > size:
                raise ValueError(error)
            native.write_bytes(handle, chunk, append=total != len(chunk))
            copied.update(chunk)
        if total == 0:
            native.write_bytes(handle, b"")
        native.require_private(handle)
        if total != size or copied.hexdigest() != digest:
            raise ValueError(error)
        if descriptor_digest(handle, size, error) != digest:
            raise ValueError(error)
        return name, handle, identity
    except BaseException as exc:
        if handle is not None:
            handle.close()
        _cleanup_owned_target(parent, name, identity, primary=exc)
        raise


def _rollback(
    path,
    parent,
    installed_identity,
    backup_handle,
    backup_identity,
    *,
    existing_identity,
    existing_size,
    existing_digest,
    error,
):
    installed = None
    try:
        installed, _created = native.open_relative(
            parent,
            path.name,
            directory=False,
            desired_access=native.FILE_WRITE_ACCESS,
            share_access=native.FILE_SHARE_ALL,
            single_link=False,
        )
        if native.identity(installed) != installed_identity:
            raise ValueError(error)
        native.require_private(installed)
        if existing_identity is None:
            native.truncate(installed, 0)
            native.delete_handle(installed)
            installed.close()
            installed = None
            _same_target(parent, path.name, None)
            return False

        if backup_handle is None or native.identity(backup_handle) != backup_identity:
            raise ValueError(error)
        native.require_private(backup_handle)
        if descriptor_digest(backup_handle, existing_size, error) != existing_digest:
            raise ValueError(error)
        installed.close()
        installed = None
        native.rename_handle(backup_handle, parent, path.name, replace=True)
        _same_target(parent, path.name, backup_identity)
        return True
    except Exception as exc:
        raise AtomicWriteAmbiguous("private atomic write rollback failed") from exc
    finally:
        if installed is not None:
            installed.close()


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
    source_handle,
    source_identity,
    source_size,
    source_digest,
    destination_identity,
    restore_name,
    restore_handle,
    restore_identity,
    destination_size,
    destination_digest,
):
    error = "private file promotion rollback failed"
    try:
        _same_target(parent, destination.name, source_identity)
        _same_target(parent, source.name, None)
        if native.identity(source_handle) != source_identity:
            raise ValueError(error)
        native.require_private(source_handle)
        if descriptor_digest(source_handle, source_size, error) != source_digest:
            raise ValueError(error)
        try:
            native.rename_handle(source_handle, parent, source.name, replace=False)
        except OSError:
            _installed_target(
                parent,
                source.name,
                source_identity,
                source_size,
                source_digest,
                error,
            )
        else:
            _installed_target(
                parent,
                source.name,
                source_identity,
                source_size,
                source_digest,
                error,
            )
        _same_target(parent, destination.name, None)

        if destination_identity is None:
            return False
        if restore_handle is None or native.identity(restore_handle) != restore_identity:
            raise ValueError(error)
        native.require_private(restore_handle)
        if (
            descriptor_digest(restore_handle, destination_size, error)
            != destination_digest
        ):
            raise ValueError(error)
        _require_temp_binding(
            parent,
            restore_name,
            restore_identity,
            error,
        )
        try:
            native.rename_handle(
                restore_handle,
                parent,
                destination.name,
                replace=False,
            )
        except OSError:
            _installed_target(
                parent,
                destination.name,
                restore_identity,
                destination_size,
                destination_digest,
                error,
            )
        else:
            _installed_target(
                parent,
                destination.name,
                restore_identity,
                destination_size,
                destination_digest,
                error,
            )
        _require_temp_binding(parent, restore_name, None, error)
        return True
    except Exception as exc:
        raise AtomicWriteAmbiguous(error) from exc


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
    destination_handle = None
    restore_name = None
    restore_handle = None
    restore_identity = None
    source_size = None
    source_digest = None
    destination_size = None
    destination_digest = None
    installed = False
    committed = False
    restore_installed = False
    primary = None
    try:
        source_handle, _created = native.open_relative(
            parent,
            source.name,
            directory=False,
            desired_access=native.FILE_WRITE_ACCESS,
            share_access=native.FILE_SHARE_ALL,
        )
        native.require_private(source_handle)
        source_identity = tuple(expected_source_identity)
        if native.identity(source_handle) != source_identity:
            raise ValueError("private file changed")
        source_size = native.facts(source_handle).size
        source_digest = descriptor_digest(
            source_handle,
            source_size,
            "private file changed",
        )

        destination_identity = (
            None
            if expected_destination_identity is None
            else tuple(expected_destination_identity)
        )
        destination_handle = _existing(parent, destination.name)
        if destination_handle is None:
            if destination_identity is not None:
                raise ValueError("private file changed")
        else:
            if destination_identity is None:
                raise ValueError("private file changed")
            if native.identity(destination_handle) != destination_identity:
                raise ValueError("private file changed")
            destination_size = native.facts(destination_handle).size
            destination_digest = descriptor_digest(
                destination_handle,
                destination_size,
                "private file changed",
            )
            restore_name, restore_handle, restore_identity = _copy_restore(
                parent,
                destination.name,
                destination_handle,
                destination_size,
                destination_digest,
                "private file changed",
            )

        _same_parent(
            source,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        _require_temp_binding(
            parent,
            source.name,
            source_identity,
            "private file changed",
        )
        _same_target(parent, destination.name, destination_identity)
        if restore_handle is not None:
            _require_temp_binding(
                parent,
                restore_name,
                restore_identity,
                "private file changed",
            )
            if (
                descriptor_digest(
                    restore_handle,
                    destination_size,
                    "private file changed",
                )
                != destination_digest
            ):
                raise ValueError("private file changed")
        if destination_handle is not None:
            destination_handle.close()
            destination_handle = None
        try:
            native.rename_handle(
                source_handle,
                parent,
                destination.name,
                replace=destination_identity is not None,
            )
        except OSError:
            try:
                _installed_target(
                    parent,
                    destination.name,
                    source_identity,
                    source_size,
                    source_digest,
                    "private file changed",
                )
            except (OSError, ValueError):
                _require_temp_binding(
                    parent,
                    source.name,
                    source_identity,
                    "private file changed",
                )
                _same_target(parent, destination.name, destination_identity)
            else:
                installed = True
            raise
        installed = True
        _installed_target(
            parent,
            destination.name,
            source_identity,
            source_size,
            source_digest,
            "private file changed",
        )
        _require_temp_binding(
            parent,
            source.name,
            None,
            "private file changed",
        )
        _same_parent(
            source,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        if restore_handle is not None:
            _require_temp_binding(
                parent,
                restore_name,
                restore_identity,
                "private file changed",
            )
            try:
                native.delete_handle(restore_handle)
            except BaseException as cleanup_error:
                restore_handle.close()
                restore_handle = None
                try:
                    _require_temp_binding(
                        parent,
                        restore_name,
                        None,
                        "private file changed",
                    )
                except (OSError, ValueError):
                    try:
                        restore_handle = _open_owned_target(
                            parent,
                            restore_name,
                            restore_identity,
                        )
                    except Exception as reopen_error:
                        raise AtomicWriteAmbiguous(
                            "private file promotion cleanup failed"
                        ) from reopen_error
                    raise cleanup_error
            else:
                restore_handle.close()
                restore_handle = None
                _require_temp_binding(
                    parent,
                    restore_name,
                    None,
                    "private file changed",
                )
        committed = True
        return destination
    except BaseException as exc:
        primary = exc
        if installed and not committed:
            restore_installed = _rollback_promotion(
                source,
                destination,
                parent,
                source_handle,
                source_identity,
                source_size,
                source_digest,
                destination_identity,
                restore_name,
                restore_handle,
                restore_identity,
                destination_size,
                destination_digest,
            )
            installed = False
        raise
    finally:
        if destination_handle is not None:
            destination_handle.close()
        if source_handle is not None:
            source_handle.close()
        if restore_handle is not None:
            if not committed and not restore_installed:
                try:
                    native.truncate(restore_handle, 0)
                    native.delete_handle(restore_handle)
                except Exception:
                    if primary is None:
                        raise
            restore_handle.close()
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
    rendered = bytes(data)
    rendered_digest = hashlib.sha256(rendered).hexdigest()
    path, parent = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    existing = None
    temp = None
    temp_identity = None
    existing_identity = None
    existing_size = None
    existing_digest = None
    _backup_name = None
    backup_handle = None
    backup_identity = None
    backup_installed = False
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    installed = False
    committed = False
    preserve_backup = False
    primary = None
    try:
        existing = _existing(parent, path.name)
        existing_identity = native.identity(existing) if existing is not None else None
        existing_size = native.facts(existing).size if existing is not None else None
        if require_absent and existing is not None:
            raise ValueError(error)
        if (
            existing is not None
            and max_existing_bytes is not None
            and existing_size > int(max_existing_bytes)
        ):
            raise ValueError("private file too large")
        if existing is not None:
            existing_digest = descriptor_digest(existing, existing_size, error)

        temp = _create_temp(parent, temp_path.name, rendered)
        temp_identity = native.identity(temp)
        if fsync_file is not None:
            fsync_file(temp.value)
        if descriptor_digest(temp, len(rendered), error) != rendered_digest:
            raise ValueError(error)

        if existing is not None:
            _backup_name, backup_handle, backup_identity = _copy_restore(
                parent,
                path.name,
                existing,
                existing_size,
                existing_digest,
                error,
            )

        _same_parent(
            path,
            parent,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        _same_target(parent, path.name, existing_identity)
        _require_temp_binding(parent, temp_path.name, temp_identity, error)
        if validate_commit is not None:
            validate_commit()

        if existing is not None:
            existing.close()
            existing = None
        try:
            native.rename_handle(
                temp,
                parent,
                path.name,
                replace=existing_identity is not None,
            )
        except OSError:
            try:
                _same_target(parent, path.name, temp_identity)
            except (OSError, ValueError):
                try:
                    _same_target(parent, path.name, existing_identity)
                except (OSError, ValueError):
                    preserve_backup = True
                    raise ValueError(error) from None
                _require_temp_binding(parent, temp_path.name, temp_identity, error)
            else:
                installed = True
            raise
        installed = True
        _installed_target(
            parent,
            path.name,
            temp_identity,
            len(rendered),
            rendered_digest,
            error,
        )
        _require_temp_binding(parent, temp_path.name, None, error)
        temp.close()
        temp = None
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

        if backup_handle is not None:
            try:
                native.delete_handle(backup_handle)
            except BaseException as cleanup_error:
                if temp is not None:
                    temp.close()
                    temp = None
                try:
                    backup_installed = _rollback(
                        path,
                        parent,
                        temp_identity,
                        backup_handle,
                        backup_identity,
                        existing_identity=existing_identity,
                        existing_size=existing_size,
                        existing_digest=existing_digest,
                        error=error,
                    )
                except BaseException as rollback_error:
                    preserve_backup = True
                    raise AtomicWriteAmbiguous(error) from rollback_error
                installed = False
                raise cleanup_error
            backup_handle.close()
            backup_handle = None
        committed = True
        return path
    except BaseException as exc:
        primary = exc
        if installed and not committed:
            if temp is not None:
                temp.close()
                temp = None
            try:
                backup_installed = _rollback(
                    path,
                    parent,
                    temp_identity,
                    backup_handle,
                    backup_identity,
                    existing_identity=existing_identity,
                    existing_size=existing_size,
                    existing_digest=existing_digest,
                    error=error,
                )
            except BaseException:
                preserve_backup = True
                raise
            installed = False
        raise
    finally:
        if existing is not None:
            existing.close()
        if temp is not None:
            if not committed:
                try:
                    native.truncate(temp, 0)
                    native.delete_handle(temp)
                except Exception:
                    if primary is None:
                        raise
            temp.close()
        if backup_handle is not None:
            if not preserve_backup and not backup_installed:
                try:
                    native.truncate(backup_handle, 0)
                    native.delete_handle(backup_handle)
                except Exception:
                    if primary is None:
                        raise
            backup_handle.close()
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
        with os.scandir(native.win32_path(directory)) as entries:
            for entry in entries:
                child = directory / entry.name
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
    digest = hashlib.sha256()
    total = 0
    for chunk in native.read_chunks(handle):
        total += len(chunk)
        if total > size:
            raise ValueError(error)
        digest.update(chunk)
    if total != size or native.facts(handle).size != size:
        raise ValueError(error)
    return digest.hexdigest()
