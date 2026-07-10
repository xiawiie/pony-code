"""Injection source renderers.

Each source produces the raw (pre-escaping) text for one
``<system-reminder><pico:name>...</pico:name></system-reminder>`` block
in the current user message. A source returns ``None`` when it has
nothing to contribute — the renderer then omits its wrapper entirely.

All renderers share the same signature ``(agent, budget_tokens) -> str | None``.
Token budget is enforced by a coarse ``1 token ≈ 4 char`` mapping; sources
that overflow their budget are tail-clipped with a trailing ellipsis.
Downstream renderer (Task 13) counts precise tokens if the model client
provides ``count_tokens`` — this per-source clip is a defense-in-depth
floor, not the primary budget enforcement.

Each renderer is defensive against attribute-missing (agent without a
`memory_store` / `repo_map`), Falsy returns, and exceptions raised by
the underlying subsystem — a broken source must never block the turn.
"""

from __future__ import annotations

import logging

from pico import security as securitylib

logger = logging.getLogger("pico")


def _tail_clip(text, char_budget):
    if len(text) <= char_budget:
        return text
    if char_budget <= 3:
        return text[:char_budget]
    return text[: char_budget - 3] + "..."


def _budget_to_chars(budget_tokens):
    # Conservative 1 token ≈ 4 char. The renderer replaces this with the
    # model-client's real tokenizer when available.
    return max(0, int(budget_tokens) * 4)


def render_workspace_state(agent, budget_tokens):
    """git branch / status / recent commits — the live workspace snapshot."""
    try:
        text = str(agent.workspace.volatile_text() or "").strip()
    except Exception as exc:
        logger.debug("workspace_state source failed: %s", type(exc).__name__)
        return None
    if not text:
        return None
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_memory_index(agent, budget_tokens):
    """List durable memory files and summaries of recently worked files."""
    entries = []
    store = getattr(agent, "memory_store", None)
    if store is not None:
        try:
            entries = list(store.list() or [])
        except Exception as exc:
            logger.debug("memory_index source failed: %s", type(exc).__name__)

    durable_lines = []
    if entries:
        durable_lines.append("Memory files:")
        for entry in entries:
            first = (getattr(entry, "first_line", "") or "")[:80]
            durable_lines.append(f"- {entry.path} ({entry.size_chars} chars) {first}")

    memory_enabled = True
    feature_enabled = getattr(agent, "feature_enabled", None)
    if callable(feature_enabled):
        memory_enabled = bool(feature_enabled("memory"))
    if memory_enabled:
        recent_files = list(getattr(getattr(agent, "memory", None), "recent_files", []) or [])
        session = getattr(agent, "session", {}) or {}
        memory_state = session.get("memory", {}) if isinstance(session, dict) else {}
        summaries = memory_state.get("file_summaries", {}) if isinstance(memory_state, dict) else {}
        working_lines = []
        for path in recent_files:
            value = summaries.get(path)
            summary = value.get("summary", "") if isinstance(value, dict) else value
            summary = str(summary or "").strip()
            if summary:
                working_lines.append(f"{path} -> {summary}")
        if working_lines:
            working_text = "\n".join(["Recent working file summaries:", *working_lines])
        else:
            working_text = ""
    else:
        working_text = ""

    durable_text = "\n".join(durable_lines)
    if not durable_text and not working_text:
        return None
    char_budget = _budget_to_chars(budget_tokens)
    if not working_text:
        return _tail_clip(durable_text, char_budget)
    if len(working_text) >= char_budget:
        return _tail_clip(working_text, char_budget)
    durable_budget = char_budget - len(working_text) - 2
    if durable_text and durable_budget > 3:
        return _tail_clip(durable_text, durable_budget) + "\n\n" + working_text
    return working_text


def render_project_structure(agent, budget_tokens):
    """Top-level tree + per-language file counts derived from RepoMap."""
    repo_map = getattr(agent, "repo_map", None)
    if repo_map is None:
        return None
    try:
        repo_map.refresh_if_stale()
    except Exception as exc:
        logger.debug(
            "project_structure source failed (refresh): %s",
            type(exc).__name__,
        )
        return None
    try:
        tree = repo_map.top_level_tree()
    except Exception as exc:
        logger.debug("project_structure source failed (tree): %s", type(exc).__name__)
        return None
    if not tree:
        return None
    try:
        stats = repo_map.language_stats() or {}
    except Exception as exc:
        logger.debug("project_structure source failed (stats): %s", type(exc).__name__)
        stats = {}
    lang_str = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
    lines = [f"Project (languages: {lang_str}):"]
    for entry in tree:
        lines.append(f"- {entry['path']}/  ({entry['file_count']} files)")
    text = "\n".join(lines)
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_checkpoint(agent, budget_tokens):
    """Resume-summary checkpoint text (blank on non-resume turns)."""
    renderer = getattr(agent, "render_checkpoint_text", None)
    if renderer is None:
        return None
    try:
        text = str(renderer() or "").strip()
    except Exception as exc:
        logger.debug("checkpoint source failed: %s", type(exc).__name__)
        return None
    if not text:
        return None
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_recalled_memory(agent, budget_tokens, user_message=""):
    """Task 24: query-driven recall block.

    Unlike the other sources, ``recalled_memory`` needs the current
    user message to score relevance — so this renderer accepts an extra
    ``user_message`` argument. The heavy lifting (four guards, provenance,
    recently-recalled bookkeeping) lives in :func:`recall_for_turn`.

    Task C2: exceptions raised by ``recall_for_turn`` are recorded into
    ``session["_recall_errors"]`` (``{count, last}``) so operators can spot
    silent failures via ``build_v2`` metadata. Behavior unchanged — the
    turn still proceeds with ``recalled_memory`` omitted.
    """
    # Local import to avoid a hard cycle with the memory subsystem.
    from pico.memory.recall import recall_for_turn

    try:
        return recall_for_turn(agent, user_message, budget_tokens)
    except Exception as exc:
        logger.debug("recalled_memory source failed: %s", type(exc).__name__)
        session = getattr(agent, "session", None)
        if isinstance(session, dict):
            counters = session.setdefault("_recall_errors", {"count": 0, "last": ""})
            counters["count"] = int(counters.get("count", 0)) + 1
            redactor = getattr(agent, "redact_text", None)
            safe_message = (
                redactor(str(exc))
                if callable(redactor)
                else securitylib.redact_text(str(exc))
            )
            counters["last"] = f"{type(exc).__name__}: {safe_message}"[:200]
        return None
