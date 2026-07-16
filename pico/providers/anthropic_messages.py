"""Anthropic Messages native provider adapter."""

from copy import deepcopy
import json
import urllib.request

from pico.agent.messages import strip_pico_meta

from .transport import (
    ProviderTransportError,
    _decode_json_object,
    _mapping_or_empty,
    _open_provider_request,
    _optional_int,
    _model_binding,
    _model_runtime_metadata,
    _record_effective_model,
    _validate_number,
    _provider_auth_headers,
    _resource_url,
)


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
                or not isinstance(item.get("input"), dict)
            ):
                raise ValueError("invalid tool_use block")
            seen_action_content = True
            action_content.append(deepcopy(item))
        elif item_type in {"thinking", "redacted_thinking"}:
            if seen_action_content:
                raise ValueError("thinking blocks must precede response content")
            provider_state.extend(_validated_anthropic_provider_state([item]))
        else:
            raise ValueError("unsupported content block")
    return action_content, provider_state


def _supports_anthropic_prompt_cache(base_url):
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    return host == "api.anthropic.com"


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
        from pico.config.model import validate_api_url

        self.model = model
        self.base_url = validate_api_url(base_url)
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
            self.base_url,
        )
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
        auth_headers = _provider_auth_headers(
            self.base_url,
            self.api_key,
            auth_mode=self.auth_mode,
            family="Anthropic",
        )
        messages = strip_pico_meta(messages)
        from .response import Response, StopReason

        # 打 cache_control 断点：把指定 message.content 转为 list-of-blocks 形式
        prepared_messages = []
        breakpoints = (
            set(cache_breakpoints or []) if self.supports_prompt_cache else set()
        )
        for idx, msg in enumerate(messages):
            content = msg["content"]
            provider_state = _validated_anthropic_provider_state(
                msg.get("_pico_provider_state")
            )
            if provider_state:
                if (
                    msg.get("role") != "assistant"
                    or not isinstance(content, list)
                    or not content
                    or not all(
                        isinstance(block, dict) and block.get("type") == "tool_use"
                        for block in content
                    )
                ):
                    raise ValueError(
                        "Anthropic provider state requires assistant tool_use"
                    )
                content = [*provider_state, *deepcopy(content)]
            if idx in breakpoints:
                if isinstance(content, str):
                    blocks = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                else:
                    blocks = list(content)
                    if blocks:
                        last = dict(blocks[-1])
                        last["cache_control"] = {"type": "ephemeral"}
                        blocks[-1] = last
                prepared_messages.append({"role": msg["role"], "content": blocks})
            else:
                prepared_messages.append({"role": msg["role"], "content": content})

        prepared_system = system
        if not self.supports_prompt_cache:
            prepared_system = []
            for block in system:
                copied = dict(block)
                copied.pop("cache_control", None)
                prepared_system.append(copied)

        prepared_tools = _anthropic_tools(
            tools,
            strict=bool(self.capabilities.get("strict_tools")),
        )
        payload = {
            "model": self.model,
            "system": prepared_system,
            "tools": prepared_tools,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if not prepared_tools:
            payload.pop("tools")
        elif self.capabilities.get("parallel_tool_control"):
            payload["tool_choice"] = {
                "type": "auto",
                "disable_parallel_tool_use": True,
            }

        request = urllib.request.Request(
            _resource_url(self.base_url, "messages"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                **auth_headers,
            },
            method="POST",
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
            raise ProviderTransportError(
                "Anthropic error: provider_protocol_mismatch",
                code="provider_protocol_mismatch",
            ) from None
        if data.get("error"):
            raise ProviderTransportError(
                "Anthropic error: backend_error",
                code="backend_error",
            ) from None

        stop_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.STOP_SEQUENCE,
            "refusal": StopReason.REFUSAL,
            "model_context_window_exceeded": StopReason.MAX_TOKENS,
        }
        try:
            raw_stop_reason = data.get("stop_reason")
            if not isinstance(raw_stop_reason, str):
                raise ValueError("stop reason must be a string")
            if raw_stop_reason == "pause_turn":
                raise ProviderTransportError(
                    "Anthropic error: unsupported_stop_reason",
                    code="unsupported_stop_reason",
                )
            stop_reason = stop_map.get(raw_stop_reason, StopReason.UNKNOWN)
            content, provider_state = _anthropic_content(data)
            usage_details = _extract_anthropic_usage_cache_details(data)
            _record_effective_model(self, data)
            request_id = response_headers.get("request-id") or response_headers.get(
                "x-request-id"
            )
            if isinstance(request_id, str) and request_id:
                usage_details["request_id"] = request_id
            response = Response(
                stop_reason=stop_reason,
                content=content,
                usage=usage_details,
                provider_state=provider_state,
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise ProviderTransportError(
                "Anthropic error: provider_protocol_mismatch",
                code="provider_protocol_mismatch",
            ) from None
        self.last_completion_metadata = usage_details
        return response
