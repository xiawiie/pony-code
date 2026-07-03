from pathlib import Path

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def _agent(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Test project\nUse pytest.\n")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_prompt_contains_memory_index(tmp_path):
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text("# Auth notes\n")
    agent = _agent(tmp_path)
    prompt = agent.prompt("say hi")
    assert "<memory_index>" in prompt
    assert "workspace/notes/auth.md" in prompt
    assert "memory_save" in prompt.lower()
    assert "memory_read" in prompt.lower() or "memory_search" in prompt.lower()


def test_prompt_contains_project_structure(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("class Foo: pass\n")
    agent = _agent(tmp_path)
    prompt = agent.prompt("hi")
    assert "<project_structure" in prompt
    assert "src" in prompt


def test_prompt_contains_memory_guidance(tmp_path):
    agent = _agent(tmp_path)
    prompt = agent.prompt("hi")
    assert "memory_save" in prompt.lower()  # from guidance segment
    assert "memory_read" in prompt.lower() or "memory_search" in prompt.lower()


def test_workspace_state_appears_after_memory_index(tmp_path):
    """Workspace state (branch/status) moved out of stable prefix to volatile section."""
    agent = _agent(tmp_path)
    prompt, metadata = agent._build_prompt_and_metadata("hi")
    # Both segments MUST be present.
    mi_pos = prompt.find("<memory_index>")
    ws_pos = prompt.find("<workspace_state>")
    transcript_pos = prompt.find("Transcript:")
    assert mi_pos > 0, "memory_index missing from stable prefix"
    assert ws_pos > 0, "workspace_state missing from volatile section"
    assert transcript_pos > 0, "Transcript missing from history section"
    # memory_index (stable prefix) must come BEFORE workspace_state (volatile section).
    assert mi_pos < ws_pos, "memory_index should precede workspace_state (volatile ordering)"
    assert ws_pos < transcript_pos, "workspace_state should precede transcript"
    assert "Working memory:" not in prompt
    assert "Relevant memory:" not in prompt
    assert metadata["section_order"] == ["prefix", "history", "current_request"]


def test_stable_prefix_no_branch_content(tmp_path):
    """Stable prefix (agent.prefix) must not embed branch/status/recent_commits.

    Otherwise branch changes would break stable prefix cache byte-identity.
    """
    agent = _agent(tmp_path)
    # agent.prefix is the stable prefix built by build_prompt_prefix()
    # It should NOT contain branch/status/recent_commits (workspace's volatile parts).
    assert "- branch:" not in agent.prefix, \
        "stable prefix leaks git branch line (should live in volatile)"
    assert "recent_commits" not in agent.prefix, \
        "stable prefix leaks recent_commits (should live in volatile)"
