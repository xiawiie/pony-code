"""Build one immutable current-turn Context Source snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging

from pony.security import redaction as securitylib
from pony.agent.model_capabilities import TokenAccounting

from .chunks import SOURCE_HARD_CAPS, allocate_context_chunks
from .escaping import escape_pony_tags
from .sources import build_source_chunks


logger = logging.getLogger("pony")

SOURCE_ORDER = (
    "recovery_state",
    "task_working_set",
    "workspace_state",
    "recalled_memory",
    "project_structure",
    "memory_index",
)


@dataclass(frozen=True)
class InjectionSource:
    name: str
    required: bool
    text: str
    token_count: int
    status: str
    reason_code: str
    hard_cap: int
    priority: int
    selected_memory_paths: tuple[str, ...] = ()
    chunk_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class InjectionSnapshot:
    current_user: str
    runtime_feedback: str
    allocator_name: str
    sources: tuple[InjectionSource, ...]

    def render(self, included_names=None):
        allowed = set(included_names) if included_names is not None else None
        blocks = [
            source.text
            for source in self.sources
            if source.text and (allowed is None or source.name in allowed)
        ]
        return (
            "\n\n".join([*blocks, self.current_user]) if blocks else self.current_user
        )


def _memory_snapshot(agent):
    retrieval = getattr(agent, "memory_retrieval", None)
    if retrieval is None:
        return None, "disabled"
    try:
        return retrieval.snapshot(), "loaded"
    except securitylib.SensitiveDataBlockedError:
        raise
    except Exception as exc:
        logger.debug("memory query snapshot failed: %s", type(exc).__name__)
        session = getattr(agent, "session", None)
        if isinstance(session, dict):
            counters = session.setdefault("_recall_errors", {"count": 0, "last": ""})
            counters["count"] = int(counters.get("count", 0)) + 1
            counters["last"] = f"{type(exc).__name__}: {exc}"[:200]
        return None, "error"


def _redaction_options(agent):
    redaction_env = getattr(agent, "redaction_env", None)
    if not isinstance(redaction_env, Mapping):
        redaction_env = None
    secret_env_names = getattr(agent, "secret_env_names", ())
    if not isinstance(secret_env_names, (list, tuple, set, frozenset)):
        secret_env_names = ()
    return redaction_env, secret_env_names


def _source_block(name, texts, agent):
    raw = "\n\n".join(texts)
    redaction_env, secret_env_names = _redaction_options(agent)
    safe, _ = securitylib.sanitize_provider_payload(
        raw,
        [],
        env=redaction_env,
        secret_env_names=secret_env_names,
    )
    escaped = escape_pony_tags(str(safe))
    return (
        "<system-reminder>\n"
        f"<pony:{name}>\n{escaped}\n</pony:{name}>\n"
        "</system-reminder>"
    )


def build_injection_snapshot(
    agent,
    user_message,
    runtime_feedback="",
    render_fn=None,
):
    """Scan Memory once, allocate whole chunks, and freeze one top-level turn."""
    del render_fn
    accounting = getattr(agent, "token_accounting", None)
    if not isinstance(accounting, TokenAccounting):
        accounting = TokenAccounting(
            getattr(getattr(agent, "model_client", None), "count_tokens", None)
        )
    memory_snapshot, memory_snapshot_status = _memory_snapshot(agent)
    chunks = build_source_chunks(
        agent,
        str(user_message),
        memory_snapshot=memory_snapshot,
    )
    raw_pool = getattr(
        getattr(agent, "model_budget", None),
        "source_pool_tokens",
        None,
    )
    config = getattr(agent, "context_config", None)
    config = config if isinstance(config, dict) else {}
    pool_tokens = (
        raw_pool
        if type(raw_pool) is int and raw_pool >= 0
        else config.get("source_pool_tokens", 16_384)
    )
    pool_tokens = int(pool_tokens)
    allocation = allocate_context_chunks(chunks, pool_tokens=pool_tokens)
    selected_by_source = {name: [] for name in SOURCE_ORDER}
    for chunk in allocation.selected:
        selected_by_source.setdefault(chunk.source, []).append(chunk)
    dropped_by_source = {name: [] for name in SOURCE_ORDER}
    for dropped in allocation.dropped:
        dropped_by_source.setdefault(dropped.chunk.source, []).append(dropped)

    sources = []
    injection_tokens = {}
    selected_paths = []
    for name in SOURCE_ORDER:
        selected = selected_by_source.get(name, [])
        dropped = dropped_by_source.get(name, [])
        block = (
            _source_block(name, [chunk.text for chunk in selected], agent)
            if selected
            else ""
        )
        actual_tokens = accounting.count_text(block) if block else 0
        injection_tokens[name] = actual_tokens
        paths = tuple(
            str(chunk.provenance.get("path", ""))
            for chunk in selected
            if str(chunk.provenance.get("path", ""))
        )
        if name == "recalled_memory":
            selected_paths.extend(paths)
        if selected:
            status = "included"
            reason = "priority_allocator"
        elif dropped:
            status = "dropped_budget"
            reason = dropped[0].reason
        else:
            status = "empty"
            reason = "source_empty"
        sources.append(
            InjectionSource(
                name=name,
                required=any(chunk.required for chunk in selected),
                text=block,
                token_count=actual_tokens,
                status=status,
                reason_code=reason,
                hard_cap=SOURCE_HARD_CAPS[name],
                priority=min((chunk.priority for chunk in selected), default=2),
                selected_memory_paths=paths,
                chunk_keys=tuple(chunk.key for chunk in selected),
            )
        )

    telemetry = {
        "context_source_allocator": {
            "name": "priority_allocator",
            "pool_tokens": allocation.pool_tokens,
            "used_tokens": allocation.used_tokens,
            "remaining_tokens": allocation.pool_tokens - allocation.used_tokens,
            "selected_chunks": len(allocation.selected),
            "dropped_chunks": len(allocation.dropped),
            "source_tokens": dict(allocation.source_tokens),
            "memory_snapshot": memory_snapshot_status,
        },
        "injection_tokens": injection_tokens,
        "injection_budget": allocation.pool_tokens,
        "injection_dropped": list(
            dict.fromkeys(dropped.chunk.source for dropped in allocation.dropped)
        ),
        "injection_truncated": {},
        "recall_selected_paths": selected_paths,
    }
    snapshot = InjectionSnapshot(
        current_user=str(user_message),
        runtime_feedback=str(runtime_feedback or ""),
        allocator_name="priority_allocator",
        sources=tuple(sources),
    )
    return snapshot, telemetry


def render_current_user_message(agent, user_message):
    snapshot, telemetry = build_injection_snapshot(agent, user_message)
    return snapshot.render(), telemetry
