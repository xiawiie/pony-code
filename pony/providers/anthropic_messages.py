"""Anthropic Messages native provider adapter."""

from copy import deepcopy
import json
import urllib.request

from pony.agent.messages import strip_pony_meta

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _mapping_or_empty,
    _open_provider_request,
    _open_provider_response,
    _iter_sse_events,
    _optional_int,
    _model_binding,
    _model_runtime_metadata,
    _record_effective_model,
    _validate_number,
    _provider_auth_headers,
    _provider_protocol_error,
    _resource_url,
    _StreamCallbacks,
    _stream_failure_after_commit,
)
from .response import Response, StopReason


def _validated_anthropic_provider_state(value):
    if value in (None, (), []):
        return []
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise ValueError("invalid Anthropic provider state")
    prepared = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid Anthropic provider state")
        item_type = item.get("type")
        if item_type == "thinking":
            valid = (
                set(item) == {"type", "thinking", "signature"}
                and isinstance(item.get("thinking"), str)
                and isinstance(item.get("signature"), str)
                and bool(item["signature"])
            )
        elif item_type == "redacted_thinking":
            valid = (
                set(item) == {"type", "data"}
                and isinstance(item.get("data"), str)
                and bool(item["data"])
            )
        else:
            valid = False
        if not valid:
            raise ValueError("invalid Anthropic provider state")
        prepared.append(deepcopy(item))
    try:
        encoded = json.dumps(prepared, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("invalid Anthropic provider state") from None
    if len(encoded) > 1024 * 1024:
        raise ValueError("Anthropic provider state too large")
    return prepared


def _anthropic_content(data):
    content = data.get("content")
    if not isinstance(content, list) or not all(
        isinstance(item, dict) for item in content
    ):
        raise ValueError("content must be a list of objects")
    action_content = []
    provider_state = []
    seen_action_content = False
    for item in content:
        item_type = item.get("type")
        if item_type == "text":
            if not isinstance(item.get("text"), str):
                raise ValueError("content text must be a string")
            seen_action_content = True
            action_content.append(deepcopy(item))
        elif item_type == "tool_use":
            if (
                not isinstance(item.get("id"), str)
                or not item["id"]
                or not isinstance(item.get("name"), str)
                or not item["name"]
            ):
                raise _provider_protocol_error(
                    "Anthropic",
                    stage="tool_call",
                    reason="tool_call_shape_invalid",
                )
            if not isinstance(item.get("input"), dict):
                raise _provider_protocol_error(
                    "Anthropic",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                )
            seen_action_content = True
            action_content.append(deepcopy(item))
        elif item_type in {"thinking", "redacted_thinking"}:
            if seen_action_content:
                raise ValueError("thinking blocks must precede response content")
            provider_state.extend(_validated_anthropic_provider_state([item]))
        else:
            raise ValueError("unsupported content block")
    return action_content, provider_state


def _anthropic_tools(tools, *, strict):
    prepared = []
    for tool in list(tools or []):
        if not isinstance(tool, dict):
            raise ValueError("tool must be an object")
        item = {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "input_schema": dict(tool.get("input_schema") or {}),
        }
        if strict:
            item["strict"] = True
        prepared.append(item)
    return prepared


def _extract_anthropic_usage_cache_details(data):
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    usage = _mapping_or_empty(data.get("usage"))
    input_tokens = _optional_int(usage.get("input_tokens"))
    output_tokens = _optional_int(usage.get("output_tokens"))
    reported_total_tokens = _optional_int(usage.get("total_tokens"))
    cache_creation_tokens = _optional_int(usage.get("cache_creation_input_tokens")) or 0
    cache_read_tokens = _optional_int(usage.get("cache_read_input_tokens")) or 0
    total_tokens = reported_total_tokens
    if input_tokens is not None and output_tokens is not None:
        total_tokens = (
            input_tokens + cache_creation_tokens + cache_read_tokens + output_tokens
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cache_read_tokens,
        "cache_hit": cache_read_tokens > 0,
        "cache_creation_input_tokens": cache_creation_tokens,
        "cache_read_input_tokens": cache_read_tokens,
    }


_ANTHROPIC_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
}


def _anthropic_request(client, *, system, tools, messages, max_tokens, cache_breakpoints, stream):
    _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
    auth_headers = _provider_auth_headers(
        client.base_url,
        client.api_key,
        auth_mode=client.auth_mode,
        family="Anthropic",
    )
    messages = strip_pony_meta(messages)
    breakpoints = (
        set(cache_breakpoints or []) if client.supports_prompt_cache else set()
    )
    prepared_messages = []
    for index, message in enumerate(messages):
        content = message["content"]
        provider_state = _validated_anthropic_provider_state(
            message.get("_pony_provider_state")
        )
        if provider_state:
            if (
                message.get("role") != "assistant"
                or not isinstance(content, list)
                or not content
                or not all(
                    isinstance(block, dict) and block.get("type") == "tool_use"
                    for block in content
                )
            ):
                raise ValueError("Anthropic provider state requires assistant tool_use")
            content = [*provider_state, *deepcopy(content)]
        if index in breakpoints:
            if isinstance(content, str):
                content = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                content = list(content)
                if content:
                    last = dict(content[-1])
                    last["cache_control"] = {"type": "ephemeral"}
                    content[-1] = last
        prepared_messages.append({"role": message["role"], "content": content})
    prepared_system = system
    if not client.supports_prompt_cache:
        prepared_system = []
        for block in system:
            copied = dict(block)
            copied.pop("cache_control", None)
            prepared_system.append(copied)
    prepared_tools = _anthropic_tools(
        tools,
        strict=bool(client.capabilities.get("strict_tools")),
    )
    payload = {
        "model": client.model,
        "system": prepared_system,
        "messages": prepared_messages,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True
    if prepared_tools:
        payload["tools"] = prepared_tools
        if client.capabilities.get("parallel_tool_control"):
            payload["tool_choice"] = {
                "type": "auto",
                "disable_parallel_tool_use": True,
            }
    if client.temperature is not None:
        payload["temperature"] = client.temperature
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        **auth_headers,
    }
    if stream:
        headers["accept"] = "text/event-stream"
    return urllib.request.Request(
        _resource_url(client.base_url, "messages"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _decode_anthropic_response(client, data, response_headers):
    if data.get("error"):
        raise ProviderTransportError(
            "Anthropic error: backend_error",
            code="backend_error",
        )
    raw_stop_reason = data.get("stop_reason")
    if not isinstance(raw_stop_reason, str):
        raise ValueError("stop reason must be a string")
    if raw_stop_reason == "pause_turn":
        raise ProviderTransportError(
            "Anthropic error: unsupported_stop_reason",
            code="unsupported_stop_reason",
        )
    content, provider_state = _anthropic_content(data)
    usage = _extract_anthropic_usage_cache_details(data)
    _record_effective_model(client, data)
    request_id = response_headers.get("request-id") or response_headers.get(
        "x-request-id"
    )
    if isinstance(request_id, str) and request_id:
        usage["request_id"] = request_id
    return Response(
        stop_reason=_ANTHROPIC_STOP_REASONS.get(raw_stop_reason, StopReason.UNKNOWN),
        content=content,
        usage=usage,
        provider_state=provider_state,
    )


def _anthropic_stream_object(event_name, payload):
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        raise ValueError("invalid Anthropic stream event") from None
    if not isinstance(data, dict):
        raise ValueError("invalid Anthropic stream event")
    item_type = data.get("type")
    if not isinstance(item_type, str) or (event_name is not None and event_name != item_type):
        raise ValueError("invalid Anthropic stream event")
    return item_type, data


def _anthropic_content_start(state, data):
    index = data.get("index")
    block = data.get("content_block")
    if type(index) is not int or index != len(state["content"]) or not isinstance(block, dict):
        raise ValueError("invalid Anthropic content block")
    item = deepcopy(block)
    item_type = item.get("type")
    if item_type == "text" and isinstance(item.get("text"), str):
        pass
    elif item_type == "tool_use" and (
        isinstance(item.get("id"), str)
        and item["id"]
        and isinstance(item.get("name"), str)
        and item["name"]
        and isinstance(item.get("input"), dict)
    ):
        item["_partial_json"] = ""
    elif item_type == "thinking" and (
        isinstance(item.get("thinking"), str) and isinstance(item.get("signature", ""), str)
    ):
        item.setdefault("signature", "")
    elif item_type == "redacted_thinking" and isinstance(item.get("data"), str):
        pass
    else:
        raise ValueError("invalid Anthropic content block")
    state["content"].append(item)
    state["open_index"] = index


def _anthropic_content_delta(state, data, callbacks):
    index = data.get("index")
    delta = data.get("delta")
    if type(index) is not int or index != state.get("open_index") or not isinstance(delta, dict):
        raise ValueError("invalid Anthropic content delta")
    block = state["content"][index]
    delta_type = delta.get("type")
    if delta_type == "text_delta" and block.get("type") == "text":
        text = delta.get("text")
        if not isinstance(text, str):
            raise ValueError("invalid Anthropic text delta")
        block["text"] += text
        callbacks.text(text)
    elif delta_type == "input_json_delta" and block.get("type") == "tool_use":
        partial = delta.get("partial_json")
        if not isinstance(partial, str):
            raise ValueError("invalid Anthropic tool delta")
        block["_partial_json"] += partial
    elif delta_type == "thinking_delta" and block.get("type") == "thinking":
        thinking = delta.get("thinking")
        if not isinstance(thinking, str):
            raise ValueError("invalid Anthropic thinking delta")
        block["thinking"] += thinking
    elif delta_type == "signature_delta" and block.get("type") == "thinking":
        signature = delta.get("signature")
        if not isinstance(signature, str):
            raise ValueError("invalid Anthropic signature delta")
        block["signature"] += signature
    else:
        raise ValueError("invalid Anthropic content delta")


def _anthropic_content_stop(state, data):
    index = data.get("index")
    if type(index) is not int or index != state.get("open_index"):
        raise ValueError("invalid Anthropic content stop")
    block = state["content"][index]
    partial = block.pop("_partial_json", None)
    if partial is not None:
        if partial:
            try:
                parsed = json.loads(partial)
            except (TypeError, ValueError):
                raise _provider_protocol_error(
                    "Anthropic",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                ) from None
            if not isinstance(parsed, dict):
                raise _provider_protocol_error(
                    "Anthropic",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                )
            block["input"] = parsed
    state["open_index"] = None


def _read_anthropic_stream(response, callbacks):
    state = {"message": None, "content": [], "open_index": None, "stopped": False}
    for event_name, payload in _iter_sse_events(response, family="Anthropic"):
        item_type, data = _anthropic_stream_object(event_name, payload)
        callbacks.commit()
        if item_type == "message_start":
            message = data.get("message")
            if state["message"] is not None or not isinstance(message, dict):
                raise ValueError("invalid Anthropic message start")
            state["message"] = deepcopy(message)
            state["message"]["content"] = state["content"]
        elif item_type == "content_block_start":
            if state["message"] is None or state["open_index"] is not None:
                raise ValueError("invalid Anthropic content start")
            _anthropic_content_start(state, data)
        elif item_type == "content_block_delta":
            _anthropic_content_delta(state, data, callbacks)
        elif item_type == "content_block_stop":
            _anthropic_content_stop(state, data)
        elif item_type == "message_delta":
            delta = data.get("delta")
            usage = data.get("usage")
            if state["message"] is None or not isinstance(delta, dict) or not isinstance(usage, dict):
                raise ValueError("invalid Anthropic message delta")
            if not delta or not set(delta).issubset({"stop_reason", "stop_sequence"}):
                raise ValueError("invalid Anthropic message delta")
            state["message"].update(delta)
            current_usage = state["message"].setdefault("usage", {})
            if not isinstance(current_usage, dict):
                raise ValueError("invalid Anthropic usage")
            current_usage.update(usage)
        elif item_type == "ping":
            continue
        elif item_type == "error":
            raise ProviderTransportError("Anthropic error: backend_error", code="backend_error")
        elif item_type == "message_stop":
            if state["message"] is None or state["open_index"] is not None:
                raise ValueError("invalid Anthropic message stop")
            state["stopped"] = True
            break
        else:
            raise ValueError("unsupported Anthropic stream event")
    if not state["stopped"]:
        raise ValueError("Anthropic stream missing message_stop")
    return state["message"]


class AnthropicMessagesModelClient:
    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        *,
        auth_mode=None,
        capabilities=None,
    ):
        from pony.config.model import validate_api_base

        self.model = model
        self.base_url = validate_api_base(base_url)
        self.api_key = api_key
        self.auth_mode = auth_mode or "x-api-key"
        self.capabilities = dict(capabilities or {})
        self.temperature = (
            None
            if temperature is None
            else _validate_number("temperature", temperature, minimum=0, maximum=1)
        )
        self.timeout = _validate_number("timeout", timeout, minimum=0.001)
        self.supports_prompt_cache = bool(self.capabilities.get("prompt_cache", False))
        self.provider_binding = _model_binding(
            "anthropic_messages",
            self.model,
            self.base_url,
        )
        self.provider_metadata = _model_runtime_metadata(
            "anthropic_messages",
            self.model,
        )
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        request = _anthropic_request(
            self,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            cache_breakpoints=cache_breakpoints,
            stream=False,
        )
        response_body, response_headers = _open_provider_request(
            self,
            request,
            family="Anthropic",
            retryable=True,
        )
        try:
            data = _decode_json_object(response_body)
        except Exception:
            raise _provider_protocol_error(
                "Anthropic",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        try:
            response = _decode_anthropic_response(self, data, response_headers)
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "Anthropic",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = response.usage
        return response

    def complete_stream(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
        on_stream_committed,
        on_text_delta,
    ):
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        callbacks = _StreamCallbacks(on_stream_committed, on_text_delta)
        request = _anthropic_request(
            self,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            cache_breakpoints=cache_breakpoints,
            stream=True,
        )
        try:
            with _open_provider_response(
                self,
                request,
                family="Anthropic",
                retryable=True,
            ) as response_stream:
                headers = getattr(response_stream, "headers", {}) or {}
                data = _read_anthropic_stream(response_stream, callbacks)
            response = _decode_anthropic_response(self, data, headers)
        except ProviderTransportError as exc:
            if callbacks.committed:
                raise _stream_failure_after_commit(exc) from None
            raise
        except Exception:
            raise _provider_protocol_error(
                "Anthropic",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = response.usage
        return response
