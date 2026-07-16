"""Tests for ContextManager.build_request injection wiring (Task 14):
- current user message wrapped with <system-reminder> injection
- pinned layer (system + tools) overflow raises SystemTooBig
- telemetry from renderer merged into metadata
"""

from unittest.mock import MagicMock

import pytest

from pico.context.renderer import render_current_user_message
from pico.context_manager import ContextManager
from pico.messages import make_tool_pair


def _agent():
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    # Fresh session — no pre-existing tail user message to trigger dedupe.
    a.session = {"messages": [{"role": "assistant", "content": "prev"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="- branch: main")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    return a


def _build_request(agent, user_message):
    agent.session["messages"].append(
        {"role": "user", "content": user_message, "_pico_meta": {}}
    )
    snapshot, telemetry = render_current_user_message(agent, user_message)
    return ContextManager(agent).build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )


def test_build_request_current_message_contains_injection():
    request, _metadata = _build_request(_agent(), "hello")
    current = request["messages"][-1]["content"]
    assert "<system-reminder>" in current
    assert "<pico:workspace_state>" in current
    assert current.strip().endswith("hello")


def test_build_request_telemetry_records_source_allocator():
    _request, metadata = _build_request(_agent(), "上次讨论过什么？")
    assert "context_source_allocator" in metadata
    assert "injection_tokens" in metadata
    assert "injection_truncated" in metadata


def test_build_request_pinned_layer_overflow_failloud():
    a = _agent()
    a.prefix = "x" * 200_000  # ~50K tokens (via /4 fallback), well above 20K cap
    a.tools = {}
    with pytest.raises(RuntimeError, match="SystemContextTooLarge"):
        _build_request(a, "hi")


def test_build_request_metadata_includes_system_and_tools_tokens():
    _request, metadata = _build_request(_agent(), "hello")
    assert "system_tokens" in metadata
    assert "tools_tokens" in metadata
    assert isinstance(metadata["system_tokens"], int)
    assert isinstance(metadata["tools_tokens"], int)


def test_build_request_replaces_persisted_current_user_in_request_view():
    a = _agent()
    a.session = {"messages": [{"role": "user", "content": "already here", "_pico_meta": {}}]}
    snapshot, telemetry = render_current_user_message(a, "already here")
    request, _metadata = ContextManager(a).build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )
    assert len(request["messages"]) == 1
    assert request["messages"][0]["content"] == snapshot


def test_build_request_tools_tokens_uses_json_serialization():
    """Task A3: tools_tokens must reflect JSON wire size, not Python repr."""
    from unittest.mock import MagicMock

    a = MagicMock()
    a.prefix = "sys"
    a.tools = {
        "read_file": {
            "schema": {"path": "str"},
            "risky": False,
            "description": "Read a file.",
        },
    }
    a.session = {"messages": [{"role": "assistant", "content": "prev"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))

    request, metadata = _build_request(a, "hello")
    # Recompute the expected token count against the JSON-serialized tools.
    expected = ContextManager(a).accounting.count_json(request["tools"])
    assert metadata["tools_tokens"] == expected


def test_build_request_budget_counts_opaque_provider_state():
    state = [{"type": "reasoning", "encrypted_content": "x" * 200}]

    def history_tokens(provider_state):
        a = _agent()
        a.model_client.count_tokens = lambda text: len(text)
        pair = make_tool_pair(
            name="read_file",
            arguments={"path": "README.md"},
            tool_use_id="toolu_state",
            result_content="body",
            created_at="now",
            tool_status="ok",
            effect_class="read_only",
            provider_state=provider_state,
        )
        a.session = {
            "messages": [
                {"role": "user", "content": "question", "_pico_meta": {}},
                *pair,
                {"role": "user", "content": "next", "_pico_meta": {}},
            ]
        }
        snapshot, telemetry = render_current_user_message(a, "next")
        _request, metadata = ContextManager(a).build_request(
            injection_snapshot=snapshot,
            injection_telemetry=telemetry,
            preflight_metadata={},
        )
        return metadata["context_breakdown"]["history"]["actual_tokens"]

    counter_agent = _agent()
    counter_agent.model_client.count_tokens = lambda text: len(text)
    accounting = ContextManager(counter_agent).accounting
    with_state = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_state",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
        provider_state=state,
    )[0]
    without_state = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="toolu_state",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
    )[0]

    assert history_tokens(state) - history_tokens(()) == (
        accounting.count_message(with_state)
        - accounting.count_message(without_state)
    )
