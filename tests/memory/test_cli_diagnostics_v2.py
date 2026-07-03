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
