"""Pico memory v2 · tool runners.

4 个 agent tool:
    memory_list, memory_read, memory_search, memory_save

`context.memory_store` 必须是 `BlockStore` 实例.
`context.memory_retrieval` 必须是 `Retrieval` 实例.
"""

from __future__ import annotations

from pico.memory.block_store import MAX_NOTE_CHARS, BlockStore
from pico.memory.retrieval import Retrieval


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
        return "memory_store unavailable"
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
        return "memory_store unavailable"
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: path must not be empty"
    try:
        raw = store.read(path)
    except FileNotFoundError:
        return f"error: memory file not found: {path}"
    except ValueError as exc:
        return f"error: {exc}"
    except OSError as exc:
        return f"error: {exc}"

    lines = raw.splitlines()
    start = max(1, int(args.get("start", 1) or 1))
    end = int(args.get("end", 200) or 200)
    if end < start:
        return "error: end must be >= start"
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
        return "memory_retrieval unavailable"
    query = str(args.get("query", "")).strip()
    if not query:
        return "error: query must not be empty"
    limit = int(args.get("limit", 5) or 5)
    limit = max(1, min(limit, 20))
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
    """Task 21: dual-path save.

    - With ``topic``: creates or appends ``agent/<topic>.md`` (per-topic file
      with frontmatter). The optional ``type`` arg overrides the frontmatter
      type field on first write (default ``feedback``); it is ignored on
      subsequent appends since the header is already fixed.
    - Without ``topic``: falls back to the legacy ``agent_notes.md`` single-file
      append. Preserved so mid-migration workflows keep working; retired once
      Task 22's migrator runs.
    """
    store: BlockStore = getattr(context, "memory_store", None)
    if store is None:
        return "memory_store unavailable"
    note = str(args.get("note", "")).strip()
    if not note:
        return "error: note must not be empty"
    if len(note) > MAX_NOTE_CHARS:
        return f"error: note exceeds {MAX_NOTE_CHARS} chars"
    scope = str(args.get("scope", "workspace")).strip() or "workspace"
    if scope not in ("workspace", "user"):
        return "error: scope must be 'workspace' or 'user'"

    topic = str(args.get("topic", "")).strip()
    if topic:
        note_type = str(args.get("type", "feedback")).strip() or "feedback"
        try:
            store.write_agent_topic(scope=scope, topic=topic, note=note, note_type=note_type)
        except ValueError as exc:
            return f"error: {exc}"
        return f"saved: {scope}/agent/{topic}.md"

    try:
        total = store.append_agent_note(scope=scope, note=note)  # type: ignore[arg-type]
    except ValueError as exc:
        return f"error: {exc}"
    return f"saved: {scope}/agent_notes.md (chars_total={total})"
