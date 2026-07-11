import json

from pico.cli_session import inspect_session


def _payload(session_id, messages):
    return {
        "record_type": "session",
        "format_version": 1,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": "/repo",
        "messages": messages,
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }


def _write_session(root, session_id, messages):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".session_store.lock").touch(mode=0o600)
    (root / f"{session_id}.json").write_text(
        json.dumps(_payload(session_id, messages)),
        encoding="utf-8",
    )


def test_inspect_reports_schema_roles_blocks_pairs_and_meta(tmp_path):
    root = tmp_path / "sessions"
    messages = [
        {"role": "user", "content": "q", "_pico_meta": {"created_at": "t"}},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}}],
            "_pico_meta": {"created_at": "t"},
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "body"}],
            "_pico_meta": {"created_at": "t"},
        },
        {"role": "assistant", "content": "done", "_pico_meta": {"created_at": "t"}},
    ]
    _write_session(root, "s1", messages)
    ok, report = inspect_session("s1", root)
    assert ok is True
    assert "record_type: session" in report
    assert "format_version: 1" in report
    assert "messages: 4" in report
    assert "role_sequence: user -> assistant -> user -> assistant" in report
    assert "tool_pairs: 1" in report
    assert "orphans: 0" in report
    assert "invariants: ok" in report


def test_inspect_fails_on_orphan_without_consulting_history(tmp_path):
    root = tmp_path / "sessions"
    _write_session(root, "bad", [{
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}}],
        "_pico_meta": {},
    }])
    ok, report = inspect_session("bad", root)
    assert ok is False
    assert "failed to read session" in report.lower()


def test_inspect_fails_when_v3_contains_history(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "bad-history.json").write_text(
        json.dumps({**_payload("bad-history", []), "history": []}),
        encoding="utf-8",
    )
    ok, report = inspect_session("bad-history", root)
    assert ok is False
    assert "history" in report.lower()


def test_inspect_missing_session_returns_false(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    ok, report = inspect_session("nope", root)
    assert ok is False
    assert "not found" in report.lower() or "missing" in report.lower()


def test_inspect_handles_malformed_json_gracefully(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "bad.json").write_text("{not valid json", encoding="utf-8")
    ok, report = inspect_session("bad", root)
    assert ok is False
    assert "failed to read session" in report
