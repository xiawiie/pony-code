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


class ProjectTrustStore:
    def __init__(self, state_root):
        self.root = Path(state_root).absolute()
        self.path = self.root / "trust.json"
        self.lock_path = self.root / ".trust.lock"
        try:
            self._root_identity = self._read_root_identity()
        except FileNotFoundError:
            self._root_identity = None

    def _read_root_identity(self):
        identity = private_directory_identity(self.root)
        info = self.root.stat(follow_symlinks=False)
        uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("private trust directory permissions are unsafe")
        return identity

    def _ensure_root(self):
        self.root = ensure_private_dir(self.root)
        self._root_identity = self._read_root_identity()

    def trust(self, project_root):
        project_root = require_directory_no_symlink(project_root)
        identity = private_directory_identity(project_root)
        self._ensure_root()
        with file_lock.locked_file(self.lock_path, require_lock=True):
            projects = self._load_projects()
            projects[str(project_root)] = {
                "device": identity[0],
                "inode": identity[1],
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
            or payload["version"] != 1
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
                or type(record["device"]) is not int
                or type(record["inode"]) is not int
            ):
                raise ValueError("invalid trust store")
        return projects

    def _write_projects(self, projects):
        rendered = (
            json.dumps(
                {"version": 1, "projects": projects},
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
