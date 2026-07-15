"""Assemble the current-turn user message.

The current user message is not just the raw user string — pico injects a
sequence of ``<system-reminder>`` blocks before it, one per injection
source, wrapped in ``<pico:name>...</pico:name>`` tags. Blocks are
selected by the intent-derived budget: a source with a zero budget is
skipped entirely; a source that has no content to render also drops out.

The renderer is a pure function of ``(agent, user_message)`` — it does
not mutate the agent, does not append to any message list, and does not
call the model. It returns ``(rendered_text, telemetry)`` and lets the
caller (``ContextManager.build_request``) decide what to do with the result.

Escaping: any ``<pico:*>`` / ``</pico:*>`` substring inside source
output is neutralized with a zero-width space (see :mod:`.escaping`).
This defends against a user note or a tool result that happens to
contain a literal system tag.

Telemetry keys:

- ``intent``: ``{name, matched_keyword}`` from :func:`classify_intent`
- ``injection_tokens[source]``: rendered token count per source
  (0 when the source was skipped or empty)
- ``injection_truncated[source]``: counter incremented when a source's
  raw output exceeded its per-source budget and was tail-clipped
- ``injection_dropped``: names of sources whose blocks were removed
  because the sum of ``injection_tokens`` exceeded ``injection_budget``;
  drops proceed in ``DROP_PRIORITY`` order (least-important first)
- ``injection_budget``: the aggregate cap
  (``injection_budget_ratio × total_budget_hard_cap``) that the drop
  logic compares the sum of ``injection_tokens`` against
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging

from pico import security as securitylib

from .escaping import escape_pico_tags
from .intent import classify_intent
from .sources import (
    render_checkpoint,
    render_memory_index,
    render_project_structure,
    render_recalled_memory,
    render_workspace_state,
)

logger = logging.getLogger("pico")


@dataclass(frozen=True)
class InjectionSource:
    name: str
    required: bool
    text: str
    token_count: int
    status: str
    reason_code: str
    selected_memory_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class InjectionSnapshot:
    current_user: str
    runtime_feedback: str
    intent_name: str
    sources: tuple[InjectionSource, ...]

    def render(self, included_names=None):
        allowed = set(included_names) if included_names is not None else None
        blocks = [source.text for source in self.sources if source.text and (allowed is None or source.name in allowed)]
        return "\n\n".join([*blocks, self.current_user]) if blocks else self.current_user

SOURCE_ORDER = (
    "workspace_state",
    "memory_index",
    "project_structure",
    "recalled_memory",
    "checkpoint",
)

# Task C1: drop-priority order — least important first. When aggregate
# injection tokens exceed injection_budget, sources are dropped from the
# start of this list. Distinct from SOURCE_ORDER (which is the *render*
# order in the outgoing user message).
DROP_PRIORITY = (
    "checkpoint",
    "project_structure",
    "memory_index",
    "workspace_state",
    "recalled_memory",  # last — decision-critical per spec §4.4.3
)

# Task 24: ``recalled_memory`` needs the current turn's user message to
# score relevance, so its renderer takes a 3rd positional arg. All other
# renderers ignore it — we call every renderer with a uniform signature
# ``(agent, budget_tokens, user_message)`` and let each ignore what it
# doesn't need. This avoids the monkey-patch trick from the original plan
# where the user message was stashed on ``agent`` as a hidden attribute.
_RENDERERS = {
    "workspace_state": lambda agent, budget, user_msg: render_workspace_state(agent, budget),
    "memory_index": lambda agent, budget, user_msg: render_memory_index(agent, budget),
    "project_structure": lambda agent, budget, user_msg: render_project_structure(agent, budget),
    "recalled_memory": render_recalled_memory,
    "checkpoint": lambda agent, budget, user_msg: render_checkpoint(agent, budget),
}


def _count_tokens(agent, text):
    counter = getattr(getattr(agent, "model_client", None), "count_tokens", None)
    if callable(counter):
        try:
            return int(counter(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def build_injection_snapshot(agent, user_message, runtime_feedback="", render_fn=None):
    """Build one immutable, side-effect-free snapshot for a model attempt."""
    recent_before = list((getattr(agent, "session", {}) or {}).get("recently_recalled") or [])
    renderer = render_fn or render_current_user_message
    text, telemetry = renderer(agent, user_message)
    session = getattr(agent, "session", {}) or {}
    recent_after = list(session.get("recently_recalled") or [])
    selected = tuple(recent_after[-1]) if len(recent_after) > len(recent_before) else ()
    if isinstance(session, dict):
        session["recently_recalled"] = recent_before
    sources = []
    for name in SOURCE_ORDER:
        marker = f"<pico:{name}>"
        block = next((part for part in text.split("\n\n") if marker in part), "")
        tokens = int(telemetry["injection_tokens"].get(name, 0) or 0)
        if name in telemetry["injection_dropped"]:
            status, reason = "dropped_budget", "aggregate_budget"
        elif tokens:
            status, reason = "included", "recall_match" if name == "recalled_memory" else "source_available"
        else:
            status, reason = "empty", "source_empty"
        sources.append(InjectionSource(name, name == "checkpoint" and bool(block), block, tokens, status, reason, selected if name == "recalled_memory" else ()))
    snapshot = InjectionSnapshot(str(user_message), str(runtime_feedback or ""), telemetry["intent"]["name"], tuple(sources))
    return snapshot, telemetry


def render_current_user_message(agent, user_message):
    """Return ``(rendered_text, telemetry_dict)`` for the current turn.

    ``rendered_text`` is the concatenation of one ``<system-reminder>``
    block per non-empty injection source, followed by the raw
    ``user_message``. If no source contributed, the raw user message is
    returned unchanged.
    """
    intent = classify_intent(user_message)
    budget = intent.budget

    # Task C4: expose why the intent classifier landed on this profile so
    # trace consumers can distinguish a real keyword hit from the
    # fallback-to-default path without re-parsing the raw keyword.
    if intent.matched_keyword:
        matched_reason = f"keyword:'{intent.matched_keyword}' via profile:{intent.name}"
    else:
        matched_reason = "default (no keyword)"
    telemetry = {
        "intent": {
            "name": intent.name,
            "matched_keyword": intent.matched_keyword,
            "matched_reason": matched_reason,
        },
        "injection_tokens": {},
        "injection_truncated": {},
        "injection_dropped": [],
    }

    # Task B6: compute the aggregate injection budget cap. C1's drop
    # logic (below) compares the sum of ``injection_tokens`` against it.
    # Guard: only trust real dicts — a MagicMock in tests would otherwise
    # return truthy sentinels whose ``__float__`` / ``__int__`` collapse
    # the budget to 1 and drop every block.
    cfg = getattr(agent, "context_config", None)
    if not isinstance(cfg, dict):
        cfg = {}
    ratio = float(cfg.get("injection_budget_ratio", 0.15))
    total = int(cfg.get("total_budget_hard_cap", 100000))
    telemetry["injection_budget"] = int(total * ratio)

    redaction_env = getattr(agent, "redaction_env", None)
    if not isinstance(redaction_env, Mapping):
        redaction_env = None
    secret_env_names = getattr(agent, "secret_env_names", ())
    if not isinstance(secret_env_names, (list, tuple, set, frozenset)):
        secret_env_names = ()

    blocks = []
    for source_name in SOURCE_ORDER:
        source_budget = int(budget.get(source_name, 0) or 0)
        if source_budget <= 0:
            telemetry["injection_tokens"][source_name] = 0
            continue
        renderer = _RENDERERS.get(source_name)
        if renderer is None:
            telemetry["injection_tokens"][source_name] = 0
            continue
        raw = renderer(agent, source_budget, user_message)
        if not raw:
            telemetry["injection_tokens"][source_name] = 0
            continue
        raw, _ = securitylib.sanitize_provider_payload(
            raw,
            [],
            env=redaction_env,
            secret_env_names=secret_env_names,
        )
        original_tokens = _count_tokens(agent, raw)
        if original_tokens > source_budget:
            telemetry["injection_truncated"][source_name] = (
                telemetry["injection_truncated"].get(source_name, 0) + 1
            )
        escaped = escape_pico_tags(raw)
        block = (
            "<system-reminder>\n"
            f"<pico:{source_name}>\n{escaped}\n</pico:{source_name}>\n"
            "</system-reminder>"
        )
        blocks.append(block)
        telemetry["injection_tokens"][source_name] = _count_tokens(agent, escaped)

    # Task C1: enforce aggregate injection budget by dropping sources in
    # DROP_PRIORITY order until we fit. `blocks` and `telemetry` track the
    # in-order rendered state; a dropped source is removed from both.
    injection_budget = telemetry["injection_budget"]

    def _current_tokens():
        return sum(telemetry["injection_tokens"].get(s, 0) for s in SOURCE_ORDER)

    if injection_budget >= 0:
        for candidate in DROP_PRIORITY:
            if _current_tokens() <= injection_budget:
                break
            if telemetry["injection_tokens"].get(candidate, 0) <= 0:
                continue
            # Remove any block whose tag matches this source name.
            blocks = [b for b in blocks if f"<pico:{candidate}>" not in b]
            telemetry["injection_tokens"][candidate] = 0
            telemetry["injection_dropped"].append(candidate)

    text = "\n\n".join(blocks + [user_message]) if blocks else user_message
    return text, telemetry
