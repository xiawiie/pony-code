from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.tool_executor import ToolExecutor, ToolExecutionResult


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_tool_executor_returns_content_and_metadata_without_side_channel(tmp_path):
    agent = build_agent(tmp_path)

    result = ToolExecutor(agent).execute("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert isinstance(result, ToolExecutionResult)
    assert "# README.md" in result.content
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["read_only"] is True
    assert result.metadata["workspace_changed"] is False


def test_pico_run_tool_keeps_compatibility_metadata(tmp_path):
    agent = build_agent(tmp_path)

    content = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert "# README.md" in content
    assert agent._last_tool_result_metadata["tool_status"] == "ok"


def test_write_file_creates_finalized_tool_change(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("write_file", {"path": "note.txt", "content": "hello\n"})

    tool_change = agent.checkpoint_store.load_tool_change_record(result.metadata["tool_change_id"])
    assert tool_change["status"] == "finalized"
    assert tool_change["affected_paths"] == ["note.txt"]
    assert tool_change["file_entries"][0]["change_kind"] == "created"


def test_invalid_tool_args_do_not_create_pending_tool_change(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("patch_file", {"path": "README.md", "old_text": "missing", "new_text": "x"})

    assert result.metadata["tool_status"] == "rejected"
    assert "tool_change_id" not in result.metadata or result.metadata["tool_change_id"] == ""


def test_tool_runtime_exception_finalizes_pending_record_as_error(tmp_path):
    agent = build_agent(tmp_path)

    def boom(args):
        raise RuntimeError("boom")

    agent.tools["write_file"]["run"] = boom
    result = agent.execute_tool("write_file", {"path": "note.txt", "content": "hello\n"})

    assert result.metadata["tool_status"] == "error"
    tool_change = agent.checkpoint_store.load_tool_change_record(result.metadata["tool_change_id"])
    assert tool_change["status"] == "error"
    assert tool_change["error"]["code"] == "tool_failed"


def test_run_shell_uses_command_policy_metadata(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["command_risk_class"] == "workspace_write"
    assert result.metadata["command_approval"]["decision"] == "allow"
    assert "generated.txt" in result.metadata["affected_paths"]
