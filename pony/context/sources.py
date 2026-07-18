"""Candidate generation for token-budgeted dynamic Context Sources."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from pathlib import Path

from pony.security import paths as security_paths
from pony.security import redaction as redaction
from pony.memory.recall import recall_candidates
from pony.memory.retrieval import Retrieval
from pony.agent.model_capabilities import TokenAccounting
from pony.state.workflow import DEFAULT_WORKFLOW_MODE

from .chunks import make_chunk


logger = logging.getLogger("pony")


def _sanitize_source_text(agent, text):
    """Redact and residual-check source text before any Provider tokenizer."""
    redaction_env = getattr(agent, "redaction_env", None)
    redaction_env = redaction_env if isinstance(redaction_env, Mapping) else None
    secret_env_names = getattr(agent, "secret_env_names", ())
    if not isinstance(secret_env_names, (list, tuple, set, frozenset)):
        secret_env_names = ()
    safe, _ = redaction.sanitize_provider_payload(
        str(text or ""),
        [],
        env=redaction_env,
        secret_env_names=secret_env_names,
    )
    return str(safe)


def _accounting(agent):
    value = getattr(agent, "token_accounting", None)
    return (
        value
        if isinstance(value, TokenAccounting)
        else TokenAccounting(
        getattr(getattr(agent, "model_client", None), "count_tokens", None)
    )
    )


def _clip_tokens(text, accounting, hard_cap):
    value = str(text or "").strip()
    if accounting.count_text(value) <= hard_cap:
        return value
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if accounting.count_text(value[:middle]) <= hard_cap:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip()


def _line_groups(text, accounting, target_tokens):
    groups = []
    current = []
    for line in str(text or "").splitlines():
        candidate = "\n".join([*current, line]).strip()
        if current and accounting.count_text(candidate) > target_tokens:
            groups.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append("\n".join(current).strip())
    return [group for group in groups if group]


def _workflow_context(agent, session):
    """Read the current-turn snapshot when policy freezes it, else Session state."""
    plan = getattr(agent, "current_workflow_plan", None)
    plan = plan() if callable(plan) else plan
    mode = getattr(agent, "current_workflow_mode", None)
    mode = mode() if callable(mode) else mode
    if isinstance(plan, dict) and "active_plan" in plan:
        mode = plan.get("workflow_mode", mode)
        plan = plan.get("active_plan")
    if not isinstance(plan, dict):
        plan = session.get("active_plan") if isinstance(session, dict) else None
    if not isinstance(mode, str):
        mode = session.get("workflow_mode") if isinstance(session, dict) else None
    return plan, mode


def workspace_state_chunks(agent, accounting):
    workspace = getattr(agent, "workspace", None)
    if workspace is None:
        return []
    try:
        display_root = str(
            getattr(workspace, "logical_root", "")
            or getattr(workspace, "repo_root", "")
        )
        display_cwd = display_root
        logical_root = str(getattr(workspace, "logical_root", "") or "")
        if logical_root:
            try:
                relative = Path(workspace.cwd).relative_to(Path(workspace.repo_root))
                if relative.parts:
                    display_cwd = (Path(display_root) / relative).as_posix()
            except ValueError:
                display_cwd = display_root
        identity = "\n".join(
            (
                "Workspace identity:",
                f"- cwd: {display_cwd}",
                f"- repo_root: {display_root}",
                f"- default_branch: {workspace.default_branch}",
            )
        )
        volatile = str(workspace.volatile_text() or "").strip()
        identity = _sanitize_source_text(agent, identity)
        volatile = _sanitize_source_text(agent, volatile)
    except redaction.SensitiveDataBlockedError:
        raise
    except Exception as exc:
        logger.debug("workspace_state source failed: %s", type(exc).__name__)
        return []
    chunks = [
        make_chunk(
            accounting,
            source="workspace_state",
            key="workspace-identity",
            text=identity,
            priority=0,
            provenance={"rank": 0},
        )
    ]
    chunks.extend(
        make_chunk(
            accounting,
            source="workspace_state",
            key=f"workspace-state-{index}",
            text=part,
            priority=0 if index == 0 else 1,
            provenance={"rank": index + 1},
        )
        for index, part in enumerate(_line_groups(volatile, accounting, 768))
    )
    return chunks


def project_structure_chunks(agent, accounting):
    repo_map = getattr(agent, "repo_map", None)
    tree = []
    stats = {}
    if repo_map is not None:
        try:
            repo_map.refresh_if_stale()
            tree = [
                entry
                for entry in repo_map.top_level_tree()
                if not security_paths.is_sensitive_path(str(entry.get("path", "")))
            ]
            stats = repo_map.language_stats() or {}
        except redaction.SensitiveDataBlockedError:
            raise
        except Exception as exc:
            logger.debug("project_structure source failed: %s", type(exc).__name__)

    chunks = []
    if tree:
        languages = ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
        lines = [f"Project structure (languages: {languages or '-'}):"]
        lines.extend(
            f"- {entry['path']}/ ({entry['file_count']} files)" for entry in tree
        )
        safe_structure = _sanitize_source_text(agent, "\n".join(lines))
        chunks.extend(
            make_chunk(
                accounting,
                source="project_structure",
                key=f"project-map-{index}",
                text=part,
                priority=1,
                provenance={"rank": index},
            )
            for index, part in enumerate(
                _line_groups(safe_structure, accounting, 1_024)
            )
        )

    workspace = getattr(agent, "workspace", None)
    docs = getattr(workspace, "project_docs", {}) if workspace is not None else {}
    docs = docs if isinstance(docs, dict) else {}
    rank = len(chunks)
    for path, snippet in docs.items():
        normalized = str(path).replace("\\", "/")
        if normalized == "AGENTS.md" or normalized.endswith("/AGENTS.md"):
            continue
        if security_paths.is_sensitive_path(normalized):
            continue
        for index, part in enumerate(
            _line_groups(
                _sanitize_source_text(
                    agent,
                    f"Project document {normalized}:\n{snippet}",
                ),
                accounting,
                1_024,
            )
        ):
            chunks.append(
                make_chunk(
                    accounting,
                    source="project_structure",
                    key=f"project-doc-{normalized}-{index}",
                    text=part,
                    priority=1,
                    provenance={"rank": rank, "path": normalized},
                )
            )
            rank += 1
    return chunks


def task_working_set_chunks(agent, accounting):
    memory = getattr(agent, "memory", None)
    session = getattr(agent, "session", {}) or {}
    checkpoint_state = (
        session.get("checkpoints", {}) if isinstance(session, dict) else {}
    )
    checkpoint_state = checkpoint_state if isinstance(checkpoint_state, dict) else {}
    checkpoint_id = str(checkpoint_state.get("current_id", "") or "")
    checkpoint_items = checkpoint_state.get("items", {})
    checkpoint_items = checkpoint_items if isinstance(checkpoint_items, dict) else {}
    checkpoint = checkpoint_items.get(checkpoint_id)
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    checkpoint_goal = str(
        checkpoint.get(
            "goal",
            checkpoint.get(
                "current_goal",
                getattr(memory, "task_summary", ""),
            ),
        )
        or ""
    ).strip()
    checkpoint_files = [
        str(item.get("path", ""))
        for item in checkpoint.get("key_files", [])
        if isinstance(item, dict) and str(item.get("path", ""))
    ]
    live_files = list(getattr(memory, "recent_files", []) or [])
    recent_files = list(dict.fromkeys([*live_files, *checkpoint_files]))
    chunks = []
    active_plan, mode = _workflow_context(agent, session)
    if isinstance(active_plan, dict) or isinstance(mode, str):
        active_plan = active_plan if isinstance(active_plan, dict) else {}
        items = active_plan.get("items", [])
        items = items if isinstance(items, list) else []
        completed = [
            item for item in items
            if isinstance(item, dict) and item.get("status") == "completed"
        ]
        current = [
            item for item in items
            if isinstance(item, dict) and item.get("status") == "in_progress"
        ]
        workflow_lines = [
            "Workflow state:",
            f"- Mode: {mode if isinstance(mode, str) else DEFAULT_WORKFLOW_MODE}",
        ]
        plan_goal = str(active_plan.get("goal", "") or "").strip()
        if plan_goal:
            workflow_lines.append(f"- Plan goal: {plan_goal}")
        if current:
            workflow_lines.append(
                f"- Current: {str(current[0].get('text', '')).strip()}"
            )
        workflow_lines.append(
            f"- Progress: {len(completed)}/{len(items)} completed; "
            f"{len(current)} current"
        )
        chunks.append(
            make_chunk(
                accounting,
                source="task_working_set",
                key="workflow-state",
                text=_sanitize_source_text(agent, "\n".join(workflow_lines)),
                priority=0,
                required=True,
                provenance={
                    "rank": 0,
                    "mode_source": "workflow_mode",
                    "plan_source": "active_plan",
                },
            )
        )
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("status") != "pending":
                continue
            chunks.append(
                make_chunk(
                    accounting,
                    source="task_working_set",
                    key=f"plan-pending-{index}",
                    text=_sanitize_source_text(
                        agent,
                        f"Pending plan item: {str(item.get('text', '')).strip()}",
                    ),
                    priority=1,
                    provenance={"rank": 10 + index},
                )
            )

    lines = ["Checkpoint state:"]
    if checkpoint_id:
        lines.append(f"- Checkpoint: {checkpoint_id}")
    if checkpoint_goal:
        lines.append(f"- Goal: {checkpoint_goal}")
    if checkpoint.get("status"):
        lines.append(f"- Status: {checkpoint['status']}")
    blocker = checkpoint.get("blocker", checkpoint.get("current_blocker", ""))
    if str(blocker or "").strip():
        lines.append(f"- Blocker: {str(blocker).strip()}")
    next_steps = checkpoint.get("next_steps")
    if not isinstance(next_steps, list):
        next_steps = [checkpoint.get("next_step", "")]
    next_steps = [str(item).strip() for item in next_steps if str(item).strip()]
    if next_steps:
        lines.append("- Next steps: " + " | ".join(next_steps))
    if len(lines) > 1:
        safe_checkpoint = _sanitize_source_text(agent, "\n".join(lines))
        chunks.append(
            make_chunk(
                accounting,
                source="task_working_set",
                key="checkpoint-state",
                text=_clip_tokens(safe_checkpoint, accounting, 768),
                priority=0,
                required=bool(checkpoint_id),
                provenance={"rank": 1},
            )
        )

    detail_lines = []
    if recent_files:
        detail_lines.append("Recent files: " + ", ".join(recent_files))
    memory_state = session.get("memory", {}) if isinstance(session, dict) else {}
    summaries = (
        memory_state.get("file_summaries", {}) if isinstance(memory_state, dict) else {}
    )
    for path in recent_files:
        if security_paths.is_sensitive_path(str(path)):
            continue
        checkpoint_item = next(
            (
                item
                for item in checkpoint.get("key_files", [])
                if isinstance(item, dict) and item.get("path") == path
            ),
            {},
        )
        value = checkpoint_item.get("summary") or summaries.get(path)
        summary = value.get("summary", "") if isinstance(value, dict) else value
        if str(summary or "").strip():
            detail_lines.append(f"- {path}: {str(summary).strip()}")
    safe_details = _sanitize_source_text(agent, "\n".join(detail_lines))
    chunks.extend(
        make_chunk(
            accounting,
            source="task_working_set",
            key=f"working-details-{index}",
            text=part,
            priority=1,
            provenance={"rank": 30 + index},
        )
        for index, part in enumerate(_line_groups(safe_details, accounting, 1_024))
    )
    return [chunk for chunk in chunks if chunk is not None]


def memory_index_chunks(agent, accounting, memory_snapshot):
    if memory_snapshot is None:
        return []
    chunks = []
    for index, document in enumerate(memory_snapshot.raw_documents):
        if security_paths.is_sensitive_path(str(document.path)):
            continue
        description = str(document.first_line or "").strip()
        text = f"- {document.path} ({document.size_chars} chars)"
        if description:
            text += f": {description}"
        text = _sanitize_source_text(agent, text)
        chunks.append(
            make_chunk(
                accounting,
                source="memory_index",
                key=document.path,
                text=text,
                priority=2,
                provenance={"rank": index, "path": document.path},
            )
        )
    return chunks


def recalled_memory_chunks(agent, accounting, user_message, memory_snapshot):
    if memory_snapshot is None:
        return []
    try:
        candidates = recall_candidates(
            agent,
            user_message,
            snapshot=memory_snapshot,
        )
    except redaction.SensitiveDataBlockedError:
        raise
    except Exception as exc:
        session = getattr(agent, "session", None)
        if isinstance(session, dict):
            counters = session.setdefault("_recall_errors", {"count": 0, "last": ""})
            counters["count"] = int(counters.get("count", 0)) + 1
            counters["last"] = f"{type(exc).__name__}: {exc}"[:200]
        logger.debug("recalled_memory source failed: %s", type(exc).__name__)
        return []
    return [
        make_chunk(
            accounting,
            source="recalled_memory",
            key=candidate.path,
            text=_sanitize_source_text(agent, candidate.text),
            priority=1,
            provenance={
                "rank": candidate.rank,
                "path": candidate.path,
                "score": candidate.score,
                "type": candidate.note_type,
                "why": candidate.why,
            },
        )
        for candidate in candidates
    ]


def recovery_state_chunks(agent, accounting):
    resume_state = getattr(agent, "resume_state", None)
    resume_state = resume_state if isinstance(resume_state, dict) else {}
    status = str(resume_state.get("status", "") or "")
    sandbox = getattr(agent, "sandbox_session", None)
    manifest = getattr(sandbox, "manifest", {}) if sandbox is not None else {}
    sandbox_state = (
        str(manifest.get("state", "") or "") if isinstance(manifest, dict) else ""
    )
    noteworthy = status in {"partial-stale", "workspace-mismatch"} or sandbox_state in {
        "pending_review",
        "review_required",
    }
    if not noteworthy:
        return []
    lines = ["Recovery state:", f"- Resume status: {status or '-'}"]
    if resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(resume_state["stale_paths"]))
    if resume_state.get("runtime_identity_mismatch_fields"):
        lines.append(
            "- Runtime mismatch: "
            + ", ".join(resume_state["runtime_identity_mismatch_fields"])
        )
    if sandbox_state:
        lines.append(f"- Sandbox state: {sandbox_state}")
    chunk = make_chunk(
        accounting,
        source="recovery_state",
        key="active-recovery",
        text=_sanitize_source_text(agent, "\n".join(lines)),
        priority=0,
        required=True,
        provenance={"status": status, "sandbox_state": sandbox_state},
    )
    return [chunk]


def build_source_chunks(agent, user_message, *, memory_snapshot=None):
    accounting = _accounting(agent)
    builders = (
        lambda: recovery_state_chunks(agent, accounting),
        lambda: workspace_state_chunks(agent, accounting),
        lambda: task_working_set_chunks(agent, accounting),
        lambda: recalled_memory_chunks(
            agent,
            accounting,
            user_message,
            memory_snapshot,
        ),
        lambda: project_structure_chunks(agent, accounting),
        lambda: memory_index_chunks(agent, accounting, memory_snapshot),
    )
    chunks = []
    for builder in builders:
        try:
            chunks.extend(chunk for chunk in builder() if chunk is not None)
        except redaction.SensitiveDataBlockedError:
            raise
        except Exception as exc:
            logger.debug("context source failed: %s", type(exc).__name__)
    return chunks


# Compatibility renderers for callers outside the new allocator. They enforce
# token caps with the shared accounting and are not used by production assembly.
def _compat_render(agent, chunks, budget_tokens):
    text = "\n".join(chunk.text for chunk in chunks if chunk is not None)
    return _clip_tokens(text, _accounting(agent), int(budget_tokens)) or None


def render_workspace_state(agent, budget_tokens):
    return _compat_render(
        agent,
        workspace_state_chunks(agent, _accounting(agent)),
        budget_tokens,
    )


def render_project_structure(agent, budget_tokens):
    return _compat_render(
        agent,
        project_structure_chunks(agent, _accounting(agent)),
        budget_tokens,
    )


def render_checkpoint(agent, budget_tokens):
    renderer = getattr(agent, "render_checkpoint_text", None)
    if not callable(renderer):
        return None
    try:
        text = str(renderer() or "").strip()
    except redaction.SensitiveDataBlockedError:
        raise
    except Exception:
        return None
    safe = _sanitize_source_text(agent, text)
    return _clip_tokens(safe, _accounting(agent), int(budget_tokens)) or None


def render_memory_index(agent, budget_tokens):
    retrieval = getattr(agent, "memory_retrieval", None)
    if retrieval is None and getattr(agent, "memory_store", None) is not None:
        retrieval = Retrieval(agent.memory_store)
    snapshot = retrieval.snapshot() if retrieval is not None else None
    index = _compat_render(
        agent,
        memory_index_chunks(agent, _accounting(agent), snapshot),
        budget_tokens,
    )
    return index


def render_recalled_memory(agent, budget_tokens, user_message=""):
    retrieval = getattr(agent, "memory_retrieval", None)
    snapshot = retrieval.snapshot() if retrieval is not None else None
    return _compat_render(
        agent,
        recalled_memory_chunks(
            agent,
            _accounting(agent),
            user_message,
            snapshot,
        ),
        budget_tokens,
    )
