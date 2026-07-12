"""Shared provider helpers."""

from http.client import HTTPException, RemoteDisconnected
import ipaddress
import json
import math
import urllib.error
import urllib.parse
import urllib.request


MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024


class _ProviderFailure(RuntimeError):
    """Safe internal provider failure used by AgentLoop retry policy."""

    def __init__(self, message, *, code, http_status=None, retryable=False):
        super().__init__(message)
        self.code = str(code)
        self.http_status = http_status if type(http_status) is int else None
        self.retryable = bool(retryable)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_PROVIDER_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _provider_urlopen(request, timeout):
    return _PROVIDER_OPENER.open(request, timeout=timeout)


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _validate_header_value(name, value):
    if any(character in str(value) for character in ("\0", "\r", "\n")):
        raise RuntimeError(f"{name} contains invalid control characters")
    try:
        str(value).encode("latin-1")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            f"{name} contains characters that cannot be sent in HTTP headers. "
            "Check .env for stray inline comments or non-ASCII suffixes."
        ) from exc


def _validate_provider_credentials(base_url, api_key, *, family):
    key = str(api_key or "")
    parsed = urllib.parse.urlsplit(str(base_url))
    host = parsed.hostname or ""
    loopback = host.casefold() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if not key.strip() and not loopback:
        raise _ProviderFailure(
            f"{family} request failed: missing_credentials",
            code="missing_credentials",
        )
    if key != key.strip():
        raise _ProviderFailure(
            f"{family} request failed: invalid_credentials",
            code="invalid_configuration",
        )
    if key and parsed.scheme.casefold() != "https" and not loopback:
        raise _ProviderFailure(
            f"{family} request failed: insecure_credentials",
            code="invalid_configuration",
        )


def _validate_number(name, value, *, minimum, maximum=None, integer=False):
    valid_type = type(value) is int if integer else type(value) in {int, float}
    if (
        not valid_type
        or (type(value) is float and not math.isfinite(value))
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"invalid {name}")
    return int(value) if integer else float(value)


def _network_failure(family, exc, *, retryable):
    if isinstance(exc, RemoteDisconnected):
        code = "remote_disconnect"
    elif isinstance(exc, TimeoutError) or (
        isinstance(exc, urllib.error.URLError)
        and isinstance(exc.reason, TimeoutError)
    ):
        code = "timeout"
    else:
        code = "network_error"
    return _ProviderFailure(
        f"{family} request failed: {code}",
        code=code,
        retryable=retryable,
    )


def _open_provider_request(client, request, *, family, retryable):
    client.last_transport_attempts += 1
    try:
        with _provider_urlopen(request, timeout=client.timeout) as response:
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            headers = getattr(response, "headers", {}) or {}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            exc.close()
        except Exception:
            pass
        if status == 429:
            code = "rate_limited"
        elif 500 <= status < 600:
            code = "http_5xx"
        elif 300 <= status < 400:
            code = "redirect_blocked"
        else:
            code = "http_4xx"
        raise _ProviderFailure(
            f"{family} request failed with HTTP {status}",
            code=code,
            http_status=status,
            retryable=retryable and code == "http_5xx",
        ) from None
    except (urllib.error.URLError, HTTPException, OSError) as exc:
        raise _network_failure(family, exc, retryable=retryable) from None
    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
        raise _ProviderFailure(
            f"{family} error: response_too_large",
            code="response_too_large",
        )
    return body, headers


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
        if value < 0:
            raise ValueError("invalid integer field")
        return value
    if isinstance(value, str):
        candidate = value.strip()
        digits = candidate[1:] if candidate[:1] in {"+", "-"} else candidate
        if digits and digits.isdecimal():
            parsed = int(candidate)
            if parsed < 0:
                raise ValueError("invalid integer field")
            return parsed
    raise ValueError("invalid integer field")
