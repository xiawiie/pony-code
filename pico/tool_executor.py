"""Structured, audited tool execution for the agent runtime."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import subprocess
import textwrap

from . import security as securitylib
from .recovery_checkpoint_writer import current_recovery_checkpoint_id
from .recovery_paths import (
    hash_bytes,
    normalize_workspace_relative_path,
    resolve_workspace_relative_path,
)
from .recovery_policy import (
    DEFAULT_MAX_BLOB_SIZE,
    assess_command,
    snapshot_bytes_eligibility,
)
from .safe_subprocess import (
    _validate_hardened_git_args,
    _validate_hardened_git_repository,
    run_hardened_git,
)
from .tools import (
    DEFAULT_RUN_SHELL_TIMEOUT,
    _ApprovedShellExecution,
    SensitiveToolError,
)
from .verification import verification_evidence_for_execution
from .workspace import clip


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


_EFFECT_CLASS_BY_TOOL = {
    "read_file": "read_only",
    "list_files": "read_only",
    "search": "read_only",
    "run_shell": "workspace_write",
    "write_file": "workspace_write",
    "patch_file": "workspace_write",
    "delegate": "read_only",
    "memory_list": "read_only",
    "memory_read": "read_only",
    "memory_search": "read_only",
    "memory_save": "memory_write",
    "repo_lookup": "read_only",
}


# 已知“直接改文件”的工具可以显式声明路径参数；未知 workspace_write 工具
# 会退回到一组常见路径参数名，避免未来新工具静默丢失 recovery 记录。
_PATH_ARG_NAMES_BY_TOOL = {
    "write_file": ("path",),
    "patch_file": ("path",),
    # 未来的 move_file 之类工具可以在这里加 ("source", "destination")
}
_GENERIC_PATH_ARG_NAMES = ("path", "paths", "target", "targets", "source", "sources", "destination", "destinations")


def _effect_class(name, risky):
    return _EFFECT_CLASS_BY_TOOL.get(name, "workspace_write" if risky else "read_only")


def _metadata(
    tool_status,
    *,
    effect_class,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "effect_class": effect_class,
        "read_only": effect_class == "read_only",
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


def _summarize_input(args):
    summary = {}
    if not isinstance(args, dict):
        return summary
    for key in ("path", "command", "pattern", "task", "timeout", "start", "end"):
        if key in args:
            value = args[key]
            if isinstance(value, str) and len(value) > 240:
                clipped = value[:240]
                marker = "<redacted>"
                marker_start = value.rfind(marker, 0, 240 + len(marker))
                if 0 <= marker_start < 240 and marker not in clipped:
                    clipped = value[:marker_start] + marker
                summary[key] = clipped + "..."
            else:
                summary[key] = value
    return summary


def _validation_rejection(agent, tool, name, args, effect_class):
    try:
        agent.validate_tool(name, args)
    except SensitiveToolError as exc:
        return ToolExecutionResult(
            content=f"error: {exc.code}",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code=exc.code,
                security_event_type="sensitive_access_block",
                risk_level="high",
            ),
        )
    except Exception as exc:
        example = agent.tool_example(name)
        message = f"error: invalid arguments for {name}: {exc}"
        if example:
            message += f"\nexample: {example}"
        security_event_type = (
            "path_escape"
            if "path escapes workspace" in str(exc)
            else ""
        )
        return ToolExecutionResult(
            content=message,
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="invalid_arguments",
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
            ),
        )
    return None


def _command_approval_metadata(
    assessment,
    mode,
    outcome,
    *,
    runner_executed=False,
    exit_code=None,
):
    result = {
        "decision": assessment["decision"],
        "reason": assessment["reason"],
        "mode": mode,
        "outcome": outcome,
        "runner_executed": bool(runner_executed),
        "execution_mode": assessment["execution_mode"],
    }
    if exit_code is not None:
        result["exit_code"] = exit_code
    return result


def _shell_rejection(
    *,
    assessment,
    mode,
    outcome,
    effect_class,
    tool_error_code,
    content,
    security_event_type="",
):
    approval = _command_approval_metadata(assessment, mode, outcome)
    return ToolExecutionResult(
        content=content,
        metadata=_add_command_policy(
            _metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code=tool_error_code,
                security_event_type=security_event_type,
                risk_level="high",
            ),
            assessment["risk_class"],
            approval,
        ),
    )


def _add_initial_shell_policy(result, assessment, mode, outcome="blocked"):
    _add_command_policy(
        result.metadata,
        assessment["risk_class"],
        _command_approval_metadata(assessment, mode, outcome),
    )
    return result


def _structured_shell_result(agent, value):
    if not isinstance(value, Mapping):
        raise ValueError("shell runner returned invalid result")
    if not {"stdout", "stderr", "exit_code"} <= set(value):
        raise ValueError("shell runner returned invalid result")
    exit_code = value["exit_code"]
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ValueError("shell runner returned invalid exit code")
    if not isinstance(value["stdout"], str) or not isinstance(
        value["stderr"],
        str,
    ):
        raise ValueError("shell runner returned invalid output")
    return {
        "stdout": agent.redact_text(value["stdout"]),
        "stderr": agent.redact_text(value["stderr"]),
        "exit_code": exit_code,
    }


def _format_shell_result(result):
    return textwrap.dedent(
        f"""\
        exit_code: {result['exit_code']}
        stdout:
        {result['stdout'].strip() or '(empty)'}
        stderr:
        {result['stderr'].strip() or '(empty)'}
        """
    ).strip()


def _prepare_shell_execution(agent, tool, args, effect_class, assessment):
    mode = agent.approval_policy
    if agent.read_only:
        return None, None, _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="blocked",
            effect_class=effect_class,
            tool_error_code="read_only_block",
            content="error: read-only mode blocks run_shell",
            security_event_type="read_only_block",
        )

    rejection = _validation_rejection(
        agent,
        tool,
        "run_shell",
        args,
        effect_class,
    )
    if rejection is not None:
        return None, None, _add_initial_shell_policy(
            rejection,
            assessment,
            mode,
        )

    if agent.repeated_tool_call("run_shell", args):
        return None, None, _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="blocked",
            effect_class=effect_class,
            tool_error_code="repeated_identical_call",
            content=(
                "error: repeated identical tool call for run_shell; "
                "choose a different tool or return a final answer"
            ),
        )

    original_args = deepcopy(args)
    original_assessment = deepcopy(assessment)
    if mode == "never":
        return None, None, _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="denied",
            effect_class=effect_class,
            tool_error_code="approval_denied",
            content="error: approval denied for run_shell",
            security_event_type="approval_denied",
        )
    if mode == "auto" and assessment["decision"] != "allow":
        return None, None, _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="blocked",
            effect_class=effect_class,
            tool_error_code="command_approval_required",
            content="error: command approval required for run_shell",
            security_event_type="command_approval_required",
        )
    if mode == "ask":
        approval_payload = agent.redact_artifact(deepcopy(original_args))
        approval_payload_snapshot = deepcopy(approval_payload)
        if not agent.approve("run_shell", approval_payload):
            return None, None, _shell_rejection(
                assessment=assessment,
                mode=mode,
                outcome="denied",
                effect_class=effect_class,
                tool_error_code="approval_denied",
                content="error: approval denied for run_shell",
                security_event_type="approval_denied",
            )
        rejection = _validation_rejection(
            agent,
            tool,
            "run_shell",
            args,
            effect_class,
        )
        if rejection is not None:
            return None, None, _add_initial_shell_policy(
                rejection,
                assessment,
                mode,
            )
        reassessment = assess_command(str(args.get("command", "")), agent.root)
        if (
            args != original_args
            or approval_payload != approval_payload_snapshot
            or reassessment != original_assessment
        ):
            return None, None, _shell_rejection(
                assessment=assessment,
                mode=mode,
                outcome="blocked",
                effect_class=effect_class,
                tool_error_code="approval_arguments_changed",
                content="error: approved arguments changed for run_shell",
                security_event_type="approval_arguments_changed",
            )
        outcome = "approved"
    elif mode == "auto":
        outcome = "allowed"
    else:
        return None, None, _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="denied",
            effect_class=effect_class,
            tool_error_code="approval_denied",
            content="error: approval denied for run_shell",
            security_event_type="approval_denied",
        )

    if assessment["execution_mode"] == "argv":
        argv = tuple(assessment["argv"])
        executable_name = argv[0]
    else:
        argv = ()
        executable_name = "sh"
    executable = (
        agent.trusted_executables.get(executable_name)
        if Path(executable_name).name == executable_name
        else None
    )
    if not executable:
        return None, None, _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="blocked",
            effect_class=effect_class,
            tool_error_code="trusted_executable_missing",
            content="error: trusted executable missing for run_shell",
            security_event_type="trusted_executable_missing",
        )
    if executable_name == "git":
        try:
            _validate_hardened_git_args(argv[1:])
        except ValueError:
            return None, None, _shell_rejection(
                assessment=assessment,
                mode=mode,
                outcome="blocked",
                effect_class=effect_class,
                tool_error_code="unsafe_git_arguments",
                content="error: unsafe git arguments",
                security_event_type="unsafe_git_arguments",
            )
        try:
            _validate_hardened_git_repository(
                executable,
                cwd=agent.root,
                args=argv[1:],
                timeout=int(
                    original_args.get("timeout", DEFAULT_RUN_SHELL_TIMEOUT)
                ),
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None, None, _shell_rejection(
                assessment=assessment,
                mode=mode,
                outcome="blocked",
                effect_class=effect_class,
                tool_error_code="unsafe_git_config",
                content="error: unsafe git repository config",
                security_event_type="unsafe_git_config",
            )
    execution = _ApprovedShellExecution(
        command=str(original_args.get("command", "")),
        argv=argv,
        execution_mode=assessment["execution_mode"],
        executable=executable,
        timeout=int(original_args.get("timeout", DEFAULT_RUN_SHELL_TIMEOUT)),
    )
    return (
        execution,
        _command_approval_metadata(assessment, mode, outcome),
        None,
    )


def _assess_shell_request(agent, name, args, effect_class):
    if name != "run_shell":
        return None, None
    assessment_args = args if isinstance(args, Mapping) else {}
    assessment = assess_command(
        str(assessment_args.get("command", "")),
        agent.root,
    )
    if assessment["decision"] != "reject":
        return assessment, None
    sensitive = assessment["reason"] == "sensitive_path"
    return assessment, _shell_rejection(
        assessment=assessment,
        mode=agent.approval_policy,
        outcome="blocked",
        effect_class=effect_class,
        tool_error_code=(
            "sensitive_path_block" if sensitive else "command_rejected"
        ),
        content=(
            "error: sensitive_path_block"
            if sensitive
            else "error: command policy rejected run_shell"
        ),
        security_event_type=(
            "sensitive_access_block" if sensitive else "command_rejected"
        ),
    )


def _availability_rejection(
    agent,
    name,
    tool,
    effect_class,
    command_assessment,
):
    if agent.allowed_tools is not None and name not in agent.allowed_tools:
        rejection = ToolExecutionResult(
            content=f"error: tool '{name}' is not allowed in this run",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="tool_not_allowed",
                risk_level="high",
            ),
        )
    elif tool is None:
        rejection = ToolExecutionResult(
            content=f"error: unknown tool '{name}'",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="unknown_tool",
                risk_level="high",
            ),
        )
    else:
        return None
    if command_assessment is None:
        return rejection
    return _add_initial_shell_policy(rejection, command_assessment, agent.approval_policy)


def _prepare_non_shell_tool(agent, tool, name, args, effect_class):
    if agent.read_only and effect_class != "read_only":
        return ToolExecutionResult(
            content=f"error: read-only mode blocks {name}",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="read_only_block",
                security_event_type="read_only_block",
                risk_level="high",
            ),
        )
    rejection = _validation_rejection(agent, tool, name, args, effect_class)
    if rejection is not None:
        return rejection
    if agent.repeated_tool_call(name, args):
        return ToolExecutionResult(
            content=(
                f"error: repeated identical tool call for {name}; "
                "choose a different tool or return a final answer"
            ),
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="repeated_identical_call",
                risk_level="high" if tool["risky"] else "low",
            ),
        )
    if not tool["risky"]:
        return None
    original_args = deepcopy(args)
    approval_args = deepcopy(original_args)
    if not agent.approve(name, approval_args):
        return ToolExecutionResult(
            content=f"error: approval denied for {name}",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="approval_denied",
                security_event_type=(
                    "read_only_block" if agent.read_only else "approval_denied"
                ),
                risk_level="high",
            ),
        )
    rejection = _validation_rejection(agent, tool, name, args, effect_class)
    if rejection is not None:
        return rejection
    if args == original_args and approval_args == original_args:
        return None
    return ToolExecutionResult(
        content=f"error: approved arguments changed for {name}",
        metadata=_metadata(
            "rejected",
            effect_class=effect_class,
            tool_error_code="approval_arguments_changed",
            security_event_type="approval_arguments_changed",
            risk_level="high",
        ),
    )


def _prepare_tool_request(agent, name, args):
    tool = agent.tools.get(name)
    effect_class = (
        "workspace_write"
        if tool is None and name not in _EFFECT_CLASS_BY_TOOL
        else _effect_class(name, bool(tool and tool["risky"]))
    )
    command_assessment, rejection = _assess_shell_request(
        agent,
        name,
        args,
        effect_class,
    )
    if rejection is not None:
        return None, rejection
    rejection = _availability_rejection(
        agent,
        name,
        tool,
        effect_class,
        command_assessment,
    )
    if rejection is not None:
        return None, rejection

    shell_execution = None
    command_risk = ""
    command_approval = {}
    if command_assessment is not None:
        command_risk = command_assessment["risk_class"]
        shell_execution, command_approval, rejection = _prepare_shell_execution(
            agent,
            tool,
            args,
            effect_class,
            command_assessment,
        )
    else:
        rejection = _prepare_non_shell_tool(
            agent,
            tool,
            name,
            args,
            effect_class,
        )
    if rejection is not None:
        return None, rejection

    records_tool_change = effect_class in {"memory_write", "workspace_write"}
    input_summary = None
    if records_tool_change:
        input_summary = _summarize_input(
            agent.redact_artifact(deepcopy(args))
        )
        if command_assessment is not None:
            input_summary["assessment"] = agent.redact_artifact(
                command_assessment
            )
    task_state = getattr(agent, "current_task_state", None)
    return {
        "agent": agent,
        "name": name,
        "args": args,
        "tool": tool,
        "effect_class": effect_class,
        "shell_execution": shell_execution,
        "command_risk": command_risk,
        "command_approval": command_approval,
        "records_tool_change": records_tool_change,
        "records_recovery": effect_class == "workspace_write",
        "input_summary": input_summary,
        "turn_id": getattr(task_state, "task_id", "") or "",
        "parent_checkpoint": current_recovery_checkpoint_id(agent.session),
    }, None


def _begin_tool_change(prepared, lifecycle):
    if not prepared["records_tool_change"]:
        return None
    agent = prepared["agent"]
    try:
        tool_reviews = agent.tool_change_recorder.pending_recovery_reviews()
        restore_reviews = agent.recovery_manager.pending_restore_reviews()
    except (OSError, ValueError):
        tool_reviews = ["invalid"]
        restore_reviews = []
    if tool_reviews or restore_reviews:
        return ToolExecutionResult(
            content="error: recovery review required",
            metadata=_metadata(
                "rejected",
                effect_class=prepared["effect_class"],
                tool_error_code="recovery_review_required",
                security_event_type="recovery_review_required",
                risk_level="high",
            ),
        )

    prepared_entries = []
    recovery_context = {}
    if prepared["records_recovery"]:
        name = prepared["name"]
        lifecycle["before_paths"] = _direct_tool_candidate_paths(
            name,
            prepared["args"],
        )
        if name == "run_shell":
            observer = agent.workspace_observer.capture()
            lifecycle["observer_before"] = observer
            recovery_context = {
                "observer_mode": str(observer.get("mode", "")),
                "git_head": str(observer.get("head", "")),
            }
        else:
            states = _capture_before_file_states_for_paths(
                agent,
                lifecycle["before_paths"],
            )
            lifecycle["before_file_states"] = states
            lifecycle["before_snapshot"] = {
                path: state["before_hash"]
                for path, state in states.items()
                if state.get("before_hash")
            }
            lifecycle["before_existed"] = set(states)
            for raw_path in lifecycle["before_paths"]:
                try:
                    path = normalize_workspace_relative_path(raw_path)
                except ValueError:
                    continue
                state = states.get(path, {})
                prepared_entries.append(
                    {
                        "path": path,
                        "before_exists": path in lifecycle["before_existed"],
                        "before_blob_ref": state.get("before_blob_ref", ""),
                        "before_hash": state.get("before_hash", ""),
                        "before_mode": state.get("before_mode"),
                    }
                )
    lifecycle["pending_record"] = agent.tool_change_recorder.start(
        checkpoint_id=prepared["parent_checkpoint"],
        turn_id=prepared["turn_id"],
        tool_name=prepared["name"],
        effect_class=prepared["effect_class"],
        input_summary=prepared["input_summary"],
        prepared_file_entries=prepared_entries,
        recovery_context=recovery_context,
    )
    return None


def _invoke_prepared_tool(prepared, execution):
    agent = prepared["agent"]
    if prepared["name"] != "run_shell":
        raw_content = prepared["tool"]["run"](prepared["args"])
        execution["runner_completed"] = True
        execution["content"] = clip(agent.redact_text(raw_content))
        return

    shell_execution = prepared["shell_execution"]
    approval = prepared["command_approval"]
    approval["runner_executed"] = True
    if (
        shell_execution.execution_mode == "argv"
        and shell_execution.argv[0] == "git"
    ):
        completed = run_hardened_git(
            shell_execution.executable,
            shell_execution.argv[1:],
            cwd=agent.root,
            timeout=shell_execution.timeout,
            text=True,
        )
        raw_result = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }
    else:
        raw_result = prepared["tool"]["run"](shell_execution)
    shell_result = _structured_shell_result(agent, raw_result)
    execution["shell_result"] = shell_result
    execution["runner_completed"] = True
    approval["exit_code"] = shell_result["exit_code"]
    execution["verification_evidence"] = verification_evidence_for_execution(
        argv=shell_execution.argv,
        risk_class=prepared["command_risk"],
        runner_executed=approval["runner_executed"],
        execution_mode=shell_execution.execution_mode,
        exit_code=shell_result["exit_code"],
        stdout=shell_result["stdout"],
        stderr=shell_result["stderr"],
        redact_text=agent.redact_text,
    )
    execution["content"] = clip(_format_shell_result(shell_result))


def _observe_tool_effects(prepared, lifecycle):
    agent = prepared["agent"]
    if (
        prepared["records_recovery"]
        and prepared["name"] == "run_shell"
        and lifecycle["observer_before"] is not None
    ):
        observer_before = lifecycle["observer_before"]
        observer_after = agent.workspace_observer.capture()
        delta = agent.workspace_observer.diff(
            observer_before,
            observer_after,
        )
        paths = list(delta.get("changed_paths", []))
        before_existed = _paths_present_in_observer(
            observer_before,
            paths,
        )
        head_paths = [path for path in paths if path not in before_existed]
        states = _fill_git_head_before_file_states(
            agent,
            head_paths,
            lifecycle["before_file_states"],
        )
        before_existed.update(states)
        lifecycle["before_file_states"] = states
        lifecycle["before_existed"] = before_existed
        lifecycle["before_snapshot"] = _merge_before_snapshot({}, states)
        summaries = _summaries_for_paths(agent, paths, before_existed)
        return {
            "affected_paths": paths,
            "diff_summary": summaries,
            "workspace_changed": bool(paths),
            "shell_side_effects": summaries,
        }
    if prepared["records_recovery"]:
        after = _capture_path_snapshot(agent, lifecycle["before_paths"])
        paths, summaries = agent.diff_workspace_snapshots(
            lifecycle["before_snapshot"],
            after,
        )
        return {
            "affected_paths": paths,
            "diff_summary": summaries,
            "workspace_changed": bool(paths),
            "shell_side_effects": [],
        }
    return {
        "affected_paths": [],
        "diff_summary": [],
        "workspace_changed": False,
        "shell_side_effects": [],
    }


def _finish_tool_success(prepared, lifecycle, execution, effects):
    agent = prepared["agent"]
    agent.update_memory_after_tool(
        prepared["name"], prepared["args"], execution["content"]
    )
    tool_status = "ok"
    tool_error_code = ""
    if prepared["name"] == "run_shell":
        exit_code = execution["shell_result"]["exit_code"]
        if exit_code != 0 and effects["workspace_changed"]:
            tool_status = "partial_success"
            tool_error_code = "tool_partial_success"
        elif exit_code != 0:
            tool_status = "error"
            tool_error_code = "tool_failed"
    metadata = _finalize_tool_side_effects(
        agent=agent,
        name=prepared["name"],
        args=prepared["args"],
        pending_record=lifecycle["pending_record"],
        affected_paths=effects["affected_paths"],
        before_snapshot=lifecycle["before_snapshot"],
        before_file_states=lifecycle["before_file_states"],
        before_existed=lifecycle["before_existed"],
        tool_status=tool_status,
        tool_error_code=tool_error_code,
        security_event_type="",
        risk_level="high" if prepared["tool"]["risky"] else "low",
        effect_class=prepared["effect_class"],
        workspace_changed=effects["workspace_changed"],
        diff_summary=effects["diff_summary"],
        shell_side_effects=effects["shell_side_effects"],
        command_risk=prepared["command_risk"],
        command_approval=prepared["command_approval"],
        content=execution["content"],
    )
    evidence = execution["verification_evidence"]
    if evidence is not None:
        metadata["verification_evidence"] = evidence
    return ToolExecutionResult(content=execution["content"], metadata=metadata)


def _finish_tool_failure(prepared, lifecycle, execution, effects, exc):
    agent = prepared["agent"]
    safe_error = agent.redact_text(str(exc))
    security_event = (
        "path_escape" if "path escapes workspace" in str(exc) else ""
    )
    pending = lifecycle["pending_record"]
    if execution["runner_completed"] and pending is not None:
        metadata = _metadata(
            "error",
            tool_error_code="tool_finalize_failed",
            security_event_type=security_event,
            risk_level="high",
            effect_class=prepared["effect_class"],
            affected_paths=effects["affected_paths"],
            workspace_changed=effects["workspace_changed"],
            diff_summary=effects["diff_summary"],
        )
        metadata["tool_change_id"] = pending["tool_change_id"]
        return ToolExecutionResult(
            content=(
                f"error: tool {prepared['name']} failed after execution: "
                f"{safe_error}"
            ),
            metadata=metadata,
        )

    changed = effects["workspace_changed"]
    status = "partial_success" if changed else "error"
    error_code = "tool_partial_success" if changed else "tool_failed"
    metadata = _finalize_tool_side_effects(
        agent=agent,
        name=prepared["name"],
        args=prepared["args"],
        pending_record=pending,
        affected_paths=effects["affected_paths"],
        before_snapshot=lifecycle["before_snapshot"],
        before_file_states=lifecycle["before_file_states"],
        before_existed=lifecycle["before_existed"],
        tool_status=status,
        tool_error_code=error_code,
        security_event_type=security_event,
        risk_level="high" if prepared["tool"]["risky"] else "low",
        effect_class=prepared["effect_class"],
        workspace_changed=changed,
        diff_summary=effects["diff_summary"],
        shell_side_effects=effects["shell_side_effects"],
        command_risk=prepared["command_risk"],
        command_approval=prepared["command_approval"],
        content="",
        error_message=safe_error,
    )
    evidence = execution["verification_evidence"]
    if evidence is not None:
        metadata["verification_evidence"] = evidence
    return ToolExecutionResult(
        content=f"error: tool {prepared['name']} failed: {safe_error}", metadata=metadata
    )


def _run_tool_lifecycle(prepared):
    lifecycle = {
        "pending_record": None,
        "before_paths": [],
        "before_snapshot": {},
        "before_file_states": {},
        "before_existed": set(),
        "observer_before": None,
    }
    execution = {
        "runner_completed": False,
        "shell_result": None,
        "verification_evidence": None,
        "content": "",
    }
    empty_effects = {
        "affected_paths": [],
        "diff_summary": [],
        "workspace_changed": False,
        "shell_side_effects": [],
    }
    try:
        try:
            rejection = _begin_tool_change(prepared, lifecycle)
        except Exception as exc:
            safe_error = prepared["agent"].redact_text(str(exc))
            metadata = _metadata(
                "error",
                effect_class=prepared["effect_class"],
                tool_error_code="tool_failed",
                risk_level=(
                    "high" if prepared["tool"]["risky"] else "low"
                ),
            )
            _add_command_policy(
                metadata,
                prepared["command_risk"],
                prepared["command_approval"],
            )
            return ToolExecutionResult(
                content=(
                    f"error: tool {prepared['name']} failed: "
                    f"{safe_error}"
                ),
                metadata=metadata,
            )
        if rejection is not None:
            return rejection

        effects = None
        observation_attempted = False
        try:
            _invoke_prepared_tool(prepared, execution)
            observation_attempted = True
            effects = _observe_tool_effects(prepared, lifecycle)
            return _finish_tool_success(
                prepared,
                lifecycle,
                execution,
                effects,
            )
        except Exception as exc:
            if not observation_attempted:
                observation_attempted = True
                try:
                    effects = _observe_tool_effects(prepared, lifecycle)
                except Exception:
                    effects = None
            return _finish_tool_failure(
                prepared,
                lifecycle,
                execution,
                effects or empty_effects,
                exc,
            )
    except BaseException as primary:
        if not isinstance(primary, Exception):
            _finalize_interrupted_pending(
                prepared["agent"],
                lifecycle["pending_record"],
                (
                    prepared["command_approval"]
                    if prepared["name"] == "run_shell"
                    else None
                ),
            )
        raise


def _exit_mutation_context(mutation_context, primary):
    if primary is None:
        mutation_context.__exit__(None, None, None)
        return
    try:
        mutation_context.__exit__(
            type(primary),
            primary,
            primary.__traceback__,
        )
    except BaseException:
        pass


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, name, args):
        prepared, rejection = _prepare_tool_request(self.agent, name, args)
        if rejection is not None:
            return rejection
        if not prepared["records_tool_change"]:
            return _run_tool_lifecycle(prepared)

        mutation_context = self.agent.checkpoint_store.mutation_lock()
        mutation_context.__enter__()
        primary = None
        try:
            return _run_tool_lifecycle(prepared)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _exit_mutation_context(mutation_context, primary)


def _add_command_policy(metadata, command_risk, command_approval):
    if command_risk:
        metadata["command_risk_class"] = command_risk
    if command_approval:
        metadata["command_approval"] = dict(command_approval)
    return metadata


def _finalize_interrupted_pending(agent, pending_record, command_approval=None):
    if pending_record is None:
        return
    try:
        current = agent.checkpoint_store.load_tool_change_record(pending_record["tool_change_id"])
        if current.get("status") == "pending":
            agent.tool_change_recorder.finalize(
                pending_record["tool_change_id"],
                status="interrupted",
                approval=command_approval or None,
            )
    except BaseException:
        pass


def _finalize_tool_side_effects(
    *,
    agent,
    name,
    args,
    pending_record,
    affected_paths,
    before_snapshot,
    before_file_states,
    before_existed,
    tool_status,
    tool_error_code,
    security_event_type,
    risk_level,
    effect_class,
    workspace_changed,
    diff_summary,
    shell_side_effects,
    command_risk,
    command_approval,
    content,
    error_message="",
):
    metadata = _metadata(
        tool_status,
        tool_error_code=tool_error_code,
        security_event_type=security_event_type,
        risk_level=risk_level,
        effect_class=effect_class,
        affected_paths=affected_paths,
        workspace_changed=workspace_changed,
        workspace_fingerprint=agent.workspace.fingerprint() if effect_class == "workspace_write" else "",
        diff_summary=diff_summary,
    )
    metadata = _add_command_policy(metadata, command_risk, command_approval)

    file_entries = []
    if effect_class == "workspace_write":
        file_entries = _build_file_entries(
            agent,
            name,
            args,
            affected_paths,
            before_snapshot,
            before_file_states,
            before_existed,
            pending_record["tool_change_id"] if pending_record else "",
        )
    if pending_record is not None:
        terminal_status = _tool_change_terminal_status(tool_status)
        error_payload = _tool_change_error_payload(
            terminal_status=terminal_status,
            tool_error_code=tool_error_code,
            content=content,
            error_message=error_message,
        )
        finalized = agent.tool_change_recorder.finalize(
            pending_record["tool_change_id"],
            status=terminal_status,
            affected_paths=affected_paths,
            file_entries=file_entries,
            error=error_payload,
            shell_side_effects=shell_side_effects if name == "run_shell" else None,
            approval=command_approval if command_approval else None,
        )
        metadata["tool_change_id"] = finalized["tool_change_id"]
        metadata["file_entries"] = list(file_entries)
        if name == "run_shell":
            metadata["shell_side_effects"] = list(shell_side_effects)
    return metadata


def _tool_change_terminal_status(tool_status):
    if tool_status in {"partial_success", "error"}:
        return tool_status
    return "finalized"


def _tool_change_error_payload(terminal_status, tool_error_code, content, error_message=""):
    if error_message and terminal_status in {"partial_success", "error"}:
        return {"code": tool_error_code or "tool_failed", "message": str(error_message)[:400]}
    if terminal_status == "error":
        return {"code": tool_error_code or "tool_failed", "message": str(content or "")[:400]}
    return None


def _direct_tool_candidate_paths(name, args):
    if not isinstance(args, dict):
        return []
    arg_names = _PATH_ARG_NAMES_BY_TOOL.get(name, _GENERIC_PATH_ARG_NAMES)
    paths = []
    for key in arg_names:
        value = args.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
        elif isinstance(value, (list, tuple)):
            paths.extend(item for item in value if isinstance(item, str) and item)
    return paths


def _capture_path_snapshot(agent, raw_paths):
    snapshot = {}
    workspace_root = agent.root
    for raw_path in raw_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        limit = getattr(
            agent, "project_max_blob_size", DEFAULT_MAX_BLOB_SIZE
        )
        try:
            state = securitylib.read_regular_bytes_anchored(
                workspace_root, normalized, max_bytes=limit
            )
        except (OSError, ValueError):
            continue
        if not state["exists"]:
            continue
        eligibility = snapshot_bytes_eligibility(
            workspace_root,
            normalized,
            state["data"],
            max_blob_size=limit,
            env=getattr(agent, "redaction_env", None),
            secret_env_names=getattr(agent, "secret_env_names", ()),
        )
        if not eligibility.get("snapshot_eligible", False):
            continue
        snapshot[normalized] = hash_bytes(state["data"])["content_hash"]
    return snapshot


def _paths_present_in_observer(observer_capture, affected_paths):
    """哪些 affected_paths 在 observer 的 before capture 里已经存在？

    只返回“存在性”这一个事实（set of normalized paths），不返回 git status 标记。
    之前把 'MM'、'??' 这些标记塞到 before_snapshot 里当 hash 用，既污染了 before_hash，
    又让 git-HEAD fallback 认为“已经有 before 记录”而跳过。分离这两个概念可以修好这两个问题。
    """
    if not observer_capture:
        return set()
    paths = observer_capture.get("paths") or {}
    present = set()
    for raw_path in affected_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        if normalized in paths:
            present.add(normalized)
    return present


def _summaries_for_paths(agent, affected_paths, before_existed):
    summaries = []
    before = set(before_existed or set())
    for raw_path in affected_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
            resolved = resolve_workspace_relative_path(agent.root, normalized)
        except ValueError:
            continue
        existed_before = normalized in before
        exists_after = resolved.exists()
        if not existed_before and exists_after:
            summaries.append(f"created:{normalized}")
        elif existed_before and not exists_after:
            summaries.append(f"deleted:{normalized}")
        else:
            summaries.append(f"modified:{normalized}")
    return summaries


def _capture_before_file_states_for_paths(agent, raw_paths):
    """Capture restorable pre-tool bytes for a bounded set of paths."""
    store = agent.checkpoint_store
    workspace_root = agent.root
    states = {}
    for raw_path in raw_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        limit = agent.project_max_blob_size
        try:
            state = securitylib.read_regular_bytes_anchored(
                workspace_root, normalized, max_bytes=limit
            )
        except (OSError, ValueError):
            continue
        if not state["exists"]:
            continue
        data = state["data"]
        states[normalized] = {"before_mode": state["mode"]}
        eligibility = snapshot_bytes_eligibility(
            workspace_root,
            normalized,
            data,
            max_blob_size=limit,
            env=getattr(agent, "redaction_env", None),
            secret_env_names=getattr(agent, "secret_env_names", ()),
        )
        if not eligibility.get("snapshot_eligible", False):
            continue
        info = store.write_blob(data, "text")
        states[normalized].update({
            "before_blob_ref": info["blob_ref"],
            "before_hash": info["content_hash"],
        })
    return states


def _merge_before_snapshot(before_snapshot, before_file_states):
    merged = dict(before_snapshot or {})
    for path, state in (before_file_states or {}).items():
        before_hash = state.get("before_hash")
        if before_hash:
            merged[path] = before_hash
    return merged


def _fill_git_head_before_file_states(agent, raw_paths, before_file_states):
    """对 delta 里每个还没有 before_blob 的路径，尝试从 git HEAD 抓一份原始字节。

    这里不再看 observer 是否见过 —— observer 状态标记（'MM'、'??'）不是可用的
    before_hash，只能表达“存在”。是否有可用的 before-blob 完全取决于我们能否
    真的从 HEAD 取出对应字节。取不到就让 _build_file_entries 把 entry 标记
    ineligible_reason='before_blob_unavailable'。
    """
    states = dict(before_file_states or {})
    missing_paths = []
    for raw_path in raw_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        if securitylib.is_sensitive_path(normalized):
            continue
        if normalized in states:
            continue
        missing_paths.append(normalized)
    if not missing_paths:
        return states

    git_executable = getattr(agent, "trusted_executables", {}).get("git")
    if not git_executable:
        return states

    for path in missing_paths:
        limit = getattr(
            agent, "project_max_blob_size", DEFAULT_MAX_BLOB_SIZE
        )
        try:
            tree = run_hardened_git(
                git_executable,
                ["ls-tree", "-l", "-z", "HEAD", "--", path],
                cwd=agent.root,
                text=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        tree_stdout = (
            tree.stdout.encode("utf-8")
            if isinstance(tree.stdout, str)
            else bytes(tree.stdout)
        )
        records = tree_stdout.split(b"\0")
        if (
            tree.returncode != 0
            or len(records) != 2
            or records[-1] != b""
            or b"\t" not in records[0]
        ):
            continue
        metadata, tree_path = records[0].split(b"\t", 1)
        fields = metadata.split()
        if (
            tree_path != path.encode("utf-8")
            or len(fields) != 4
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
            or len(fields[2]) not in {40, 64}
            or any(char not in b"0123456789abcdef" for char in fields[2])
        ):
            continue
        try:
            blob_size = int(fields[3])
        except ValueError:
            continue
        if blob_size < 0 or blob_size > limit:
            continue
        try:
            proc = run_hardened_git(
                git_executable,
                ["show", fields[2].decode("ascii")],
                cwd=agent.root,
                text=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if proc.returncode != 0:
            continue
        raw_stdout = (
            proc.stdout.encode("utf-8")
            if isinstance(proc.stdout, str)
            else bytes(proc.stdout)
        )
        eligibility = snapshot_bytes_eligibility(
            agent.root,
            path,
            raw_stdout,
            max_blob_size=limit,
            env=getattr(agent, "redaction_env", None),
            secret_env_names=getattr(agent, "secret_env_names", ()),
        )
        if not eligibility.get("snapshot_eligible", False):
            continue
        if len(raw_stdout) != blob_size:
            continue
        before_mode = int(fields[0], 8) & 0o777
        info = agent.checkpoint_store.write_blob(raw_stdout, "text")
        states[path] = {
            "before_blob_ref": info["blob_ref"],
            "before_hash": info["content_hash"],
            "before_mode": before_mode,
        }
    return states


def _build_file_entries(
    agent,
    name,
    args,
    affected_paths,
    before_snapshot,
    before_file_states=None,
    before_existed=None,
    tool_change_id="",
):
    """基于工具类型和实际 affected_paths 构造 file_entries。

    每个 entry 都记录：
    - snapshot_eligible + ineligible_reason（不合格就直接跳出，不落 blob）
    - after_blob_ref/after_hash/expected_current_hash（合格时按 sha256 落到 blob store）
    - before_blob_ref/before_hash（当且仅当拿到了真实字节 or 合法 sha256）
    - change_kind：created / modified / deleted，用 before_existed 判断“执行前是否存在”

    before_snapshot 里的值都必须是真实 sha256；path 是否存在的问题由 before_existed
    单独回答。这样 observer 的 git 状态标记（'MM'、'??'）不会被误当成 hash。
    """
    store = agent.checkpoint_store
    workspace_root = agent.root
    entries = []

    for raw_path in affected_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        if before_existed is not None:
            existed_before = normalized in before_existed
        else:
            existed_before = normalized in (before_snapshot or {})
        limit = agent.project_max_blob_size
        try:
            after_state = securitylib.read_regular_bytes_anchored(
                workspace_root, normalized, max_bytes=limit
            )
        except (OSError, ValueError):
            after_state = {"exists": False, "data": None, "mode": None}
        exists_after = after_state["exists"]
        before_state = dict((before_file_states or {}).get(normalized) or {})
        before_mode = before_state.get("before_mode") if existed_before else None
        after_mode = None
        if not existed_before and exists_after:
            change_kind = "created"
        elif existed_before and not exists_after:
            change_kind = "deleted"
        else:
            change_kind = "modified"

        path_sensitive = securitylib.is_sensitive_path(normalized)
        entry = {
            "path": normalized,
            "change_kind": change_kind,
            "snapshot_eligible": not path_sensitive,
            "ineligible_reason": "sensitive_path" if path_sensitive else "",
            "content_kind": "text",
            "before_exists": existed_before,
            "before_blob_ref": "",
            "before_hash": "",
            "before_mode": before_mode,
            "after_exists": exists_after,
            "after_blob_ref": "",
            "after_hash": "",
            "after_mode": None,
            "expected_current_hash": "",
            "source_tool_change_ids": [tool_change_id] if tool_change_id else [],
        }

        if entry["snapshot_eligible"] and exists_after:
            try:
                data = after_state["data"]
                after_mode = after_state["mode"]
                entry["after_mode"] = after_mode
                eligibility = snapshot_bytes_eligibility(
                    workspace_root,
                    normalized,
                    data,
                    max_blob_size=limit,
                    env=getattr(agent, "redaction_env", None),
                    secret_env_names=getattr(agent, "secret_env_names", ()),
                )
                if not eligibility.get("snapshot_eligible", False):
                    entry["snapshot_eligible"] = False
                    entry["ineligible_reason"] = eligibility[
                        "ineligible_reason"
                    ]
                else:
                    info = store.write_blob(data, "text")
                    entry["after_blob_ref"] = info["blob_ref"]
                    entry["after_hash"] = info["content_hash"]
                    entry["expected_current_hash"] = info["content_hash"]
            except (OSError, ValueError):
                if entry["ineligible_reason"] != "sensitive_content":
                    entry["snapshot_eligible"] = False
                    entry["ineligible_reason"] = "read_failed"

        # before_file_states 保存了执行前的真实字节；如果没有对应 blob，
        # 退回到 before_snapshot 里的 sha256，至少保留冲突判断线索。
        if before_state:
            entry["before_blob_ref"] = str(before_state.get("before_blob_ref") or "")
            entry["before_hash"] = str(before_state.get("before_hash") or "")
        elif existed_before:
            candidate = str((before_snapshot or {}).get(normalized) or "")
            # 只接受合法 sha256 十六进制；observer 的 git 状态标记不会通过。
            if len(candidate) == 64 and all(char in "0123456789abcdef" for char in candidate):
                entry["before_hash"] = candidate
        if (
            entry["snapshot_eligible"]
            and change_kind in {"modified", "deleted"}
            and not entry["before_blob_ref"]
        ):
            entry["snapshot_eligible"] = False
            entry["ineligible_reason"] = "before_blob_unavailable"
        if (
            entry["before_exists"] and entry["before_mode"] is None
        ) or (
            entry["after_exists"] and entry["after_mode"] is None
        ):
            entry["snapshot_eligible"] = False
            entry["ineligible_reason"] = "mode_unknown"
            entry["before_mode"] = None
            entry["after_mode"] = None
        entries.append(entry)
    return entries
