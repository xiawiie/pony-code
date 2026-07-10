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


def test_native_tool_has_highest_priority_and_counts_ignored_calls():
    action = decode_action(
        response(
            text("<final>do not finish</final>"),
            tool(),
            tool(name="search", arguments={"pattern": "x"}, tool_use_id="toolu_2"),
            stop=StopReason.STOP_SEQUENCE,
        )
    )
    assert action == ToolAction(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_1",
        origin="native_tool_use",
        ignored_tool_count=1,
    )


@pytest.mark.parametrize(
    "bad_block",
    [
        tool(name=""),
        tool(arguments=["README.md"]),
        {"type": "tool_use", "id": "x", "input": {}},
    ],
)
def test_first_invalid_native_tool_retries_without_skipping_second(bad_block):
    action = decode_action(response(bad_block, tool(name="search", arguments={"pattern": "x"})))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "invalid_native_tool"
    assert action.origin == "response"


@pytest.mark.parametrize(
    ("raw", "name", "arguments"),
    [
        ('<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>', "read_file", {"path": "README.md"}),
        ('<tool>{"name":"search","arguments":{"pattern":"x"}}</tool>', "search", {"pattern": "x"}),
        ('<tool name="write_file" path="a.py"><content>print("ok")</content></tool>', "write_file", {"path": "a.py", "content": 'print("ok")'}),
        ('<tool name="list_files" path="." />', "list_files", {"path": "."}),
    ],
)
def test_leading_text_tool_protocol(raw, name, arguments):
    action = decode_action(response(text("  " + raw)))
    assert action == ToolAction(
        name=name,
        arguments=arguments,
        tool_use_id=None,
        origin="text_protocol",
    )


@pytest.mark.parametrize(
    "raw",
    [
        'Example: <tool>{"name":"read_file","args":{}}</tool>',
        '\x60\x60\x60xml\n<tool>{"name":"read_file","args":{}}</tool>\n\x60\x60\x60',
        '> <tool>{"name":"read_file","args":{}}</tool>',
        '"<tool>{\\"name\\":\\"read_file\\",\\"args\\":{}}</tool>"',
        "<toolbox>not a call</toolbox>",
    ],
)
def test_nonleading_or_similar_tool_tags_are_provider_text(raw):
    assert decode_action(response(text(raw))) == FinalAction(
        text=raw,
        origin="provider_text",
    )


def test_all_nonempty_text_blocks_are_joined_in_order():
    assert decode_action(response(text("alpha"), text(""), text("beta"))) == FinalAction(
        text="alpha\nbeta",
        origin="provider_text",
    )


def test_leading_final_protocol_unwraps_nonempty_body():
    assert decode_action(response(text(" <final>Done.</final>"))) == FinalAction(
        text="Done.",
        origin="text_protocol",
    )


def test_max_tokens_preserves_incomplete_final_body_as_truncated():
    assert decode_action(
        response(text("<final>Partial answer"), stop=StopReason.MAX_TOKENS)
    ) == FinalAction(
        text="Partial answer",
        origin="text_protocol",
        truncated=True,
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("<tool>{bad json}</tool>", "malformed_tool_protocol"),
        ("<final></final>", "empty_final_protocol"),
        ("", "empty_response"),
    ],
)
def test_protocol_and_empty_failures_are_bounded_retries(raw, reason):
    action = decode_action(response(text(raw)) if raw else response())
    assert isinstance(action, RetryAction)
    assert action.reason_code == reason
    assert len(action.excerpt) <= 160
    if raw:
        assert raw not in action.notice


def test_stop_sequence_with_text_is_not_a_final_answer():
    action = decode_action(response(text("not complete"), stop=StopReason.STOP_SEQUENCE))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "stop_sequence"


def test_unknown_stop_reason_is_not_treated_as_end_turn():
    action = decode_action(response(text("ambiguous"), stop=StopReason.UNKNOWN))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "unsupported_response_shape"


def test_max_tokens_plain_text_is_marked_truncated():
    assert decode_action(
        response(text("partial but useful"), stop=StopReason.MAX_TOKENS)
    ) == FinalAction(
        text="partial but useful",
        origin="provider_text",
        truncated=True,
    )


def test_unsupported_content_shape_is_a_total_retry():
    action = decode_action(response({"type": "image", "source": "x"}))
    assert isinstance(action, RetryAction)
    assert action.reason_code == "unsupported_response_shape"
