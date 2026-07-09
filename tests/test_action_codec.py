from pico.action_codec import ActionCodec
from pico.model_actions import ActionOrigin, FinalAction, RetryAction, ToolAction
from pico.providers.response import Response, StopReason


def decode(response):
    return ActionCodec().decode(response)


def test_native_single_tool_call_decodes_to_tool_action():
    action = decode(
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
            usage={},
        )
    )

    assert action == ToolAction(
        name="read_file",
        arguments={"path": "README.md"},
        id="toolu_1",
        origin=ActionOrigin.NATIVE_TOOL_USE,
    )


def test_native_multiple_tool_calls_tracks_ignored_count():
    action = decode(
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                {"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "b", "name": "read_file", "input": {"path": "b.py"}},
            ],
            usage={},
        )
    )

    assert isinstance(action, ToolAction)
    assert action.name == "read_file"
    assert action.arguments == {"path": "a.py"}
    assert action.ignored_tool_count == 1


def test_native_tool_call_missing_name_is_retry_not_tool_action():
    action = decode(
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "toolu_bad", "name": "", "input": {"path": "README.md"}}],
            usage={},
        )
    )

    assert isinstance(action, RetryAction)
    assert action.origin == ActionOrigin.UNSUPPORTED_RESPONSE
    assert action.model_visible is True
    assert "tool name" in action.reason


def test_native_tool_call_non_mapping_input_is_retry_not_tool_action():
    action = decode(
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "toolu_bad", "name": "read_file", "input": "not-a-dict"}],
            usage={},
        )
    )

    assert isinstance(action, RetryAction)
    assert action.origin == ActionOrigin.UNSUPPORTED_RESPONSE
    assert action.model_visible is True
    assert "tool arguments" in action.reason


def test_leading_json_tool_protocol_decodes_to_tool_action():
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[
                {
                    "type": "text",
                    "text": '  <tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
                }
            ],
            usage={},
        )
    )

    assert action == ToolAction(
        name="read_file",
        arguments={"path": "README.md"},
        id=None,
        origin=ActionOrigin.TEXT_PROTOCOL_TOOL,
    )


def test_leading_json_tool_protocol_accepts_arguments_alias():
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[
                {
                    "type": "text",
                    "text": '<tool>{"name":"read_file","arguments":{"path":"README.md"}}</tool>',
                }
            ],
            usage={},
        )
    )

    assert isinstance(action, ToolAction)
    assert action.name == "read_file"
    assert action.arguments == {"path": "README.md"}
    assert action.origin == ActionOrigin.TEXT_PROTOCOL_TOOL


def test_leading_xml_attribute_tool_protocol_decodes_to_tool_action():
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[
                {
                    "type": "text",
                    "text": '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
                }
            ],
            usage={},
        )
    )

    assert isinstance(action, ToolAction)
    assert action.name == "write_file"
    assert action.arguments == {"path": "hello.py", "content": 'print("hi")\n'}
    assert action.origin == ActionOrigin.TEXT_PROTOCOL_TOOL


def test_non_leading_tool_protocol_is_plain_text_final():
    text = 'Here is an example:\n<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>'
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": text}],
            usage={},
        )
    )

    assert action == FinalAction(text=text, origin=ActionOrigin.PLAIN_TEXT_FINAL)


def test_toolkit_prefix_does_not_trigger_tool_execution():
    text = "<toolkit>documentation only</toolkit>"
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": text}],
            usage={},
        )
    )

    assert action == FinalAction(text=text, origin=ActionOrigin.PLAIN_TEXT_FINAL)


def test_leading_malformed_tool_protocol_is_model_visible_retry():
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<tool>{not valid json</tool>"}],
            usage={},
        )
    )

    assert isinstance(action, RetryAction)
    assert action.model_visible is True
    assert action.origin == ActionOrigin.MALFORMED_TEXT_PROTOCOL
    assert "valid <tool> call" in action.reason


def test_final_protocol_decodes_to_final_action():
    action = decode(
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<final>Done.</final>"}],
            usage={},
        )
    )

    assert action == FinalAction(text="Done.", origin=ActionOrigin.TEXT_PROTOCOL_FINAL)


def test_plain_text_with_max_tokens_becomes_model_visible_retry():
    action = decode(
        Response(
            stop_reason=StopReason.MAX_TOKENS,
            content=[{"type": "text", "text": "partial but useful"}],
            usage={},
        )
    )

    assert isinstance(action, RetryAction)
    assert action.origin == ActionOrigin.STOP_SEQUENCE
    assert action.model_visible is True
    assert "valid <tool> call" in action.reason


def test_valid_final_protocol_still_decodes_with_max_tokens():
    action = decode(
        Response(
            stop_reason=StopReason.MAX_TOKENS,
            content=[{"type": "text", "text": "<final>Done.</final>"}],
            usage={},
        )
    )

    assert action == FinalAction(text="Done.", origin=ActionOrigin.TEXT_PROTOCOL_FINAL)


def test_empty_response_is_retry():
    action = decode(Response(stop_reason=StopReason.END_TURN, content=[], usage={}))

    assert isinstance(action, RetryAction)
    assert action.origin == ActionOrigin.EMPTY_RESPONSE
    assert action.model_visible is True


def test_stop_sequence_without_content_is_model_visible_retry():
    action = decode(Response(stop_reason=StopReason.STOP_SEQUENCE, content=[], usage={}))

    assert isinstance(action, RetryAction)
    assert action.origin == ActionOrigin.STOP_SEQUENCE
    assert action.model_visible is True


def test_stop_sequence_with_valid_leading_tool_still_decodes_to_tool_action():
    action = decode(
        Response(
            stop_reason=StopReason.STOP_SEQUENCE,
            content=[{"type": "text", "text": '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>'}],
            usage={},
        )
    )

    assert isinstance(action, ToolAction)
    assert action.name == "read_file"
    assert action.arguments == {"path": "README.md"}
    assert action.origin == ActionOrigin.TEXT_PROTOCOL_TOOL


def test_stop_sequence_with_valid_final_still_decodes_to_final_action():
    action = decode(
        Response(
            stop_reason=StopReason.STOP_SEQUENCE,
            content=[{"type": "text", "text": "<final>Done.</final>"}],
            usage={},
        )
    )

    assert action == FinalAction(text="Done.", origin=ActionOrigin.TEXT_PROTOCOL_FINAL)


def test_stop_sequence_with_text_is_model_visible_retry_not_final():
    action = decode(
        Response(
            stop_reason=StopReason.STOP_SEQUENCE,
            content=[{"type": "text", "text": "Runtime notice: use a valid <tool> call."}],
            usage={},
        )
    )

    assert isinstance(action, RetryAction)
    assert action.origin == ActionOrigin.STOP_SEQUENCE
    assert action.model_visible is True
    assert "valid <tool> call" in action.reason
