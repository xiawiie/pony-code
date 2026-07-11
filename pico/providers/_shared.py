"""Shared provider helpers."""

import json


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _validate_header_value(name, value):
    try:
        str(value).encode("latin-1")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            f"{name} contains characters that cannot be sent in HTTP headers. "
            "Check .env for stray inline comments or non-ASCII suffixes."
        ) from exc


def _decode_json_object(body):
    if isinstance(body, bytes):
        text = body.decode("utf-8")
    elif isinstance(body, str):
        text = body
    else:
        raise ValueError("invalid response body")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    return data


def _mapping_or_empty(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("response field must be an object")
    return value


def _iter_sse_data_payloads(lines):
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8")
        elif isinstance(raw_line, str):
            line = raw_line
        else:
            raise ValueError("invalid event stream line")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload:
            yield payload


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    usage = _mapping_or_empty(data.get("usage"))
    input_tokens = _optional_int(
        usage.get("input_tokens", usage.get("prompt_tokens"))
    )
    output_tokens = _optional_int(
        usage.get("output_tokens", usage.get("completion_tokens"))
    )
    total_tokens = _optional_int(usage.get("total_tokens"))
    input_details = _mapping_or_empty(usage.get("input_tokens_details"))
    if not input_details:
        input_details = _mapping_or_empty(usage.get("prompt_tokens_details"))
    cached_tokens = _optional_int(input_details.get("cached_tokens")) or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def _optional_int(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid integer field")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        digits = candidate[1:] if candidate[:1] in {"+", "-"} else candidate
        if digits and digits.isdecimal():
            return int(candidate)
    raise ValueError("invalid integer field")
