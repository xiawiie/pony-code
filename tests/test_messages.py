import copy

import pytest

from pico.messages import (
    MessageValidationError,
    append_messages,
    build_request_messages,
    make_tool_pair,
    message_metrics,
    render_transcript,
    strip_pico_meta,
    validate_messages,
)


def plain(role, content, created_at="2026-07-10T00:00:00Z"):
    return {"role": role, "content": content, "_pico_meta": {"created_at": created_at}}


def test_request_overlay_replaces_latest_plain_user_and_ignores_tool_result_carrier():
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_1",
        result_content="body",
        created_at="2026-07-10T00:00:01Z",
        tool_status="ok",
        effect_class="read_only",
    )
    source = [plain("user", "question"), *pair]
    before = copy.deepcopy(source)
    request = build_request_messages(
        source,
        rendered_user="<system-reminder>snapshot</system-reminder>\nquestion",
        runtime_feedback="use a valid tool call",
    )
    assert source == before
    assert "snapshot" in request[0]["content"]
    assert "<pico:runtime_feedback>" in request[0]["content"]
    assert request[-1]["content"][0]["type"] == "tool_result"
    assert all("_pico_meta" not in message for message in request)


def test_runtime_feedback_is_absent_when_empty():
    request = build_request_messages(
        [plain("user", "question")],
        rendered_user="snapshot\nquestion",
    )
    assert "runtime_feedback" not in request[0]["content"]


def test_append_messages_does_not_mutate_input():
    source = [plain("user", "q")]
    result = append_messages(source, plain("assistant", "a"))
    assert len(source) == 1
    assert [item["role"] for item in result] == ["user", "assistant"]


def test_tool_pair_has_matching_id_error_semantics_and_metadata():
    assistant, result = make_tool_pair(
        name="run_shell",
        arguments={"command": "false"},
        tool_use_id="toolu_2",
        result_content="exit_code: 1",
        created_at="2026-07-10T00:00:00Z",
        tool_status="error",
        effect_class="workspace_write",
        tool_change_id="tc_1",
    )
    assert assistant["content"][0]["id"] == "toolu_2"
    assert result["content"][0]["tool_use_id"] == "toolu_2"
    assert result["content"][0]["is_error"] is True
    assert result["_pico_meta"]["tool_status"] == "error"
    assert result["_pico_meta"]["effect_class"] == "workspace_write"
    assert result["_pico_meta"]["tool_change_id"] == "tc_1"


def test_strip_pico_meta_returns_new_top_level_dicts():
    source = [plain("user", "q")]
    cleaned = strip_pico_meta(source)
    assert "_pico_meta" not in cleaned[0]
    assert "_pico_meta" in source[0]
    assert cleaned[0] is not source[0]


def test_render_and_metrics_use_content_not_internal_meta():
    messages = [plain("user", "question"), plain("assistant", "answer")]
    rendered = render_transcript(messages)
    metrics = message_metrics(messages, token_of=lambda value: len(value))
    assert "[user] question" in rendered
    assert "[assistant] answer" in rendered
    assert "created_at" not in rendered
    assert metrics == {
        "messages_count": 2,
        "messages_chars": len("question") + len("answer"),
        "messages_tokens": len("question") + len("answer"),
    }


def test_validate_messages_accepts_a_complete_pair():
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_ok",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
    )
    validate_messages(
        [plain("user", "q"), *pair, plain("assistant", "done")],
        require_meta=True,
    )


def test_validate_messages_requires_meta_on_tool_result_carrier():
    assistant, result = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_meta",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
    )
    result.pop("_pico_meta")

    with pytest.raises(MessageValidationError):
        validate_messages([assistant, result], require_meta=True)


def test_validate_messages_rejects_non_dict_paired_result():
    assistant = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_bad_result", "name": "x", "input": {}}
        ],
        "_pico_meta": {},
    }

    with pytest.raises(MessageValidationError):
        validate_messages([assistant, "not-a-message"], require_meta=True)


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "system", "content": "bad", "_pico_meta": {}}],
        [{"role": "assistant", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "bad"}], "_pico_meta": {}}],
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "", "name": "x", "input": {}}], "_pico_meta": {}}],
        [{"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "x", "input": {}}], "_pico_meta": {}}],
    ],
)
def test_validate_messages_rejects_bad_roles_blocks_ids_and_orphans(messages):
    with pytest.raises(MessageValidationError):
        validate_messages(messages, require_meta=True)
