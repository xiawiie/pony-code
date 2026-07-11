"""Task 20: tombstone filter — frontmatter `supersedes: [name, ...]`
declarations remove the listed notes from retrieval + link expansion.
The disk files stay untouched; retrieval simply skips them."""

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_superseded_note_excluded(tmp_path):
    ws = tmp_path / "ws"
    _w(
        ws,
        "notes/old.md",
        "---\nname: old\ntype: feedback\ndescription: cache old\n---\nold body\n",
    )
    _w(
        ws,
        "notes/new.md",
        "---\nname: new\ntype: feedback\ndescription: cache new\nsupersedes: [old]\n---\nnew body\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    assert "workspace/notes/old.md" not in paths
    assert "workspace/notes/new.md" in paths


def test_link_expansion_skips_tombstoned(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "notes/a.md", "---\nname: a\ntype: feedback\ndescription: cache\n---\nsee [[old]]\n")
    _w(ws, "notes/old.md", "---\nname: old\ntype: feedback\ndescription: ancient\n---\nold\n")
    _w(
        ws,
        "notes/new.md",
        "---\nname: new\ntype: feedback\ndescription: replaces old\nsupersedes: [old]\n---\nnew\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    hits = ret.search("cache")
    paths = [h.path for h in hits]
    # Even though a.md references [[old]], the tombstoned neighbor is skipped.
    assert "workspace/notes/old.md" not in paths


def test_tombstoned_note_disk_file_preserved(tmp_path):
    ws = tmp_path / "ws"
    _w(ws, "notes/old.md", "---\nname: old\ntype: feedback\ndescription: cache old\n---\nold\n")
    _w(
        ws,
        "notes/new.md",
        "---\nname: new\ntype: feedback\ndescription: cache new\nsupersedes: [old]\n---\nnew\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    _hits = ret.search("cache")
    # Tombstone hides from retrieval but does NOT delete the file.
    assert (ws / "notes" / "old.md").exists()
