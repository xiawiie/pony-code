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


def test_bad_type_falls_back_to_default(tmp_path):
    (tmp_path / "pico.toml").write_text(
        '[context]\nhistory_soft_cap = "not-an-int"\n', encoding="utf-8"
    )
    # Fallback rather than raise.
    assert context_history_soft_cap(tmp_path) == 40000


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
