from unittest.mock import MagicMock
from pico.providers.fallback_adapter import FallbackAdapter
from pico.providers.response import Response, StopReason


class _StubInner:
    def __init__(self, canned):
        self.canned = canned
        self.last_prompt = None
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
        self.last_prompt = prompt
        self.last_completion_metadata = {"input_tokens": 3, "output_tokens": 2}
        return self.canned


def test_fallback_flattens_system_tools_messages():
    inner = _StubInner('<final>done</final>')
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "SYSTEM_CORE"}],
        tools=[{"name": "read_file", "description": "d", "input_schema": {}}],
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
    )
    assert isinstance(resp, Response)
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": "done"}]
    assert "SYSTEM_CORE" in inner.last_prompt
    assert "read_file" in inner.last_prompt
    assert "hi" in inner.last_prompt


def test_fallback_parses_xml_tool_call_to_native_shape():
    inner = _StubInner('<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>')
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "s"}],
        tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10,
    )
    assert resp.stop_reason == StopReason.TOOL_USE
    assert resp.content[0]["type"] == "tool_use"
    assert resp.content[0]["name"] == "read_file"
    assert resp.content[0]["input"] == {"path": "a.py"}
    assert resp.content[0]["id"].startswith("toolu_local_")


def test_fallback_ignores_cache_breakpoints():
    inner = _StubInner('<final>ok</final>')
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10, cache_breakpoints=[0],
    )
    # 不支持 prompt cache 的 provider 应静默忽略 breakpoints
    assert resp.stop_reason == StopReason.END_TURN
