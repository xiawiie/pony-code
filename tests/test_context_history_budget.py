"""History is either sent intact or compacted explicitly; it is never dropped."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pony.context.renderer import render_current_user_message
from pony.agent.context_manager import ContextBudgetExceeded, ContextManager
from pony.agent.model_capabilities import (
    build_model_budget,
    ModelCapabilities,
    TokenAccounting,
)


def _message(role, content):
    return {"role": role, "content": content, "_pony_meta": {"created_at": "t"}}


def _agent(messages, *, context_window=32_768):
    accounting = TokenAccounting()
    capabilities = ModelCapabilities(
        context_window=context_window,
        max_output_tokens=16_384,
        token_counter_mode="provider_usage_or_estimate",
        source="config",
    )
    budget = build_model_budget(
        capabilities,
        output_limit=16_384,
        reserve_tokens=16_384,
        source_pool_tokens=1_024,
        system_tools_hard_cap=4_096,
    )
    return SimpleNamespace(
        prefix="system",
        tools={},
        visible_tools=lambda: {},
        session={"id": "", "messages": list(messages), "recently_recalled": []},
        session_store=None,
        workspace=MagicMock(volatile_text=lambda: ""),
        memory_store=None,
        memory_retrieval=None,
        repo_map=None,
        memory=SimpleNamespace(task_summary="", recent_files=[]),
        render_checkpoint_text=lambda: "",
        resume_state={},
        sandbox_session=None,
        model_client=MagicMock(supports_prompt_cache=False),
        token_accounting=accounting,
        model_budget=budget,
        context_config={
            "source_pool_tokens": 1_024,
            "compaction": {"enabled": True},
            "recall": {},
        },
        redaction_env={},
        secret_env_names=(),
        _pending_token_anchor=None,
    )


def _request(agent, current_user):
    snapshot, telemetry = render_current_user_message(agent, current_user)
    return ContextManager(agent).build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )


def test_history_below_limit_is_sent_intact():
    messages = [
        _message("user" if index % 2 == 0 else "assistant", f"history-{index}")
        for index in range(20)
    ]
    messages.append(_message("user", "current"))
    agent = _agent(messages)

    request, metadata = _request(agent, "current")

    rendered = repr(request["messages"])
    assert "history-0" in rendered
    assert "history-19" in rendered
    assert metadata["dropped_messages"] == 0
    assert metadata["context_breakdown"]["history"]["dropped_turns"] == 0


def test_over_limit_fails_without_mutating_or_slicing_history():
    messages = [
        _message("user", "old-0 " + ("x" * 80_000)),
        _message("assistant", "old-1 " + ("y" * 20_000)),
        _message("user", "current"),
    ]
    agent = _agent(messages)
    before = list(agent.session["messages"])

    with pytest.raises(ContextBudgetExceeded, match="Run /compact"):
        _request(agent, "current")

    assert agent.session["messages"] == before


def test_history_budget_is_dynamic_remainder_of_actual_request():
    messages = [_message("user", "question")]
    agent = _agent(messages, context_window=128_000)

    _, metadata = _request(agent, "question")
    breakdown = metadata["context_breakdown"]

    assert breakdown["history"]["budget"] == (
        breakdown["budget"]["input_limit"]
        - breakdown["pinned"]["actual"]
        - breakdown["current_request"]["actual_tokens"]
    )
    assert breakdown["current_request"]["user_tokens"] > 0
    assert breakdown["budget"]["remaining"] >= 0
