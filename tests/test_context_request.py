"""Tests for the provider request built from canonical messages."""

from unittest.mock import MagicMock

from pony.context.renderer import render_current_user_message
from pony.agent.context_manager import ContextManager
from pony.agent.model_capabilities import (
    ModelCapabilities,
    TokenAccounting,
    build_model_budget,
)


def _make_agent():
    a = MagicMock()
    a.prefix = "SYSTEM_CORE_TEXT"
    a.tools = {
        "read_file": {
            "schema": {"path": "str"},
            "risky": False,
            "description": "Read a file.",
        },
        "write_file": {
            "schema": {"path": "str", "content": "str"},
            "risky": True,
            "description": "Write a file.",
        },
    }
    a.visible_tools = lambda: dict(a.tools)
    a.session = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="<workspace_state>...</workspace_state>")
    a.render_checkpoint_text = MagicMock(return_value="")
    a.feature_enabled = MagicMock(return_value=True)
    a.memory_store = None
    a.repo_map = None
    a.model_client = MagicMock(count_tokens=lambda t: len(t) // 4)
    capabilities = ModelCapabilities(128_000, 16_384, "test", "test")
    a.model_budget = build_model_budget(capabilities)
    a.token_accounting = TokenAccounting(a.model_client.count_tokens)
    a.context_config = {"compaction": {"enabled": True}}
    a.redaction_env = {}
    a.secret_env_names = ()
    a.session_store = None
    return a


def _build_request(agent, user_message):
    agent.session["messages"].append(
        {"role": "user", "content": user_message, "_pony_meta": {}}
    )
    snapshot, telemetry = render_current_user_message(agent, user_message)
    return ContextManager(agent).build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )


def test_build_request_returns_system_tools_messages():
    a = _make_agent()
    request, metadata = _build_request(a, "current input")
    assert isinstance(request, dict)
    assert isinstance(request["system"], list)
    assert request["system"][0]["type"] == "text"
    assert "SYSTEM_CORE_TEXT" in request["system"][0]["text"]
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(request["tools"], list)
    # tools 转换到 Anthropic schema
    tools_by_name = {t["name"]: t for t in request["tools"]}
    assert "read_file" in tools_by_name
    assert "input_schema" in tools_by_name["read_file"]
    # risky flag 迁移到 description
    assert "approval" in tools_by_name["write_file"]["description"].lower()


def test_build_request_uses_persisted_current_user_message():
    a = _make_agent()
    request, _ = _build_request(a, "current input")
    assert request["messages"][-1]["role"] == "user"
    text = request["messages"][-1]["content"]
    assert "current input" in text


def test_build_request_history_messages_preserved():
    a = _make_agent()
    request, _ = _build_request(a, "x")
    # 历史两条 + 当前一条
    assert len(request["messages"]) == 3
    assert request["messages"][0]["content"] == "hello"
    assert request["messages"][1]["content"] == "hi there"


def test_build_request_cache_breakpoint_on_second_to_last():
    a = _make_agent()
    request, _ = _build_request(a, "x")
    # messages 长度 3，断点 2 应位于 index 1（当前 user 消息的前一条）
    assert request["cache_control_breakpoints"] == [len(request["messages"]) - 2]


def test_build_request_metadata_contains_system_prefix_hash():
    import hashlib
    a = _make_agent()
    _, metadata = _build_request(a, "x")
    assert "system_prefix_hash" in metadata
    expected = hashlib.sha256(a.prefix.encode("utf-8")).hexdigest()
    assert metadata["system_prefix_hash"] == expected


def test_int_schema_field_maps_to_integer_json_type():
    """Task E8: tool schema 'int' variants must map to Anthropic-shape
    input_schema.properties.<field>.type = 'integer', not 'string'."""
    from pony.agent.context_manager import _build_tools_list

    tools = {
        "read_file": {
            "schema": {"start": "int=1", "end": "int=200"},
            "risky": False,
            "description": "read a slice",
        },
    }
    out = _build_tools_list(tools)
    props = out[0]["input_schema"]["properties"]
    assert props["start"]["type"] == "integer"
    assert props["end"]["type"] == "integer"
