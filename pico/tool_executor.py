"""Structured tool execution for the agent runtime.

Phase 1 里这里承担两件事：
1. 沿用原来的“校验 → 审批 → 执行 → 度量”流程，metadata 与旧字段保持兼容。
2. 把每一次真正跑到 tool["run"] 的调用都记进一条 Tool Change Record：
   pending → finalized/error/partial_success，中途无论走哪条路都要 finalize。
"""

from dataclasses import dataclass
import re
import subprocess

from .recovery_checkpoint_writer import current_recovery_checkpoint_id
from .recovery_paths import (
    hash_file_bytes,
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


# 每种“直接改文件”的工具，声明它们的参数里哪些 key 装了 workspace 相对路径。
# 之所以要显式声明，是为了：
#   - 新增工具时必须显式登记，否则 pending Tool Change Record 找不到 before-blob；
#   - _direct_tool_candidate_paths 不再靠 hard-coded write_file/patch_file 分支。
_PATH_ARG_NAMES_BY_TOOL = {
    "write_file": ("path",),
    "patch_file": ("path",),
    # 未来的 move_file 之类工具可以在这里加 ("source", "destination")
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
            if command_approval.get("decision") == "ask" and agent.approval_policy != "ask":
                return ToolExecutionResult(
                    content=f"error: command approval required for {name}",
                    metadata=_add_command_policy(
                        _metadata(
                            "rejected",
                            tool_error_code="command_approval_required",
                            security_event_type="command_approval_required",
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
        records_recovery = effect_class != "read_only"
        pending_record = None
        if records_recovery:
            pending_record = agent.tool_change_recorder.start(
                checkpoint_id=parent_checkpoint,
                turn_id=turn_id,
                tool_name=name,
                effect_class=effect_class,
                input_summary=_summarize_input(args),
            )

        before_paths = _direct_tool_candidate_paths(name, args)
        # 直接改文件的工具（write_file/patch_file）在执行前只对它们要碰的那一小
        # 组路径落 blob；这样恢复能拿到真实字节，也不会去读整个 workspace。
        before_snapshot = _capture_path_snapshot(agent, before_paths) if records_recovery else {}
        before_file_states = _capture_before_file_states_for_paths(agent, before_paths) if records_recovery else {}
        observer_before = None
        if name == "run_shell":
            # run_shell 只做轻量的 before 观察，不预先落 blob。真正的 before-blob
            # 会在观察到 delta 之后按需生成（见下面的 _lazy_capture_before_file_states）。
            observer_before = agent.workspace_observer.capture()

        try:
            content = clip(tool["run"](args))
            shell_side_effects = []
            if name == "run_shell" and observer_before is not None:
                observer_after = agent.workspace_observer.capture()
                delta = agent.workspace_observer.diff(observer_before, observer_after)
                affected_paths = list(delta.get("changed_paths", []))
                shell_side_effects = list(delta.get("summaries", []))
                diff_summary = shell_side_effects
                # 只对 delta 里真正变了的路径去挖 before-blob；这里
                # 先看 git HEAD（对于 tracked file 最准确），拿不到再兜底。
                before_file_states = _fill_git_head_before_file_states(agent, affected_paths, before_file_states)
                before_snapshot = _capture_path_snapshot_from_observer(observer_before, affected_paths)
                before_snapshot = _merge_before_snapshot(before_snapshot, before_file_states)
                workspace_changed = bool(affected_paths)
            else:
                after_snapshot = _capture_path_snapshot(agent, before_paths) if records_recovery else before_snapshot
                affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
                workspace_changed = bool(affected_paths)

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

            file_entries = _build_file_entries(agent, name, args, affected_paths, before_snapshot, before_file_states)
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
            shell_side_effects = []
            if name == "run_shell" and observer_before is not None:
                try:
                    observer_after = agent.workspace_observer.capture()
                    delta = agent.workspace_observer.diff(observer_before, observer_after)
                    affected_paths = list(delta.get("changed_paths", []))
                    shell_side_effects = list(delta.get("summaries", []))
                    diff_summary = shell_side_effects
                    before_file_states = _fill_git_head_before_file_states(agent, affected_paths, before_file_states)
                    before_snapshot = _capture_path_snapshot_from_observer(observer_before, affected_paths)
                    before_snapshot = _merge_before_snapshot(before_snapshot, before_file_states)
                    workspace_changed = bool(affected_paths)
                except Exception:  # pragma: no cover - defensive
                    affected_paths = []
                    diff_summary = []
                    workspace_changed = False
                    shell_side_effects = []
            else:
                after_snapshot = _capture_path_snapshot(agent, before_paths) if records_recovery else before_snapshot
                affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
                workspace_changed = bool(affected_paths)
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

            file_entries = _build_file_entries(agent, name, args, affected_paths, before_snapshot, before_file_states)
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


def _direct_tool_candidate_paths(name, args):
    if not isinstance(args, dict):
        return []
    arg_names = _PATH_ARG_NAMES_BY_TOOL.get(name)
    if not arg_names:
        return []
    paths = []
    for key in arg_names:
        value = args.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    return paths


def _capture_path_snapshot(agent, raw_paths):
    snapshot = {}
    workspace_root = agent.root
    for raw_path in raw_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
            resolved = resolve_workspace_relative_path(workspace_root, normalized)
        except ValueError:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            snapshot[normalized] = hash_file_bytes(resolved)["content_hash"]
        except OSError:
            continue
    return snapshot


def _capture_path_snapshot_from_observer(observer_capture, affected_paths):
    """从 observer 的 before capture 里挑出 affected_paths 对应的记录。

    这样 diff_workspace_snapshots 依然能识别“路径原本存在”这个事实，用来判断
    change_kind=created / modified / deleted，而无需在执行前就对每个 dirty 路径
    做一次 sha256 or write_blob。
    """
    if not observer_capture:
        return {}
    paths = observer_capture.get("paths") or {}
    result = {}
    for raw_path in affected_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        marker = paths.get(normalized)
        if marker is None:
            continue
        result[normalized] = str(marker)
    return result


def _capture_before_file_states_for_paths(agent, raw_paths):
    """Capture restorable pre-tool bytes for a bounded set of paths."""
    store = agent.checkpoint_store
    workspace_root = agent.root
    states = {}
    for raw_path in raw_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
            resolved = resolve_workspace_relative_path(workspace_root, normalized)
        except ValueError:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        eligibility = snapshot_eligibility(workspace_root, normalized)
        if not eligibility.get("snapshot_eligible", False):
            continue
        try:
            data = resolved.read_bytes()
        except OSError:
            continue
        info = store.write_blob(data, "text")
        states[normalized] = {
            "before_blob_ref": info["blob_ref"],
            "before_hash": info["content_hash"],
        }
    return states


def _merge_before_snapshot(before_snapshot, before_file_states):
    merged = dict(before_snapshot or {})
    for path, state in (before_file_states or {}).items():
        before_hash = state.get("before_hash")
        if before_hash:
            merged[path] = before_hash
    return merged


def _fill_git_head_before_file_states(agent, raw_paths, before_file_states):
    states = dict(before_file_states or {})
    missing_paths = []
    for raw_path in raw_paths or []:
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ValueError:
            continue
        if normalized not in states:
            missing_paths.append(normalized)
    if not missing_paths:
        return states

    for path in missing_paths:
        proc = subprocess.run(
            ["git", "show", "HEAD:" + path],
            cwd=str(agent.root),
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        info = agent.checkpoint_store.write_blob(proc.stdout, "text")
        states[path] = {
            "before_blob_ref": info["blob_ref"],
            "before_hash": info["content_hash"],
        }
    return states


def _build_file_entries(agent, name, args, affected_paths, before_snapshot, before_file_states=None):
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

        # before_file_states 保存了执行前的真实字节；如果没有对应 blob，
        # 退回到 before_snapshot 里的 sha256，至少保留冲突判断线索。
        before_state = dict((before_file_states or {}).get(normalized) or {})
        if before_state:
            entry["before_blob_ref"] = str(before_state.get("before_blob_ref") or "")
            entry["before_hash"] = str(before_state.get("before_hash") or "")
        elif existed_before:
            entry["before_hash"] = str((before_snapshot or {}).get(normalized) or "")
        entries.append(entry)
    return entries
