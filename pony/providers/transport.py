"""Shared HTTP transport, validation, and response helpers."""

from http.client import HTTPException, IncompleteRead, RemoteDisconnected
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import ipaddress
import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request


MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024
_PROVIDER_ERROR_STAGES = frozenset(
    {"tool_call", "tool_result", "response_decode", "runtime"}
)
_PROVIDER_PROTOCOL_REASONS = frozenset(
    {
        "reasoning_replay_required",
        "response_shape_invalid",
        "tool_arguments_invalid",
        "tool_call_missing",
        "tool_call_shape_invalid",
        "tool_result_rejected",
    }
)
_PROVIDER_PROTOCOL_FAMILIES = frozenset(
    {
        "anthropic_messages",
        "ollama_chat",
        "openai_chat_completions",
        "openai_responses",
    }
)


class ProviderTransportError(RuntimeError):
    """Safe internal provider failure used by AgentLoop retry policy."""

    def __init__(
        self,
        message,
        *,
        code,
        http_status=None,
        retryable=False,
        retry_after=None,
        stage=None,
        protocol_reason=None,
        protocol_family=None,
    ):
        super().__init__(message)
        self.code = str(code)
        self.http_status = http_status if type(http_status) is int else None
        self.retryable = bool(retryable)
        self.retry_after = (
            min(10.0, max(0.0, float(retry_after)))
            if type(retry_after) in {int, float}
            else None
        )
        self.stage = (
            stage
            if isinstance(stage, str) and stage in _PROVIDER_ERROR_STAGES
            else None
        )
        self.protocol_reason = (
            protocol_reason
            if isinstance(protocol_reason, str)
            and protocol_reason in _PROVIDER_PROTOCOL_REASONS
            else None
        )
        self.protocol_family = (
            protocol_family
            if isinstance(protocol_family, str)
            and protocol_family in _PROVIDER_PROTOCOL_FAMILIES
            else None
        )


def _provider_protocol_error(family, *, stage, reason):
    return ProviderTransportError(
        f"{family} error: provider_protocol_mismatch",
        code="provider_protocol_mismatch",
        stage=stage,
        protocol_reason=reason,
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


_PROVIDER_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _provider_urlopen(request, timeout):
    return _PROVIDER_OPENER.open(request, timeout=timeout)


def _resource_url(base_url, resource):
    return str(base_url).rstrip("/") + "/" + str(resource).lstrip("/")


def _model_binding(protocol_family, model, base_url):
    endpoint_hash = hashlib.sha256(str(base_url).encode("utf-8")).hexdigest()
    return {
        "protocol_family": str(protocol_family),
        "model": str(model),
        "endpoint_hash": f"sha256:{endpoint_hash}",
    }


def _model_runtime_metadata(protocol_family, model):
    return {
        "protocol_family": str(protocol_family),
        "requested_model": str(model),
        "effective_model": str(model),
    }


def _record_effective_model(client, data):
    value = data.get("model")
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(character in value for character in ("\0", "\r", "\n"))
    ):
        raise ValueError("invalid response model")
    client.provider_metadata["effective_model"] = value


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


def _validate_provider_credentials(
    base_url,
    api_key,
    *,
    family,
    required=True,
):
    key = str(api_key or "")
    parsed = urllib.parse.urlsplit(str(base_url))
    host = parsed.hostname or ""
    loopback = host.casefold() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if required and not key.strip():
        raise ProviderTransportError(
            f"{family} request failed: missing_credentials",
            code="missing_credentials",
        )
    if key != key.strip():
        raise ProviderTransportError(
            f"{family} request failed: invalid_credentials",
            code="invalid_configuration",
        )
    if key and parsed.scheme.casefold() != "https" and not loopback:
        raise ProviderTransportError(
            f"{family} request failed: insecure_credentials",
            code="invalid_configuration",
        )


def _provider_auth_headers(base_url, api_key, *, auth_mode, family):
    if auth_mode not in {"x-api-key", "bearer", "none"}:
        raise ProviderTransportError(
            f"{family} request failed: invalid_auth_mode",
            code="invalid_configuration",
        )
    _validate_provider_credentials(
        base_url,
        api_key,
        family=family,
        required=auth_mode != "none",
    )
    if auth_mode == "none":
        return {}
    if auth_mode == "x-api-key":
        _validate_header_value(f"{family} API key", api_key)
        return {"x-api-key": str(api_key)}
    authorization = f"Bearer {api_key}"
    _validate_header_value(f"{family} authorization", authorization)
    return {"Authorization": authorization}


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
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, IncompleteRead):
        code = "response_truncated"
    elif isinstance(reason, RemoteDisconnected):
        code = "remote_disconnect"
    elif isinstance(reason, ConnectionResetError):
        code = "connection_reset"
    elif isinstance(reason, ssl.SSLError):
        code = "tls_error"
    elif isinstance(reason, TimeoutError):
        code = "timeout"
    else:
        code = "network_error"
    return ProviderTransportError(
        f"{family} request failed: {code}",
        code=code,
        retryable=retryable,
    )


def _open_provider_request(
    client,
    request,
    *,
    family,
    retryable,
    detect_reasoning_replay=False,
):
    client.last_transport_attempts += 1
    try:
        with _provider_urlopen(request, timeout=client.timeout) as response:
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            headers = getattr(response, "headers", {}) or {}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        retry_after = _retry_after_seconds(getattr(exc, "headers", None))
        try:
            error_body = exc.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except Exception:
            error_body = b""
        try:
            exc.close()
        except Exception:
            pass
        error_text = (
            error_body.decode("utf-8", errors="ignore").casefold()
            if len(error_body) <= MAX_PROVIDER_RESPONSE_BYTES
            else ""
        )
        context_markers = (
            "context_length_exceeded",
            "context window",
            "maximum context length",
            "prompt is too long",
            "prompt too long",
            "too many tokens",
            "input token limit",
        )
        if status in {400, 413, 422} and any(
            marker in error_text for marker in context_markers
        ):
            code = "context_length_exceeded"
        elif status == 408:
            code = "request_timeout"
        elif status == 413:
            code = "request_too_large"
        elif status == 429:
            code = "rate_limited"
        elif 500 <= status < 600:
            code = "http_5xx"
        elif 300 <= status < 400:
            code = "redirect_blocked"
        else:
            code = "http_4xx"
        reasoning_required = (
            detect_reasoning_replay
            and status in {400, 422}
            and any(
                marker in error_text
                for marker in (
                    "reasoning state is required",
                    "reasoning content is required",
                    "missing reasoning content",
                    "must include reasoning",
                )
            )
        )
        raise ProviderTransportError(
            f"{family} request failed with HTTP {status}",
            code=code,
            http_status=status,
            retryable=retryable
            and code in {"request_timeout", "rate_limited", "http_5xx"},
            retry_after=retry_after if code == "rate_limited" else None,
            protocol_reason=(
                "reasoning_replay_required" if reasoning_required else None
            ),
        ) from None
    except (urllib.error.URLError, HTTPException, OSError) as exc:
        raise _network_failure(family, exc, retryable=retryable) from None
    if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProviderTransportError(
            f"{family} error: response_too_large",
            code="response_too_large",
        )
    return body, headers


def _retry_after_seconds(headers):
    if headers is None:
        return None
    value = headers.get("Retry-After", "")
    try:
        seconds = float(str(value).strip())
    except ValueError:
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return min(10.0, max(0.0, seconds))


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


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    usage = _mapping_or_empty(data.get("usage"))
    input_tokens = _optional_int(usage.get("input_tokens", usage.get("prompt_tokens")))
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
