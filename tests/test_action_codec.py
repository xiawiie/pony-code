import pytest

from pico.action_codec import FinalAction, RetryAction, ToolAction, decode_action
from pico.providers.response import Response, StopReason


def response(*content, stop=StopReason.END_TURN):
    return Response(stop_reason=stop, content=list(content), usage={})


def text(value):
    return {"type": "text", "text": value}


def tool(name="read_file", arguments=None, tool_use_id="toolu_1"):
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": {"path": "README.md"} if arguments is None else arguments,
    }


def test_multiple_native_tools_retry_without_selecting_one():
    action = decode_action(
        response(
            text("<final>do not finish</final>"),
            tool(),
            tool(name="search", arguments={"pattern": "x"}, tool_use_id="toolu_2"),
            stop=StopReason.TOOL_USE,
        )
    )
    assert isinstance(action, RetryAction)
    assert action.reason_code == "multiple_actions_not_supported"
    assert action.origin == "response"


@pytest.mark.parametrize(
    "stop_reason",
    [
        StopReason.END_TURN,
        StopReason.MAX_TOKENS,
        StopReason.STOP_SEQUENCE,
        StopReason.REFUSAL,
        StopReason.UNKNOWN,
    ],
)
def test_tool_block_requires_tool_use_stop_reason(stop_reason):
    action = decode_action(response(tool(), stop=stop_reason))

    assert isinstance(action, RetryAction)
    assert action.reason_code == "provider_protocol_mismatch"


@pytest.mark.parametrize(
    "bad_block",
    [
        tool(name=""),
        tool(arguments=["README.md"]),
        {"type": "tool_use", "id": "x", "input": {}},
    ],
)
def test_invalid_native_tool_retries(bad_block):
    action = decode_action(response(bad_block, stop=StopReason.TOOL_USE))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "invalid_native_tool"
    assert action.origin == "response"


@pytest.mark.parametrize(
    "raw",
    [
        '{"name":"read_file","arguments":{"path":"README.md"}}',
        '<tool name="write_file" path="a.py"><content>x</content></tool>',
        'Example: <tool>{"name":"read_file","args":{}}</tool>',
        '\x60\x60\x60xml\n<tool>{"name":"read_file","args":{}}</tool>\n\x60\x60\x60',
        '> <tool>{"name":"read_file","args":{}}</tool>',
        '"<tool>{\\"name\\":\\"read_file\\",\\"args\\":{}}</tool>"',
        "<toolbox>not a call</toolbox>",
    ],
)
def test_text_tool_like_content_is_never_executed(raw):
    assert decode_action(response(text(raw))) == FinalAction(
        text=raw,
        origin="provider_text",
    )


def test_all_nonempty_text_blocks_are_joined_in_order():
    assert decode_action(response(text("alpha"), text(""), text("beta"))) == FinalAction(
        text="alpha\nbeta",
        origin="provider_text",
    )


def test_empty_response_is_a_bounded_retry():
    action = decode_action(response())
    assert isinstance(action, RetryAction)
    assert action.reason_code == "empty_response"


def test_stop_sequence_with_text_is_not_a_final_answer():
    action = decode_action(response(text("not complete"), stop=StopReason.STOP_SEQUENCE))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "stop_sequence"


def test_unknown_stop_reason_is_not_treated_as_end_turn():
    action = decode_action(response(text("ambiguous"), stop=StopReason.UNKNOWN))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "provider_protocol_mismatch"


def test_max_tokens_plain_text_is_marked_truncated():
    assert decode_action(
        response(text("partial but useful"), stop=StopReason.MAX_TOKENS)
    ) == FinalAction(
        text="partial but useful",
        origin="provider_text",
        truncated=True,
    )


def test_refusal_is_terminal_instead_of_retrying_same_model():
    assert decode_action(
        response(text("Request declined"), stop=StopReason.REFUSAL)
    ) == FinalAction(text="Request declined", origin="provider_text")


def test_empty_refusal_uses_safe_terminal_text():
    assert decode_action(response(stop=StopReason.REFUSAL)) == FinalAction(
        text="The model declined this request.",
        origin="provider_text",
    )


def test_unsupported_content_shape_is_a_total_retry():
    action = decode_action(response({"type": "image", "source": "x"}))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "provider_protocol_mismatch"


def test_unsupported_content_is_not_ignored_beside_a_valid_tool():
    action = decode_action(
        response(tool(), {"type": "tool_result", "tool_use_id": "toolu_1"})
    )

    assert isinstance(action, RetryAction)
    assert action.reason_code == "provider_protocol_mismatch"


def test_non_iterable_content_is_a_total_retry():
    action = decode_action(
        Response(stop_reason=StopReason.END_TURN, content=42, usage={})
    )

    assert isinstance(action, RetryAction)
    assert action.reason_code == "provider_protocol_mismatch"
    assert action.origin == "response"


def test_one_native_tool_preserves_validated_provider_state():
    state = [{"type": "reasoning", "encrypted_content": "opaque"}]
    action = decode_action(
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[tool()],
            usage={},
            provider_state=state,
        )
    )

    assert action == ToolAction(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_1",
        origin="native_tool_use",
        provider_state=tuple(state),
    )
