"""OpenAI-compatible provider adapter."""

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from ._shared import (
    _decode_json_object,
    _extract_usage_cache_details,
    _iter_sse_data_payloads,
    _normalize_versioned_base_url,
    _validate_header_value,
)

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1"


def _extract_openai_text(data):
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    output_text = data.get("output_text")
    if output_text is not None and not isinstance(output_text, str):
        raise ValueError("output text must be a string")
    if output_text:
        return output_text

    output = data.get("output", [])
    if not isinstance(output, list) or not all(
        isinstance(item, dict) for item in output
    ):
        raise ValueError("output must be a list of objects")
    for item in output:
        content_items = item.get("content", [])
        if not isinstance(content_items, list) or not all(
            isinstance(content, dict) for content in content_items
        ):
            raise ValueError("output content must be a list of objects")
        for content in content_items:
            text = content.get("text")
            if text is not None and not isinstance(text, str):
                raise ValueError("content text must be a string")
            if text:
                return text

    choices = data.get("choices", [])
    if not isinstance(choices, list) or not all(
        isinstance(choice, dict) for choice in choices
    ):
        raise ValueError("choices must be a list of objects")
    if choices:
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            raise ValueError("choice message must be an object")
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    raise ValueError("message content must contain objects")
                text = item.get("text")
                if text is not None and not isinstance(text, str):
                    raise ValueError("message text must be a string")
                if text:
                    return text
        elif content is not None:
            raise ValueError("message content must be text or a list")

    return ""


def _iter_openai_sse_events(body_text):
    if not isinstance(body_text, str):
        raise ValueError("event stream body must be text")
    for payload in _iter_sse_data_payloads(body_text.splitlines()):
        if payload == "[DONE]":
            break
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("event stream item must be an object")
        yield event


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for event in _iter_openai_sse_events(body_text):
        event_type = event.get("type", "")
        if not isinstance(event_type, str):
            raise ValueError("event type must be a string")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ValueError("text delta must be a string")
            deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if not isinstance(text, str):
                raise ValueError("completed text must be a string")
            if text:
                return text
        part = event.get("part")
        if part is not None and not isinstance(part, dict):
            raise ValueError("event part must be an object")
        if part is not None:
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if item is not None and not isinstance(item, dict):
            raise ValueError("event item must be an object")
        if item is not None:
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if response is not None and not isinstance(response, dict):
            raise ValueError("event response must be an object")
        if response is not None:
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    completed_text = ""
    for event in _iter_openai_sse_events(body_text):
        response = event.get("response")
        if response is not None and not isinstance(response, dict):
            raise ValueError("event response must be an object")
        event_type = event.get("type", "")
        if not isinstance(event_type, str):
            raise ValueError("event type must be a string")
        if response is not None:
            last_response = response
            if event_type == "response.completed":
                text = _extract_openai_text(response)
                return text or completed_text or "".join(deltas), response
        elif event_type == "response.completed":
            raise ValueError("completed event must contain a response")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ValueError("text delta must be a string")
            deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if not isinstance(text, str):
                raise ValueError("completed text must be a string")
            if text:
                completed_text = text
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if completed_text:
        return completed_text, last_response or {}
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        from pico.config import validate_provider_base_url

        self.model = model
        self.base_url = _normalize_versioned_base_url(validate_provider_base_url(base_url))
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.last_completion_metadata = {}

    def _responses_payload(self, prompt, max_tokens):
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def _headers(self, accept):
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            authorization = f"Bearer {self.api_key}"
            _validate_header_value("OpenAI-compatible API key", authorization)
            headers["Authorization"] = authorization
        return headers

    def _request(self, payload, headers):
        return urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def complete_text(self, prompt, max_tokens):
        """Return one text response from the non-streaming Responses API."""
        self.last_completion_metadata = {}
        payload = self._responses_payload(prompt, max_tokens)
        request = self._request(payload, self._headers("application/json"))
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_body = response.read()
                    response_headers = getattr(response, "headers", {}) or {}
                break
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"OpenAI-compatible request failed with HTTP {exc.code}"
                ) from None
            except (urllib.error.URLError, RemoteDisconnected):
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "OpenAI-compatible request failed: network_error"
                ) from None

        try:
            content_type = response_headers.get("Content-Type", "")
            if not isinstance(content_type, str):
                raise ValueError("content type must be a string")
            body_text = response_body.decode("utf-8")
        except Exception:
            raise RuntimeError(
                "OpenAI-compatible error: invalid_response"
            ) from None

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith(
            "text/event-stream"
        ) or body_text.lstrip().startswith("data:"):
            try:
                text, response_data = _extract_openai_response_from_sse(body_text)
                metadata = None
                if response_data:
                    metadata = _extract_usage_cache_details(response_data)
            except Exception:
                raise RuntimeError(
                    "OpenAI-compatible error: invalid_response"
                ) from None
            if metadata is not None:
                self.last_completion_metadata = metadata
            if text:
                return text
            raise RuntimeError("OpenAI-compatible error: could not extract text from event stream response")

        try:
            data = _decode_json_object(body_text)
        except Exception:
            raise RuntimeError(
                "OpenAI-compatible error: invalid_response"
            ) from None
        if data.get("error"):
            raise RuntimeError("OpenAI-compatible error: backend_error") from None
        try:
            metadata = _extract_usage_cache_details(data)
            text = _extract_openai_text(data)
        except Exception:
            raise RuntimeError(
                "OpenAI-compatible error: invalid_response"
            ) from None
        self.last_completion_metadata = metadata
        return text
