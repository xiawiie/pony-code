"""Task 23: recall_for_turn — four guards (min_score, max_tokens_per_note,
tombstone, recently-recalled) + provenance in the rendered block."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pico.memory.block_store as block_store_module
from pico.memory.block_store import BlockStore
from pico.memory.recall import recall_for_turn
from pico.memory.retrieval import Retrieval


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _agent(tmp_path):
    ws = tmp_path / "ws"
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    retrieval = Retrieval(store)
    return (
        SimpleNamespace(
            memory_store=store,
            memory_retrieval=retrieval,
            session={"recently_recalled": []},
            model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
            memory=SimpleNamespace(task_summary=""),
        ),
        ws,
    )


def test_recall_returns_recall_block(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        "notes/cache.md",
        "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nParagraph one.\n\nSecond para.\n",
    )
    out = recall_for_turn(a, "how does cache work?", budget_tokens=1000)
    assert out is not None
    assert "<pico:recalled_memory" in out
    assert "score=" in out
    assert "path=" in out
    assert "Paragraph one." in out


def test_recall_min_score_filters(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        "notes/weakly.md",
        "---\nname: weakly\ntype: feedback\ndescription: banana\n---\ntotally unrelated body\n",
    )
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None  # no hit clears min_score


def test_recall_tombstoned_skipped(tmp_path):
    a, ws = _agent(tmp_path)
    _w(ws, "notes/old.md", "---\nname: old\ntype: feedback\ndescription: cache old\n---\nold.\n")
    _w(
        ws,
        "notes/new.md",
        "---\nname: new\ntype: feedback\ndescription: cache new\nsupersedes: [old]\n---\nnew.\n",
    )
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is not None
    assert 'path="workspace/notes/new.md"' in out
    assert 'path="workspace/notes/old.md"' not in out


def test_recall_recently_skipped(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        "notes/cache.md",
        "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nP1\n",
    )
    a.session["recently_recalled"] = [["workspace/notes/cache.md"], []]
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None


def test_recall_updates_recently_recalled(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        "notes/cache.md",
        "---\nname: cache\ntype: feedback\ndescription: cache invariant\n---\nP1\n",
    )
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is not None
    assert a.session["recently_recalled"][-1] == ["workspace/notes/cache.md"]


def test_recall_provenance_fields(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        "notes/cache.md",
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nP1\n",
    )
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert 'type="reference"' in out
    assert 'why="' in out


def test_recall_none_when_no_retrieval(tmp_path):
    a, _ws = _agent(tmp_path)
    a.memory_retrieval = None
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is None


def test_recall_reads_each_candidate_once_and_reuses_link_neighbor_snapshot(
    tmp_path,
    monkeypatch,
):
    ws = tmp_path / "ws"
    _w(
        ws,
        "notes/a-hub.md",
        "---\nname: hub\ntype: feedback\ndescription: cache\n---\n"
        "Hub paragraph. See [[target]].\n",
    )
    _w(
        ws,
        "notes/b-target.md",
        "---\nname: target\ntype: reference\ndescription: unrelated\n---\n"
        "Target paragraph.\n",
    )
    _w(ws, "notes/c-other.md", "unrelated body\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    calls = []
    real_read = block_store_module._read_bounded_regular

    def counting_read(path, limit, *, private=False):
        calls.append(path.name)
        return real_read(path, limit, private=private)

    monkeypatch.setattr(block_store_module, "_read_bounded_regular", counting_read)
    monkeypatch.setattr(store, "list", lambda: pytest.fail("recall reopened list"))
    monkeypatch.setattr(store, "read", lambda _path: pytest.fail("recall reopened file"))
    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )

    out = recall_for_turn(a, "cache", budget_tokens=1000)

    assert out is not None
    assert "Target paragraph." in out
    assert 'type="reference"' in out
    assert calls == ["a-hub.md", "b-target.md", "c-other.md"]


def test_recall_quote_escapes_provenance_attributes(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        'notes/bad" onload="x.md',
        "---\nname: bad\ntype: reference\" injected=\"yes\n"
        'description: cache\n---\nSafe body evil="x.\n',
    )

    out = recall_for_turn(a, 'cache evil="x', budget_tokens=1000)

    assert out is not None
    assert ' onload="x.md"' not in out
    assert ' injected="yes"' not in out
    assert "&quot;" in out
    assert 'why="cache,evil,x"' in out
    assert "Safe body" in out


def test_recall_renders_best_matching_passage_not_first_paragraph(tmp_path):
    a, ws = _agent(tmp_path)
    _w(
        ws,
        "notes/database.md",
        "---\nname: database\ntype: reference\ndescription: database guide\n---\n"
        "This opening paragraph is only a broad introduction.\n\n"
        "Connection pool saturation requires lowering checkout latency.\n",
    )

    out = recall_for_turn(a, "connection pool saturation", budget_tokens=1000)

    assert out is not None
    assert "Connection pool saturation" in out
    assert "broad introduction" not in out
