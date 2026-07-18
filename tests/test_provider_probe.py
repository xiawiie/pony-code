from unittest.mock import Mock
from types import SimpleNamespace
import json

import pytest

import pony.providers.transport as provider_shared
import pony.providers.probe as probe_module
from pony.config.model import resolve_model_config
from pony.providers.transport import ProviderTransportError
from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient
from pony.providers.probe import probe_model_client, resolve_provider_client
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


def test_probe_payload_contains_only_fixed_synthetic_content():
    client = _successful_client()

    probe_model_client(client)

    payload = json.dumps(client.calls, sort_keys=True)
    assert "pony_probe" in payload
    assert "ping" in payload
    assert "pong" in payload
    for canary in ("user-task-canary", "repository-canary", "memory-canary"):
        assert canary not in payload


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
        protocol_family="secret protocol",
    )

    assert failure.stage is None
    assert failure.protocol_reason is None
    assert failure.protocol_family is None


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


def _auto_config(*, provider="auto", key="test-key", required=True):
    return resolve_model_config(
        project_env={
            "PONY_PROVIDER": provider,
            "PONY_API_BASE": "https://gateway.example/v1",
            "PONY_API_KEY": key,
            "PONY_MODEL": "test-model",
        },
        process_env={},
        required=required,
    )


def _failed_report(*, code="provider_protocol_mismatch", status=None):
    report = {
        "status": "failed",
        "stage": "tool_call",
        "category": "response_invalid",
        "model_calls": 1,
        "usage_status": "degraded",
        "error_code": code,
    }
    if status is not None:
        report["http_status"] = status
    return report


def test_auto_detection_uses_candidate_order_and_returns_first_success(monkeypatch):
    built = []
    clients = []
    reports = iter(
        [
            _failed_report(),
            {
                "status": "ok",
                "stage": "complete",
                "category": "ok",
                "model_calls": 2,
                "usage_status": "degraded",
            },
        ]
    )

    def build(config, timeout):
        built.append((config["protocol"]["value"], timeout))
        client = SimpleNamespace(provider_metadata={})
        clients.append(client)
        return client

    monkeypatch.setattr(probe_module, "_client_from_config", build)
    monkeypatch.setattr(probe_module, "probe_model_client", lambda _client: next(reports))

    client, resolved, report = resolve_provider_client(
        _auto_config(),
        timeout=60,
        verify_resolved=True,
    )

    assert client is not None
    assert [protocol for protocol, _timeout in built] == [
        "openai_chat_completions",
        "openai_responses",
        "openai_responses",
    ]
    assert all(timeout <= 30 for _protocol, timeout in built[:2])
    assert built[2][1] == 60
    assert client is clients[2]
    assert client is not clients[1]
    assert resolved["resolved_provider"]["value"] == "openai-responses"
    assert report["candidate_count"] == 2
    assert report["model_calls"] == 3
    assert report["usage_status"] == "degraded"
    assert client.provider_resolution_metadata == {
        "resolution_source": "probe",
        "protocol": "openai_responses",
        "candidate_count": 2,
        "probe_model_calls": 3,
        "usage_status": "degraded",
    }


def test_auto_auth_failure_skips_sibling_protocol_family(monkeypatch):
    built = []
    reports = iter(
        [
            _failed_report(code="http_4xx", status=401),
            {
                "status": "ok",
                "stage": "complete",
                "category": "ok",
                "model_calls": 2,
                "usage_status": "complete",
            },
        ]
    )

    def build(config, _timeout):
        built.append(config["protocol"]["value"])
        return object()

    monkeypatch.setattr(probe_module, "_client_from_config", build)
    monkeypatch.setattr(probe_module, "probe_model_client", lambda _client: next(reports))

    _client, resolved, _report = resolve_provider_client(
        _auto_config(),
        timeout=2,
        verify_resolved=True,
    )

    assert built == [
        "openai_chat_completions",
        "anthropic_messages",
        "anthropic_messages",
    ]
    assert resolved["resolved_provider"]["value"] == "anthropic"


def test_openai_auth_and_transient_failures_stop_without_fallback(monkeypatch):
    for provider, failure in (
        ("openai", _failed_report(code="http_4xx", status=401)),
        ("auto", _failed_report(code="timeout")),
    ):
        built = []
        monkeypatch.setattr(
            probe_module,
            "_client_from_config",
            lambda config, _timeout: built.append(config["protocol"]["value"])
            or object(),
        )
        monkeypatch.setattr(
            probe_module,
            "probe_model_client",
            lambda _client, failure=failure: failure,
        )

        with pytest.raises(ProviderTransportError) as caught:
            resolve_provider_client(
                _auto_config(provider=provider),
                timeout=2,
                verify_resolved=True,
            )

        assert len(built) == 1
        assert caught.value.code == failure["error_code"]


def test_detection_caps_candidates_and_model_calls(monkeypatch):
    builder = Mock(return_value=object())
    probe = Mock(
        return_value={
            **_failed_report(),
            "model_calls": 2,
        }
    )
    monkeypatch.setattr(probe_module, "_client_from_config", builder)
    monkeypatch.setattr(probe_module, "probe_model_client", probe)

    with pytest.raises(ProviderTransportError) as caught:
        resolve_provider_client(
            _auto_config(),
            timeout=30,
            verify_resolved=True,
        )

    assert caught.value.code == "provider_detection_failed"
    assert builder.call_count == 3
    assert probe.call_count == 3


def test_detection_wall_cap_stops_before_starting_late_candidate(monkeypatch):
    builder = Mock(side_effect=AssertionError("late candidate was built"))
    clock = Mock(side_effect=(0.0, 91.0))
    monkeypatch.setattr(probe_module, "_client_from_config", builder)

    with pytest.raises(ProviderTransportError) as caught:
        resolve_provider_client(
            _auto_config(),
            timeout=30,
            verify_resolved=True,
            clock=clock,
        )

    assert caught.value.code == "timeout"
    builder.assert_not_called()


def test_detection_without_required_key_performs_zero_model_calls(monkeypatch):
    builder = Mock(side_effect=AssertionError("client must not be built"))
    monkeypatch.setattr(probe_module, "_client_from_config", builder)

    with pytest.raises(ProviderTransportError) as caught:
        resolve_provider_client(
            _auto_config(key="", required=False),
            timeout=2,
            verify_resolved=True,
        )

    assert caught.value.code == "api_key_not_configured"
    builder.assert_not_called()


def test_resolved_target_builds_without_probe_unless_verification_requested(
    monkeypatch,
):
    config = resolve_model_config(
        project_env={
            "PONY_PROVIDER": "openai-responses",
            "PONY_API_BASE": "https://api.openai.com/v1",
            "PONY_API_KEY": "test-key",
            "PONY_MODEL": "test-model",
        },
        process_env={},
    )
    client = SimpleNamespace(provider_metadata={})
    builder = Mock(return_value=client)
    probe = Mock(
        return_value={
            "status": "ok",
            "stage": "complete",
            "category": "ok",
            "model_calls": 2,
            "usage_status": "complete",
        }
    )
    monkeypatch.setattr(probe_module, "_client_from_config", builder)
    monkeypatch.setattr(probe_module, "probe_model_client", probe)

    built, resolved, report = resolve_provider_client(config, timeout=4)

    assert (built, resolved) == (client, config)
    assert report["status"] == "not_run"
    assert client.provider_resolution_metadata == {
        "resolution_source": "explicit",
        "protocol": "openai_responses",
        "candidate_count": 0,
        "probe_model_calls": 0,
        "usage_status": "not_checked",
    }
    probe.assert_not_called()

    resolve_provider_client(config, timeout=4, verify_resolved=True)
    probe.assert_called_once_with(client)


def test_forced_provider_verification_preserves_protocol_error(monkeypatch):
    config = resolve_model_config(
        project_env={
            "PONY_PROVIDER": "anthropic",
            "PONY_API_BASE": "https://gateway.example/v1",
            "PONY_API_KEY": "test-key",
            "PONY_MODEL": "test-model",
        },
        process_env={},
    )
    monkeypatch.setattr(probe_module, "_client_from_config", lambda *_args: object())
    monkeypatch.setattr(
        probe_module,
        "probe_model_client",
        lambda _client: _failed_report(),
    )

    with pytest.raises(ProviderTransportError) as caught:
        resolve_provider_client(config, timeout=2, verify_resolved=True)

    assert caught.value.code == "provider_protocol_mismatch"
    assert caught.value.protocol_family == "anthropic_messages"
