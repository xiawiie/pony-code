from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pony.context.renderer import (
    build_injection_snapshot,
    render_current_user_message,
)
from pony.agent.model_capabilities import TokenAccounting
from pony.security.redaction import SensitiveDataBlockedError


def _agent(*, pool=16_384):
    return SimpleNamespace(
        workspace=MagicMock(
            volatile_text=lambda: (
                "<workspace_state>\n- branch: main\n- status: clean\n</workspace_state>"
            )
        ),
        memory_store=None,
        memory_retrieval=None,
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda text: max(1, len(str(text)) // 4)),
        token_accounting=TokenAccounting(lambda text: max(1, len(str(text)) // 4)),
        model_budget=SimpleNamespace(source_pool_tokens=pool),
        memory=SimpleNamespace(task_summary="", recent_files=[]),
        session={"recently_recalled": [], "messages": [], "memory": {}},
        resume_state={},
        sandbox_session=None,
        redaction_env={},
        secret_env_names=(),
        context_config={"source_pool_tokens": pool, "recall": {}},
    )


def test_renders_current_message_with_wrapped_source_and_user_trailing():
    text, telemetry = render_current_user_message(_agent(), "hi")

    assert "<system-reminder>" in text
    assert "<pony:workspace_state>" in text
    assert text.strip().endswith("hi")
    assert "intent" not in telemetry
    assert telemetry["context_source_allocator"]["selected_chunks"] >= 1


def test_source_content_tags_are_escaped_but_renderer_tags_remain_structural():
    agent = _agent()
    agent.workspace.volatile_text = lambda: "<pony:evil>attack</pony:evil>"

    text, _ = render_current_user_message(agent, "hi")

    assert "<pony​:evil>" in text
    assert "</pony:workspace_state>" in text


def test_zero_source_pool_drops_optional_chunks_without_touching_user_message():
    text, telemetry = render_current_user_message(_agent(pool=0), "raw user message")

    assert text == "raw user message"
    assert telemetry["injection_budget"] == 0
    assert telemetry["injection_dropped"] == ["workspace_state"]


def test_snapshot_preserves_multiline_source_without_reverse_parsing():
    agent = _agent()
    agent.workspace.volatile_text = lambda: (
        "first paragraph\n\nsecond paragraph\nclosing"
    )

    snapshot, telemetry = build_injection_snapshot(agent, "question")
    source = next(item for item in snapshot.sources if item.name == "workspace_state")

    assert "first paragraph\n\nsecond paragraph\nclosing" in source.text
    assert source.chunk_keys == ("workspace-identity", "workspace-state-0")
    assert snapshot.render().endswith("question")
    assert telemetry["context_source_allocator"]["memory_snapshot"] == "disabled"


def test_recovery_state_is_required_and_selected_before_optional_sources():
    agent = _agent(pool=300)
    agent.resume_state = {
        "status": "workspace-mismatch",
        "runtime_identity_mismatch_fields": ["cwd"],
        "stale_paths": [],
    }

    snapshot, _ = build_injection_snapshot(agent, "continue")
    recovery = next(item for item in snapshot.sources if item.name == "recovery_state")

    assert recovery.required is True
    assert recovery.status == "included"
    assert "workspace-mismatch" in recovery.text


def test_telemetry_reports_pool_source_and_whole_chunk_counts():
    _, telemetry = render_current_user_message(_agent(), "hi")
    allocator = telemetry["context_source_allocator"]

    assert allocator["name"] == "priority_allocator"
    assert allocator["pool_tokens"] == 16_384
    assert allocator["used_tokens"] <= allocator["pool_tokens"]
    assert allocator["remaining_tokens"] == (
        allocator["pool_tokens"] - allocator["used_tokens"]
    )
    assert telemetry["injection_truncated"] == {}


def test_memory_snapshot_security_failure_is_never_downgraded_to_empty_memory():
    agent = _agent()
    agent.memory_retrieval = MagicMock()
    agent.memory_retrieval.snapshot.side_effect = SensitiveDataBlockedError(
        "blocked residual"
    )

    with pytest.raises(SensitiveDataBlockedError, match="blocked residual"):
        render_current_user_message(agent, "hi")
