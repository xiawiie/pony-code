import json
from unittest.mock import patch

from pico.providers.openai_chat import OpenAIChatAdapter, _extract_chat_text
from pico.providers.response import StopReason


def test_extract_chat_text_handles_string_content():
    data = {"choices": [{"message": {"content": "plain text"}}]}

    assert _extract_chat_text(data) == "plain text"


def test_extract_chat_text_handles_list_content():
    data = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}},
                        {"type": "tool_result", "content": "tool output"},
                    ]
                }
            }
        ]
    }

    assert _extract_chat_text(data) == 'hello\nread_file({"path": "a.py"})\ntool output'


def test_openai_chat_complete_v2_posts_chat_completion_and_returns_response():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [{"message": {"content": "raw answer"}}],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                        "prompt_tokens_details": {"cached_tokens": 1},
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAIChatAdapter(
        model="gpt-test",
        base_url="https://api.example.test/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        response = client.complete_v2(
            system=[{"type": "text", "text": "system text"}],
            tools=[{"name": "read_file"}],
            messages=[{"role": "user", "content": "hello", "_pico_meta": {"trace": "x"}}],
            max_tokens=42,
            cache_breakpoints=[0],
        )

    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["timeout"] == 30
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["max_tokens"] == 42
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "hello"},
    ]
    assert "tools" not in captured["body"]
    assert "_pico_meta" not in json.dumps(captured["body"])
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [{"type": "text", "text": "raw answer"}]
    assert response.usage == {
        "input_tokens": 5,
        "output_tokens": 2,
        "total_tokens": 7,
        "cached_tokens": 1,
        "cache_hit": True,
    }
    assert client.last_completion_metadata == response.usage


def test_openai_chat_complete_v2_uses_full_chat_completions_endpoint():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    client = OpenAIChatAdapter(
        model="gpt-test",
        base_url="https://api.openai.com/v1/chat/completions",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_v2(system=[], tools=[], messages=[{"role": "user", "content": "hello"}], max_tokens=10)

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"


def test_openai_chat_complete_v2_appends_chat_completions_to_provider_base():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    client = OpenAIChatAdapter(
        model="glm-test",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_v2(system=[], tools=[], messages=[{"role": "user", "content": "hello"}], max_tokens=10)

    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
