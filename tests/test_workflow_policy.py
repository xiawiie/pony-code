import json
import sys
from unittest.mock import Mock

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path, outputs=(), **options):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    approval_policy = options.pop("approval_policy", "auto")
    return Pony(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(approval_policy=approval_policy, **options),
    )


def _plan_json():
    return json.dumps(
        {
            "goal": "Inspect the workflow",
            "items": [
                {"id": "inspect", "text": "Inspect state", "status": "in_progress"}
            ],
        }
    )


def test_mode_filters_model_schemas_and_executor_still_blocks_hidden_tools(tmp_path):
    agent = _agent(tmp_path)
    agent.set_workflow_mode("plan")

    visible = agent.visible_tools()
    assert {"read_file", "delegate", "update_plan", "run_shell"} <= set(visible)
    assert "write_file" not in visible
    assert "memory_save" not in visible

    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner
    blocked = agent.execute_tool(
        "write_file", {"path": "blocked.txt", "content": "blocked"}
    )

    assert blocked.metadata["tool_error_code"] == "workflow_mode_block"
    runner.assert_not_called()


def test_mode_allows_only_read_only_shell_in_plan_and_never_does_not_escalate(tmp_path):
    agent = _agent(tmp_path)
    agent.set_workflow_mode("plan")
    runner = Mock(return_value={"stdout": "ok\n", "stderr": "", "exit_code": 0})
    agent.tools["run_shell"]["run"] = runner

    allowed = agent.execute_tool("run_shell", {"command": "pwd", "timeout": 5})
    denied = agent.execute_tool(
        "run_shell", {"command": "printf changed > blocked.txt", "timeout": 5}
    )

    assert allowed.metadata["tool_status"] == "ok"
    assert denied.metadata["tool_error_code"] == "workflow_mode_block"
    assert runner.call_count == 1

    never = _agent(tmp_path / "never", approval_policy="never")
    never.set_workflow_mode("plan")
    result = never.execute_tool("run_shell", {"command": "pwd", "timeout": 5})
    assert result.metadata["tool_error_code"] == "approval_denied"


def test_runtime_read_only_hides_shell_and_rejects_plan_updates(tmp_path):
    agent = _agent(tmp_path, read_only=True)

    assert "run_shell" not in agent.visible_tools()
    assert "update_plan" not in agent.visible_tools()
    result = agent.execute_tool("update_plan", {"plan_json": _plan_json()})

    assert result.metadata["tool_error_code"] == "read_only_block"


def test_workflow_turn_freezes_mode_plan_and_blocks_mode_changes(tmp_path):
    agent = _agent(tmp_path)
    frozen_plan = agent.current_workflow_plan()
    agent.begin_workflow_turn()
    try:
        agent.session["workflow_mode"] = "review"
        agent.session["active_plan"] = json.loads(_plan_json())
        assert agent.current_workflow_mode() == "act"
        assert agent.current_workflow_plan() == frozen_plan
        with pytest.raises(RuntimeError, match="workflow_turn_active"):
            agent.set_workflow_mode("plan")
    finally:
        agent.end_workflow_turn()


def test_invalid_update_plan_has_stable_error_and_no_session_effect(tmp_path):
    agent = _agent(tmp_path)
    result = agent.execute_tool("update_plan", {"plan_json": "not-json"})

    assert result.metadata["tool_error_code"] == "invalid_plan"
    assert agent.session_store.load(agent.session["id"])["active_plan"]["goal"] == ""


def test_review_requires_ask_for_external_effect_shell(tmp_path):
    trusted = {"python": sys.executable}
    agent = _agent(
        tmp_path,
        approval_policy="ask",
        trusted_executables=trusted,
    )
    agent.set_workflow_mode("review")
    agent.approve = Mock(return_value=True)
    runner = Mock(return_value={"stdout": "ok\n", "stderr": "", "exit_code": 0})
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell", {"command": 'python -c "print(1)"', "timeout": 5}
    )

    assert result.metadata["tool_status"] == "ok"
    runner.assert_called_once()

    automatic = _agent(tmp_path / "automatic", trusted_executables=trusted)
    automatic.set_workflow_mode("review")
    blocked = automatic.execute_tool(
        "run_shell", {"command": 'python -c "print(1)"', "timeout": 5}
    )
    assert blocked.metadata["tool_error_code"] == "workflow_mode_block"


def test_update_plan_is_committed_verified_before_trace_and_frozen_for_followup(tmp_path):
    plan_json = _plan_json()
    agent = _agent(
        tmp_path,
        [{"name": "update_plan", "args": {"plan_json": plan_json}}, "done"],
    )
    agent.set_workflow_mode("plan")
    listener_plans = []

    def listener(event):
        if event["event"] == "tool_started":
            listener_plans.append(agent.session_store.load(agent.session["id"])["active_plan"])

    agent._trace_listener = listener
    assert agent.ask("make a plan") == "done"

    assert agent.session["active_plan"]["goal"] == "Inspect the workflow"
    assert listener_plans == [agent.session["active_plan"]]
    assert agent.current_workflow_plan() == agent.session["active_plan"]
    assert "write_file" not in {
        tool["name"] for tool in agent.model_client.requests[0]["tools"]
    }
    assert "update_plan" in {
        tool["name"] for tool in agent.model_client.requests[1]["tools"]
    }
    metadata = agent.last_request_metadata
    assert metadata["workflow_mode"] == "plan"
    assert metadata["workflow_plan_item_count"] == 0
    assert "Inspect the workflow" not in json.dumps(metadata)


def test_unverified_update_plan_blocks_later_session_writes(tmp_path, monkeypatch):
    agent = _agent(
        tmp_path,
        [{"name": "update_plan", "args": {"plan_json": _plan_json()}}, "done"],
    )
    agent.set_workflow_mode("plan")
    original_reload = agent._reload_session_projection
    listener = Mock()
    agent._trace_listener = listener
    monkeypatch.setattr(agent, "_reload_session_projection", lambda: {"active_plan": {}})

    with pytest.raises(RuntimeError, match="committed session projection"):
        agent.ask("make a plan")

    assert agent._session_write_blocked_cause is not None
    assert not any(
        call.args[0]["event"] == "tool_started" for call in listener.call_args_list
    )
    monkeypatch.setattr(agent, "_reload_session_projection", original_reload)
