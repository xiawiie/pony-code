"""Strict current-session JSON persistence."""

from copy import deepcopy
import json
import re

from . import file_lock
from .messages import MessageValidationError, validate_messages
from .security import (
    ensure_private_file,
    harden_private_tree,
    private_directory_identity,
    read_private_bytes,
    require_regular_no_symlink,
    write_private_bytes_atomic,
)


SESSION_RECORD_TYPE = "session"
SESSION_FORMAT_VERSION = 1


class SessionMigrationError(ValueError):
    """A session record does not match the current on-disk contract."""


def _identity(value):
    return value


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUIRED_FIELDS = frozenset(
    {
        "record_type",
        "format_version",
        "id",
        "created_at",
        "workspace_root",
        "messages",
        "working_memory",
        "memory",
        "recently_recalled",
        "checkpoints",
        "resume_state",
        "recovery",
        "runtime_identity",
    }
)
_DICT_FIELDS = (
    "working_memory",
    "memory",
    "checkpoints",
    "resume_state",
    "recovery",
    "runtime_identity",
)


def _session_id(value):
    session_id = value if isinstance(value, str) else ""
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid session id")
    return session_id


def _decode_json_object(raw):
    def object_from_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SessionMigrationError("duplicate session key")
            value[key] = item
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_from_pairs)
    except SessionMigrationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SessionMigrationError("failed to decode session") from None


def _validate_payload(payload, session_id):
    if not isinstance(payload, dict):
        raise SessionMigrationError("session payload must be an object")
    if not _REQUIRED_FIELDS <= payload.keys():
        raise SessionMigrationError("session payload missing required fields")
    if "history" in payload or "schema_version" in payload:
        raise SessionMigrationError("session payload contains obsolete fields")
    if payload.get("record_type") != SESSION_RECORD_TYPE:
        raise SessionMigrationError("invalid session record type")
    if (
        type(payload.get("format_version")) is not int
        or payload["format_version"] != SESSION_FORMAT_VERSION
    ):
        raise SessionMigrationError("invalid session format version")
    if payload.get("id") != session_id:
        raise SessionMigrationError("session id does not match file name")
    if any(not isinstance(payload.get(key), str) for key in ("id", "created_at", "workspace_root")):
        raise SessionMigrationError("invalid session string field")
    if any(not isinstance(payload.get(key), dict) for key in _DICT_FIELDS):
        raise SessionMigrationError("invalid session object field")
    if not isinstance(payload.get("recently_recalled"), list):
        raise SessionMigrationError("invalid session list field")
    identities = [payload["runtime_identity"]]
    items = payload["checkpoints"].get("items", {})
    if isinstance(items, dict):
        for checkpoint in items.values():
            if not isinstance(checkpoint, dict):
                raise SessionMigrationError("invalid embedded checkpoint")
            if "schema_version" in checkpoint:
                raise SessionMigrationError("obsolete embedded checkpoint version")
            identity = checkpoint.get("runtime_identity")
            if isinstance(identity, dict):
                identities.append(identity)
    if any(
        "prompt_cache" in identity.get("feature_flags", {})
        for identity in identities
        if isinstance(identity.get("feature_flags", {}), dict)
    ):
        raise SessionMigrationError("obsolete runtime identity flag")
    try:
        validate_messages(payload.get("messages"), require_meta=True)
    except MessageValidationError as exc:
        raise SessionMigrationError(str(exc)) from None
    return payload


def _atomic_write_locked(path, payload, root, root_identity):
    rendered = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    write_private_bytes_atomic(
        path,
        rendered,
        trusted_root=root,
        trusted_root_identity=root_identity,
        error="session temp changed",
    )


class SessionStore:
    def __init__(self, root, redactor=None):
        self.root = harden_private_tree(root)
        self._root_identity = private_directory_identity(self.root)
        self.lock_path = self.root / ".session_store.lock"
        self._redactor = redactor or _identity
        self._redactor_configured = redactor is not None

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity
        self._redactor_configured = redactor is not None

    def path(self, session_id):
        return self.root / f"{_session_id(session_id)}.json"

    def path_for(self, session_id):
        return self.path(session_id)

    def save(self, session):
        if not isinstance(session, dict):
            raise SessionMigrationError("session payload must be an object")
        session_id = _session_id(session.get("id"))
        payload = self._redactor(deepcopy(session))
        _validate_payload(payload, session_id)
        path = self.path(session_id)
        with file_lock.locked_file(self.lock_path):
            _atomic_write_locked(path, payload, self.root, self._root_identity)
        return path

    def load(self, session_id):
        session_id = _session_id(session_id)
        with file_lock.locked_file(self.lock_path, require_existing=True):
            return self._load_unlocked(session_id)

    def _load_unlocked(self, session_id):
        session_id = _session_id(session_id)
        raw = read_private_bytes(
            self.path(session_id),
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
        )
        payload = _decode_json_object(raw)
        _validate_payload(payload, session_id)
        return payload

    def latest(self):
        files = []
        for path in self.root.glob("*.json"):
            try:
                files.append(ensure_private_file(require_regular_no_symlink(path)))
            except (OSError, ValueError):
                continue
        files.sort(key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
