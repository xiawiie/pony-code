import json
import os

from pony.cli.app import main
from pony.cli.session import (
    handle_session_command,
    inspect_session,
    resolve_session_id_readonly,
)
from pony.state.session_store import PREVIOUS_SESSION_FORMAT_VERSION, SessionStore


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
        **(
            {
                "workflow_mode": "act",
                "active_plan": {"goal": "", "items": []},
            }
            if version == 3
            else {}
        ),
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


def _rewrite_as_v2(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["format_version"] = PREVIOUS_SESSION_FORMAT_VERSION
        if row.get("type") == "session_info":
            row["data"]["set"]["format_version"] = PREVIOUS_SESSION_FORMAT_VERSION
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


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
    session = _payload(tmp_path, "s1", _tool_messages(), version=3)
    path = store.save(session)
    original = path.read_bytes()

    ok, report = inspect_session("s1", root)

    assert ok is True
    assert path.read_bytes() == original
    assert "storage: current" in report
    assert "format_version: 3" in report
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


def test_inspect_legacy_preserves_file_identity_and_permissions(tmp_path):
    root = tmp_path / "sessions"
    legacy = _write_legacy(root, tmp_path, "readonly-v1", _tool_messages())
    legacy.chmod(0o644)
    before = legacy.stat()
    original = legacy.read_bytes()

    ok, _report = inspect_session("readonly-v1", root)

    after = legacy.stat()
    assert ok is False
    assert legacy.read_bytes() == original
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )


def test_inspect_v2_and_tree_are_read_only(tmp_path, capsys):
    root = tmp_path / "sessions"
    store = SessionStore(root)
    path = store.save(_payload(tmp_path, "v2", _tool_messages(), version=3))
    _rewrite_as_v2(path)
    before = path.stat()
    original = path.read_bytes()

    ok, report = inspect_session("v2", root)
    tree_code = handle_session_command(["tree", "v2"], sessions_root=root)

    after = path.stat()
    assert ok is True
    assert tree_code == 0
    assert "format_version: 2" in report
    assert "migration: required on explicit resume" in report
    assert "workflow_mode: act" in report
    assert "format_version: 2" in capsys.readouterr().out
    assert path.read_bytes() == original
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not store.candidate_path("v2").exists()
    assert not (root / "legacy-backups").exists()


def test_cli_inspect_latest_returns_bounded_workflow_json(tmp_path, capsys, monkeypatch):
    sessions = tmp_path / ".pony" / "sessions"
    store = SessionStore(sessions)
    old = store.save(_payload(tmp_path, "old", [], version=3))
    latest = store.save(_payload(tmp_path, "latest-session", [], version=3))
    os.utime(old, ns=(1, 1))
    os.utime(latest, ns=(2, 2))
    secret = "workflow-secret-value-123456789"
    monkeypatch.setenv("WORKFLOW_INSPECT_SECRET", secret)
    store.set_workflow_mode("latest-session", "review")
    store.set_active_plan(
        "latest-session",
        {
            "goal": f"Review {secret}",
            "items": [
                {"id": "done", "text": "private completed text", "status": "completed"},
                {"id": "now", "text": "private current text", "status": "in_progress"},
            ],
        },
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--secret-env-name",
            "WORKFLOW_INSPECT_SECRET",
            "--format",
            "json",
            "session",
            "inspect",
            "latest",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["kind"] == "session_inspect"
    assert payload["data"]["session_id"] == "latest-session"
    assert payload["data"]["workflow_mode"] == "review"
    assert payload["data"]["plan"] == {
        "goal": "Review <redacted>",
        "items": 2,
        "pending": 0,
        "in_progress": 1,
        "completed": 1,
    }
    output = json.dumps(payload)
    assert secret not in output
    assert "private completed text" not in output
    assert "private current text" not in output


def test_latest_skips_unsafe_session_without_changing_its_permissions(tmp_path):
    sessions = tmp_path / ".pony" / "sessions"
    store = SessionStore(sessions)
    safe = store.save(_payload(tmp_path, "safe", [], version=3))
    unsafe = store.save(_payload(tmp_path, "unsafe", [], version=3))
    os.utime(safe, ns=(1, 1))
    os.utime(unsafe, ns=(2, 2))
    unsafe.chmod(0o644)
    before = unsafe.stat()
    original = unsafe.read_bytes()

    latest = resolve_session_id_readonly("latest", sessions)

    after = unsafe.stat()
    assert latest == "safe"
    assert unsafe.read_bytes() == original
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )


def test_cli_inspect_redacts_absolute_path_in_plan_goal(tmp_path, capsys):
    sessions = tmp_path / ".pony" / "sessions"
    store = SessionStore(sessions)
    store.save(_payload(tmp_path, "path-goal", [], version=3))
    store.set_active_plan(
        "path-goal",
        {
            "goal": "Inspect /private/hidden/project",
            "items": [{"id": "one", "text": "private item", "status": "pending"}],
        },
    )

    assert (
        main(["--cwd", str(tmp_path), "--format", "json", "session", "inspect", "path-goal"])
        == 0
    )

    output = capsys.readouterr().out
    assert "/private/hidden/project" not in output
    assert json.loads(output)["data"]["plan"]["goal"] == "<redacted>"


def test_v2_writer_uses_stable_migration_error_envelope(tmp_path, capsys):
    sessions = tmp_path / ".pony" / "sessions"
    store = SessionStore(sessions)
    path = store.save(_payload(tmp_path, "v2-writer", [], version=3))
    _rewrite_as_v2(path)
    original = path.read_bytes()

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "session",
            "fork",
            "v2-writer",
            "missing-entry",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "session_migration_required"
    assert path.read_bytes() == original


def test_inspect_reports_branch_facts(tmp_path):
    root = tmp_path / "sessions"
    store = SessionStore(root)
    store.save(_payload(tmp_path, "branch", _tool_messages(), version=3))
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
