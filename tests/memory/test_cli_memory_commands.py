"""CLI memory list/show/search/review command tests."""

from types import SimpleNamespace

import pytest


def _args(cwd, fmt="text"):
    return SimpleNamespace(format=fmt, cwd=str(cwd))


def test_memory_list_empty(tmp_path, capsys):
    from pico.cli.memory import handle_memory
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = handle_memory(["list"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no memory" in out.lower() or "empty" in out.lower()


def test_memory_list_shows_workspace_files(tmp_path, capsys):
    from pico.cli.memory import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("# Auth\n")
    rc = handle_memory(["list"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth.md" in out


def test_memory_list_json_format(tmp_path, capsys):
    from pico.cli.memory import handle_memory
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
    from pico.cli.memory import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("hello\nworld\n")
    rc = handle_memory(["show", "workspace/notes/auth.md"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello" in out
    assert "world" in out


def test_memory_show_missing(tmp_path):
    from pico.cli.memory import handle_memory
    from pico.cli.errors import CliError
    import pytest
    with pytest.raises(CliError):
        handle_memory(["show", "workspace/notes/nope.md"], str(tmp_path), _args(tmp_path))


def test_memory_search(tmp_path, capsys):
    from pico.cli.memory import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("bcrypt info\n")
    rc = handle_memory(["search", "bcrypt"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth.md" in out


def test_memory_search_no_match(tmp_path, capsys):
    from pico.cli.memory import handle_memory
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("hello\n")
    rc = handle_memory(["search", "nonexistent"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no match" in out.lower() or "0" in out


def test_memory_search_limit(tmp_path, capsys):
    from pico.cli.memory import handle_memory
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
    from pico.cli.memory import handle_memory
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = handle_memory(["review"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no agent_notes" in out.lower() or "empty" in out.lower()


def test_memory_review_shows_content(tmp_path, capsys):
    from pico.cli.memory import handle_memory
    memory_dir = tmp_path / ".pico" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "agent_notes.md").write_text("- 2026-07-03  bcrypt rounds > 12 timeout\n")
    rc = handle_memory(["review"], str(tmp_path), _args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "bcrypt rounds > 12" in out


@pytest.mark.parametrize("output_format", ("text", "json"))
def test_memory_review_rejects_symlink_without_reading_canary(
    tmp_path,
    capsys,
    output_format,
):
    from pico.cli import main
    from pico.cli.errors import CLI_EXIT_CONFIG

    canary = "memory-review-outside-canary"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-agent-notes"
    outside.write_text(canary, encoding="utf-8")
    memory_root = tmp_path / ".pico" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "agent_notes.md").symlink_to(outside)

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        output_format,
        "memory",
        "review",
    ])

    captured = capsys.readouterr()
    assert code == CLI_EXIT_CONFIG
    assert "agent notes could not be read safely" in captured.out + captured.err
    assert canary not in captured.out + captured.err
    assert str(outside) not in captured.out + captured.err
    if output_format == "json":
        import json

        payload = json.loads(captured.out)
        assert payload["error"]["code"] == "memory_unavailable"


@pytest.mark.parametrize("tokens", (["migrate"], ["migrate", "--apply"]))
def test_memory_migrate_is_not_a_command(tmp_path, tokens):
    from pico.cli.memory import handle_memory
    from pico.cli.errors import CliError

    with pytest.raises(CliError) as raised:
        handle_memory(tokens, str(tmp_path), _args(tmp_path))

    assert raised.value.code == "usage"
    assert "migrate" not in raised.value.message


def test_memory_unknown_subcommand(tmp_path):
    from pico.cli.memory import handle_memory
    from pico.cli.errors import CliError
    with pytest.raises(CliError):
        handle_memory(["bogus"], str(tmp_path), _args(tmp_path))


def test_memory_top_level_command_dispatch(tmp_path, capsys, monkeypatch):
    """`pico memory list` via main() dispatch."""
    from pico.cli import main
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pico" / "memory").mkdir(parents=True)
    rc = main(["memory", "list"])
    assert rc == 0


def test_known_top_level_commands_includes_memory():
    from pico.cli.parser import KNOWN_TOP_LEVEL_COMMANDS
    assert "memory" in KNOWN_TOP_LEVEL_COMMANDS
