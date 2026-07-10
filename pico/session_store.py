"""Session JSON persistence."""

from copy import deepcopy
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from . import file_lock
from .messages import MessageValidationError, validate_messages

class SessionMigrationError(ValueError):
    """A legacy session cannot be converted without losing transcript data."""


def _history_to_messages(history):
    if not isinstance(history, list):
        raise SessionMigrationError("history must be a list")
    messages = []
    for entry in history:
        if not isinstance(entry, dict):
            raise SessionMigrationError("history entry must be an object")
        role = entry.get("role")
        created_at = entry.get("created_at")
        if not isinstance(role, str):
            raise SessionMigrationError(f"unknown history role: {role!r}")
        if role in {"user", "assistant"}:
            content = entry.get("content")
            if not isinstance(content, str):
                raise SessionMigrationError("plain history content must be text")
            messages.append({
                "role": role,
                "content": content,
                "_pico_meta": {"created_at": created_at} if created_at else {},
            })
            continue
        if role == "tool":
            name = entry.get("name")
            arguments = entry.get("args", {})
            content = entry.get("content")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
                or not isinstance(content, str)
            ):
                raise SessionMigrationError("invalid tool history entry")
            tool_use_id = f"toolu_migrated_{uuid.uuid4().hex[:12]}"
            meta = {"tool_use_id": tool_use_id}
            if created_at:
                meta["created_at"] = created_at
            messages.extend([
                {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": name,
                        "input": dict(arguments),
                    }],
                    "_pico_meta": dict(meta),
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }],
                    "_pico_meta": dict(meta),
                },
            ])
            continue
        raise SessionMigrationError(f"unknown history role: {role!r}")
    return messages


def _normalized_messages(messages):
    normalized = deepcopy(messages)
    if isinstance(normalized, list):
        for message in normalized:
            if isinstance(message, dict):
                message.setdefault("_pico_meta", {})
    validate_messages(normalized, require_meta=True)
    return normalized


def migrate_session_to_v3(session):
    if not isinstance(session, dict):
        raise SessionMigrationError("session must be an object")
    migrated = deepcopy(session)
    raw_version = migrated.get("schema_version", 1)
    if (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, (int, float, str))
        or (isinstance(raw_version, float) and not raw_version.is_integer())
    ):
        raise SessionMigrationError("invalid session schema version")
    try:
        version = int(raw_version)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SessionMigrationError("invalid session schema version") from exc
    if version not in {1, 2, 3}:
        raise SessionMigrationError(
            f"unsupported session schema version: {version}"
        )
    history = migrated.get("history", [])

    if version == 3:
        if type(raw_version) is not int or raw_version != 3:
            raise SessionMigrationError("invalid session schema version")
        if "history" in migrated:
            raise SessionMigrationError("v3 session must not contain history")
        try:
            validate_messages(migrated.get("messages"), require_meta=True)
        except MessageValidationError as exc:
            raise SessionMigrationError(str(exc)) from exc
        return migrated

    if version == 1:
        selected = _history_to_messages(history)
    else:
        messages = migrated.get("messages")
        selected = None
        if isinstance(messages, list) and messages:
            try:
                selected = _normalized_messages(messages)
            except MessageValidationError:
                selected = None
        if selected is None:
            if isinstance(messages, list) and not messages:
                selected = _history_to_messages(history)
            elif isinstance(history, list) and history:
                selected = _history_to_messages(history)
            else:
                raise SessionMigrationError("session has no valid transcript")
    try:
        validate_messages(selected, require_meta=True)
    except MessageValidationError as exc:
        raise SessionMigrationError(str(exc)) from exc

    migrated["messages"] = selected
    migrated.pop("history", None)
    migrated["schema_version"] = 3
    return migrated


def _identity(value):
    return value


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _session_id(value):
    session_id = str(value or "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id


def _validate_v3_payload(payload, session_id):
    if not isinstance(payload, dict):
        raise SessionMigrationError("session payload must be an object")
    if payload.get("id") != session_id:
        raise SessionMigrationError("session id does not match file name")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 3:
        raise SessionMigrationError("invalid session schema version")
    if "history" in payload:
        raise SessionMigrationError("v3 session must not contain history")
    try:
        validate_messages(payload.get("messages"), require_meta=True)
    except MessageValidationError as exc:
        raise SessionMigrationError(str(exc)) from exc


def _prepare_v3_payload(value, session_id):
    payload = deepcopy(value)
    if not isinstance(payload, dict):
        raise SessionMigrationError("session payload must be an object")
    payload.pop("history", None)
    _validate_v3_payload(payload, session_id)
    return payload


def _atomic_write_locked(path, payload):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_backup_locked(session_path, raw_bytes, session_id, source_version):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        f"{session_id}.v{source_version}.{time.time_ns()}."
        f"{uuid.uuid4().hex}.json"
    )
    with backup_path.open("xb") as handle:
        handle.write(raw_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    return backup_path


class SessionStore:
    def __init__(self, root, redactor=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".session_store.lock"
        self._redactor = redactor or _identity

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity

    def path(self, session_id):
        session_id = _session_id(session_id)
        return self.root / f"{session_id}.json"

    def path_for(self, session_id):
        return self.path(_session_id(session_id))

    def save(self, session):
        session_id = _session_id(session["id"])
        path = self.path(session_id)
        try:
            source_version = int(session.get("schema_version", 1) or 1)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SessionMigrationError("invalid session schema version") from exc
        if source_version == 3:
            payload = _prepare_v3_payload(self._redactor(session), session_id)
        else:
            payload = deepcopy(self._redactor(session))
        with file_lock.locked_file(self.lock_path):
            _atomic_write_locked(path, payload)
        return path

    def load(self, session_id):
        session_id = _session_id(session_id)
        path = self.path(session_id)
        with file_lock.locked_file(self.lock_path):
            raw = path.read_bytes()
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionMigrationError(
                    f"failed to decode session {session_id}"
                ) from exc
            if not isinstance(decoded, dict) or decoded.get("id") != session_id:
                raise SessionMigrationError("session id does not match file name")
            try:
                version = int(decoded.get("schema_version", 1) or 1)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SessionMigrationError(
                    "invalid session schema version"
                ) from exc
            migrated = migrate_session_to_v3(decoded)
            if version == 3:
                _validate_v3_payload(migrated, session_id)
                return migrated
            payload = _prepare_v3_payload(self._redactor(migrated), session_id)
            _write_backup_locked(path, raw, session_id, version)
            _atomic_write_locked(path, payload)
            return payload

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
