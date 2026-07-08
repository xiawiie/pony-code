"""Read-only session inspector.

Task A5 replaces Finding 11's runtime dual-write assertion with a
CLI-driven static check. Users or CI run
``pico-cli session inspect <session_id>`` and get a report on whether
``session["history"]`` (legacy) and ``session["messages"]`` (v2) hold
consistent turn counts. Exit 0 on match, 1 on drift.

The tool is intentionally forgiving: it never mutates the session, never
raises on structural quirks, and reports mismatches in plain English so
an operator can decide whether the drift is intentional (e.g.,
mid-migration state) or a bug.
"""

from __future__ import annotations

import json
from pathlib import Path


def _count_role(items, role):
    """Count entries with ``role``. Accepts either flat dicts (history) or
    Anthropic-shape messages (v2) — the ``role`` key is the same in both."""
    return sum(1 for it in (items or []) if isinstance(it, dict) and it.get("role") == role)


def inspect_session(session_id, sessions_root):
    """Return ``(ok, report_str)`` for the named session.

    ``ok`` is True iff:
    - session file exists
    - user-turn count in ``history`` equals user-turn count in ``messages``
      (a light dual-write invariant that catches obvious drift)

    The report is human-readable multi-line text — no JSON, no colors.
    Users pipe it into their preferred filter.
    """
    sessions_root = Path(sessions_root)
    path = sessions_root / f"{session_id}.json"
    if not path.exists():
        return False, f"session not found: {path}"

    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"failed to read session {session_id}: {exc}"

    history = session.get("history", []) or []
    messages = session.get("messages", []) or []
    hist_user = _count_role(history, "user")
    hist_asst = _count_role(history, "assistant")
    msg_user = _count_role(messages, "user")
    msg_asst = _count_role(messages, "assistant")

    lines = [
        f"session: {session_id}",
        f"schema_version: {session.get('schema_version', 'unknown')}",
        f"history: user={hist_user}, assistant={hist_asst}, total={len(history)}",
        f"messages: user={msg_user}, assistant={msg_asst}, total={len(messages)}",
    ]

    ok = True
    if hist_user != msg_user:
        ok = False
        lines.append(
            f"MISMATCH: history has {hist_user} user turns, "
            f"messages has {msg_user}"
        )
    else:
        lines.append("user-turn count: match")

    return ok, "\n".join(lines)


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
