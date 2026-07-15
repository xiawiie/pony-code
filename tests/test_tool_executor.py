import json
import subprocess
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
import pico.tool_executor as tool_executor_module
from pico.memory.tools import tool_memory_list, tool_memory_search
from pico.tool_executor import (
    ToolExecutor,
    ToolExecutionResult,
    _capture_path_snapshot,
    _effect_class,
    _fill_git_head_before_file_states,
)


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient([] if outputs is None else outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def init_git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "pico@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Pico Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)


def read_trace(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_tool_executor_returns_content_and_metadata_without_side_channel(tmp_path):
    agent = build_agent(tmp_path)

    result = ToolExecutor(agent).execute("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert isinstance(result, ToolExecutionResult)
    assert "# README.md" in result.content
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["effect_class"] == "read_only"
    assert result.metadata["read_only"] is True
    assert result.metadata["workspace_changed"] is False


def test_effect_class_table_is_explicit():
    assert _effect_class("read_file", False) == "read_only"
    assert _effect_class("delegate", False) == "read_only"
    assert _effect_class("memory_save", False) == "memory_write"
    assert _effect_class("write_file", True) == "workspace_write"
    assert _effect_class("unknown_safe", False) == "read_only"
    assert _effect_class("unknown_risky", True) == "workspace_write"


@pytest.mark.parametrize(
    (
        "scenario",
        "name",
        "arguments",
        "options",
        "expected_code",
        "expected_security",
        "expected_effect",
    ),
    [
        ("unknown", "unknown_tool", {}, {}, "unknown_tool", "", "workspace_write"),
        (
            "disallowed",
            "write_file",
            {"path": "blocked.txt", "content": "no"},
            {"allowed_tools": {"read_file"}},
            "tool_not_allowed",
            "",
            "workspace_write",
        ),
        (
            "invalid",
            "write_file",
            {"path": "missing-content.txt"},
            {},
            "invalid_arguments",
            "",
            "workspace_write",
        ),
        (
            "sensitive",
            "memory_save",
            {"note": "github_pat_A123456789012345678901234567890"},
            {},
            "sensitive_content_block",
            "sensitive_access_block",
            "memory_write",
        ),
        (
            "read_only",
            "memory_save",
            {"note": "remember this"},
            {"read_only": True},
            "read_only_block",
            "read_only_block",
            "memory_write",
        ),
        (
            "repeated",
            "write_file",
            {"path": "repeat.txt", "content": "no"},
            {},
            "repeated_identical_call",
            "",
            "workspace_write",
        ),
        (
            "approval_denied",
            "write_file",
            {"path": "denied.txt", "content": "no"},
            {},
            "approval_denied",
            "approval_denied",
            "workspace_write",
        ),
    ],
)
def test_early_rejection_matrix_has_exact_metadata_and_no_execution_evidence(
    tmp_path,
    monkeypatch,
    scenario,
    name,
    arguments,
    options,
    expected_code,
    expected_security,
    expected_effect,
):
    agent = build_agent(tmp_path, **options)
    runner = Mock(return_value="must not run")
    if name in agent.tools:
        agent.tools[name]["run"] = runner
    if scenario == "repeated":
        monkeypatch.setattr(agent, "repeated_tool_call", lambda *_args: True)
    if scenario == "approval_denied":
        monkeypatch.setattr(agent, "approve", Mock(return_value=False))

    result = agent.execute_tool(name, arguments)

    assert result.metadata == {
        "tool_status": "rejected",
        "tool_error_code": expected_code,
        "security_event_type": expected_security,
        "risk_level": "high",
        "effect_class": expected_effect,
        "read_only": expected_effect == "read_only",
        "affected_paths": [],
        "workspace_changed": False,
        "diff_summary": [],
        "policy_decision": {
            "schema_version": 1,
            "decision": "deny",
            "reason_code": expected_code,
            "effect_class": expected_effect,
            "risk_class": "complex",
            "evidence_complete": True,
            "approval": {
                "mode": "auto",
                "required": False,
                "outcome": "denied",
            },
        },
        "sandbox": {"status": "not_started"},
    }
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_read_only_agent_rejects_memory_write_before_runner(tmp_path):
    agent = build_agent(tmp_path, read_only=True)
    runner = Mock(return_value="must not run")
    agent.tools["memory_save"]["run"] = runner

    result = agent.execute_tool("memory_save", {"note": "remember this"})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["effect_class"] == "memory_write"
    assert result.metadata["read_only"] is False
    assert result.metadata["tool_error_code"] == "read_only_block"
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("read_file", {"path": "README.md"}),
        ("delegate", {"task": "inspect README", "max_steps": 1}),
    ],
)
def test_read_only_agent_allows_read_effects(tmp_path, name, arguments):
    agent = build_agent(tmp_path, read_only=True)
    agent.tools[name]["run"] = Mock(return_value="ok")

    result = agent.execute_tool(name, arguments)

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["effect_class"] == "read_only"
    assert result.metadata["read_only"] is True


def test_memory_write_is_audited_without_workspace_snapshot_or_recovery_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        outputs=[
            '<tool>{"name":"memory_save","args":{"note":"remember this"}}</tool>',
            "<final>done</final>",
        ],
    )

    assert agent.ask("remember this") == "done"

    memory_event = next(
        event
        for event in read_trace(agent)
        if event.get("event") == "tool_executed" and event.get("name") == "memory_save"
    )
    assert memory_event["tool_status"] == "ok"
    assert memory_event["effect_class"] == "memory_write"
    assert memory_event["read_only"] is False
    assert memory_event["tool_change_id"]
    assert memory_event["affected_paths"] == []
    assert agent.current_task_state.recovery_checkpoint_id == ""


def test_runner_keyboard_interrupt_finalizes_pending_change_then_reraises(tmp_path):
    agent = build_agent(tmp_path)
    agent.tools["write_file"]["run"] = lambda args: (_ for _ in ()).throw(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("write_file", {"path": "x.txt", "content": "x"})

    records = agent.checkpoint_store.list_tool_change_records()
    assert records[-1]["status"] == "interrupted"


def test_public_runtime_rejects_legacy_sandbox_context_before_runner(tmp_path):
    with pytest.raises(ValueError, match="DockerSandboxContext"):
        build_agent(tmp_path, sandbox_context=SimpleNamespace())


def test_shell_runner_interrupt_persists_attempted_approval_metadata(tmp_path):
    agent = build_agent(tmp_path)
    agent.tools["run_shell"]["run"] = Mock(side_effect=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("run_shell", {"command": "pwd", "timeout": 5})

    record = agent.checkpoint_store.list_tool_change_records()[-1]
    assert record["status"] == "interrupted"
    assert record["approval"]["runner_executed"] is True
    assert record["approval"]["outcome"] == "allowed"
    assert record["approval"]["execution_mode"] == "argv"
    assert "exit_code" not in record["approval"]


def test_post_runner_interrupt_closes_workspace_change_then_reraises(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    original_capture = agent.workspace_observer.capture
    capture_calls = 0

    def interrupt_after_runner():
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise KeyboardInterrupt()
        return original_capture()

    agent.tools["run_shell"]["run"] = lambda execution: {
        "stdout": "ok\n",
        "stderr": "",
        "exit_code": 0,
    }
    monkeypatch.setattr(agent.workspace_observer, "capture", interrupt_after_runner)

    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("run_shell", {"command": "pwd", "timeout": 5})

    records = agent.checkpoint_store.list_tool_change_records()
    assert records[-1]["status"] == "interrupted"
    assert records[-1]["approval"]["runner_executed"] is True
    assert records[-1]["approval"]["outcome"] == "allowed"
    assert records[-1]["approval"]["execution_mode"] == "argv"
    assert records[-1]["approval"]["exit_code"] == 0


def test_post_runner_interrupt_closes_memory_audit_then_reraises(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    agent.tools["memory_save"]["run"] = lambda args: "saved"
    monkeypatch.setattr(
        agent,
        "update_memory_after_tool",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("memory_save", {"note": "remember this"})

    records = agent.checkpoint_store.list_tool_change_records()
    assert records[-1]["effect_class"] == "memory_write"
    assert records[-1]["status"] == "interrupted"


class FatalToolSignal(BaseException):
    pass


def test_post_pending_system_exit_closes_change_and_preserves_primary(tmp_path):
    agent = build_agent(tmp_path)
    primary = SystemExit("runner stopped")
    agent.tools["write_file"]["run"] = Mock(side_effect=primary)

    with pytest.raises(SystemExit) as caught:
        agent.execute_tool("write_file", {"path": "x.txt", "content": "x"})

    assert caught.value is primary
    assert agent.checkpoint_store.list_tool_change_records()[-1]["status"] == "interrupted"


def test_post_pending_custom_base_exception_closes_change_and_preserves_primary(tmp_path):
    agent = build_agent(tmp_path)
    primary = FatalToolSignal("runner stopped")
    agent.tools["write_file"]["run"] = Mock(side_effect=primary)

    with pytest.raises(FatalToolSignal) as caught:
        agent.execute_tool("write_file", {"path": "x.txt", "content": "x"})

    assert caught.value is primary
    assert agent.checkpoint_store.list_tool_change_records()[-1]["status"] == "interrupted"


def test_fatal_interrupt_observes_workspace_effect_before_terminalizing(tmp_path):
    agent = build_agent(tmp_path)

    def write_then_interrupt(args):
        (tmp_path / args["path"]).write_text(args["content"], encoding="utf-8")
        raise FatalToolSignal("interrupted after write")

    agent.tools["write_file"]["run"] = write_then_interrupt
    with pytest.raises(FatalToolSignal):
        agent.execute_tool("write_file", {"path": "x.txt", "content": "x"})

    record = agent.checkpoint_store.list_tool_change_records()[-1]
    assert record["status"] == "interrupted"
    assert record["affected_paths"] == ["x.txt"]
    assert len(record["file_entries"]) == 1
    assert record["file_entries"][0]["path"] == "x.txt"
    assert record["file_entries"][0]["after_blob_ref"]

    blocked = agent.execute_tool(
        "write_file", {"path": "second.txt", "content": "second"}
    )
    assert blocked.metadata["tool_error_code"] == "recovery_review_required"
    assert not (tmp_path / "second.txt").exists()


def test_post_pending_observer_base_exception_closes_change_and_preserves_primary(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    primary = FatalToolSignal("observer stopped")
    original_capture = agent.workspace_observer.capture
    captures = 0

    def fail_after_runner():
        nonlocal captures
        captures += 1
        if captures == 2:
            raise primary
        return original_capture()

    agent.tools["run_shell"]["run"] = Mock(
        return_value={"stdout": "ok\n", "stderr": "", "exit_code": 0}
    )
    monkeypatch.setattr(agent.workspace_observer, "capture", fail_after_runner)

    with pytest.raises(FatalToolSignal) as caught:
        agent.execute_tool("run_shell", {"command": "pwd", "timeout": 5})

    assert caught.value is primary
    assert agent.checkpoint_store.list_tool_change_records()[-1]["status"] == "interrupted"


def test_post_pending_verification_base_exception_closes_change_and_preserves_primary(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    primary = FatalToolSignal("verification stopped")
    agent.tools["run_shell"]["run"] = Mock(
        return_value={"stdout": "ok\n", "stderr": "", "exit_code": 0}
    )
    monkeypatch.setattr(
        "pico.tool_executor.verification_evidence_for_execution",
        Mock(side_effect=primary),
    )

    with pytest.raises(FatalToolSignal) as caught:
        agent.execute_tool("run_shell", {"command": "pwd", "timeout": 5})

    assert caught.value is primary
    assert agent.checkpoint_store.list_tool_change_records()[-1]["status"] == "interrupted"


def test_post_pending_memory_update_base_exception_closes_change_and_preserves_primary(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    primary = FatalToolSignal("memory update stopped")
    agent.tools["memory_save"]["run"] = Mock(return_value="saved")
    monkeypatch.setattr(agent, "update_memory_after_tool", Mock(side_effect=primary))

    with pytest.raises(FatalToolSignal) as caught:
        agent.execute_tool("memory_save", {"note": "remember this"})

    assert caught.value is primary
    record = agent.checkpoint_store.list_tool_change_records()[-1]
    assert record["effect_class"] == "memory_write"
    assert record["status"] == "interrupted"


def test_effect_observation_is_not_repeated_after_memory_update_failure(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    capture = Mock(wraps=agent.workspace_observer.capture)
    agent.tools["run_shell"]["run"] = Mock(
        return_value={"stdout": "ok\n", "stderr": "", "exit_code": 0}
    )
    monkeypatch.setattr(agent.workspace_observer, "capture", capture)
    monkeypatch.setattr(
        agent,
        "update_memory_after_tool",
        Mock(side_effect=OSError("memory update failed")),
    )

    result = agent.execute_tool(
        "run_shell",
        {"command": "pwd", "timeout": 5},
    )

    assert result.metadata["tool_error_code"] == "tool_finalize_failed"
    assert capture.call_count == 2  # one before-state and one after-state read


def test_unknown_effect_after_runner_failure_requires_review(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)

    def write_then_fail(args):
        (tmp_path / args["path"]).write_text("changed\n", encoding="utf-8")
        raise RuntimeError("runner failed")

    agent.tools["write_file"]["run"] = write_then_fail
    monkeypatch.setattr(
        tool_executor_module,
        "_observe_tool_effects",
        Mock(side_effect=OSError("observer failed")),
    )

    first = agent.execute_tool(
        "write_file", {"path": "first.txt", "content": "unused"}
    )

    assert first.metadata["tool_status"] == "error"
    assert first.metadata["tool_error_code"] == "recovery_review_required"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "changed\n"
    record = agent.checkpoint_store.load_tool_change_record(
        first.metadata["tool_change_id"]
    )
    assert record["status"] == "interrupted"

    second = agent.execute_tool(
        "write_file", {"path": "second.txt", "content": "second"}
    )
    assert second.metadata["tool_error_code"] == "recovery_review_required"
    assert not (tmp_path / "second.txt").exists()


def test_post_runner_finalization_failure_keeps_effect_evidence(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    changed = tmp_path / "after-run.txt"

    def write_then_return(_execution):
        changed.write_text("changed\n", encoding="utf-8")
        return {"stdout": "", "stderr": "", "exit_code": 0}

    agent.tools["run_shell"]["run"] = write_then_return
    monkeypatch.setattr(
        agent,
        "update_memory_after_tool",
        Mock(side_effect=OSError("memory update failed")),
    )

    result = agent.execute_tool(
        "run_shell", {"command": "pwd", "timeout": 5}
    )

    assert result.metadata["tool_error_code"] == "tool_finalize_failed"
    assert result.metadata["affected_paths"] == ["after-run.txt"]
    assert result.metadata["file_entries"][0]["path"] == "after-run.txt"
    record = agent.checkpoint_store.load_tool_change_record(
        result.metadata["tool_change_id"]
    )
    assert record["status"] == "partial_success"
    assert record["file_entries"][0]["path"] == "after-run.txt"
    assert record["shell_side_effects"]


def test_recorder_start_persisted_then_raised_leaves_review_evidence(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    real_start = agent.tool_change_recorder.start
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    def persisted_then_raised(*args, **kwargs):
        real_start(*args, **kwargs)
        raise OSError("start failed after persistence")

    monkeypatch.setattr(agent.tool_change_recorder, "start", persisted_then_raised)

    first = agent.execute_tool(
        "write_file",
        {"path": "first.txt", "content": "first"},
    )
    second = agent.execute_tool(
        "write_file",
        {"path": "second.txt", "content": "second"},
    )

    assert first.metadata["tool_status"] == "error"
    assert first.metadata["tool_error_code"] == "tool_failed"
    records = agent.checkpoint_store.list_tool_change_records()
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert second.metadata["tool_status"] == "rejected"
    assert second.metadata["tool_error_code"] == "recovery_review_required"
    runner.assert_not_called()


def test_missing_memory_dependencies_and_files_never_report_ok(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    agent.tools["memory_list"]["run"] = lambda args: tool_memory_list(
        SimpleNamespace(memory_store=None), args
    )
    missing_store = agent.execute_tool("memory_list", {})
    assert missing_store.metadata["tool_status"] == "error"

    agent.tools["memory_search"]["run"] = lambda args: tool_memory_search(
        SimpleNamespace(memory_retrieval=None), args
    )
    missing_retrieval = agent.execute_tool("memory_search", {"query": "cache"})
    assert missing_retrieval.metadata["tool_status"] == "error"

    missing_file = agent.execute_tool("memory_read", {"path": "workspace/notes/missing.md"})
    assert missing_file.metadata["tool_status"] == "error"

    context = agent.tools["memory_read"]["run"].args[0]
    monkeypatch.setattr(context.memory_store, "read", Mock(side_effect=OSError("memory disk failed")))
    io_error = agent.execute_tool("memory_read", {"path": "workspace/notes/other.md"})
    assert io_error.metadata["tool_status"] == "error"


@pytest.mark.parametrize("removed_field", ("topic", "type"))
def test_removed_memory_save_fields_are_rejected_before_runner(
    tmp_path,
    removed_field,
):
    agent = build_agent(tmp_path)
    runner = Mock(return_value="must not run")
    agent.tools["memory_save"]["run"] = runner

    result = agent.execute_tool(
        "memory_save",
        {"note": "remember this", removed_field: ""},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert "memory_save accepts only note and scope" in result.content
    runner.assert_not_called()


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


def test_success_and_exception_paths_persist_terminal_effect_evidence(tmp_path):
    agent = build_agent(tmp_path)
    success = agent.execute_tool("write_file", {"path": "ok.txt", "content": "ok\n"})

    def boom(args):
        (tmp_path / args["path"]).write_text("partial\n", encoding="utf-8")
        raise RuntimeError("boom")

    agent.tools["write_file"]["run"] = boom
    failure = agent.execute_tool("write_file", {"path": "partial.txt", "content": "unused\n"})

    assert success.metadata["tool_status"] == "ok"
    assert failure.metadata["tool_status"] == "partial_success"
    success_record = agent.checkpoint_store.load_tool_change_record(
        success.metadata["tool_change_id"]
    )
    failure_record = agent.checkpoint_store.load_tool_change_record(
        failure.metadata["tool_change_id"]
    )
    assert success_record["status"] == "finalized"
    assert success_record["affected_paths"] == ["ok.txt"]
    assert failure_record["status"] == "partial_success"
    assert failure_record["affected_paths"] == ["partial.txt"]
    assert failure_record["error"]["code"] == "tool_partial_success"


def test_run_shell_uses_command_policy_metadata(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    agent.approve = Mock(return_value=True)

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["command_risk_class"] == "workspace_write"
    assert result.metadata["command_approval"]["decision"] == "ask"
    assert result.metadata["command_approval"]["outcome"] == "approved"
    assert result.metadata["command_approval"]["runner_executed"] is True
    assert "generated.txt" in result.metadata["affected_paths"]


def test_run_shell_rechecks_command_policy_after_approval_mutation(
    tmp_path,
    monkeypatch,
):
    import pico.tool_executor as tool_executor

    agent = build_agent(tmp_path, approval_policy="ask")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    runner = Mock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
    agent.tools["run_shell"]["run"] = runner
    policy_calls = []
    real_policy = tool_executor.assess_command

    def record_policy(command, workspace_root):
        result = real_policy(command, workspace_root)
        policy_calls.append(result["risk_class"])
        return result

    monkeypatch.setattr(tool_executor, "assess_command", record_policy)

    def approve(name, args):
        args["command"] = "rm -f victim.txt"
        return True

    agent.approve = approve

    result = agent.execute_tool(
        "run_shell",
        {"command": "printf safe > approved.txt", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "approval_arguments_changed"
    assert result.metadata["command_risk_class"] == "workspace_write"
    assert policy_calls == ["workspace_write", "workspace_write"]
    assert victim.read_text(encoding="utf-8") == "keep\n"
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_run_shell_rejects_safe_arguments_changed_after_approval(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    runner = Mock(return_value={"stdout": "", "stderr": "", "exit_code": 0})
    agent.tools["run_shell"]["run"] = runner

    def approve(name, args):
        args["command"] = "printf changed > unapproved.txt"
        return True

    agent.approve = approve

    result = agent.execute_tool(
        "run_shell",
        {"command": "printf safe > approved.txt", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "approval_arguments_changed"
    assert not (tmp_path / "approved.txt").exists()
    assert not (tmp_path / "unapproved.txt").exists()
    runner.assert_not_called()
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_destructive_run_shell_is_not_auto_approved(tmp_path):
    agent = build_agent(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")

    result = agent.execute_tool("run_shell", {"command": "rm -f victim.txt", "timeout": 5})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "command_approval_required"
    assert result.metadata["command_approval"]["decision"] == "ask"
    assert victim.exists()
    assert "tool_change_id" not in result.metadata


def test_destructive_run_shell_wrapped_forms_are_not_auto_approved(tmp_path):
    for command in (
        "rm -f victim.txt $(echo ignored)",
        "(rm -f victim.txt)",
        "{ rm -f victim.txt; }",
        "echo ok\nrm -f victim.txt",
        "env rm -f victim.txt",
        "find . -name victim.txt -delete",
        "git -C . reset --hard",
    ):
        agent = build_agent(tmp_path)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")

        result = agent.execute_tool("run_shell", {"command": command, "timeout": 5})

        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata["tool_error_code"] == "command_approval_required"
        assert result.metadata["command_approval"]["decision"] == "ask"
        assert victim.read_text(encoding="utf-8") == "keep\n"
        assert "tool_change_id" not in result.metadata


@pytest.mark.parametrize(
    ("command", "expected_error"),
    [
        ("ls README.md\nrm victim.txt", "command_approval_required"),
        ("cat README.md > .e\\\nnv", "sensitive_path_block"),
        ("wc {,.}env", "command_approval_required"),
    ],
)
def test_shell_expansion_bypasses_never_reach_runner(
    tmp_path,
    command,
    expected_error,
):
    agent = build_agent(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    runner = Mock(return_value="must not run")
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool("run_shell", {"command": command, "timeout": 5})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == expected_error
    runner.assert_not_called()
    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / ".env").exists()


def test_write_file_recovery_does_not_use_full_workspace_snapshot(tmp_path):
    agent = build_agent(tmp_path)

    def fail_full_snapshot():
        raise AssertionError("full snapshot should not run")

    agent.capture_workspace_snapshot = fail_full_snapshot

    result = agent.execute_tool("write_file", {"path": "note.txt", "content": "hello\n"})

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["affected_paths"] == ["note.txt"]


def test_run_shell_recovery_does_not_use_full_workspace_snapshot(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    agent.approve = Mock(return_value=True)

    def fail_full_snapshot():
        raise AssertionError("full snapshot should not run")

    agent.capture_workspace_snapshot = fail_full_snapshot

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    assert "generated.txt" in result.metadata["affected_paths"]


def test_run_shell_recovery_does_not_blob_unrelated_dirty_paths(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    agent.approve = Mock(return_value=True)
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("user dirty\n", encoding="utf-8")
    written_blobs = []
    original_write_blob = agent.checkpoint_store.write_blob

    def spy_write_blob(data, content_kind="text"):
        written_blobs.append(bytes(data))
        return original_write_blob(data, content_kind)

    agent.checkpoint_store.write_blob = spy_write_blob

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    assert "generated.txt" in result.metadata["affected_paths"]
    assert b"user dirty\n" not in written_blobs


def test_run_shell_recovery_marks_dirty_before_tracked_file_unrestorable(tmp_path):
    agent = build_agent(tmp_path, approval_policy="ask")
    agent.approve = Mock(return_value=True)
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("user dirty\n", encoding="utf-8")

    result = agent.execute_tool("run_shell", {"command": "printf 'tool changed\\n' > README.md", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    entry = result.metadata["file_entries"][0]
    assert entry["path"] == "README.md"
    assert entry["change_kind"] == "modified"
    assert entry["before_blob_ref"] == ""
    assert entry["snapshot_eligible"] is False
    assert entry["ineligible_reason"] == "mode_unknown"
    assert entry["before_mode"] is None
    assert entry["after_mode"] is None


def test_run_shell_recovery_populates_before_blob_from_git_head(tmp_path):
    # clean tracked file: observer 看不到，HEAD fallback 应该把 before-blob 抓下来。
    agent = build_agent(tmp_path, approval_policy="ask")
    agent.approve = Mock(return_value=True)
    init_git_repo(tmp_path)

    result = agent.execute_tool("run_shell", {"command": "printf 'tool changed\\n' > README.md", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    entry = result.metadata["file_entries"][0]
    assert entry["path"] == "README.md"
    assert entry["change_kind"] == "modified"
    assert entry["before_blob_ref"]
    assert agent.checkpoint_store.read_blob(entry["before_blob_ref"]) == b"demo\n"
    assert entry["snapshot_eligible"] is True
    assert result.metadata["diff_summary"] == ["modified:README.md"]


def test_git_head_fallback_uses_frozen_hardened_git(tmp_path, monkeypatch):
    import pico.tool_executor as tool_executor

    calls = []

    def fake_git(executable, args, **kwargs):
        calls.append((executable, list(args), kwargs))
        if args[0] == "ls-tree":
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    b"100644 blob "
                    + b"c" * 40
                    + b" 9\tREADME.md\0"
                ),
                stderr=b"",
            )
        return subprocess.CompletedProcess([], 0, stdout=b"original\n", stderr=b"")

    monkeypatch.setattr(tool_executor, "run_hardened_git", fake_git, raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bare git executed")
        ),
    )

    class Store:
        @staticmethod
        def write_blob(data, content_kind):
            assert data == b"original\n"
            assert content_kind == "text"
            return {"blob_ref": "a" * 64, "content_hash": "b" * 64}

    agent = SimpleNamespace(
        root=tmp_path,
        trusted_executables=MappingProxyType({"git": "/frozen/git"}),
        checkpoint_store=Store(),
    )

    states = _fill_git_head_before_file_states(agent, ["README.md"], {})

    assert states["README.md"]["before_blob_ref"] == "a" * 64
    assert states["README.md"]["before_mode"] == 0o644
    assert calls == [
        (
            "/frozen/git",
            ["ls-tree", "-l", "-z", "HEAD", "--", "README.md"],
            {"cwd": tmp_path, "text": False},
        ),
        (
            "/frozen/git",
            ["show", "c" * 40],
            {"cwd": tmp_path, "text": False},
        ),
    ]


def test_git_head_fallback_without_frozen_git_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bare git executed")
        ),
    )
    agent = SimpleNamespace(
        root=tmp_path,
        trusted_executables=MappingProxyType({}),
        checkpoint_store=Mock(),
    )

    states = _fill_git_head_before_file_states(agent, ["README.md"], {})

    assert states == {}
    agent.checkpoint_store.write_blob.assert_not_called()


def test_git_head_fallback_rejects_sensitive_path_before_git_or_blob(
    tmp_path,
    monkeypatch,
):
    import pico.tool_executor as tool_executor

    git = Mock(side_effect=AssertionError("git must not run"))
    monkeypatch.setattr(tool_executor, "run_hardened_git", git)
    agent = SimpleNamespace(
        root=tmp_path,
        trusted_executables=MappingProxyType({"git": "/frozen/git"}),
        checkpoint_store=Mock(),
        redaction_env={},
        secret_env_names=(),
    )

    states = _fill_git_head_before_file_states(agent, [".env"], {})

    assert states == {}
    git.assert_not_called()
    agent.checkpoint_store.write_blob.assert_not_called()


def test_git_head_fallback_rejects_sensitive_descendant_before_git_or_blob(
    tmp_path,
    monkeypatch,
):
    import pico.tool_executor as tool_executor

    git = Mock(side_effect=AssertionError("git must not run"))
    monkeypatch.setattr(tool_executor, "run_hardened_git", git)
    agent = SimpleNamespace(
        root=tmp_path,
        trusted_executables=MappingProxyType({"git": "/frozen/git"}),
        checkpoint_store=Mock(),
        redaction_env={},
        secret_env_names=(),
    )

    states = _fill_git_head_before_file_states(
        agent,
        [".env/child.txt"],
        {},
    )

    assert states == {}
    git.assert_not_called()
    agent.checkpoint_store.write_blob.assert_not_called()


def test_git_head_fallback_rejects_secret_stdout_before_blob(
    tmp_path,
    monkeypatch,
):
    import pico.tool_executor as tool_executor

    secret = "opaque-head-value-123456789"
    git = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            0,
            stdout=("prefix " + secret).encode(),
            stderr=b"",
        )
    )
    monkeypatch.setattr(tool_executor, "run_hardened_git", git)
    agent = SimpleNamespace(
        root=tmp_path,
        trusted_executables=MappingProxyType({"git": "/frozen/git"}),
        checkpoint_store=Mock(),
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    states = _fill_git_head_before_file_states(agent, ["README.md"], {})

    assert states == {}
    git.assert_called_once()
    agent.checkpoint_store.write_blob.assert_not_called()


def test_git_head_fallback_rejects_symlink_and_oversized_tree_entries(
    tmp_path, monkeypatch
):
    import pico.tool_executor as tool_executor

    git = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                b"120000 blob " + b"c" * 40 + b" 12\tREADME.md\0"
            ),
            stderr=b"",
        )
    )
    monkeypatch.setattr(tool_executor, "run_hardened_git", git)
    agent = SimpleNamespace(
        root=tmp_path,
        trusted_executables=MappingProxyType({"git": "/frozen/git"}),
        checkpoint_store=Mock(),
        project_max_blob_size=8,
    )

    states = _fill_git_head_before_file_states(agent, ["README.md"], {})

    assert states == {}
    git.assert_called_once()
    agent.checkpoint_store.write_blob.assert_not_called()


def test_path_snapshot_never_hashes_safe_named_secret_content(
    tmp_path,
    monkeypatch,
):
    import pico.tool_executor as tool_executor

    secret = "opaque-snapshot-value-123456789"
    (tmp_path / "source.py").write_text(secret, encoding="utf-8")
    monkeypatch.setattr(
        tool_executor,
        "hash_bytes",
        Mock(side_effect=AssertionError("secret content hashed")),
    )
    agent = SimpleNamespace(
        root=tmp_path,
        project_max_blob_size=1024,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    assert _capture_path_snapshot(agent, ["source.py"]) == {}


def test_workspace_write_tool_uses_generic_path_argument_for_recovery(tmp_path):
    agent = build_agent(tmp_path)

    def custom_write(args):
        (tmp_path / args["path"]).write_text("custom\n", encoding="utf-8")
        return "ok"

    agent.tools["custom_write"] = {
        "schema": {"path": "str"},
        "risky": True,
        "description": "Custom write tool used by the test.",
        "run": custom_write,
    }

    result = agent.execute_tool("custom_write", {"path": "custom.txt"})

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["affected_paths"] == ["custom.txt"]
    assert result.metadata["file_entries"][0]["path"] == "custom.txt"


def test_generic_path_arg_registry_covers_destination_and_paths_list(tmp_path):
    # 目的：future tool（move_file / delete_files 等）用非 "path" 参数时也要能记录 recovery。
    agent = build_agent(tmp_path)
    (tmp_path / "existing.txt").write_text("original\n", encoding="utf-8")

    def custom_move(args):
        source = tmp_path / args["source"]
        destination = tmp_path / args["destination"]
        destination.write_bytes(source.read_bytes())
        source.unlink()
        return "moved"

    agent.tools["custom_move"] = {
        "schema": {"source": "str", "destination": "str"},
        "risky": True,
        "description": "Move a file.",
        "run": custom_move,
    }

    result = agent.execute_tool("custom_move", {"source": "existing.txt", "destination": "moved.txt"})

    assert result.metadata["tool_status"] == "ok"
    affected = set(result.metadata["affected_paths"])
    assert "existing.txt" in affected
    assert "moved.txt" in affected


def test_generic_path_arg_registry_covers_list_arg(tmp_path):
    agent = build_agent(tmp_path)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")

    def custom_delete(args):
        for path in args["paths"]:
            (tmp_path / path).unlink()
        return "deleted"

    agent.tools["custom_delete"] = {
        "schema": {"paths": "list[str]"},
        "risky": True,
        "description": "Delete files.",
        "run": custom_delete,
    }

    result = agent.execute_tool("custom_delete", {"paths": ["a.txt", "b.txt"]})

    assert result.metadata["tool_status"] == "ok"
    affected = set(result.metadata["affected_paths"])
    assert affected == {"a.txt", "b.txt"}


def test_delegate_is_read_only_and_does_not_create_a_tool_change(tmp_path):
    agent = build_agent(tmp_path)
    agent.tools["delegate"]["run"] = Mock(return_value="delegated")

    result = agent.execute_tool("delegate", {"task": "inspect README", "max_steps": 1})

    assert result.metadata["effect_class"] == "read_only"
    assert result.metadata["read_only"] is True
    assert "tool_change_id" not in result.metadata
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_write_file_entry_records_complete_exists_hash_mode_and_source(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    agent = build_agent(tmp_path)

    result = agent.execute_tool(
        "write_file", {"path": "note.txt", "content": "after"}
    )
    entry = result.metadata["file_entries"][0]

    assert entry["before_exists"] is True
    assert entry["after_exists"] is True
    assert entry["before_blob_ref"] == entry["before_hash"]
    assert entry["after_blob_ref"] == entry["after_hash"]
    assert entry["expected_current_hash"] == entry["after_hash"]
    assert entry["before_mode"] == 0o640
    assert entry["after_mode"] == 0o640
    assert entry["source_tool_change_ids"] == [result.metadata["tool_change_id"]]


def test_sensitive_after_bytes_never_reach_blob_store(tmp_path, monkeypatch):
    sentinel = "sk-sensitive-recovery-value"
    monkeypatch.setenv("PICO_OPENAI_API_KEY", sentinel)
    target = tmp_path / "safe.py"
    target.write_text("before", encoding="utf-8")
    agent = build_agent(tmp_path)

    result = agent.execute_tool(
        "write_file", {"path": "safe.py", "content": sentinel}
    )

    assert result.metadata["tool_status"] == "rejected"
    assert all(
        sentinel.encode() not in path.read_bytes()
        for path in agent.checkpoint_store.blobs_dir.rglob("*")
        if path.is_file()
    )


def test_existing_sensitive_before_file_is_modified_not_created(tmp_path, monkeypatch):
    sentinel = "sk-sensitive-existing-before"
    monkeypatch.setenv("PICO_OPENAI_API_KEY", sentinel)
    target = tmp_path / "safe.py"
    target.write_text(sentinel, encoding="utf-8")
    target.chmod(0o640)
    agent = build_agent(tmp_path)

    result = agent.execute_tool(
        "write_file", {"path": "safe.py", "content": "safe-after"}
    )
    entry = result.metadata["file_entries"][0]

    assert entry["change_kind"] == "modified"
    assert entry["before_exists"] is True
    assert entry["before_mode"] == 0o640
    assert entry["before_blob_ref"] == ""
    assert entry["snapshot_eligible"] is False
    assert entry["ineligible_reason"] == "before_blob_unavailable"


def test_existing_oversized_before_file_keeps_presence_without_blob(tmp_path):
    target = tmp_path / "large.txt"
    target.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    target.chmod(0o600)
    agent = build_agent(tmp_path)

    result = agent.execute_tool(
        "write_file", {"path": "large.txt", "content": "safe-after"}
    )
    entry = result.metadata["file_entries"][0]

    assert entry["change_kind"] == "modified"
    assert entry["before_exists"] is True
    assert entry["before_mode"] == 0o600
    assert entry["before_blob_ref"] == ""
    assert entry["ineligible_reason"] == "before_blob_unavailable"
