"""Read-only session inspector for canonical message transcripts."""

from __future__ import annotations

from pathlib import Path
import re

from pico import file_lock
from pico.messages import MessageValidationError, validate_messages
from pico.security import private_directory_identity
from pico.session_store import (
    SESSION_FORMAT_VERSION,
    SESSION_RECORD_TYPE,
    SessionMigrationError,
    SessionStore,
)


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _pair_count(messages):
    return sum(
        1
        for message in messages
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_use"
    )


def inspect_session(session_id, sessions_root):
    """Return ``(ok, report_str)`` for the named session.

    The report is human-readable multi-line text — no JSON, no colors.
    """
    session_id = str(session_id or "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        return False, f"session not found: {session_id}"
    sessions_root = Path(sessions_root)
    path = sessions_root / f"{session_id}.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return False, f"session not found: {session_id}"
    except OSError:
        return False, f"failed to read session {session_id}: unsafe session artifact"
    try:
        store = object.__new__(SessionStore)
        store.root = sessions_root
        store._root_identity = private_directory_identity(sessions_root)
        store.lock_path = sessions_root / ".session_store.lock"
        with file_lock.locked_file(store.lock_path, require_existing=True):
            session = store._load_unlocked(session_id)
    except FileNotFoundError:
        return False, f"failed to read session {session_id}: unsafe session artifact"
    except (OSError, ValueError, SessionMigrationError):
        return False, f"failed to read session {session_id}: unsafe session artifact"

    if not isinstance(session, dict):
        return False, f"session: {session_id}\ninvariants: failed (session must be an object)"
    record_type = session.get("record_type")
    version = session.get("format_version")
    valid_version = (
        record_type == SESSION_RECORD_TYPE
        and type(version) is int
        and version == SESSION_FORMAT_VERSION
    )
    messages = session.get("messages")
    lines = [
        f"session: {session_id}",
        f"record_type: {record_type if record_type == SESSION_RECORD_TYPE else 'invalid'}",
        f"format_version: {version if valid_version else 'invalid'}",
        f"messages: {len(messages) if isinstance(messages, list) else 0}",
        "role_sequence: " + (
            " -> ".join(
                role if (role := message.get("role")) in {"user", "assistant"} else "?"
                for message in messages
                if isinstance(message, dict)
            )
            if isinstance(messages, list)
            else "invalid"
        ),
    ]
    if not valid_version:
        lines.extend([
            "tool_pairs: 0",
            "orphans: unknown",
            "invariants: failed (invalid schema version)",
        ])
        return False, "\n".join(lines)
    try:
        validate_messages(messages, require_meta=True)
    except (MessageValidationError, SessionMigrationError) as exc:
        lines.extend(["tool_pairs: 0", "orphans: 1", f"invariants: failed ({exc})"])
        return False, "\n".join(lines)
    lines.extend([
        f"tool_pairs: {_pair_count(messages)}",
        "orphans: 0",
        "invariants: ok",
    ])
    return True, "\n".join(lines)


def handle_session_command(argv, sessions_root=None, redactor=None):
    """CLI entry point: `pico session inspect <session_id>`.

    Returns an exit code (0 or 1). Prints the report to stdout.
    """
    if len(argv) < 2 or argv[0] != "inspect":
        print("usage: pico session inspect <session_id>")
        return 2
    session_id = argv[1]
    if sessions_root is None:
        sessions_root = Path.cwd() / ".pico" / "sessions"
    ok, report = inspect_session(session_id, sessions_root=sessions_root)
    if redactor is not None:
        report = redactor(report)
    print(report)
    return 0 if ok else 1
