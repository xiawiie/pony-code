"""Anthropic-compatible provider adapter."""

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from ._shared import _normalize_versioned_base_url, _optional_int


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and (item.get("type") in ("", None, "text") or "text" in item):
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


def _anthropic_no_text_error(data, max_new_tokens):
    content = data.get("content") or []
    has_thinking = any(isinstance(item, dict) and item.get("type") == "thinking" for item in content)
    if has_thinking and data.get("stop_reason") == "max_tokens":
        return (
            "Anthropic-compatible error: response contained thinking blocks but no text before max_tokens. "
            f"Increase max_new_tokens/--max-new-tokens above {max_new_tokens} for reasoning-heavy models."
        )
    return "Anthropic-compatible error: could not extract text from response"


def _supports_anthropic_prompt_cache(base_url):
    return any(host in base_url for host in ("anthropic.com", "right.codes"))


def _anthropic_cache_control(prompt_cache_retention):
    cache_control = {"type": "ephemeral"}
    if prompt_cache_retention in {"1h", "one_hour"}:
        cache_control["ttl"] = "1h"
    return cache_control


def _extract_anthropic_usage_cache_details(data):
    usage = data.get("usage") or {}
    input_tokens = _optional_int(usage.get("input_tokens"))
    output_tokens = _optional_int(usage.get("output_tokens"))
    total_tokens = _optional_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
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
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = _supports_anthropic_prompt_cache(self.base_url)
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.supports_prompt_cache and prompt_cache_key:
            payload["cache_control"] = _anthropic_cache_control(prompt_cache_retention)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_anthropic_usage_cache_details(data),
        }
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError(_anthropic_no_text_error(data, max_new_tokens))

    def stream_complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        yield self.complete(
            prompt,
            max_new_tokens,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
