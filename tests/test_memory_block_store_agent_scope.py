"""Single-write BlockStore scope contract."""

import os

import pytest

from pico.memory.block_store import BlockStore


def _mk_store(tmp_path):
    return BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")


def test_obsolete_agent_tree_is_ignored_without_reading_canary(tmp_path, monkeypatch):
    agent_dir = tmp_path / "ws" / "agent"
    agent_dir.mkdir(parents=True)
    canary = agent_dir / "topic.md"
    canary.write_text("obsolete-agent-canary", encoding="utf-8")
    original_lstat = os.lstat

    def guarded_lstat(path, *args, **kwargs):
        candidate = os.path.abspath(os.fspath(path))
        if os.path.commonpath((candidate, agent_dir)) == os.fspath(agent_dir):
            raise AssertionError("obsolete agent tree was inspected")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", guarded_lstat)

    assert _mk_store(tmp_path).list() == []


def test_list_includes_only_notes_and_agent_notes(tmp_path):
    notes = tmp_path / "ws" / "notes"
    notes.mkdir(parents=True)
    (notes / "user.md").write_text("user note", encoding="utf-8")
    (tmp_path / "ws" / "agent_notes.md").write_text("agent note", encoding="utf-8")
    (tmp_path / "ws" / "other.md").write_text("ignored", encoding="utf-8")

    paths = {entry.path for entry in _mk_store(tmp_path).list()}

    assert paths == {"workspace/notes/user.md", "workspace/agent_notes.md"}


def test_notes_agent_subdirectory_remains_user_note_with_frontmatter(tmp_path):
    note = tmp_path / "ws" / "notes" / "agent" / "topic.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\nname: topic\ntype: reference\ndescription: hi\n---\nbody\n",
        encoding="utf-8",
    )

    entries = [
        entry
        for entry in _mk_store(tmp_path).list()
        if entry.path == "workspace/notes/agent/topic.md"
    ]

    assert len(entries) == 1
    assert entries[0].frontmatter["type"] == "reference"


def test_read_rejects_obsolete_and_out_of_model_paths(tmp_path):
    store = _mk_store(tmp_path)

    for path in (
        "workspace/agent/topic.md",
        "workspace/other.md",
        "workspace/notes/not-markdown.txt",
    ):
        with pytest.raises(ValueError, match="invalid memory path"):
            store.read(path)


def test_topic_writer_is_deleted(tmp_path):
    store = _mk_store(tmp_path)

    assert not hasattr(store, "write_agent_topic")
