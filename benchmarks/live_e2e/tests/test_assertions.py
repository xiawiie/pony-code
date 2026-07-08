"""Unit tests for benchmarks.live_e2e.run_live_session.AssertionEngine.

Tests are offline: no API is called, no fixture writes, no pico repo mutation.
Populated in Tasks 6-9.
"""

from unittest.mock import MagicMock

from benchmarks.live_e2e.run_live_session import (
    Assertion,
    AssertionEngine,
    TurnResult,
)


def _turn_result_stub(**overrides):
    defaults = dict(
        turn=1,
        user_prompt="上次讨论过 cache invariant 的问题",
        expected_behavior="recall_triggered",
        final_answer="ok",
        metadata={
            "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
            "injection_tokens": {"recalled_memory": 42, "workspace_state": 10},
            "recall.error_count": 0,
        },
        session_message_count_before=0,
        session_message_count_after=2,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={"input_tokens": 10, "output_tokens": 5},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=1,
        current_user_content=(
            "<system-reminder><pico:recalled_memory path=\"workspace/agent/cache-invariant.md\">"
            "content</pico:recalled_memory></system-reminder>\n上次讨论过 cache invariant 的问题"
        ),
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_1_recall_passes_on_valid_metadata():
    engine = AssertionEngine()
    result = _turn_result_stub()
    asserts = engine.check_turn_1_recall(result)
    # All 6 required assertions present and passed
    assert len(asserts) == 6
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_1_recall_fails_when_intent_not_recall():
    engine = AssertionEngine()
    result = _turn_result_stub(metadata={
        "intent": {"name": "default", "matched_keyword": "", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 42},
        "recall.error_count": 0,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "intent_name_recall" for a in failed)


def test_check_turn_1_recall_fails_when_no_recall_block_rendered():
    engine = AssertionEngine()
    result = _turn_result_stub(current_user_content="上次讨论过什么", metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 0},
        "recall.error_count": 0,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_block_present" for a in failed)


def test_check_turn_1_recall_fails_when_recall_error_nonzero():
    engine = AssertionEngine()
    result = _turn_result_stub(metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 42},
        "recall.error_count": 3,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recall_error_count_zero" for a in failed)


def test_assertion_is_frozen():
    a = Assertion(name="x", passed=True, expected="e", actual="a")
    import pytest
    with pytest.raises(Exception):
        a.name = "y"


def test_dispatch_routes_turn_1_to_recall_check():
    engine = AssertionEngine()
    result = _turn_result_stub()
    asserts = engine.dispatch(1, result, pico=MagicMock(), all_results=[result])
    assert len(asserts) == 6
