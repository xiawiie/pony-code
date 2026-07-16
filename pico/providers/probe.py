"""Explicit, bounded model/tool-loop probe primitives."""

from pico.action_codec import FinalAction, ToolAction, decode_action
from pico.messages import make_tool_pair

from ._shared import _ProviderFailure


_PROBE_TOOL = {
    "name": "pico_probe",
    "description": "Return the supplied probe value.",
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}
_PROBE_SYSTEM = [{"type": "text", "text": "Follow the probe request exactly."}]
_PROBE_TEXT_MAX_TOKENS = 16
_PROBE_TOOL_MAX_TOKENS = 128


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
    if not isinstance(exc, _ProviderFailure):
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
    }
    if isinstance(exc, _ProviderFailure):
        report["error_code"] = exc.code
        if exc.http_status is not None:
            report["http_status"] = exc.http_status
    return report


def probe_model_client(client):
    """Verify text, one native tool call, and tool-result continuation."""
    model_calls = 0
    try:
        model_calls += 1
        text_response = client.complete(
            system=_PROBE_SYSTEM,
            tools=[],
            messages=[{"role": "user", "content": "Reply with the word ready."}],
            max_tokens=_PROBE_TEXT_MAX_TOKENS,
        )
    except Exception as exc:
        return _failed(client, "text", exc, model_calls)
    if not isinstance(decode_action(text_response), FinalAction):
        return _failed(client, "text", None, model_calls)

    try:
        model_calls += 1
        tool_response = client.complete(
            system=_PROBE_SYSTEM,
            tools=[_PROBE_TOOL],
            messages=[
                {
                    "role": "user",
                    "content": "Call pico_probe once with value ping.",
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
        tool_action.name != "pico_probe"
        or tool_action.arguments != {"value": "ping"}
        or not tool_action.tool_use_id
    ):
        return {
            **_failed(client, "tool_call", None, model_calls),
            "category": "tool_call_invalid",
        }
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
                    "content": "Call pico_probe once with value ping.",
                },
                assistant,
                result,
            ],
            max_tokens=_PROBE_TOOL_MAX_TOKENS,
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
    }
