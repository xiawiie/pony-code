"""Decode provider-neutral Response objects into runtime actions."""

from __future__ import annotations

from typing import Any

from pico.model_actions import ActionOrigin, AgentAction, FinalAction, RetryAction, ToolAction
from pico.model_output_parser import parse_model_output, retry_notice
from pico.providers.response import Response, StopReason


class ActionCodec:
    """Pure response decoder used by AgentLoop."""

    def decode(self, response: Response) -> AgentAction:
        content_blocks = list(response.content or [])
        tool_blocks = [
            block
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if tool_blocks:
            return self._decode_native_tool(tool_blocks)

        text = self._first_non_empty_text(content_blocks)
        if not text:
            if response.stop_reason == StopReason.STOP_SEQUENCE:
                return RetryAction(
                    reason=retry_notice("model stopped before returning a tool call or final answer"),
                    model_visible=True,
                    origin=ActionOrigin.STOP_SEQUENCE,
                )
            return RetryAction(
                reason=retry_notice("model returned an empty response"),
                model_visible=True,
                origin=ActionOrigin.EMPTY_RESPONSE,
            )

        stripped = text.lstrip()
        if self._starts_with_tool_protocol(stripped):
            return self._decode_text_tool(stripped)
        if stripped.startswith("<final>"):
            kind, payload = parse_model_output(stripped)
            if kind == "final":
                return FinalAction(text=str(payload), origin=ActionOrigin.TEXT_PROTOCOL_FINAL)
            return RetryAction(
                reason=str(payload),
                model_visible=True,
                origin=ActionOrigin.MALFORMED_TEXT_PROTOCOL,
            )
        if response.stop_reason == StopReason.STOP_SEQUENCE:
            return RetryAction(
                reason=retry_notice("model stopped before returning a tool call or final answer"),
                model_visible=True,
                origin=ActionOrigin.STOP_SEQUENCE,
            )
        return FinalAction(text=text.strip(), origin=ActionOrigin.PLAIN_TEXT_FINAL)

    def _decode_native_tool(self, tool_blocks: list[dict[str, Any]]) -> AgentAction:
        first = tool_blocks[0]
        name = str(first.get("name", "") or "").strip()
        if not name:
            return RetryAction(
                reason=retry_notice("native tool_use block is missing a tool name"),
                model_visible=True,
                origin=ActionOrigin.UNSUPPORTED_RESPONSE,
            )
        raw_input = first.get("input", {})
        if not isinstance(raw_input, dict):
            return RetryAction(
                reason=retry_notice("native tool_use block has non-object tool arguments"),
                model_visible=True,
                origin=ActionOrigin.UNSUPPORTED_RESPONSE,
            )
        return ToolAction(
            name=name,
            arguments=dict(raw_input),
            id=str(first.get("id")) if first.get("id") else None,
            origin=ActionOrigin.NATIVE_TOOL_USE,
            ignored_tool_count=max(0, len(tool_blocks) - 1),
        )

    def _decode_text_tool(self, text: str) -> AgentAction:
        kind, payload = parse_model_output(text)
        if kind != "tool":
            return RetryAction(
                reason=str(payload),
                model_visible=True,
                origin=ActionOrigin.MALFORMED_TEXT_PROTOCOL,
            )
        if not isinstance(payload, dict):
            return RetryAction(
                reason=retry_notice("tool payload must be a JSON object"),
                model_visible=True,
                origin=ActionOrigin.MALFORMED_TEXT_PROTOCOL,
            )
        name = str(payload.get("name", "") or "").strip()
        args = payload.get("args", payload.get("arguments", {}))
        if args is None:
            args = {}
        if not name or not isinstance(args, dict):
            return RetryAction(
                reason=retry_notice("tool payload must include a name and object arguments"),
                model_visible=True,
                origin=ActionOrigin.MALFORMED_TEXT_PROTOCOL,
            )
        return ToolAction(
            name=name,
            arguments=dict(args),
            id=None,
            origin=ActionOrigin.TEXT_PROTOCOL_TOOL,
        )

    def _first_non_empty_text(self, content_blocks: list[dict]) -> str:
        for block in content_blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text", "") or "")
            if text.strip():
                return text
        return ""

    def _starts_with_tool_protocol(self, text: str) -> bool:
        return text.startswith("<tool>") or text.startswith("<tool ")
