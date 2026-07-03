"""Task 10 · REPL /save 和 /memory-review 命令.

/save 把一条 note 追加到 workspace 的 agent_notes.md.
/memory-review 打印 agent_notes.md 内容与编辑提示.
"""

import pytest


def _build_agent(tmp_path):
    from pico.runtime import Pico, SessionStore
    from pico.workspace import WorkspaceContext
    from pico.providers.clients import FakeModelClient

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_repl_save_command_appends_agent_note(tmp_path, monkeypatch, capsys):
    from pico.cli_commands import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/save bcrypt rounds > 12 timeout", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    agent_notes = tmp_path / ".pico" / "memory" / "agent_notes.md"
    assert agent_notes.exists()
    assert "bcrypt rounds > 12 timeout" in agent_notes.read_text(encoding="utf-8")


def test_repl_save_without_body_shows_usage(tmp_path, monkeypatch, capsys):
    from pico.cli_commands import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/save    ", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    agent_notes = tmp_path / ".pico" / "memory" / "agent_notes.md"
    assert not agent_notes.exists() or "usage:" not in agent_notes.read_text(encoding="utf-8")


def test_repl_memory_review_shows_agent_notes(tmp_path, monkeypatch, capsys):
    from pico.cli_commands import run_repl

    memory = tmp_path / ".pico" / "memory"
    memory.mkdir(parents=True)
    (memory / "agent_notes.md").write_text("- old note\n")

    agent = _build_agent(tmp_path)
    inputs = iter(["/memory-review", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "old note" in out


def test_repl_memory_review_when_empty(tmp_path, monkeypatch, capsys):
    from pico.cli_commands import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/memory-review", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "no agent_notes" in out.lower() or "empty" in out.lower()


def test_repl_memory_after_save_shows_memory_file(tmp_path, monkeypatch, capsys):
    """`/save` 后 `/memory` 必须能看到刚存进 agent_notes.md 的文件.

    Locks the DX contract: the three memory surfaces refer to the same store.
    """
    from pico.cli_commands import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/save something worth remembering", "/memory", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    # working-memory 仪表盘头
    assert "Working memory:" in out or "working memory:" in out.lower()
    # v2 侧的文件列表以及新写入的文件
    assert "Memory files:" in out
    assert "workspace/agent_notes.md" in out
