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


from pathlib import Path


def _turn_2_result_stub(**overrides):
    """Session state includes a tool_result message with digest applied."""
    defaults = dict(
        turn=2,
        user_prompt="读一下 pico/runtime.py",
        expected_behavior="digest_applied",
        final_answer="ok",
        metadata={},
        session_message_count_before=2,
        session_message_count_after=6,
        provider_call_count_this_turn=2,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=6,
        current_user_content="",
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def _pico_stub_with_digested_message(raw_body: str, raw_dir: Path, source_hash: str = "abc12345"):
    """Build a MagicMock pico whose session has a digested tool_result at the tail."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f"{source_hash}.txt"
    raw_file.write_text(raw_body, encoding="utf-8")

    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1",
                             "content": f"[digest] runtime.py (900 lines)\n- import\n(raw at {raw_file})"}],
                "_pico_meta": {"digest_applied": True, "source_hash": source_hash, "tool_use_id": "t1"},
            },
        ]
    }
    return pico, raw_file


def test_check_turn_2_digest_passes_on_valid_state(tmp_path):
    engine = AssertionEngine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs" / "tool_results")
    result = _turn_2_result_stub()
    asserts = engine.check_turn_2_digest(result, pico)
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_2_digest_fails_when_no_digest_applied(tmp_path):
    engine = AssertionEngine()
    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "raw output"}],
             "_pico_meta": {"digest_applied": False, "tool_use_id": "t1"}},
        ]
    }
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "digest_applied_flag_true" for a in failed)


def test_check_turn_2_digest_verifies_raw_file_exists(tmp_path):
    engine = AssertionEngine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs" / "tool_results")
    raw_file.unlink()  # remove the raw file → check should fail
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "raw_file_exists_on_disk" for a in failed)


def _turn_3_result_stub(**overrides):
    defaults = dict(
        turn=3,
        user_prompt="再看一下",
        expected_behavior="injection_dropped",
        final_answer="ok",
        metadata={
            "injection_budget": 500,
            "injection_dropped": ["checkpoint", "project_structure"],
            "injection_tokens": {
                "workspace_state": 100,
                "memory_index": 50,
                "project_structure": 0,
                "recalled_memory": 200,
                "checkpoint": 0,
            },
        },
        session_message_count_before=6,
        session_message_count_after=8,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=8,
        current_user_content="",
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_3_injection_drop_passes_when_checkpoint_dropped():
    engine = AssertionEngine()
    asserts = engine.check_turn_3_injection_drop(_turn_3_result_stub())
    assert len(asserts) == 4
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_3_injection_drop_accepts_checkpoint_zero_tokens():
    """Assertion 14 accepts either dropped OR zero-tokens-so-never-rendered."""
    engine = AssertionEngine()
    result = _turn_3_result_stub(metadata={
        "injection_budget": 500,
        "injection_dropped": ["project_structure"],  # checkpoint NOT dropped
        "injection_tokens": {
            "workspace_state": 100, "memory_index": 50,
            "project_structure": 0, "recalled_memory": 200,
            "checkpoint": 0,  # zero tokens — never rendered — should still pass
        },
    })
    asserts = engine.check_turn_3_injection_drop(result)
    failed = [a for a in asserts if not a.passed]
    assert not any(a.name == "checkpoint_dropped_or_zero_tokens" for a in failed)


def test_check_turn_3_injection_drop_fails_when_recalled_memory_dropped():
    engine = AssertionEngine()
    result = _turn_3_result_stub(metadata={
        "injection_budget": 500,
        "injection_dropped": ["checkpoint", "project_structure", "recalled_memory"],
        "injection_tokens": {"recalled_memory": 0, "checkpoint": 0},
    })
    asserts = engine.check_turn_3_injection_drop(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_not_dropped" for a in failed)
