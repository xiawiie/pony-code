"""Read-only session inspector for canonical message transcripts."""

from __future__ import annotations

import json
from pathlib import Path

from pico.messages import MessageValidationError, validate_messages


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
    sessions_root = Path(sessions_root)
    path = sessions_root / f"{session_id}.json"
    if not path.exists():
        return False, f"session not found: {path}"

    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"failed to read session {session_id}: {exc}"

    version = session.get("schema_version", "unknown")
    messages = session.get("messages")
    lines = [
        f"session: {session_id}",
        f"schema_version: {version}",
        f"messages: {len(messages) if isinstance(messages, list) else 0}",
        "role_sequence: " + (
            " -> ".join(
                str(message.get("role", "?"))
                for message in messages
                if isinstance(message, dict)
            )
            if isinstance(messages, list)
            else "invalid"
        ),
    ]
    if version == 3 and "history" in session:
        lines.extend(["tool_pairs: 0", "orphans: unknown", "invariants: failed (v3 contains history)"])
        return False, "\n".join(lines)
    try:
        validate_messages(messages, require_meta=version == 3)
    except MessageValidationError as exc:
        lines.extend(["tool_pairs: 0", "orphans: 1", f"invariants: failed ({exc})"])
        return False, "\n".join(lines)
    lines.extend([
        f"tool_pairs: {_pair_count(messages)}",
        "orphans: 0",
        "invariants: ok",
    ])
    return True, "\n".join(lines)


def handle_session_command(argv, sessions_root=None):
    """CLI entry point: `pico-cli session inspect <session_id>`.

    Returns an exit code (0 or 1). Prints the report to stdout.
    """
    if len(argv) < 2 or argv[0] != "inspect":
        print("usage: pico-cli session inspect <session_id>")
        return 2
    session_id = argv[1]
    if sessions_root is None:
        sessions_root = Path.cwd() / ".pico" / "sessions"
    ok, report = inspect_session(session_id, sessions_root=sessions_root)
    print(report)
    return 0 if ok else 1
