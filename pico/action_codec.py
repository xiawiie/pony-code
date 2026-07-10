"""Pure decoding from provider Responses to runtime Actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .providers.response import Response, StopReason

_EXCERPT_LIMIT = 160
_ATTR_RE = re.compile(
    r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_ATTRIBUTE_TOOL_OPEN_RE = re.compile(r"^<tool(?=\s|/?>)")


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments: dict
    tool_use_id: str | None
    origin: Literal["native_tool_use", "text_protocol"]
    ignored_tool_count: int = 0


@dataclass(frozen=True)
class FinalAction:
    text: str
    origin: Literal["provider_text", "text_protocol"]
    truncated: bool = False


@dataclass(frozen=True)
class RetryAction:
    reason_code: str
    notice: str
    origin: Literal["response", "text_protocol"]
    excerpt: str = ""


Action: TypeAlias = ToolAction | FinalAction | RetryAction

_NOTICES = {
    "empty_response": "Runtime notice: the model returned no actionable content. Return one tool call or a non-empty final answer.",
    "malformed_tool_protocol": "Runtime notice: the text tool call was malformed. Return one valid tool call or a non-empty final answer.",
    "empty_final_protocol": "Runtime notice: the final answer was empty or incomplete. Return a non-empty final answer.",
    "invalid_native_tool": "Runtime notice: the native tool call had an invalid name or arguments object. Return one valid tool call.",
    "stop_sequence": "Runtime notice: the model stopped before completing an action. Return one tool call or a non-empty final answer.",
    "unsupported_response_shape": "Runtime notice: the model response shape was unsupported. Return one tool call or a non-empty final answer.",
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


def _native_action(tool_blocks):
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
        ignored_tool_count=len(tool_blocks) - 1,
    )


def _tag_body(text, tag):
    opening = f"<{tag}>"
    closing = f"</{tag}>"
    body_start = len(opening)
    body_end = text.find(closing, body_start)
    if body_end < 0:
        return text[body_start:], False
    return text[body_start:body_end], True


def _attrs(text):
    values = {}
    for match in _ATTR_RE.finditer(text):
        values[match.group(1)] = (
            match.group(2) if match.group(2) is not None else match.group(3)
        )
    return values


def _nested_value(body, key):
    opening = f"<{key}>"
    closing = f"</{key}>"
    start = body.find(opening)
    if start < 0:
        return None
    start += len(opening)
    end = body.find(closing, start)
    return None if end < 0 else body[start:end]


def _attribute_tool(text):
    open_end = text.find(">")
    if open_end < 0:
        return None
    self_closing = text[:open_end].rstrip().endswith("/")
    close_start = text.find("</tool>", open_end + 1)
    if not self_closing and close_start < 0:
        return None
    values = _attrs(text[len("<tool"):open_end])
    name = str(values.pop("name", "")).strip()
    if not name:
        return None
    body = "" if self_closing else text[open_end + 1:close_start]
    arguments = dict(values)
    nested_stack = []
    for match in re.finditer(
        r"</?(content|old_text|new_text|command|task|pattern|path)>", body
    ):
        key = match.group(1)
        if match.group(0).startswith("</"):
            if not nested_stack or nested_stack.pop() != key:
                return None
        else:
            nested_stack.append(key)
    if nested_stack:
        return None
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        nested = _nested_value(body, key)
        if nested is not None:
            arguments[key] = nested
    if name == "write_file" and "content" not in arguments and body.strip():
        arguments["content"] = body.strip("\n")
    if name == "delegate" and "task" not in arguments and body.strip():
        arguments["task"] = body.strip()
    return ToolAction(
        name=name,
        arguments=arguments,
        tool_use_id=None,
        origin="text_protocol",
    )


def _text_tool(text):
    if text.startswith("<tool>"):
        body, closed = _tag_body(text, "tool")
        if not closed:
            return _retry("malformed_tool_protocol", "text_protocol", text)
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return _retry("malformed_tool_protocol", "text_protocol", text)
        if not isinstance(payload, dict):
            return _retry("malformed_tool_protocol", "text_protocol", text)
        name = payload.get("name")
        arguments = payload.get("args", payload.get("arguments", {}))
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
            return _retry("malformed_tool_protocol", "text_protocol", text)
        return ToolAction(
            name=name.strip(),
            arguments=dict(arguments),
            tool_use_id=None,
            origin="text_protocol",
        )
    action = _attribute_tool(text)
    return action or _retry("malformed_tool_protocol", "text_protocol", text)


def decode_action(response: Response) -> Action:
    try:
        content = list(response.content or [])
    except TypeError:
        return _retry("unsupported_response_shape", "response", response.content)
    tool_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if tool_blocks:
        return _native_action(tool_blocks)

    merged_text, saw_unsupported = _joined_text(content)
    leading = merged_text.lstrip()

    if leading.startswith("<tool>") or _ATTRIBUTE_TOOL_OPEN_RE.match(leading):
        return _text_tool(leading)

    if leading.startswith("<final>"):
        body, closed = _tag_body(leading, "final")
        body = body.strip()
        if body and (closed or response.stop_reason == StopReason.MAX_TOKENS):
            return FinalAction(
                text=body,
                origin="text_protocol",
                truncated=not closed or response.stop_reason == StopReason.MAX_TOKENS,
            )
        return _retry("empty_final_protocol", "text_protocol", leading)

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
    if not merged_text and not saw_unsupported:
        return _retry("empty_response", "response")
    return _retry("unsupported_response_shape", "response", merged_text or content)
