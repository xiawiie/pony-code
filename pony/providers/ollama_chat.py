"""Ollama Chat native provider adapter."""

from copy import deepcopy
import json
import urllib.request
import uuid

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _iter_ndjson_events,
    _open_provider_request,
    _open_provider_response,
    _optional_int,
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


def _ollama_tools(tools):
    prepared = []
    for tool in list(tools or []):
        if not isinstance(tool, dict):
            raise ValueError("tool must be an object")
        prepared.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": str(tool.get("description", "") or ""),
                    "parameters": dict(tool.get("input_schema") or {}),
                },
            }
        )
    return prepared


def _ollama_messages(system, messages):
    prepared = []
    system_text = "\n\n".join(
        str(block.get("text", ""))
        for block in list(system or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if system_text:
        prepared.append({"role": "system", "content": system_text})
    tool_names = {}
    for message in list(messages or []):
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            if role not in {"user", "assistant"}:
                raise ValueError("invalid message role")
            prepared.append({"role": role, "content": content})
            continue
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("canonical tool message must have one block")
        block = content[0]
        if not isinstance(block, dict):
            raise ValueError("message block must be an object")
        if role == "assistant" and block.get("type") == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            tool_names[tool_id] = name
            prepared.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": tool_id,
                            "function": {
                                "name": name,
                                "arguments": dict(block.get("input") or {}),
                            },
                        }
                    ],
                }
            )
            continue
        if role == "user" and block.get("type") == "tool_result":
            tool_id = block.get("tool_use_id")
            item = {
                "role": "tool",
                "content": str(block.get("content", "")),
            }
            name = tool_names.get(tool_id)
            if isinstance(name, str) and name:
                item["tool_name"] = name
            prepared.append(item)
            continue
        raise ValueError("unsupported canonical message")
    return prepared


def _ollama_content(data):
    message = data.get("message")
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    text = message.get("content", "")
    if not isinstance(text, str):
        raise ValueError("message content must be text")
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
        raise _provider_protocol_error(
            "Ollama",
            stage="tool_call",
            reason="tool_call_shape_invalid",
        )
    content = []
    if text.strip():
        content.append({"type": "text", "text": text})
    for call in calls:
        function = call.get("function")
        if not isinstance(function, dict):
            raise _provider_protocol_error(
                "Ollama",
                stage="tool_call",
                reason="tool_call_shape_invalid",
            )
        name = function.get("name")
        arguments = function.get("arguments")
        call_id = call.get("id")
        if (
            not isinstance(name, str)
            or not name
            or call_id is not None
            and (not isinstance(call_id, str) or not call_id)
        ):
            raise _provider_protocol_error(
                "Ollama",
                stage="tool_call",
                reason="tool_call_shape_invalid",
            )
        if not isinstance(arguments, dict):
            raise _provider_protocol_error(
                "Ollama",
                stage="tool_call",
                reason="tool_arguments_invalid",
            )
        content.append(
            {
                "type": "tool_use",
                "id": call_id or f"toolu_ollama_{uuid.uuid4().hex[:12]}",
                "name": name,
                "input": dict(arguments),
            }
        )
    return content


def _ollama_request(client, *, system, tools, messages, max_tokens, stream):
    _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
    auth_headers = _provider_auth_headers(
        client.host,
        client.api_key,
        auth_mode=client.auth_mode,
        family="Ollama",
    )
    prepared_tools = _ollama_tools(tools)
    payload = {
        "model": client.model,
        "messages": _ollama_messages(system, messages),
        "stream": stream,
        "think": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": client.temperature,
            "top_p": client.top_p,
        },
    }
    if prepared_tools:
        payload["tools"] = prepared_tools
    headers = {"Content-Type": "application/json", **auth_headers}
    if stream:
        headers["Accept"] = "application/x-ndjson"
    return urllib.request.Request(
        _resource_url(client.host, "api/chat"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _decode_ollama_response(client, data, response_headers):
    if data.get("error") or data.get("done") is not True:
        raise ValueError("unsuccessful Ollama response")
    content = _ollama_content(data)
    input_tokens = _optional_int(data.get("prompt_eval_count"))
    output_tokens = _optional_int(data.get("eval_count"))
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": 0,
        "cache_hit": False,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    _record_effective_model(client, data)
    request_id = response_headers.get("x-request-id")
    if isinstance(request_id, str) and request_id:
        usage["request_id"] = request_id
    done_reason = data.get("done_reason")
    if done_reason == "length":
        stop_reason = StopReason.MAX_TOKENS
    elif done_reason in {None, "stop"}:
        stop_reason = (
            StopReason.TOOL_USE
            if any(block.get("type") == "tool_use" for block in content)
            else StopReason.END_TURN
        )
    else:
        stop_reason = StopReason.UNKNOWN
    return Response(stop_reason=stop_reason, content=content, usage=usage)


def _read_ollama_stream(response_stream, callbacks):
    content_parts = []
    tool_calls = []
    terminal = None
    model = None
    for data in _iter_ndjson_events(response_stream, family="Ollama"):
        if data.get("error"):
            callbacks.commit()
            raise ProviderTransportError(
                "Ollama error: backend_error", code="backend_error"
            )
        done = data.get("done")
        message = data.get("message")
        if type(done) is not bool or not isinstance(message, dict):
            raise ValueError("invalid Ollama stream event")
        role = message.get("role")
        text = message.get("content", "")
        calls = message.get("tool_calls", [])
        if role not in {None, "assistant"} or not isinstance(text, str):
            raise ValueError("invalid Ollama stream message")
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            raise ValueError("invalid Ollama stream tool calls")
        event_model = data.get("model")
        if event_model is not None:
            if not isinstance(event_model, str) or model not in {None, event_model}:
                raise ValueError("Ollama stream model changed")
            model = event_model
        callbacks.commit()
        content_parts.append(text)
        tool_calls.extend(deepcopy(calls))
        if not done:
            callbacks.text(text)
            continue
        terminal = deepcopy(data)
        break
    if terminal is None:
        raise ValueError("Ollama stream missing done event")
    terminal_message = dict(terminal["message"])
    terminal_message["content"] = "".join(content_parts)
    if tool_calls:
        terminal_message["tool_calls"] = tool_calls
    else:
        terminal_message.pop("tool_calls", None)
    terminal["message"] = terminal_message
    if model is not None:
        terminal["model"] = model
    return terminal


class OllamaChatModelClient:
    def __init__(
        self,
        model,
        host,
        temperature,
        top_p,
        timeout,
        *,
        auth_mode=None,
        api_key="",
        capabilities=None,
    ):
        from pony.config.model import validate_api_base

        self.model = str(model)
        self.host = validate_api_base(host)
        self.api_key = str(api_key or "")
        self.auth_mode = auth_mode or "none"
        self.capabilities = dict(capabilities or {})
        self.temperature = _validate_number("temperature", temperature, minimum=0)
        self.top_p = _validate_number("top_p", top_p, minimum=0, maximum=1)
        self.timeout = _validate_number("timeout", timeout, minimum=0.001)
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.provider_binding = _model_binding(
            "ollama_chat",
            self.model,
            self.host,
        )
        self.provider_metadata = _model_runtime_metadata(
            "ollama_chat",
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
        request = _ollama_request(
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
            family="Ollama",
            retryable=True,
        )
        try:
            data = _decode_json_object(response_body)
            response = _decode_ollama_response(self, data, response_headers)
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "Ollama",
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
        request = _ollama_request(
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
                family="Ollama",
                retryable=True,
            ) as response_stream:
                headers = getattr(response_stream, "headers", {}) or {}
                data = _read_ollama_stream(response_stream, callbacks)
            response = _decode_ollama_response(self, data, headers)
        except ProviderTransportError as exc:
            if callbacks.committed:
                raise _stream_failure_after_commit(exc) from None
            raise
        except Exception:
            raise _provider_protocol_error(
                "Ollama",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = response.usage
        return response
