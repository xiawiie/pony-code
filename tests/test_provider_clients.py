import io
import json
from http.client import IncompleteRead, RemoteDisconnected
import ssl
import urllib.error
from unittest.mock import Mock

import pytest

import pony.providers.transport as provider_shared
from pony.providers.transport import ProviderTransportError
from pony.providers.factory import build_transport_client
from pony.providers.anthropic_messages import AnthropicMessagesModelClient
from pony.providers.ollama_chat import OllamaChatModelClient
from pony.providers.openai_responses import OpenAIResponsesModelClient
from pony.providers.response import StopReason


OFFICIAL_OPENAI_CAPABILITIES = {
    "strict_tools": True,
    "parallel_tool_control": True,
    "reasoning_replay": True,
}


class _Response:
    def __init__(self, body, content_type="application/json", headers=None):
        self.body = body
        self.headers = {
            "Content-Type": content_type,
            **dict(headers or {}),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def _openai_client(api_key="test-key", **overrides):
    values = {
        "model": "gpt-test",
        "base_url": "https://api.openai.com/v1",
        "api_key": api_key,
        "temperature": 0.0,
        "timeout": 30,
        "auth_mode": "bearer",
        "capabilities": OFFICIAL_OPENAI_CAPABILITIES,
    }
    values.update(overrides)
    return OpenAIResponsesModelClient(**values)


def _anthropic_client():
    return AnthropicMessagesModelClient(
        model="claude-test",
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


def _ollama_client(**overrides):
    values = {
        "model": "qwen-test",
        "host": "http://127.0.0.1:11434",
        "temperature": 0.0,
        "top_p": 0.9,
        "timeout": 30,
        "auth_mode": "none",
    }
    values.update(overrides)
    return OllamaChatModelClient(**values)


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


def _complete(client, *, tools=None, messages=None):
    return client.complete(
        system=[{"type": "text", "text": "SYSTEM"}],
        tools=list(tools or []),
        messages=list(messages or [{"role": "user", "content": "hello"}]),
        max_tokens=42,
    )


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (
            _openai_client(),
            {
                "protocol_family": "openai_responses",
                "requested_model": "gpt-test",
                "effective_model": "gpt-test",
            },
        ),
        (
            _anthropic_client(),
            {
                "protocol_family": "anthropic_messages",
                "requested_model": "claude-test",
                "effective_model": "claude-test",
            },
        ),
        (
            _ollama_client(),
            {
                "protocol_family": "ollama_chat",
                "requested_model": "qwen-test",
                "effective_model": "qwen-test",
            },
        ),
    ],
)
def test_native_clients_expose_safe_report_metadata(client, expected):
    assert client.provider_metadata == expected


@pytest.mark.parametrize(
    ("client_kind", "client_type", "base_url", "auth_mode"),
    [
        (
            "anthropic_messages",
            AnthropicMessagesModelClient,
            "https://anthropic.example/v1",
            "x-api-key",
        ),
        (
            "openai_responses",
            OpenAIResponsesModelClient,
            "https://openai.example/v1",
            "bearer",
        ),
        (
            "ollama_chat",
            OllamaChatModelClient,
            "http://127.0.0.1:11434",
            "none",
        ),
    ],
)
def test_builder_constructs_explicit_protocol_clients(
    client_kind,
    client_type,
    base_url,
    auth_mode,
):
    client = build_transport_client(
        client_kind,
        model="test-model",
        base_url=base_url,
        api_key="" if auth_mode == "none" else "test-key",
        timeout=10,
        auth_mode=auth_mode,
        capabilities={},
    )

    assert isinstance(client, client_type)
    assert client.provider_binding["protocol_family"] == client_kind


def test_factory_constructs_openai_chat_and_rejects_unknown_transport():
    from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient

    client = build_transport_client(
        "openai_chat_completions",
        model="test-model",
        base_url="https://chat.example/v1",
        api_key="test-key",
        timeout=10,
        auth_mode="bearer",
    )
    assert isinstance(client, OpenAIChatCompletionsModelClient)

    with pytest.raises(ValueError, match="unsupported transport kind"):
        build_transport_client(
            "deepseek",
            model="test-model",
            base_url="https://example.com/v1",
            api_key="test-key",
            timeout=10,
            auth_mode="bearer",
        )


@pytest.mark.parametrize(
    ("client", "payload", "expected"),
    [
        (
            _openai_client(),
            {
                "model": "gpt-effective",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
            "gpt-effective",
        ),
        (
            _anthropic_client(),
            {
                "model": "claude-effective",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
            },
            "claude-effective",
        ),
        (
            _ollama_client(),
            {
                "model": "qwen-effective",
                "message": {"content": "done"},
                "done": True,
                "done_reason": "stop",
            },
            "qwen-effective",
        ),
    ],
)
def test_native_clients_record_validated_effective_model(
    monkeypatch,
    client,
    payload,
    expected,
):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(json.dumps(payload).encode()),
    )

    _complete(client)

    assert client.provider_metadata["effective_model"] == expected


def test_openai_official_wire_uses_responses_native_history_and_strict_tools(
    monkeypatch,
):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        captured["headers"] = dict(request.header_items())
        return _Response(
            json.dumps(
                {
                    "id": "resp_1",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "done"}],
                        }
                    ],
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                }
            ).encode(),
            headers={"x-request-id": "req_1"},
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    state = [{"type": "reasoning", "encrypted_content": "opaque", "summary": []}]
    messages = [
        {"role": "user", "content": "first"},
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
            "_pony_provider_state": state,
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
        {"role": "user", "content": "next"},
    ]

    response = _complete(_openai_client(), tools=[_tool_schema()], messages=messages)

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["timeout"] == 30
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"] == {
        "model": "gpt-test",
        "instructions": "SYSTEM",
        "input": [
            {"role": "user", "content": "first"},
            state[0],
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"pattern":"x"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "match",
            },
            {"role": "user", "content": "next"},
        ],
        "max_output_tokens": 42,
        "store": False,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "name": "search",
                "description": "Search files",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                    },
                    "required": ["pattern", "path"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ],
        "temperature": 0.0,
        "parallel_tool_calls": False,
        "include": ["reasoning.encrypted_content"],
    }
    assert response.content == [{"type": "text", "text": "done"}]
    assert response.usage["request_id"] == "req_1"


def test_openai_function_call_preserves_reasoning_and_drops_optional_null(
    monkeypatch,
):
    state = {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "opaque",
        "summary": [],
        "content": [],
    }
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            json.dumps(
                {
                    "output": [
                        state,
                        {
                            "type": "function_call",
                            "call_id": "call_2",
                            "name": "search",
                            "arguments": '{"pattern":"x","path":null}',
                        },
                    ],
                    "usage": {},
                }
            ).encode()
        ),
    )

    response = _complete(_openai_client(), tools=[_tool_schema()])

    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content == [
        {
            "type": "tool_use",
            "id": "call_2",
            "name": "search",
            "input": {"pattern": "x"},
        }
    ]
    assert response.provider_state == [state]


def test_openai_unknown_incomplete_reason_does_not_authorize_tool_use(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"status":"incomplete","incomplete_details":{"reason":"new_reason"},"output":[{"type":"function_call","call_id":"call_3","name":"search","arguments":"{\\"pattern\\":\\"x\\"}"}],"usage":{}}'
        ),
    )

    response = _complete(_openai_client(), tools=[_tool_schema()])

    assert response.stop_reason == StopReason.UNKNOWN


def test_openai_custom_uses_exact_root_and_conservative_fields(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["headers"] = dict(request.header_items())
        return _Response(
            b'{"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{}}'
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _openai_client(
        base_url="https://gateway.example/native/v7",
        auth_mode="x-api-key",
        capabilities={},
    )

    _complete(client, tools=[_tool_schema()])

    assert captured["url"] == "https://gateway.example/native/v7/responses"
    assert captured["headers"]["X-api-key"] == "test-key"
    tool = captured["body"]["tools"][0]
    assert "strict" not in tool
    assert tool["parameters"]["required"] == ["pattern"]
    assert "parallel_tool_calls" not in captured["body"]
    assert "include" not in captured["body"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices":[{"message":{"content":"wrong protocol"}}]}',
        b'data: {"type":"response.output_text.delta","delta":"wrong"}',
        b"[]",
        b"\xff",
    ],
)
def test_openai_rejects_non_responses_json_without_fallback(monkeypatch, body):
    urlopen = Mock(return_value=_Response(body))
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_openai_client())

    assert caught.value.code == "provider_protocol_mismatch"
    assert caught.value.stage == "response_decode"
    assert caught.value.protocol_reason == "response_shape_invalid"
    assert urlopen.call_count == 1


def test_openai_rejects_invalid_function_arguments(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"output":[{"type":"function_call","call_id":"c1","name":"search","arguments":"[]"}],"usage":{}}'
        ),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_openai_client(), tools=[_tool_schema()])

    assert caught.value.code == "provider_protocol_mismatch"
    assert caught.value.stage == "tool_call"
    assert caught.value.protocol_reason == "tool_arguments_invalid"


def test_ollama_chat_wire_uses_native_messages_and_tools(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        return _Response(
            b'{"message":{"role":"assistant","content":"done"},"done":true,"done_reason":"stop","prompt_eval_count":12,"eval_count":3}'
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    messages = [
        {"role": "user", "content": "first"},
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
            "_pony_provider_state": [
                {"type": "reasoning", "encrypted_content": "ignored"}
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

    response = _complete(_ollama_client(), tools=[_tool_schema()], messages=messages)

    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["timeout"] == 30
    assert captured["body"] == {
        "model": "qwen-test",
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "",
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
            {"role": "tool", "content": "match", "tool_name": "search"},
        ],
        "stream": False,
        "think": False,
        "options": {
            "num_predict": 42,
            "temperature": 0.0,
            "top_p": 0.9,
        },
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search files",
                    "parameters": _tool_schema()["input_schema"],
                },
            }
        ],
    }
    assert response.content == [{"type": "text", "text": "done"}]
    assert response.usage["total_tokens"] == 15


def test_ollama_generates_an_id_for_a_single_wire_call_without_one(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"message":{"content":"","tool_calls":[{"function":{"name":"search","arguments":{"pattern":"x"}}}]},"done":true,"done_reason":"stop"}'
        ),
    )

    response = _complete(_ollama_client(), tools=[_tool_schema()])

    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content[0]["id"].startswith("toolu_ollama_")


def test_ollama_unknown_done_reason_does_not_authorize_tool_use(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"message":{"content":"","tool_calls":[{"id":"call_4","function":{"name":"search","arguments":{"pattern":"x"}}}]},"done":true,"done_reason":"new_reason"}'
        ),
    )

    response = _complete(_ollama_client(), tools=[_tool_schema()])

    assert response.stop_reason == StopReason.UNKNOWN


def test_ollama_custom_bearer_uses_exact_chat_root(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        return _Response(
            b'{"message":{"content":"ok"},"done":true,"done_reason":"stop"}'
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _ollama_client(
        host="https://ollama.example/native",
        auth_mode="bearer",
        api_key="custom-key",
    )

    _complete(client)

    assert captured["url"] == "https://ollama.example/native/api/chat"
    assert captured["headers"]["Authorization"] == "Bearer custom-key"


def test_ollama_preserves_timeout_layer_and_does_not_try_generate(monkeypatch):
    urlopen = Mock(side_effect=TimeoutError("secret"))
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_ollama_client())

    assert caught.value.code == "timeout"
    assert caught.value.retryable is True
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    ("error_value", "reason"),
    [
        (urllib.error.URLError("secret"), "network_error"),
        (RemoteDisconnected("secret"), "remote_disconnect"),
        (TimeoutError("secret"), "timeout"),
    ],
)
def test_openai_network_error_is_classified_after_one_transport(
    monkeypatch,
    error_value,
    reason,
):
    urlopen = Mock(side_effect=error_value)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_openai_client())

    assert caught.value.code == reason
    assert caught.value.retryable is True
    assert urlopen.call_count == 1


def test_provider_429_retry_after_is_capped(monkeypatch):
    error_value = urllib.error.HTTPError(
        "https://api.openai.com/v1/responses",
        429,
        "rate limited",
        hdrs={"Retry-After": "99"},
        fp=io.BytesIO(b"secret backend response"),
    )
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(side_effect=error_value),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_openai_client())

    assert caught.value.code == "rate_limited"
    assert caught.value.retryable is True
    assert caught.value.retry_after == 10.0


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (408, "request_timeout", True),
        (413, "request_too_large", False),
    ],
)
@pytest.mark.parametrize(
    "client_factory",
    [_anthropic_client, _openai_client, _ollama_client],
)
def test_provider_preserves_specific_http_failure_layer(
    monkeypatch,
    status,
    code,
    retryable,
    client_factory,
):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(
            side_effect=urllib.error.HTTPError(
            "https://gateway.example/v1/resource",
            status,
            "failed",
            hdrs={},
            fp=io.BytesIO(b"opaque backend response"),
            )
        ),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(client_factory())

    assert caught.value.code == code
    assert caught.value.http_status == status
    assert caught.value.retryable is retryable


@pytest.mark.parametrize(
    ("error_value", "code"),
    [
        (ssl.SSLError("opaque"), "tls_error"),
        (ConnectionResetError("opaque"), "connection_reset"),
        (IncompleteRead(b"partial", 10), "response_truncated"),
    ],
)
@pytest.mark.parametrize(
    "client_factory",
    [_anthropic_client, _openai_client, _ollama_client],
)
def test_provider_preserves_specific_transport_failure_layer(
    monkeypatch,
    error_value,
    code,
    client_factory,
):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(side_effect=error_value),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(client_factory())

    assert caught.value.code == code
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    ("family", "client_factory"),
    [
        ("Anthropic", _anthropic_client),
        ("OpenAI", _openai_client),
        ("Ollama", _ollama_client),
    ],
)
def test_provider_http_errors_expose_only_family_and_status(
    monkeypatch,
    caplog,
    family,
    client_factory,
):
    secret = "github_pat_" + "B" * 32
    credential_url = f"https://user:{secret}@example.test/v1?api_key={secret}"
    error_value = urllib.error.HTTPError(
        credential_url,
        401,
        "unauthorized",
        hdrs={},
        fp=io.BytesIO(f'{{"error":"{secret}"}}'.encode()),
    )
    urlopen = Mock(side_effect=error_value)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        _complete(client_factory())

    assert str(caught.value) == f"{family} request failed with HTTP 401"
    assert secret not in str(caught.value) + caplog.text
    assert credential_url not in str(caught.value)
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    "client_factory",
    [_anthropic_client, _openai_client, _ollama_client],
)
def test_provider_tool_request_preserves_http_400_classification(
    monkeypatch,
    client_factory,
):
    error_value = urllib.error.HTTPError(
        "https://gateway.example/v1/resource",
        400,
        "bad request",
        hdrs={},
        fp=io.BytesIO(b'{"error":"invalid thinking state"}'),
    )
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(side_effect=error_value),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(client_factory(), tools=[_tool_schema()])

    assert caught.value.code == "http_4xx"
    assert caught.value.http_status == 400


@pytest.mark.parametrize(
    "url",
    [
        "https://user:opaque-password@example.test/v1",
        "https://example.test/v1?api_key=opaque-value",
        "https://example.test/v1#fragment",
    ],
)
@pytest.mark.parametrize(
    "client_factory",
    [
        lambda url: _openai_client(base_url=url),
        lambda url: AnthropicMessagesModelClient(
            model="test",
            base_url=url,
            api_key="test-key",
            temperature=0.0,
            timeout=1,
        ),
        lambda url: _ollama_client(host=url),
    ],
)
def test_provider_clients_reject_unsafe_base_urls(url, client_factory):
    with pytest.raises(ValueError):
        client_factory(url)


def test_provider_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b"x" * (provider_shared.MAX_PROVIDER_RESPONSE_BYTES + 1)
        ),
    )

    with pytest.raises(ProviderTransportError) as caught:
        _complete(_openai_client())

    assert caught.value.code == "response_too_large"


def test_redirect_handler_never_follows_provider_redirects():
    assert (
        provider_shared._NoRedirectHandler().redirect_request(
        None,
        None,
        302,
        "redirect",
        {},
        "https://elsewhere.test",
        )
        is None
    )
