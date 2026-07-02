"""验证 Pico 运行时把 BlockStore / Retrieval / RepoMap wire 到 ToolContext。"""

from pathlib import Path

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def _isolate_home(monkeypatch, tmp_path):
    """让 Path.home() 指向 tmp_path/home, 隔离用户本机 ~/.pico/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    return fake_home


def test_pico_has_memory_store_and_repo_map(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").write_text("# project\n")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    assert agent.memory_store is not None
    assert agent.memory_retrieval is not None
    assert agent.repo_map is not None


def test_tool_context_has_wiring(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    ctx = agent.tool_context()
    assert ctx.memory_store is agent.memory_store
    assert ctx.memory_retrieval is agent.memory_retrieval
    assert ctx.repo_map is agent.repo_map
