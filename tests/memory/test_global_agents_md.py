"""~/.pony/AGENTS.md 作为全局约定的加载与在 stable prefix 中的可见性.

关键点:
- 存在时应作为 project_docs 中的一项进入 stable_text
- 缺失时不影响其它 project_docs
- 只出现在 stable prefix, 不出现在 volatile 部分
"""

from pathlib import Path


def _stubbed_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


def test_global_agents_md_appears_in_stable_text(tmp_path, monkeypatch):
    home = _stubbed_home(tmp_path, monkeypatch)
    (home / ".pony").mkdir()
    (home / ".pony" / "AGENTS.md").write_text("# Global\n\n- prefer uv\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Repo\n")

    from pony.workspace.context import WorkspaceContext

    ws = WorkspaceContext.build(str(repo))
    text = ws.stable_text()

    assert "<global>/AGENTS.md" in text
    assert "prefer uv" in text
    # 保证工作区自己那份也仍在
    assert "AGENTS.md" in text


def test_missing_global_agents_md_is_silent(tmp_path, monkeypatch):
    _stubbed_home(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Repo\n")

    from pony.workspace.context import WorkspaceContext

    ws = WorkspaceContext.build(str(repo))
    text = ws.stable_text()

    assert "<global>/AGENTS.md" not in text
    assert "AGENTS.md" in text  # workspace 自己那份仍在


def test_global_agents_md_not_in_volatile_text(tmp_path, monkeypatch):
    home = _stubbed_home(tmp_path, monkeypatch)
    (home / ".pony").mkdir()
    (home / ".pony" / "AGENTS.md").write_text("# Global\n")

    repo = tmp_path / "repo"
    repo.mkdir()

    from pony.workspace.context import WorkspaceContext

    ws = WorkspaceContext.build(str(repo))
    assert "AGENTS.md" not in ws.volatile_text()
    assert "<global>" not in ws.volatile_text()
