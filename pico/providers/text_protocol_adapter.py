"""Adapt a text-only provider to Pico's structured completion surface."""

import json

from pico.messages import render_transcript, strip_pico_meta

from .response import Response, StopReason


_TEXT_PROTOCOL_INSTRUCTION = """Text response protocol:
Return exactly one action.
For a tool call, use strict JSON:
<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>
For a final answer, use:
<final>answer</final>
Do not wrap examples or explanations around the action."""


def _flatten_system(system):
    return "\n\n".join(block.get("text", "") for block in system if block.get("text"))


def _flatten_tools(tools):
    if not tools:
        return ""
    return "Available tools (JSON):\n" + json.dumps(
        tools,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class TextProtocolAdapter:
    def __init__(self, text_provider):
        self._inner = text_provider
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        del cache_breakpoints
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        prompt = "\n\n".join(
            part
            for part in (
                _flatten_system(system),
                _flatten_tools(tools),
                _TEXT_PROTOCOL_INSTRUCTION,
                "<transcript>\n"
                + render_transcript(strip_pico_meta(messages))
                + "\n</transcript>",
            )
            if part
        )
        try:
            raw = self._inner.complete_text(prompt, max_tokens)
        finally:
            attempts = getattr(self._inner, "last_transport_attempts", None)
            self.last_transport_attempts = (
                attempts if type(attempts) is int and attempts >= 0 else None
            )
        usage = dict(getattr(self._inner, "last_completion_metadata", {}) or {})
        self.last_completion_metadata = usage
        stop_reason = getattr(self._inner, "last_stop_reason", StopReason.END_TURN)
        if not isinstance(stop_reason, StopReason):
            stop_reason = StopReason.END_TURN
        return Response(
            stop_reason=stop_reason,
            content=[{"type": "text", "text": str(raw)}],
            usage=usage,
        )
