"""Machine-local project trust bound to a no-follow root identity."""

import json
import os
from pathlib import Path
import stat

from pony.security.paths import require_directory_no_symlink
from pony.security.private_files import (
    ensure_private_dir,
    private_directory_identity,
    read_private_bytes,
    write_private_bytes_atomic,
)
from pony.state import file_lock


_MAX_TRUST_BYTES = 1024 * 1024
_BYTES_IDENTITY_PREFIX = "bytes:"


class _RepairableTrustRootPermissions(ValueError):
    def __init__(self, identity):
        super().__init__("private trust directory permissions are unsafe")
        self.identity = identity


def _encode_identity_value(value):
    if type(value) is int:
        return value
    if type(value) is bytes:
        return _BYTES_IDENTITY_PREFIX + value.hex()
    raise ValueError("invalid project identity")


def _decode_identity_value(value):
    if type(value) is int:
        return value
    if not isinstance(value, str) or not value.startswith(_BYTES_IDENTITY_PREFIX):
        raise ValueError("invalid trust store")
    encoded = value.removeprefix(_BYTES_IDENTITY_PREFIX)
    try:
        decoded = bytes.fromhex(encoded)
    except ValueError as exc:
        raise ValueError("invalid trust store") from exc
    if decoded.hex() != encoded:
        raise ValueError("invalid trust store")
    return decoded


class ProjectTrustStore:
    def __init__(self, state_root):
        self.root = Path(state_root).absolute()
        self.path = self.root / "trust.json"
        self.lock_path = self.root / ".trust.lock"
        try:
            self._root_identity = self._read_root_identity()
        except FileNotFoundError:
            self._root_identity = None
        except _RepairableTrustRootPermissions as exc:
            # Host sandboxes can add directory ACLs. Harden the owned root before
            # reading; the trust file itself remains strict and is never repaired here.
            self._ensure_root(expected_identity=exc.identity)

    def _read_root_identity(self):
        identity = private_directory_identity(self.root)
        if os.name == "nt":
            from pony.security import windows_native

            with windows_native.open_path(self.root, directory=True) as handle:
                if windows_native.identity(handle) != tuple(identity):
                    raise ValueError("trust store root changed")
                windows_native.require_current_owner(handle)
                try:
                    windows_native.require_private(handle)
                except ValueError as exc:
                    raise _RepairableTrustRootPermissions(identity) from exc
            return identity
        info = self.root.stat(follow_symlinks=False)
        uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
            raise ValueError("private trust directory owner is unsafe")
        if identity != (info.st_dev, info.st_ino):
            raise ValueError("trust store root changed")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise _RepairableTrustRootPermissions(identity)
        return identity

    def _ensure_root(self, *, expected_identity=None):
        self.root = ensure_private_dir(self.root)
        identity = self._read_root_identity()
        if expected_identity is not None and identity != expected_identity:
            raise ValueError("trust store root changed")
        self._root_identity = identity

    def trust(self, project_root):
        project_root = require_directory_no_symlink(project_root)
        identity = private_directory_identity(project_root)
        self._ensure_root()
        with file_lock.locked_file(self.lock_path, require_lock=True):
            projects = self._load_projects()
            projects[str(project_root)] = {
                "device": identity.filesystem_id,
                "inode": identity.file_id,
            }
            if private_directory_identity(project_root) != identity:
                raise ValueError("project root changed")
            self._write_projects(projects)

    def revoke(self, project_root):
        project_root = require_directory_no_symlink(project_root)
        self._ensure_root()
        with file_lock.locked_file(self.lock_path, require_lock=True):
            projects = self._load_projects()
            projects.pop(str(project_root), None)
            self._write_projects(projects)

    def is_trusted(self, project_root):
        try:
            project_root = require_directory_no_symlink(project_root)
            expected = self._load_projects().get(str(project_root))
            if expected is None:
                return False
            return private_directory_identity(project_root) == (
                expected["device"],
                expected["inode"],
            )
        except (OSError, TypeError, ValueError):
            return False

    def _load_projects(self):
        if self._root_identity is None:
            try:
                self._root_identity = self._read_root_identity()
            except FileNotFoundError:
                return {}
        elif self._read_root_identity() != self._root_identity:
            raise ValueError("trust store root changed")
        try:
            raw = read_private_bytes(
                self.path,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=_MAX_TRUST_BYTES,
                harden=False,
            ).decode("utf-8")
        except FileNotFoundError:
            return {}
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "projects"}
            or payload["version"] not in {1, 2}
        ):
            raise ValueError("invalid trust store")
        projects = payload["projects"]
        if not isinstance(projects, dict):
            raise ValueError("invalid trust store")
        for path, record in projects.items():
            if (
                not isinstance(path, str)
                or not isinstance(record, dict)
                or set(record) != {"device", "inode"}
            ):
                raise ValueError("invalid trust store")
            if payload["version"] == 1:
                if type(record["device"]) is not int or type(record["inode"]) is not int:
                    raise ValueError("invalid trust store")
                continue
            record["device"] = _decode_identity_value(record["device"])
            record["inode"] = _decode_identity_value(record["inode"])
        return projects

    def _write_projects(self, projects):
        encoded_projects = {
            path: {
                "device": _encode_identity_value(record["device"]),
                "inode": _encode_identity_value(record["inode"]),
            }
            for path, record in projects.items()
        }
        rendered = (
            json.dumps(
                {"version": 2, "projects": encoded_projects},
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(rendered) > _MAX_TRUST_BYTES:
            raise ValueError("trust store too large")
        write_private_bytes_atomic(
            self.path,
            rendered,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            error="trust store changed",
            max_existing_bytes=_MAX_TRUST_BYTES,
        )
