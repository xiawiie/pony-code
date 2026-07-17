"""Pony memory tool runners.

4 个 agent tool:
    memory_list, memory_read, memory_search, memory_save

`context.memory_store` 必须是 `BlockStore` 实例.
`context.memory_retrieval` 必须是 `Retrieval` 实例.
"""

from __future__ import annotations

from pony.memory.block_store import BlockStore
from pony.memory.retrieval import Retrieval


def _rel_time(delta_seconds: float) -> str:
    if delta_seconds < 60:
        return "just now"
    if delta_seconds < 3600:
        return f"{int(delta_seconds // 60)}m ago"
    if delta_seconds < 86400:
        return f"{int(delta_seconds // 3600)}h ago"
    return f"{int(delta_seconds // 86400)}d ago"


def _now_ts() -> float:
    import time
    return time.time()


def tool_memory_list(context, args: dict) -> str:
    store: BlockStore = getattr(context, "memory_store", None)
    if store is None:
        raise RuntimeError("memory_store unavailable")
    prefix = str(args.get("prefix", "")).strip()
    entries = store.list()
    if prefix:
        entries = [e for e in entries if e.path.startswith(prefix)]
    if not entries:
        return "(no memory files yet)"

    now = _now_ts()
    lines = []
    notes = [e for e in entries if "/notes/" in e.path]
    agent_notes = [e for e in entries if e.path.endswith("/agent_notes.md")]
    if notes:
        lines.append("Notes (user-written, read-only for agent):")
        for e in notes:
            age = _rel_time(now - e.mtime)
            lines.append(f"- {e.path} ({e.size_chars} chars, {age})")
    if agent_notes:
        if lines:
            lines.append("")
        lines.append("Agent records:")
        for e in agent_notes:
            age = _rel_time(now - e.mtime)
            lines.append(f"- {e.path} ({e.size_chars} chars, {age})")
    return "\n".join(lines)


def tool_memory_read(context, args: dict) -> str:
    store: BlockStore = getattr(context, "memory_store", None)
    if store is None:
        raise RuntimeError("memory_store unavailable")
    path = str(args.get("path", "")).strip()
    raw = store.read(path)

    lines = raw.splitlines()
    start = int(args.get("start", 1) or 1)
    end = int(args.get("end", 200) or 200)
    slice_lines = lines[start - 1 : end]
    numbered = [f"{start + i:>4}: {line}" for i, line in enumerate(slice_lines)]
    footer = ""
    if len(lines) > end:
        footer = f"\n[... {len(lines) - end} more lines, use start/end for paging]"
    header = f"# {path} (lines {start}-{min(end, len(lines))} of {len(lines)})\n"
    return header + "\n".join(numbered) + footer


def tool_memory_search(context, args: dict) -> str:
    retrieval: Retrieval = getattr(context, "memory_retrieval", None)
    if retrieval is None:
        raise RuntimeError("memory_retrieval unavailable")
    query = str(args.get("query", "")).strip()
    limit = int(args.get("limit", 5) or 5)
    hits = retrieval.search(query, limit=limit)
    if not hits:
        return f"No matches for {query!r}."
    lines = [f"Found {len(hits)} match(es) for {query!r}:"]
    for hit in hits:
        lines.append(f"- {hit.path} (score={hit.score:.2f})")
        for snip in hit.snippets[:2]:
            lines.append(f"  {snip}")
    return "\n".join(lines)


def tool_memory_save(context, args: dict) -> str:
    """Append one agent note to the selected scope."""
    store: BlockStore = getattr(context, "memory_store", None)
    if store is None:
        raise RuntimeError("memory_store unavailable")
    if set(args) - {"note", "scope"}:
        raise ValueError("memory_save accepts only note and scope")
    note = str(args.get("note", "")).strip()
    scope = str(args.get("scope", "workspace")).strip()
    total = store.append_agent_note(scope=scope, note=note)  # type: ignore[arg-type]
    return f"saved: {scope}/agent_notes.md (chars_total={total})"
