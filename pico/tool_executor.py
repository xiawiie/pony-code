"""Structured tool execution for the agent runtime.

Phase 1 里这里承担两件事：
1. 沿用原来的“校验 → 审批 → 执行 → 度量”流程，metadata 与旧字段保持兼容。
2. 把每一次真正跑到 tool["run"] 的调用都记进一条 Tool Change Record：
   pending → finalized/error/partial_success，中途无论走哪条路都要 finalize。
"""

from dataclasses import dataclass
import re

from .recovery_checkpoint_writer import current_recovery_checkpoint_id
from .recovery_paths import (
    normalize_workspace_relative_path,
    resolve_workspace_relative_path,
)
from .recovery_policy import (
    command_risk_class,
    evaluate_command_approval,
    snapshot_eligibility,
)
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
    "delegate": "workspace_write",
}


def _effect_class(name, risky):
    return _EFFECT_CLASS_BY_TOOL.get(name, "workspace_write" if risky else "read_only")


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
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
        "read_only": read_only,
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
                summary[key] = value[:240] + "..."
            else:
                summary[key] = value
    return summary


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, name, args):
        agent = self.agent
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            )

        tool = agent.tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            )

        try:
            agent.validate_tool(name, args)
        except Exception as exc:
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        if agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        # 命令策略只针对 run_shell 才有意义，其它工具的 approval 仍走原有的 risky/approve 路径。
        command_risk = ""
        command_approval = {}
        if name == "run_shell":
            command_text = str(args.get("command", ""))
            command_risk = command_risk_class(command_text)
            command_approval = evaluate_command_approval(command_risk)
            if command_approval.get("decision") == "reject":
                return ToolExecutionResult(
                    content=f"error: command policy rejected {name}",
                    metadata=_add_command_policy(
                        _metadata(
                            "rejected",
                            tool_error_code="command_rejected",
                            risk_level="high",
                            read_only=False,
                        ),
                        command_risk,
                        command_approval,
                    ),
                )

        if tool["risky"] and not agent.approve(name, args):
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_add_command_policy(
                    _metadata(
                        "rejected",
                        tool_error_code="approval_denied",
                        security_event_type="read_only_block" if agent.read_only else "approval_denied",
                        risk_level="high",
                        read_only=False,
                    ),
                    command_risk,
                    command_approval,
                ),
            )

        # 到这里我们准备真的跑工具，可以开一条 pending 记录。
        turn_id = getattr(getattr(agent, "current_task_state", None), "task_id", "") or ""
        parent_checkpoint = current_recovery_checkpoint_id(agent.session)
        effect_class = _effect_class(name, tool["risky"])
        pending_record = None
        if tool["risky"]:
            pending_record = agent.tool_change_recorder.start(
                checkpoint_id=parent_checkpoint,
                turn_id=turn_id,
                tool_name=name,
                effect_class=effect_class,
                input_summary=_summarize_input(args),
            )

        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        # run_shell 需要一个更细的前后对比来算出“这一条命令改了哪些文件”
        observer_before = None
        if name == "run_shell":
            observer_before = agent.workspace_observer.capture()

        try:
            content = clip(tool["run"](args))
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)

            shell_side_effects = []
            if name == "run_shell" and observer_before is not None:
                observer_after = agent.workspace_observer.capture()
                delta = agent.workspace_observer.diff(observer_before, observer_after)
                delta_paths = list(delta.get("changed_paths", []))
                shell_side_effects = list(delta.get("summaries", []))
                if delta_paths:
                    # 用 observer 算出来的 delta 覆盖掉旧的 snapshot 差集，行为更精准
                    affected_paths = delta_paths
                    diff_summary = shell_side_effects
                    workspace_changed = True

            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"

            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            metadata = _add_command_policy(metadata, command_risk, command_approval)

            file_entries = _build_file_entries(agent, name, args, affected_paths, before_snapshot)
            if pending_record is not None:
                terminal_status = "finalized"
                if tool_status == "partial_success":
                    terminal_status = "partial_success"
                elif tool_status == "error":
                    terminal_status = "error"
                error_payload = None
                if tool_status == "error":
                    error_payload = {"code": tool_error_code or "tool_failed", "message": content[:400]}
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
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=content, metadata=metadata)
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            shell_side_effects = []
            if name == "run_shell" and observer_before is not None:
                try:
                    observer_after = agent.workspace_observer.capture()
                    delta = agent.workspace_observer.diff(observer_before, observer_after)
                    delta_paths = list(delta.get("changed_paths", []))
                    shell_side_effects = list(delta.get("summaries", []))
                    if delta_paths:
                        affected_paths = delta_paths
                        diff_summary = shell_side_effects
                        workspace_changed = True
                except Exception:  # pragma: no cover - defensive
                    shell_side_effects = []
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            metadata = _add_command_policy(metadata, command_risk, command_approval)

            file_entries = _build_file_entries(agent, name, args, affected_paths, before_snapshot)
            if pending_record is not None:
                terminal_status = "partial_success" if workspace_changed else "error"
                error_payload = {"code": metadata["tool_error_code"] or "tool_failed", "message": str(exc)[:400]}
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
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)


def _add_command_policy(metadata, command_risk, command_approval):
    if command_risk:
        metadata["command_risk_class"] = command_risk
    if command_approval:
        metadata["command_approval"] = dict(command_approval)
    return metadata


def _build_file_entries(agent, name, args, affected_paths, before_snapshot):
    """基于工具类型和实际 affected_paths 构造 file_entries。

    每个 entry 都记录：
    - snapshot_eligible + ineligible_reason（不合格就直接跳出，不落 blob）
    - after_blob_ref/after_hash/expected_current_hash（合格时按 sha256 落到 blob store）
    - before_blob_ref/before_hash（当且仅当 before_snapshot 里有旧的 sha256）
    - change_kind：created / modified / deleted，用 before_snapshot 是否含 key 判断
    """
    store = agent.checkpoint_store
    workspace_root = agent.root
    entries = []

    for raw_path in affected_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        eligibility = snapshot_eligibility(workspace_root, normalized)
        try:
            resolved = resolve_workspace_relative_path(workspace_root, normalized)
        except ValueError:
            continue

        existed_before = normalized in (before_snapshot or {})
        exists_after = resolved.exists()
        if not existed_before and exists_after:
            change_kind = "created"
        elif existed_before and not exists_after:
            change_kind = "deleted"
        else:
            change_kind = "modified"

        entry = {
            "path": normalized,
            "change_kind": change_kind,
            "snapshot_eligible": bool(eligibility.get("snapshot_eligible", False)),
            "ineligible_reason": eligibility.get("ineligible_reason", ""),
            "content_kind": "text",
            "before_blob_ref": "",
            "before_hash": "",
            "after_blob_ref": "",
            "after_hash": "",
            "expected_current_hash": "",
        }

        if entry["snapshot_eligible"] and exists_after:
            try:
                data = resolved.read_bytes()
                info = store.write_blob(data, "text")
                entry["after_blob_ref"] = info["blob_ref"]
                entry["after_hash"] = info["content_hash"]
                entry["expected_current_hash"] = info["content_hash"]
            except OSError:
                entry["snapshot_eligible"] = False
                entry["ineligible_reason"] = "read_failed"

        # before_snapshot 里存的是 sha256（见 Pico.capture_workspace_snapshot），
        # 直接把它当作 before_hash 使用，但 before_blob 没有真实字节留存下来。
        if existed_before:
            entry["before_hash"] = str((before_snapshot or {}).get(normalized) or "")
        entries.append(entry)
    return entries
