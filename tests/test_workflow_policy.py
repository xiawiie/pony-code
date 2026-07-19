import sys
from unittest.mock import Mock

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.state.task_state import TaskState
from pony.workspace.context import WorkspaceContext


def _agent(
    tmp_path,
    outputs=(),
    *,
    trusted=True,
    read_only=False,
    executables=None,
    redaction_env=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=trusted,
            read_only=read_only,
            trusted_executables=executables,
            redaction_env=redaction_env,
            trusted_redaction_env=redaction_env is not None,
        ),
    )


def test_plan_filters_schemas_and_asks_before_hidden_mutation(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("plan")
    prompt = Mock(return_value=False)
    runner = Mock(return_value="must not run")
    agent._approval_prompt = prompt
    agent.tools["write_file"]["run"] = runner
    agent.set_permission_rule("write_file", "allow")

    assert "read_file" in agent.visible_tools()
    assert "delegate" in agent.visible_tools()
    assert "run_shell" not in agent.visible_tools()
    assert "write_file" not in agent.visible_tools()
    assert "memory_save" not in agent.visible_tools()
    assert {"read_plan", "write_plan", "exit_plan_mode"} <= set(
        agent.visible_tools()
    )

    blocked = agent.execute_tool(
        "write_file", {"path": "blocked.txt", "content": "blocked"}
    )

    assert blocked.metadata["tool_error_code"] == "approval_denied"
    prompt.assert_called_once()
    runner.assert_not_called()


def test_permission_rules_persist_and_deny_precedes_auto(tmp_path):
    agent = _agent(tmp_path)

    agent.set_permission_rule("read_file", "deny")
    result = agent.execute_tool(
        "read_file", {"path": "README.md", "start": 1, "end": 1}
    )

    assert result.metadata["tool_error_code"] == "permission_mode_block"
    resumed = agent.session_store.load(agent.session["id"])
    assert resumed["permission_rules"] == {
        "allow": [],
        "ask": [],
        "deny": ["read_file"],
    }


def test_dont_ask_honors_explicit_allow_rule(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("dontAsk")
    agent.set_permission_rule("write_file", "allow")

    result = agent.execute_tool(
        "write_file", {"path": "allowed.txt", "content": "allowed\n"}
    )

    assert result.metadata["tool_status"] == "ok"
    assert (tmp_path / "allowed.txt").read_text(encoding="utf-8") == "allowed\n"


def test_permission_turn_freezes_mode_and_visible_tools(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("plan")
    frozen_tools = set(agent.visible_tools())

    agent.begin_permission_turn()
    try:
        agent.session["permission_mode"] = "default"
        assert agent.current_permission_mode() == "plan"
        assert set(agent.visible_tools()) == frozen_tools
        with pytest.raises(RuntimeError, match="permission_turn_active"):
            agent.set_permission_mode("default")
    finally:
        agent.end_permission_turn()


def test_agent_loop_sends_frozen_plan_tools_and_permission_metadata(tmp_path):
    agent = _agent(tmp_path, ["done"])
    agent.set_permission_mode("plan")

    assert agent.ask("inspect only") == "done"

    visible = {tool["name"] for tool in agent.model_client.requests[0]["tools"]}
    assert "read_file" in visible
    assert "write_file" not in visible
    assert "run_shell" not in visible
    assert {"read_plan", "write_plan", "exit_plan_mode"} <= visible
    assert "Plan mode is active" in agent.model_client.requests[0]["system"][0]["text"]
    assert agent.last_request_metadata["permission_mode"] == "plan"


def test_plan_approval_restores_mode_and_continues_same_request(tmp_path):
    outputs = [
        {"name": "write_plan", "arguments": {"plan": "# Plan\n1. Implement"}},
        {"name": "exit_plan_mode", "arguments": {}},
        {
            "name": "write_file",
            "arguments": {"path": "implemented.txt", "content": "done\n"},
        },
        "implemented",
    ]
    agent = _agent(tmp_path, outputs)
    agent.set_permission_mode("plan")
    approval = Mock(return_value=True)
    agent._approval_prompt = approval

    assert agent.ask("plan and implement") == "implemented"

    approval.assert_called_once_with(
        "exit_plan_mode",
        {"plan": "# Plan\n1. Implement", "revision": 1},
    )
    assert agent.session["permission_mode"] == "auto"
    assert agent.session["plan_text"] == "# Plan\n1. Implement"
    assert agent.session["plan_revision"] == 1
    assert (tmp_path / "implemented.txt").read_text(encoding="utf-8") == "done\n"
    plan_tools = {tool["name"] for tool in agent.model_client.requests[1]["tools"]}
    act_tools = {tool["name"] for tool in agent.model_client.requests[2]["tools"]}
    assert "exit_plan_mode" in plan_tools
    assert "write_file" not in plan_tools
    assert "write_file" in act_tools
    assert "exit_plan_mode" not in act_tools
    assert "Plan mode is active" not in agent.model_client.requests[2]["system"][0]["text"]


def test_rejected_plan_stays_in_plan_mode(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("plan")
    agent.execute_tool("write_plan", {"plan": "# Plan\n1. Inspect"})
    approval = Mock(return_value=False)
    agent._approval_prompt = approval

    result = agent.execute_tool("exit_plan_mode", {})

    assert result.metadata["tool_error_code"] == "plan_rejected"
    assert agent.session["permission_mode"] == "plan"
    assert agent.current_plan() == "# Plan\n1. Inspect"


def test_write_plan_rejects_oversized_and_sensitive_content(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("plan")
    sensitive_agent = _agent(
        tmp_path / "sensitive",
        redaction_env={"PONY_API_KEY": "live-plan-secret"},
    )
    sensitive_agent.set_permission_mode("plan")

    oversized = agent.execute_tool("write_plan", {"plan": "x" * (12 * 1024 + 1)})
    sensitive = sensitive_agent.execute_tool(
        "write_plan",
        {"plan": "Use live-plan-secret while testing"},
    )

    assert oversized.metadata["tool_error_code"] == "invalid_arguments"
    assert sensitive.metadata["tool_error_code"] == "sensitive_content_block"
    assert agent.current_plan() == ""
    assert agent.current_plan_revision() == 0
    assert sensitive_agent.current_plan() == ""


def test_accept_edits_only_skips_prompt_for_builtin_file_edits(tmp_path):
    agent = _agent(
        tmp_path,
        executables={"python": sys.executable},
    )
    agent.set_permission_mode("acceptEdits")
    prompt = Mock(return_value=True)
    shell_runner = Mock(
        return_value={"stdout": "ok\n", "stderr": "", "exit_code": 0}
    )
    agent._approval_prompt = prompt
    agent.tools["run_shell"]["run"] = shell_runner

    written = agent.execute_tool(
        "write_file", {"path": "allowed.txt", "content": "before\n"}
    )
    patched = agent.execute_tool(
        "patch_file",
        {"path": "allowed.txt", "old_text": "before", "new_text": "after"},
    )
    shell = agent.execute_tool(
        "run_shell", {"command": 'python -c "print(1)"', "timeout": 5}
    )

    assert written.metadata["tool_status"] == "ok"
    assert patched.metadata["tool_status"] == "ok"
    assert shell.metadata["tool_status"] == "ok"
    prompt.assert_called_once()
    shell_runner.assert_called_once()


def test_default_prompts_and_revalidates_approved_arguments(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("default")
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    def mutate(_name, args):
        args["path"] = "changed.txt"
        return True

    agent.approve = mutate
    result = agent.execute_tool(
        "write_file", {"path": "approved.txt", "content": "content"}
    )

    assert result.metadata["tool_error_code"] == "approval_arguments_changed"
    assert not (tmp_path / "approved.txt").exists()
    assert not (tmp_path / "changed.txt").exists()
    runner.assert_not_called()


def test_dont_ask_denies_prompted_mutation_without_calling_prompt(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("dontAsk")
    prompt = Mock(return_value=True)
    runner = Mock(return_value="must not run")
    agent._approval_prompt = prompt
    agent.tools["write_file"]["run"] = runner

    result = agent.execute_tool(
        "write_file", {"path": "blocked.txt", "content": "blocked"}
    )

    assert result.metadata["tool_error_code"] == "permission_mode_block"
    prompt.assert_not_called()
    runner.assert_not_called()


def test_accept_edits_still_prompts_for_memory_write(tmp_path):
    agent = _agent(tmp_path)
    agent.set_permission_mode("acceptEdits")
    agent.current_task_state = TaskState.create(
        task_id="remember",
        user_request="remember this rule",
    )
    prompt = Mock(return_value=False)
    agent._approval_prompt = prompt

    result = agent.execute_tool("memory_save", {"note": "remembered rule"})

    assert result.metadata["tool_error_code"] == "approval_denied"
    prompt.assert_called_once()


def test_untrusted_and_read_only_boundaries_fail_closed(tmp_path):
    untrusted = _agent(tmp_path / "untrusted", trusted=False)
    read_only = _agent(tmp_path / "readonly", read_only=True)
    prompt = Mock(return_value=True)
    untrusted._approval_prompt = prompt
    read_only._approval_prompt = prompt

    denied_read = untrusted.execute_tool(
        "read_file", {"path": "README.md", "start": 1, "end": 1}
    )
    denied_write = read_only.execute_tool(
        "write_file", {"path": "blocked.txt", "content": "blocked"}
    )

    assert denied_read.metadata["tool_error_code"] == "permission_denied"
    assert denied_write.metadata["tool_error_code"] == "read_only_block"
    assert "run_shell" not in read_only.visible_tools()
    prompt.assert_not_called()


def test_update_plan_is_not_an_active_tool(tmp_path):
    agent = _agent(tmp_path)

    assert "update_plan" not in agent.tools
    result = agent.execute_tool("update_plan", {"plan_json": "{}"})

    assert result.metadata["tool_error_code"] == "unknown_tool"
