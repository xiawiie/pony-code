"""Task B4-B6: pico.toml overrides for memory subsystem."""

import pytest

from pico.config import memory_recall_config


def test_recall_config_defaults(tmp_path):
    cfg = memory_recall_config(tmp_path)
    assert cfg == {
        "min_score": pytest.approx(0.3),
        "top_k": 2,
        "max_tokens_per_note": 400,
        "skip_recent_turns": 2,
    }


def test_recall_config_partial_override(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.recall]\nmin_score = 0.5\ntop_k = 4\n", encoding="utf-8"
    )
    cfg = memory_recall_config(tmp_path)
    assert cfg["min_score"] == pytest.approx(0.5)
    assert cfg["top_k"] == 4
    # Un-overridden keys still take defaults.
    assert cfg["max_tokens_per_note"] == 400
    assert cfg["skip_recent_turns"] == 2


def test_recall_for_turn_reads_min_score_from_agent(tmp_path):
    """recall_for_turn should filter using agent.context_config['recall']['min_score']."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.memory.block_store import BlockStore
    from pico.memory.recall import recall_for_turn
    from pico.memory.retrieval import Retrieval

    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nP1\n",
        encoding="utf-8",
    )

    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)

    # Scores are normalized per-query against max_score, so a single hit
    # always has norm_score = 1.0. Pick a threshold > 1.0 to prove the
    # config knob is actually being read (module default 0.3 is unreachable
    # so we couldn't distinguish "config read" from "default used" otherwise).
    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={"recall": {"min_score": 1.5, "top_k": 2, "max_tokens_per_note": 400, "skip_recent_turns": 2}},
    )
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None  # gate closed by config-provided min_score
