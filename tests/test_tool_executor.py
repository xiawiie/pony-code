from pathlib import Path
from unittest.mock import Mock

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.state.task_state import TaskState
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path, **options):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    runtime_options = RuntimeOptions(project_trusted=True, **options)
    return Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=runtime_options,
    )


def test_unknown_tool_is_rejected_before_execution(tmp_path):
    result = _agent(tmp_path).execute_tool("unknown_tool", {})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unknown_tool"


def test_read_only_runtime_blocks_workspace_write(tmp_path):
    agent = _agent(tmp_path, read_only=True)

    result = agent.execute_tool(
        "write_file",
        {"path": "blocked.txt", "content": "no\n"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "read_only_block"
    assert not (Path(tmp_path) / "blocked.txt").exists()


def test_workspace_path_escape_is_rejected(tmp_path):
    agent = _agent(tmp_path)

    result = agent.execute_tool(
        "write_file",
        {"path": "../outside.txt", "content": "no\n"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert not (Path(tmp_path).parent / "outside.txt").exists()


def test_workspace_write_runs_without_recovery_store(tmp_path):
    agent = _agent(tmp_path)

    result = agent.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "saved\n"},
    )

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["affected_paths"] == ["note.txt"]
    assert result.metadata["workspace_changed"] is True
    assert not hasattr(agent, "checkpoint_store")
    assert not hasattr(agent, "tool_change_recorder")
    assert (Path(tmp_path) / "note.txt").read_text(encoding="utf-8") == "saved\n"


def test_tool_runtime_error_is_stable_and_does_not_create_recovery_state(tmp_path):
    agent = _agent(tmp_path)
    agent.tools["write_file"]["run"] = lambda _args: (_ for _ in ()).throw(
        RuntimeError("boom")
    )

    result = agent.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "saved\n"},
    )

    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert "boom" in result.content
    assert not (Path(tmp_path) / ".pony" / "checkpoints").exists()


def test_memory_save_requires_current_request_authorization(tmp_path):
    agent = _agent(tmp_path)
    agent.current_task_state = TaskState.create("task", "inspect the repository")
    runner = Mock(return_value="saved")
    agent.tools["memory_save"]["run"] = runner

    result = agent.execute_tool("memory_save", {"note": "keep this"})

    assert result.metadata["tool_error_code"] == "memory_write_not_authorized"
    runner.assert_not_called()


def test_memory_save_accepts_current_request_authorization(tmp_path):
    agent = _agent(tmp_path)
    agent.current_task_state = TaskState.create("task", "/remember keep this")
    runner = Mock(return_value="saved")
    agent.tools["memory_save"]["run"] = runner

    result = agent.execute_tool("memory_save", {"note": "keep this"})

    assert result.metadata["tool_status"] == "ok"
    runner.assert_called_once()


def test_finalization_failure_keeps_observed_workspace_effect(tmp_path, monkeypatch):
    agent = _agent(tmp_path)

    def write(args):
        (Path(tmp_path) / args["path"]).write_text(args["content"], encoding="utf-8")
        return "written"

    agent.tools["write_file"]["run"] = write
    monkeypatch.setattr(
        agent,
        "update_memory_after_tool",
        Mock(side_effect=OSError("memory update failed")),
    )

    result = agent.execute_tool("write_file", {"path": "after.txt", "content": "ok\n"})

    assert result.metadata["tool_status"] == "partial_success"
    assert result.metadata["tool_error_code"] == "tool_finalize_failed"
    assert result.metadata["affected_paths"] == ["after.txt"]
    assert result.metadata["workspace_changed"] is True


def test_finalization_interrupt_keeps_observed_workspace_metadata(tmp_path, monkeypatch):
    agent = _agent(tmp_path)

    def write(args):
        (Path(tmp_path) / args["path"]).write_text(args["content"], encoding="utf-8")
        return "written"

    agent.tools["write_file"]["run"] = write
    monkeypatch.setattr(
        agent,
        "update_memory_after_tool",
        Mock(side_effect=KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("write_file", {"path": "after.txt", "content": "ok\n"})

    assert agent._last_tool_result_metadata["tool_status"] == "interrupted"
    assert agent._last_tool_result_metadata["affected_paths"] == ["after.txt"]


def test_sensitive_content_is_rejected_before_memory_write(tmp_path):
    agent = _agent(tmp_path)

    result = agent.execute_tool(
        "memory_save",
        {"note": "github_pat_A123456789012345678901234567890"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sensitive_content_block"


def test_repeated_workspace_call_is_rejected(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(agent, "repeated_tool_call", lambda *_args: True)

    result = agent.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "saved\n"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "repeated_identical_call"
