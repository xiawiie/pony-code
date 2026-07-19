"""Durable before-images for Pony-owned workspace edits."""

import hashlib
import json
import os
from pathlib import Path

from pony.security import private_files, workspace_files

from . import file_lock


FORMAT_VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_TURN_KEY_BYTES = 256
_MANIFEST_FIELDS = {"format_version", "turn_key", "paths"}
_ENTRY_FIELDS = {"before", "post"}
_STATE_FIELDS = {"exists", "sha256", "blob_ref", "mode"}


class EditCheckpointError(ValueError):
    def __init__(self, code, *, paths=()):
        self.code = str(code)
        self.paths = tuple(paths)
        super().__init__(self.code)


class EditCheckpointStore:
    """Capture each path once and assess whether rewind is still safe."""

    def __init__(self, state_root, workspace_root, *, max_file_bytes=MAX_FILE_BYTES):
        self.root = private_files.ensure_private_dir(state_root)
        self.turns = private_files.ensure_private_dir(self.root / "turns")
        self.blobs = private_files.ensure_private_dir(self.root / "blobs")
        self.lock = self.root / ".store.lock"
        self.workspace = Path(os.path.abspath(os.fspath(workspace_root)))
        self.max_file_bytes = int(max_file_bytes)
        if self.max_file_bytes < 0:
            raise ValueError("invalid edit checkpoint file limit")
        self._root_identity = private_files.private_directory_identity(self.root)
        self._workspace_identity = private_files.private_directory_identity(self.workspace)

    def capture_before(self, turn_key, raw_path):
        self._require_roots()
        key, path = _turn_key(turn_key), _relative_path(raw_path)
        with file_lock.locked_file(self.lock, require_lock=True):
            manifest = self._load(key) or _manifest(key)
            if path in manifest["paths"]:
                return dict(manifest["paths"][path]["before"])
            current = self._workspace_state(path)
            before = _state(
                current,
                self._write_blob(current["data"]) if current["exists"] else "",
            )
            manifest["paths"][path] = {"before": before, "post": None}
            self._save(manifest)
            return dict(before)

    def record_post(self, turn_key, raw_path):
        self._require_roots()
        key, path = _turn_key(turn_key), _relative_path(raw_path)
        with file_lock.locked_file(self.lock, require_lock=True):
            manifest = self._load(key)
            if manifest is None or path not in manifest["paths"]:
                raise EditCheckpointError("edit_checkpoint_before_missing")
            post = _state(self._workspace_state(path))
            manifest["paths"][path]["post"] = post
            self._save(manifest)
            return dict(post)

    def assess_restore(self, turn_key):
        """Read-only eligibility check; this store never mutates the workspace."""
        self._require_roots()
        key = _turn_key(turn_key)
        with file_lock.locked_file(self.lock, require_lock=True):
            manifest = self._load(key)
            if manifest is None:
                raise EditCheckpointError("edit_checkpoint_not_found")
            eligible, restored, conflicts, incomplete = [], [], [], []
            for path, entry in manifest["paths"].items():
                if entry["before"]["exists"]:
                    try:
                        self._read_blob(entry["before"]["blob_ref"])
                    except FileNotFoundError:
                        raise EditCheckpointError("edit_checkpoint_blob_invalid") from None
                if entry["post"] is None:
                    incomplete.append(path)
                    continue
                current = self._workspace_state(path)
                if _same(current, entry["before"]):
                    restored.append(path)
                elif _same(current, entry["post"]):
                    eligible.append(path)
                else:
                    conflicts.append(path)
            return {
                "turn_key": key,
                "eligible": tuple(eligible),
                "already_restored": tuple(restored),
                "conflicts": tuple(conflicts),
                "incomplete": tuple(incomplete),
            }

    def read_before(self, turn_key, raw_path):
        """Return a verified before-image for a later platform-specific restorer."""
        self._require_roots()
        key, path = _turn_key(turn_key), _relative_path(raw_path)
        with file_lock.locked_file(self.lock, require_lock=True):
            manifest = self._load(key)
            if manifest is None or path not in manifest["paths"]:
                raise EditCheckpointError("edit_checkpoint_not_found")
            before = manifest["paths"][path]["before"]
            return self._read_blob(before["blob_ref"]) if before["exists"] else None

    def _workspace_state(self, path):
        return workspace_files.read_regular_bytes_anchored(
            self.workspace,
            path,
            max_bytes=self.max_file_bytes,
            expected_root_identity=self._workspace_identity,
        )

    def _require_roots(self):
        try:
            current = (
                private_files.private_directory_identity(self.root),
                private_files.private_directory_identity(self.workspace),
            )
        except (FileNotFoundError, OSError, ValueError):
            current = (None, None)
        if current != (self._root_identity, self._workspace_identity):
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe", "workspace or checkpoint root changed"
            )

    def _load(self, key):
        try:
            raw = private_files.read_private_bytes(
                self._manifest_path(key),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_MANIFEST_BYTES,
                harden=False,
            )
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EditCheckpointError("edit_checkpoint_invalid") from None
        if not _valid_manifest(value, key):
            raise EditCheckpointError("edit_checkpoint_invalid")
        return value

    def _save(self, manifest):
        raw = json.dumps(
            manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
        if len(raw) > MAX_MANIFEST_BYTES:
            raise EditCheckpointError("edit_checkpoint_too_large")
        private_files.write_private_bytes_atomic(
            self._manifest_path(manifest["turn_key"]),
            raw,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            max_existing_bytes=MAX_MANIFEST_BYTES,
        )

    def _write_blob(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self._blob_path(digest)
        private_files.ensure_private_dir(path.parent)
        try:
            current = self._read_blob(digest)
        except FileNotFoundError:
            private_files.write_private_bytes_atomic(
                path,
                data,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_existing_bytes=self.max_file_bytes,
            )
        else:
            if current != data:
                raise EditCheckpointError("edit_checkpoint_blob_invalid")
        return digest

    def _read_blob(self, digest):
        if not _digest(digest):
            raise EditCheckpointError("edit_checkpoint_blob_invalid")
        try:
            data = private_files.read_private_bytes(
                self.blobs / digest[:2] / digest,
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=self.max_file_bytes,
                harden=False,
            )
        except ValueError:
            raise EditCheckpointError("edit_checkpoint_blob_invalid") from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise EditCheckpointError("edit_checkpoint_blob_invalid")
        return data

    def _manifest_path(self, key):
        return self.turns / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _blob_path(self, digest):
        if not _digest(digest):
            raise EditCheckpointError("edit_checkpoint_blob_invalid")
        return self.blobs / digest[:2] / digest


def _manifest(key):
    return {"format_version": FORMAT_VERSION, "turn_key": key, "paths": {}}


def _turn_key(value):
    key = str(value)
    if not key or "\x00" in key or len(key.encode()) > MAX_TURN_KEY_BYTES:
        raise ValueError("invalid edit checkpoint turn key")
    return key


def _relative_path(value):
    return "/".join(workspace_files._workspace_relative_parts(value))


def _state(value, blob_ref=""):
    return {
        "exists": value["exists"],
        "sha256": value["sha256"],
        "blob_ref": blob_ref,
        "mode": value["mode"],
    }


def _same(current, recorded):
    return all(current[field] == recorded[field] for field in ("exists", "sha256", "mode"))


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _valid_state(value, before):
    if not isinstance(value, dict) or value.keys() != _STATE_FIELDS:
        return False
    if not value["exists"]:
        return value == {"exists": False, "sha256": "", "blob_ref": "", "mode": None}
    return (
        type(value["exists"]) is bool
        and _digest(value["sha256"])
        and type(value["mode"]) is int
        and value["mode"] == (value["mode"] & 0o7777)
        and (value["blob_ref"] == value["sha256"] if before else value["blob_ref"] == "")
    )


def _valid_manifest(value, key):
    if (
        not isinstance(value, dict)
        or value.keys() != _MANIFEST_FIELDS
        or value["format_version"] != FORMAT_VERSION
        or value["turn_key"] != key
        or not isinstance(value["paths"], dict)
    ):
        return False
    for path, entry in value["paths"].items():
        try:
            path_valid = _relative_path(path) == path
        except (TypeError, ValueError):
            path_valid = False
        if (
            not path_valid
            or not isinstance(entry, dict)
            or entry.keys() != _ENTRY_FIELDS
            or not _valid_state(entry["before"], True)
            or entry["post"] is not None
            and not _valid_state(entry["post"], False)
        ):
            return False
    return True
