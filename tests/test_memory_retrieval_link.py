"""Task 19: Retrieval link expansion — after top-k BM25 hits, scan
their bodies for `[[name]]` wiki-style links and pull neighboring
notes into the result set with decayed score.

Constraints: max_added=3 per query, decay=0.4, depth=1 (no recursion).
"""


from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_link_expansion_adds_neighbor(tmp_path):
    ws = tmp_path / "ws"
    _w(
        ws,
        "notes/a.md",
        "---\nname: a\ntype: feedback\ndescription: about cache\n---\nsee [[b]] for related\n",
    )
    _w(
        ws,
        "notes/b.md",
        "---\nname: b\ntype: feedback\ndescription: unrelated\n---\nnothing about cache here\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/notes/a.md" in paths
    assert "workspace/notes/b.md" in paths  # pulled in via one-hop link


def test_link_expansion_capped_at_three(tmp_path):
    ws = tmp_path / "ws"
    body_links = "\n".join([f"see [[n{i}]]" for i in range(10)])
    _w(
        ws,
        "notes/hub.md",
        f"---\nname: hub\ntype: feedback\ndescription: cache hub\n---\n{body_links}\n",
    )
    for i in range(10):
        _w(
            ws,
            f"notes/n{i}.md",
            f"---\nname: n{i}\ntype: feedback\ndescription: none\n---\ncontent\n",
        )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache", limit=20)
    expanded = [h for h in hits if h.path != "workspace/notes/hub.md"]
    assert len(expanded) <= 3


def test_link_expansion_does_not_recurse(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "notes/a.md", "---\nname: a\ntype: feedback\ndescription: about cache\n---\nsee [[b]]\n")
    _w(ws, "notes/b.md", "---\nname: b\ntype: feedback\ndescription: unrelated\n---\nsee [[c]]\n")
    _w(ws, "notes/c.md", "---\nname: c\ntype: feedback\ndescription: unrelated too\n---\nno links\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/notes/b.md" in paths
    assert "workspace/notes/c.md" not in paths  # depth cap = 1


def test_link_expansion_score_decays(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "notes/a.md", "---\nname: a\ntype: feedback\ndescription: about cache cache cache\n---\nsee [[b]]\n")
    _w(ws, "notes/b.md", "---\nname: b\ntype: feedback\ndescription: unrelated\n---\nnothing\n")
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    a_hit = next(h for h in hits if h.path == "workspace/notes/a.md")
    b_hit = next(h for h in hits if h.path == "workspace/notes/b.md")
    # b came in via link expansion with decay=0.4
    assert b_hit.score < a_hit.score
    assert b_hit.score == a_hit.score * 0.4 or abs(b_hit.score - a_hit.score * 0.4) < 1e-9


def test_next_query_sees_link_change(tmp_path):
    ws = tmp_path / "ws"
    hub = ws / "notes" / "hub.md"
    _w(
        ws,
        "notes/hub.md",
        "---\nname: hub\ndescription: cache\n---\nsee [[a]]\n",
    )
    _w(ws, "notes/a.md", "---\nname: a\ndescription: alpha\n---\na\n")
    _w(ws, "notes/b.md", "---\nname: b\ndescription: beta\n---\nb\n")
    retrieval = Retrieval(
        BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    )

    assert [hit.path for hit in retrieval.search("cache", limit=1)] == [
        "workspace/notes/hub.md",
        "workspace/notes/a.md",
    ]

    hub.write_text(
        "---\nname: hub\ndescription: cache\n---\nsee [[b]]\n",
        encoding="utf-8",
    )

    assert [hit.path for hit in retrieval.search("cache", limit=1)] == [
        "workspace/notes/hub.md",
        "workspace/notes/b.md",
    ]


def test_duplicate_link_name_keeps_sorted_last_wins_ordering(tmp_path):
    ws = tmp_path / "ws"
    _w(
        ws,
        "notes/hub.md",
        "---\nname: hub\ndescription: cache\n---\nsee [[duplicate]]\n",
    )
    _w(
        ws,
        "notes/a-first.md",
        "---\nname: duplicate\ndescription: first\n---\nfirst\n",
    )
    _w(
        ws,
        "notes/z-last.md",
        "---\nname: duplicate\ndescription: last\n---\nlast\n",
    )

    paths = [
        hit.path
        for hit in Retrieval(
            BlockStore(workspace_root=ws, user_root=tmp_path / "user")
        ).search("cache", limit=1)
    ]

    assert paths == [
        "workspace/notes/hub.md",
        "workspace/notes/z-last.md",
    ]
