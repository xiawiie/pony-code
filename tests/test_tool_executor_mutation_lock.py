from contextlib import contextmanager

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_approval_finishes_before_mutation_lock(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    events = []
    monkeypatch.setattr(
        agent, "approve", lambda name, args: events.append("approval") or True
    )

    @contextmanager
    def lock():
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(agent.checkpoint_store, "mutation_lock", lock)
    agent.execute_tool("write_file", {"path": "note.txt", "content": "value"})

    assert events.index("approval") < events.index("lock-enter")
    assert events[-1] == "lock-exit"


def test_existing_same_owner_pending_blocks_runner(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    agent.tool_change_recorder.start(
        "", "old-turn", "write_file", "workspace_write", {"path": "old.txt"}
    )
    calls = []
    monkeypatch.setitem(
        agent.tools["write_file"],
        "run",
        lambda args: calls.append(args) or "ok",
    )

    result = agent.execute_tool(
        "write_file", {"path": "new.txt", "content": "value"}
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "recovery_review_required"
    assert calls == []


def test_malformed_mutation_record_blocks_runner(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    (agent.checkpoint_store.tool_changes_dir / "secret-filename.json").write_bytes(
        b"{invalid"
    )
    calls = []
    monkeypatch.setitem(
        agent.tools["write_file"],
        "run",
        lambda args: calls.append(args) or "ok",
    )

    result = agent.execute_tool(
        "write_file", {"path": "new.txt", "content": "value"}
    )

    assert result.metadata["tool_error_code"] == "recovery_review_required"
    assert calls == []


def test_memory_write_uses_same_mutation_lock(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    events = []

    @contextmanager
    def lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(agent.checkpoint_store, "mutation_lock", lock)
    agent.execute_tool(
        "memory_save", {"note": "safe local note"}
    )
    assert events == ["enter", "exit"]


def test_finalize_failure_blocks_next_same_owner_mutation(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    real_finalize = agent.tool_change_recorder.finalize
    calls = {"count": 0}

    def fail_once(tool_change_id, status, **fields):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated finalize failure")
        return real_finalize(tool_change_id, status, **fields)

    monkeypatch.setattr(agent.tool_change_recorder, "finalize", fail_once)
    try:
        first = agent.execute_tool(
            "write_file", {"path": "first.txt", "content": "first"}
        )
    except OSError:
        first = None
    second = agent.execute_tool(
        "write_file", {"path": "second.txt", "content": "second"}
    )

    assert first is None or first.metadata["tool_status"] == "error"
    assert second.metadata["tool_error_code"] == "recovery_review_required"
    assert not (tmp_path / "second.txt").exists()


@pytest.mark.parametrize("failure_point", ["guard", "prepared"])
def test_pre_runner_base_exception_releases_mutation_lock(
    tmp_path, monkeypatch, failure_point
):
    agent = build_agent(tmp_path)
    events = []

    @contextmanager
    def lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(agent.checkpoint_store, "mutation_lock", lock)
    if failure_point == "guard":
        monkeypatch.setattr(
            agent.tool_change_recorder,
            "pending_recovery_reviews",
            lambda: (_ for _ in ()).throw(KeyboardInterrupt("guard")),
        )
    else:
        monkeypatch.setattr(
            "pico.tool_executor._capture_before_file_states_for_paths",
            lambda *args: (_ for _ in ()).throw(KeyboardInterrupt("prepared")),
        )

    with pytest.raises(KeyboardInterrupt, match=failure_point):
        agent.execute_tool(
            "write_file", {"path": "note.txt", "content": "value"}
        )
    assert events == ["enter", "exit"]
