"""Token-budgeted, whole-chunk allocation for dynamic Context Sources."""

from __future__ import annotations

from dataclasses import dataclass, field


SOURCE_HARD_CAPS = {
    "active_skill": 10_240,
    "workspace_state": 3_072,
    "project_structure": 6_144,
    "task_working_set": 3_072,
    "recalled_memory": 6_144,
    "memory_index": 1_024,
    "recovery_state": 2_048,
}


class RequiredContextTooLarge(RuntimeError):
    code = "required_context_too_large"


@dataclass(frozen=True)
class ContextChunk:
    source: str
    key: str
    text: str
    tokens: int
    priority: int
    required: bool
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DroppedContextChunk:
    chunk: ContextChunk
    reason: str


@dataclass(frozen=True)
class ContextAllocation:
    selected: tuple[ContextChunk, ...]
    dropped: tuple[DroppedContextChunk, ...]
    pool_tokens: int
    used_tokens: int
    source_tokens: dict


def make_chunk(
    accounting,
    *,
    source,
    key,
    text,
    priority,
    required=False,
    provenance=None,
):
    value = str(text or "").strip()
    if not value:
        return None
    # Include conservative XML/system-reminder framing overhead in allocation.
    tokens = accounting.count_text(value) + 12
    return ContextChunk(
        source=str(source),
        key=str(key),
        text=value,
        tokens=max(1, int(tokens)),
        priority=int(priority),
        required=bool(required),
        provenance=dict(provenance or {}),
    )


def allocate_context_chunks(
    chunks,
    *,
    pool_tokens,
    source_hard_caps=None,
):
    """Allocate complete chunks by required/P0/P1/P2 order; never slice text."""
    pool = max(0, int(pool_tokens))
    caps = dict(SOURCE_HARD_CAPS)
    if source_hard_caps:
        caps.update(source_hard_caps)
    indexed = list(enumerate(chunk for chunk in chunks if chunk is not None))
    indexed.sort(
        key=lambda item: (
            not item[1].required,
            item[1].priority,
            int(item[1].provenance.get("rank", item[0])),
            item[0],
        )
    )
    selected = []
    dropped = []
    used = 0
    source_used = {source: 0 for source in caps}
    for _, chunk in indexed:
        cap = int(caps.get(chunk.source, 0))
        reason = ""
        if cap <= 0:
            reason = "unknown_source"
        elif chunk.tokens > cap:
            reason = "chunk_exceeds_source_cap"
        elif source_used.get(chunk.source, 0) + chunk.tokens > cap:
            reason = "source_cap"
        elif used + chunk.tokens > pool:
            reason = "global_pool"
        if reason:
            if chunk.required:
                raise RequiredContextTooLarge(
                    f"required context chunk {chunk.source}:{chunk.key} does not fit ({reason})"
                )
            dropped.append(DroppedContextChunk(chunk, reason))
            continue
        selected.append(chunk)
        used += chunk.tokens
        source_used[chunk.source] = source_used.get(chunk.source, 0) + chunk.tokens
    return ContextAllocation(
        selected=tuple(selected),
        dropped=tuple(dropped),
        pool_tokens=pool,
        used_tokens=used,
        source_tokens={key: value for key, value in source_used.items() if value},
    )
