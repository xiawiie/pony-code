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


def test_read_missing_raises(tmp_path):
    ctx = _context(tmp_path)
    with pytest.raises(FileNotFoundError):
        tool_memory_read(ctx, {"path": "workspace/notes/missing.md"})


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


def test_save_empty_raises(tmp_path):
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="note must not be empty"):
        tool_memory_save(ctx, {"note": ""})


def test_save_too_long_raises(tmp_path):
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="note exceeds"):
        tool_memory_save(ctx, {"note": "x" * 501})


def test_save_rejects_secret_content_but_allows_security_prose(tmp_path):
    ctx = _context(tmp_path)
    secret = "github_pat_A123456789012345678901234567890"

    with pytest.raises(ValueError, match="sensitive_content"):
        tool_memory_save(ctx, {"note": secret})

    result = tool_memory_save(ctx, {"note": "password policy"})
    assert result.startswith("saved:")
    assert "password policy" in (
        ctx.memory_store.workspace_root / "agent_notes.md"
    ).read_text(encoding="utf-8")


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


def test_effect_class_for_new_tools_is_read_only():
    from pico.tool_executor import _EFFECT_CLASS_BY_TOOL
    for name in ("memory_list", "memory_read", "memory_search", "repo_lookup"):
        assert _EFFECT_CLASS_BY_TOOL[name] == "read_only"
    assert _EFFECT_CLASS_BY_TOOL["memory_save"] == "memory_write"


@pytest.mark.parametrize(
    ("runner", "args", "message"),
    [
        (tool_memory_list, {}, "memory_store unavailable"),
        (tool_memory_read, {"path": "workspace/notes/auth.md"}, "memory_store unavailable"),
        (tool_memory_save, {"note": "remember this"}, "memory_store unavailable"),
        (tool_memory_search, {"query": "cache"}, "memory_retrieval unavailable"),
    ],
)
def test_memory_runners_raise_when_dependencies_are_unavailable(runner, args, message):
    context = SimpleNamespace(memory_store=None, memory_retrieval=None)

    with pytest.raises(RuntimeError, match=message):
        runner(context, args)


def test_memory_read_propagates_io_error(tmp_path, monkeypatch):
    ctx = _context(tmp_path)

    def fail_read(path):
        raise OSError("memory disk failed")

    monkeypatch.setattr(ctx.memory_store, "read", fail_read)
    with pytest.raises(OSError, match="memory disk failed"):
        tool_memory_read(ctx, {"path": "workspace/notes/auth.md"})
