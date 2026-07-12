"""Anthropic structured completion payload and response contracts."""
import json
from http.client import RemoteDisconnected
import urllib.error
from unittest.mock import MagicMock, Mock, patch

import pytest

import pico.providers._shared as provider_shared
from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
from pico.providers.response import Response, StopReason


def _mock_urlopen(response_body):
    m = MagicMock()
    m.__enter__.return_value = MagicMock(read=lambda *_args: json.dumps(response_body).encode("utf-8"))
    m.__exit__.return_value = False
    return m


def _make_client():
    return AnthropicCompatibleModelClient(
        model="claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
    )


def test_complete_payload_shape_and_cache_control():
    client = _make_client()
    system = [{"type": "text", "text": "SYSTEM_CORE", "cache_control": {"type": "ephemeral"}}]
    tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    messages = [{"role": "user", "content": "hi", "_pico_meta": {"secret": "x"}}]

    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen({
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5},
        })

    with patch("pico.providers._shared._provider_urlopen", fake_urlopen):
        resp = client.complete(system=system, tools=tools, messages=messages, max_tokens=100)

    assert captured_payload["data"]["system"] == system
    assert captured_payload["data"]["tools"] == tools
    assert captured_payload["data"]["messages"] == [{"role": "user", "content": "hi"}]
    assert isinstance(resp, Response)
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": "ok"}]
    assert resp.usage["cache_creation_input_tokens"] == 5
    assert not hasattr(client, "complete_text")


def test_complete_cache_breakpoint_on_message():
    client = _make_client()
    system = [{"type": "text", "text": "sys"}]
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}})
    with patch("pico.providers._shared._provider_urlopen", fake_urlopen):
        client.complete(system=system, tools=[], messages=messages, max_tokens=10, cache_breakpoints=[1])
    msg1 = captured["data"]["messages"][1]
    assert msg1["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_complete_tool_use_response():
    client = _make_client()
    def fake_urlopen(req, timeout=None):
        return _mock_urlopen({
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}}],
            "stop_reason": "tool_use",
            "usage": {},
        })
    with patch("pico.providers._shared._provider_urlopen", fake_urlopen):
        resp = client.complete(system=[{"type": "text", "text": "s"}], tools=[], messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.content[0]["name"] == "read_file"
    assert resp.content[0]["input"]["path"] == "a.py"


def test_complete_unknown_stop_reason_is_unknown():
    client = _make_client()

    def fake_urlopen(req, timeout=None):
        return _mock_urlopen(
            {
                "content": [{"type": "text", "text": "ambiguous"}],
                "stop_reason": "new_wire_value",
                "usage": {},
            }
        )

    with patch("pico.providers._shared._provider_urlopen", fake_urlopen):
        response = client.complete(
            system=[{"type": "text", "text": "s"}],
            tools=[],
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
        )

    assert response.stop_reason == StopReason.UNKNOWN


def test_complete_records_cached_token_usage():
    client = _make_client()
    response_body = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 4,
            "cache_creation_input_tokens": 8,
            "cache_read_input_tokens": 12,
        },
    }

    with patch("pico.providers._shared._provider_urlopen", return_value=_mock_urlopen(response_body)):
        response = client.complete(
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
        )

    assert response.usage["cached_tokens"] == 12
    assert response.usage["cache_hit"] is True
    assert response.usage["cache_creation_input_tokens"] == 8
    assert response.usage["total_tokens"] == 44
    assert client.last_completion_metadata == response.usage


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.URLError("secret"), "network_error"),
        (RemoteDisconnected("secret"), "remote_disconnect"),
        (TimeoutError("secret"), "timeout"),
    ],
)
def test_complete_network_error_is_classified_after_one_transport(monkeypatch, error, reason):
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(
        RuntimeError,
        match=rf"^Anthropic-compatible request failed: {reason}$",
    ):
        _make_client().complete(
            system=[], tools=[], messages=[], max_tokens=10
        )

    assert urlopen.call_count == 1


def test_complete_rejects_non_header_api_key_before_request(monkeypatch):
    urlopen = Mock()
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _make_client()
    client.api_key = "bad\u2603"

    with pytest.raises(RuntimeError, match="cannot be sent in HTTP headers"):
        client.complete(system=[], tools=[], messages=[], max_tokens=10)

    urlopen.assert_not_called()


def test_deepseek_anthropic_surface_does_not_claim_prompt_cache():
    client = AnthropicCompatibleModelClient(
        model="deepseek-test",
        base_url="https://api.deepseek.com/anthropic",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
    )

    assert client.supports_prompt_cache is False


@pytest.mark.parametrize(
    "base_url",
    [
        "https://notanthropic.com/v1",
        "https://example.test/anthropic.com/v1",
    ],
)
def test_prompt_cache_capability_requires_exact_known_endpoint(base_url):
    client = AnthropicCompatibleModelClient(
        model="test",
        base_url=base_url,
        api_key="test-key",
        temperature=None,
        timeout=30,
    )

    assert client.supports_prompt_cache is False


def test_pause_turn_is_not_replayed_as_a_generic_retry(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _mock_urlopen(
            {"content": [], "stop_reason": "pause_turn", "usage": {}}
        ),
    )

    with pytest.raises(RuntimeError, match="unsupported_stop_reason"):
        _make_client().complete(system=[], tools=[], messages=[], max_tokens=10)
