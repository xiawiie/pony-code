import json
import os

from pony.cli.session import inspect_session
from pony.state.session_store import SessionStore


def _payload(workspace, session_id, messages, *, version=1):
    return {
        "record_type": "session",
        "format_version": version,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(workspace),
        "messages": messages,
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }


def _write_legacy(root, workspace, session_id, messages):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    lock = root / ".session_store.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    path = root / f"{session_id}.json"
    path.write_text(
        json.dumps(_payload(workspace, session_id, messages)),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _tool_messages():
    return [
        {"role": "user", "content": "q", "_pony_meta": {"created_at": "t"}},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                }
            ],
            "_pony_meta": {"created_at": "t"},
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "body",
                }
            ],
            "_pony_meta": {"created_at": "t"},
        },
        {"role": "assistant", "content": "done", "_pony_meta": {"created_at": "t"}},
    ]


def test_inspect_reports_current_tree_without_mutating_it(tmp_path):
    root = tmp_path / "sessions"
    store = SessionStore(root)
    session = _payload(tmp_path, "s1", _tool_messages(), version=2)
    path = store.save(session)
    original = path.read_bytes()

    ok, report = inspect_session("s1", root)

    assert ok is True
    assert path.read_bytes() == original
    assert "storage: current" in report
    assert "format_version: 2" in report
    assert "messages: 4" in report
    assert "role_sequence: user -> assistant -> user -> assistant" in report
    assert "entries: 4" in report
    assert "active_path_entries: 4" in report
    assert "tool_pairs: 1" in report
    assert "invariants: ok" in report


def test_inspect_legacy_reports_pending_migration_without_migrating(tmp_path):
    root = tmp_path / "sessions"
    legacy = _write_legacy(root, tmp_path, "legacy", _tool_messages())
    original = legacy.read_bytes()

    ok, report = inspect_session("legacy", root)

    assert ok is True
    assert legacy.read_bytes() == original
    assert not (root / "legacy.jsonl").exists()
    assert "storage: legacy" in report
    assert "format_version: 1" in report
    assert "migration: required on explicit resume" in report
    assert "entries: 0" in report


def test_inspect_reports_branch_facts(tmp_path):
    root = tmp_path / "sessions"
    store = SessionStore(root)
    store.save(_payload(tmp_path, "branch", _tool_messages(), version=2))
    first_message = next(
        entry for entry in store.entries("branch") if entry["type"] == "message"
    )
    fork = store.fork("branch", first_message["id"])
    store.append_control(
        "branch",
        "compaction",
        {
            "summary": "short",
            "first_kept_entry_id": first_message["id"],
            "tokens_before": 10,
            "summary_tokens": 2,
            "tail_tokens": 4,
            "reason": "test",
        },
        parent_id=fork["id"],
    )

    ok, report = inspect_session("branch", root)

    assert ok is True
    assert "branch_points: 1" in report
    assert "compactions: 1" in report


def test_inspect_fails_on_orphan_without_consulting_history(tmp_path):
    root = tmp_path / "sessions"
    _write_legacy(
        root,
        tmp_path,
        "bad",
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "read_file",
                        "input": {},
                    }
                ],
                "_pony_meta": {},
            }
        ],
    )
    ok, report = inspect_session("bad", root)
    assert ok is False
    assert "failed to read session" in report.lower()


def test_inspect_fails_when_legacy_contains_unknown_field(tmp_path):
    root = tmp_path / "sessions"
    path = _write_legacy(root, tmp_path, "bad-history", [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["history"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    ok, report = inspect_session("bad-history", root)

    assert ok is False
    assert "failed to read session" in report.lower()


def test_inspect_missing_session_returns_false(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    os.chmod(root, 0o700)
    ok, report = inspect_session("nope", root)
    assert ok is False
    assert "not found" in report.lower()


def test_inspect_handles_malformed_json_gracefully(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    lock = root / ".session_store.lock"
    lock.touch(mode=0o600)
    path = root / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    path.chmod(0o600)

    ok, report = inspect_session("bad", root)

    assert ok is False
    assert "failed to read session" in report
