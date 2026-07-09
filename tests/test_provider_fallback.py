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
    assert resp.content == [{"type": "text", "text": "<final>done</final>"}]
    assert "SYSTEM_CORE" in inner.last_prompt
    assert "read_file" in inner.last_prompt
    assert "hi" in inner.last_prompt


def test_fallback_preserves_xml_tool_call_as_raw_text():
    raw = '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>'
    inner = _StubInner(raw)
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "s"}],
        tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10,
    )
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": raw}]


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


def test_fallback_malformed_output_preserves_raw_text():
    raw = "<tool>{not valid json</tool>"
    inner = _StubInner(raw)
    adapter = FallbackAdapter(inner)
    resp = adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10,
    )
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.content == [{"type": "text", "text": raw}]


def test_fallback_flatten_messages_handles_structured_content_blocks():
    inner = _StubInner("<final>ok</final>")
    adapter = FallbackAdapter(inner)
    adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[
            {"role": "user", "content": "plain string"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_x",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_x",
                        "content": "file body",
                    }
                ],
            },
        ],
        max_tokens=10,
    )
    prompt = inner.last_prompt
    assert "plain string" in prompt
    assert "hi" in prompt
    assert "read_file" in prompt
    assert '"path"' in prompt
    assert '"a.py"' in prompt
    assert "file body" in prompt
    assert "toolu_x" in prompt


def test_fallback_does_not_forward_prompt_cache_kwargs():
    inner = MagicMock()
    inner.last_completion_metadata = {}
    inner.complete.return_value = "<final>ok</final>"
    adapter = FallbackAdapter(inner)
    adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "x"}],
        max_tokens=42, cache_breakpoints=[0],  # cache_breakpoints must also be dropped
    )
    # Verify inner.complete was called with positional (prompt, max_tokens) only,
    # no prompt_cache_key / prompt_cache_retention kwargs.
    inner.complete.assert_called_once()
    call_args, call_kwargs = inner.complete.call_args
    assert len(call_args) == 2  # prompt, max_tokens
    assert call_args[1] == 42
    assert "prompt_cache_key" not in call_kwargs
    assert "prompt_cache_retention" not in call_kwargs
    assert "cache_breakpoints" not in call_kwargs


def test_fallback_last_completion_metadata_mirrors_inner():
    class _VaryingStubInner:
        def __init__(self):
            self.n = 0
            self.last_completion_metadata = {}
        def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None):
            self.n += 1
            self.last_completion_metadata = {"input_tokens": self.n, "output_tokens": self.n * 2}
            return "<final>ok</final>"

    inner = _VaryingStubInner()
    adapter = FallbackAdapter(inner)
    adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "x"}], max_tokens=10,
    )
    assert adapter.last_completion_metadata == {"input_tokens": 1, "output_tokens": 2}
    adapter.complete_v2(
        system=[{"type": "text", "text": "s"}], tools=[],
        messages=[{"role": "user", "content": "y"}], max_tokens=10,
    )
    # Mirror the LATEST inner metadata, not the accumulated union.
    assert adapter.last_completion_metadata == {"input_tokens": 2, "output_tokens": 4}
