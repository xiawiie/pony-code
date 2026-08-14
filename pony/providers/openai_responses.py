"""OpenAI Responses native provider adapter."""

from copy import deepcopy
import json
import urllib.request

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _extract_usage_cache_details,
    _iter_sse_events,
    _open_provider_request,
    _open_provider_response,
    _provider_auth_headers,
    _provider_protocol_error,
    _model_binding,
    _model_runtime_metadata,
    _record_effective_model,
    _resource_url,
    _validate_number,
    _StreamCallbacks,
    _stream_failure_after_commit,
)
from .response import Response, StopReason
from .openai_wire import (
    OPENAI_USER_AGENT,
    drop_optional_null_arguments,
    prepare_function_tools,
    render_system_instructions,
)


MAX_PROVIDER_STATE_ITEMS = 32
MAX_PROVIDER_STATE_BYTES = 1024 * 1024


def _responses_tools(tools, *, strict):
    prepared, optional_by_name = prepare_function_tools(tools, strict=strict)
    return [
        {
            "type": "function",
            **tool,
        }
        for tool in prepared
    ], optional_by_name


def _validated_provider_state(value):
    if value in (None, (), []):
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_PROVIDER_STATE_ITEMS:
        raise _provider_protocol_error(
            "OpenAI",
            stage="response_decode",
            reason="response_shape_invalid",
        )
    prepared = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        encrypted = item.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted:
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        if any(
            key
            not in {
                "id",
                "type",
                "encrypted_content",
                "summary",
                "content",
                "status",
            }
            for key in item
        ):
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        if "content" in item and not isinstance(item["content"], list):
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            )
        copied = deepcopy(item)
        try:
            encoded = json.dumps(copied, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        prepared.append((copied, len(encoded)))
    if sum(size for _item, size in prepared) > MAX_PROVIDER_STATE_BYTES:
        raise _provider_protocol_error(
            "OpenAI",
            stage="response_decode",
            reason="response_shape_invalid",
        )
    return [item for item, _size in prepared]


def _canonical_input(messages, *, replay_reasoning):
    output = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            if role not in {"user", "assistant"}:
                raise ValueError("invalid message role")
            output.append({"role": role, "content": content})
            continue
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("canonical tool message must have one block")
        block = content[0]
        if not isinstance(block, dict):
            raise ValueError("message block must be an object")
        if role == "assistant" and block.get("type") == "tool_use":
            if replay_reasoning:
                output.extend(
                    _validated_provider_state(message.get("_pony_provider_state", []))
                )
            output.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(
                        block.get("input"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
            continue
        if role == "user" and block.get("type") == "tool_result":
            output.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id"),
                    "output": str(block.get("content", "")),
                }
            )
            continue
        raise ValueError("unsupported canonical message")
    return output


def _response_content(data, *, optional_by_name, preserve_reasoning):
    output = data.get("output")
    if not isinstance(output, list) or not all(
        isinstance(item, dict) for item in output
    ):
        raise ValueError("output must be a list of objects")
    content = []
    provider_state = []
    refusal = False
    for item in output:
        item_type = item.get("type")
        if item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if preserve_reasoning and encrypted is not None:
                provider_state.extend(_validated_provider_state([item]))
            continue
        if item_type == "function_call":
            name = item.get("name")
            call_id = item.get("call_id")
            arguments = item.get("arguments")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(call_id, str)
                or not call_id
            ):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_call_shape_invalid",
                )
            if not isinstance(arguments, str):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                )
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                ) from None
            if not isinstance(parsed, dict):
                raise _provider_protocol_error(
                    "OpenAI",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                )
            drop_optional_null_arguments(parsed, optional_by_name.get(name, set()))
            content.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": parsed,
                }
            )
            continue
        if item_type != "message":
            raise ValueError("unsupported output item")
        blocks = item.get("content")
        if not isinstance(blocks, list) or not all(
            isinstance(block, dict) for block in blocks
        ):
            raise ValueError("message content must be a list")
        for block in blocks:
            block_type = block.get("type")
            if block_type == "output_text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ValueError("output text must be text")
                content.append({"type": "text", "text": text})
            elif block_type == "refusal":
                text = block.get("refusal")
                if not isinstance(text, str):
                    raise ValueError("refusal must be text")
                content.append({"type": "text", "text": text})
                refusal = True
            else:
                raise ValueError("unsupported message content")
    return content, provider_state, refusal


def _stop_reason(data, content, refusal):
    status = data.get("status")
    if status is not None and not isinstance(status, str):
        raise ValueError("status must be a string")
    incomplete = data.get("incomplete_details")
    if incomplete is not None and not isinstance(incomplete, dict):
        raise ValueError("incomplete details must be an object")
    reason = (incomplete or {}).get("reason")
    if reason == "max_output_tokens":
        return StopReason.MAX_TOKENS
    if reason == "content_filter" or refusal:
        return StopReason.REFUSAL
    if incomplete is not None or status == "incomplete":
        return StopReason.UNKNOWN
    if status not in {None, "completed"}:
        return StopReason.UNKNOWN
    if any(block.get("type") == "tool_use" for block in content):
        return StopReason.TOOL_USE
    return StopReason.END_TURN


def _responses_request(client, *, system, tools, messages, max_tokens, stream):
    _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
    auth_headers = _provider_auth_headers(
        client.base_url,
        client.api_key,
        auth_mode=client.auth_mode,
        family="OpenAI",
    )
    strict = bool(client.capabilities.get("strict_tools"))
    prepared_tools, optional_by_name = _responses_tools(tools, strict=strict)
    replay_reasoning = bool(client.capabilities.get("reasoning_replay"))
    payload = {
        "model": client.model,
        "instructions": render_system_instructions(system),
        "input": _canonical_input(messages, replay_reasoning=replay_reasoning),
        "max_output_tokens": max_tokens,
        "store": False,
        "stream": stream,
    }
    if prepared_tools:
        payload["tools"] = prepared_tools
    if client.temperature is not None:
        payload["temperature"] = client.temperature
    if client.capabilities.get("parallel_tool_control"):
        payload["parallel_tool_calls"] = False
    if replay_reasoning:
        payload["include"] = ["reasoning.encrypted_content"]
    request = urllib.request.Request(
        _resource_url(client.base_url, "responses"),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": OPENAI_USER_AGENT,
            **auth_headers,
        },
        method="POST",
    )
    return request, optional_by_name, replay_reasoning


def _decode_responses_response(
    client,
    data,
    response_headers,
    *,
    optional_by_name,
    replay_reasoning,
):
    if "choices" in data or data.get("error") or data.get("status") == "failed":
        raise ValueError("not a successful Responses object")
    content, provider_state, refusal = _response_content(
        data,
        optional_by_name=optional_by_name,
        preserve_reasoning=replay_reasoning,
    )
    usage = _extract_usage_cache_details(data)
    _record_effective_model(client, data)
    request_id = response_headers.get("x-request-id") or data.get("id")
    if isinstance(request_id, str) and request_id:
        usage["request_id"] = request_id
    return Response(
        stop_reason=_stop_reason(data, content, refusal),
        content=content,
        usage=usage,
        provider_state=provider_state,
    )


def _responses_stream_object(event_name, payload):
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        raise ValueError("invalid Responses stream event") from None
    if not isinstance(data, dict):
        raise ValueError("invalid Responses stream event")
    item_type = data.get("type")
    if (
        not isinstance(item_type, str)
        or not item_type.startswith("response.") and item_type != "error"
        or event_name is not None and event_name != item_type
    ):
        raise ValueError("invalid Responses stream event")
    return item_type, data


def _record_responses_item(seen_items, data, *, require_existing):
    index = data.get("output_index")
    item = data.get("item")
    if type(index) is not int or index < 0 or not isinstance(item, dict):
        raise ValueError("invalid Responses output item")
    identity = (item.get("id"), item.get("type"))
    if not all(isinstance(value, str) and value for value in identity):
        raise ValueError("invalid Responses output item")
    if require_existing:
        if seen_items.get(index) != identity:
            raise ValueError("Responses output item identity changed")
    elif index in seen_items or index != len(seen_items):
        raise ValueError("invalid Responses output index")
    else:
        seen_items[index] = identity


def _validate_completed_items(response, seen_items):
    output = response.get("output")
    if not isinstance(output, list):
        raise ValueError("invalid completed Responses output")
    for index, identity in seen_items.items():
        if index >= len(output) or not isinstance(output[index], dict):
            raise ValueError("completed Responses item missing")
        item = output[index]
        if (item.get("id"), item.get("type")) != identity:
            raise ValueError("completed Responses item identity changed")


def _validate_responses_sequence(state, data):
    sequence = data.get("sequence_number")
    if sequence is None:
        return
    if type(sequence) is not int or sequence <= state["last_sequence"]:
        raise ValueError("invalid Responses stream sequence")
    state["last_sequence"] = sequence


def _read_responses_stream(response_stream, callbacks):
    state = {"last_sequence": -1}
    seen_items = {}
    completed = None
    for event_name, payload in _iter_sse_events(response_stream, family="OpenAI"):
        item_type, data = _responses_stream_object(event_name, payload)
        _validate_responses_sequence(state, data)
        callbacks.commit()
        if item_type in {"error", "response.failed", "response.incomplete"}:
            raise ProviderTransportError("OpenAI error: backend_error", code="backend_error")
        if item_type == "response.output_item.added":
            _record_responses_item(seen_items, data, require_existing=False)
        elif item_type == "response.output_item.done":
            _record_responses_item(seen_items, data, require_existing=True)
        elif item_type == "response.output_text.delta":
            output_index = data.get("output_index")
            content_index = data.get("content_index")
            if (
                type(output_index) is not int
                or type(content_index) is not int
                or output_index < 0
                or content_index < 0
                or seen_items.get(output_index, (None, None))[1] != "message"
            ):
                raise ValueError("invalid Responses text delta target")
            delta = data.get("delta")
            if not isinstance(delta, str):
                raise ValueError("invalid Responses text delta")
            callbacks.text(delta)
        elif item_type == "response.completed":
            completed = data.get("response")
            if not isinstance(completed, dict):
                raise ValueError("invalid completed Responses object")
            _validate_completed_items(completed, seen_items)
            break
    if completed is None:
        raise ValueError("Responses stream missing response.completed")
    return completed


class OpenAIResponsesModelClient:
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

        self.model = str(model)
        self.base_url = validate_api_base(base_url)
        self.api_key = str(api_key or "")
        self.auth_mode = auth_mode or "bearer"
        self.capabilities = dict(capabilities or {})
        self.temperature = (
            None
            if temperature is None
            else _validate_number("temperature", temperature, minimum=0, maximum=2)
        )
        self.timeout = _validate_number("timeout", timeout, minimum=0.001)
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.provider_binding = _model_binding(
            "openai_responses",
            self.model,
            self.base_url,
        )
        self.provider_metadata = _model_runtime_metadata(
            "openai_responses",
            self.model,
        )

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
        request, optional_by_name, replay_reasoning = _responses_request(
            self,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            stream=False,
        )
        response_body, response_headers = _open_provider_request(
            self,
            request,
            family="OpenAI",
            retryable=True,
        )
        try:
            data = _decode_json_object(response_body)
            response = _decode_responses_response(
                self,
                data,
                response_headers,
                optional_by_name=optional_by_name,
                replay_reasoning=replay_reasoning,
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "OpenAI",
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
        del cache_breakpoints
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        callbacks = _StreamCallbacks(on_stream_committed, on_text_delta)
        request, optional_by_name, replay_reasoning = _responses_request(
            self,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
        )
        try:
            with _open_provider_response(
                self,
                request,
                family="OpenAI",
                retryable=True,
            ) as response_stream:
                headers = getattr(response_stream, "headers", {}) or {}
                data = _read_responses_stream(response_stream, callbacks)
            response = _decode_responses_response(
                self,
                data,
                headers,
                optional_by_name=optional_by_name,
                replay_reasoning=replay_reasoning,
            )
        except ProviderTransportError as exc:
            if callbacks.committed:
                raise _stream_failure_after_commit(exc) from None
            raise
        except Exception:
            raise _provider_protocol_error(
                "OpenAI",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = response.usage
        return response
