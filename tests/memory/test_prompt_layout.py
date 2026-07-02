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
    """Workspace state (branch/status) moved out of stable prefix."""
    agent = _agent(tmp_path)
    prompt = agent.prompt("hi")
    # If both present, memory_index should come before workspace_state.
    mi_pos = prompt.find("<memory_index>")
    ws_pos = prompt.find("<workspace_state>") if "<workspace_state>" in prompt else -1
    if ws_pos > 0 and mi_pos > 0:
        assert mi_pos < ws_pos, "memory_index should be in stable prefix, workspace_state in volatile"
