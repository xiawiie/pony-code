"""Anthropic-compatible provider adapter."""

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from pico.messages import strip_pico_meta

from ._shared import (
    _decode_json_object,
    _mapping_or_empty,
    _normalize_versioned_base_url,
    _optional_int,
    _validate_header_value,
)


def _anthropic_content(data):
    content = data.get("content", [])
    if not isinstance(content, list) or not all(
        isinstance(item, dict) for item in content
    ):
        raise ValueError("content must be a list of objects")
    for item in content:
        if item.get("type") is not None and not isinstance(item["type"], str):
            raise ValueError("content type must be a string")
        if item.get("text") is not None and not isinstance(item["text"], str):
            raise ValueError("content text must be a string")
    return content


def _supports_anthropic_prompt_cache(base_url):
    return any(
        host in base_url
        for host in ("anthropic.com", "deepseek.com", "right.codes")
    )


def _extract_anthropic_usage_cache_details(data):
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    usage = _mapping_or_empty(data.get("usage"))
    input_tokens = _optional_int(usage.get("input_tokens"))
    output_tokens = _optional_int(usage.get("output_tokens"))
    total_tokens = _optional_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cache_creation_tokens = (
        _optional_int(usage.get("cache_creation_input_tokens")) or 0
    )
    cache_read_tokens = _optional_int(usage.get("cache_read_input_tokens")) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cache_read_tokens,
        "cache_hit": cache_read_tokens > 0,
        "cache_creation_input_tokens": cache_creation_tokens,
        "cache_read_input_tokens": cache_read_tokens,
    }


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        from pico.config import validate_provider_base_url

        self.model = model
        self.base_url = _normalize_versioned_base_url(validate_provider_base_url(base_url))
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = _supports_anthropic_prompt_cache(self.base_url)
        self.last_completion_metadata = {}

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.last_completion_metadata = {}
        messages = strip_pico_meta(messages)
        from .response import Response, StopReason

        # 打 cache_control 断点：把指定 message.content 转为 list-of-blocks 形式
        prepared_messages = []
        breakpoints = set(cache_breakpoints or [])
        for idx, msg in enumerate(messages):
            if idx in breakpoints:
                content = msg["content"]
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    blocks = list(content)
                    if blocks:
                        last = dict(blocks[-1])
                        last["cache_control"] = {"type": "ephemeral"}
                        blocks[-1] = last
                prepared_messages.append({"role": msg["role"], "content": blocks})
            else:
                prepared_messages.append({"role": msg["role"], "content": msg["content"]})

        payload = {
            "model": self.model,
            "system": system,
            "tools": tools,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if not tools:
            payload.pop("tools")

        _validate_header_value("Anthropic-compatible API key", self.api_key)
        request = urllib.request.Request(
            f"{self.base_url}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as raw:
                    response_body = raw.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"Anthropic-compatible request failed with HTTP {exc.code}"
                ) from None
            except (urllib.error.URLError, RemoteDisconnected):
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Anthropic-compatible request failed: network_error"
                ) from None
        try:
            data = _decode_json_object(response_body)
        except Exception:
            raise RuntimeError(
                "Anthropic-compatible error: invalid_response"
            ) from None
        if data.get("error"):
            raise RuntimeError("Anthropic-compatible error: backend_error") from None

        stop_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.STOP_SEQUENCE,
        }
        try:
            raw_stop_reason = data.get("stop_reason")
            if raw_stop_reason is not None and not isinstance(raw_stop_reason, str):
                raise ValueError("stop reason must be a string")
            stop_reason = stop_map.get(raw_stop_reason, StopReason.UNKNOWN)
            content = _anthropic_content(data)
            usage_details = _extract_anthropic_usage_cache_details(data)
            response = Response(
                stop_reason=stop_reason,
                content=content,
                usage=usage_details,
            )
        except Exception:
            raise RuntimeError(
                "Anthropic-compatible error: invalid_response"
            ) from None
        self.last_completion_metadata = usage_details
        return response
