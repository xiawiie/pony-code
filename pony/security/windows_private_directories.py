"""Anchored Windows directory operations for migration cutovers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import windows_native as native


_MISSING_ERRORS = {2, 3}


class DirectoryMoveAmbiguous(RuntimeError):
    """A directory move could not prove either committed or rolled back state."""


def _signature(value):
    return (
        value.filesystem_id,
        value.file_id,
        value.directory,
        value.link_count,
        value.size,
        value.modified_ns,
        value.changed_ns,
        value.reparse_tag,
    )


def _open_tree(path, *, trusted_root, trusted_root_identity, access):
    path, parent = native.open_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        share_access=native.FILE_SHARE_READ_WRITE,
    )
    try:
        handle, _created = native.open_relative(
            parent,
            path.name,
            directory=True,
            desired_access=access,
            share_access=native.FILE_SHARE_READ_WRITE,
        )
        return path, parent, handle
    except Exception:
        parent.close()
        raise


def _children(path, handle):
    before = native.identity(handle)
    with os.scandir(native.win32_path(path)) as entries:
        names = sorted(entry.name for entry in entries)
    if native.identity(handle) != before:
        raise ValueError("migration directory changed")
    return names


def _collect(path, handle, relative, entries):
    for name in _children(path, handle):
        child_path = path / name
        child_relative = relative / name
        try:
            child, _created = native.open_relative(
                handle,
                name,
                directory=True,
                desired_access=native.FILE_DIRECTORY_ACCESS,
                share_access=native.FILE_SHARE_READ_WRITE,
            )
        except OSError:
            child, _created = native.open_relative(
                handle,
                name,
                directory=False,
                desired_access=native.FILE_READ_ACCESS,
                share_access=native.FILE_SHARE_READ_WRITE,
            )
        try:
            facts = native.facts(child)
            entries.append((child_relative, facts.directory, _signature(facts)))
            if facts.directory:
                _collect(child_path, child, child_relative, entries)
        finally:
            child.close()


def _open_relative_tree(root, relative, *, directory):
    current = root
    opened = []
    try:
        for index, name in enumerate(relative.parts):
            final = index == len(relative.parts) - 1
            child, _created = native.open_relative(
                current,
                name,
                directory=directory if final else True,
                desired_access=(
                    native.FILE_DIRECTORY_ACCESS
                    if directory or not final
                    else native.FILE_READ_ACCESS
                ),
                share_access=native.FILE_SHARE_READ_WRITE,
            )
            opened.append(child)
            current = child
        return opened
    except Exception:
        for handle in reversed(opened):
            handle.close()
        raise


def private_tree_manifest(path, *, trusted_root, trusted_root_identity):
    try:
        return _private_tree_manifest(
            path,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
    except native.ReparsePointError as exc:
        raise ValueError("unsafe migration tree") from exc


def _private_tree_manifest(path, *, trusted_root, trusted_root_identity):
    path, parent, root = _open_tree(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        access=native.FILE_DIRECTORY_ACCESS,
    )
    try:
        root_identity = native.identity(root)
        entries = []
        _collect(path, root, Path(), entries)
        digest = hashlib.sha256()
        for relative, directory, expected in sorted(entries, key=lambda item: item[0].as_posix()):
            handles = _open_relative_tree(root, relative, directory=directory)
            try:
                leaf = handles[-1]
                if _signature(native.facts(leaf)) != expected:
                    raise ValueError("migration tree changed")
                digest.update(relative.as_posix().encode() + b"\0")
                if not directory:
                    for chunk in native.read_chunks(leaf):
                        digest.update(chunk)
                    if _signature(native.facts(leaf)) != expected:
                        raise ValueError("migration file changed")
            finally:
                for handle in reversed(handles):
                    handle.close()
        if native.identity(root) != root_identity:
            raise ValueError("migration directory changed")
        return {"manifest_hash": "sha256:" + digest.hexdigest()}
    finally:
        root.close()
        parent.close()


def _remove_contents(path, handle):
    for name in _children(path, handle):
        try:
            child, _created = native.open_relative(
                handle,
                name,
                directory=True,
                desired_access=native.DIRECTORY_DELETE_ACCESS,
                share_access=native.FILE_SHARE_READ_WRITE,
            )
        except OSError:
            child, _created = native.open_relative(
                handle,
                name,
                directory=False,
                desired_access=native.FILE_DELETE_ACCESS,
                share_access=native.FILE_SHARE_READ_WRITE,
            )
        try:
            facts = native.facts(child)
            if facts.directory:
                _remove_contents(path / name, child)
            native.delete_handle(child)
        finally:
            child.close()


def remove_private_tree(path, *, trusted_root, trusted_root_identity):
    path, parent, root = _open_tree(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        access=native.DIRECTORY_DELETE_ACCESS,
    )
    try:
        _remove_contents(path, root)
        native.delete_handle(root)
    finally:
        root.close()
        parent.close()
    if path.exists():
        raise DirectoryMoveAmbiguous("migration tree removal could not be verified")


def _missing(parent, name):
    errors = []
    for directory in (True, False):
        try:
            handle, _created = native.open_relative(
                parent,
                name,
                directory=directory,
                desired_access=(
                    native.FILE_DIRECTORY_ACCESS if directory else native.FILE_READ_ACCESS
                ),
            )
        except OSError as exc:
            errors.append(native.error_code(exc))
        except ValueError:
            return False
        else:
            handle.close()
            return False
    return all(error in _MISSING_ERRORS or error == 267 for error in errors)


def move_private_directory(source, destination, *, trusted_root, trusted_root_identity):
    source, source_parent, handle = _open_tree(
        source,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        access=native.DIRECTORY_DELETE_ACCESS,
    )
    destination, destination_parent = native.open_parent(
        destination,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        share_access=native.FILE_SHARE_READ_WRITE,
    )
    installed = False
    expected = native.identity(handle)
    try:
        if not _missing(destination_parent, destination.name):
            raise ValueError("migration destination exists")
        native.rename_handle(handle, destination_parent, destination.name)
        installed = True
        if not _missing(source_parent, source.name):
            raise DirectoryMoveAmbiguous("migration source still exists after move")
        current, _created = native.open_relative(
            destination_parent,
            destination.name,
            directory=True,
            desired_access=native.FILE_DIRECTORY_ACCESS,
        )
        try:
            if native.identity(current) != expected:
                raise DirectoryMoveAmbiguous("migration destination identity changed")
        finally:
            current.close()
    except BaseException:
        if installed:
            try:
                native.rename_handle(handle, source_parent, source.name)
                if _missing(source_parent, source.name) or not _missing(
                    destination_parent, destination.name
                ):
                    raise DirectoryMoveAmbiguous("migration move rollback was not verified")
            except Exception as rollback:
                raise DirectoryMoveAmbiguous(
                    "migration directory move rollback failed"
                ) from rollback
        raise
    finally:
        handle.close()
        destination_parent.close()
        source_parent.close()
