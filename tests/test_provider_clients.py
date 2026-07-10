import io
import json
from http.client import RemoteDisconnected
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import pytest

from pico.providers.clients import (
    AnthropicCompatibleModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
    _extract_usage_cache_details,
)


class _RawResponse:
    def __init__(self, body, content_type="application/json"):
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body

    def __iter__(self):
        return iter(self.body.splitlines(keepends=True))


def _anthropic_test_client():
    return AnthropicCompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="safe-test-key",
        temperature=0.0,
        timeout=1,
    )


def _openai_test_client():
    return OpenAICompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="safe-test-key",
        temperature=0.0,
        timeout=1,
    )


def _ollama_test_client():
    return OllamaModelClient(
        model="test",
        host="https://example.test",
        temperature=0.0,
        top_p=1.0,
        timeout=1,
    )


_JSON_RESPONSE_CASES = (
    pytest.param(
        "Anthropic-compatible",
        _anthropic_test_client,
        lambda client: client.complete("hello", 10),
        id="anthropic-legacy",
    ),
    pytest.param(
        "Anthropic-compatible",
        _anthropic_test_client,
        lambda client: client.complete_v2(
            system=[], tools=[], messages=[], max_tokens=10
        ),
        id="anthropic-v2",
    ),
    pytest.param(
        "OpenAI-compatible",
        _openai_test_client,
        lambda client: client.complete("hello", 10),
        id="openai-json",
    ),
    pytest.param(
        "OpenAI-compatible",
        _openai_test_client,
        lambda client: list(client.stream_complete("hello", 10)),
        id="openai-stream-json",
    ),
    pytest.param(
        "Ollama",
        _ollama_test_client,
        lambda client: client.complete("hello", 10),
        id="ollama",
    ),
)


@pytest.mark.parametrize(("family", "client_factory", "invoke"), _JSON_RESPONSE_CASES)
def test_provider_malformed_top_level_is_fixed_invalid_response(
    monkeypatch,
    caplog,
    family,
    client_factory,
    invoke,
):
    secret = "github_pat_" + "J" * 32
    urlopen = Mock(
        return_value=_RawResponse(json.dumps([secret]).encode("utf-8"))
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} error: invalid_response"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 1


@pytest.mark.parametrize(("family", "client_factory", "invoke"), _JSON_RESPONSE_CASES)
def test_provider_invalid_utf8_is_fixed_invalid_response(
    monkeypatch,
    caplog,
    family,
    client_factory,
    invoke,
):
    secret = "github_pat_" + "U" * 32
    urlopen = Mock(return_value=_RawResponse(b"\xff" + secret.encode("utf-8")))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} error: invalid_response"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    "invoke",
    (
        pytest.param(
            lambda client: client.complete("hello", 10),
            id="openai-sse",
        ),
        pytest.param(
            lambda client: list(client.stream_complete("hello", 10)),
            id="openai-stream-sse",
        ),
    ),
)
@pytest.mark.parametrize(
    "body_factory",
    (
        pytest.param(
            lambda secret: b"data: "
            + json.dumps([secret]).encode("utf-8")
            + b"\n\n",
            id="top-level-list",
        ),
        pytest.param(
            lambda secret: b"data: \xff" + secret.encode("utf-8") + b"\n\n",
            id="invalid-utf8",
        ),
    ),
)
def test_openai_malformed_sse_is_fixed_invalid_response(
    monkeypatch,
    caplog,
    invoke,
    body_factory,
):
    secret = "github_pat_" + "S" * 32
    urlopen = Mock(
        return_value=_RawResponse(body_factory(secret), "text/event-stream")
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(_openai_test_client())

    assert str(caught.value) == "OpenAI-compatible error: invalid_response"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    "error_factory",
    (
        pytest.param(urllib.error.URLError, id="url-error"),
        pytest.param(RemoteDisconnected, id="remote-disconnected"),
    ),
)
def test_openai_stream_iterator_network_error_retries_and_is_stable(
    monkeypatch,
    caplog,
    error_factory,
):
    secret = "github_pat_" + "N" * 32

    class FailingStreamResponse(_RawResponse):
        def __iter__(self):
            raise error_factory(secret)

    urlopen = Mock(
        return_value=FailingStreamResponse(b"", "text/event-stream")
    )
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        "pico.providers.openai_compatible.time.sleep", lambda _: None
    )

    with pytest.raises(RuntimeError) as caught:
        list(_openai_test_client().stream_complete("hello", 10))

    assert str(caught.value) == "OpenAI-compatible request failed: network_error"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 3


@pytest.mark.parametrize(
    ("family", "client_factory", "invoke", "payload", "content_type"),
    (
        pytest.param(
            "Anthropic-compatible",
            _anthropic_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"cache_read_input_tokens": secret},
            },
            "application/json",
            id="anthropic-legacy",
        ),
        pytest.param(
            "Anthropic-compatible",
            _anthropic_test_client,
            lambda client: client.complete_v2(
                system=[], tools=[], messages=[], max_tokens=10
            ),
            lambda secret: {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"cache_read_input_tokens": secret},
            },
            "application/json",
            id="anthropic-v2",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "output_text": "ok",
                "usage": {"input_tokens_details": {"cached_tokens": secret}},
            },
            "application/json",
            id="openai-json",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: list(client.stream_complete("hello", 10)),
            lambda secret: {
                "output_text": "ok",
                "usage": {"input_tokens_details": {"cached_tokens": secret}},
            },
            "application/json",
            id="openai-stream-json",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "type": "response.completed",
                "response": {
                    "output_text": "ok",
                    "usage": {
                        "input_tokens_details": {"cached_tokens": secret}
                    },
                },
            },
            "text/event-stream",
            id="openai-sse",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: list(client.stream_complete("hello", 10)),
            lambda secret: {
                "type": "response.completed",
                "response": {
                    "output_text": "ok",
                    "usage": {
                        "input_tokens_details": {"cached_tokens": secret}
                    },
                },
            },
            "text/event-stream",
            id="openai-stream-sse",
        ),
    ),
)
def test_provider_secret_usage_is_fixed_invalid_response(
    monkeypatch,
    caplog,
    family,
    client_factory,
    invoke,
    payload,
    content_type,
):
    secret = "github_pat_" + "G" * 32
    encoded = json.dumps(payload(secret)).encode("utf-8")
    body = b"data: " + encoded + b"\n\n" if content_type == "text/event-stream" else encoded
    urlopen = Mock(return_value=_RawResponse(body, content_type))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} error: invalid_response"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    ("family", "client_factory", "invoke", "payload"),
    (
        pytest.param(
            "Anthropic-compatible",
            _anthropic_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": secret},
            },
            id="anthropic-numeric-field",
        ),
        pytest.param(
            "Anthropic-compatible",
            _anthropic_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "content": [{"type": "text", "text": "ok"}],
                "usage": secret,
            },
            id="anthropic-usage-shape",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "output_text": "ok",
                "usage": {"input_tokens": secret},
            },
            id="openai-numeric-field",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: client.complete("hello", 10),
            lambda secret: {
                "output_text": "ok",
                "usage": {"input_tokens_details": secret},
            },
            id="openai-details-shape",
        ),
    ),
)
def test_provider_malformed_usage_shape_is_fixed_invalid_response(
    monkeypatch,
    caplog,
    family,
    client_factory,
    invoke,
    payload,
):
    secret = "github_pat_" + "M" * 32
    body = json.dumps(payload(secret)).encode("utf-8")
    urlopen = Mock(return_value=_RawResponse(body))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} error: invalid_response"
    assert caught.value.__cause__ is None
    assert secret not in str(caught.value) + caplog.text
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    ("family", "client", "invoke"),
    (
        (
            "OpenAI-compatible",
            OpenAICompatibleModelClient(
                model="test",
                base_url="https://example.test/v1",
                api_key="safe-test-key",
                temperature=0.0,
                timeout=1,
            ),
            lambda client: client.complete("hello", 10),
        ),
        (
            "Anthropic-compatible",
            AnthropicCompatibleModelClient(
                model="test",
                base_url="https://example.test/v1",
                api_key="safe-test-key",
                temperature=0.0,
                timeout=1,
            ),
            lambda client: client.complete("hello", 10),
        ),
        (
            "Ollama",
            OllamaModelClient(
                model="test",
                host="https://example.test",
                temperature=0.0,
                top_p=1.0,
                timeout=1,
            ),
            lambda client: client.complete("hello", 10),
        ),
    ),
)
def test_provider_http_errors_expose_only_family_and_status(
    monkeypatch,
    family,
    client,
    invoke,
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
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError) as caught:
        invoke(client)

    assert str(caught.value) == f"{family} request failed with HTTP 401"
    assert secret not in str(caught.value)
    assert credential_url not in str(caught.value)
    assert urlopen.call_count == 1


@pytest.mark.parametrize(
    ("family", "client_factory", "invoke", "expected_calls"),
    (
        pytest.param(
            "Anthropic-compatible",
            _anthropic_test_client,
            lambda client: client.complete("hello", 10),
            3,
            id="anthropic-legacy",
        ),
        pytest.param(
            "Anthropic-compatible",
            _anthropic_test_client,
            lambda client: client.complete_v2(
                system=[], tools=[], messages=[], max_tokens=10
            ),
            1,
            id="anthropic-v2",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: client.complete("hello", 10),
            3,
            id="openai-json",
        ),
        pytest.param(
            "OpenAI-compatible",
            _openai_test_client,
            lambda client: list(client.stream_complete("hello", 10)),
            3,
            id="openai-stream",
        ),
        pytest.param(
            "Ollama",
            _ollama_test_client,
            lambda client: client.complete("hello", 10),
            1,
            id="ollama",
        ),
    ),
)
def test_provider_http_500_retry_counts_are_preserved(
    monkeypatch,
    family,
    client_factory,
    invoke,
    expected_calls,
):
    error = urllib.error.HTTPError(
        "https://example.test/v1",
        500,
        "server error",
        hdrs={},
        fp=io.BytesIO(b"backend failure"),
    )
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        "pico.providers.anthropic_compatible.time.sleep", lambda _: None
    )
    monkeypatch.setattr(
        "pico.providers.openai_compatible.time.sleep", lambda _: None
    )

    with pytest.raises(RuntimeError) as caught:
        invoke(client_factory())

    assert str(caught.value) == f"{family} request failed with HTTP 500"
    assert caught.value.__cause__ is None
    assert urlopen.call_count == expected_calls


def test_anthropic_v2_http_error_is_stable(monkeypatch):
    secret = "github_pat_" + "V" * 32
    credential_url = f"https://user:{secret}@example.test/v1?api_key={secret}"
    error = urllib.error.HTTPError(
        credential_url,
        429,
        "limited",
        hdrs={},
        fp=io.BytesIO(secret.encode()),
    )
    urlopen = Mock(side_effect=error)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    client = AnthropicCompatibleModelClient(
        model="test",
        base_url="https://example.test/v1",
        api_key="safe-test-key",
        temperature=0.0,
        timeout=1,
    )

    with pytest.raises(RuntimeError) as caught:
        client.complete_v2(
            system=[],
            tools=[],
            messages=[],
            max_tokens=10,
        )

    assert str(caught.value) == (
        "Anthropic-compatible request failed with HTTP 429"
    )
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
            api_key="safe-test-key",
            temperature=0.0,
            timeout=1,
        ),
        lambda url: AnthropicCompatibleModelClient(
            model="test",
            base_url=url,
            api_key="safe-test-key",
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
def test_credential_bearing_base_url_is_rejected_at_client_boundary(url, client_factory):
    with pytest.raises(ValueError, match="provider_base_url_credentials"):
        client_factory(url)


# =============================================================================
# Provider client tests
# =============================================================================


def test_ollama_client_posts_expected_payload():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OllamaModelClient(
        model="qwen3.5:4b",
        host="http://127.0.0.1:11434",
        temperature=0.2,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 30
    assert captured["body"]["model"] == "qwen3.5:4b"
    assert captured["body"]["prompt"] == "hello"
    assert captured["body"]["stream"] is False


def test_openai_compatible_client_posts_expected_responses_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://right.codes/v1/responses"
    assert captured["timeout"] == 30
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-agent"] == "pico/0.1"
    assert captured["body"] == {
        "model": "right.codes/codex-mini",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_output_tokens": 42,
        "stream": False,
        "temperature": 0.2,
    }


def test_openai_compatible_client_reports_non_header_api_key_characters():
    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test新",
        temperature=0.2,
        timeout=30,
    )

    with pytest.raises(RuntimeError, match="Check .env for stray inline comments"):
        client.complete("hello", 42)


def test_openai_compatible_client_sends_prompt_cache_fields_and_records_usage():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output_text": "<final>ok</final>",
                    "usage": {
                        "input_tokens": 2048,
                        "input_tokens_details": {"cached_tokens": 1536},
                        "output_tokens": 32,
                        "total_tokens": 2080,
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete(
            "hello",
            42,
            prompt_cache_key="prefix-hash-123",
            prompt_cache_retention="in_memory",
        )

    assert result == "<final>ok</final>"
    assert captured["body"]["prompt_cache_key"] == "prefix-hash-123"
    assert captured["body"]["prompt_cache_retention"] == "in_memory"
    assert client.last_completion_metadata["prompt_cache_supported"] is True
    assert client.last_completion_metadata["cached_tokens"] == 1536
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["input_tokens"] == 2048


def test_openai_usage_falls_back_from_empty_response_details():
    details = _extract_usage_cache_details(
        {
            "usage": {
                "input_tokens_details": {},
                "prompt_tokens_details": {"cached_tokens": 9},
            }
        }
    )

    assert details["cached_tokens"] == 9


def test_openai_compatible_client_extracts_text_from_event_stream():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                'data: {"type":"response.created","response":{"id":"resp_1","output":[]}}\n'
                'data: {"type":"response.completed","response":{"output":[{"content":[{"text":"<final>stream ok</final>"}]}]}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>stream ok</final>"


def test_openai_compatible_client_extracts_text_from_event_stream_deltas():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"<final>"}\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"OK"}\n'
                'event: response.output_text.done\n'
                'data: {"type":"response.output_text.done","text":"<final>OK</final>"}\n'
                "data: [DONE]\n"
            ).encode("utf-8")

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>OK</final>"


def test_openai_compatible_client_streams_event_deltas_and_records_usage():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            return iter(
                [
                    b'event: response.output_text.delta\n',
                    b'data: {"type":"response.output_text.delta","delta":"<final>"}\n',
                    b'\n',
                    b'event: response.output_text.delta\n',
                    b'data: {"type":"response.output_text.delta","delta":"OK</final>"}\n',
                    b'\n',
                    (
                        b'data: {"type":"response.completed","response":{"output":[],"usage":'
                        b'{"input_tokens":10,"input_tokens_details":{"cached_tokens":4},'
                        b'"output_tokens":2,"total_tokens":12}}}\n'
                    ),
                    b'\n',
                    b"data: [DONE]\n",
                ]
            )

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        chunks = list(
            client.stream_complete(
                "hello",
                42,
                prompt_cache_key="prefix-hash-123",
                prompt_cache_retention="in_memory",
            )
        )

    assert chunks == ["<final>", "OK</final>"]
    assert captured["timeout"] == 30
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["body"]["stream"] is True
    assert captured["body"]["prompt_cache_key"] == "prefix-hash-123"
    assert client.last_completion_metadata["cached_tokens"] == 4
    assert client.last_completion_metadata["cache_hit"] is True


def test_anthropic_compatible_client_posts_expected_messages_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "<final>ok</final>",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://www.right.codes/claude-aws/v1/messages"
    assert captured["timeout"] == 30
    assert captured["headers"]["X-api-key"] == "sk-test"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "claude-sonnet-4-5-20250929",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_tokens": 42,
        "stream": False,
        "temperature": 0.2,
    }


def test_anthropic_compatible_client_reports_non_header_api_key_characters():
    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test新",
        temperature=0.2,
        timeout=30,
    )

    with pytest.raises(RuntimeError, match="Check .env for stray inline comments"):
        client.complete("hello", 42)


def test_anthropic_compatible_client_sends_prompt_cache_control_and_records_usage():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "<final>cached</final>",
                        }
                    ],
                    "usage": {
                        "input_tokens": 2048,
                        "cache_creation_input_tokens": 1024,
                        "cache_read_input_tokens": 512,
                        "output_tokens": 32,
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete(
            "hello",
            42,
            prompt_cache_key="prefix-hash-123",
            prompt_cache_retention="in_memory",
        )

    assert result == "<final>cached</final>"
    assert captured["body"]["cache_control"] == {"type": "ephemeral"}
    assert client.supports_prompt_cache is True
    assert client.last_completion_metadata["prompt_cache_supported"] is True
    assert client.last_completion_metadata["prompt_cache_key"] == "prefix-hash-123"
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["cached_tokens"] == 512
    assert client.last_completion_metadata["cache_creation_input_tokens"] == 1024


def test_anthropic_compatible_client_does_not_enable_cache_for_deepseek_base_url():
    client = AnthropicCompatibleModelClient(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/anthropic",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    assert client.supports_prompt_cache is False


def test_anthropic_compatible_client_extracts_first_text_block():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "<final>ok</final>"},
                    ]
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"


def test_anthropic_compatible_client_extracts_text_block_without_type():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {"thinking": "hidden"},
                        {"text": "<final>ok</final>"},
                    ]
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="glm-5.2",
        base_url="https://lumina.tripo3d.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"


def test_anthropic_compatible_client_explains_thinking_only_token_exhaustion():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {"type": "thinking", "thinking": "analysis consumed the output budget"},
                    ],
                    "stop_reason": "max_tokens",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 42,
                    },
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="glm-5.2",
        base_url="https://lumina.tripo3d.com/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        with pytest.raises(RuntimeError, match="thinking.*max_tokens.*max_new_tokens"):
            client.complete("hello", 42)


def test_anthropic_stream_complete_falls_back_to_complete():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "<final>fallback</final>",
                        }
                    ]
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        chunks = list(client.stream_complete("hello", 42))

    assert chunks == ["<final>fallback</final>"]
