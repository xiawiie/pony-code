"""Per-turn relevance recall for pico memory (Task 23).

The renderer (:mod:`pico.context.renderer`) calls :func:`recall_for_turn`
once per turn to decide which memory notes should be surfaced to the
model as ``<pico:recalled_memory>`` blocks. Retrieval itself lives in
:mod:`pico.memory.retrieval`; this module adds the *contextual*
decisions on top:

**Four guards** — a recall candidate must clear all of them, or the note
is silently skipped:

1. **min_score** — BM25 score, normalized against the top hit of this
   query, must be ≥ ``RECALL_MIN_SCORE``. Weak keyword overlap should
   not push spurious notes into the prompt.
2. **max_tokens_per_note** — a note's rendered first paragraph is tail-
   clipped to ``RECALL_MAX_TOKENS_PER_NOTE`` tokens. Long notes never
   dominate the injection budget.
3. **tombstone** — retrieval already excludes notes whose ``name``
   appears in some other note's ``supersedes`` list (Task 20).
4. **recently-recalled** — a note recalled in any of the last
   ``RECALL_SKIP_RECENT_TURNS`` turns is skipped to avoid re-hammering
   the model with the same content each turn.

The rendered block carries provenance (``path=``, ``type=``, ``score=``,
``why=``) so the model can weight the memory appropriately.
"""

from __future__ import annotations

import logging

from pico.context.escaping import escape_pico_tags

logger = logging.getLogger("pico")

RECALL_TOP_K = 2
RECALL_MIN_SCORE = 0.3
RECALL_MAX_TOKENS_PER_NOTE = 400
RECALL_SKIP_RECENT_TURNS = 2


def _strip_frontmatter(text):
    """Return the body of a memory file, stripping a leading ``---`` block."""
    if not text.startswith("---\n"):
        return text
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        return text
    return rest[end + len("\n---\n") :]


def _first_paragraph(text):
    """Return the first non-empty paragraph of a note's body."""
    body = _strip_frontmatter(text)
    lines = body.splitlines()
    para = []
    started = False
    for line in lines:
        if not line.strip():
            if started:
                break
            continue
        started = True
        para.append(line)
    return "\n".join(para)


def _count_tokens(agent, text):
    counter = getattr(getattr(agent, "model_client", None), "count_tokens", None)
    if callable(counter):
        try:
            return int(counter(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _flatten_recent(session_recent, skip_turns):
    """Union of paths recalled in the last ``skip_turns`` turns."""
    out = set()
    for turn in (session_recent or [])[-skip_turns:]:
        for p in turn or []:
            out.add(p)
    return out


def _lookup_type(store, path):
    """Fish the frontmatter ``type`` out of a stored note (empty when absent)."""
    for entry in store.list():
        if entry.path == path:
            return (entry.frontmatter or {}).get("type", "") or ""
    return ""


def _why_terms(snippets, query_text, cap=3):
    """Extract the query terms that survived into the retrieved snippets.

    Serves as the ``why="..."`` provenance annotation on the rendered block
    — it lets the model see *which* query words matched, not just that they
    did. Falls back to ``"matched"`` when we can't identify overlap.
    """
    query_lower = (query_text or "").lower()
    terms = []
    for snip in snippets:
        for tok in snip.split():
            clean = tok.strip(".,:;!?()[]{}\"'")
            if clean and clean.lower() in query_lower and clean not in terms:
                terms.append(clean)
                if len(terms) >= cap:
                    break
        if len(terms) >= cap:
            break
    return ",".join(terms) if terms else "matched"


def recall_for_turn(agent, user_message, budget_tokens):
    """Return one or more ``<pico:recalled_memory>`` blocks, or ``None``.

    Callers pass ``budget_tokens`` for consistency with other injection
    sources; this function respects ``RECALL_MAX_TOKENS_PER_NOTE`` per
    note and drops candidates that fail any of the four guards.
    """
    retrieval = getattr(agent, "memory_retrieval", None)
    if retrieval is None:
        return None
    # Task B4: recall knobs overridable via pico.toml → agent.context_config["recall"].
    cfg_all = getattr(agent, "context_config", None)
    if not isinstance(cfg_all, dict):
        cfg_all = {}
    cfg = cfg_all.get("recall") if isinstance(cfg_all.get("recall"), dict) else {}
    min_score = float(cfg.get("min_score", RECALL_MIN_SCORE))
    top_k = int(cfg.get("top_k", RECALL_TOP_K))
    max_tokens_per_note = int(cfg.get("max_tokens_per_note", RECALL_MAX_TOKENS_PER_NOTE))
    skip_recent_turns = int(cfg.get("skip_recent_turns", RECALL_SKIP_RECENT_TURNS))
    task_summary = getattr(getattr(agent, "memory", None), "task_summary", "") or ""
    query = f"{user_message} {task_summary}".strip()
    if not query:
        return None

    # Ask for more than top_k so the four-guard filter has room to skip.
    hits = retrieval.search(query, limit=top_k * 3)
    if not hits:
        return None

    # Normalize score against this query's own maximum. Absolute BM25
    # scores are corpus-dependent; per-query normalization makes the
    # ``min_score`` threshold interpretable across different vocabularies.
    max_score = max(h.score for h in hits) or 1.0
    recent_skip = _flatten_recent(agent.session.get("recently_recalled"), skip_recent_turns)

    picked = []
    for h in hits:
        if len(picked) >= top_k:
            break
        norm_score = h.score / max_score
        if norm_score < min_score:
            continue
        if h.path in recent_skip:
            continue
        picked.append((h, norm_score))

    if not picked:
        return None

    store = agent.memory_store
    blocks = []
    picked_paths = []
    for hit, norm_score in picked:
        try:
            raw = store.read(hit.path)
        except (OSError, ValueError) as exc:
            logger.debug("recall: store.read(%s) failed: %s", hit.path, exc)
            continue
        para = _first_paragraph(raw)
        para_tokens = _count_tokens(agent, para)
        if para_tokens > max_tokens_per_note:
            char_budget = max_tokens_per_note * 4
            para = para[: max(3, char_budget) - 3] + "..." if char_budget > 3 else para[:char_budget]
        note_type = _lookup_type(store, hit.path)
        why = _why_terms(hit.snippets, query)
        block = (
            f'<pico:recalled_memory path="{hit.path}" type="{note_type}" '
            f'score="{norm_score:.2f}" why="{why}">\n'
            f"{escape_pico_tags(para)}\n"
            f"</pico:recalled_memory>"
        )
        blocks.append(block)
        picked_paths.append(hit.path)

    if not blocks:
        return None

    # Record the recall in the session so subsequent turns can honor the
    # recently-recalled guard. Bound the window at
    # skip_recent_turns + 1 entries to keep the session dict small.
    recent = list(agent.session.get("recently_recalled") or [])
    recent.append(picked_paths)
    agent.session["recently_recalled"] = recent[-(skip_recent_turns + 1) :]

    return "\n".join(blocks)
