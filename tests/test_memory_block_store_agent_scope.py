"""Tests for BlockStore agent/ scope support (Task 17).

BlockStore v2 recognizes:
- `notes/**/*.md` (user-written)
- `agent/**/*.md` (agent-owned, per-topic)
- `agent_notes.md` (legacy single-file agent notes)

Files ending in `.legacy` (post-migration renames) are excluded from
`list()`. `MemoryFile` carries a `.frontmatter` dict (empty when the
file has no frontmatter). `write_agent_topic()` creates a per-topic
file with fresh frontmatter or appends body to an existing one.
"""


import pytest

from pico.memory.block_store import BlockStore


def _mk_store(tmp_path):
    return BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")


def test_list_includes_agent_dir(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws" / "agent").mkdir(parents=True)
    (tmp_path / "ws" / "agent" / "topic-a.md").write_text(
        "---\nname: topic-a\ntype: feedback\ndescription: d\n---\nbody\n",
        encoding="utf-8",
    )
    entries = store.list()
    paths = [e.path for e in entries]
    assert "workspace/agent/topic-a.md" in paths


def test_list_skips_legacy_files(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws").mkdir(parents=True)
    (tmp_path / "ws" / "agent_notes.md.legacy").write_text("old", encoding="utf-8")
    (tmp_path / "ws" / "agent_notes.md").write_text("current", encoding="utf-8")
    paths = [e.path for e in store.list()]
    assert "workspace/agent_notes.md.legacy" not in paths
    assert "workspace/agent_notes.md" in paths


def test_memory_file_carries_frontmatter(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws" / "agent").mkdir(parents=True)
    (tmp_path / "ws" / "agent" / "x.md").write_text(
        "---\nname: x\ntype: reference\ndescription: hi\n---\nbody\n",
        encoding="utf-8",
    )
    entries = [e for e in store.list() if e.path == "workspace/agent/x.md"]
    assert entries
    assert entries[0].frontmatter["name"] == "x"
    assert entries[0].frontmatter["type"] == "reference"


def test_memory_file_frontmatter_empty_for_plain_body(tmp_path):
    store = _mk_store(tmp_path)
    (tmp_path / "ws" / "notes").mkdir(parents=True)
    (tmp_path / "ws" / "notes" / "no-fm.md").write_text("# just body\n", encoding="utf-8")
    entries = [e for e in store.list() if e.path == "workspace/notes/no-fm.md"]
    assert entries
    assert entries[0].frontmatter == {}


def test_write_agent_topic_new_file(tmp_path):
    store = _mk_store(tmp_path)
    p = store.write_agent_topic("workspace", "prompt-cache", "first note", note_type="feedback")
    assert p.exists()
    body = p.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "name: prompt-cache" in body
    assert "type: feedback" in body
    assert "first note" in body


def test_write_agent_topic_appends_body_only(tmp_path):
    store = _mk_store(tmp_path)
    p = store.write_agent_topic("workspace", "prompt-cache", "first", note_type="feedback")
    store.write_agent_topic("workspace", "prompt-cache", "second")
    body = p.read_text(encoding="utf-8")
    # Frontmatter written exactly once
    assert body.count("name: prompt-cache") == 1
    assert "first" in body
    assert "second" in body


def test_write_agent_topic_rejects_invalid_topic(tmp_path):
    store = _mk_store(tmp_path)
    with pytest.raises(ValueError):
        store.write_agent_topic("workspace", "../evil", "note")


def test_write_agent_topic_rejects_empty_note(tmp_path):
    store = _mk_store(tmp_path)
    with pytest.raises(ValueError):
        store.write_agent_topic("workspace", "topic", "")


def test_write_agent_topic_rejects_bad_scope(tmp_path):
    store = _mk_store(tmp_path)
    with pytest.raises(ValueError):
        store.write_agent_topic("nope", "topic", "note")
