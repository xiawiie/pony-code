"""Adapter that lets non-tool_use providers speak system+tools+messages API.

Flattens system+tools+messages to a single prompt string, delegates to
inner.complete(prompt, ...), then returns the raw text for the shared action
codec to decode.
"""
from __future__ import annotations

from pico.messages import render_transcript, strip_pico_meta
from pico.providers.response import Response, StopReason


_TEXT_PROTOCOL_INSTRUCTION = """Text response protocol:
Return exactly one action.
For a tool call, use strict JSON:
<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>
For a final answer, use:
<final>answer</final>
Do not wrap examples or explanations around the action."""


def _flatten_system(system: list[dict]) -> str:
    parts = []
    for block in system:
        text = block.get("text", "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _flatten_tools(tools: list[dict]) -> str:
    if not tools:
        return ""
    lines = ["Available tools:"]
    for t in tools:
        schema = t.get("input_schema", {}).get("properties", {})
        fields = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in schema.items())
        lines.append(f"- {t['name']}({fields}): {t.get('description', '')}")
    return "\n".join(lines)


class FallbackAdapter:
    def __init__(self, inner_provider):
        self._inner = inner_provider
        self.supports_prompt_cache = False
        self.supports_native_tools = False
        self.last_completion_metadata = {}

    def __getattr__(self, name):
        # Delegate attribute reads (`.prompts`, `.outputs`, etc.) to inner so
        # tests / callers that peek at the underlying provider keep working
        # after the runtime auto-wraps a legacy provider. This only fires when
        # normal attribute lookup fails, so declared attributes on the adapter
        # itself still win.
        return getattr(self._inner, name)

    def complete_v2(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        del cache_breakpoints
        clean_messages = strip_pico_meta(messages)
        prompt = "\n\n".join(
            part
            for part in (
                _flatten_system(system),
                _flatten_tools(tools),
                _TEXT_PROTOCOL_INSTRUCTION,
                render_transcript(clean_messages),
            )
            if part
        )
        raw = self._inner.complete(prompt, max_tokens)
        usage = dict(getattr(self._inner, "last_completion_metadata", {}) or {})
        self.last_completion_metadata = usage
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": str(raw)}],
            usage=usage,
        )
