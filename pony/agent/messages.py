"""Pure operations for Pony's canonical message transcript."""

from __future__ import annotations

import json


class MessageValidationError(ValueError):
    """A canonical transcript violates the v3 message contract."""


def _valid_provider_state_item(item):
    if not isinstance(item, dict):
        return False
    item_type = item.get("type")
    if item_type == "reasoning":
        if any(
            key not in {
                "id",
                "type",
                "encrypted_content",
                "summary",
                "content",
                "status",
            }
            for key in item
        ):
            return False
        encrypted = item.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted:
            return False
        return "content" not in item or isinstance(item["content"], list)
    if item_type == "thinking":
        return (
            set(item) == {"type", "thinking", "signature"}
            and isinstance(item.get("thinking"), str)
            and isinstance(item.get("signature"), str)
            and bool(item["signature"])
        )
    if item_type == "redacted_thinking":
        return (
            set(item) == {"type", "data"}
            and isinstance(item.get("data"), str)
            and bool(item["data"])
        )
    return False


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


def strip_pony_meta(messages):
    cleaned = []
    for message in list(messages or []):
        item = dict(message)
        item.pop("_pony_meta", None)
        cleaned.append(item)
    return cleaned


def build_request_messages(messages, *, rendered_user, runtime_feedback=""):
    content = str(rendered_user)
    feedback = str(runtime_feedback or "").strip()
    if feedback:
        content += (
            "\n\n<system-reminder>\n"
            "<pony:runtime_feedback>\n"
            + feedback
            + "\n</pony:runtime_feedback>\n"
            "</system-reminder>"
        )
    return strip_pony_meta(replace_latest_plain_user(messages, content))


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
    provider_state=(),
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
        "_pony_meta": {
            "created_at": str(created_at),
            "tool_use_id": str(tool_use_id),
        },
    }
    if provider_state:
        assistant["_pony_provider_state"] = [
            dict(item) for item in provider_state
        ]
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
        "_pony_meta": metadata,
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


def tool_event_metrics(messages):
    name_counts = {}
    status_counts = {}
    event_count = 0
    for message in list(messages or []):
        content = message.get("content")
        if (
            message.get("role") == "assistant"
            and isinstance(content, list)
            and content
            and content[0].get("type") == "tool_use"
        ):
            name = str(content[0].get("name", "") or "")
            event_count += 1
            if name:
                name_counts[name] = name_counts.get(name, 0) + 1
        if (
            message.get("role") == "user"
            and isinstance(content, list)
            and content
            and content[0].get("type") == "tool_result"
        ):
            metadata = message.get("_pony_meta", {})
            status = (
                str(metadata.get("tool_status", "") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "event_count": event_count,
        "name_counts": name_counts,
        "status_counts": status_counts,
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
        if require_meta and not isinstance(message.get("_pony_meta"), dict):
            raise MessageValidationError("_pony_meta must be an object")
        if not require_meta and "_pony_meta" in message and not isinstance(
            message.get("_pony_meta"), dict
        ):
            raise MessageValidationError("_pony_meta must be an object")
        content = message.get("content")
        if isinstance(content, str):
            if "_pony_provider_state" in message:
                raise MessageValidationError(
                    "provider state requires an assistant tool_use"
                )
            index += 1
            continue
        if not isinstance(content, list) or not content:
            raise MessageValidationError("message content must be a string or blocks")
        first_type = content[0].get("type") if isinstance(content[0], dict) else ""
        if first_type == "tool_use":
            block = _tool_block(message, "tool_use", "assistant")
            provider_state = message.get("_pony_provider_state")
            if provider_state is not None:
                if (
                    not isinstance(provider_state, list)
                    or len(provider_state) > 32
                    or any(not _valid_provider_state_item(item) for item in provider_state)
                ):
                    raise MessageValidationError("invalid provider state")
                try:
                    provider_state_size = len(
                        json.dumps(provider_state, ensure_ascii=False).encode("utf-8")
                    )
                except (TypeError, ValueError):
                    raise MessageValidationError("invalid provider state") from None
                if provider_state_size > 1024 * 1024:
                    raise MessageValidationError("provider state too large")
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
            if require_meta and not isinstance(result_message.get("_pony_meta"), dict):
                raise MessageValidationError("_pony_meta must be an object")
            if not require_meta and "_pony_meta" in result_message and not isinstance(
                result_message.get("_pony_meta"), dict
            ):
                raise MessageValidationError("_pony_meta must be an object")
            if "_pony_provider_state" in result_message:
                raise MessageValidationError(
                    "provider state requires an assistant tool_use"
                )
            result = _tool_block(result_message, "tool_result", "user")
            if result.get("tool_use_id") != tool_use_id:
                raise MessageValidationError("tool_result id does not match")
            index += 2
            continue
        if first_type == "tool_result":
            raise MessageValidationError("orphan tool_result")
        if "_pony_provider_state" in message:
            raise MessageValidationError(
                "provider state requires an assistant tool_use"
            )
        if any(
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            for block in content
        ):
            raise MessageValidationError("unsupported content block")
        index += 1
