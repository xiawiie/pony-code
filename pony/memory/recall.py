"""Per-turn lexical Memory recall over one shared query snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape as _html_escape
import re

from pony.security import redaction as securitylib
from pony.context.escaping import escape_pony_tags
from pony.agent.model_capabilities import TokenAccounting

from .retrieval import MemoryQuerySnapshot, tokenize


RECALL_TOP_K = 6
RECALL_MIN_SCORE = 0.3
RECALL_MAX_TOKENS_PER_NOTE = 1_024
RECALL_SKIP_RECENT_TURNS = 2


@dataclass(frozen=True)
class RecallCandidate:
    path: str
    text: str
    tokens: int
    score: float
    rank: int
    note_type: str
    why: str


def _strip_frontmatter(text):
    if not text.startswith("---\n"):
        return text
    rest = text[4:]
    end = rest.find("\n---\n")
    return text if end == -1 else rest[end + len("\n---\n") :]


def _paragraphs(text):
    body = _strip_frontmatter(text)
    values = [value.strip() for value in re.split(r"\n\s*\n", body) if value.strip()]
    return values or ([body.strip()] if body.strip() else [])


def _accounting(agent):
    value = getattr(agent, "token_accounting", None)
    return (
        value
        if isinstance(value, TokenAccounting)
        else TokenAccounting(
        getattr(getattr(agent, "model_client", None), "count_tokens", None)
    )
    )


def _sanitize_before_tokens(agent, text):
    safe, _ = securitylib.sanitize_provider_payload(
        str(text or ""),
        [],
        env=getattr(agent, "redaction_env", None),
        secret_env_names=getattr(agent, "secret_env_names", ()),
    )
    return str(safe)


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


def _best_passage(raw, query):
    passages = _paragraphs(raw)
    if not passages:
        return ""
    terms = set(tokenize(query))

    def score(passage):
        lowered = passage.casefold()
        passage_tokens = set(tokenize(passage))
        overlap = len(terms & passage_tokens)
        literal = sum(term in lowered for term in terms)
        return overlap * 2 + literal

    return max(enumerate(passages), key=lambda item: (score(item[1]), -item[0]))[1]


def _flatten_recent(session_recent, skip_turns):
    return {
        path for turn in (session_recent or [])[-skip_turns:] for path in (turn or [])
    }


def _why_terms(snippets, query_text, cap=3):
    query_tokens = set(tokenize(query_text))
    terms = []
    for snippet in snippets:
        for token in tokenize(snippet):
            if token in query_tokens and token not in terms:
                terms.append(token)
                if len(terms) >= cap:
                    return ",".join(terms)
    return ",".join(terms) if terms else "matched"


def _escape_attribute(value):
    return _html_escape(str(value), quote=True)


def _recall_config(agent):
    all_config = getattr(agent, "context_config", None)
    all_config = all_config if isinstance(all_config, dict) else {}
    config = all_config.get("recall")
    config = config if isinstance(config, dict) else {}
    return {
        "min_score": float(config.get("min_score", RECALL_MIN_SCORE)),
        "top_k": int(config.get("top_k", RECALL_TOP_K)),
        "max_tokens_per_note": int(
            config.get("max_tokens_per_note", RECALL_MAX_TOKENS_PER_NOTE)
        ),
        "skip_recent_turns": int(
            config.get("skip_recent_turns", RECALL_SKIP_RECENT_TURNS)
        ),
    }


def build_recall_query(agent, user_message):
    goal = str(getattr(getattr(agent, "memory", None), "task_summary", "") or "")
    recent_files = list(
        getattr(getattr(agent, "memory", None), "recent_files", []) or []
    )
    return " ".join(
        part for part in (str(user_message or ""), goal, " ".join(recent_files)) if part
    ).strip()


def recall_candidates(agent, user_message, *, snapshot=None):
    retrieval = getattr(agent, "memory_retrieval", None)
    if retrieval is None:
        return []
    if snapshot is None:
        snapshot = retrieval.snapshot()
    if not isinstance(snapshot, MemoryQuerySnapshot):
        raise TypeError("snapshot must be a MemoryQuerySnapshot")
    config = _recall_config(agent)
    query = build_recall_query(agent, user_message)
    if not query:
        return []
    hits, documents = retrieval.search_snapshot(
        snapshot,
        query,
        limit=max(1, config["top_k"] * 3),
    )
    if not hits:
        return []
    max_score = max(hit.score for hit in hits) or 1.0
    session = getattr(agent, "session", {}) or {}
    recent_skip = _flatten_recent(
        session.get("recently_recalled"),
        config["skip_recent_turns"],
    )
    accounting = _accounting(agent)
    selected = []
    for hit in hits:
        if len(selected) >= config["top_k"]:
            break
        normalized = hit.score / max_score
        if normalized < config["min_score"] or hit.path in recent_skip:
            continue
        document = documents.get(hit.path)
        if document is None:
            continue
        passage = _clip_tokens(
            _sanitize_before_tokens(agent, _best_passage(document.raw, query)),
            accounting,
            config["max_tokens_per_note"],
        )
        if not passage:
            continue
        safe_path = _sanitize_before_tokens(agent, hit.path)
        note_type = _sanitize_before_tokens(
            agent,
            str((document.frontmatter or {}).get("type", "") or ""),
        )
        why = _sanitize_before_tokens(agent, _why_terms(hit.snippets, query))
        block = (
            f'<pony:recalled_memory path="{_escape_attribute(safe_path)}" '
            f'type="{_escape_attribute(note_type)}" '
            f'score="{normalized:.2f}" why="{_escape_attribute(why)}">\n'
            f"{escape_pony_tags(passage)}\n"
            "</pony:recalled_memory>"
        )
        selected.append(
            RecallCandidate(
                path=safe_path,
                text=block,
                tokens=accounting.count_text(block),
                score=normalized,
                rank=len(selected),
                note_type=note_type,
                why=why,
            )
        )
    return selected


def recall_for_turn(agent, user_message, budget_tokens, *, snapshot=None):
    """Compatibility renderer; new Context assembly uses candidates directly."""
    budget = max(0, int(budget_tokens))
    selected = []
    used = 0
    for candidate in recall_candidates(agent, user_message, snapshot=snapshot):
        if used + candidate.tokens > budget:
            continue
        selected.append(candidate)
        used += candidate.tokens
    if not selected:
        return None
    config = _recall_config(agent)
    session = getattr(agent, "session", None)
    if isinstance(session, dict):
        recent = list(session.get("recently_recalled") or [])
        recent.append([candidate.path for candidate in selected])
        session["recently_recalled"] = recent[-(config["skip_recent_turns"] + 1) :]
    return "\n".join(candidate.text for candidate in selected)
