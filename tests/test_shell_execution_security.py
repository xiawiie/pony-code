import json
import os
import subprocess
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient


def build_agent(
    tmp_path,
    *,
    approval_policy="auto",
    executables=None,
    read_only=False,
    outputs=None,
    redaction_env=None,
    secret_env_names=(),
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient(list(outputs or [])),
        workspace=WorkspaceContext.build(
            tmp_path,
            executables={} if executables is None else executables,
        ),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy=approval_policy,
        read_only=read_only,
        redaction_env=redaction_env,
        secret_env_names=secret_env_names,
    )


def completed(stdout="ok\n", stderr="", exit_code=0):
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}


def assert_shell_metadata(
    result,
    *,
    risk_class,
    decision,
    reason,
    mode,
    outcome,
    runner_executed,
    execution_mode,
    exit_code=None,
):
    assert result.metadata["command_risk_class"] == risk_class
    assert result.metadata["command_approval"] == {
        "decision": decision,
        "reason": reason,
        "mode": mode,
        "outcome": outcome,
        "runner_executed": runner_executed,
        "execution_mode": execution_mode,
        **({"exit_code": exit_code} if exit_code is not None else {}),
    }


@pytest.mark.parametrize(
    (
        "case",
        "command",
        "approval_policy",
        "read_only",
        "executables",
        "approve_result",
        "expected_error",
        "risk_class",
        "decision",
        "reason",
        "outcome",
        "execution_mode",
        "prompt_count",
        "runner_count",
    ),
    [
        (
            "read-only",
            "pwd",
            "ask",
            True,
            {"pwd": "/frozen/pwd"},
            True,
            "read_only_block",
            "read_only",
            "allow",
            "proved_read_only",
            "blocked",
            "argv",
            0,
            0,
        ),
        (
            "never",
            "pwd",
            "never",
            False,
            {"pwd": "/frozen/pwd"},
            True,
            "approval_denied",
            "read_only",
            "allow",
            "proved_read_only",
            "denied",
            "argv",
            0,
            0,
        ),
        (
            "auto-allow",
            "pwd",
            "auto",
            False,
            {"pwd": "/frozen/pwd"},
            True,
            "",
            "read_only",
            "allow",
            "proved_read_only",
            "allowed",
            "argv",
            0,
            1,
        ),
        (
            "auto-missing",
            "pwd",
            "auto",
            False,
            {},
            True,
            "trusted_executable_missing",
            "read_only",
            "allow",
            "proved_read_only",
            "blocked",
            "argv",
            0,
            0,
        ),
        (
            "auto-ask-argv",
            "python -m pytest",
            "auto",
            False,
            {"python": "/frozen/python"},
            True,
            "command_approval_required",
            "external_effect",
            "ask",
            "interpreter_requires_approval",
            "blocked",
            "argv",
            0,
            0,
        ),
        (
            "auto-ask-shell",
            "pwd && ls",
            "auto",
            False,
            {"sh": "/frozen/sh"},
            True,
            "command_approval_required",
            "external_effect",
            "ask",
            "shell_grammar_requires_approval",
            "blocked",
            "shell",
            0,
            0,
        ),
        (
            "ask-allow",
            "pwd",
            "ask",
            False,
            {"pwd": "/frozen/pwd"},
            True,
            "",
            "read_only",
            "allow",
            "proved_read_only",
            "approved",
            "argv",
            1,
            1,
        ),
        (
            "ask-argv",
            "python -m pytest",
            "ask",
            False,
            {"python": "/frozen/python"},
            True,
            "",
            "external_effect",
            "ask",
            "interpreter_requires_approval",
            "approved",
            "argv",
            1,
            1,
        ),
        (
            "ask-missing",
            "python -m pytest",
            "ask",
            False,
            {},
            True,
            "trusted_executable_missing",
            "external_effect",
            "ask",
            "interpreter_requires_approval",
            "blocked",
            "argv",
            1,
            0,
        ),
        (
            "ask-denied",
            "pwd",
            "ask",
            False,
            {"pwd": "/frozen/pwd"},
            False,
            "approval_denied",
            "read_only",
            "allow",
            "proved_read_only",
            "denied",
            "argv",
            1,
            0,
        ),
        (
            "ask-shell",
            "pwd && ls",
            "ask",
            False,
            {"sh": "/frozen/sh"},
            True,
            "",
            "external_effect",
            "ask",
            "shell_grammar_requires_approval",
            "approved",
            "shell",
            1,
            1,
        ),
        (
            "ask-shell-missing",
            "pwd && ls",
            "ask",
            False,
            {},
            True,
            "trusted_executable_missing",
            "external_effect",
            "ask",
            "shell_grammar_requires_approval",
            "blocked",
            "shell",
            1,
            0,
        ),
        (
            "sensitive",
            "cat .env",
            "ask",
            False,
            {"cat": "/frozen/cat"},
            True,
            "sensitive_path_block",
            "destructive",
            "reject",
            "sensitive_path",
            "blocked",
            "argv",
            0,
            0,
        ),
    ],
)
def test_single_shell_gate_mode_matrix(
    tmp_path,
    case,
    command,
    approval_policy,
    read_only,
    executables,
    approve_result,
    expected_error,
    risk_class,
    decision,
    reason,
    outcome,
    execution_mode,
    prompt_count,
    runner_count,
):
    agent = build_agent(
        tmp_path,
        approval_policy=approval_policy,
        executables=executables,
        read_only=read_only,
    )
    approve = Mock(return_value=approve_result)
    runner = Mock(return_value=completed())
    agent.approve = approve
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 5},
    )

    assert approve.call_count == prompt_count, case
    assert runner.call_count == runner_count, case
    assert result.metadata["tool_error_code"] == expected_error
    assert result.metadata["tool_status"] == (
        "ok" if runner_count else "rejected"
    )
    assert_shell_metadata(
        result,
        risk_class=risk_class,
        decision=decision,
        reason=reason,
        mode=approval_policy,
        outcome=outcome,
        runner_executed=bool(runner_count),
        execution_mode=execution_mode,
        exit_code=0 if runner_count else None,
    )
    records = agent.checkpoint_store.list_tool_change_records()
    assert bool(records) is bool(runner_count)
    if records:
        assert records[-1]["input_summary"]["assessment"] == {
            "risk_class": risk_class,
            "decision": decision,
            "reason": reason,
            "argv": (
                []
                if execution_mode == "shell"
                else command.split()
            ),
            "execution_mode": execution_mode,
        }
        assert records[-1]["approval"] == result.metadata["command_approval"]


@pytest.mark.parametrize(
    "command",
    [
        "git show HEAD:.env",
        "git show HEAD:credentials.json",
        "git show HEAD:.ssh/id_rsa",
        "git cat-file blob HEAD:.env",
    ],
)
def test_sensitive_git_object_path_is_rejected_before_prompt_or_runner(
    tmp_path,
    monkeypatch,
    command,
):
    from pico import tool_executor as tool_executor_module

    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": "/frozen/git"},
    )
    approve = Mock(return_value=True)
    registry_runner = Mock(return_value=completed())
    git_runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    agent.approve = approve
    agent.tools["run_shell"]["run"] = registry_runner
    monkeypatch.setattr(tool_executor_module, "run_hardened_git", git_runner)

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sensitive_path_block"
    assert_shell_metadata(
        result,
        risk_class="destructive",
        decision="reject",
        reason="sensitive_path",
        mode="ask",
        outcome="blocked",
        runner_executed=False,
        execution_mode="argv",
    )
    approve.assert_not_called()
    registry_runner.assert_not_called()
    git_runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_ask_eof_denies_without_runner_or_exit_code(tmp_path, monkeypatch):
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"pwd": "/frozen/pwd"},
    )
    runner = Mock(return_value=completed())
    agent.tools["run_shell"]["run"] = runner
    monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_error_code"] == "approval_denied"
    assert result.metadata["command_approval"]["outcome"] == "denied"
    assert "exit_code" not in result.metadata["command_approval"]
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("case", "agent_options", "arguments", "expected_error"),
    [
        (
            "invalid-arguments",
            {},
            {"command": "pwd", "timeout": 0},
            "invalid_arguments",
        ),
        (
            "tool-not-allowed",
            {"allowed_tools": {"read_file"}},
            {"command": "pwd", "timeout": 5},
            "tool_not_allowed",
        ),
    ],
)
def test_shell_early_rejections_keep_complete_assessment_metadata(
    tmp_path,
    case,
    agent_options,
    arguments,
    expected_error,
):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    if "allowed_tools" in agent_options:
        agent.allowed_tools = tuple(agent_options["allowed_tools"])
        agent.tools = agent._apply_tool_allowlist(agent.build_tools())
    approve = Mock(return_value=True)
    agent.approve = approve

    result = agent.execute_tool("run_shell", arguments)

    assert result.metadata["tool_error_code"] == expected_error, case
    assert_shell_metadata(
        result,
        risk_class="read_only",
        decision="allow",
        reason="proved_read_only",
        mode="auto",
        outcome="blocked",
        runner_executed=False,
        execution_mode="argv",
    )
    approve.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


@pytest.mark.parametrize("arguments", [["not-a-mapping"], "not-a-mapping"])
def test_nonmapping_shell_arguments_return_structured_invalid_metadata(
    tmp_path,
    arguments,
):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    approve = Mock(return_value=True)
    runner = Mock(return_value=completed())
    agent.approve = approve
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool("run_shell", arguments)

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "invalid_arguments"
    assert_shell_metadata(
        result,
        risk_class="external_effect",
        decision="ask",
        reason="empty_command",
        mode="auto",
        outcome="blocked",
        runner_executed=False,
        execution_mode="shell",
    )
    approve.assert_not_called()
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_repeated_shell_rejection_keeps_complete_assessment_metadata(tmp_path):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    agent.session["messages"] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "run_shell",
                    "input": {"command": "pwd", "timeout": 5},
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "run_shell",
                    "input": {"command": "pwd", "timeout": 5},
                }
            ],
        },
    ]
    approve = Mock(return_value=True)
    runner = Mock(return_value=completed())
    agent.approve = approve
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_error_code"] == "repeated_identical_call"
    assert_shell_metadata(
        result,
        risk_class="read_only",
        decision="allow",
        reason="proved_read_only",
        mode="auto",
        outcome="blocked",
        runner_executed=False,
        execution_mode="argv",
    )
    approve.assert_not_called()
    runner.assert_not_called()


def test_hard_reject_does_not_read_empty_argv(tmp_path, monkeypatch):
    from pico import tool_executor as tool_executor_module

    assessment = {
        "risk_class": "destructive",
        "decision": "reject",
        "reason": "sensitive_path",
        "argv": [],
        "execution_mode": "argv",
    }
    monkeypatch.setattr(
        tool_executor_module,
        "assess_command",
        Mock(return_value=assessment),
        raising=False,
    )
    agent = build_agent(tmp_path, approval_policy="ask", executables={})
    approve = Mock(return_value=True)
    runner = Mock(return_value=completed())
    agent.approve = approve
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": "opaque", "timeout": 5},
    )

    assert result.metadata["tool_error_code"] == "sensitive_path_block"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    approve.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("command", "executables", "expected_argv", "expected_shell", "expected_executable"),
    [
        ("pwd", {"pwd": "/frozen/pwd"}, ["/frozen/pwd"], False, None),
        (
            "python -m pytest",
            {"python": "/frozen/python"},
            ["/frozen/python", "-m", "pytest"],
            False,
            None,
        ),
        (
            "bash -c 'pwd && ls'",
            {"bash": "/frozen/bash"},
            ["/frozen/bash", "-c", "pwd && ls"],
            False,
            None,
        ),
        (
            "pwd && ls",
            {"sh": "/frozen/sh"},
            "pwd && ls",
            True,
            "/frozen/sh",
        ),
    ],
)
def test_execution_shape_uses_only_frozen_executables(
    tmp_path,
    monkeypatch,
    command,
    executables,
    expected_argv,
    expected_shell,
    expected_executable,
):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = Mock(returncode=0)
        process.communicate.return_value = ("ok\n", "")
        return process

    @contextmanager
    def passthrough(executable):
        yield str(executable)

    monkeypatch.setattr("pico.safe_subprocess._prepared_executable", passthrough)
    monkeypatch.setattr("pico.safe_subprocess.subprocess.Popen", fake_popen)
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables=executables,
    )
    agent.approve = Mock(return_value=True)

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 7},
    )

    assert result.metadata["tool_status"] == "ok"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == (
        ["/frozen/sh", "-c", expected_argv]
        if expected_shell
        else expected_argv
    )
    assert kwargs["shell"] is False
    assert kwargs.get("executable") == expected_executable
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == agent.shell_env()


def test_simple_unknown_command_is_not_rewrapped_in_shell(tmp_path, monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = Mock(returncode=0)
        process.communicate.return_value = ("ok", "")
        return process

    @contextmanager
    def passthrough(executable):
        yield str(executable)

    monkeypatch.setattr("pico.safe_subprocess._prepared_executable", passthrough)
    monkeypatch.setattr("pico.safe_subprocess.subprocess.Popen", fake_popen)
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"echo": "/frozen/echo"},
    )
    agent.approve = Mock(return_value=True)

    result = agent.execute_tool(
        "run_shell",
        {"command": "echo hello", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "ok"
    assert calls[0][0] == ["/frozen/echo", "hello"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1].get("executable") is None


def test_runtime_path_spoof_cannot_replace_frozen_executable(tmp_path, monkeypatch):
    fake_pwd = tmp_path / "pwd"
    fake_pwd.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_pwd.chmod(0o755)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        process = Mock(returncode=0)
        process.communicate.return_value = ("safe\n", "")
        return process

    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    @contextmanager
    def passthrough(executable):
        yield str(executable)

    monkeypatch.setattr("pico.safe_subprocess._prepared_executable", passthrough)
    monkeypatch.setattr("pico.safe_subprocess.subprocess.Popen", fake_popen)

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "ok"
    assert calls[0][0] == ["/frozen/pwd"]


def test_executable_path_never_falls_back_to_frozen_basename(tmp_path):
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={
            "python": "/frozen/python",
            "/usr/bin/python": "/usr/bin/python",
        },
    )
    approve = Mock(return_value=True)
    runner = Mock(return_value=completed())
    agent.approve = approve
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": "/usr/bin/python -m pytest", "timeout": 5},
    )

    assert approve.call_count == 1
    runner.assert_not_called()
    assert result.metadata["tool_error_code"] == "trusted_executable_missing"
    assert result.metadata["command_approval"]["outcome"] == "blocked"


def test_approval_payload_and_runner_output_are_redacted(tmp_path):
    secret = "opaque-shell-token-123456789"
    seen_payloads = []
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"pwd": "/frozen/pwd"},
        redaction_env={"PICO_TEST_TOKEN": secret},
        secret_env_names=("PICO_TEST_TOKEN",),
    )
    args = {"command": "pwd", "timeout": 5, "note": secret}

    def approve(name, payload):
        seen_payloads.append(payload)
        return True

    agent.approve = approve
    agent.tools["run_shell"]["run"] = Mock(
        return_value=completed(
            stdout=f"stdout {secret}\n",
            stderr=f"stderr {secret}\n",
        )
    )

    result = agent.execute_tool("run_shell", args)

    assert seen_payloads == [
        {"command": "pwd", "timeout": 5, "note": "<redacted>"}
    ]
    serialized = result.content + json.dumps(result.metadata, sort_keys=True)
    assert secret not in serialized
    assert "<redacted>" in result.content


def test_shell_record_redacts_full_command_before_truncating_summary(tmp_path):
    secret = "LEAKME_opaque_shell_token_123456789"
    command = "echo " + ("x" * 230) + secret
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"echo": "/frozen/echo"},
        redaction_env={"PICO_TEST_TOKEN": secret},
        secret_env_names=("PICO_TEST_TOKEN",),
    )
    agent.approve = Mock(return_value=True)
    agent.tools["run_shell"]["run"] = Mock(return_value=completed())

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 5},
    )

    record = agent.checkpoint_store.load_tool_change_record(
        result.metadata["tool_change_id"]
    )
    disk = next(
        (tmp_path / ".pico" / "checkpoints" / "tool_changes").glob("*.json")
    ).read_text(encoding="utf-8")
    assert "LEAKM" not in disk
    assert secret not in json.dumps(record)
    assert "<redacted>" in record["input_summary"]["command"]
    assert record["input_summary"]["assessment"]["reason"] == (
        "unknown_command_requires_approval"
    )


@pytest.mark.parametrize("mutation_target", ["original", "approval_payload"])
def test_approval_mutation_blocks_execution(tmp_path, mutation_target):
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"pwd": "/frozen/pwd"},
    )
    args = {"command": "pwd", "timeout": 5}
    runner = Mock(return_value=completed())
    agent.tools["run_shell"]["run"] = runner

    def approve(name, payload):
        target = args if mutation_target == "original" else payload
        target["command"] = "rm -f README.md"
        return True

    agent.approve = Mock(side_effect=approve)

    result = agent.execute_tool("run_shell", args)

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "approval_arguments_changed"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert agent.approve.call_count == 1
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_post_prompt_reassessment_blocks_filesystem_swap_without_second_prompt(
    tmp_path,
):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"wc": "/frozen/wc"},
    )
    runner = Mock(return_value=completed())
    agent.tools["run_shell"]["run"] = runner

    def approve(name, payload):
        (tmp_path / "README.md").unlink()
        (tmp_path / "README.md").symlink_to(outside)
        return True

    agent.approve = Mock(side_effect=approve)

    result = agent.execute_tool(
        "run_shell",
        {"command": "wc README.md", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "approval_arguments_changed"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert agent.approve.call_count == 1
    runner.assert_not_called()


def test_decoded_secret_tool_action_is_blocked_before_prompt_and_runner(tmp_path):
    secret = "opaque-decoded-token-123456789"
    tool_call = {
        "name": "run_shell",
        "args": {"command": f"echo {secret}", "timeout": 5},
    }
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"echo": "/frozen/echo"},
        outputs=(tool_call, "done"),
        redaction_env={"PICO_TEST_TOKEN": secret},
        secret_env_names=("PICO_TEST_TOKEN",),
    )
    approve = Mock(return_value=True)
    runner = Mock(return_value=completed())
    agent.approve = approve
    agent.tools["run_shell"]["run"] = runner

    assert agent.ask("try the command") == "done"

    approve.assert_not_called()
    runner.assert_not_called()
    event = next(
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
        if '"event": "tool_executed"' in line
    )
    assert event["tool_error_code"] == "sensitive_content_block"
    assert event["command_risk_class"] == "external_effect"
    assert "command_approval" not in event
    assert event.get("runner_executed", False) is False
    assert secret not in json.dumps(event)


def test_runner_exception_records_attempt_without_invented_exit_code(tmp_path):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    agent.tools["run_shell"]["run"] = Mock(
        side_effect=RuntimeError("runner failed")
    )

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    approval = result.metadata["command_approval"]
    assert approval["runner_executed"] is True
    assert approval["outcome"] == "allowed"
    assert "exit_code" not in approval
    record = agent.checkpoint_store.load_tool_change_record(
        result.metadata["tool_change_id"]
    )
    assert record["status"] == "error"
    assert record["approval"] == approval


def test_before_capture_failure_does_not_create_nonexecuted_shell_record(
    tmp_path,
):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    runner = Mock(return_value=completed())
    agent.tools["run_shell"]["run"] = runner
    agent.workspace_observer.capture = Mock(
        side_effect=RuntimeError("capture failed")
    )

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert "exit_code" not in result.metadata["command_approval"]
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_before_capture_interrupt_does_not_create_nonexecuted_shell_record(
    tmp_path,
):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    runner = Mock(return_value=completed())
    agent.tools["run_shell"]["run"] = runner
    agent.workspace_observer.capture = Mock(side_effect=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool(
            "run_shell",
            {"command": "pwd", "timeout": 5},
        )

    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


@pytest.mark.parametrize(
    "runner_result",
    [
        "exit_code: 0\nstdout:\nok\nstderr:\n(empty)",
        None,
        {"stdout": "ok", "stderr": ""},
        {"stdout": "ok", "stderr": "", "exit_code": True},
    ],
)
def test_malformed_structured_runner_result_fails_closed(
    tmp_path,
    runner_result,
):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    agent.tools["run_shell"]["run"] = Mock(return_value=runner_result)

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert result.metadata["command_approval"]["runner_executed"] is True
    assert "exit_code" not in result.metadata["command_approval"]
    record = agent.checkpoint_store.load_tool_change_record(
        result.metadata["tool_change_id"]
    )
    assert record["status"] == "error"


def test_malformed_shell_result_after_side_effect_preserves_recovery_evidence(
    tmp_path,
):
    agent = build_agent(
        tmp_path,
        executables={"pwd": "/frozen/pwd"},
    )
    changed = tmp_path / "malformed-side-effect.txt"

    def malformed_after_write(_execution):
        changed.write_text("changed\n", encoding="utf-8")
        return {"stdout": "", "stderr": ""}

    agent.tools["run_shell"]["run"] = Mock(side_effect=malformed_after_write)

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "partial_success"
    assert result.metadata["tool_error_code"] == "tool_partial_success"
    assert result.metadata["affected_paths"] == ["malformed-side-effect.txt"]
    assert result.metadata["workspace_changed"] is True
    record = agent.checkpoint_store.load_tool_change_record(
        result.metadata["tool_change_id"]
    )
    assert record["status"] == "partial_success"
    assert record["affected_paths"] == ["malformed-side-effect.txt"]
    assert record["file_entries"][0]["path"] == "malformed-side-effect.txt"
    assert record["error"]["code"] == "tool_partial_success"


def test_pico_has_no_raw_tool_proxies_and_executor_remains_registered(tmp_path):
    agent = build_agent(tmp_path, executables={})

    for name in (
        "tool_list_files",
        "tool_read_file",
        "tool_search",
        "tool_run_shell",
        "tool_write_file",
        "tool_patch_file",
        "tool_delegate",
    ):
        assert not callable(getattr(agent, name, None)), name
    assert agent.tool_executor.agent is agent
    assert "read_file" in agent.tools
    assert "run_shell" in agent.tools
    assert "# README.md" in agent.execute_tool(
        "read_file",
        {"path": "README.md", "start": 1, "end": 1},
    ).content


def _init_git_repo(root):
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "pico@example.test"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Pico Test"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_production_git_path_disables_fsmonitor_and_optional_index_writes(
    tmp_path,
    monkeypatch,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.fsmonitor", str(hook)],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "README.md"
    index = tmp_path / ".git" / "index"
    control_bytes = index.read_bytes()
    control_stat = index.stat()
    tracked_stat = tracked.stat()
    os.utime(
        tracked,
        ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 10_000_000_000),
    )
    subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    refreshed_stat = index.stat()
    assert index.read_bytes() != control_bytes or (
        refreshed_stat.st_mtime_ns,
        refreshed_stat.st_size,
    ) != (control_stat.st_mtime_ns, control_stat.st_size)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(tmp_path, executables={"git": git})
    tracked_stat = tracked.stat()
    os.utime(
        tracked,
        ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 10_000_000_000),
    )
    before_bytes = index.read_bytes()
    before_stat = index.stat()
    calls = []
    real_run = tool_executor_module.run_hardened_git

    def capture_git(executable, args, **kwargs):
        result = real_run(executable, args, **kwargs)
        calls.append((executable, list(args), kwargs, result.args))
        return result

    monkeypatch.setattr(tool_executor_module, "run_hardened_git", capture_git)
    assert not marker.exists()

    result = agent.execute_tool(
        "run_shell",
        {"command": "git status --short", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["command_approval"]["runner_executed"] is True
    assert not marker.exists()
    assert len(calls) == 1
    executable, args, kwargs, hardened_argv = calls[0]
    assert executable == git
    assert args == ["status", "--short"]
    assert kwargs["cwd"] == tmp_path.resolve()
    assert "--no-pager" in hardened_argv
    assert "--no-optional-locks" in hardened_argv
    assert "-c" in hardened_argv
    assert "core.fsmonitor=false" in hardened_argv
    after_stat = index.stat()
    assert index.read_bytes() == before_bytes
    assert (after_stat.st_mtime_ns, after_stat.st_size) == (
        before_stat.st_mtime_ns,
        before_stat.st_size,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git -c core.fsmonitor=/tmp/marker status --short",
        "git -ccore.fsmonitor=/tmp/marker status --short",
        "git --config-env=core.fsmonitor=PICO_MARKER status --short",
        "git --paginate status --short",
        "git --exec-path=. status --short",
        "git --git-dir=../outside status --short",
        "git --work-tree=../outside status --short",
        "git diff --ext-diff",
        "git diff --textconv",
        "git submodule update",
    ],
)
def test_approved_git_cannot_override_hardening_or_run_config_helpers(
    tmp_path,
    monkeypatch,
    command,
):
    from pico import tool_executor as tool_executor_module

    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": "/frozen/git"},
    )
    agent.approve = Mock(return_value=True)
    hardened_git = Mock(side_effect=AssertionError("unsafe git reached runner"))
    monkeypatch.setattr(tool_executor_module, "run_hardened_git", hardened_git)

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 5},
    )

    assert agent.approve.call_count == 1
    hardened_git.assert_not_called()
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_arguments"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert "exit_code" not in result.metadata["command_approval"]
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_approved_git_config_override_cannot_execute_fsmonitor_marker(
    tmp_path,
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    marker = tmp_path / "approved-fsmonitor-ran"
    hook = tmp_path / "approved-fsmonitor-hook"
    hook.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    agent.approve = Mock(return_value=True)

    result = agent.execute_tool(
        "run_shell",
        {
            "command": f"git -c core.fsmonitor={hook} status --short",
            "timeout": 5,
        },
    )

    assert agent.approve.call_count == 1
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_arguments"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert not marker.exists()


def test_approved_git_fetch_blocks_repo_ssh_command_before_runner(
    tmp_path,
    monkeypatch,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    marker = tmp_path / "ssh-command-ran"
    ssh_command = tmp_path / "ssh-command-hook"
    ssh_command.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 1\n",
        encoding="utf-8",
    )
    ssh_command.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.sshCommand", str(ssh_command)],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "ssh://example.invalid/repo"],
        cwd=tmp_path,
        check=True,
    )
    approve = Mock(return_value=True)
    git_runner = Mock(side_effect=AssertionError("unsafe fetch reached runner"))
    agent.approve = approve
    monkeypatch.setattr(tool_executor_module, "run_hardened_git", git_runner)

    result = agent.execute_tool(
        "run_shell",
        {"command": "git fetch origin", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_config"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert "exit_code" not in result.metadata["command_approval"]
    assert approve.call_count == 1
    git_runner.assert_not_called()
    assert not marker.exists()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_approved_git_fetch_blocks_repo_uploadpack_before_execution(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    remote = tmp_path / "remote.git"
    subprocess.run([git, "init", "--bare", str(remote)], check=True)
    marker = tmp_path / "uploadpack-ran"
    uploadpack = tmp_path / "uploadpack-hook"
    uploadpack.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 1\n",
        encoding="utf-8",
    )
    uploadpack.chmod(0o755)
    subprocess.run(
        [git, "remote", "add", "origin", str(remote)],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [git, "config", "remote.origin.uploadpack", str(uploadpack)],
        cwd=tmp_path,
        check=True,
    )
    approve = Mock(return_value=True)
    agent.approve = approve

    result = agent.execute_tool(
        "run_shell",
        {"command": "git fetch origin", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_config"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert "exit_code" not in result.metadata["command_approval"]
    assert approve.call_count == 1
    assert not marker.exists()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_approved_git_fetch_without_dangerous_config_reaches_runner(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "missing.git")],
        cwd=tmp_path,
        check=True,
    )
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    approve = Mock(return_value=True)
    agent.approve = approve

    result = agent.execute_tool(
        "run_shell",
        {"command": "git fetch origin", "timeout": 5},
    )

    approval = result.metadata["command_approval"]
    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert approval["outcome"] == "approved"
    assert approval["runner_executed"] is True
    assert approval["exit_code"] != 0
    assert approve.call_count == 1
    records = agent.checkpoint_store.list_tool_change_records()
    assert len(records) == 1
    assert records[0]["approval"] == approval


def test_approved_git_fetch_blocks_unknown_remote_helper_protocol(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    helper_dir = tmp_path.parent / f"{tmp_path.name}-helper-bin"
    helper_dir.mkdir(mode=0o755)
    marker = tmp_path.parent / f"{tmp_path.name}-remote-helper-ran"
    helper = helper_dir / "git-remote-evil"
    helper.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 1\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    subprocess.run(
        [git, "remote", "add", "origin", "evil::payload"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [git, "config", "protocol.allow", "always"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [git, "config", "protocol.evil.allow", "always"],
        cwd=tmp_path,
        check=True,
    )
    poisoned_path = f"{helper_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    control_env = dict(os.environ, PATH=poisoned_path)
    control_env.pop("GIT_ALLOW_PROTOCOL", None)
    subprocess.run(
        [git, "fetch", "origin"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env=control_env,
    )
    assert marker.exists()
    marker.unlink()
    monkeypatch.setenv("PATH", poisoned_path)
    agent.approve = Mock(return_value=True)

    result = agent.execute_tool(
        "run_shell",
        {"command": "git fetch origin", "timeout": 5},
    )

    approval = result.metadata["command_approval"]
    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert approval["outcome"] == "approved"
    assert approval["runner_executed"] is True
    assert approval["exit_code"] != 0
    assert not marker.exists()
    assert len(agent.checkpoint_store.list_tool_change_records()) == 1


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://example.invalid/repo.git",
        "ssh://example.invalid/repo.git",
    ],
)
def test_approved_git_fetch_builtin_protocol_passes_preflight(
    tmp_path,
    monkeypatch,
    remote_url,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    subprocess.run(
        [git, "remote", "add", "origin", remote_url],
        cwd=tmp_path,
        check=True,
    )
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            [git, "fetch", "origin"],
            1,
            stdout="",
            stderr="expected test failure",
        )
    )
    agent.approve = Mock(return_value=True)
    monkeypatch.setattr(tool_executor_module, "run_hardened_git", runner)

    result = agent.execute_tool(
        "run_shell",
        {"command": "git fetch origin", "timeout": 5},
    )

    assert result.metadata["tool_error_code"] == "tool_failed"
    assert result.metadata["command_approval"]["runner_executed"] is True
    runner.assert_called_once()


def test_approved_unknown_git_command_cannot_execute_repo_alias(
    tmp_path,
    monkeypatch,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    marker = tmp_path / "git-alias-ran"
    subprocess.run(
        ["git", "config", "alias.pwn", f"!touch {marker}"],
        cwd=tmp_path,
        check=True,
    )
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    agent.approve = Mock(return_value=True)
    calls = []
    real_run = tool_executor_module.run_hardened_git

    def capture_git(executable, args, **kwargs):
        result = real_run(executable, args, **kwargs)
        calls.append(result.args)
        return result

    monkeypatch.setattr(tool_executor_module, "run_hardened_git", capture_git)
    assert not marker.exists()

    result = agent.execute_tool(
        "run_shell",
        {"command": "git pwn", "timeout": 5},
    )

    assert agent.approve.call_count == 1
    assert result.metadata["command_approval"]["runner_executed"] is True
    assert not marker.exists()
    assert len(calls) == 1
    assert "alias.pwn=" in calls[0]


@pytest.mark.parametrize(
    ("command", "expected_status"),
    [
        ("git log -p -1", "ok"),
        ("git blame sample.txt", "ok"),
        ("git annotate sample.txt", "ok"),
        ("git reflog show -p -1", "ok"),
        ("git range-diff HEAD^...HEAD HEAD^...HEAD", "rejected"),
    ],
)
def test_approved_git_diff_rendering_cannot_execute_repo_textconv(
    tmp_path,
    monkeypatch,
    command,
    expected_status,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    marker = tmp_path / "git-textconv-ran"
    textconv = tmp_path / "textconv-hook"
    textconv.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat \"$1\"\n",
        encoding="utf-8",
    )
    textconv.chmod(0o755)
    (tmp_path / ".gitattributes").write_text(
        "*.txt diff=evil\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.txt").write_text("sample\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "diff.evil.textconv", str(textconv)],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "add", ".gitattributes", "sample.txt"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add text file"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="ask",
        executables={"git": git},
    )
    agent.approve = Mock(return_value=True)
    calls = []
    real_run = tool_executor_module.run_hardened_git

    def capture_git(executable, args, **kwargs):
        result = real_run(executable, args, **kwargs)
        calls.append(result.args)
        return result

    monkeypatch.setattr(tool_executor_module, "run_hardened_git", capture_git)
    assert not marker.exists()

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 5},
    )

    assert agent.approve.call_count == 1
    assert result.metadata["tool_status"] == expected_status
    assert not marker.exists()
    if expected_status == "ok":
        assert len(calls) == 1
        assert "--no-ext-diff" in calls[0]
        assert "--no-textconv" in calls[0]
    else:
        assert result.metadata["tool_error_code"] == "unsafe_git_config"
        assert result.metadata["command_approval"]["runner_executed"] is False
        assert calls == []


def test_automatic_git_status_blocks_repo_clean_filter_before_execution(
    tmp_path,
    monkeypatch,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    workspace = WorkspaceContext.build(tmp_path)
    assert workspace.status == "clean"
    git = workspace.trusted_executables.get("git")
    assert git
    (tmp_path / ".gitattributes").write_text(
        "*.txt filter=evil\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes", "sample.txt"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add filtered file"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    marker = tmp_path / "git-clean-filter-ran"
    clean_filter = tmp_path / "clean-filter-hook"
    clean_filter.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o755)
    subprocess.run(
        ["git", "config", "filter.evil.clean", str(clean_filter)],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "sample.txt").write_text("changed\n", encoding="utf-8")
    guarded_workspace = WorkspaceContext.build(
        tmp_path,
        executables={"git": git},
    )
    assert guarded_workspace.status == "(unavailable)"
    assert not marker.exists()
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )
    assert agent.workspace_observer.capture()["mode"] == "filesystem"
    assert not marker.exists()
    approve = Mock(return_value=True)
    user_git_runner = Mock(side_effect=AssertionError("user git reached runner"))
    agent.approve = approve
    monkeypatch.setattr(
        tool_executor_module,
        "run_hardened_git",
        user_git_runner,
    )

    result = agent.execute_tool(
        "run_shell",
        {"command": "git status --short", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_config"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert "exit_code" not in result.metadata["command_approval"]
    approve.assert_not_called()
    user_git_runner.assert_not_called()
    assert not marker.exists()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_non_repo_git_status_reaches_runner_and_returns_nonzero(tmp_path):
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="auto",
        executables={"git": git},
    )
    approve = Mock(return_value=True)
    agent.approve = approve

    result = agent.execute_tool(
        "run_shell",
        {"command": "git status --short", "timeout": 5},
    )

    approval = result.metadata["command_approval"]
    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "tool_failed"
    assert approval["decision"] == "allow"
    assert approval["outcome"] == "allowed"
    assert approval["runner_executed"] is True
    assert approval["execution_mode"] == "argv"
    assert approval["exit_code"] != 0
    approve.assert_not_called()
    records = agent.checkpoint_store.list_tool_change_records()
    assert len(records) == 1
    assert records[0]["approval"] == approval


def test_automatic_git_status_blocks_repo_worktree_escape(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / "victim.txt").write_text("inside\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    subprocess.run(["git", "add", "victim.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add victim"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    git = WorkspaceContext.build(tmp_path).trusted_executables.get("git")
    assert git
    agent = build_agent(
        tmp_path,
        approval_policy="auto",
        executables={"git": git},
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside-worktree"
    outside.mkdir()
    (outside / "victim.txt").write_text("outside changed\n", encoding="utf-8")
    subprocess.run(
        [git, "config", "core.worktree", str(outside)],
        cwd=tmp_path,
        check=True,
    )
    guarded_workspace = WorkspaceContext.build(
        tmp_path,
        executables={"git": git},
    )
    assert guarded_workspace.repo_root == str(tmp_path.resolve())
    assert guarded_workspace.status == "(unavailable)"

    result = agent.execute_tool(
        "run_shell",
        {"command": "git status --short", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_config"
    assert result.metadata["command_approval"]["outcome"] == "blocked"
    assert result.metadata["command_approval"]["runner_executed"] is False
    assert "exit_code" not in result.metadata["command_approval"]
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_automatic_parent_git_status_blocks_uninspected_submodule_config(
    tmp_path,
    monkeypatch,
):
    from pico import tool_executor as tool_executor_module

    (tmp_path / "README.md").write_text("parent\n", encoding="utf-8")
    _init_git_repo(tmp_path)
    child_source = tmp_path.parent / f"{tmp_path.name}-child-source"
    child_source.mkdir()
    (child_source / "README.md").write_text("child\n", encoding="utf-8")
    _init_git_repo(child_source)
    (child_source / ".gitattributes").write_text(
        "*.txt filter=evil\n",
        encoding="utf-8",
    )
    (child_source / "sample.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes", "sample.txt"],
        cwd=child_source,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add filtered file"],
        cwd=child_source,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child_source),
            "modules/child",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-am", "add submodule"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    workspace = WorkspaceContext.build(tmp_path)
    git = workspace.trusted_executables.get("git")
    assert git
    child = tmp_path / "modules" / "child"
    marker = tmp_path / "submodule-clean-filter-ran"
    clean_filter = tmp_path / "submodule-clean-filter-hook"
    clean_filter.write_text(
        f"#!/bin/sh\ntouch {marker}\ncat\n",
        encoding="utf-8",
    )
    clean_filter.chmod(0o755)
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"],
        cwd=child,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--worktree", "filter.evil.clean", str(clean_filter)],
        cwd=child,
        check=True,
    )
    tracked = child / "sample.txt"
    tracked_stat = tracked.stat()
    os.utime(
        tracked,
        ns=(tracked_stat.st_atime_ns, tracked_stat.st_mtime_ns + 10_000_000_000),
    )
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )
    approve = Mock(return_value=True)
    user_git_runner = Mock(side_effect=AssertionError("user git reached runner"))
    agent.approve = approve
    monkeypatch.setattr(
        tool_executor_module,
        "run_hardened_git",
        user_git_runner,
    )

    result = agent.execute_tool(
        "run_shell",
        {"command": "git status --short", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "unsafe_git_config"
    assert result.metadata["command_approval"]["runner_executed"] is False
    approve.assert_not_called()
    user_git_runner.assert_not_called()
    assert not marker.exists()
    assert agent.checkpoint_store.list_tool_change_records() == []
