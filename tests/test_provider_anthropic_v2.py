"""Anthropic adapter v2 payload shape + response normalization tests."""
import json
from unittest.mock import patch, MagicMock

from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
from pico.providers.response import Response, StopReason


def _mock_urlopen(response_body):
    m = MagicMock()
    m.__enter__.return_value = MagicMock(read=lambda: json.dumps(response_body).encode("utf-8"))
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


def test_complete_v2_payload_shape_and_cache_control():
    client = _make_client()
    system = [{"type": "text", "text": "SYSTEM_CORE", "cache_control": {"type": "ephemeral"}}]
    tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    messages = [{"role": "user", "content": "hi"}]

    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen({
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5},
        })

    with patch("urllib.request.urlopen", fake_urlopen):
        resp = client.complete_v2(system=system, tools=tools, messages=messages, max_tokens=100)

    assert captured_payload["data"]["system"] == system
    assert captured_payload["data"]["tools"] == tools
    assert captured_payload["data"]["messages"] == messages
    assert isinstance(resp, Response)
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": "ok"}]
    assert resp.usage["cache_creation_input_tokens"] == 5


def test_complete_v2_cache_breakpoint_on_message():
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
    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_v2(system=system, tools=[], messages=messages, max_tokens=10, cache_breakpoints=[1])
    # 断点位置的 message.content 应被转成 content-block list 并带 cache_control
    msg1 = captured["data"]["messages"][1]
    if isinstance(msg1["content"], list):
        assert msg1["content"][-1].get("cache_control") == {"type": "ephemeral"}
    else:
        # 允许把 string 消息扩展为 list of content blocks 来打 cache_control
        assert False, "cache_breakpoint 应把 message content 转为 list 形式"


def test_complete_v2_tool_use_response():
    client = _make_client()
    def fake_urlopen(req, timeout=None):
        return _mock_urlopen({
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}}],
            "stop_reason": "tool_use",
            "usage": {},
        })
    with patch("urllib.request.urlopen", fake_urlopen):
        resp = client.complete_v2(system=[{"type": "text", "text": "s"}], tools=[], messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.content[0]["name"] == "read_file"
    assert resp.content[0]["input"]["path"] == "a.py"
