"""REPL /remember (/save alias) and /memory-review commands.

/save 把一条 note 追加到 workspace 的 agent_notes.md.
/memory-review 打印 agent_notes.md 内容与编辑提示.
"""

import re


def _build_agent(tmp_path):
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext
    from pico.providers.fake import FakeModelClient

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(["done"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_repl_save_command_appends_agent_note(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/save bcrypt rounds > 12 timeout", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    agent_notes = tmp_path / ".pico" / "memory" / "agent_notes.md"
    assert agent_notes.exists()
    assert "bcrypt rounds > 12 timeout" in agent_notes.read_text(encoding="utf-8")


def test_repl_remember_is_the_explicit_primary_memory_command(
    tmp_path,
    monkeypatch,
):
    from pico.cli_start import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/remember keep the recovery invariant", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    run_repl(agent)

    notes = tmp_path / ".pico" / "memory" / "agent_notes.md"
    assert "keep the recovery invariant" in notes.read_text(encoding="utf-8")


def test_repl_remember_enforces_1024_model_token_limit(
    tmp_path,
    monkeypatch,
    capsys,
):
    from pico.cli_start import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/remember " + ("记忆" * 600), "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    run_repl(agent)

    assert "1024 model tokens" in capsys.readouterr().out
    notes = tmp_path / ".pico" / "memory" / "agent_notes.md"
    assert not notes.exists()


def test_repl_save_without_body_shows_usage(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/save    ", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    agent_notes = tmp_path / ".pico" / "memory" / "agent_notes.md"
    assert not agent_notes.exists() or "usage:" not in agent_notes.read_text(encoding="utf-8")


def test_repl_save_rejects_secret_and_keeps_security_prose(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

    secret = "github_pat_A123456789012345678901234567890"
    agent = _build_agent(tmp_path)
    inputs = iter([f"/save {secret}", "/save password policy", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    run_repl(agent)

    output = capsys.readouterr().out
    contents = (
        tmp_path / ".pico" / "memory" / "agent_notes.md"
    ).read_text(encoding="utf-8")
    assert "sensitive_content" in output
    assert secret not in output
    assert secret not in contents
    assert "password policy" in contents


def test_repl_memory_review_shows_agent_notes(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

    memory = tmp_path / ".pico" / "memory"
    memory.mkdir(parents=True)
    (memory / "agent_notes.md").write_text("- old note\n")

    agent = _build_agent(tmp_path)
    inputs = iter(["/memory-review", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "old note" in out


def test_repl_memory_review_uses_block_store_not_direct_path_read(
    tmp_path,
    monkeypatch,
    capsys,
):
    from pathlib import Path

    from pico.cli_start import run_repl

    memory = tmp_path / ".pico" / "memory"
    memory.mkdir(parents=True)
    notes = memory / "agent_notes.md"
    notes.write_text("- anchored note\n", encoding="utf-8")
    real_read_text = Path.read_text

    def reject_direct_read(self, *args, **kwargs):
        if self == notes:
            raise AssertionError("REPL reopened Agent Notes directly")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_direct_read)
    agent = _build_agent(tmp_path)
    inputs = iter(["/memory-review", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))

    run_repl(agent)

    assert "anchored note" in capsys.readouterr().out


def test_repl_memory_review_when_empty(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

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
    from pico.cli_start import run_repl

    agent = _build_agent(tmp_path)
    inputs = iter(["/save something worth remembering", "/memory", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "task:" in out
    assert "recent:" in out
    assert "Memory files:" in out
    assert "workspace/agent_notes.md" in out
    assert re.search(r"workspace/agent_notes\.md \(\d+ chars\)", out)
    assert "Working memory:" not in out
    assert "<working_memory" not in out
    assert "</working_memory>" not in out


def test_repl_memory_shows_working_memory_summary(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

    sample = tmp_path / "sample.txt"
    sample.write_text("sample\n", encoding="utf-8")

    agent = _build_agent(tmp_path)
    agent.memory.set_task_summary("Task")
    agent.memory.remember_file("sample.txt")
    agent._sync_working_memory()

    inputs = iter(["/memory", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "task: Task" in out
    assert "recent: sample.txt" in out


def test_repl_memory_does_not_call_memory_text(tmp_path, monkeypatch, capsys):
    from pico.cli_start import run_repl

    agent = _build_agent(tmp_path)

    def fail_memory_text():
        raise AssertionError("memory_text should not be called")

    monkeypatch.setattr(agent, "memory_text", fail_memory_text)
    inputs = iter(["/memory", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    run_repl(agent)

    out = capsys.readouterr().out
    assert "task:" in out
    assert "recent:" in out
