import io
import json
import urllib.error
from unittest.mock import Mock

import pytest

import pony.providers.transport as provider_shared
from pony.providers.transport import ProviderTransportError
from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient
from pony.providers.response import StopReason


class _Response:
    def __init__(self, payload, headers=None):
        self.body = json.dumps(payload).encode()
        self.headers = dict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def _client(**overrides):
    values = {
        "model": "qwen-test",
        "base_url": "https://gateway.example/v7",
        "api_key": "test-key",
        "temperature": 0.0,
        "timeout": 30,
        "auth_mode": "bearer",
        "capabilities": {},
    }
    values.update(overrides)
    return OpenAIChatCompletionsModelClient(**values)


def _tool_schema():
    return {
        "name": "search",
        "description": "Search files",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    }


def _complete(client, *, tools=(), messages=None):
    return client.complete(
        system=[{"type": "text", "text": "SYSTEM"}],
        tools=list(tools),
        messages=list(messages or [{"role": "user", "content": "hello"}]),
        max_tokens=42,
    )


def test_chat_uses_exact_gateway_root_and_native_tool_history(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(
            {
                "id": "chat_1",
                "model": "qwen-effective",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            },
            {"x-request-id": "req_1"},
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    messages = [
        {"role": "user", "content": "find x"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "search",
                    "input": {"pattern": "x"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "match",
                }
            ],
        },
    ]

    response = _complete(
        _client(),
        tools=[_tool_schema()],
        messages=messages,
    )

    assert captured["url"] == "https://gateway.example/v7/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert "enable_thinking" not in captured["body"]
    assert "thinking" not in captured["body"]
    assert "parallel_tool_calls" not in captured["body"]
    assert "strict" not in captured["body"]["tools"][0]["function"]
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "find x"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": '{"pattern":"x"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "match"},
    ]
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [{"type": "text", "text": "done"}]
    assert response.usage["request_id"] == "req_1"
    assert response.usage["total_tokens"] == 10
    assert response.usage["input_tokens"] == 8
    assert response.usage["output_tokens"] == 2
    assert _client().provider_binding == {
        "protocol_family": "openai_chat_completions",
        "model": "qwen-test",
        "endpoint_hash": _client().provider_binding["endpoint_hash"],
    }


@pytest.mark.parametrize(
    "usage",
    [
        {
            "input_tokens": None,
            "output_tokens": None,
            "prompt_tokens": 8,
            "completion_tokens": 2,
            "total_tokens": 10,
        },
        {"prompt_tokens": 8, "completion_tokens": 2},
    ],
)
def test_chat_normalizes_compatible_usage_aliases(monkeypatch, usage):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "usage": usage,
            }
        ),
    )

    response = _complete(_client())

    assert response.usage["input_tokens"] == 8
    assert response.usage["output_tokens"] == 2
    assert response.usage["total_tokens"] == 10


def test_chat_parses_multiple_native_tool_calls(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"pattern":"x","path":null}',
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"pattern":"y"}',
                                    },
                                },
                            ],
                        },
                    }
                ],
                "usage": {},
            }
        ),
    )

    response = _complete(_client(), tools=[_tool_schema()])

    assert response.stop_reason == StopReason.TOOL_USE
    assert [block["id"] for block in response.content] == ["call_1", "call_2"]
    assert response.content[0]["input"] == {"pattern": "x"}


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", StopReason.END_TURN),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.REFUSAL),
        ("future_reason", StopReason.UNKNOWN),
    ],
)
def test_chat_finish_reason_mapping(monkeypatch, finish_reason, expected):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": "text"},
                    }
                ],
                "usage": {},
            }
        ),
    )

    assert _complete(_client()).stop_reason == expected


def test_chat_discards_reasoning_and_normalizes_gateway_tool_shape(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "opaque reasoning",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "search",
                                        "arguments": {"pattern": "x"},
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {},
            }
        ),
    )

    response = _complete(_client(), tools=[_tool_schema()])

    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "search",
            "input": {"pattern": "x"},
        }
    ]


@pytest.mark.parametrize(
    ("tool_call", "reason"),
    [
        (
            {
                "type": "function",
                "function": {"name": "search", "arguments": {"pattern": "x"}},
            },
            "tool_call_shape_invalid",
        ),
        (
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": []},
            },
            "tool_arguments_invalid",
        ),
    ],
)
def test_chat_reports_safe_tool_failure_reason(monkeypatch, tool_call, reason):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": None, "tool_calls": [tool_call]},
                    }
                ]
            }
        ),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_client(), tools=[_tool_schema()])

    assert caught.value.code == "provider_protocol_mismatch"
    assert caught.value.stage == "tool_call"
    assert caught.value.protocol_reason == reason


def test_chat_reports_missing_tool_call_and_malformed_response(monkeypatch):
    responses = iter(
        [
            _Response(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {"content": None},
                        }
                    ]
                }
            ),
            _Response({"choices": []}),
        ]
    )
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(ProviderTransportError) as missing:
        _complete(_client(), tools=[_tool_schema()])
    with pytest.raises(ProviderTransportError) as malformed:
        _complete(_client())

    assert missing.value.stage == "tool_call"
    assert missing.value.protocol_reason == "tool_call_missing"
    assert malformed.value.stage == "response_decode"
    assert malformed.value.protocol_reason == "response_shape_invalid"


def test_chat_preserves_http_400_classification(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(
            side_effect=urllib.error.HTTPError(
                "https://gateway.example/v7/chat/completions",
                400,
                "bad request",
                hdrs={},
                fp=io.BytesIO(b'{"error":"bad thinking state"}'),
            )
        ),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_client(), tools=[_tool_schema()])

    assert caught.value.code == "http_4xx"
    assert caught.value.http_status == 400


@pytest.mark.parametrize(
    ("body", "reason"),
    (
        (b'{"error":"tool result rejected"}', "tool_result_rejected"),
        (
            b'{"error":"reasoning state is required for continuation"}',
            "reasoning_replay_required",
        ),
    ),
)
def test_chat_classifies_tool_continuation_rejection(monkeypatch, body, reason):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(
            side_effect=urllib.error.HTTPError(
                "https://gateway.example/v7/chat/completions",
                400,
                "bad request",
                hdrs={},
                fp=io.BytesIO(body),
            )
        ),
    )
    messages = [
        {"role": "user", "content": "find x"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "search",
                    "input": {"pattern": "x"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "result",
                }
            ],
        },
    ]

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_client(), tools=[_tool_schema()], messages=messages)

    assert caught.value.code == "http_4xx"
    assert caught.value.stage == "tool_result"
    assert caught.value.protocol_reason == reason
