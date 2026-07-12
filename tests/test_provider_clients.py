import io
import json
from http.client import RemoteDisconnected
import urllib.error
from unittest.mock import Mock

import pytest

import pico.providers._shared as provider_shared
from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
from pico.providers.ollama import OllamaModelClient
from pico.providers.openai_compatible import OpenAICompatibleModelClient
from pico.providers.response import StopReason


class _Response:
    def __init__(self, body, content_type="application/json"):
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def _openai_client(api_key="test-key"):
    return OpenAICompatibleModelClient(
        model="gpt-test",
        base_url="https://api.openai.com",
        api_key=api_key,
        temperature=0.0,
        timeout=30,
    )


def _anthropic_client():
    return AnthropicCompatibleModelClient(
        model="claude-test",
        base_url="https://api.anthropic.com",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
    )


def _ollama_client():
    return OllamaModelClient(
        model="qwen-test",
        host="http://127.0.0.1:11434",
        temperature=0.0,
        top_p=0.9,
        timeout=30,
    )


def test_openai_text_only_payload_has_no_cache_request_fields(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        captured["headers"] = dict(request.header_items())
        return _Response(
            json.dumps(
                {
                    "output_text": "ok",
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                        "input_tokens_details": {"cached_tokens": 7},
                    },
                }
            ).encode()
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _openai_client()

    assert client.complete_text("hello", 42) == "ok"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["timeout"] == 30
    assert captured["body"] == {
        "model": "gpt-test",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ],
            "max_output_tokens": 42,
            "stream": False,
            "store": False,
            "temperature": 0.0,
    }
    assert "prompt_cache_key" not in captured["body"]
    assert "prompt_cache_retention" not in captured["body"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert client.last_completion_metadata == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": None,
        "cached_tokens": 7,
        "cache_hit": True,
    }
    assert not hasattr(client, "complete")
    assert not hasattr(client, "supports_prompt_cache")


def test_openai_non_streaming_call_accepts_sse_response(monkeypatch):
    body = b"\n".join(
        [
            b'data: {"type":"response.output_text.delta","delta":"hel"}',
            b'data: {"type":"response.output_text.delta","delta":"lo"}',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":2,"input_tokens_details":{"cached_tokens":4}}}}',
            b"data: [DONE]",
        ]
    )
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(body, "text/event-stream"),
    )
    client = _openai_client()

    assert client.complete_text("hello", 10) == "hello"
    assert client.last_completion_metadata["cached_tokens"] == 4
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_transport_attempts == 1


def test_openai_sse_done_waits_for_completed_usage(monkeypatch):
    body = b"\n".join(
        [
            b'data: {"type":"response.output_text.done","text":"hello"}',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":2,"input_tokens_details":{"cached_tokens":4}}}}',
            b"data: [DONE]",
        ]
    )
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(body, "text/event-stream"),
    )
    client = _openai_client()

    assert client.complete_text("hello", 10) == "hello"
    assert client.last_completion_metadata["cached_tokens"] == 4
    assert client.last_completion_metadata["cache_hit"] is True


def test_openai_incomplete_response_propagates_max_tokens(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"output_text":"<final>partial","incomplete_details":{"reason":"max_output_tokens"},"usage":{"input_tokens":1,"output_tokens":2}}'
        ),
    )
    client = _openai_client()

    assert client.complete_text("hello", 10) == "<final>partial"
    assert client.last_stop_reason == StopReason.MAX_TOKENS


def test_openai_refusal_is_extracted_without_retry(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"output":[{"content":[{"type":"refusal","refusal":"declined"}]}],"usage":{"input_tokens":1,"output_tokens":0}}'
        ),
    )
    client = _openai_client()

    assert client.complete_text("hello", 10) == "declined"
    assert client.last_stop_reason == StopReason.REFUSAL


def test_openai_aggregates_multiple_output_text_blocks(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"output":[{"content":[{"type":"output_text","text":"one"},{"type":"output_text","text":"two"}]}],"usage":{}}'
        ),
    )

    assert _openai_client().complete_text("hello", 10) == "one\ntwo"


@pytest.mark.parametrize("body", [b"[]", b"\xff"])
def test_openai_invalid_response_is_stable(monkeypatch, body):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(body),
    )

    with pytest.raises(RuntimeError, match="^OpenAI-compatible error: invalid_response$"):
        _openai_client().complete_text("hello", 10)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.URLError("secret"), "network_error"),
        (RemoteDisconnected("secret"), "remote_disconnect"),
        (TimeoutError("secret"), "timeout"),
    ],
)
def test_openai_network_error_is_classified_after_one_transport(monkeypatch, error, reason):
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _openai_client()

    with pytest.raises(
        RuntimeError,
        match=rf"^OpenAI-compatible request failed: {reason}$",
    ):
        client.complete_text("hello", 10)

    assert urlopen.call_count == 1
    assert client.last_transport_attempts == 1


def test_openai_rejects_non_header_api_key_before_request(monkeypatch):
    urlopen = Mock()
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(RuntimeError, match="cannot be sent in HTTP headers"):
        _openai_client("bad\u2603").complete_text("hello", 10)

    urlopen.assert_not_called()


def test_ollama_text_only_payload(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        return _Response(
            b'{"response":"ok","done":true,"done_reason":"stop","prompt_eval_count":12,"eval_count":3}'
        )

    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)
    client = _ollama_client()

    assert client.complete_text("hello", 42) == "ok"
    assert captured == {
        "url": "http://127.0.0.1:11434/api/generate",
        "timeout": 30,
        "body": {
            "model": "qwen-test",
            "prompt": "hello",
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": 42,
                "temperature": 0.0,
                "top_p": 0.9,
            },
        },
    }
    assert not hasattr(client, "complete")
    assert not hasattr(client, "supports_prompt_cache")
    assert client.last_completion_metadata == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cached_tokens": 0,
        "cache_hit": False,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert client.last_transport_attempts == 1
    assert client.last_stop_reason == StopReason.END_TURN


def test_ollama_length_stop_is_propagated(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b'{"response":"<final>partial","done":true,"done_reason":"length"}'
        ),
    )
    client = _ollama_client()

    assert client.complete_text("hello", 10) == "<final>partial"
    assert client.last_stop_reason == StopReason.MAX_TOKENS


def test_ollama_timeout_is_stable(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(side_effect=TimeoutError("secret")),
    )

    with pytest.raises(
        RuntimeError,
        match="^Ollama request failed: timeout$",
    ):
        _ollama_client().complete_text("hello", 10)


def test_provider_connection_reset_is_classified_without_raw_error(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        Mock(side_effect=ConnectionResetError("secret socket detail")),
    )

    with pytest.raises(
        RuntimeError,
        match="^OpenAI-compatible request failed: network_error$",
    ):
        _openai_client().complete_text("hello", 10)


def test_ollama_invalid_response_is_stable(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(b"[]"),
    )

    with pytest.raises(RuntimeError, match="^Ollama error: invalid_response$"):
        _ollama_client().complete_text("hello", 10)


@pytest.mark.parametrize(
    ("family", "client_factory", "invoke"),
    (
        (
            "Anthropic-compatible",
            _anthropic_client,
            lambda client: client.complete(
                system=[], tools=[], messages=[], max_tokens=10
            ),
        ),
        (
            "OpenAI-compatible",
            _openai_client,
            lambda client: client.complete_text("hello", 10),
        ),
        (
            "Ollama",
            _ollama_client,
            lambda client: client.complete_text("hello", 10),
        ),
    ),
)
def test_provider_http_errors_expose_only_family_and_status(
    monkeypatch, caplog, family, client_factory, invoke
):
    secret = "github_pat_" + "B" * 32
    credential_url = f"https://user:{secret}@example.test/v1?api_key={secret}"
    error = urllib.error.HTTPError(
        credential_url,
        401,
        "unauthorized",
        hdrs={},
        fp=io.BytesIO(f'{{"error":"{secret}"}}'.encode()),
    )
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} request failed with HTTP 401"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert credential_url not in str(caught.value)
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    ("family", "client_factory", "invoke", "expected_calls"),
    (
        (
            "Anthropic-compatible",
            _anthropic_client,
            lambda client: client.complete(
                system=[], tools=[], messages=[], max_tokens=10
            ),
            1,
        ),
        (
            "OpenAI-compatible",
            _openai_client,
            lambda client: client.complete_text("hello", 10),
            1,
        ),
        (
            "Ollama",
            _ollama_client,
            lambda client: client.complete_text("hello", 10),
            1,
        ),
    ),
)
def test_provider_http_500_retry_counts_are_preserved(
    monkeypatch, family, client_factory, invoke, expected_calls
):
    error = urllib.error.HTTPError(
        "https://example.test/v1",
        500,
        "server error",
        hdrs={},
        fp=io.BytesIO(b"backend failure"),
    )
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} request failed with HTTP 500"
    assert caught.value.__cause__ is None
    assert urlopen.call_count == expected_calls


@pytest.mark.parametrize(
    ("family", "client_factory", "invoke", "payload"),
    (
        (
            "Anthropic-compatible",
            _anthropic_client,
            lambda client: client.complete(
                system=[], tools=[], messages=[], max_tokens=10
            ),
            lambda secret: {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"cache_read_input_tokens": secret},
            },
        ),
        (
            "OpenAI-compatible",
            _openai_client,
            lambda client: client.complete_text("hello", 10),
            lambda secret: {
                "output_text": "ok",
                "usage": {"input_tokens_details": {"cached_tokens": secret}},
            },
        ),
    ),
)
def test_provider_malformed_usage_is_fixed_and_secret_free(
    monkeypatch, caplog, family, client_factory, invoke, payload
):
    secret = "github_pat_" + "M" * 32
    body = json.dumps(payload(secret)).encode()
    urlopen = Mock(return_value=_Response(body))
    monkeypatch.setattr(provider_shared, "_provider_urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} error: invalid_response"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    "url",
    (
        "https://user:opaque-password@example.test/v1",
        "https://example.test/v1?api_key=opaque-value",
        "https://example.test/v1?token=opaque-value",
    ),
)
@pytest.mark.parametrize(
    "client_factory",
    (
        lambda url: OpenAICompatibleModelClient(
            model="test",
            base_url=url,
            api_key="test-key",
            temperature=0.0,
            timeout=1,
        ),
        lambda url: AnthropicCompatibleModelClient(
            model="test",
            base_url=url,
            api_key="test-key",
            temperature=0.0,
            timeout=1,
        ),
        lambda url: OllamaModelClient(
            model="test",
            host=url,
            temperature=0.0,
            top_p=1.0,
            timeout=1,
        ),
    ),
)
def test_provider_clients_reject_credential_bearing_base_url(url, client_factory):
    with pytest.raises(ValueError, match="provider_base_url_credentials"):
        client_factory(url)


@pytest.mark.parametrize("value", ["file:///tmp/x", "ftp://example.test/v1", "not-a-url"])
def test_provider_clients_reject_invalid_base_urls(value):
    with pytest.raises(ValueError, match="provider_base_url_invalid"):
        _openai_client().__class__(
            model="test",
            base_url=value,
            api_key="test",
            temperature=None,
            timeout=1,
        )


def test_provider_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(
        provider_shared,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _Response(
            b"x" * (provider_shared.MAX_PROVIDER_RESPONSE_BYTES + 1)
        ),
    )
    with pytest.raises(RuntimeError, match="response_too_large"):
        _openai_client().complete_text("hello", 10)


def test_redirect_handler_never_follows_provider_redirects():
    assert provider_shared._NoRedirectHandler().redirect_request(
        None, None, 302, "redirect", {}, "https://elsewhere.test"
    ) is None
