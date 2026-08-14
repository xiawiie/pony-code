import hashlib

from benchmarks.support.fake_provider import FakeModelClient

from pony import Pony
from pony.state.session_store import SessionStore
from pony.state.task_state import TaskState
from pony.tools.validation import validate_tool
from pony.runtime.options import RuntimeOptions
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path):
    return Pony(
        FakeModelClient([]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )


def test_current_run_raw_result_is_model_readable_through_executor(tmp_path):
    agent = _agent(tmp_path)
    task = TaskState.create("task1", "inspect", run_id="run1")
    agent.current_task_state = task
    agent.current_run_dir = agent.run_store.start_run(task)
    content = "".join(f"line {number}\n" for number in range(3_000))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    agent.run_store.write_tool_result(task, content_hash, content)
    agent.begin_permission_turn()
    try:
        result = agent.execute_tool(
            "read_tool_result",
            {"tool_result_id": f"tool_result:{content_hash}", "start": 1},
        )
    finally:
        agent.end_permission_turn()

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["result_view"]["delivery"] == "page"
    assert result.metadata["result_view"]["next_start"] == 2_001
    assert "[continuation]" in result.content
    assert str(agent.current_run_dir) not in result.content


def test_terminal_run_raw_result_is_expired_before_read(tmp_path):
    agent = _agent(tmp_path)
    task = TaskState.create("task1", "inspect", run_id="run1")
    agent.current_task_state = task
    agent.current_run_dir = agent.run_store.start_run(task)
    content = "safe\n"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    agent.run_store.write_tool_result(task, content_hash, content)
    task.finish_success("done")
    agent.begin_permission_turn()
    try:
        result = agent.execute_tool(
            "read_tool_result",
            {"tool_result_id": f"tool_result:{content_hash}"},
        )
    finally:
        agent.end_permission_turn()

    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_result_expired"
    assert "tool_result_expired" in result.content


def test_read_tool_result_rejects_non_capability_arguments(tmp_path):
    agent = _agent(tmp_path)
    tool_result_id = f"tool_result:{'0' * 64}"

    for args in (
        {"tool_result_id": tool_result_id[:28]},
        {"tool_result_id": tool_result_id, "path": "outside.txt"},
        {"tool_result_id": tool_result_id, "run_id": "other"},
    ):
        try:
            validate_tool(agent.tool_context(), "read_tool_result", args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe arguments accepted: {args}")


def test_read_file_executor_returns_structured_page_metadata(tmp_path):
    (tmp_path / "large.txt").write_text("x\n" * 3_000, encoding="utf-8")
    agent = _agent(tmp_path)
    agent.begin_permission_turn()
    try:
        result = agent.execute_tool("read_file", {"path": "large.txt"})
    finally:
        agent.end_permission_turn()

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["result_view"] == {
        "delivery": "page",
        "truncated": False,
        "start_line": 1,
        "end_line": 2_000,
        "total_lines": 3_000,
        "next_start": 2_001,
        "reasons": ["lines"],
    }
