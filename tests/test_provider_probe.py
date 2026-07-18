from unittest.mock import Mock

import pytest

import pony.providers.transport as provider_shared
from pony.providers.transport import ProviderTransportError
from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient
from pony.providers.probe import probe_model_client
from pony.providers.response import Response, StopReason


def _response(stop_reason, *content, usage=None):
    return Response(stop_reason=stop_reason, content=list(content), usage=usage or {})


def _text(value="ready"):
    return {"type": "text", "text": value}


def _tool(value="ping"):
    return {
        "type": "tool_use",
        "id": "call_1",
        "name": "pony_probe",
        "input": {"value": value},
    }


class _ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.provider_binding = {
            "protocol_family": "test",
            "model": "test-model",
            "endpoint_hash": "sha256:test",
        }

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _successful_client():
    usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
    return _ScriptedClient(
        [
            _response(StopReason.TOOL_USE, _tool(), usage=usage),
            _response(StopReason.END_TURN, _text("done"), usage=usage),
        ],
    )


def test_probe_verifies_tool_and_continuation_in_two_calls():
    client = _successful_client()

    report = probe_model_client(client)

    assert report == {
        "status": "ok",
        "stage": "complete",
        "category": "ok",
        "model_calls": 2,
        "binding": {
            "protocol_family": "test",
            "model": "test-model",
            "endpoint_hash": "sha256:test",
        },
        "usage_status": "complete",
    }
    assert [call["max_tokens"] for call in client.calls] == [128, 32]


def test_probe_distinguishes_tool_and_continuation_failures():
    no_tool = _ScriptedClient(
        [
            _response(StopReason.END_TURN, _text("no tool")),
        ]
    )
    bad_continuation = _ScriptedClient(
        [
            _response(StopReason.TOOL_USE, _tool()),
            _response(StopReason.TOOL_USE, _tool()),
        ]
    )

    assert probe_model_client(no_tool)["category"] == "tool_call_not_supported"
    assert (
        probe_model_client(bad_continuation)["category"]
        == "tool_result_continuation_failed"
    )


def test_probe_allows_success_with_degraded_usage():
    client = _ScriptedClient(
        [
            _response(StopReason.TOOL_USE, _tool()),
            _response(StopReason.END_TURN, _text("done")),
        ]
    )

    report = probe_model_client(client)

    assert report["status"] == "ok"
    assert report["usage_status"] == "degraded"


def test_probe_reports_safe_provider_failure_category():
    secret = "secret-token-value"
    client = _ScriptedClient(
        [
            ProviderTransportError(
                f"provider rejected {secret}",
                code="http_4xx",
                http_status=401,
            )
        ]
    )

    report = probe_model_client(client)

    assert report["category"] == "authentication_failed"
    assert report["stage"] == "tool_call"
    assert report["error_code"] == "http_4xx"
    assert report["http_status"] == 401
    assert secret not in str(report)


def test_probe_reports_degraded_usage_and_safe_protocol_reason():
    client = _ScriptedClient(
        [
            _response(StopReason.TOOL_USE, _tool()),
            ProviderTransportError(
                "safe provider failure",
                code="provider_protocol_mismatch",
                stage="tool_result",
                protocol_reason="tool_result_rejected",
            ),
        ]
    )

    report = probe_model_client(client)

    assert report["usage_status"] == "degraded"
    assert report["stage"] == "tool_result"
    assert report["protocol_reason"] == "tool_result_rejected"


def test_provider_transport_error_drops_unbounded_diagnostic_values():
    failure = ProviderTransportError(
        "safe",
        code="provider_protocol_mismatch",
        stage="secret stage",
        protocol_reason="secret response detail",
    )

    assert failure.stage is None
    assert failure.protocol_reason is None


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("connection_reset", "connection_failed"),
        ("request_timeout", "timeout"),
        ("tls_error", "tls_failed"),
        ("request_too_large", "request_too_large"),
        ("response_truncated", "response_truncated"),
    ],
)
def test_probe_preserves_specific_safe_failure_category(code, category):
    client = _ScriptedClient(
        [
            ProviderTransportError("provider request failed", code=code),
        ]
    )

    report = probe_model_client(client)

    assert report["category"] == category
    assert report["error_code"] == code


def test_probe_missing_key_performs_zero_network_requests(monkeypatch):
    urlopen = Mock(side_effect=AssertionError("network must not be called"))
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = OpenAIChatCompletionsModelClient(
        model="model",
        base_url="https://gateway.example/v1",
        api_key="",
        temperature=0.0,
        timeout=1,
    )

    report = probe_model_client(client)

    assert report["category"] == "authentication_failed"
    assert report["stage"] == "tool_call"
    assert urlopen.call_count == 0
