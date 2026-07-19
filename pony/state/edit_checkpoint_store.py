"""Small append-only checkpoints for Pony-owned workspace edits."""

import hashlib
import json
import os
from pathlib import Path

from pony.security import private_files, workspace_files

from . import file_lock


FORMAT_VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_TURN_KEY_BYTES = 256
_EVENT_FIELDS = {
    "record_type",
    "format_version",
    "turn_key",
    "phase",
    "path",
    "exists",
    "sha256",
    "blob_ref",
    "mode",
}


class EditCheckpointError(ValueError):
    """Stable fail-closed edit checkpoint error."""

    def __init__(self, code, *, paths=()):
        self.code = str(code)
        self.paths = tuple(paths)
        super().__init__(self.code)


class EditCheckpointStore:
    """Capture first before-images and CAS-restore one top-level turn."""

    def __init__(self, state_root, workspace_root, *, max_file_bytes=MAX_FILE_BYTES):
        self.root = private_files.ensure_private_dir(state_root)
        self.turns_dir = private_files.ensure_private_dir(self.root / "turns")
        self.blobs_dir = private_files.ensure_private_dir(self.root / "blobs")
        self.lock_path = self.root / ".store.lock"
        self.workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
        self.max_file_bytes = int(max_file_bytes)
        if self.max_file_bytes < 0:
            raise ValueError("invalid edit checkpoint file limit")
        self._root_identity = private_files.private_directory_identity(self.root)
        self._workspace_identity = private_files.private_directory_identity(
            self.workspace_root
        )

    def capture_before(self, turn_key, raw_path):
        """Capture a path once for a turn, before an Edit or Write runs."""
        key = _turn_key(turn_key)
        path = _relative_path(raw_path)
        with file_lock.locked_file(self.lock_path, require_lock=True):
            checkpoint = self._checkpoint(key)
            if path in checkpoint:
                return dict(checkpoint[path]["before"])
            state = self._read_workspace(path)
            blob_ref = self._write_blob(state["data"]) if state["exists"] else ""
            event = _event(key, "before", path, state, blob_ref=blob_ref)
            self._append(key, event)
            return dict(event)

    def record_post(self, turn_key, raw_path):
        """Record the current digest after a successful Pony-owned edit."""
        key = _turn_key(turn_key)
        path = _relative_path(raw_path)
        with file_lock.locked_file(self.lock_path, require_lock=True):
            if path not in self._checkpoint(key):
                raise EditCheckpointError("edit_checkpoint_before_missing")
            event = _event(key, "post", path, self._read_workspace(path))
            self._append(key, event)
            return dict(event)

    def restore(self, turn_key):
        """Restore all captured paths after a full conflict preflight."""
        key = _turn_key(turn_key)
        with file_lock.locked_file(self.lock_path, require_lock=True):
            checkpoint = self._checkpoint(key)
            if not checkpoint:
                raise EditCheckpointError("edit_checkpoint_not_found")
            operations = self._preflight(checkpoint)
            for operation in reversed(operations):
                self._restore_path(operation)
            return {"turn_key": key, "paths": tuple(checkpoint)}

    def _preflight(self, checkpoint):
        conflicts = []
        incomplete = []
        operations = []
        for path, states in checkpoint.items():
            post = states["post"]
            if post is None:
                incomplete.append(path)
                continue
            current = self._read_workspace(path)
            if not _same_state(current, post):
                conflicts.append(path)
                continue
            before = states["before"]
            try:
                data = self._read_blob(before["blob_ref"]) if before["exists"] else None
            except FileNotFoundError:
                raise EditCheckpointError("edit_checkpoint_blob_invalid") from None
            operations.append((path, before, post, data))
        if incomplete:
            raise EditCheckpointError("edit_checkpoint_incomplete", paths=incomplete)
        if conflicts:
            raise EditCheckpointError("edit_checkpoint_conflict", paths=conflicts)
        return operations

    def _restore_path(self, operation):
        path, before, post, data = operation
        if before["exists"]:
            workspace_files.write_regular_bytes_anchored_atomic(
                self.workspace_root,
                path,
                data,
                max_bytes=self.max_file_bytes,
                expected_sha256=post["sha256"] if post["exists"] else None,
                expected_missing=not post["exists"],
                mode=before["mode"],
                expected_root_identity=self._workspace_identity,
            )
        elif post["exists"]:
            workspace_files.remove_regular_file_anchored(
                self.workspace_root,
                path,
                max_bytes=self.max_file_bytes,
                expected_sha256=post["sha256"],
                expected_root_identity=self._workspace_identity,
            )

    def _read_workspace(self, path):
        return workspace_files.read_regular_bytes_anchored(
            self.workspace_root,
            path,
            max_bytes=self.max_file_bytes,
            expected_root_identity=self._workspace_identity,
        )

    def _checkpoint(self, key):
        checkpoint = {}
        for event in self._events(key):
            path = event["path"]
            states = checkpoint.setdefault(path, {"before": None, "post": None})
            if event["phase"] == "before":
                if states["before"] is not None:
                    raise EditCheckpointError("edit_checkpoint_invalid")
                states["before"] = event
            elif states["before"] is None:
                raise EditCheckpointError("edit_checkpoint_invalid")
            else:
                states["post"] = event
        return checkpoint

    def _events(self, key):
        try:
            raw = private_files.read_private_bytes(
                self._ledger_path(key),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_LEDGER_BYTES,
                harden=False,
            )
        except FileNotFoundError:
            return ()
        try:
            events = tuple(json.loads(line) for line in raw.splitlines())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EditCheckpointError("edit_checkpoint_invalid") from None
        if not events or any(not _valid_event(event, key) for event in events):
            raise EditCheckpointError("edit_checkpoint_invalid")
        return events

    def _append(self, key, event):
        raw = json.dumps(
            event,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii") + b"\n"
        private_files.append_private_bytes(
            self._ledger_path(key),
            raw,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            max_total_bytes=MAX_LEDGER_BYTES,
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

    def _read_blob(self, blob_ref):
        try:
            data = private_files.read_private_bytes(
                self._blob_path(blob_ref),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=self.max_file_bytes,
                harden=False,
            )
        except ValueError:
            raise EditCheckpointError("edit_checkpoint_blob_invalid") from None
        if hashlib.sha256(data).hexdigest() != blob_ref:
            raise EditCheckpointError("edit_checkpoint_blob_invalid")
        return data

    def _ledger_path(self, key):
        return self.turns_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.jsonl"

    def _blob_path(self, digest):
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise EditCheckpointError("edit_checkpoint_blob_invalid")
        return self.blobs_dir / digest[:2] / digest


def _turn_key(value):
    key = str(value)
    size = len(key.encode("utf-8"))
    if not key or "\x00" in key or size > MAX_TURN_KEY_BYTES:
        raise ValueError("invalid edit checkpoint turn key")
    return key


def _relative_path(value):
    return "/".join(workspace_files._workspace_relative_parts(value))


def _event(key, phase, path, state, *, blob_ref=""):
    return {
        "record_type": "edit_checkpoint",
        "format_version": FORMAT_VERSION,
        "turn_key": key,
        "phase": phase,
        "path": path,
        "exists": state["exists"],
        "sha256": state["sha256"],
        "blob_ref": blob_ref,
        "mode": state["mode"],
    }


def _same_state(current, recorded):
    return (
        current["exists"] == recorded["exists"]
        and current["sha256"] == recorded["sha256"]
        and current["mode"] == recorded["mode"]
    )


def _valid_event(event, key):
    if not isinstance(event, dict) or event.keys() != _EVENT_FIELDS:
        return False
    if (
        event["record_type"] != "edit_checkpoint"
        or event["format_version"] != FORMAT_VERSION
        or event["turn_key"] != key
        or event["phase"] not in {"before", "post"}
        or type(event["exists"]) is not bool
    ):
        return False
    try:
        if _relative_path(event["path"]) != event["path"]:
            return False
    except (TypeError, ValueError):
        return False
    if not event["exists"]:
        return event["sha256"] == event["blob_ref"] == "" and event["mode"] is None
    digest = event["sha256"]
    return (
        len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
        and type(event["mode"]) is int
        and event["mode"] >= 0
        and event["mode"] == (event["mode"] & 0o7777)
        and (
            event["blob_ref"] == digest
            if event["phase"] == "before"
            else event["blob_ref"] == ""
        )
    )
