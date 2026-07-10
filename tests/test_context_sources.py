"""Tests for pico.context.sources — per-source renderers that produce
pre-escaping raw text or None for each injection source."""

from unittest.mock import MagicMock

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context.sources import (
    render_checkpoint,
    render_memory_index,
    render_project_structure,
    render_workspace_state,
)
from pico.features.memory import set_file_summary_dict


def _agent():
    a = MagicMock()
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(
        return_value="<workspace_state>\n- branch: main\n</workspace_state>"
    )
    a.memory_store = MagicMock()
    file_entry = MagicMock(path="workspace/notes/a.md", size_chars=100, first_line="# A")
    a.memory_store.list = MagicMock(return_value=[file_entry])
    a.repo_map = MagicMock()
    a.repo_map.refresh_if_stale = MagicMock()
    a.repo_map.top_level_tree = MagicMock(return_value=[{"path": "pico", "file_count": 30}])
    a.repo_map.language_stats = MagicMock(return_value={"python": 30})
    a.render_checkpoint_text = MagicMock(return_value="")
    return a


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_workspace_state_returns_content():
    out = render_workspace_state(_agent(), budget_tokens=500)
    assert out is not None
    assert "branch: main" in out


def test_workspace_state_returns_none_when_empty():
    a = _agent()
    a.workspace.volatile_text.return_value = ""
    assert render_workspace_state(a, budget_tokens=500) is None


def test_workspace_state_returns_none_on_exception():
    a = _agent()
    a.workspace.volatile_text.side_effect = RuntimeError("boom")
    assert render_workspace_state(a, budget_tokens=500) is None


def test_memory_index_lists_entries():
    out = render_memory_index(_agent(), budget_tokens=500)
    assert out is not None
    assert "workspace/notes/a.md" in out


def test_memory_index_returns_none_when_no_store():
    a = MagicMock()
    a.memory_store = None
    assert render_memory_index(a, budget_tokens=500) is None


def test_memory_index_returns_none_when_no_entries():
    a = _agent()
    a.memory_store.list.return_value = []
    assert render_memory_index(a, budget_tokens=500) is None


def test_memory_index_renders_recent_summary_without_durable_entries(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.remember_file("README.md")
    agent._sync_working_memory()
    set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        "README.md",
        "project entry point",
        workspace_root=agent.root,
    )

    text = render_memory_index(agent, budget_tokens=200)

    assert "Recent working file summaries:" in text
    assert "README.md -> project entry point" in text


def test_memory_index_omits_working_summaries_when_memory_is_off(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.remember_file("README.md")
    agent._sync_working_memory()
    set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        "README.md",
        "project entry point",
        workspace_root=agent.root,
    )
    agent.feature_flags["memory"] = False

    text = render_memory_index(agent, budget_tokens=200)

    assert text is None or "Recent working file summaries:" not in text


def test_memory_index_reserves_working_summary_when_durable_index_overflows(tmp_path):
    agent = build_agent(tmp_path)
    agent.memory.remember_file("README.md")
    agent._sync_working_memory()
    set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        "README.md",
        "project entry point",
        workspace_root=agent.root,
    )
    agent.memory_store = MagicMock()
    agent.memory_store.list.return_value = [
        MagicMock(path=f"notes/{index}.md", size_chars=1000, first_line="x" * 80)
        for index in range(30)
    ]

    text = render_memory_index(agent, budget_tokens=60)

    assert "Recent working file summaries:" in text
    assert "README.md -> project entry point" in text


def test_project_structure_shows_tree():
    out = render_project_structure(_agent(), budget_tokens=500)
    assert out is not None
    assert "pico" in out


def test_project_structure_returns_none_when_no_repo_map():
    a = MagicMock()
    a.repo_map = None
    assert render_project_structure(a, budget_tokens=500) is None


def test_project_structure_returns_none_when_empty_tree():
    a = _agent()
    a.repo_map.top_level_tree.return_value = []
    assert render_project_structure(a, budget_tokens=500) is None


def test_checkpoint_none_when_empty():
    assert render_checkpoint(_agent(), budget_tokens=500) is None


def test_checkpoint_returns_text_when_present():
    a = _agent()
    a.render_checkpoint_text.return_value = "Task checkpoint:\nNext step: continue"
    out = render_checkpoint(a, budget_tokens=500)
    assert out is not None
    assert "Task checkpoint:" in out


def test_source_respects_budget_via_tail_clip():
    a = _agent()
    long_state = "\n".join([f"- commit {i}: xxxx" for i in range(200)])
    a.workspace.volatile_text.return_value = long_state
    # budget 100 token ≈ 400 char, output should be truncated
    out = render_workspace_state(a, budget_tokens=100)
    assert len(out) <= 400 + 20  # small tolerance for the ellipsis
