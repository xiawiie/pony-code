"""Task 21: memory_save(topic=...) tool routes to per-topic agent files.

- With ``topic``: writes ``agent/<topic>.md`` (frontmatter on first write, body-append after).
- Without ``topic``: falls back to legacy ``agent_notes.md`` append.
- Invalid topic slug is rejected by validation; an unvalidated runner raises.
"""

from types import SimpleNamespace

import pytest

from pico.memory.block_store import BlockStore
from pico.memory.tools import tool_memory_save
from pico.tools import validate_tool


def test_save_with_topic_creates_agent_file(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    result = tool_memory_save(ctx, {"note": "hello world", "topic": "greeting"})
    assert "saved" in result
    target = tmp_path / "ws" / "agent" / "greeting.md"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "name: greeting" in body


def test_save_without_topic_uses_legacy_agent_notes(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    tool_memory_save(ctx, {"note": "hello"})
    legacy = tmp_path / "ws" / "agent_notes.md"
    assert legacy.exists()
    assert "hello" in legacy.read_text(encoding="utf-8")


def test_save_topic_invalid_raises_from_unvalidated_runner(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    with pytest.raises(ValueError, match="invalid topic"):
        tool_memory_save(ctx, {"note": "x", "topic": "../evil"})


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"note": "x", "topic": "../evil"}, "invalid topic"),
        ({"note": "x", "type": " "}, "type must not be empty"),
    ],
)
def test_save_topic_validation_rejects_predictable_inputs(tmp_path, arguments, message):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)

    with pytest.raises(ValueError, match=message):
        validate_tool(ctx, "memory_save", arguments)


def test_save_topic_second_write_appends_body(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    tool_memory_save(ctx, {"note": "first note", "topic": "same"})
    tool_memory_save(ctx, {"note": "second note", "topic": "same"})
    body = (tmp_path / "ws" / "agent" / "same.md").read_text(encoding="utf-8")
    assert body.count("name: same") == 1  # frontmatter written exactly once
    assert "first note" in body
    assert "second note" in body


def test_save_topic_custom_type(tmp_path):
    store = BlockStore(workspace_root=tmp_path / "ws", user_root=tmp_path / "user")
    ctx = SimpleNamespace(memory_store=store)
    tool_memory_save(ctx, {"note": "x", "topic": "typed", "type": "reference"})
    body = (tmp_path / "ws" / "agent" / "typed.md").read_text(encoding="utf-8")
    assert "type: reference" in body
