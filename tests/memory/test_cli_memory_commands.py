"""CLI `pico-cli memory {list, show, search, review, migrate}` 命令测试.

用 `handle_memory` 直接调用（in-process），避免 subprocess 慢和环境依赖.
"""

from pathlib import Path
from types import SimpleNamespace


def _args(cwd, fmt="text"):
    return SimpleNamespace(format=fmt, cwd=str(cwd))


def test_memory_list_empty(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = handle_memory(["list"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no memory" in out.lower() or "empty" in out.lower()


def test_memory_list_shows_workspace_files(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("# Auth\n")
    rc = handle_memory(["list"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth.md" in out


def test_memory_list_json_format(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("# Auth\n")
    rc = handle_memory(["list"], str(tmp_path), _args(tmp_path, fmt="json"))
    assert rc == 0
    out = capsys.readouterr().out
    import json
    payload = json.loads(out)
    assert payload["ok"] is True
    paths = {entry["path"] for entry in payload["data"]}
    assert "workspace/notes/auth.md" in paths


def test_memory_show(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("hello\nworld\n")
    rc = handle_memory(["show", "workspace/notes/auth.md"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello" in out
    assert "world" in out


def test_memory_show_missing(tmp_path):
    from pico.cli_commands import handle_memory
    from pico.cli_errors import CliError
    import pytest
    with pytest.raises(CliError):
        handle_memory(["show", "workspace/notes/nope.md"], str(tmp_path), _args(tmp_path))


def test_memory_search(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("bcrypt info\n")
    rc = handle_memory(["search", "bcrypt"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth.md" in out


def test_memory_search_no_match(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("hello\n")
    rc = handle_memory(["search", "nonexistent"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no match" in out.lower() or "0" in out


def test_memory_search_limit(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    notes = tmp_path / ".pico" / "memory" / "notes"
    notes.mkdir(parents=True)
    for i in range(5):
        (notes / f"n{i}.md").write_text(f"keyword content {i}\n")
    rc = handle_memory(["search", "keyword", "--limit", "2"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    hits = sum(1 for line in out.splitlines() if "notes/n" in line)
    assert hits == 2


def test_memory_review_empty(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = handle_memory(["review"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no agent_notes" in out.lower() or "empty" in out.lower()


def test_memory_review_shows_content(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    memory_dir = tmp_path / ".pico" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "agent_notes.md").write_text("- 2026-07-03  bcrypt rounds > 12 timeout\n")
    rc = handle_memory(["review"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "bcrypt rounds > 12" in out


def test_memory_migrate_preview(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    topics = tmp_path / ".pico" / "memory" / "topics"
    topics.mkdir(parents=True)
    (topics / "project-conventions.md").write_text("- use uv\n")
    rc = handle_memory(["migrate"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "preview" in out.lower() or "would" in out.lower()
    # preview 不应实际创建目标文件
    assert not (tmp_path / ".pico" / "memory" / "notes" / "project-conventions.md").exists()


def test_memory_migrate_apply(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    topics = tmp_path / ".pico" / "memory" / "topics"
    topics.mkdir(parents=True)
    (topics / "project-conventions.md").write_text("- use uv\n")
    (topics / "dependency-facts.md").write_text("- Python 3.10+\n")
    rc = handle_memory(["migrate", "--apply"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    notes = tmp_path / ".pico" / "memory" / "notes"
    assert (notes / "project-conventions.md").exists()
    assert (notes / "dependency-facts.md").exists()
    # 原文件加 .deprecated 后缀
    assert (topics / "project-conventions.md.deprecated").exists()
    # 原文件已重命名, 不再存在于原路径
    assert not (topics / "project-conventions.md").exists()


def test_memory_migrate_no_legacy(tmp_path, capsys):
    from pico.cli_commands import handle_memory
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = handle_memory(["migrate"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no legacy" in out.lower() or "no topics" in out.lower() or "nothing" in out.lower()


def test_memory_unknown_subcommand(tmp_path):
    from pico.cli_commands import handle_memory
    from pico.cli_errors import CliError
    import pytest
    with pytest.raises(CliError):
        handle_memory(["bogus"], str(tmp_path), _args(tmp_path))


def test_memory_top_level_command_dispatch(tmp_path, capsys, monkeypatch):
    """`pico-cli memory list` via main() dispatch."""
    from pico.cli import main
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = main(["memory", "list"])
    assert rc == 0


def test_known_top_level_commands_includes_memory():
    from pico.cli_parser import KNOWN_TOP_LEVEL_COMMANDS
    assert "memory" in KNOWN_TOP_LEVEL_COMMANDS
