"""Assemble the current-turn user message.

The current user message is not just the raw user string — pico injects a
sequence of ``<system-reminder>`` blocks before it, one per injection
source, wrapped in ``<pico:name>...</pico:name>`` tags. Blocks are
selected by the intent-derived budget: a source with a zero budget is
skipped entirely; a source that has no content to render also drops out.

The renderer is a pure function of ``(agent, user_message)`` — it does
not mutate the agent, does not append to any message list, and does not
call the model. It returns ``(rendered_text, telemetry)`` and lets the
caller (``ContextManager.build_v2``) decide what to do with the result.

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
- ``injection_dropped``: reserved for a future overflow step where an
  entire source is dropped because the aggregate injection budget was
  breached; currently always ``[]``
- ``injection_budget``: the aggregate cap
  (``injection_budget_ratio × total_budget_hard_cap``) that Stream C1's
  drop logic will compare the sum of ``injection_tokens`` against
"""

from __future__ import annotations

from .escaping import escape_pico_tags
from .intent import classify_intent
from .sources import (
    render_checkpoint,
    render_memory_index,
    render_project_structure,
    render_recalled_memory,
    render_workspace_state,
)

SOURCE_ORDER = (
    "workspace_state",
    "memory_index",
    "project_structure",
    "recalled_memory",
    "checkpoint",
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


def render_current_user_message(agent, user_message):
    """Return ``(rendered_text, telemetry_dict)`` for the current turn.

    ``rendered_text`` is the concatenation of one ``<system-reminder>``
    block per non-empty injection source, followed by the raw
    ``user_message``. If no source contributed, the raw user message is
    returned unchanged.
    """
    intent = classify_intent(user_message)
    budget = intent.budget

    telemetry = {
        "intent": {"name": intent.name, "matched_keyword": intent.matched_keyword},
        "injection_tokens": {},
        "injection_truncated": {},
        "injection_dropped": [],
    }

    # Task B6: compute the aggregate injection budget cap. Downstream C1
    # will use it to drop least-important blocks when sum overflows.
    cfg = getattr(agent, "context_config", {}) or {}
    ratio = float(cfg.get("injection_budget_ratio", 0.15))
    total = int(cfg.get("total_budget_hard_cap", 100000))
    telemetry["injection_budget"] = int(total * ratio)

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

    text = "\n\n".join(blocks + [user_message]) if blocks else user_message
    return text, telemetry
