"""Checkpoint and resume-state helpers."""

import uuid

import pony.memory.service as memorylib
from pony.workspace.context import clip, now

CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"

RUNTIME_IDENTITY_KEYS = (
    "cwd",
    "model",
    "model_client",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_output_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
)


def current_runtime_identity(agent):
    # Text-only transports are explicitly wrapped; checkpoint the transport
    # identity so resumes do not depend on the adapter class name.
    underlying_client = getattr(agent.model_client, "_inner", agent.model_client)
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.root),
        "model": str(getattr(underlying_client, "model", "")),
        "model_client": underlying_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_output_tokens": int(agent.max_output_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(
            getattr(agent, "prefix_state", None),
            "workspace_fingerprint",
            agent.workspace.fingerprint(),
        ),
        "tool_signature": agent.tool_signature(),
    }


def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]


def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)


def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        for item in checkpoint.get("key_files", []):
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            expected = item.get("freshness")
            current = memorylib.file_freshness(path, agent.root)
            if expected != current and path not in stale_paths:
                stale_paths.append(path)
        saved_identity = dict(
            checkpoint.get("runtime_identity", {})
            or agent.session.get("runtime_identity", {})
            or {}
        )
        current_identity = current_runtime_identity(agent)
        for key in RUNTIME_IDENTITY_KEYS:
            if key not in saved_identity:
                continue
            if saved_identity.get(key) != current_identity.get(key):
                mismatch_fields.append(key)
        mismatch_fields.sort()
        if stale_paths:
            status = CHECKPOINT_PARTIAL_STALE_STATUS
        elif mismatch_fields:
            status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
        else:
            status = CHECKPOINT_FULL_VALID_STATUS

    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state


def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    if not checkpoint:
        return ""
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        f"- Status: {checkpoint.get('status', '-') or '-'}",
        f"- Current goal: {checkpoint.get('goal', checkpoint.get('current_goal', '-')) or '-'}",
        f"- Current blocker: {checkpoint.get('blocker', checkpoint.get('current_blocker', '-')) or '-'}",
    ]
    next_steps = checkpoint.get("next_steps")
    if not isinstance(next_steps, list):
        next_steps = [checkpoint.get("next_step", "")]
    rendered_next_steps = " | ".join(
        str(item) for item in next_steps if str(item).strip()
    )
    lines.append(f"- Next steps: {rendered_next_steps or '-'}")
    key_files = [
        str(item.get("path", "")).strip()
        for item in checkpoint.get("key_files", [])
        if str(item.get("path", "")).strip()
    ]
    lines.append(f"- Key files: {', '.join(key_files) or '-'}")
    if checkpoint.get("completed"):
        lines.append(
            "- Completed: "
            + " | ".join(str(item) for item in checkpoint.get("completed", []))
        )
    if checkpoint.get("in_progress"):
        lines.append(
            "- In progress: "
            + " | ".join(str(item) for item in checkpoint.get("in_progress", []))
        )
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    summary = str(checkpoint.get("summary", "")).strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    return "\n".join(lines)


def infer_next_step(task_state):
    if task_state.status == "completed":
        return "No next step recorded."
    if task_state.stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if task_state.last_tool:
        return f"Decide the next action after {task_state.last_tool}."
    return "Continue the task from the latest checkpoint."


def _context_usage(agent):
    metadata = getattr(agent, "last_request_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    breakdown = metadata.get("context_breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    budget = breakdown.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    compaction = breakdown.get("compaction")
    compaction = compaction if isinstance(compaction, dict) else {}
    sources = breakdown.get("sources")
    sources = sources if isinstance(sources, list) else []
    return {
        "input_tokens": int(budget.get("used", 0) or 0),
        "input_limit": int(budget.get("input_limit", 0) or 0),
        "remaining_tokens": int(budget.get("remaining", 0) or 0),
        "token_count_mode": str(metadata.get("token_count_mode", "") or ""),
        "compaction_entry_id": str(compaction.get("entry_id", "") or ""),
        "summary_tokens": int(compaction.get("summary_tokens", 0) or 0),
        "tail_tokens": int(compaction.get("tail_tokens", 0) or 0),
        "source_tokens": {
            str(item.get("name", "")): int(item.get("actual_tokens", 0) or 0)
            for item in sources
            if isinstance(item, dict) and str(item.get("name", ""))
        },
    }


def _worktree_identity_digest(agent):
    try:
        tree = agent.session_store.load_tree(agent.session["id"])
    except (OSError, RuntimeError, ValueError):
        return ""
    identity = tree.header.get("worktree_identity", {})
    return str(identity.get("digest", "") or "") if isinstance(identity, dict) else ""


def _safe_paths(agent, values):
    paths = []
    for value in values or []:
        path = str(agent.redact_text(value) or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def create_checkpoint(
    agent,
    task_state,
    user_message,
    trigger,
    *,
    modified_files=(),
    label="",
):
    state = checkpoint_state(agent)
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
    safe_user_message = agent.redact_text(user_message)
    safe_final_answer = str(agent.redact_text(task_state.final_answer) or "").strip()
    modified_files = _safe_paths(agent, modified_files)
    recent_files = _safe_paths(agent, agent.memory.recent_files)
    read_files = [path for path in recent_files if path not in modified_files]
    all_key_paths = list(dict.fromkeys([*recent_files, *modified_files]))
    key_files = []
    freshness = {}
    summaries = agent.session.get("memory", {}).get("file_summaries", {})
    summaries = summaries if isinstance(summaries, dict) else {}
    for path in all_key_paths:
        file_freshness = memorylib.file_freshness(path, agent.root)
        freshness[path] = file_freshness
        summary_value = summaries.get(path)
        summary = (
            summary_value.get("summary", "")
            if isinstance(summary_value, dict)
            else summary_value
        )
        key_files.append(
            {
                "path": path,
                "freshness": file_freshness,
                "summary": str(summary or "").strip(),
            }
        )
    goal = str(safe_user_message).strip()
    blocker = (
        ""
        if str(task_state.stop_reason or "") in ("", "final_answer_returned")
        else str(task_state.stop_reason)
    )
    next_step = infer_next_step(task_state)
    completed = [safe_final_answer] if safe_final_answer else []
    in_progress = [] if task_state.status == "completed" else [goal]
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        "created_at": now(),
        "goal": goal,
        "status": str(task_state.status or ""),
        "completed": completed,
        "in_progress": in_progress,
        "blocker": blocker,
        "next_steps": [next_step] if next_step else [],
        "key_files": key_files,
        "read_files": read_files,
        "modified_files": modified_files,
        "workspace_checkpoint_id": str(
            getattr(task_state, "recovery_checkpoint_id", "") or ""
        ),
        "worktree_identity_digest": _worktree_identity_digest(agent),
        "context_usage": _context_usage(agent),
        "label": str(agent.redact_text(label) or "").strip(),
        "trigger": str(trigger or ""),
        "freshness": freshness,
        "summary": f"{trigger}: {clip(goal, 120)}",
        "runtime_identity": current_runtime_identity(agent),
    }
    state["items"][checkpoint_id] = checkpoint
    state["current_id"] = checkpoint_id
    task_state.checkpoint_id = checkpoint_id
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]
    agent.session_store.append_task_checkpoint(
        agent.session["id"],
        checkpoint,
    )
    agent.session_path = agent.session_store.path_for(agent.session["id"])
    derived_files = [item["path"] for item in key_files if item.get("path")][:8]
    agent.memory.set_task_summary(goal)
    agent.memory.recent_files = list(derived_files)
    agent._sync_working_memory()
    agent.session["memory"] = {
        "file_summaries": {
            item["path"]: str(item.get("summary", "") or "")
            for item in key_files
            if item.get("path") and str(item.get("summary", "") or "").strip()
        }
    }
    agent.session["recently_recalled"] = []
    return checkpoint


def create_manual_checkpoint(agent, label=""):
    current = current_checkpoint(agent) or {}
    goal = (
        str(label or "").strip()
        or str(current.get("goal", current.get("current_goal", "")) or "").strip()
        or str(getattr(agent.memory, "task_summary", "") or "").strip()
        or "Manual checkpoint"
    )

    class _ManualTask:
        status = "in_progress"
        stop_reason = ""
        final_answer = ""
        last_tool = ""
        checkpoint_id = ""
        recovery_checkpoint_id = str(
            agent.session.get("recovery", {}).get("current_checkpoint_id", "") or ""
        )

    return create_checkpoint(
        agent,
        _ManualTask(),
        goal,
        "manual",
        label=label,
    )
