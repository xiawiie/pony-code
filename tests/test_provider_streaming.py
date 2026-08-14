import io
import inspect
import json

import pytest

import pony.providers.transport as provider_transport
from pony.providers.anthropic_messages import AnthropicMessagesModelClient
from pony.providers.ollama_chat import OllamaChatModelClient
from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient
from pony.providers.openai_responses import OpenAIResponsesModelClient
from pony.providers.response import StopReason
from pony.providers.transport import ProviderTransportError


class _StreamResponse:
    def __init__(self, body=b"", *, headers=None, chunks=None, failure=None):
        self.headers = dict(headers or {})
        self._body = io.BytesIO(body)
        self._chunks = list(chunks) if chunks is not None else None
        self._failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        if self._chunks is None:
            return self._body.read(size)
        if self._chunks:
            return self._chunks.pop(0)
        if self._failure is not None:
            raise self._failure
        return b""

    def readline(self, size=-1):
        if self._chunks is None:
            return self._body.readline(size)
        return self.read(size)


def _sse(*events):
    parts = []
    for event_name, value in events:
        if event_name is not None:
            parts.append(f"event: {event_name}\n")
        payload = value if isinstance(value, str) else json.dumps(value)
        parts.append(f"data: {payload}\n\n")
    return "".join(parts).encode()


def _callbacks():
    calls = []
    return calls, lambda: calls.append("committed"), lambda text: calls.append(text)


def _anthropic_client():
    return AnthropicMessagesModelClient(
        model="claude-test",
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
        capabilities={},
    )


def _responses_client():
    return OpenAIResponsesModelClient(
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
        capabilities={"reasoning_replay": True},
    )


def _chat_client():
    return OpenAIChatCompletionsModelClient(
        model="chat-test",
        base_url="https://gateway.example/v1",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
        capabilities={},
    )


def _ollama_client():
    return OllamaChatModelClient(
        model="qwen-test",
        host="http://127.0.0.1:11434",
        temperature=0.0,
        top_p=0.9,
        timeout=30,
    )


def _complete_stream(client, on_committed, on_text):
    return client.complete_stream(
        system=[{"type": "text", "text": "SYSTEM"}],
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=42,
        on_stream_committed=on_committed,
        on_text_delta=on_text,
    )


@pytest.mark.parametrize(
    "client",
    [_anthropic_client(), _responses_client(), _chat_client(), _ollama_client()],
)
def test_complete_stream_signature_is_keyword_only_and_frozen(client):
    parameters = inspect.signature(client.complete_stream).parameters

    assert list(parameters) == [
        "system",
        "tools",
        "messages",
        "max_tokens",
        "cache_breakpoints",
        "on_stream_committed",
        "on_text_delta",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )
    assert parameters["cache_breakpoints"].default is None
    assert parameters["on_stream_committed"].default is inspect.Parameter.empty
    assert parameters["on_text_delta"].default is inspect.Parameter.empty


def _equivalent_stream_cases():
    anthropic_final = {
        "model": "claude-test",
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    anthropic_stream = _sse(
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "model": "claude-test",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 2},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "done"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    response_item = {
        "id": "msg_1",
        "type": "message",
        "content": [{"type": "output_text", "text": "done"}],
    }
    responses_final = {
        "id": "resp_1",
        "model": "gpt-test",
        "status": "completed",
        "output": [response_item],
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    responses_stream = _sse(
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "done"},
        ),
        (
            "response.completed",
            {"type": "response.completed", "response": responses_final},
        ),
    )
    chat_final = {
        "id": "chat_1",
        "model": "chat-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }
    chat_stream = _sse(
        (
            None,
            {
                "id": "chat_1",
                "model": "chat-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ),
        (
            None,
            {
                "id": "chat_1",
                "model": "chat-test",
                "choices": [],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        ),
        (None, "[DONE]"),
    )
    ollama_final = {
        "model": "qwen-test",
        "message": {"role": "assistant", "content": "done"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 2,
        "eval_count": 1,
    }
    ollama_stream = (
        json.dumps(
            {"model": "qwen-test", "message": {"content": "done"}, "done": False}
        ).encode()
        + b"\n"
        + json.dumps(
            {
                "model": "qwen-test",
                "message": {"content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 2,
                "eval_count": 1,
            }
        ).encode()
        + b"\n"
    )
    return [
        (_anthropic_client(), anthropic_final, anthropic_stream),
        (_responses_client(), responses_final, responses_stream),
        (_chat_client(), chat_final, chat_stream),
        (_ollama_client(), ollama_final, ollama_stream),
    ]


@pytest.mark.parametrize(("client", "final_data", "stream_body"), _equivalent_stream_cases())
def test_complete_stream_returns_the_same_terminal_response(
    monkeypatch, client, final_data, stream_body
):
    responses = iter(
        [
            _StreamResponse(json.dumps(final_data).encode()),
            _StreamResponse(stream_body),
        ]
    )
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: next(responses),
    )

    expected = client.complete(
        system=[{"type": "text", "text": "SYSTEM"}],
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=42,
    )
    calls, committed, text = _callbacks()
    actual = _complete_stream(client, committed, text)

    assert actual == expected
    assert calls[0] == "committed"


def test_anthropic_stream_assembles_text_usage_and_stop(monkeypatch):
    captured = {}
    body = _sse(
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "model": "claude-effective",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 5},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello\n"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return _StreamResponse(body, headers={"request-id": "req_1"})

    monkeypatch.setattr(provider_transport, "_provider_urlopen", urlopen)
    calls, committed, text = _callbacks()
    response = _complete_stream(_anthropic_client(), committed, text)

    assert captured["body"]["stream"] is True
    assert calls == ["committed", "hello\n"]
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [{"type": "text", "text": "hello\n"}]
    assert response.usage["total_tokens"] == 7
    assert response.usage["request_id"] == "req_1"


def test_anthropic_stream_assembles_tool_state_without_preview(monkeypatch):
    body = _sse(
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "thinking",
                    "thinking": "",
                    "signature": "",
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "private"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"a.py"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 3},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(body),
    )
    calls, committed, text = _callbacks()
    response = _complete_stream(_anthropic_client(), committed, text)

    assert calls == ["committed"]
    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_file",
            "input": {"path": "a.py"},
        }
    ]
    assert response.provider_state[0]["signature"] == "opaque"


def test_responses_stream_uses_completed_response_as_authority(monkeypatch):
    item = {
        "id": "msg_1",
        "type": "message",
        "content": [{"type": "output_text", "text": "hello\n"}],
    }
    completed = {
        "id": "resp_1",
        "model": "gpt-effective",
        "status": "completed",
        "output": [item],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    body = _sse(
        ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
        (
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": item},
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "hello\n",
            },
        ),
        (
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ),
        ("response.completed", {"type": "response.completed", "response": completed}),
    )
    captured = {}

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return _StreamResponse(body, headers={"x-request-id": "req_1"})

    monkeypatch.setattr(provider_transport, "_provider_urlopen", urlopen)
    calls, committed, text = _callbacks()
    response = _complete_stream(_responses_client(), committed, text)

    assert captured["body"]["stream"] is True
    assert calls == ["committed", "hello\n"]
    assert response.content == [{"type": "text", "text": "hello\n"}]
    assert response.usage["total_tokens"] == 6
    assert response.usage["request_id"] == "req_1"


def test_responses_stream_never_previews_tool_arguments_or_reasoning(monkeypatch):
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "opaque",
        "summary": [],
    }
    tool = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "search",
        "arguments": '{"pattern":"secret"}',
    }
    body = _sse(
        (
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
        ),
        (
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        ),
        (
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 1, "item": tool},
        ),
        (
            "response.function_call_arguments.delta",
            {"type": "response.function_call_arguments.delta", "delta": "secret"},
        ),
        (
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 1, "item": tool},
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [reasoning, tool], "usage": {}},
            },
        ),
    )
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(body),
    )
    calls, committed, text = _callbacks()
    response = _complete_stream(_responses_client(), committed, text)

    assert calls == ["committed"]
    assert response.content[0]["input"] == {"pattern": "secret"}
    assert response.provider_state == [reasoning]


def test_chat_stream_assembles_tool_usage_and_sends_stream_options(monkeypatch):
    body = _sse(
        (
            None,
            {
                "id": "chat_1",
                "model": "chat-effective",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "hello\n"},
                        "finish_reason": None,
                    }
                ],
            },
        ),
        (
            None,
            {
                "id": "chat_1",
                "model": "chat-effective",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "search", "arguments": '{"pattern":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
        ),
        (
            None,
            {
                "id": "chat_1",
                "model": "chat-effective",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ),
        (None, {"id": "chat_1", "model": "chat-effective", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}),
        (None, "[DONE]"),
    )
    captured = {}

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return _StreamResponse(body)

    monkeypatch.setattr(provider_transport, "_provider_urlopen", urlopen)
    calls, committed, text = _callbacks()
    response = _complete_stream(_chat_client(), committed, text)

    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert calls == ["committed", "hello\n"]
    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content == [
        {"type": "text", "text": "hello\n"},
        {"type": "tool_use", "id": "call_1", "name": "search", "input": {"pattern": "x"}},
    ]
    assert response.usage["total_tokens"] == 7


def test_ollama_stream_assembles_chunks_and_terminal_usage(monkeypatch):
    events = [
        {
            "model": "qwen-effective",
            "message": {"role": "assistant", "content": "hello\n"},
            "done": False,
        },
        {
            "model": "qwen-effective",
            "message": {"role": "assistant", "content": "", "thinking": "private"},
            "done": False,
        },
        {
            "model": "qwen-effective",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "search", "arguments": {"pattern": "x"}},
                    }
                ],
            },
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 2,
        },
    ]
    body = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    captured = {}

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return _StreamResponse(body)

    monkeypatch.setattr(provider_transport, "_provider_urlopen", urlopen)
    calls, committed, text = _callbacks()
    response = _complete_stream(_ollama_client(), committed, text)

    assert captured["body"]["stream"] is True
    assert calls == ["committed", "hello\n"]
    assert response.stop_reason == StopReason.TOOL_USE
    assert response.content[0] == {"type": "text", "text": "hello\n"}
    assert response.content[1]["input"] == {"pattern": "x"}
    assert response.usage["total_tokens"] == 7


def test_stream_callback_failure_does_not_change_final_response(monkeypatch):
    item = {
        "id": "msg_1",
        "type": "message",
        "content": [{"type": "output_text", "text": "done"}],
    }
    body = _sse(
        (
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": item},
        ),
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "done"},
        ),
        (
            "response.completed",
            {"type": "response.completed", "response": {"output": [item], "usage": {}}},
        ),
    )
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(body),
    )
    text_calls = []

    def fail_committed():
        raise RuntimeError("presentation failed")

    response = _complete_stream(_responses_client(), fail_committed, text_calls.append)

    assert response.content == [{"type": "text", "text": "done"}]
    assert text_calls == []


def test_stream_disconnect_after_first_event_is_not_retryable(monkeypatch):
    first_event = _sse(
        ("response.created", {"type": "response.created", "response": {"id": "r1"}})
    )
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(
            chunks=first_event.splitlines(keepends=True),
            failure=TimeoutError("private"),
        ),
    )
    calls, committed, text = _callbacks()

    with pytest.raises(ProviderTransportError) as caught:
        _complete_stream(_responses_client(), committed, text)

    assert calls == ["committed"]
    assert caught.value.code == "timeout"
    assert caught.value.retryable is False


def test_stream_disconnect_before_event_remains_retryable(monkeypatch):
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(
            chunks=[], failure=TimeoutError("private")
        ),
    )
    calls, committed, text = _callbacks()

    with pytest.raises(ProviderTransportError) as caught:
        _complete_stream(_responses_client(), committed, text)

    assert calls == []
    assert caught.value.code == "timeout"
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    ("client", "body"),
    [
        (
            _anthropic_client(),
            _sse(("error", {"type": "error", "error": {"type": "overloaded_error"}})),
        ),
        (
            _responses_client(),
            _sse(("error", {"type": "error", "error": {"code": "server_error"}})),
        ),
        (_chat_client(), _sse((None, {"error": {"code": "server_error"}}))),
        (_ollama_client(), b'{"error":"server error"}\n'),
    ],
)
def test_stream_error_frames_commit_and_fail_without_remote_detail(
    monkeypatch, client, body
):
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(body),
    )
    calls, committed, text = _callbacks()

    with pytest.raises(ProviderTransportError) as caught:
        _complete_stream(client, committed, text)

    assert calls == ["committed"]
    assert caught.value.code == "backend_error"
    assert caught.value.retryable is False
    assert "server_error" not in str(caught.value)


def test_sse_and_ndjson_framing_limits_are_exact(monkeypatch):
    assert provider_transport.MAX_PROVIDER_STREAM_LINE_BYTES == 256 * 1024
    assert provider_transport.MAX_PROVIDER_RESPONSE_BYTES == 16 * 1024 * 1024
    assert provider_transport.MAX_PROVIDER_STREAM_EVENTS == 100_000

    exact_line = b"x" * provider_transport.MAX_PROVIDER_STREAM_LINE_BYTES + b"\n"
    assert list(
        provider_transport._bounded_stream_lines(
            _StreamResponse(exact_line),
            family="Test",
            allow_unterminated_final_line=True,
        )
    ) == ["x" * provider_transport.MAX_PROVIDER_STREAM_LINE_BYTES]

    with pytest.raises(ProviderTransportError) as too_long:
        list(
            provider_transport._bounded_stream_lines(
                _StreamResponse(exact_line[:-1] + b"x\n"),
                family="Test",
                allow_unterminated_final_line=True,
            )
        )
    assert too_long.value.code == "response_too_large"

    monkeypatch.setattr(provider_transport, "MAX_PROVIDER_RESPONSE_BYTES", 6)
    assert list(provider_transport._iter_ndjson_events(_StreamResponse(b"{}\n{}\n"), family="Test")) == [{}, {}]
    with pytest.raises(ProviderTransportError) as body_too_large:
        list(provider_transport._iter_ndjson_events(_StreamResponse(b"{}\n{}\nX"), family="Test"))
    assert body_too_large.value.code == "response_too_large"


def test_stream_framing_rejects_invalid_utf8_missing_terminator_and_event_overflow(monkeypatch):
    with pytest.raises(ProviderTransportError) as invalid_utf8:
        list(provider_transport._iter_ndjson_events(_StreamResponse(b"\xff\n"), family="Test"))
    assert invalid_utf8.value.code == "provider_protocol_mismatch"

    with pytest.raises(ProviderTransportError) as truncated:
        list(provider_transport._iter_sse_events(_StreamResponse(b"data: {}\n"), family="Test"))
    assert truncated.value.code == "response_truncated"

    monkeypatch.setattr(provider_transport, "MAX_PROVIDER_STREAM_EVENTS", 2)
    assert list(provider_transport._iter_ndjson_events(_StreamResponse(b"{}\n{}\n"), family="Test")) == [{}, {}]
    with pytest.raises(ProviderTransportError) as too_many:
        list(provider_transport._iter_ndjson_events(_StreamResponse(b"{}\n{}\n{}\n"), family="Test"))
    assert too_many.value.code == "response_too_large"


@pytest.mark.parametrize(
    ("client", "body"),
    [
        (
            _anthropic_client(),
            _sse(("message_start", {"type": "message_start", "message": {"content": []}})),
        ),
        (
            _responses_client(),
            _sse(("response.created", {"type": "response.created", "response": {}})),
        ),
        (
            _chat_client(),
            _sse((None, {"choices": [{"index": 0, "delta": {}, "finish_reason": None}]})),
        ),
        (
            _ollama_client(),
            json.dumps({"message": {"content": ""}, "done": False}).encode() + b"\n",
        ),
    ],
)
def test_stream_requires_protocol_terminator(monkeypatch, client, body):
    monkeypatch.setattr(
        provider_transport,
        "_provider_urlopen",
        lambda *_args, **_kwargs: _StreamResponse(body),
    )
    calls, committed, text = _callbacks()

    with pytest.raises(ProviderTransportError) as caught:
        _complete_stream(client, committed, text)

    assert calls[0] == "committed"
    assert caught.value.retryable is False
