"""Pure projections consumed by interactive resume and request assembly."""

from __future__ import annotations

from copy import deepcopy
import re

from pony.state.workflow import DEFAULT_WORKFLOW_MODE


MAX_PROMPT_HISTORY_ITEMS = 100
MAX_PROMPT_HISTORY_BYTES = 64 * 1024
MAX_PROMPT_HISTORY_ITEM_BYTES = 16 * 1024
_DISPLAY_PATH_RE = re.compile(r"(?<!\w)(?:/[\w./-]+|[A-Za-z]:[\\/][\w.\\/-]+)")


def _active_plan(session):
    plan = session.get("active_plan", {}) if isinstance(session, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    items = plan.get("items", [])
    return plan, items if isinstance(items, list) else []


def _display_text(value):
    return _DISPLAY_PATH_RE.sub("<path>", str(value or "").strip())[:300]


def build_workflow_request_metadata(session, *, visible_tool_count):
    """Return bounded workflow counts without copying model-visible Plan text."""
    _plan, items = _active_plan(session)
    completed = sum(
        isinstance(item, dict) and item.get("status") == "completed"
        for item in items
    )
    current = sum(
        isinstance(item, dict) and item.get("status") == "in_progress"
        for item in items
    )
    mode = session.get("workflow_mode") if isinstance(session, dict) else None
    return {
        "workflow_mode": mode if isinstance(mode, str) else DEFAULT_WORKFLOW_MODE,
        "plan_item_count": len(items),
        "plan_completed_count": completed,
        "plan_current_count": current,
        "visible_tool_count": max(0, int(visible_tool_count)),
    }


def build_resume_projection(session, *, redactor=None):
    """Combine current workflow/checkpoint facts without I/O or internal IDs."""
    session = session if isinstance(session, dict) else {}
    plan, items = _active_plan(session)
    current = next(
        (
            item
            for item in items
            if isinstance(item, dict) and item.get("status") == "in_progress"
        ),
        None,
    )
    completed_count = sum(
        isinstance(item, dict) and item.get("status") == "completed"
        for item in items
    )

    checkpoint_state = session.get("checkpoints", {})
    checkpoint_state = checkpoint_state if isinstance(checkpoint_state, dict) else {}
    checkpoint_items = checkpoint_state.get("items", {})
    checkpoint_items = checkpoint_items if isinstance(checkpoint_items, dict) else {}
    checkpoint = checkpoint_items.get(checkpoint_state.get("current_id"), {})
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}

    plan_goal = _display_text(plan.get("goal", ""))
    checkpoint_goal = _display_text(
        checkpoint.get("goal", checkpoint.get("current_goal", ""))
    )
    next_steps = checkpoint.get("next_steps")
    if not isinstance(next_steps, list):
        next_steps = [checkpoint.get("next_step", "")]
    next_steps = [_display_text(item) for item in next_steps if _display_text(item)]
    resume_state = session.get("resume_state", {})
    resume_state = resume_state if isinstance(resume_state, dict) else {}
    binding = session.get("provider_binding", {})
    binding = binding if isinstance(binding, dict) else {}

    projection = {
        "mode": str(session.get("workflow_mode", DEFAULT_WORKFLOW_MODE)),
        "goal": {
            "text": plan_goal or checkpoint_goal,
            "source": "plan" if plan_goal else "checkpoint" if checkpoint_goal else "",
        },
        "plan": {
            "source": "plan",
            "item_count": len(items),
            "completed_count": completed_count,
            "current_count": 1 if current is not None else 0,
        },
        "checkpoint": {
            "source": "checkpoint",
            "status": str(checkpoint.get("status", "") or ""),
            "blocker": _display_text(
                checkpoint.get("blocker", checkpoint.get("current_blocker", ""))
            ),
            "next_steps": next_steps,
        },
        "resume": {
            "source": "resume_state",
            "status": str(resume_state.get("status", "") or ""),
            "stale_path_count": len(resume_state.get("stale_paths", []) or []),
            "runtime_mismatch_count": len(
                resume_state.get("runtime_identity_mismatch_fields", []) or []
            ),
        },
        "model": {
            "source": "provider_binding",
            "protocol_family": str(binding.get("protocol_family", "") or ""),
            "model": str(binding.get("model", "") or ""),
        },
    }
    return redactor(deepcopy(projection)) if callable(redactor) else projection


def active_prompt_history(messages):
    """Return the newest complete top-level user prompts within fixed byte caps."""
    candidates = []
    for message in list(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            continue
        if size <= MAX_PROMPT_HISTORY_ITEM_BYTES:
            candidates.append((content, size))

    selected = []
    total = 0
    for content, size in reversed(candidates):
        if len(selected) >= MAX_PROMPT_HISTORY_ITEMS:
            break
        if total + size > MAX_PROMPT_HISTORY_BYTES:
            continue
        selected.append(content)
        total += size
    selected.reverse()
    return selected
