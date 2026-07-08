"""Task B7: a single pico.toml overriding every wired key end-to-end."""

import pytest

from pico.config import (
    context_digest_size_threshold,
    context_history_floor_messages,
    context_history_soft_cap,
    context_injection_budget_ratio,
    context_system_tools_hard_cap,
    memory_field_boosts,
    memory_link_config,
    memory_recall_config,
)


PICO_TOML = """
[context]
history_soft_cap = 12345
history_floor_messages = 8
injection_budget_ratio = 0.25
system_tools_hard_cap = 30000

[context.digest]
size_threshold_chars = 500

[memory.recall]
min_score = 0.45
top_k = 3
max_tokens_per_note = 300
skip_recent_turns = 4

[memory.retrieval.field_boost]
name = 6.0
description = 3.5
tags = 4.5
aliases = 4.0
body = 1.5

[memory.retrieval.link]
max_added = 5
decay = 0.5
"""


def test_full_pico_toml_overrides_take_effect(tmp_path):
    (tmp_path / "pico.toml").write_text(PICO_TOML, encoding="utf-8")

    assert context_history_soft_cap(tmp_path) == 12345
    assert context_history_floor_messages(tmp_path) == 8
    assert context_injection_budget_ratio(tmp_path) == pytest.approx(0.25)
    assert context_system_tools_hard_cap(tmp_path) == 30000
    assert context_digest_size_threshold(tmp_path) == 500

    recall = memory_recall_config(tmp_path)
    assert recall == {
        "min_score": pytest.approx(0.45),
        "top_k": 3,
        "max_tokens_per_note": 300,
        "skip_recent_turns": 4,
    }

    fb = memory_field_boosts(tmp_path)
    assert fb == {
        "name": 6.0,
        "description": 3.5,
        "tags": 4.5,
        "aliases": 4.0,
        "body": 1.5,
    }

    assert memory_link_config(tmp_path) == (5, 0.5)


def test_partial_pico_toml_only_overrides_provided_keys(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.recall]\nmin_score = 0.7\n", encoding="utf-8"
    )
    # Only min_score changed; everything else stays default.
    assert context_history_soft_cap(tmp_path) == 40000  # default
    recall = memory_recall_config(tmp_path)
    assert recall["min_score"] == pytest.approx(0.7)
    assert recall["top_k"] == 2  # default
