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
        before_existed = set(before_snapshot.keys())
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
                # 存在性来自 observer；HEAD 只对 observer 看不到（=clean tracked）的路径可靠。
                before_existed = _paths_present_in_observer(observer_before, affected_paths)
                dirty_before = set(before_existed)
                head_candidates = [p for p in affected_paths if p not in dirty_before]
                before_file_states = _fill_git_head_before_file_states(
                    agent, head_candidates, before_file_states
                )
                # HEAD fallback 覆盖 clean tracked file 的存在性；命中即视为“执行前存在”。
                for path in before_file_states.keys():
                    before_existed.add(path)
                before_snapshot = _merge_before_snapshot({}, before_file_states)
                shell_side_effects = _summaries_for_paths(agent, affected_paths, before_existed)
                diff_summary = shell_side_effects
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
            metadata = _finalize_tool_side_effects(
                agent=agent,
                name=name,
                args=args,
                pending_record=pending_record,
                affected_paths=affected_paths,
                before_snapshot=before_snapshot,
                before_file_states=before_file_states,
                before_existed=before_existed,
                tool_status=tool_status,
                tool_error_code=tool_error_code,
                security_event_type="",
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                workspace_changed=workspace_changed,
                diff_summary=diff_summary,
                shell_side_effects=shell_side_effects if name == "run_shell" else [],
                command_risk=command_risk,
                command_approval=command_approval,
                content=content,
            )
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
                    before_existed = _paths_present_in_observer(observer_before, affected_paths)
                    dirty_before = set(before_existed)
                    head_candidates = [p for p in affected_paths if p not in dirty_before]
                    before_file_states = _fill_git_head_before_file_states(
                        agent, head_candidates, before_file_states
                    )
                    for path in before_file_states.keys():
                        before_existed.add(path)
                    before_snapshot = _merge_before_snapshot({}, before_file_states)
                    shell_side_effects = _summaries_for_paths(agent, affected_paths, before_existed)
                    diff_summary = shell_side_effects
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
            tool_status = "partial_success" if workspace_changed else "error"
            tool_error_code = "tool_partial_success" if workspace_changed else "tool_failed"
            metadata = _finalize_tool_side_effects(
                agent=agent,
                name=name,
                args=args,
                pending_record=pending_record,
                affected_paths=affected_paths,
                before_snapshot=before_snapshot,
                before_file_states=before_file_states,
                before_existed=before_existed,
                tool_status=tool_status,
                tool_error_code=tool_error_code,
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                workspace_changed=workspace_changed,
                diff_summary=diff_summary,
                shell_side_effects=shell_side_effects if name == "run_shell" else [],
                command_risk=command_risk,
                command_approval=command_approval,
                content="",
                error_message=str(exc),
            )
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)


def _add_command_policy(metadata, command_risk, command_approval):
    if command_risk:
        metadata["command_risk_class"] = command_risk
    if command_approval:
        metadata["command_approval"] = dict(command_approval)
    return metadata


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
    read_only,
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
        read_only=read_only,
        affected_paths=affected_paths,
        workspace_changed=workspace_changed,
        workspace_fingerprint=agent.workspace.fingerprint(),
        diff_summary=diff_summary,
    )
    metadata = _add_command_policy(metadata, command_risk, command_approval)

    file_entries = _build_file_entries(
        agent,
        name,
        args,
        affected_paths,
        before_snapshot,
        before_file_states,
        before_existed,
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
    agent.record_process_note_for_tool(name, metadata)
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
        if normalized in states:
            continue
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


def _build_file_entries(agent, name, args, affected_paths, before_snapshot, before_file_states=None, before_existed=None):
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
        eligibility = snapshot_eligibility(workspace_root, normalized)
        try:
            resolved = resolve_workspace_relative_path(workspace_root, normalized)
        except ValueError:
            continue

        if before_existed is not None:
            existed_before = normalized in before_existed
        else:
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
        entries.append(entry)
    return entries
