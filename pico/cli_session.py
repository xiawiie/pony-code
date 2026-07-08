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

Counting rule
-------------
The v2 ``messages`` shape stores tool-loop turns as *two* records: an
``assistant`` message carrying the ``tool_use`` block, and a follow-up
``user`` message carrying the matching ``tool_result`` block(s). Those
tool_result "carriers" are transport artifacts, not human turns, so the
legacy ``history`` list never contains them. To keep the dual-write
invariant meaningful, :func:`_count_role` **skips v2 tool_result
carriers when counting user turns**: a ``role="user"`` message whose
``content`` is a list containing any ``{"type": "tool_result"}`` block
is excluded. Plain-string user content and ordinary block-list user
content are still counted.
"""

from __future__ import annotations

import json
from pathlib import Path


def _is_tool_result_carrier(item):
    """True if ``item`` is a v2 user message that carries tool_result blocks.

    Such messages exist purely to feed tool outputs back to the model; a
    single human turn with N tool loops produces 1 real user message plus
    N carriers. Legacy ``history`` never stores carriers, so they must be
    excluded before comparing user-turn counts across the two shapes.
    """
    if not isinstance(item, dict) or item.get("role") != "user":
        return False
    content = item.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _count_role(items, role):
    """Count entries with ``role``. Accepts either flat dicts (history) or
    Anthropic-shape messages (v2) — the ``role`` key is the same in both.

    For ``role="user"``, v2 tool_result carriers are skipped so the count
    reflects human turns only (see module docstring for rationale).
    """
    count = 0
    for it in (items or []):
        if not isinstance(it, dict) or it.get("role") != role:
            continue
        if role == "user" and _is_tool_result_carrier(it):
            continue
        count += 1
    return count


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
