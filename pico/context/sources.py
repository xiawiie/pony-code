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
    except Exception:
        return None
    if not text:
        return None
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_memory_index(agent, budget_tokens):
    """List of memory files (path, size, first line) available to the turn."""
    store = getattr(agent, "memory_store", None)
    if store is None:
        return None
    try:
        entries = store.list()
    except Exception:
        return None
    if not entries:
        return None
    lines = ["Memory files:"]
    for e in entries:
        first = getattr(e, "first_line", "") or ""
        first = first[:80]
        lines.append(f"- {e.path} ({e.size_chars} chars) {first}")
    text = "\n".join(lines)
    return _tail_clip(text, _budget_to_chars(budget_tokens))


def render_project_structure(agent, budget_tokens):
    """Top-level tree + per-language file counts derived from RepoMap."""
    repo_map = getattr(agent, "repo_map", None)
    if repo_map is None:
        return None
    try:
        repo_map.refresh_if_stale()
    except Exception:
        return None
    try:
        tree = repo_map.top_level_tree()
    except Exception:
        return None
    if not tree:
        return None
    try:
        stats = repo_map.language_stats() or {}
    except Exception:
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
    except Exception:
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
        session = getattr(agent, "session", None)
        if isinstance(session, dict):
            counters = session.setdefault("_recall_errors", {"count": 0, "last": ""})
            counters["count"] = int(counters.get("count", 0)) + 1
            counters["last"] = f"{type(exc).__name__}: {exc}"[:200]
        return None
