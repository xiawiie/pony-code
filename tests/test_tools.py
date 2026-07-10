from pathlib import Path
from types import SimpleNamespace

import pytest

from pico.tool_context import ToolContext
from pico.repo_map import tool_repo_lookup
from pico.tools import (
    DEFAULT_RUN_SHELL_TIMEOUT,
    build_tool_registry,
    tool_delegate,
    tool_read_file,
    tool_run_shell,
    validate_tool,
)


def test_tool_context_supports_file_tools_without_full_pico(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    result = tool_read_file(context, {"path": "sample.txt", "start": 1, "end": 1})

    assert "# sample.txt" in result
    assert "alpha" in result


def test_delegate_uses_context_spawn_without_runtime_import(tmp_path):
    calls = []
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: calls.append(args) or "delegate_result:\nDone",
    )

    result = tool_delegate(context, {"task": "inspect README.md", "max_steps": 2})

    assert result == "delegate_result:\nDone"
    assert calls == [{"task": "inspect README.md", "max_steps": 2}]


def test_build_tool_registry_binds_runners_to_tool_context(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=1,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    tools = build_tool_registry(context)

    assert "read_file" in tools
    assert "delegate" not in tools


def test_run_shell_uses_larger_default_timeout(tmp_path, monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    tool_run_shell(context, {"command": "echo ok"})

    assert captured["timeout"] == DEFAULT_RUN_SHELL_TIMEOUT == 60


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("write_file", {"path": ".pico/memory/notes/secret.md", "content": "no"}),
        (
            "patch_file",
            {"path": ".pico/memory/notes/secret.md", "old_text": "a", "new_text": "b"},
        ),
    ],
)
def test_validate_tool_rejects_protected_user_notes_before_runner(tmp_path, name, arguments):
    protected = tmp_path / ".pico" / "memory" / "notes" / "secret.md"
    protected.parent.mkdir(parents=True)
    protected.write_text("a", encoding="utf-8")
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )

    with pytest.raises(ValueError, match="refusing to write user note path"):
        validate_tool(context, name, arguments)


def test_repo_lookup_raises_when_repo_map_is_unavailable():
    with pytest.raises(RuntimeError, match="repo_map unavailable"):
        tool_repo_lookup(SimpleNamespace(repo_map=None), {"symbol": "Thing"})
