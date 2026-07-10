from pico.providers.response import Response, StopReason


def test_stop_reason_enum_values():
    assert StopReason.END_TURN == "end_turn"
    assert StopReason.TOOL_USE == "tool_use"
    assert StopReason.MAX_TOKENS == "max_tokens"
    assert StopReason.STOP_SEQUENCE == "stop_sequence"
    assert StopReason.UNKNOWN == "unknown"


def test_response_shape_text_only():
    r = Response(
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": "hello"}],
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    assert r.stop_reason == StopReason.END_TURN
    assert r.content[0]["text"] == "hello"


def test_response_shape_tool_use():
    r = Response(
        stop_reason=StopReason.TOOL_USE,
        content=[{"type": "tool_use", "id": "toolu_x", "name": "read_file", "input": {"path": "a.py"}}],
        usage={},
    )
    assert r.content[0]["name"] == "read_file"
    assert r.content[0]["input"]["path"] == "a.py"
