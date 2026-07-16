import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pico.tools.context import ToolContext
from pico.memory.repo_map import tool_repo_lookup
from pico.tools import (
    DEFAULT_RUN_SHELL_TIMEOUT,
    build_tool_registry,
    tool_delegate,
    tool_read_file,
    tool_search,
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


def test_run_shell_registry_runner_is_not_a_raw_command_bypass(tmp_path):
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: Path(tmp_path / raw_path),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
    )
    runner = build_tool_registry(context)["run_shell"]["run"]

    with pytest.raises(ValueError, match="approved execution plan"):
        runner({"command": "echo ok", "timeout": DEFAULT_RUN_SHELL_TIMEOUT})


def test_search_rg_return_codes_are_truthful(tmp_path, monkeypatch):
    results = iter([
        subprocess.CompletedProcess([], 0, stdout="sample.txt\0" "1:hit\n", stderr=""),
        subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        subprocess.CompletedProcess([], 2, stdout="", stderr="regex parse error\n"),
    ])
    calls = []

    def fake_rg(executable, args, **kwargs):
        calls.append((executable, list(args), kwargs))
        return next(results)

    monkeypatch.setattr("pico.tools.run_hardened_rg", fake_rg, raising=False)
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={"rg": "/frozen/rg"},
    )

    assert tool_search(context, {"pattern": "hit"}) == "sample.txt:1:hit"
    assert tool_search(context, {"pattern": "missing"}) == "(no matches)"
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        tool_search(context, {"pattern": "["})
    assert exc_info.value.returncode == 2
    assert all(call[0] == "/frozen/rg" for call in calls)
    assert all(call[1][-2] == "--" for call in calls)
    assert all("--with-filename" in call[1] for call in calls)
    assert all("--null" in call[1] for call in calls)
    assert all("--glob-case-insensitive" in call[1] for call in calls)


def test_search_without_frozen_rg_never_rescans_path(tmp_path, monkeypatch):
    (tmp_path / "sample.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(
        "shutil.which",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime PATH rescan")
        ),
    )
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={},
    )

    result = tool_search(context, {"pattern": "needle", "path": "."})

    assert "sample.txt:1:needle" in result


def test_search_passes_option_shaped_pattern_as_literal(tmp_path, monkeypatch):
    captured = {}

    def fake_rg(executable, args, **kwargs):
        captured["executable"] = executable
        captured["args"] = list(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr("pico.tools.run_hardened_rg", fake_rg, raising=False)
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={"rg": "/frozen/rg"},
    )

    assert tool_search(context, {"pattern": "--config=attack", "path": "."}) == "(no matches)"
    assert captured["executable"] == "/frozen/rg"
    assert captured["args"][-4:] == ["-e", "--config=attack", "--", str(tmp_path)]


def test_search_filters_every_rg_result_path_defensively(tmp_path, monkeypatch):
    def fake_rg(executable, args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "safe.py\0" "1:needle\n"
                ".env.local\0" "1:needle\n"
                "malformed-without-null\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("pico.tools.run_hardened_rg", fake_rg)
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={"rg": "/frozen/rg"},
    )

    result = tool_search(context, {"pattern": "needle", "path": "."})

    assert result == "safe.py:1:needle"


def test_rg_search_runs_allowed_env_template_through_frozen_rg(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env.example").write_text("needle\n", encoding="utf-8")
    calls = []

    def fake_rg(executable, args, **kwargs):
        calls.append(list(args))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=".env.example\0" "1:needle\n",
            stderr="",
        )

    monkeypatch.setattr("pico.tools.run_hardened_rg", fake_rg)
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={"rg": "/frozen/rg"},
    )

    result = tool_search(context, {"pattern": "needle", "path": "."})

    assert result == ".env.example:1:needle"
    assert len(calls) == 2
    assert calls[1][-2:] == [
        "--",
        str(tmp_path / ".env.example"),
    ]
    assert "--smart-case" in calls[1]
    assert "--glob" not in calls[1]


def test_python_search_never_stats_or_reads_sensitive_or_symlink_files(
    tmp_path,
    monkeypatch,
):
    sensitive = tmp_path / "credentials.json"
    sensitive.write_text("needle secret\n", encoding="utf-8")
    safe = tmp_path / "safe.txt"
    safe.write_text("needle safe\n", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(safe)
    real_stat = Path.stat
    real_read_text = Path.read_text

    def guarded_stat(self, *args, **kwargs):
        if self == sensitive:
            raise AssertionError("unsafe file stat")
        if self == linked and kwargs.get("follow_symlinks", True):
            raise AssertionError("symlink-following stat")
        return real_stat(self, *args, **kwargs)

    def guarded_read_text(self, *args, **kwargs):
        if self in {sensitive, linked}:
            raise AssertionError("unsafe file read")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    context = ToolContext(
        root=tmp_path,
        path_resolver=lambda raw_path: (tmp_path / raw_path).resolve(),
        shell_env_provider=lambda: {"PWD": str(tmp_path)},
        depth=0,
        max_depth=1,
        spawn_delegate=lambda args: "unused",
        trusted_executables={},
    )

    result = tool_search(context, {"pattern": "needle", "path": "."})

    assert result == "safe.txt:1:needle safe"


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
