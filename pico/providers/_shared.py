"""Shared provider helpers."""

import json


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _iter_sse_data_payloads(lines):
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload:
            yield payload


def _iter_openai_stream_chunks(lines):
    from .clients import _extract_openai_text

    yielded_delta = False
    for payload in _iter_sse_data_payloads(lines):
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        response_data = response if isinstance(response, dict) else {}
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yielded_delta = True
                yield delta, response_data
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text and not yielded_delta:
                yield text, response_data
            continue
        if event_type == "response.completed":
            text = _extract_openai_text(response_data)
            if text and not yielded_delta:
                yield text, response_data
            else:
                yield "", response_data
            continue
        text = _extract_openai_text(event)
        if text and not yielded_delta:
            yield text, event


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _optional_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None
