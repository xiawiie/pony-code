"""Task C5: metadata surface completeness gate.

Locks in that build_v2 populates every field spec §9 promised. Runs
with a fully-mocked agent so no external state matters."""

from unittest.mock import MagicMock

from pico.context.renderer import render_current_user_message
from pico.context_manager import ContextManager


REQUIRED_METADATA_FIELDS = {
    "system_cache_key",
    "system_tokens",
    "tools_tokens",
    "prompt_cache_supported",
    "messages_count",
    "messages_chars",
    "messages_tokens",
    "dropped_messages",
    "cache_control_breakpoints",
    "runtime_feedback_present",
    "injection_tokens",
    "injection_truncated",
    "injection_dropped",
    "injection_budget",
    "intent",
    "prefix_chars",
    "workspace_changed",
    "prefix_changed",
    "workspace_fingerprint",
    "tool_signature",
    "resume_status",
    "request_chars",
    "tool_count",
    "workspace_docs",
    "recent_commits",
}

FORBIDDEN_METADATA_FIELDS = {
    "prompt" + "_chars",
    "sections",
    "section" + "_order",
    "section" + "_budgets",
    "budget" + "_reductions",
    "history" + "_chars",
    "prompt" + "_cache_key",
}


def test_metadata_covers_spec_section_9():
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    a.session = {"messages": [{"role": "user", "content": "hi"}]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}

    cm = ContextManager(a)
    snapshot, telemetry = render_current_user_message(a, "hi")
    _request, metadata = cm.build_v2(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={
            "prefix_chars": len(a.prefix),
            "workspace_changed": False,
            "prefix_changed": False,
            "workspace_fingerprint": "workspace",
            "tool_signature": "tools",
            "resume_status": "no-checkpoint",
            "request_chars": 2,
            "tool_count": 0,
            "workspace_docs": 0,
            "recent_commits": 0,
        },
    )

    missing = REQUIRED_METADATA_FIELDS - set(metadata.keys())
    assert not missing, f"metadata missing spec §9 fields: {sorted(missing)}"
    assert FORBIDDEN_METADATA_FIELDS.isdisjoint(metadata)

    # Structural checks on non-scalar fields
    assert isinstance(metadata["intent"], dict)
    assert "name" in metadata["intent"]
    assert "matched_keyword" in metadata["intent"]
    assert "matched_reason" in metadata["intent"]
    assert isinstance(metadata["injection_tokens"], dict)
    assert isinstance(metadata["injection_dropped"], list)
    assert isinstance(metadata["cache_control_breakpoints"], list)
