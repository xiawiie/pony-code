"""Tests for ContextManager.build_v2 injection wiring (Task 14):
- current user message wrapped with <system-reminder> injection
- pinned layer (system + tools) overflow raises SystemTooBig
- telemetry from renderer merged into metadata
"""

from unittest.mock import MagicMock

import pytest

from pico.context.renderer import render_current_user_message
from pico.context_manager import ContextManager


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


def _build_v2(agent, user_message):
    agent.session["messages"].append(
        {"role": "user", "content": user_message, "_pico_meta": {}}
    )
    snapshot, telemetry = render_current_user_message(agent, user_message)
    return ContextManager(agent).build_v2(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )


def test_build_v2_current_message_contains_injection():
    request, _metadata = _build_v2(_agent(), "hello")
    current = request["messages"][-1]["content"]
    assert "<system-reminder>" in current
    assert "<pico:workspace_state>" in current
    assert current.strip().endswith("hello")


def test_build_v2_telemetry_records_intent():
    _request, metadata = _build_v2(_agent(), "上次讨论过什么？")
    assert metadata["intent"]["name"] == "recall"
    assert "injection_tokens" in metadata
    assert "injection_truncated" in metadata


def test_build_v2_pinned_layer_overflow_failloud():
    a = _agent()
    a.prefix = "x" * 200_000  # ~50K tokens (via /4 fallback), well above 20K cap
    a.tools = {}
    with pytest.raises(RuntimeError, match="SystemTooBig"):
        _build_v2(a, "hi")


def test_build_v2_metadata_includes_system_and_tools_tokens():
    _request, metadata = _build_v2(_agent(), "hello")
    assert "system_tokens" in metadata
    assert "tools_tokens" in metadata
    assert isinstance(metadata["system_tokens"], int)
    assert isinstance(metadata["tools_tokens"], int)


def test_build_v2_replaces_persisted_current_user_in_request_view():
    a = _agent()
    a.session = {"messages": [{"role": "user", "content": "already here", "_pico_meta": {}}]}
    snapshot, telemetry = render_current_user_message(a, "already here")
    request, _metadata = ContextManager(a).build_v2(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )
    assert len(request["messages"]) == 1
    assert request["messages"][0]["content"] == snapshot


def test_build_v2_tools_tokens_uses_json_serialization():
    """Task A3: tools_tokens must reflect JSON wire size, not Python repr."""
    import json
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

    request, metadata = _build_v2(a, "hello")
    # Recompute the expected token count against the JSON-serialized tools.
    expected = max(1, len(json.dumps(request["tools"])) // 4)
    assert metadata["tools_tokens"] == expected
