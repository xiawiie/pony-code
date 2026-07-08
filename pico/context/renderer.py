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
"""

from __future__ import annotations

from .escaping import escape_pico_tags
from .intent import classify_intent
from .sources import (
    render_checkpoint,
    render_memory_index,
    render_project_structure,
    render_workspace_state,
)

# ``recalled_memory`` is a Phase 3 source. Slot is reserved here so budgets
# and telemetry line up; the placeholder returns ``None`` for now.
SOURCE_ORDER = (
    "workspace_state",
    "memory_index",
    "project_structure",
    "recalled_memory",
    "checkpoint",
)

_RENDERERS = {
    "workspace_state": render_workspace_state,
    "memory_index": render_memory_index,
    "project_structure": render_project_structure,
    "recalled_memory": lambda agent, budget_tokens: None,  # P3 replaces
    "checkpoint": render_checkpoint,
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
        raw = renderer(agent, source_budget)
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
