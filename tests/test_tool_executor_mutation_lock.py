from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient


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
    first = agent.execute_tool(
        "write_file", {"path": "first.txt", "content": "first"}
    )
    second = agent.execute_tool(
        "write_file", {"path": "second.txt", "content": "second"}
    )

    assert first.metadata["tool_status"] == "error"
    assert first.metadata["tool_error_code"] == "tool_finalize_failed"
    records = agent.checkpoint_store.list_tool_change_records()
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert second.metadata["tool_error_code"] == "recovery_review_required"
    assert not (tmp_path / "second.txt").exists()


def test_interrupted_finalize_failure_preserves_primary_and_review_evidence(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    primary = KeyboardInterrupt("runner interrupted")
    agent.tools["write_file"]["run"] = lambda _args: (_ for _ in ()).throw(primary)
    monkeypatch.setattr(
        agent.tool_change_recorder,
        "finalize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("finalize failed")),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        agent.execute_tool("write_file", {"path": "first.txt", "content": "first"})

    assert caught.value is primary
    records = agent.checkpoint_store.list_tool_change_records()
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    second = agent.execute_tool(
        "write_file", {"path": "second.txt", "content": "second"}
    )
    assert second.metadata["tool_error_code"] == "recovery_review_required"
    assert not (tmp_path / "second.txt").exists()


class FatalLockSignal(BaseException):
    pass


def test_mutation_lock_exit_preserves_active_primary_but_raises_without_one(
    tmp_path,
    monkeypatch,
):
    class FailingExit:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            raise OSError("lock exit failed")

    primary_root = tmp_path / "primary"
    primary_root.mkdir()
    primary_agent = build_agent(primary_root)
    primary = FatalLockSignal("runner failed")
    primary_agent.tools["write_file"]["run"] = Mock(side_effect=primary)
    monkeypatch.setattr(
        primary_agent.checkpoint_store,
        "mutation_lock",
        lambda: FailingExit(),
    )

    with pytest.raises(FatalLockSignal) as caught:
        primary_agent.execute_tool(
            "write_file", {"path": "note.txt", "content": "value"}
        )

    assert caught.value is primary
    assert primary_agent.checkpoint_store.list_tool_change_records()[-1]["status"] == "interrupted"

    success_root = tmp_path / "success"
    success_root.mkdir()
    success_agent = build_agent(success_root)
    monkeypatch.setattr(
        success_agent.checkpoint_store,
        "mutation_lock",
        lambda: FailingExit(),
    )

    with pytest.raises(OSError, match="lock exit failed"):
        success_agent.execute_tool(
            "write_file", {"path": "note.txt", "content": "value"}
        )


def test_mutation_lock_enter_failure_preserves_primary_identity(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    primary = FatalLockSignal("enter")
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    @contextmanager
    def lock():
        raise primary
        yield

    monkeypatch.setattr(agent.checkpoint_store, "mutation_lock", lock)

    with pytest.raises(BaseException) as caught:
        agent.execute_tool("write_file", {"path": "note.txt", "content": "value"})

    assert caught.value is primary
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


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
