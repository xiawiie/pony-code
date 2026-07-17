"""Anthropic structured completion payload and response contracts."""

import json
from http.client import RemoteDisconnected
import urllib.error
from unittest.mock import MagicMock, Mock, patch

import pytest

import pony.providers.transport as provider_shared
from pony.providers.anthropic_messages import AnthropicMessagesModelClient
from pony.providers.response import Response, StopReason


def _mock_urlopen(response_body):
    m = MagicMock()
    m.__enter__.return_value = MagicMock(
        read=lambda *_args: json.dumps(response_body).encode("utf-8")
    )
    m.__exit__.return_value = False
    return m


def _make_client():
    return AnthropicMessagesModelClient(
        model="claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
        auth_mode="x-api-key",
        capabilities={
            "prompt_cache": True,
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    )


def test_complete_payload_shape_and_cache_control():
    client = _make_client()
    system = [
        {"type": "text", "text": "SYSTEM_CORE", "cache_control": {"type": "ephemeral"}}
    ]
    tools = [
        {
            "name": "read_file",
            "description": "d",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    messages = [{"role": "user", "content": "hi", "_pony_meta": {"secret": "x"}}]

    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload["url"] = req.full_url
        captured_payload["headers"] = dict(req.header_items())
        captured_payload["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen(
            {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 5,
                },
            }
        )

    with patch("pony.providers.transport._provider_urlopen", fake_urlopen):
        resp = client.complete(
            system=system, tools=tools, messages=messages, max_tokens=100
        )

    assert captured_payload["url"] == "https://api.anthropic.com/v1/messages"
    assert captured_payload["headers"]["X-api-key"] == "test-key"
    assert captured_payload["data"]["system"] == system
    assert captured_payload["data"]["tools"] == [{**tools[0], "strict": True}]
    assert captured_payload["data"]["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }
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
        return _mock_urlopen(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        )

    with patch("pony.providers.transport._provider_urlopen", fake_urlopen):
        client.complete(
            system=system,
            tools=[],
            messages=messages,
            max_tokens=10,
            cache_breakpoints=[1],
        )
    msg1 = captured["data"]["messages"][1]
    assert msg1["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_complete_tool_use_response():
    client = _make_client()

    def fake_urlopen(req, timeout=None):
        return _mock_urlopen(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    }
                ],
            "stop_reason": "tool_use",
            "usage": {},
            }
        )

    with patch("pony.providers.transport._provider_urlopen", fake_urlopen):
        resp = client.complete(
            system=[{"type": "text", "text": "s"}],
            tools=[],
            messages=[{"role": "user", "content": "x"}],
            max_tokens=10,
        )
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.content[0]["name"] == "read_file"
    assert resp.content[0]["input"]["path"] == "a.py"


def test_thinking_tool_state_is_preserved_and_replayed(monkeypatch):
    thinking = {
        "type": "thinking",
        "thinking": "use the file tool",
        "signature": "opaque-signature",
    }
    redacted = {"type": "redacted_thinking", "data": "opaque-redacted"}
    queued = [
        {
            "content": [
                thinking,
                redacted,
                {
                    "type": "tool_use",
                    "id": "toolu_thinking",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {},
        },
        {
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {},
        },
    ]
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append(json.loads(request.data))
        return _mock_urlopen(queued.pop(0))

    monkeypatch.setattr(provider_shared, "_provider_urlopen", fake_urlopen)
    client = _make_client()
    first = client.complete(
        system=[],
        tools=[],
        messages=[{"role": "user", "content": "read"}],
        max_tokens=10,
    )
    assert first.provider_state == [thinking, redacted]

    client.complete(
        system=[],
        tools=[],
        messages=[
            {"role": "user", "content": "read"},
            {
                "role": "assistant",
                "content": [first.content[-1]],
                "_pony_provider_state": first.provider_state,
            },
            {
                "role": "user",
                "content": [
                    {
                    "type": "tool_result",
                    "tool_use_id": "toolu_thinking",
                    "content": "body",
                    }
                ],
            },
        ],
        max_tokens=10,
    )

    assert captured[1]["messages"][1]["content"] == [
        thinking,
        redacted,
        first.content[-1],
    ]


def test_empty_anthropic_object_is_protocol_mismatch(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _mock_urlopen({}),
    )

    with pytest.raises(provider_shared.ProviderTransportError) as caught:
        _make_client().complete(system=[], tools=[], messages=[], max_tokens=10)

    assert caught.value.code == "provider_protocol_mismatch"


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

    with patch("pony.providers.transport._provider_urlopen", fake_urlopen):
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

    with patch(
        "pony.providers.transport._provider_urlopen",
        return_value=_mock_urlopen(response_body),
    ):
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
def test_complete_network_error_is_classified_after_one_transport(
    monkeypatch, error, reason
):
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(
        RuntimeError,
        match=rf"^Anthropic request failed: {reason}$",
    ):
        _make_client().complete(system=[], tools=[], messages=[], max_tokens=10)

    assert urlopen.call_count == 1


def test_complete_rejects_non_header_api_key_before_request(monkeypatch):
    urlopen = Mock()
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _make_client()
    client.api_key = "bad\u2603"

    with pytest.raises(RuntimeError, match="cannot be sent in HTTP headers"):
        client.complete(system=[], tools=[], messages=[], max_tokens=10)

    urlopen.assert_not_called()


@pytest.mark.parametrize(
    ("base_url", "messages_url"),
    [
        (
            "https://gateway.example/anthropic/v1",
            "https://gateway.example/anthropic/v1/messages",
        ),
        (
            "https://lumina.tripo3d.com/v1",
            "https://lumina.tripo3d.com/v1/messages",
        ),
    ],
)
def test_custom_anthropic_surface_omits_unsupported_extension_fields(
    monkeypatch,
    base_url,
    messages_url,
):
    captured = {}
    client = AnthropicMessagesModelClient(
        model="custom-test",
        base_url=base_url,
        api_key="test-key",
        temperature=0.0,
        timeout=30,
        auth_mode="x-api-key",
        capabilities={
            "prompt_cache": False,
            "strict_tools": False,
            "parallel_tool_control": False,
        },
    )
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda request, timeout: (
            captured.update(
                {
                "url": request.full_url,
                "body": json.loads(request.data),
                }
            )
            or _mock_urlopen(
                {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
                }
            )
        ),
    )

    client.complete(
        system=[
            {
            "type": "text",
            "text": "system",
            "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[
            {
            "name": "read_file",
            "description": "read",
            "input_schema": {"type": "object", "properties": {}},
            }
        ],
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=10,
        cache_breakpoints=[0],
    )

    assert client.supports_prompt_cache is False
    assert captured["url"] == messages_url
    assert "cache_control" not in captured["body"]["system"][0]
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert "strict" not in captured["body"]["tools"][0]
    assert "tool_choice" not in captured["body"]
    assert "thinking" not in captured["body"]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://notanthropic.com/v1",
        "https://example.test/anthropic.com/v1",
        "https://www.right.codes/claude/v1",
    ],
)
def test_custom_endpoint_never_claims_prompt_cache(base_url):
    client = AnthropicMessagesModelClient(
        model="test",
        base_url=base_url,
        api_key="test-key",
        temperature=None,
        timeout=30,
        auth_mode="x-api-key",
        capabilities={},
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
