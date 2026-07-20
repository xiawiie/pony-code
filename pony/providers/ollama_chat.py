"""Ollama Chat native provider adapter."""

import json
import urllib.request
import uuid

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _open_provider_request,
    _optional_int,
    _provider_auth_headers,
    _provider_protocol_error,
    _model_binding,
    _model_runtime_metadata,
    _record_effective_model,
    _resource_url,
    _validate_number,
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
        _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
        auth_headers = _provider_auth_headers(
            self.host,
            self.api_key,
            auth_mode=self.auth_mode,
            family="Ollama",
        )
        prepared_tools = _ollama_tools(tools)
        payload = {
            "model": self.model,
            "messages": _ollama_messages(system, messages),
            "stream": False,
            "think": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        if prepared_tools:
            payload["tools"] = prepared_tools
        request = urllib.request.Request(
            _resource_url(self.host, "api/chat"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **auth_headers},
            method="POST",
        )
        response_body, response_headers = _open_provider_request(
            self,
            request,
            family="Ollama",
            retryable=True,
        )
        try:
            data = _decode_json_object(response_body)
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
            _record_effective_model(self, data)
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
            response = Response(
                stop_reason=stop_reason,
                content=content,
                usage=usage,
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "Ollama",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = usage
        return response
