"""Pure operations for Pico's canonical message transcript."""

from __future__ import annotations

import json


class MessageValidationError(ValueError):
    """A canonical transcript violates the v3 message contract."""


def append_messages(messages, *new_messages):
    return [*list(messages or []), *new_messages]


def replace_latest_plain_user(messages, rendered_user):
    copied = [dict(message) for message in list(messages or [])]
    for index in range(len(copied) - 1, -1, -1):
        message = copied[index]
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = str(rendered_user)
            return copied
    raise MessageValidationError("request view has no top-level plain user message")


def strip_pico_meta(messages):
    cleaned = []
    for message in list(messages or []):
        item = dict(message)
        item.pop("_pico_meta", None)
        cleaned.append(item)
    return cleaned


def build_request_messages(messages, *, rendered_user, runtime_feedback=""):
    content = str(rendered_user)
    feedback = str(runtime_feedback or "").strip()
    if feedback:
        content += (
            "\n\n<system-reminder>\n"
            "<pico:runtime_feedback>\n"
            + feedback
            + "\n</pico:runtime_feedback>\n"
            "</system-reminder>"
        )
    return strip_pico_meta(replace_latest_plain_user(messages, content))


def make_tool_pair(
    *,
    name,
    arguments,
    tool_use_id,
    result_content,
    created_at,
    tool_status,
    effect_class,
    tool_change_id="",
    result_meta=None,
):
    result_meta = dict(result_meta or {})
    assistant = {
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": str(tool_use_id),
            "name": str(name),
            "input": dict(arguments),
        }],
        "_pico_meta": {
            "created_at": str(created_at),
            "tool_use_id": str(tool_use_id),
        },
    }
    result_block = {
        "type": "tool_result",
        "tool_use_id": str(tool_use_id),
        "content": str(result_content),
    }
    if tool_status in {"rejected", "error", "partial_success"}:
        result_block["is_error"] = True
    metadata = {
        "created_at": str(created_at),
        "tool_use_id": str(tool_use_id),
        "tool_status": str(tool_status),
        "effect_class": str(effect_class),
        **result_meta,
    }
    if tool_change_id:
        metadata["tool_change_id"] = str(tool_change_id)
    return assistant, {
        "role": "user",
        "content": [result_block],
        "_pico_meta": metadata,
    }


def message_content_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            parts.append(str(block.get("name", "")))
            parts.append(json.dumps(block.get("input", {}), sort_keys=True))
        elif block.get("type") == "tool_result":
            parts.append(str(block.get("content", "")))
    return "\n".join(parts)


def render_transcript(messages):
    lines = []
    for message in list(messages or []):
        role = str(message.get("role", ""))
        content = message.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                lines.append(
                    f"[assistant:tool_use id={block.get('id', '')}] "
                    f"{block.get('name', '')}("
                    f"{json.dumps(block.get('input', {}), sort_keys=True)})"
                )
            elif block.get("type") == "tool_result":
                lines.append(
                    f"[user:tool_result id={block.get('tool_use_id', '')}] "
                    f"{block.get('content', '')}"
                )
            elif block.get("type") == "text":
                lines.append(f"[{role}] {block.get('text', '')}")
    return "\n".join(lines)


def message_metrics(messages, token_of):
    values = [message_content_text(message) for message in list(messages or [])]
    return {
        "messages_count": len(values),
        "messages_chars": sum(len(value) for value in values),
        "messages_tokens": sum(int(token_of(value)) for value in values),
    }


def _tool_block(message, expected_type, expected_role):
    if message.get("role") != expected_role:
        raise MessageValidationError(
            f"{expected_type} must use role={expected_role}"
        )
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1:
        raise MessageValidationError(
            f"{expected_type} message must contain exactly one block"
        )
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != expected_type:
        raise MessageValidationError(f"invalid {expected_type} block")
    return block


def validate_messages(messages, *, require_meta):
    if not isinstance(messages, list):
        raise MessageValidationError("messages must be a list")
    seen_ids = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            raise MessageValidationError("message must be an object")
        role = message.get("role")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            raise MessageValidationError("message role must be user or assistant")
        if require_meta and not isinstance(message.get("_pico_meta"), dict):
            raise MessageValidationError("_pico_meta must be an object")
        if not require_meta and "_pico_meta" in message and not isinstance(
            message.get("_pico_meta"), dict
        ):
            raise MessageValidationError("_pico_meta must be an object")
        content = message.get("content")
        if isinstance(content, str):
            index += 1
            continue
        if not isinstance(content, list) or not content:
            raise MessageValidationError("message content must be a string or blocks")
        first_type = content[0].get("type") if isinstance(content[0], dict) else ""
        if first_type == "tool_use":
            block = _tool_block(message, "tool_use", "assistant")
            tool_use_id = block.get("id")
            if (
                not isinstance(tool_use_id, str)
                or not tool_use_id
                or tool_use_id in seen_ids
                or not isinstance(block.get("name"), str)
                or not block.get("name")
                or not isinstance(block.get("input"), dict)
            ):
                raise MessageValidationError("invalid or duplicate tool_use")
            seen_ids.add(tool_use_id)
            if index + 1 >= len(messages):
                raise MessageValidationError("orphan tool_use")
            result_message = messages[index + 1]
            if not isinstance(result_message, dict):
                raise MessageValidationError("message must be an object")
            if require_meta and not isinstance(result_message.get("_pico_meta"), dict):
                raise MessageValidationError("_pico_meta must be an object")
            if not require_meta and "_pico_meta" in result_message and not isinstance(
                result_message.get("_pico_meta"), dict
            ):
                raise MessageValidationError("_pico_meta must be an object")
            result = _tool_block(result_message, "tool_result", "user")
            if result.get("tool_use_id") != tool_use_id:
                raise MessageValidationError("tool_result id does not match")
            index += 2
            continue
        if first_type == "tool_result":
            raise MessageValidationError("orphan tool_result")
        if any(
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            for block in content
        ):
            raise MessageValidationError("unsupported content block")
        index += 1
