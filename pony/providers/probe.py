"""Explicit, bounded model/tool-loop probe primitives."""

import time

from pony.agent.action_codec import FinalAction, ToolAction, decode_action
from pony.agent.messages import make_tool_pair

from .transport import ProviderTransportError


_PROBE_TOOL = {
    "name": "pony_probe",
    "description": "Return the supplied probe value.",
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}
_PROBE_SYSTEM = [{"type": "text", "text": "Follow the probe request exactly."}]
_PROBE_TOOL_MAX_TOKENS = 128
_PROBE_CONTINUATION_MAX_TOKENS = 32
_DETECTION_MAX_CANDIDATES = 3
_DETECTION_REQUEST_TIMEOUT_SECONDS = 30.0
_DETECTION_WALL_SECONDS = 90.0
_TRANSIENT_FAILURE_CODES = frozenset(
    {
        "connection_reset",
        "http_5xx",
        "network_error",
        "rate_limited",
        "redirect_blocked",
        "remote_disconnect",
        "request_timeout",
        "timeout",
        "tls_error",
    }
)
_PROTOCOL_FAILURE_CODES = frozenset(
    {"provider_protocol_mismatch", "unsupported_stop_reason"}
)


def _probe_identity(client):
    binding = getattr(client, "provider_binding", {})
    binding = dict(binding) if isinstance(binding, dict) else {}
    return {
        key: str(binding.get(key, ""))
        for key in (
            "protocol_family",
            "model",
            "endpoint_hash",
        )
        if binding.get(key) not in {None, ""}
    }


def _failure_category(exc):
    if not isinstance(exc, ProviderTransportError):
        return "probe_internal_error"
    if exc.code == "missing_credentials" or exc.http_status == 401:
        return "authentication_failed"
    if exc.http_status == 403:
        return "forbidden"
    if exc.http_status == 404:
        return "not_found"
    if exc.code in {"network_error", "remote_disconnect", "connection_reset"}:
        return "connection_failed"
    if exc.code in {"timeout", "request_timeout"}:
        return "timeout"
    if exc.code == "tls_error":
        return "tls_failed"
    if exc.code == "request_too_large":
        return "request_too_large"
    if exc.code == "response_truncated":
        return "response_truncated"
    if exc.code == "rate_limited":
        return "rate_limited"
    if exc.code == "http_5xx":
        return "provider_unavailable"
    if exc.code in {"provider_protocol_mismatch", "unsupported_stop_reason"}:
        return "response_invalid"
    if exc.code == "invalid_configuration":
        return "configuration_invalid"
    return "request_failed"


def _failed(client, stage, exc, model_calls):
    report = {
        "status": "failed",
        "stage": stage,
        "category": _failure_category(exc),
        "model_calls": model_calls,
        "binding": _probe_identity(client),
        "usage_status": "degraded",
    }
    if isinstance(exc, ProviderTransportError):
        report["error_code"] = exc.code
        if exc.protocol_reason is not None:
            report["protocol_reason"] = exc.protocol_reason
        if exc.http_status is not None:
            report["http_status"] = exc.http_status
    return report


def _usage_complete(response):
    usage = getattr(response, "usage", None)
    return isinstance(usage, dict) and all(
        type(usage.get(name)) is int and usage[name] >= 0
        for name in ("input_tokens", "output_tokens", "total_tokens")
    )


def probe_model_client(client):
    """Verify one native tool call and a final tool-result continuation."""
    model_calls = 0
    try:
        model_calls += 1
        tool_response = client.complete(
            system=_PROBE_SYSTEM,
            tools=[_PROBE_TOOL],
            messages=[
                {
                    "role": "user",
                    "content": "Call pony_probe once with value ping.",
                }
            ],
            max_tokens=_PROBE_TOOL_MAX_TOKENS,
        )
    except Exception as exc:
        return _failed(client, "tool_call", exc, model_calls)
    tool_action = decode_action(tool_response)
    if not isinstance(tool_action, ToolAction):
        return {
            **_failed(client, "tool_call", None, model_calls),
            "category": "tool_call_not_supported",
        }
    if (
        tool_action.name != "pony_probe"
        or tool_action.arguments != {"value": "ping"}
        or not tool_action.tool_use_id
    ):
        return {
            **_failed(client, "tool_call", None, model_calls),
            "category": "tool_call_invalid",
        }
    usage_complete = _usage_complete(tool_response)
    assistant, result = make_tool_pair(
        name=tool_action.name,
        arguments=tool_action.arguments,
        tool_use_id=tool_action.tool_use_id,
        result_content="pong",
        created_at="probe",
        tool_status="ok",
        effect_class="read_only",
        provider_state=tool_action.provider_state,
    )
    try:
        model_calls += 1
        continuation = client.complete(
            system=_PROBE_SYSTEM,
            tools=[_PROBE_TOOL],
            messages=[
                {
                    "role": "user",
                    "content": "Call pony_probe once with value ping.",
                },
                assistant,
                result,
            ],
            max_tokens=_PROBE_CONTINUATION_MAX_TOKENS,
        )
    except Exception as exc:
        return _failed(client, "tool_result", exc, model_calls)
    if not isinstance(decode_action(continuation), FinalAction):
        return {
            **_failed(client, "tool_result", None, model_calls),
            "category": "tool_result_continuation_failed",
        }
    return {
        "status": "ok",
        "stage": "complete",
        "category": "ok",
        "model_calls": model_calls,
        "binding": _probe_identity(client),
        "usage_status": (
            "complete"
            if usage_complete and _usage_complete(continuation)
            else "degraded"
        ),
    }


def _client_from_config(config, timeout, *, client_builder=None):
    if client_builder is None:
        from .factory import build_transport_client

        client_builder = build_transport_client

    return client_builder(
        config["protocol"]["value"],
        model=config["model"]["value"],
        base_url=config["base_url"]["value"],
        api_key=config["api_key"]["value"],
        timeout=timeout,
        auth_mode=config["auth_mode"]["value"],
        capabilities=config["capabilities"],
    )


def _candidate_configs(config, verify_resolved):
    status = config.get("resolution_status")
    if status == "resolved":
        return [config] if verify_resolved else []
    if status != "probe_required":
        raise ValueError(config.get("resolution_error") or "provider_detection_failed")

    from pony.config.model import resolve_provider_candidate

    return [
        resolve_provider_candidate(config, item["protocol"])
        for item in list(config.get("candidates", []))[:_DETECTION_MAX_CANDIDATES]
    ]


def _protocol_family(protocol):
    return "openai" if str(protocol).startswith("openai_") else str(protocol)


def _can_try_next(report, selector):
    code = report.get("error_code", "")
    status = report.get("http_status")
    if code in _TRANSIENT_FAILURE_CODES or status == 429 or (
        type(status) is int and 500 <= status < 600
    ):
        return False
    if status in {401, 403} or code == "missing_credentials":
        return selector == "auto"
    if code in _PROTOCOL_FAILURE_CODES:
        return True
    if type(status) is int and 400 <= status < 500:
        return True
    return report.get("category") in {
        "response_invalid",
        "tool_call_invalid",
        "tool_call_not_supported",
        "tool_result_continuation_failed",
    }


def _probe_error(report, *, protocol, exhausted):
    code = (
        "provider_detection_failed"
        if exhausted
        else report.get("error_code") or "provider_detection_failed"
    )
    return ProviderTransportError(
        "Provider verification failed",
        code=code,
        http_status=report.get("http_status"),
        stage=report.get("stage"),
        protocol_reason=report.get("protocol_reason"),
        protocol_family=protocol,
    )


def _record_resolution_metadata(client, config, report):
    metadata = {
        "resolution_source": config.get("resolution_source", ""),
        "protocol": config.get("protocol", {}).get("value", ""),
        "candidate_count": int(report.get("candidate_count", 0) or 0),
        "probe_model_calls": int(report.get("model_calls", 0) or 0),
        "usage_status": str(
            report.get("usage_status", "not_checked") or "not_checked"
        ),
    }
    try:
        client.provider_resolution_metadata = metadata
    except (AttributeError, TypeError):
        pass


def resolve_provider_client(
    config,
    *,
    timeout,
    verify_resolved=False,
    clock=time.monotonic,
    client_builder=None,
):
    """Build or detect one client without replaying a real user request."""
    candidates = _candidate_configs(config, verify_resolved)
    builder_args = (
        {} if client_builder is None else {"client_builder": client_builder}
    )
    if not candidates:
        client = _client_from_config(
            config,
            timeout,
            **builder_args,
        )
        report = {
            "status": "not_run",
            "candidate_count": 0,
            "model_calls": 0,
            "usage_status": "not_checked",
        }
        _record_resolution_metadata(client, config, report)
        return client, config, report
    if not config.get("api_key", {}).get("value") and all(
        item["auth_mode"]["value"] != "none" for item in candidates
    ):
        protocol = candidates[0]["protocol"]["value"] if len(candidates) == 1 else None
        raise ProviderTransportError(
            "Provider verification failed",
            code="api_key_not_configured",
            stage="runtime",
            protocol_family=protocol,
        )

    request_timeout = min(float(timeout), _DETECTION_REQUEST_TIMEOUT_SECONDS)
    deadline = clock() + _DETECTION_WALL_SECONDS
    selector = config.get("provider", {}).get("value", "")
    attempted = 0
    model_calls = 0
    last_report = None
    last_protocol = None
    skipped_family = ""
    for candidate in candidates:
        protocol = candidate["protocol"]["value"]
        family = _protocol_family(protocol)
        if skipped_family and family == skipped_family:
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProviderTransportError(
                "Provider verification failed",
                code="timeout",
                stage="runtime",
                protocol_family=protocol,
            )
        client = _client_from_config(
            candidate,
            min(request_timeout, max(0.001, remaining / 2)),
            **builder_args,
        )
        report = probe_model_client(client)
        attempted += 1
        model_calls += int(report.get("model_calls", 0) or 0)
        last_report = report
        last_protocol = protocol
        if report.get("status") == "ok":
            completed = {
                **report,
                "candidate_count": attempted,
                "model_calls": model_calls,
                "protocol": protocol,
                "resolution_source": candidate.get("resolution_source", ""),
            }
            _record_resolution_metadata(client, candidate, completed)
            return client, candidate, completed
        if not _can_try_next(report, selector):
            raise _probe_error(report, protocol=protocol, exhausted=False)
        if (
            selector == "auto"
            and (
                report.get("http_status") in {401, 403}
                or report.get("error_code") == "missing_credentials"
            )
        ):
            skipped_family = family

    if last_report is None:
        raise ProviderTransportError(
            "Provider verification failed",
            code="provider_detection_failed",
            stage="runtime",
        )
    raise _probe_error(
        last_report,
        protocol=last_protocol,
        exhausted=config.get("resolution_status") == "probe_required",
    )
