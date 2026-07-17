"""Pure decoding from native provider Responses to runtime Actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from pony.providers.response import Response, StopReason

_EXCERPT_LIMIT = 160
@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict
    tool_use_id: str | None
    origin: Literal["native_tool_use"]
    provider_state: tuple[dict, ...] = ()


@dataclass(frozen=True)
class FinalAction:
    text: str
    origin: Literal["provider_text"]
    truncated: bool = False


@dataclass(frozen=True)
class RetryAction:
    reason_code: str
    notice: str
    origin: Literal["response"]
    excerpt: str = ""


Action: TypeAlias = ToolAction | FinalAction | RetryAction

_NOTICES = {
    "empty_response": "Runtime notice: the model returned no actionable content. Return one tool call or a non-empty final answer.",
    "invalid_native_tool": "Runtime notice: the native tool call had an invalid name or arguments object. Return one valid tool call.",
    "multiple_actions_not_supported": "Runtime notice: Pony accepts exactly one tool call per model response. Return exactly one tool call and do not repeat the other calls.",
    "stop_sequence": "Runtime notice: the model stopped before completing an action. Return one tool call or a non-empty final answer.",
    "provider_protocol_mismatch": "Runtime notice: the native provider response did not match the configured protocol. Return one native tool call or a non-empty final answer.",
}


def _retry(reason_code, origin, raw=""):
    return RetryAction(
        reason_code=reason_code,
        notice=_NOTICES[reason_code],
        origin=origin,
        excerpt=str(raw).strip()[:_EXCERPT_LIMIT],
    )


def _joined_text(content):
    parts = []
    saw_unsupported = False
    for block in content:
        if not isinstance(block, dict):
            saw_unsupported = True
            continue
        if block.get("type") == "text":
            value = str(block.get("text", "") or "")
            if value.strip():
                parts.append(value.strip())
        elif block.get("type") != "tool_use":
            saw_unsupported = True
    return "\n".join(parts), saw_unsupported


def _native_action(tool_blocks, provider_state):
    if len(tool_blocks) != 1:
        return _retry("multiple_actions_not_supported", "response", tool_blocks)
    first = tool_blocks[0]
    name = first.get("name")
    arguments = first.get("input")
    tool_use_id = first.get("id")
    if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
        return _retry("invalid_native_tool", "response", first)
    if tool_use_id is not None and not isinstance(tool_use_id, str):
        return _retry("invalid_native_tool", "response", first)
    return ToolAction(
        name=name.strip(),
        arguments=dict(arguments),
        tool_use_id=tool_use_id or None,
        origin="native_tool_use",
        provider_state=tuple(dict(item) for item in provider_state),
    )


def decode_action(response: Response) -> Action:
    try:
        content = list(response.content or [])
    except TypeError:
        return _retry("provider_protocol_mismatch", "response", response.content)
    merged_text, saw_unsupported = _joined_text(content)
    if saw_unsupported:
        return _retry(
            "provider_protocol_mismatch",
            "response",
            merged_text or content,
        )
    tool_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if tool_blocks:
        if response.stop_reason != StopReason.TOOL_USE:
            return _retry(
                "provider_protocol_mismatch",
                "response",
                tool_blocks,
            )
        return _native_action(tool_blocks, response.provider_state)

    if response.stop_reason == StopReason.REFUSAL:
        return FinalAction(
            text=merged_text or "The model declined this request.",
            origin="provider_text",
        )

    if response.stop_reason == StopReason.END_TURN and merged_text:
        return FinalAction(text=merged_text, origin="provider_text")
    if response.stop_reason == StopReason.MAX_TOKENS and merged_text:
        return FinalAction(
            text=merged_text,
            origin="provider_text",
            truncated=True,
        )
    if response.stop_reason == StopReason.STOP_SEQUENCE:
        return _retry("stop_sequence", "response", merged_text)
    if not merged_text:
        return _retry("empty_response", "response")
    return _retry("provider_protocol_mismatch", "response", merged_text or content)
