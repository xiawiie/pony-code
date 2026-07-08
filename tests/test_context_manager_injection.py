"""Tests for ContextManager.build_v2 injection wiring (Task 14):
- current user message wrapped with <system-reminder> injection
- pinned layer (system + tools) overflow raises SystemTooBig
- telemetry from renderer merged into metadata
"""

from unittest.mock import MagicMock

import pytest

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


def test_build_v2_current_message_contains_injection():
    cm = ContextManager(_agent())
    request, _metadata = cm.build_v2("hello")
    current = request["messages"][-1]["content"]
    assert "<system-reminder>" in current
    assert "<pico:workspace_state>" in current
    assert current.strip().endswith("hello")


def test_build_v2_telemetry_records_intent():
    cm = ContextManager(_agent())
    _request, metadata = cm.build_v2("上次讨论过什么？")
    assert metadata["intent"]["name"] == "recall"
    assert "injection_tokens" in metadata
    assert "injection_truncated" in metadata


def test_build_v2_pinned_layer_overflow_failloud():
    a = _agent()
    a.prefix = "x" * 200_000  # ~50K tokens (via /4 fallback), well above 20K cap
    a.tools = {}
    cm = ContextManager(a)
    with pytest.raises(RuntimeError, match="SystemTooBig"):
        cm.build_v2("hi")


def test_build_v2_metadata_includes_system_and_tools_tokens():
    cm = ContextManager(_agent())
    _request, metadata = cm.build_v2("hello")
    assert "system_tokens" in metadata
    assert "tools_tokens" in metadata
    assert isinstance(metadata["system_tokens"], int)
    assert isinstance(metadata["tools_tokens"], int)


def test_build_v2_last_user_already_present_skips_append():
    # When the session already ends with a user turn, build_v2 must NOT
    # duplicate the user message (Anthropic rejects back-to-back user).
    a = _agent()
    a.session = {"messages": [{"role": "user", "content": "already here"}]}
    cm = ContextManager(a)
    request, _metadata = cm.build_v2("would be duplicate")
    # only one user message in the messages array
    assert len(request["messages"]) == 1
    assert request["messages"][0]["content"] == "already here"
