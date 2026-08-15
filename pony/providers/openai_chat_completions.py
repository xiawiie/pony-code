"""OpenAI Chat Completions native provider adapter."""

import json
import urllib.request

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _extract_usage_cache_details,
    _iter_sse_events,
    _model_binding,
    _model_runtime_metadata,
    _open_provider_request,
    _open_provider_response,
    _provider_protocol_error,
    _provider_auth_headers,
    _record_effective_model,
    _resource_url,
    _validate_number,
    _StreamCallbacks,
    _stream_failure_after_commit,
)
from .openai_wire import (
    OPENAI_USER_AGENT,
    drop_optional_null_arguments,
    prepare_function_tools,
    render_system_instructions,
)
from .response import Response, StopReason


def _chat_tools(tools, *, strict):
    prepared, optional_by_name = prepare_function_tools(tools, strict=strict)
    return [
        {
            "type": "function",
            "function": tool,
        }
        for tool in prepared
    ], optional_by_name


def _chat_messages(system, messages):
    output = []
    instructions = render_system_instructions(system)
    if instructions:
        output.append({"role": "system", "content": instructions})
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
            call_id = block.get("id")
            name = block.get("name")
            arguments = block.get("input")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
            ):
                raise ValueError("invalid canonical tool_use")
            output.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            },
                        }
                    ],
                }
            )
            continue
        if role == "user" and block.get("type") == "tool_result":
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("invalid canonical tool_result")
            output.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(block.get("content", "")),
                }
            )
            continue
        raise ValueError("unsupported canonical message")
    return output


def _contains_tool_result(messages):
    return any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in message["content"]
        )
        for message in list(messages or [])
    )


def _chat_content(data, optional_by_name):
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("choices must contain one object")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ValueError("choice message must be an object")
    message = choice["message"]
    if message.get("role") not in {None, "assistant"}:
        raise ValueError("invalid response role")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("response content must be text or null")
    tool_calls = message.get("tool_calls", [])
    if tool_calls is None:
        tool_calls = []
    if not isinstance(tool_calls, list) or not all(
        isinstance(item, dict) for item in tool_calls
    ):
        raise _provider_protocol_error(
            "OpenAI Chat",
            stage="tool_call",
            reason="tool_call_shape_invalid",
        )
    result = []
    if content:
        result.append({"type": "text", "text": content})
    for item in tool_calls:
        function = item.get("function")
        if item.get("type") not in {None, "function"} or not isinstance(
            function, dict
        ):
            raise _provider_protocol_error(
                "OpenAI Chat",
                stage="tool_call",
                reason="tool_call_shape_invalid",
            )
        call_id = item.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
        ):
            raise _provider_protocol_error(
                "OpenAI Chat",
                stage="tool_call",
                reason="tool_call_shape_invalid",
            )
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (TypeError, ValueError):
                raise _provider_protocol_error(
                    "OpenAI Chat",
                    stage="tool_call",
                    reason="tool_arguments_invalid",
                ) from None
        elif isinstance(arguments, dict):
            parsed = dict(arguments)
        else:
            raise _provider_protocol_error(
                "OpenAI Chat",
                stage="tool_call",
                reason="tool_arguments_invalid",
            )
        if not isinstance(parsed, dict):
            raise _provider_protocol_error(
                "OpenAI Chat",
                stage="tool_call",
                reason="tool_arguments_invalid",
            )
        drop_optional_null_arguments(parsed, optional_by_name.get(name, set()))
        result.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": parsed,
            }
        )
    raw_reason = choice.get("finish_reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        raise ValueError("finish_reason must be text or null")
    stop_reason = {
        "stop": StopReason.END_TURN,
        "tool_calls": StopReason.TOOL_USE,
        "length": StopReason.MAX_TOKENS,
        "content_filter": StopReason.REFUSAL,
    }.get(raw_reason, StopReason.UNKNOWN)
    refusal = message.get("refusal")
    if refusal is not None:
        if not isinstance(refusal, str):
            raise ValueError("refusal must be text")
        if refusal and not content:
            result.append({"type": "text", "text": refusal})
        stop_reason = StopReason.REFUSAL
    if tool_calls:
        stop_reason = StopReason.TOOL_USE
    elif stop_reason == StopReason.TOOL_USE:
        raise _provider_protocol_error(
            "OpenAI Chat",
            stage="tool_call",
            reason="tool_call_missing",
        )
    return result, stop_reason


def _chat_request(client, *, system, tools, messages, max_tokens, stream):
    _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
    auth_headers = _provider_auth_headers(
        client.base_url,
        client.api_key,
        auth_mode=client.auth_mode,
        family="OpenAI Chat",
    )
    prepared_tools, optional_by_name = _chat_tools(
        tools,
        strict=bool(client.capabilities.get("strict_tools")),
    )
    payload = {
        "model": client.model,
        "messages": _chat_messages(system, messages),
        "stream": stream,
    }
    output_token_field = client.capabilities.get("output_token_field", "max_tokens")
    if output_token_field not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("unsupported Chat output token field")
    payload[output_token_field] = max_tokens
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if prepared_tools:
        payload["tools"] = prepared_tools
    if client.temperature is not None:
        payload["temperature"] = client.temperature
    if client.capabilities.get("parallel_tool_control"):
        payload["parallel_tool_calls"] = False
    return (
        urllib.request.Request(
            _resource_url(client.base_url, "chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "User-Agent": OPENAI_USER_AGENT,
                **auth_headers,
            },
            method="POST",
        ),
        optional_by_name,
        _contains_tool_result(messages),
    )


def _decode_chat_response(client, data, response_headers, optional_by_name):
    if data.get("error") or "output" in data:
        raise ValueError("not a successful Chat Completions object")
    content, stop_reason = _chat_content(data, optional_by_name)
    usage = _extract_usage_cache_details(data)
    _record_effective_model(client, data)
    request_id = response_headers.get("x-request-id") or data.get("id")
    if isinstance(request_id, str) and request_id:
        usage["request_id"] = request_id
    return Response(stop_reason=stop_reason, content=content, usage=usage)


def _chat_stream_object(event_name, payload):
    if event_name not in {None, "message"}:
        raise ValueError("invalid Chat stream event")
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        raise ValueError("invalid Chat stream event") from None
    if not isinstance(data, dict) or "output" in data:
        raise ValueError("invalid Chat stream event")
    if data.get("error"):
        return data, None
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) > 1:
        raise ValueError("invalid Chat stream choices")
    return data, choices


def _chat_stream_tool_calls(state, value):
    if not isinstance(value, list):
        raise ValueError("invalid Chat tool call delta")
    previous_index = -1
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid Chat tool call delta")
        index = item.get("index")
        if type(index) is not int or index <= previous_index or index < 0:
            raise ValueError("invalid Chat tool call index")
        previous_index = index
        calls = state["tool_calls"]
        if index not in calls:
            if index != len(calls):
                raise ValueError("invalid Chat tool call index")
            calls[index] = {"id": "", "type": "function", "name": "", "arguments": ""}
        call = calls[index]
        item_type = item.get("type")
        if item_type not in {None, "function"}:
            raise ValueError("invalid Chat tool call type")
        call_id = item.get("id")
        if call_id is not None:
            if not isinstance(call_id, str) or call["id"] and call["id"] != call_id:
                raise ValueError("Chat tool call identity changed")
            call["id"] = call_id
        function = item.get("function", {})
        if not isinstance(function, dict):
            raise ValueError("invalid Chat tool call function")
        name = function.get("name")
        arguments = function.get("arguments")
        if name is not None:
            if not isinstance(name, str):
                raise ValueError("invalid Chat tool call name")
            call["name"] += name
        if arguments is not None:
            if not isinstance(arguments, str):
                raise ValueError("invalid Chat tool arguments")
            call["arguments"] += arguments


def _chat_stream_choice(state, choice, callbacks):
    if not isinstance(choice, dict) or choice.get("index") != 0:
        raise ValueError("invalid Chat stream choice")
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        raise ValueError("invalid Chat stream delta")
    role = delta.get("role")
    if role not in {None, "assistant"}:
        raise ValueError("invalid Chat stream role")
    content = delta.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise ValueError("invalid Chat text delta")
        state["content"].append(content)
        callbacks.text(content)
    refusal = delta.get("refusal")
    if refusal is not None:
        if not isinstance(refusal, str):
            raise ValueError("invalid Chat refusal delta")
        state["refusal"].append(refusal)
    if "tool_calls" in delta:
        _chat_stream_tool_calls(state, delta["tool_calls"])
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None:
        if not isinstance(finish_reason, str):
            raise ValueError("invalid Chat finish reason")
        if state["finish_reason"] not in {None, finish_reason}:
            raise ValueError("Chat finish reason changed")
        state["finish_reason"] = finish_reason


def _chat_stream_response(state):
    tool_calls = []
    for index in range(len(state["tool_calls"])):
        call = state["tool_calls"][index]
        tool_calls.append(
            {
                "id": call["id"],
                "type": call["type"],
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
        )
    message = {"role": "assistant", "content": "".join(state["content"]) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    refusal = "".join(state["refusal"])
    if refusal:
        message["refusal"] = refusal
    data = {
        "choices": [{"index": 0, "message": message, "finish_reason": state["finish_reason"]}],
        "usage": state["usage"],
    }
    if state["id"] is not None:
        data["id"] = state["id"]
    if state["model"] is not None:
        data["model"] = state["model"]
    return data


def _read_chat_stream(response_stream, callbacks):
    state = {
        "content": [],
        "refusal": [],
        "tool_calls": {},
        "finish_reason": None,
        "usage": {},
        "id": None,
        "model": None,
        "saw_choice": False,
    }
    done = False
    for event_name, payload in _iter_sse_events(response_stream, family="OpenAI Chat"):
        if payload == "[DONE]":
            callbacks.commit()
            done = True
            break
        data, choices = _chat_stream_object(event_name, payload)
        callbacks.commit()
        if choices is None:
            raise ProviderTransportError(
                "OpenAI Chat error: backend_error", code="backend_error"
            )
        for name in ("id", "model"):
            value = data.get(name)
            if value is not None:
                if not isinstance(value, str) or state[name] not in {None, value}:
                    raise ValueError(f"Chat stream {name} changed")
                state[name] = value
        usage = data.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("invalid Chat stream usage")
            state["usage"] = usage
        if not choices:
            if usage is None:
                raise ValueError("empty Chat stream chunk")
            continue
        state["saw_choice"] = True
        _chat_stream_choice(state, choices[0], callbacks)
    if not done or not state["saw_choice"]:
        raise ValueError("Chat stream missing completion")
    return _chat_stream_response(state)


class OpenAIChatCompletionsModelClient:
    def __init__(
        self,
        model,
        base_url,
        api_key,
        temperature,
        timeout,
        *,
        auth_mode="bearer",
        capabilities=None,
    ):
        from pony.config.model import validate_api_base

        self.model = str(model)
        self.base_url = validate_api_base(base_url)
        self.api_key = str(api_key or "")
        self.auth_mode = str(auth_mode)
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
            "openai_chat_completions",
            self.model,
            self.base_url,
        )
        self.provider_metadata = _model_runtime_metadata(
            "openai_chat_completions",
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
        request, optional_by_name, contains_tool_result = _chat_request(
            self,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            response_body, response_headers = _open_provider_request(
                self,
                request,
                family="OpenAI Chat",
                retryable=True,
                detect_reasoning_replay=contains_tool_result,
            )
        except ProviderTransportError as exc:
            if contains_tool_result and exc.code == "http_4xx":
                raise ProviderTransportError(
                    "OpenAI Chat request failed during tool continuation",
                    code=exc.code,
                    http_status=exc.http_status,
                    retryable=False,
                    stage="tool_result",
                    protocol_reason=(
                        exc.protocol_reason or "tool_result_rejected"
                    ),
                    protocol_family="openai_chat_completions",
                ) from None
            raise
        try:
            data = _decode_json_object(response_body)
            response = _decode_chat_response(
                self, data, response_headers, optional_by_name
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "OpenAI Chat",
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
        request, optional_by_name, contains_tool_result = _chat_request(
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
                family="OpenAI Chat",
                retryable=True,
                detect_reasoning_replay=contains_tool_result,
            ) as response_stream:
                headers = getattr(response_stream, "headers", {}) or {}
                data = _read_chat_stream(response_stream, callbacks)
            response = _decode_chat_response(
                self, data, headers, optional_by_name
            )
        except ProviderTransportError as exc:
            if callbacks.committed:
                raise _stream_failure_after_commit(exc) from None
            if contains_tool_result and exc.code == "http_4xx":
                raise ProviderTransportError(
                    "OpenAI Chat request failed during tool continuation",
                    code=exc.code,
                    http_status=exc.http_status,
                    retryable=False,
                    stage="tool_result",
                    protocol_reason=(exc.protocol_reason or "tool_result_rejected"),
                    protocol_family="openai_chat_completions",
                ) from None
            raise
        except Exception:
            raise _provider_protocol_error(
                "OpenAI Chat",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = response.usage
        return response
