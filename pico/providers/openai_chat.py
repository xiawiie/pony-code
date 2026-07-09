"""OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import json
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from ._shared import _normalize_versioned_base_url, _optional_int, _validate_header_value
from .message_utils import strip_pico_meta
from .response import Response, StopReason

OPENAI_CHAT_USER_AGENT = "pico/0.1"


def _content_to_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"} or "text" in block:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
            continue
        if block_type == "tool_use":
            name = str(block.get("name", "") or "")
            raw_input = block.get("input", {})
            if isinstance(raw_input, dict):
                rendered_input = json.dumps(raw_input, sort_keys=True)
            else:
                rendered_input = str(raw_input or "")
            if name:
                parts.append(f"{name}({rendered_input})")
            elif rendered_input:
                parts.append(rendered_input)
            continue
        if block_type == "tool_result":
            result = block.get("content", "")
            if isinstance(result, list):
                text = _content_to_text(result)
            elif isinstance(result, str):
                text = result
            elif isinstance(result, dict):
                text = json.dumps(result, sort_keys=True)
            else:
                text = str(result or "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _extract_chat_text(data):
    for choice in data.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        text = _content_to_text(message.get("content", ""))
        if text:
            return text
    return ""


def _extract_chat_usage(data):
    usage = data.get("usage") or {}
    input_tokens = _optional_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = _optional_int(usage.get("completion_tokens", usage.get("output_tokens")))
    total_tokens = _optional_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached_tokens = _optional_int(input_details.get("cached_tokens")) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class OpenAIChatAdapter:
    supports_prompt_cache = False
    supports_native_tools = False

    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.last_completion_metadata = {}

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_CHAT_USER_AGENT,
        }
        if self.api_key:
            authorization = f"Bearer {self.api_key}"
            _validate_header_value("OpenAI Chat API key", authorization)
            headers["Authorization"] = authorization
        return headers

    def _chat_messages(self, system, messages):
        chat_messages = []
        system_text = _content_to_text(system)
        if system_text:
            chat_messages.append({"role": "system", "content": system_text})
        for message in strip_pico_meta(messages):
            chat_messages.append(
                {
                    "role": message.get("role", "user"),
                    "content": _content_to_text(message.get("content", "")),
                }
            )
        return chat_messages

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": self._chat_messages(system, messages),
            "max_tokens": max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI Chat request failed with HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, RemoteDisconnected) as exc:
            raise RuntimeError(
                "Could not reach the OpenAI Chat backend.\n"
                f"Base URL: {self.base_url}\n"
                f"Model: {self.model}"
            ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("OpenAI Chat error: backend returned non-JSON content") from exc
        if data.get("error"):
            raise RuntimeError(f"OpenAI Chat error: {data['error']}")

        usage = _extract_chat_usage(data)
        self.last_completion_metadata = usage
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": _extract_chat_text(data)}],
            usage=usage,
        )
