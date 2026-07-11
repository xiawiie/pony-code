"""Adapt a text-only provider to Pico's structured completion surface."""

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
    lines = ["Available tools:"]
    for tool in tools:
        schema = tool.get("input_schema", {}).get("properties", {})
        fields = ", ".join(
            f"{name}: {value.get('type', 'any')}" for name, value in schema.items()
        )
        lines.append(
            f"- {tool['name']}({fields}): {tool.get('description', '')}"
        )
    return "\n".join(lines)


class TextProtocolAdapter:
    def __init__(self, text_provider):
        self._inner = text_provider
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

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
        prompt = "\n\n".join(
            part
            for part in (
                _flatten_system(system),
                _flatten_tools(tools),
                _TEXT_PROTOCOL_INSTRUCTION,
                render_transcript(strip_pico_meta(messages)),
            )
            if part
        )
        raw = self._inner.complete_text(prompt, max_tokens)
        usage = dict(getattr(self._inner, "last_completion_metadata", {}) or {})
        self.last_completion_metadata = usage
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": str(raw)}],
            usage=usage,
        )
