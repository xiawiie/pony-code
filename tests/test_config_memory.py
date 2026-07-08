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


def test_field_boosts_defaults(tmp_path):
    from pico.config import memory_field_boosts
    fb = memory_field_boosts(tmp_path)
    assert fb == {
        "name": 5.0,
        "description": 3.0,
        "tags": 4.0,
        "aliases": 4.0,
        "body": 1.0,
    }


def test_field_boosts_override(tmp_path):
    from pico.config import memory_field_boosts
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.field_boost]\nname = 8.0\ndescription = 2.0\n",
        encoding="utf-8",
    )
    fb = memory_field_boosts(tmp_path)
    assert fb["name"] == 8.0
    assert fb["description"] == 2.0
    # Un-overridden keys retain defaults.
    assert fb["tags"] == 4.0


def test_link_config_defaults(tmp_path):
    from pico.config import memory_link_config
    assert memory_link_config(tmp_path) == (3, 0.4)


def test_link_config_override(tmp_path):
    from pico.config import memory_link_config
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.link]\nmax_added = 5\ndecay = 0.6\n", encoding="utf-8"
    )
    assert memory_link_config(tmp_path) == (5, 0.6)


def test_retrieval_uses_field_boosts_from_config(tmp_path):
    """A note where 'cache' appears only in body loses to a note where it appears
    only in description when field_boosts default; if we push body up above
    description, the body-hit note should win."""
    from pico.memory.block_store import BlockStore
    from pico.memory.retrieval import Retrieval

    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "in_desc.md").write_text(
        "---\nname: in_desc\ntype: feedback\ndescription: cache mention\n---\nother body\n",
        encoding="utf-8",
    )
    (ws / "agent" / "in_body.md").write_text(
        "---\nname: in_body\ntype: feedback\ndescription: unrelated\n---\ncache appears here\n",
        encoding="utf-8",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    # Push body way up above description → in_body wins.
    ret = Retrieval(store, config={
        "field_boosts": {"name": 5.0, "description": 1.0, "tags": 4.0, "aliases": 4.0, "body": 10.0},
        "link_config": (3, 0.4),
    })
    hits = ret.search("cache")
    assert hits[0].path == "workspace/agent/in_body.md"
