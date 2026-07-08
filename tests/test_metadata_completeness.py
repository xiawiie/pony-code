"""Task C5: metadata surface completeness gate.

Locks in that build_v2 populates every field spec §9 promised. Runs
with a fully-mocked agent so no external state matters."""

from unittest.mock import MagicMock

from pico.context_manager import ContextManager


REQUIRED_METADATA_FIELDS = {
    "system_cache_key",
    "system_tokens",
    "tools_tokens",
    "messages_count",
    "messages_tokens",
    "cache_control_breakpoints",
    "injection_tokens",
    "injection_truncated",
    "injection_dropped",
    "injection_budget",
    "intent",
    "recall.error_count",
    "recall.last_error",
    "dropped_messages",
    "prompt_cache_key",
}


def test_metadata_covers_spec_section_9():
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    a.session = {"messages": [{"role": "assistant", "content": "prev"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}

    cm = ContextManager(a)
    _request, metadata = cm.build_v2("hi")

    missing = REQUIRED_METADATA_FIELDS - set(metadata.keys())
    assert not missing, f"metadata missing spec §9 fields: {sorted(missing)}"

    # Structural checks on non-scalar fields
    assert isinstance(metadata["intent"], dict)
    assert "name" in metadata["intent"]
    assert "matched_keyword" in metadata["intent"]
    assert "matched_reason" in metadata["intent"]
    assert isinstance(metadata["injection_tokens"], dict)
    assert isinstance(metadata["injection_dropped"], list)
    assert isinstance(metadata["cache_control_breakpoints"], list)
