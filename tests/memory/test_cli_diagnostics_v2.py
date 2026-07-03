"""Task 10 · doctor 检测 CLAUDE.md-without-AGENTS.md.

只提示一次，不干扰其余诊断.
"""

from types import SimpleNamespace


def test_doctor_flags_claude_md_without_agents_md(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    from pico.cli_diagnostics import collect_doctor

    args = SimpleNamespace(cwd=str(tmp_path))
    result = collect_doctor(str(tmp_path), args, offline=True)

    project_docs = result.get("project_docs") or {}
    hints = project_docs.get("hints") or []
    text_dump = " ".join(hint.get("message", "") for hint in hints)
    assert "CLAUDE.md" in text_dump
    assert "AGENTS.md" in text_dump


def test_doctor_no_claude_hint_when_agents_md_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agents\n")
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    from pico.cli_diagnostics import collect_doctor

    args = SimpleNamespace(cwd=str(tmp_path))
    result = collect_doctor(str(tmp_path), args, offline=True)

    project_docs = result.get("project_docs") or {}
    hints = project_docs.get("hints") or []
    for hint in hints:
        assert "CLAUDE.md" not in hint.get("message", "")


def test_doctor_no_hint_when_neither_file_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    from pico.cli_diagnostics import collect_doctor

    args = SimpleNamespace(cwd=str(tmp_path))
    result = collect_doctor(str(tmp_path), args, offline=True)

    project_docs = result.get("project_docs") or {}
    hints = project_docs.get("hints") or []
    assert hints == []


def test_doctor_text_output_shows_claude_md_hint(tmp_path, monkeypatch, capsys):
    """`pico-cli doctor --offline` 的默认 text 输出必须包含 CLAUDE.md hint."""
    from pico.cli_commands import handle_doctor

    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    args = SimpleNamespace(format="text", cwd=str(tmp_path))
    rc = handle_doctor(["--offline"], str(tmp_path), args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "CLAUDE.md" in out
    assert "AGENTS.md" in out
