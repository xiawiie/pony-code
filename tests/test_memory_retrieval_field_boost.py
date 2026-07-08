"""Task 18: Retrieval BM25 field boost — hits in frontmatter fields
(name/description/tags/aliases) weigh more than hits in body."""

from pathlib import Path

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_hit_in_description_ranks_above_hit_in_body(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws,
        "agent/a.md",
        "---\nname: a\ntype: feedback\ndescription: cache invariant note\n---\nunrelated body content\n",
    )
    _write(
        ws,
        "agent/b.md",
        "---\nname: b\ntype: feedback\ndescription: nothing here\n---\nsomething cache mentioned in body\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    assert hits[0].path == "workspace/agent/a.md"


def test_hit_in_name_ranks_highest(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws,
        "agent/mycache.md",
        "---\nname: mycache\ntype: feedback\ndescription: irrelevant\n---\nirrelevant body\n",
    )
    _write(
        ws,
        "agent/other.md",
        "---\nname: other\ntype: feedback\ndescription: mycache reference\n---\nsomething mycache in body\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("mycache")
    assert hits[0].path == "workspace/agent/mycache.md"


def test_hit_in_tags_boosts_score(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws,
        "agent/tagged.md",
        "---\nname: tagged\ntype: feedback\ndescription: general\ntags: [auth, cache]\n---\nno keyword in body\n",
    )
    _write(
        ws,
        "agent/plain.md",
        "---\nname: plain\ntype: feedback\ndescription: general\n---\nauth appears exactly once in body\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("auth")
    assert hits[0].path == "workspace/agent/tagged.md"


def test_body_only_hit_still_scored(tmp_path):
    ws = tmp_path / "ws"
    _write(
        ws,
        "agent/body-only.md",
        "---\nname: body-only\ntype: feedback\ndescription: unrelated\n---\ncache mentioned only in body\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    assert hits
    assert hits[0].path == "workspace/agent/body-only.md"
