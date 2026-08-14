import hashlib
import json
from types import SimpleNamespace

import pytest

from pony.memory.block_store import BlockStore
from pony.memory.retrieval import Retrieval
from pony.memory.tools import (
    tool_memory_list,
    tool_memory_read,
    tool_memory_save,
    tool_memory_search,
)
from pony.tools.result_view import ResultPagePolicy


def _context(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    return SimpleNamespace(memory_store=store, memory_retrieval=Retrieval(store))


def _read(context, args):
    return tool_memory_read(
        context,
        args,
        page_policy=ResultPagePolicy(16_384, lambda text: len(text) // 4, str),
    ).content


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
    (ctx.memory_store.workspace_root / "notes" / "auth.md").write_bytes(
        b"first\nsecond\nthird\n"
    )
    out = _read(ctx, {"path": "workspace/notes/auth.md"})
    assert "first\nsecond\nthird\n" in out
    assert "second" in out


def test_read_preserves_crlf_for_body_and_source_hash(tmp_path):
    ctx = _context(tmp_path)
    target = ctx.memory_store.workspace_root / "notes" / "windows.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = "first\r\nsecond\r\n"
    target.write_bytes(source.encode("utf-8"))

    out = _read(ctx, {"path": "workspace/notes/windows.md"})

    assert out.split("\n", 1)[1] == source
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() in out


def test_read_supports_paging(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"line{i}" for i in range(1, 301))
    (ctx.memory_store.workspace_root / "notes" / "big.md").write_text(lines)
    out = _read(ctx, {"path": "workspace/notes/big.md", "start": 250, "end": 260})
    assert "line250" in out
    assert "line260" in out
    assert "line200" not in out


def test_read_runner_pages_an_explicit_large_range(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"line{i}" for i in range(1, 3_001))
    (ctx.memory_store.workspace_root / "notes" / "big.md").write_text(lines)

    out = _read(
        ctx,
        {"path": "workspace/notes/big.md", "start": 250, "end": 2_450},
    )

    assert "line250" in out
    assert "[continuation]" in out


def test_read_accepts_its_exact_continuation_arguments(tmp_path):
    from pony.tools.validation import validate_tool

    ctx = _context(tmp_path)
    target = ctx.memory_store.workspace_root / "notes" / "big.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n" * 3_000, encoding="utf-8")
    first = _read(ctx, {"path": "workspace/notes/big.md"})
    marker = next(
        line for line in first.splitlines() if line.startswith("[continuation] ")
    )
    continuation = json.loads(marker.removeprefix("[continuation] "))

    validate_tool(ctx, "memory_read", continuation)
    second = _read(ctx, continuation)

    assert set(continuation) == {"path", "start", "expected_sha256"}
    assert '"start":2001' in second


def test_read_validator_accepts_an_explicit_large_range(tmp_path):
    from pony.tools.validation import validate_tool

    ctx = _context(tmp_path)
    validate_tool(
        ctx,
        "memory_read",
        {"path": "workspace/notes/big.md", "start": 250, "end": 450},
    )


def test_read_missing_raises(tmp_path):
    ctx = _context(tmp_path)
    with pytest.raises(FileNotFoundError):
        _read(ctx, {"path": "workspace/notes/missing.md"})


def test_search_returns_matches(tmp_path):
    ctx = _context(tmp_path)
    (ctx.memory_store.workspace_root / "notes").mkdir(parents=True, exist_ok=True)
    (ctx.memory_store.workspace_root / "notes" / "auth.md").write_text(
        "bcrypt rounds 12\n"
    )
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
        tool_memory_save(ctx, {"note": "x" * 16_385})


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


@pytest.mark.parametrize("extra", ({"topic": ""}, {"type": ""}))
def test_save_runner_rejects_removed_fields_even_when_empty(tmp_path, extra):
    ctx = _context(tmp_path)

    with pytest.raises(ValueError) as exc_info:
        tool_memory_save(ctx, {"note": "remember this", **extra})

    assert str(exc_info.value) == "memory_save accepts only note and scope"
    assert not (ctx.memory_store.workspace_root / "agent_notes.md").exists()


@pytest.mark.parametrize("extra", ({"topic": ""}, {"type": ""}))
def test_save_validator_rejects_removed_fields_even_when_empty(tmp_path, extra):
    from pony.tools.validation import validate_tool

    ctx = _context(tmp_path)

    with pytest.raises(ValueError) as exc_info:
        validate_tool(ctx, "memory_save", {"note": "remember this", **extra})

    assert str(exc_info.value) == "memory_save accepts only note and scope"


def test_save_rejects_unknown_scope(tmp_path):
    """validate_tool must reject scope values outside {workspace, user}."""
    from pony.tools.validation import validate_tool

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
    from pony.tools.registry import legal_tool_names

    names = legal_tool_names()
    for expected in (
        "memory_list",
        "memory_read",
        "memory_search",
        "memory_save",
        "repo_lookup",
        "read_tool_result",
    ):
        assert expected in names, f"missing tool {expected}"


def test_tool_examples_present():
    from pony.tools.registry import tool_example

    for name in (
        "memory_list",
        "memory_read",
        "memory_search",
        "memory_save",
        "repo_lookup",
        "read_tool_result",
    ):
        assert tool_example(name), f"missing example for {name}"


def test_effect_class_for_new_tools_is_read_only():
    from pony.tools.registry import BASE_TOOL_SPECS

    for name in (
        "memory_list",
        "memory_read",
        "memory_search",
        "repo_lookup",
        "read_tool_result",
    ):
        assert BASE_TOOL_SPECS[name]["effect_class"] == "read_only"
    assert BASE_TOOL_SPECS["memory_save"]["effect_class"] == "memory_write"


@pytest.mark.parametrize(
    ("runner", "args", "message"),
    [
        (tool_memory_list, {}, "memory_store unavailable"),
        (
            tool_memory_read,
            {"path": "workspace/notes/auth.md"},
            "memory_store unavailable",
        ),
        (tool_memory_save, {"note": "remember this"}, "memory_store unavailable"),
        (tool_memory_search, {"query": "cache"}, "memory_retrieval unavailable"),
    ],
)
def test_memory_runners_raise_when_dependencies_are_unavailable(runner, args, message):
    context = SimpleNamespace(memory_store=None, memory_retrieval=None)

    kwargs = {}
    if runner is tool_memory_read:
        kwargs["page_policy"] = ResultPagePolicy(
            16_384,
            lambda text: len(text) // 4,
            str,
        )
    with pytest.raises(RuntimeError, match=message):
        runner(context, args, **kwargs)


def test_memory_read_propagates_io_error(tmp_path, monkeypatch):
    ctx = _context(tmp_path)

    def fail_read(path):
        raise OSError("memory disk failed")

    monkeypatch.setattr(ctx.memory_store, "read_verbatim", fail_read)
    with pytest.raises(OSError, match="memory disk failed"):
        _read(ctx, {"path": "workspace/notes/auth.md"})
