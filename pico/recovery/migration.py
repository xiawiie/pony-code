"""Transactional directory cutover for explicit workspace migrations."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from pico.state.file_lock import locked_file
from pico.security.private_files import (
    PrivateAtomicWriteError,
    ensure_private_dir,
    private_directory_identity,
    read_private_bytes,
    write_private_bytes_atomic,
)
from pico.security.paths import require_directory_no_symlink

ABSENT = "absent"
PREPARING = "preparing"
CANDIDATE_READY = "candidate_ready"
OLD_MOVED = "old_moved"
NEW_INSTALLED = "new_installed"
VALIDATED = "validated"
COMMITTED = "committed"
ROLLBACK_REQUIRED = "rollback_required"
ROLLED_BACK = "rolled_back"
ROLLBACK_FAILED = "rollback_failed"
STATES = {
    PREPARING,
    CANDIDATE_READY,
    OLD_MOVED,
    NEW_INSTALLED,
    VALIDATED,
    COMMITTED,
    ROLLBACK_REQUIRED,
    ROLLED_BACK,
    ROLLBACK_FAILED,
}
_KEYS = {
    "record_type",
    "format_version",
    "migration_id",
    "contract",
    "source_version",
    "target_version",
    "state",
    "created_at",
    "updated_at",
    "workspace_identity",
    "paths",
    "source_identity",
    "candidate_identity",
    "error_code",
}
_MAX_JOURNAL_BYTES = 1024 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal key")
        result[key] = value
    return result


def _relative(value):
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("invalid migration path")
    return path


def _manifest(path):
    root = require_directory_no_symlink(path)
    digest = hashlib.sha256()
    for item in sorted(
        root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()
    ):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("unsafe migration tree")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("migration file has multiple links")
        digest.update(item.relative_to(root).as_posix().encode() + b"\0")
        if stat.S_ISREG(info.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(item, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise ValueError("migration file changed")
                while chunk := os.read(descriptor, 64 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                current = item.stat(follow_symlinks=False)
                if (
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    or (
                        current.st_dev,
                        current.st_ino,
                        current.st_size,
                        current.st_mtime_ns,
                    )
                    != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    or after.st_nlink != 1
                    or current.st_nlink != 1
                ):
                    raise ValueError("migration file changed")
            finally:
                os.close(descriptor)
    return {"manifest_hash": "sha256:" + digest.hexdigest()}


class Migration:
    """A single live/candidate/rollback directory transaction."""

    def __init__(
        self,
        pico_root,
        *,
        contract,
        source_version,
        target_version,
        live,
        workspace_identity,
        validate,
        namespace="",
        validate_candidate=None,
    ):
        self.root = require_directory_no_symlink(pico_root)
        namespace = str(namespace or "")
        if namespace and (
            "/" in namespace or "\\" in namespace or namespace in {".", ".."}
        ):
            raise ValueError("invalid migration namespace")
        self.area = (
            self.root / ".migration" / namespace
            if namespace
            else self.root / ".migration"
        )
        self.live_rel = _relative(live)
        area_rel = Path(".migration") / namespace if namespace else Path(".migration")
        self.candidate_rel = area_rel / "candidate" / self.live_rel
        self.rollback_rel = area_rel / "rollback" / self.live_rel
        self.live, self.candidate, self.rollback = (
            self.root / item
            for item in (self.live_rel, self.candidate_rel, self.rollback_rel)
        )
        self.journal = self.area / "journal.json"
        self.lock = self.area / "lock"
        self.contract, self.source_version, self.target_version = (
            str(contract),
            int(source_version),
            int(target_version),
        )
        self.workspace_identity, self.validate = dict(workspace_identity), validate
        self.validate_candidate = validate_candidate
        self._area_identity = None

    def status(self):
        if not self.area.exists():
            return {"state": ABSENT, "contract": self.contract}
        value = self._read()
        return (
            value if value is not None else {"state": ABSENT, "contract": self.contract}
        )

    def _setup(self):
        ensure_private_dir(self.area)
        ensure_private_dir(self.candidate.parent)
        ensure_private_dir(self.rollback.parent)
        area_identity = self._trusted_area_identity()
        if private_directory_identity(self.root)[0] != area_identity[0]:
            raise ValueError("candidate is on another filesystem")

    def _trusted_area_identity(self):
        current = private_directory_identity(self.area)
        if self._area_identity is None:
            self._area_identity = current
        elif current != self._area_identity:
            raise ValueError("migration area changed")
        return self._area_identity

    def _read(self):
        try:
            value = json.loads(
                read_private_bytes(
                    self.journal,
                    trusted_root=self.area,
                    trusted_root_identity=self._trusted_area_identity(),
                    max_bytes=_MAX_JOURNAL_BYTES,
                ),
                object_pairs_hook=_object,
            )
        except FileNotFoundError:
            return None
        paths = {
            "live": self.live_rel.as_posix(),
            "candidate": self.candidate_rel.as_posix(),
            "rollback": self.rollback_rel.as_posix(),
        }
        if (
            not isinstance(value, dict)
            or set(value) != _KEYS
            or value["record_type"] != "migration_journal"
            or value["format_version"] != 1
            or value["state"] not in STATES
            or value["paths"] != paths
        ):
            raise ValueError("invalid journal schema")
        for path in value["paths"].values():
            _relative(path)
        if (
            value["workspace_identity"] != self.workspace_identity
            or value["contract"] != self.contract
            or value["source_version"] != self.source_version
            or value["target_version"] != self.target_version
        ):
            raise ValueError("migration_identity_mismatch")
        return value

    def _write(self, value, state, error=""):
        value = dict(value, state=state, updated_at=_now(), error_code=error)
        data = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        if len(data) > _MAX_JOURNAL_BYTES:
            raise ValueError("private file too large")
        write_private_bytes_atomic(
            self.journal,
            data,
            trusted_root=self.area,
            trusted_root_identity=self._trusted_area_identity(),
            max_existing_bytes=_MAX_JOURNAL_BYTES,
        )
        return value

    @staticmethod
    def _fsync_dir(path):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_tree(self, path):
        shutil.rmtree(path)
        self._fsync_dir(path.parent)

    def _remove_journal(self):
        self.journal.unlink()
        self._fsync_dir(self.journal.parent)

    def _rename(self, source, destination):
        require_directory_no_symlink(source)
        if destination.exists() or destination.is_symlink():
            raise ValueError("migration destination exists")
        os.replace(source, destination)
        for parent in {source.parent, destination.parent}:
            self._fsync_dir(parent)

    def apply(self, build_candidate=None):
        self._setup()
        with locked_file(self.lock, require_lock=True):
            value = self._read()
            if value is None:
                if build_candidate is None:
                    raise ValueError("candidate builder required")
                created = _now()
                value = {
                    "record_type": "migration_journal",
                    "format_version": 1,
                    "migration_id": "mig_" + secrets.token_hex(12),
                    "contract": self.contract,
                    "source_version": self.source_version,
                    "target_version": self.target_version,
                    "state": PREPARING,
                    "created_at": created,
                    "updated_at": created,
                    "workspace_identity": self.workspace_identity,
                    "paths": {
                        "live": self.live_rel.as_posix(),
                        "candidate": self.candidate_rel.as_posix(),
                        "rollback": self.rollback_rel.as_posix(),
                    },
                    "source_identity": _manifest(self.live),
                    "candidate_identity": {},
                    "error_code": "",
                }
                value = self._write(value, PREPARING)
                if self.candidate.exists():
                    self._remove_tree(self.candidate)
                build_candidate(self.live, self.candidate)
                if (
                    private_directory_identity(self.candidate)[0]
                    != private_directory_identity(self.root)[0]
                ):
                    raise ValueError("candidate is on another filesystem")
                value["candidate_identity"] = _manifest(self.candidate)
                value = self._write(value, CANDIDATE_READY)
            return self._advance(value)

    def _advance(self, value):
        state = value["state"]
        if state == CANDIDATE_READY:
            if (
                _manifest(self.live) != value["source_identity"]
                or _manifest(self.candidate) != value["candidate_identity"]
            ):
                raise ValueError("migration_identity_mismatch")
            if (
                self.validate_candidate is not None
                and self.validate_candidate(self.candidate) is False
            ):
                raise ValueError("candidate validation failed")
            self._rename(self.live, self.rollback)
            value, state = self._write(value, OLD_MOVED), OLD_MOVED
        if state == OLD_MOVED:
            self._rename(self.candidate, self.live)
            value, state = self._write(value, NEW_INSTALLED), NEW_INSTALLED
        if state == NEW_INSTALLED:
            try:
                if self.validate(self.live) is False:
                    raise ValueError("validation failed")
            except Exception:
                return self._rollback(
                    self._write(value, ROLLBACK_REQUIRED, "validation_failed")
                )
            value, state = self._write(value, VALIDATED), VALIDATED
        if state == VALIDATED:
            self._remove_tree(self.rollback)
            value, state = self._write(value, COMMITTED), COMMITTED
        if state == COMMITTED:
            self._remove_journal()
            return ABSENT
        return state

    def _reconcile(self, value):
        """Advance the journal to match durable rename facts after a crash."""
        state = value["state"]
        live = self.live.exists() and not self.live.is_symlink()
        candidate = self.candidate.exists() and not self.candidate.is_symlink()
        rollback = self.rollback.exists() and not self.rollback.is_symlink()
        if state == CANDIDATE_READY and not live and candidate and rollback:
            return self._write(value, OLD_MOVED)
        if state == OLD_MOVED and live and not candidate and rollback:
            return self._write(value, NEW_INSTALLED)
        if state == VALIDATED and live and not rollback:
            return self._write(value, COMMITTED)
        if state == ROLLBACK_REQUIRED:
            if not live and candidate and rollback:
                self._rename(self.rollback, self.live)
                return self._write(value, ROLLED_BACK, value["error_code"])
            if live and candidate and not rollback:
                return self._write(value, ROLLED_BACK, value["error_code"])
        return value

    def _rollback(self, value):
        try:
            if self.live.exists():
                self._rename(self.live, self.candidate)
            self._rename(self.rollback, self.live)
            self._write(value, ROLLED_BACK, value["error_code"])
            return ROLLED_BACK
        except PrivateAtomicWriteError:
            raise
        except Exception:
            self._write(value, ROLLBACK_FAILED, "migration_rollback_failed")
            raise

    def abort(self):
        self._setup()
        with locked_file(self.lock, require_lock=True):
            value = self._read()
            if value is None:
                return ABSENT
            if value["state"] not in {PREPARING, CANDIDATE_READY}:
                raise ValueError("migration cannot be aborted")
            if self.candidate.exists():
                self._remove_tree(self.candidate)
            self._remove_journal()
            return ABSENT

    def recover(self):
        self._setup()
        with locked_file(self.lock, require_lock=True):
            value = self._read()
            if value is None:
                return ABSENT
            value = self._reconcile(value)
            state = value["state"]
            if state == PREPARING:
                if self.candidate.exists():
                    self._remove_tree(self.candidate)
                self._remove_journal()
                return ABSENT
            if (
                state == CANDIDATE_READY
                and self.live.exists()
                and not self.live.is_symlink()
                and not self.candidate.exists()
                and not self.rollback.exists()
            ):
                # An abort can finish candidate deletion before its parent fsync.
                self._remove_journal()
                return ABSENT
            if state in {
                CANDIDATE_READY,
                OLD_MOVED,
                NEW_INSTALLED,
                VALIDATED,
                COMMITTED,
            }:
                return self._advance(value)
            if state == ROLLBACK_REQUIRED:
                return self._rollback(value)
            if state == ROLLED_BACK:
                if self.candidate.exists():
                    self._remove_tree(self.candidate)
                self._remove_journal()
                return ABSENT
            raise ValueError("migration_rollback_failed")
