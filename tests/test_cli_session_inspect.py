"""Task A5: pico-cli session inspect flags dual-write drift."""

import json

from pico.cli_session import inspect_session


def _write_session(sessions_root, session_id, session_dict):
    sessions_root.mkdir(parents=True, exist_ok=True)
    path = sessions_root / f"{session_id}.json"
    path.write_text(json.dumps(session_dict), encoding="utf-8")


def test_inspect_matches_when_history_and_messages_align(tmp_path):
    """User turn count in history == count in messages → OK."""
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", {
        "id": "s1",
        "workspace_root": str(tmp_path),
        "schema_version": 2,
        "history": [
            {"role": "user", "content": "q", "created_at": "t"},
            {"role": "assistant", "content": "a", "created_at": "t"},
        ],
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ],
    })
    ok, report = inspect_session("s1", sessions_root=sessions)
    assert ok is True
    assert "match" in report.lower() or "ok" in report.lower()


def test_inspect_flags_user_count_mismatch(tmp_path):
    """history has 2 user turns; messages has 1 → mismatch."""
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s2", {
        "id": "s2",
        "workspace_root": str(tmp_path),
        "schema_version": 2,
        "history": [
            {"role": "user", "content": "q1"},
            {"role": "user", "content": "q2"},
        ],
        "messages": [
            {"role": "user", "content": "q1"},
        ],
    })
    ok, report = inspect_session("s2", sessions_root=sessions)
    assert ok is False
    assert "user" in report.lower()
    assert "2" in report and "1" in report


def test_inspect_missing_session_returns_false(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    ok, report = inspect_session("nope", sessions_root=sessions)
    assert ok is False
    assert "not found" in report.lower() or "missing" in report.lower()


def test_inspect_handles_malformed_json_gracefully(tmp_path):
    """Corrupt JSON returns (False, err) rather than raising."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "bad.json").write_text("{not valid json", encoding="utf-8")
    ok, report = inspect_session("bad", sessions_root=sessions)
    assert ok is False
    assert "failed to read session" in report


def test_inspect_ignores_tool_result_carriers(tmp_path):
    """v2 tool_result carrier messages must not count as human user turns.

    A real session with 1 human turn + 2 tool loops produces:
      - history: 1 user entry (the human turn only)
      - messages: 1 human user + 2 tool_result-carrier user messages
    The inspector must treat that as a match, not drift.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s3", {
        "id": "s3",
        "workspace_root": str(tmp_path),
        "schema_version": 2,
        "history": [
            {"role": "user", "content": "draw a plot", "created_at": "t"},
            {"role": "assistant", "content": "done", "created_at": "t"},
        ],
        "messages": [
            {"role": "user", "content": "draw a plot"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "run", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_2", "name": "run", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_2", "content": "ok"},
                ],
            },
            {"role": "assistant", "content": "done"},
        ],
    })
    ok, report = inspect_session("s3", sessions_root=sessions)
    assert ok is True, report
    assert "match" in report.lower()
