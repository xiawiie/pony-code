"""Task B2: pico.toml overrides context settings via helper functions."""

import pytest

from pico.config import (
    context_history_floor_messages,
    context_history_soft_cap,
    context_injection_budget_ratio,
    context_system_tools_hard_cap,
)


def test_history_soft_cap_default(tmp_path):
    assert context_history_soft_cap(tmp_path) == 40000


def test_history_soft_cap_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 12345\n", encoding="utf-8"
    )
    assert context_history_soft_cap(tmp_path) == 12345


def test_history_floor_default(tmp_path):
    assert context_history_floor_messages(tmp_path) == 6


def test_history_floor_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_floor_messages = 10\n", encoding="utf-8"
    )
    assert context_history_floor_messages(tmp_path) == 10


def test_injection_budget_ratio_default(tmp_path):
    assert context_injection_budget_ratio(tmp_path) == pytest.approx(0.15)


def test_injection_budget_ratio_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\ninjection_budget_ratio = 0.25\n", encoding="utf-8"
    )
    assert context_injection_budget_ratio(tmp_path) == pytest.approx(0.25)


def test_system_tools_hard_cap_default(tmp_path):
    assert context_system_tools_hard_cap(tmp_path) == 20000


def test_system_tools_hard_cap_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nsystem_tools_hard_cap = 30000\n", encoding="utf-8"
    )
    assert context_system_tools_hard_cap(tmp_path) == 30000


def test_total_budget_hard_cap_default(tmp_path):
    from pico.config import context_total_budget_hard_cap
    assert context_total_budget_hard_cap(tmp_path) == 100000


def test_total_budget_hard_cap_override(tmp_path):
    from pico.config import context_total_budget_hard_cap
    (tmp_path / "pico.toml").write_text(
        "[context]\ntotal_budget_hard_cap = 50000\n", encoding="utf-8"
    )
    assert context_total_budget_hard_cap(tmp_path) == 50000


def test_bad_type_falls_back_to_default(tmp_path):
    (tmp_path / "pico.toml").write_text(
        '[context]\nhistory_soft_cap = "not-an-int"\n', encoding="utf-8"
    )
    # Fallback rather than raise.
    assert context_history_soft_cap(tmp_path) == 40000


def test_digest_size_threshold_default(tmp_path):
    from pico.config import context_digest_size_threshold
    assert context_digest_size_threshold(tmp_path) == 1200


def test_digest_size_threshold_override(tmp_path):
    from pico.config import context_digest_size_threshold
    (tmp_path / "pico.toml").write_text(
        "[context.digest]\nsize_threshold_chars = 500\n", encoding="utf-8"
    )
    assert context_digest_size_threshold(tmp_path) == 500


def test_append_tool_result_uses_config_threshold(tmp_path):
    """Overriding digest.size_threshold_chars via context_config makes small
    tool results digest even though they'd otherwise be inlined."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.agent_loop import _append_tool_result

    session_messages = []
    a = MagicMock()
    a.session = {"messages": session_messages, "id": "s1"}
    a.record_message = MagicMock(side_effect=lambda m: session_messages.append(m))
    a.workspace = MagicMock()
    a.workspace.repo_root = str(tmp_path)
    a.current_task_state = SimpleNamespace(run_id="r1", task_id="t1")
    a.current_run_dir = tmp_path / ".pico" / "runs" / "r1"
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    # Force a small threshold so a 100-char payload triggers digest.
    a.context_config = {"digest_size_threshold": 50}

    _append_tool_result(
        a,
        tool_use_id="t1",
        content="x" * 100,  # over threshold=50
        tool_name="read_file",
        tool_args={"path": "a.py"},
    )
    msg = session_messages[-1]
    assert msg["_pico_meta"]["digest_applied"] is True


def test_build_v2_reads_system_tools_hard_cap_from_pico_toml(tmp_path):
    """Overriding system_tools_hard_cap in pico.toml raises SystemTooBig sooner."""
    from unittest.mock import MagicMock

    from pico.context_manager import ContextManager

    (tmp_path / "pico.toml").write_text(
        "[context]\nsystem_tools_hard_cap = 100\n", encoding="utf-8"
    )

    a = MagicMock()
    a.prefix = "x" * 500  # ~125 tokens with /4 fallback -> over 100 cap
    a.tools = {}
    a.session = {"messages": []}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {"system_tools_hard_cap": 100}

    cm = ContextManager(a)
    with pytest.raises(RuntimeError, match="SystemTooBig"):
        cm.build_v2("hi")
