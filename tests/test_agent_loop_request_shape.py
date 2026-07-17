"""Canonical agent-loop message constructors preserve the request wire shape."""

from pony.agent.messages import make_tool_pair, message_content_text


def test_plain_message_builds_user_message():
    from pony.agent.loop import _plain_message

    message = _plain_message("user", "hello world")
    assert message == {
        "role": "user",
        "content": "hello world",
        "_pony_meta": {"created_at": message["_pony_meta"]["created_at"]},
    }


def test_make_tool_pair_has_request_wire_shape():
    tool_use, tool_result = make_tool_pair(
        name="read_file",
        arguments={"path": "a.py"},
        tool_use_id="toolu_x",
        result_content="file text",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
    )

    assert tool_use["role"] == "assistant"
    assert tool_use["content"][0]["type"] == "tool_use"
    assert tool_use["content"][0]["id"] == "toolu_x"
    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "toolu_x"
    assert tool_result["content"][0]["content"] == "file text"


def test_make_tool_pair_carries_meta_fields():
    """Task E8: paired tool messages carry the required _pony_meta fields."""
    tool_use, tool_result = make_tool_pair(
        name="read_file",
        arguments={"path": "a.py"},
        tool_use_id="t1",
        result_content="short",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
        result_meta={"digest_applied": False, "source_hash": None},
    )

    assert tool_use["_pony_meta"]["tool_use_id"] == "t1"
    assert "created_at" in tool_use["_pony_meta"]
    assert tool_result["_pony_meta"]["tool_use_id"] == "t1"
    assert "created_at" in tool_result["_pony_meta"]
    assert tool_result["_pony_meta"]["digest_applied"] is False


def test_make_tool_pair_keeps_provider_state_outside_rendered_content():
    state = [{"type": "reasoning", "encrypted_content": "opaque"}]
    tool_use, tool_result = make_tool_pair(
        name="read_file",
        arguments={"path": "a.py"},
        tool_use_id="t1",
        result_content="body",
        created_at="now",
        tool_status="success",
        effect_class="read_only",
        provider_state=state,
    )

    assert tool_use["_pony_provider_state"] == state
    assert "_pony_provider_state" not in tool_result
    assert "opaque" not in message_content_text(tool_use)
