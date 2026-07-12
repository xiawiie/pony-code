"""OpenAI-compatible provider adapter."""

import json
import urllib.request

from ._shared import (
    _decode_json_object,
    _extract_usage_cache_details,
    _iter_sse_data_payloads,
    _normalize_versioned_base_url,
    _open_provider_request,
    _validate_header_value,
    _validate_number,
    _validate_provider_credentials,
)
from .response import StopReason

OPENAI_COMPATIBLE_USER_AGENT = "pico/0.1"


def _extract_openai_text(data):
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    output_text = data.get("output_text")
    if output_text is not None and not isinstance(output_text, str):
        raise ValueError("output text must be a string")
    if output_text:
        return output_text

    parts = []
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
                parts.append(text)
                continue
            refusal = content.get("refusal")
            if refusal is not None and not isinstance(refusal, str):
                raise ValueError("refusal must be a string")
            if refusal:
                parts.append(refusal)

    if parts:
        return "\n".join(parts)

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
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    raise ValueError("message content must contain objects")
                text = item.get("text")
                if text is not None and not isinstance(text, str):
                    raise ValueError("message text must be a string")
                if text:
                    parts.append(text)
            if parts:
                return "\n".join(parts)
        elif content is not None:
            raise ValueError("message content must be text or a list")

    return ""


def _openai_stop_reason(data):
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    incomplete = data.get("incomplete_details")
    if incomplete is not None and not isinstance(incomplete, dict):
        raise ValueError("incomplete details must be an object")
    reason = (incomplete or {}).get("reason")
    if reason == "max_output_tokens":
        return StopReason.MAX_TOKENS
    if reason == "content_filter":
        return StopReason.REFUSAL

    choices = data.get("choices", [])
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
        if finish_reason == "length":
            return StopReason.MAX_TOKENS
        if finish_reason == "content_filter":
            return StopReason.REFUSAL

    output = data.get("output", [])
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "refusal"
                for block in content
            ):
                return StopReason.REFUSAL
    return StopReason.END_TURN


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


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    completed_texts = []
    fallback_texts = []
    for event in _iter_openai_sse_events(body_text):
        response = event.get("response")
        if response is not None and not isinstance(response, dict):
            raise ValueError("event response must be an object")
        event_type = event.get("type", "")
        if not isinstance(event_type, str):
            raise ValueError("event type must be a string")
        if response is not None:
            last_response = response
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
                completed_texts.append(text)
        else:
            text = _extract_openai_text(event)
            if text:
                fallback_texts.append(text)
    if isinstance(last_response, dict):
        text = _extract_openai_text(last_response)
        if text:
            return text, last_response
    if completed_texts:
        return "\n".join(completed_texts), last_response or {}
    if deltas:
        return "".join(deltas), last_response or {}
    if fallback_texts:
        return "\n".join(fallback_texts), last_response or {}
    return "", {}


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        from pico.config import validate_provider_base_url

        self.model = model
        self.base_url = _normalize_versioned_base_url(validate_provider_base_url(base_url))
        self.api_key = api_key
        self.temperature = (
            None
            if temperature is None
            else _validate_number("temperature", temperature, minimum=0, maximum=2)
        )
        self.timeout = _validate_number("timeout", timeout, minimum=0.001)
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.last_stop_reason = StopReason.END_TURN

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
            "store": False,
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
        self.last_transport_attempts = 0
        self.last_stop_reason = StopReason.END_TURN
        _validate_number("max_tokens", max_tokens, minimum=1, integer=True)
        _validate_provider_credentials(
            self.base_url,
            self.api_key,
            family="OpenAI-compatible",
        )
        payload = self._responses_payload(prompt, max_tokens)
        request = self._request(payload, self._headers("application/json"))
        response_body, response_headers = _open_provider_request(
            self,
            request,
            family="OpenAI-compatible",
            retryable=True,
        )

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
            self.last_stop_reason = _openai_stop_reason(response_data)
            if text:
                return text
            raise RuntimeError("OpenAI-compatible error: could not extract text from event stream response")

        try:
            data = _decode_json_object(body_text)
        except Exception:
            raise RuntimeError(
                "OpenAI-compatible error: invalid_response"
            ) from None
        if data.get("error") or data.get("status") == "failed":
            raise RuntimeError("OpenAI-compatible error: backend_error") from None
        try:
            metadata = _extract_usage_cache_details(data)
            text = _extract_openai_text(data)
        except Exception:
            raise RuntimeError(
                "OpenAI-compatible error: invalid_response"
            ) from None
        self.last_completion_metadata = metadata
        self.last_stop_reason = _openai_stop_reason(data)
        return text
