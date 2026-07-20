"""Structured, audited tool execution for the agent runtime."""

from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import subprocess
import textwrap

from pony.security import workspace_files as workspace_files
from pony.security import private_files as private_files
from pony.security.command_policy import assess_command
from pony.tools.permissions import PermissionDecision, PermissionMode, decide_permission
from pony.tools.subprocess import (
    _validate_hardened_git_args,
    _validate_hardened_git_repository,
    run_hardened_git,
)
from pony.tools.registry import _ALLOWED_EFFECT_CLASSES, memory_write_intent
from pony.tools.shell import (
    DEFAULT_RUN_SHELL_TIMEOUT,
    ApprovedShellExecution,
)
from pony.tools.validation import SensitiveToolError
from pony.state.workflow import PlanValidationError, SensitivePlanError
from pony.state.file_lock import locked_file
from pony.agent.verification import verification_evidence_for_execution


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


@dataclass(frozen=True)
class PolicyDecision:
    schema_version: int
    decision: str
    reason_code: str
    effect_class: str
    risk_class: str
    evidence_complete: bool
    approval: dict

    @classmethod
    def unknown_tool(cls):
        return cls(1, "deny", "unknown_tool", "workspace_write", "complex", True, {})

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "effect_class": self.effect_class,
            "risk_class": self.risk_class,
            "evidence_complete": self.evidence_complete,
            "approval": dict(self.approval),
        }


def _effect_class(tool):
    if isinstance(tool, Mapping):
        effect_class = tool.get("effect_class")
        if effect_class in _ALLOWED_EFFECT_CLASSES:
            return effect_class
    return "workspace_write"


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


def _validation_rejection(agent, tool, name, args, effect_class):
    try:
        agent.validate_tool(name, args)
    except (SensitiveToolError, SensitivePlanError) as exc:
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
    except PlanValidationError as exc:
        return ToolExecutionResult(
            content=f"error: {exc.code}",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code=exc.code,
                risk_level="low",
            ),
        )
    except workspace_files.WorkspaceIOError as exc:
        return ToolExecutionResult(
            content=f"error: {exc.code}",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code=exc.code,
                security_event_type=exc.code,
                risk_level="high",
            ),
        )
    except Exception as exc:
        example = agent.tool_example(name)
        message = f"error: invalid arguments for {name}: {exc}"
        if example:
            message += f"\nexample: {example}"
        security_event_type = (
            "path_escape" if "path escapes workspace" in str(exc) else ""
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
        "timed_out": bool(value.get("timed_out", False)),
    }


def _format_shell_result(result):
    return textwrap.dedent(
        f"""\
        exit_code: {result["exit_code"]}
        stdout:
        {result["stdout"].strip() or "(empty)"}
        stderr:
        {result["stderr"].strip() or "(empty)"}
        """
    ).strip()


@dataclass(frozen=True)
class _ShellPreparation:
    agent: object
    tool: Mapping
    args: dict
    effect_class: str
    assessment: dict
    mode: str
    original_args: dict
    original_assessment: dict
    timeout: int
    executable_name: str
    argv: tuple


def _shell_preflight_rejection(agent, tool, args, effect_class, assessment, mode):
    if agent.read_only:
        return _shell_rejection(
            assessment=assessment,
            mode=mode,
            outcome="blocked",
            effect_class=effect_class,
            tool_error_code="read_only_block",
            content="error: read-only mode blocks run_shell",
            security_event_type="read_only_block",
        )
    rejection = _validation_rejection(agent, tool, "run_shell", args, effect_class)
    if rejection is not None:
        return _add_initial_shell_policy(rejection, assessment, mode)
    if agent.repeated_tool_call("run_shell", args):
        return _shell_rejection(
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
    return None


def _freeze_shell_preparation(agent, tool, args, effect_class, assessment, mode):
    original_args = deepcopy(args)
    if assessment["execution_mode"] == "argv":
        argv = tuple(assessment["argv"])
        executable_name = argv[0]
    else:
        argv = ()
        executable_name = "sh"
    return _ShellPreparation(
        agent=agent,
        tool=tool,
        args=args,
        effect_class=effect_class,
        assessment=assessment,
        mode=mode,
        original_args=original_args,
        original_assessment=deepcopy(assessment),
        timeout=int(original_args.get("timeout", DEFAULT_RUN_SHELL_TIMEOUT)),
        executable_name=executable_name,
        argv=argv,
    )


def _preparation_rejection(preparation, *, code, content, outcome="blocked"):
    return _shell_rejection(
        assessment=preparation.assessment,
        mode=preparation.mode,
        outcome=outcome,
        effect_class=preparation.effect_class,
        tool_error_code=code,
        content=content,
        security_event_type=code,
    )


def _approval_payload(preparation):
    return preparation.agent.redact_artifact(deepcopy(preparation.original_args))


def _revalidate_approved_shell(preparation, approval_payload, payload_snapshot):
    rejection = _validation_rejection(
        preparation.agent,
        preparation.tool,
        "run_shell",
        preparation.args,
        preparation.effect_class,
    )
    if rejection is not None:
        return _add_initial_shell_policy(
            rejection,
            preparation.assessment,
            preparation.mode,
        )
    reassessment = assess_command(
        str(preparation.args.get("command", "")),
        preparation.agent.root,
    )
    if (
        preparation.args != preparation.original_args
        or approval_payload != payload_snapshot
        or reassessment != preparation.original_assessment
    ):
        return _preparation_rejection(
            preparation,
            code="approval_arguments_changed",
            content="error: approved arguments changed for run_shell",
        )
    return None


def _approve_shell(preparation, *, requires_approval):
    if not requires_approval:
        return "allowed", None
    payload = _approval_payload(preparation)
    payload_snapshot = deepcopy(payload)
    if not preparation.agent.approve("run_shell", payload):
        return None, _preparation_rejection(
            preparation,
            code="approval_denied",
            content="error: approval denied for run_shell",
            outcome="denied",
        )
    rejection = _revalidate_approved_shell(preparation, payload, payload_snapshot)
    return (None, rejection) if rejection is not None else ("approved", None)


def _validate_host_git(preparation, executable):
    try:
        _validate_hardened_git_args(preparation.argv[1:])
    except ValueError:
        return _preparation_rejection(
            preparation,
            code="unsafe_git_arguments",
            content="error: unsafe git arguments",
        )
    try:
        _validate_hardened_git_repository(
            executable,
            cwd=preparation.agent.root,
            args=preparation.argv[1:],
            timeout=preparation.timeout,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return _preparation_rejection(
            preparation,
            code="unsafe_git_config",
            content="error: unsafe git repository config",
        )
    return None


def _compile_host_shell(preparation):
    executable = (
        preparation.agent.trusted_executables.get(preparation.executable_name)
        if Path(preparation.executable_name).name == preparation.executable_name
        else None
    )
    if not executable:
        available = ", ".join(sorted(preparation.agent.trusted_executables)) or "none"
        return None, _preparation_rejection(
            preparation,
            code="trusted_executable_missing",
            content=(
                "error: trusted executable missing for run_shell; "
                f"available trusted executable names: {available}"
            ),
        )
    if preparation.executable_name == "git":
        rejection = _validate_host_git(preparation, executable)
        if rejection is not None:
            return None, rejection
    return (
        ApprovedShellExecution(
            exact_command=str(preparation.original_args.get("command", "")),
            argv=preparation.argv,
            execution_mode=preparation.assessment["execution_mode"],
            executable=executable,
            timeout=preparation.timeout,
        ),
        None,
    )


def _prepare_shell_execution(
    agent,
    tool,
    args,
    effect_class,
    assessment,
    *,
    requires_approval,
):
    mode = agent.current_permission_mode()
    rejection = _shell_preflight_rejection(
        agent, tool, args, effect_class, assessment, mode
    )
    if rejection is not None:
        return None, None, rejection

    preparation = _freeze_shell_preparation(
        agent, tool, args, effect_class, assessment, mode
    )
    outcome, rejection = _approve_shell(
        preparation,
        requires_approval=requires_approval,
    )
    if rejection is not None:
        return None, None, rejection
    execution, rejection = _compile_host_shell(preparation)
    if rejection is not None:
        return None, None, rejection
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
        mode=agent.current_permission_mode(),
        outcome="blocked",
        effect_class=effect_class,
        tool_error_code=("sensitive_path_block" if sensitive else "command_rejected"),
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
    elif tool is None or tool.get("effect_class") not in _ALLOWED_EFFECT_CLASSES:
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
    return _add_initial_shell_policy(
        rejection, command_assessment, agent.current_permission_mode()
    )


def _permission_decision(agent, name, effect_class, command_assessment):
    mode = agent.current_permission_mode()
    if (
        mode == PermissionMode.BYPASS_PERMISSIONS.value
        and not getattr(agent, "bypass_permissions_available", False)
    ):
        decision = PermissionDecision.DENY
        code = "bypass_permissions_not_authorized"
    elif agent.read_only and effect_class != "read_only":
        decision = PermissionDecision.DENY
        code = "read_only_block"
    else:
        decision = decide_permission(
            project_trusted=agent.project_trusted,
            mode=mode,
            effect_class=effect_class,
            explicit=agent.permission_for_tool(name),
            builtin_edit=name in {"write_file", "patch_file"},
            auto_allow=(
                name in {"write_file", "patch_file", "memory_save"}
                or (
                    command_assessment is not None
                    and command_assessment.get("decision") == "allow"
                )
            ),
            plan_write=name == "write_plan",
        )
        code = (
            "permission_mode_block"
            if agent.project_trusted
            else "permission_denied"
        )
    if decision is not PermissionDecision.DENY:
        return decision, None
    rejection = ToolExecutionResult(
        content=f"error: permission mode '{mode}' blocks {name}",
        metadata=_metadata(
            "rejected",
            effect_class=effect_class,
            tool_error_code=code,
            security_event_type=code,
            risk_level="high",
        ),
    )
    if command_assessment is None:
        return decision, rejection
    return decision, _add_initial_shell_policy(
        rejection,
        command_assessment,
        mode,
    )


def _prepare_non_shell_tool(
    agent,
    tool,
    name,
    args,
    effect_class,
    *,
    requires_approval,
):
    rejection = _validation_rejection(agent, tool, name, args, effect_class)
    if rejection is not None:
        return rejection
    if name == "memory_save":
        task_state = getattr(agent, "current_task_state", None)
        current_user = str(getattr(task_state, "user_request", "") or "")
        if not memory_write_intent(
            current_user,
            delegated=int(getattr(agent, "depth", 0) or 0) > 0,
        ):
            return ToolExecutionResult(
                content=(
                    "error: memory_save requires explicit authorization in "
                    "the current user request"
                ),
                metadata=_metadata(
                    "rejected",
                    effect_class=effect_class,
                    tool_error_code="memory_write_not_authorized",
                    security_event_type="memory_write_not_authorized",
                    risk_level="high",
                ),
            )
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
    if not requires_approval:
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


def _prepare_exit_plan_tool(agent, tool, args, effect_class):
    rejection = _validation_rejection(
        agent,
        tool,
        "exit_plan_mode",
        args,
        effect_class,
    )
    if rejection is not None:
        return rejection, None
    durable_tree = agent.session_store.load_tree(agent.session["id"])
    durable = durable_tree.projection
    if any(
        durable.get(key) != agent.session.get(key)
        for key in (
            "permission_mode",
            "plan_text",
            "plan_revision",
            "pre_plan_mode",
        )
    ):
        agent._reload_session_projection()
        return ToolExecutionResult(
            content="error: plan changed during approval",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="plan_approval_changed",
                security_event_type="approval_arguments_changed",
                risk_level="high",
            ),
        ), None
    if (
        durable.get("pre_plan_mode") == PermissionMode.BYPASS_PERMISSIONS.value
        and not getattr(agent, "bypass_permissions_available", False)
    ):
        return ToolExecutionResult(
            content="error: bypass permission capability is unavailable",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="bypass_permissions_not_authorized",
                security_event_type="bypass_permissions_not_authorized",
                risk_level="high",
            ),
        ), None
    plan = agent.current_plan()
    revision = agent.current_plan_revision()
    if not plan.strip() or revision < 1:
        return ToolExecutionResult(
            content="error: no plan has been saved",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="plan_missing",
                risk_level="high",
            ),
        ), None
    expected_leaf_id = durable_tree.leaf_id
    original_args = deepcopy(args)
    approval_payload = {"plan": plan, "revision": revision}
    payload_snapshot = deepcopy(approval_payload)
    if not agent.approve("exit_plan_mode", approval_payload):
        return ToolExecutionResult(
            content="error: plan rejected; remain in plan mode",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="plan_rejected",
                security_event_type="approval_denied",
                risk_level="high",
            ),
        ), None
    rejection = _validation_rejection(
        agent,
        tool,
        "exit_plan_mode",
        args,
        effect_class,
    )
    if rejection is not None:
        return rejection, None
    if (
        args != original_args
        or approval_payload != payload_snapshot
        or agent.current_plan_revision() != revision
        or agent.current_plan() != plan
    ):
        return ToolExecutionResult(
            content="error: plan changed during approval",
            metadata=_metadata(
                "rejected",
                effect_class=effect_class,
                tool_error_code="plan_approval_changed",
                security_event_type="approval_arguments_changed",
                risk_level="high",
            ),
        ), None
    return None, {
        **payload_snapshot,
        "expected_leaf_id": expected_leaf_id,
    }


def _prepare_tool_request(agent, name, args):
    tool = agent.tools.get(name)
    effect_class = _effect_class(tool)
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
    rejection = _validation_rejection(agent, tool, name, args, effect_class)
    if rejection is not None:
        if command_assessment is None:
            return None, rejection
        return None, _add_initial_shell_policy(
            rejection,
            command_assessment,
            agent.current_permission_mode(),
        )
    permission, rejection = _permission_decision(
        agent,
        name,
        effect_class,
        command_assessment,
    )
    if rejection is not None:
        return None, rejection
    requires_approval = permission is PermissionDecision.ASK

    shell_execution = None
    command_risk = ""
    command_approval = {}
    runner_args = args
    if command_assessment is not None:
        command_risk = command_assessment["risk_class"]
        shell_execution, command_approval, rejection = _prepare_shell_execution(
            agent,
            tool,
            args,
            effect_class,
            command_assessment,
            requires_approval=requires_approval,
        )
    else:
        if name == "exit_plan_mode":
            rejection, runner_args = _prepare_exit_plan_tool(
                agent, tool, args, effect_class
            )
        else:
            rejection = _prepare_non_shell_tool(
                agent,
                tool,
                name,
                args,
                effect_class,
                requires_approval=requires_approval,
            )
    if rejection is not None:
        return None, rejection

    policy_decision = PolicyDecision(
        1,
        "allow",
        "allowed",
        effect_class,
        command_risk or ("complex" if tool["risky"] else "simple"),
        True,
        {
            "mode": agent.current_permission_mode(),
            "required": requires_approval,
            "outcome": command_approval.get("outcome", "not_required"),
        },
    ).to_dict()
    return {
        "agent": agent,
        "name": name,
        "args": args,
        "runner_args": runner_args,
        "tool": tool,
        "effect_class": effect_class,
        "shell_execution": shell_execution,
        "command_risk": command_risk,
        "command_approval": command_approval,
        "policy_decision": policy_decision,
    }, None


def _invoke_prepared_tool(prepared, execution):
    agent = prepared["agent"]
    if prepared["name"] != "run_shell":
        execution["runner_started"] = True
        raw_content = prepared["tool"]["run"](prepared["runner_args"])
        execution["runner_completed"] = True
        execution["content"] = str(agent.redact_text(raw_content))
        return

    shell_execution = prepared["shell_execution"]
    approval = prepared["command_approval"]
    if shell_execution.execution_mode == "argv" and shell_execution.argv[0] == "git":
        approval["runner_executed"] = True
        execution["runner_started"] = True
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
        approval["runner_executed"] = True
        execution["runner_started"] = True
        raw_result = prepared["tool"]["run"](shell_execution)
    shell_result = _structured_shell_result(agent, raw_result)
    execution["shell_result"] = shell_result
    execution["runner_completed"] = True
    approval["exit_code"] = shell_result["exit_code"]
    if shell_result["exit_code"] is not None:
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
    execution["content"] = _format_shell_result(shell_result)


def _empty_effects():
    return {
        "affected_paths": [],
        "diff_summary": [],
        "workspace_changed": False,
    }


def _observe_tool_effects(prepared, before):
    if before is None:
        return _empty_effects()
    observer = prepared["agent"].workspace_observer
    delta = observer.diff(before, observer.capture_call_end())
    paths = list(delta.get("changed_paths", []))
    return {
        "affected_paths": paths,
        "diff_summary": list(delta.get("summaries", [])),
        "workspace_changed": bool(paths),
    }


def _workspace_root_identity_rejection(prepared):
    try:
        current = private_files.private_directory_identity(prepared["agent"].root)
    except (OSError, RuntimeError, ValueError):
        current = None
    if current == prepared["agent"].workspace_root_identity:
        return None
    return ToolExecutionResult(
        content="error: workspace root changed before workspace write",
        metadata=_base_result_metadata(
            prepared,
            {"verification_evidence": None},
            "rejected",
            "workspace_root_changed",
            _empty_effects(),
        ),
    )


def _effect_observation_unknown_result(prepared, execution, primary_error=None):
    content = "workspace effects could not be verified; stopping this run"
    if primary_error is not None:
        content = (
            f"error: tool {prepared['name']} failed: "
            f"{prepared['agent'].redact_text(primary_error)}\n{content}"
        )
    metadata = _base_result_metadata(
        prepared,
        execution,
        "error",
        getattr(primary_error, "code", "") or "workspace_effect_unknown",
        _empty_effects(),
    )
    metadata["effect_observation_unknown"] = True
    metadata["security_event_type"] = "workspace_effect_unknown"
    return ToolExecutionResult(content=content, metadata=metadata)


def _base_result_metadata(prepared, execution, status, code, effects):
    metadata = _metadata(
        status,
        effect_class=prepared["effect_class"],
        tool_error_code=code,
        risk_level="high" if prepared["tool"]["risky"] else "low",
        affected_paths=effects["affected_paths"],
        workspace_changed=effects["workspace_changed"],
        diff_summary=effects["diff_summary"],
    )
    metadata["policy_decision"] = dict(prepared["policy_decision"])
    _add_command_policy(
        metadata,
        prepared["command_risk"],
        prepared["command_approval"],
    )
    evidence = execution["verification_evidence"]
    if evidence is not None:
        metadata["verification_evidence"] = evidence
    return metadata


def _finish_tool_success(prepared, execution, effects):
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
    metadata = _base_result_metadata(
        prepared,
        execution,
        tool_status,
        tool_error_code,
        effects,
    )
    return ToolExecutionResult(content=execution["content"], metadata=metadata)


def _run_tool_lifecycle(prepared):
    execution = {
        "runner_started": False,
        "runner_completed": False,
        "shell_result": None,
        "verification_evidence": None,
        "content": "",
    }
    before = None
    effects = _empty_effects()
    mutation_context = (
        locked_file(prepared["agent"].mutation_lock_path, require_lock=True)
        if prepared["effect_class"] != "read_only"
        else nullcontext()
    )
    try:
        with mutation_context:
            if prepared["effect_class"] == "workspace_write":
                rejection = _workspace_root_identity_rejection(prepared)
                if rejection is not None:
                    return rejection
                before = prepared["agent"].workspace_observer.capture_call_start()
            try:
                _invoke_prepared_tool(prepared, execution)
            except KeyboardInterrupt:
                try:
                    effects = _observe_tool_effects(prepared, before)
                except Exception:
                    effects = _empty_effects()
                metadata = _base_result_metadata(
                    prepared,
                    execution,
                    "interrupted",
                    "tool_interrupted",
                    effects,
                )
                prepared["agent"]._last_tool_result_metadata = dict(metadata)
                raise
            except Exception as exc:
                try:
                    effects = _observe_tool_effects(prepared, before)
                except Exception:
                    if execution["runner_started"] and before is not None:
                        return _effect_observation_unknown_result(
                            prepared, execution, exc
                        )
                    effects = _empty_effects()
                changed = effects["workspace_changed"]
                metadata = _base_result_metadata(
                    prepared,
                    execution,
                    "partial_success" if changed else "error",
                    getattr(exc, "code", "")
                    or ("tool_partial_success" if changed else "tool_failed"),
                    effects,
                )
                return ToolExecutionResult(
                    content=(
                        f"error: tool {prepared['name']} failed: "
                        f"{prepared['agent'].redact_text(exc)}"
                    ),
                    metadata=metadata,
                )
            try:
                effects = _observe_tool_effects(prepared, before)
            except Exception:
                if execution["runner_started"] and before is not None:
                    return _effect_observation_unknown_result(prepared, execution)
                raise
            return _finish_tool_success(prepared, execution, effects)
    except KeyboardInterrupt:
        metadata = _base_result_metadata(
            prepared,
            execution,
            "interrupted",
            "tool_interrupted",
            effects,
        )
        prepared["agent"]._last_tool_result_metadata = dict(metadata)
        raise
    except Exception as exc:
        status = "partial_success" if effects["workspace_changed"] else "error"
        metadata = _base_result_metadata(
            prepared,
            execution,
            status,
            getattr(exc, "code", "")
            or ("tool_finalize_failed" if effects["workspace_changed"] else "tool_failed"),
            effects,
        )
        return ToolExecutionResult(
            content=(
                f"error: tool {prepared['name']} failed: "
                f"{prepared['agent'].redact_text(exc)}"
            ),
            metadata=metadata,
        )


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, name, args):
        prepared, rejection = _prepare_tool_request(self.agent, name, args)
        if rejection is not None:
            rejection.metadata["policy_decision"] = PolicyDecision(
                1,
                "deny",
                rejection.metadata.get("tool_error_code") or "denied",
                rejection.metadata.get("effect_class", "workspace_write"),
                rejection.metadata.get("command_risk_class", "complex"),
                True,
                {
                    "mode": self.agent.current_permission_mode(),
                    "required": False,
                    "outcome": "denied",
                },
            ).to_dict()
            return rejection
        return _run_tool_lifecycle(prepared)


def _add_command_policy(metadata, command_risk, command_approval):
    if command_risk:
        metadata["command_risk_class"] = command_risk
    if command_approval:
        metadata["command_approval"] = dict(command_approval)
    return metadata
