"""Task 23: recall_for_turn — four guards (min_score, max_tokens_per_note,
tombstone, recently-recalled) + provenance in the rendered block."""

from types import SimpleNamespace
from unittest.mock import MagicMock

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


def test_recall_uses_single_store_scan(tmp_path, monkeypatch):
    """Task D2: recall_for_turn calls store.list() at most once per call,
    not once per hit."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.memory.block_store import BlockStore
    from pico.memory.recall import recall_for_turn
    from pico.memory.retrieval import Retrieval

    ws = tmp_path / "ws"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "a.md").write_text(
        "---\nname: a\ntype: feedback\ndescription: cache one\n---\np1\n", encoding="utf-8"
    )
    (ws / "notes" / "b.md").write_text(
        "---\nname: b\ntype: feedback\ndescription: cache two\n---\np2\n", encoding="utf-8"
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)

    call_count = {"n": 0}
    original_list = store.list
    def counting_list(*args, **kwargs):
        call_count["n"] += 1
        return original_list(*args, **kwargs)
    monkeypatch.setattr(store, "list", counting_list)

    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )
    # Reset counter to isolate recall's contribution.
    call_count["n"] = 0
    out = recall_for_turn(a, "cache", budget_tokens=1000)
    assert out is not None
    # recall_for_turn should scan the store at most once for the type index.
    # Retrieval.search internally scans 4 times (_load_docs +
    # _superseded_names + _name_to_path_index — an existing quirk, out of
    # scope for this task). Recall's own contribution is exactly 1 (the
    # store_index build). Pre-fix impl called store.list() once per picked
    # hit (top_k=2), yielding 4+2=6 calls. Post-fix: 4+1=5.
    assert call_count["n"] <= 5, f"store.list() called {call_count['n']} times"
