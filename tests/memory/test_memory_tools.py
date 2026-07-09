from types import SimpleNamespace

import pytest

from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval
from pico.memory.tools import (
    tool_memory_list,
    tool_memory_read,
    tool_memory_save,
    tool_memory_search,
)


def _context(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    return SimpleNamespace(memory_store=store, memory_retrieval=Retrieval(store))


def test_list_empty_returns_hint(tmp_path):
    ctx = _context(tmp_path)
    out = tool_memory_list(ctx, {})
    assert "no memory" in out.lower()


def test_list_shows_files(tmp_path):
    ctx = _context(tmp_path)
    ctx.memory_store.append_agent_note(scope="workspace", note="hi")
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    (ctx.memory_store.workspace_root / "notes" / "auth.md").write_text("# Auth")
    out = tool_memory_list(ctx, {})
    assert "workspace/notes/auth.md" in out
    assert "workspace/agent_notes.md" in out


def test_read_returns_content_with_line_numbers(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    (ctx.memory_store.workspace_root / "notes" / "auth.md").write_text("first\nsecond\nthird\n")
    out = tool_memory_read(ctx, {"path": "workspace/notes/auth.md"})
    assert "1: first" in out or "L1" in out
    assert "second" in out


def test_read_supports_paging(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"line{i}" for i in range(1, 301))
    (ctx.memory_store.workspace_root / "notes" / "big.md").write_text(lines)
    out = tool_memory_read(ctx, {"path": "workspace/notes/big.md", "start": 250, "end": 260})
    assert "line250" in out
    assert "line260" in out
    assert "line200" not in out


def test_read_missing_returns_error(tmp_path):
    ctx = _context(tmp_path)
    out = tool_memory_read(ctx, {"path": "workspace/notes/missing.md"})
    assert "not found" in out.lower() or "error" in out.lower()


def test_search_returns_matches(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    (ctx.memory_store.workspace_root / "notes" / "auth.md").write_text("bcrypt rounds 12\n")
    out = tool_memory_search(ctx, {"query": "bcrypt"})
    assert "auth.md" in out
    assert "bcrypt" in out


def test_search_limits_results(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    for i in range(10):
        (ctx.memory_store.workspace_root / "notes" / f"n{i}.md").write_text("keyword\n")
    out = tool_memory_search(ctx, {"query": "keyword", "limit": 3})
    # Count occurrences of "n" note references
    n_refs = sum(1 for line in out.splitlines() if "notes/n" in line)
    assert n_refs == 3


def test_save_appends_to_workspace_agent_notes(tmp_path):
    ctx = _context(tmp_path)
    out = tool_memory_save(ctx, {"note": "bcrypt rounds > 12 timeout"})
    assert "saved" in out.lower()
    contents = (ctx.memory_store.workspace_root / "agent_notes.md").read_text()
    assert "bcrypt rounds > 12 timeout" in contents


def test_save_accepts_ordinary_token_prose(tmp_path):
    ctx = _context(tmp_path)
    note = "Remember token refresh tests are flaky on CI"

    out = tool_memory_save(ctx, {"note": note})

    assert out.lower().startswith("saved:")
    contents = (ctx.memory_store.workspace_root / "agent_notes.md").read_text()
    assert note in contents


def test_save_rejects_empty(tmp_path):
    ctx = _context(tmp_path)
    out = tool_memory_save(ctx, {"note": ""})
    assert "error" in out.lower()


def test_save_rejects_secret_shaped_note(tmp_path):
    ctx = _context(tmp_path)

    out = tool_memory_save(ctx, {"note": "OPENAI_API_KEY=sk-test-secret-value"})

    assert "error" in out.lower()
    assert "secret" in out.lower()
    assert not (ctx.memory_store.workspace_root / "agent_notes.md").exists()


def test_save_rejects_bare_secret_shaped_note(tmp_path):
    ctx = _context(tmp_path)

    out = tool_memory_save(ctx, {"note": "sk-test-secret-value"})

    assert "error" in out.lower()
    assert "secret" in out.lower()
    assert not (ctx.memory_store.workspace_root / "agent_notes.md").exists()


def test_save_rejects_too_long(tmp_path):
    ctx = _context(tmp_path)
    out = tool_memory_save(ctx, {"note": "x" * 501})
    assert "error" in out.lower()


def test_save_rejects_unknown_scope(tmp_path):
    """validate_tool must reject scope values outside {workspace, user}."""
    from pico.tools import validate_tool

    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        validate_tool(ctx, "memory_save", {"note": "hi", "scope": "hack"})


def test_list_with_prefix(tmp_path):
    ctx = _context(tmp_path)
    ws_root = ctx.memory_store.workspace_root
    (ws_root / "notes").mkdir(parents=True, exist_ok=True)
    (ws_root / "notes" / "auth.md").write_text("a")
    (ws_root / "notes" / "testing.md").write_text("t")
    out = tool_memory_list(ctx, {"prefix": "workspace/notes/auth"})
    assert "auth.md" in out
    assert "testing.md" not in out


def test_tool_registry_includes_new_tools():
    from pico.tools import legal_tool_names
    names = legal_tool_names()
    for expected in ("memory_list", "memory_read", "memory_search", "memory_save", "repo_lookup"):
        assert expected in names, f"missing tool {expected}"


def test_tool_examples_present():
    from pico.tools import tool_example
    for name in ("memory_list", "memory_read", "memory_search", "memory_save", "repo_lookup"):
        assert tool_example(name), f"missing example for {name}"


def test_effect_class_for_memory_tools_distinguishes_reads_and_writes():
    from pico.tool_executor import _EFFECT_CLASS_BY_TOOL

    for name in ("memory_list", "memory_read", "memory_search", "repo_lookup"):
        assert _EFFECT_CLASS_BY_TOOL[name] == "read_only"
    assert _EFFECT_CLASS_BY_TOOL["memory_save"] == "memory_write"
