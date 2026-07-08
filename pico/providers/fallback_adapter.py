"""Adapter that lets non-tool_use providers speak system+tools+messages API.

Flattens system+tools+messages to a single prompt string, delegates to
inner.complete(prompt, ...), then parses <tool>/<final> XML back into
native Response shape.
"""
from __future__ import annotations

import json
import uuid

from pico.model_output_parser import parse_model_output
from pico.providers.response import Response, StopReason


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


def _flatten_messages(messages: list[dict]) -> str:
    lines = ["Transcript:"]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for block in content:
            btype = block.get("type")
            if btype == "text":
                lines.append(f"[{role}] {block.get('text', '')}")
            elif btype == "tool_use":
                tid = block.get("id", "")
                id_part = f" id={tid}" if tid else ""
                lines.append(f"[{role}:tool_use{id_part}] {block['name']}({json.dumps(block.get('input', {}), sort_keys=True)})")
            elif btype == "tool_result":
                tid = block.get("tool_use_id", "")
                id_part = f" id={tid}" if tid else ""
                lines.append(f"[{role}:tool_result{id_part}] {block.get('content', '')}")
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

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        from .message_utils import strip_pico_meta
        messages = strip_pico_meta(messages)
        prompt = "\n\n".join(part for part in (_flatten_system(system), _flatten_tools(tools), _flatten_messages(messages)) if part)
        raw = self._inner.complete(prompt, max_tokens)
        self.last_completion_metadata = dict(getattr(self._inner, "last_completion_metadata", {}))

        kind, payload = parse_model_output(raw)
        if kind == "tool":
            return Response(
                stop_reason=StopReason.TOOL_USE,
                content=[{
                    "type": "tool_use",
                    "id": f"toolu_local_{uuid.uuid4().hex[:12]}",
                    "name": payload["name"],
                    "input": dict(payload.get("args", {})),
                }],
                usage=self.last_completion_metadata,
            )
        if kind == "final":
            return Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": payload}],
                usage=self.last_completion_metadata,
            )
        # retry / malformed → 用 STOP_SEQUENCE 让上层看到"未完成"
        return Response(
            stop_reason=StopReason.STOP_SEQUENCE,
            content=[{"type": "text", "text": str(payload)}],
            usage=self.last_completion_metadata,
        )
