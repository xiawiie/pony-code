"""OpenAI Chat Completions native provider adapter."""

import json
import urllib.request

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _extract_usage_cache_details,
    _model_binding,
    _model_runtime_metadata,
    _open_provider_request,
    _provider_protocol_error,
    _provider_auth_headers,
    _record_effective_model,
    _resource_url,
    _validate_number,
)
from .openai_responses import OPENAI_USER_AGENT, _openai_tools, _system_instructions
from .response import Response, StopReason


def _chat_tools(tools, *, strict):
    prepared, optional_by_name = _openai_tools(tools, strict=strict)
    return [
        {
            "type": "function",
            "function": {key: value for key, value in tool.items() if key != "type"},
        }
        for tool in prepared
    ], optional_by_name


def _chat_messages(system, messages):
    output = []
    instructions = _system_instructions(system)
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
        for argument in optional_by_name.get(name, set()):
            if parsed.get(argument) is None:
                parsed.pop(argument, None)
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
        _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
        auth_headers = _provider_auth_headers(
            self.base_url,
            self.api_key,
            auth_mode=self.auth_mode,
            family="OpenAI Chat",
        )
        prepared_tools, optional_by_name = _chat_tools(
            tools,
            strict=bool(self.capabilities.get("strict_tools")),
        )
        payload = {
            "model": self.model,
            "messages": _chat_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if prepared_tools:
            payload["tools"] = prepared_tools
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.capabilities.get("parallel_tool_control"):
            payload["parallel_tool_calls"] = False
        request = urllib.request.Request(
            _resource_url(self.base_url, "chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": OPENAI_USER_AGENT,
                **auth_headers,
            },
            method="POST",
        )
        contains_tool_result = _contains_tool_result(messages)
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
            if data.get("error") or "output" in data:
                raise ValueError("not a successful Chat Completions object")
            content, stop_reason = _chat_content(data, optional_by_name)
            usage = _extract_usage_cache_details(data)
            _record_effective_model(self, data)
            request_id = response_headers.get("x-request-id") or data.get("id")
            if isinstance(request_id, str) and request_id:
                usage["request_id"] = request_id
            response = Response(
                stop_reason=stop_reason,
                content=content,
                usage=usage,
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise _provider_protocol_error(
                "OpenAI Chat",
                stage="response_decode",
                reason="response_shape_invalid",
            ) from None
        self.last_completion_metadata = usage
        return response
