"""Session JSON persistence."""

from copy import deepcopy
import json
import logging
import tempfile
import time
import uuid
from pathlib import Path

from . import file_lock
from .messages import MessageValidationError, validate_messages

logger = logging.getLogger("pico")


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
            if isinstance(history, list) and history:
                selected = _history_to_messages(history)
            elif isinstance(messages, list) and not messages:
                selected = []
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


def _migrate_v1_to_v2(session: dict) -> dict:
    if session.get("schema_version", 1) >= 2:
        return session
    old_history = session.pop("history", [])
    messages = []
    for entry in old_history:
        role = entry.get("role")
        created_at = entry.get("created_at")
        if role == "tool":
            tool_use_id = f"toolu_migrated_{uuid.uuid4().hex[:12]}"
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": entry.get("name", ""),
                    "input": entry.get("args", {}),
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": entry.get("content", ""),
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
        elif role in ("user", "assistant"):
            messages.append({
                "role": role,
                "content": entry.get("content", ""),
                "_pico_meta": {"created_at": created_at},
            })
        else:
            logger.debug("session migrator: unknown role %r, skipping entry", role)
    session["messages"] = messages
    session.setdefault("recently_recalled", [])
    session["schema_version"] = 2
    return session


def _write_backup(session_path, raw_bytes, session_id):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Task A4: nanosecond precision prevents same-second filename collisions.
    ts = time.time_ns()
    (backup_dir / f"{session_id}.v1.{ts}.json").write_bytes(raw_bytes)


class SessionStore:
    def __init__(self, root, redactor=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".session_store.lock"
        self._redactor = redactor or _identity

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def path_for(self, session_id):
        return self.path(session_id)

    def save(self, session):
        path = self.path(session["id"])
        payload = self._redactor(session)
        with file_lock.locked_file(self.lock_path):
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                temp_name = handle.name
            Path(temp_name).replace(path)
        return path

    def load(self, session_id):
        p = self.path(session_id)
        raw = p.read_bytes()
        session = json.loads(raw.decode("utf-8"))
        if session.get("schema_version", 1) < 2:
            _write_backup(p, raw, session_id)
            session = _migrate_v1_to_v2(session)
            # 立即写回升级后的格式
            p.write_text(json.dumps(session), encoding="utf-8")
        return session

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
