"""Dynamic Context Source candidate generation and shared Memory snapshots."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.context.renderer import render_current_user_message
from pony.context.sources import (
    build_source_chunks,
    memory_index_chunks,
    project_structure_chunks,
    recalled_memory_chunks,
    recovery_state_chunks,
    task_working_set_chunks,
    workspace_state_chunks,
)
from pony.agent.model_capabilities import TokenAccounting
from benchmarks.support.fake_provider import FakeModelClient
from pony.security.redaction import SensitiveDataBlockedError
from pony.runtime.options import RuntimeOptions


def _agent():
    accounting = TokenAccounting()
    workspace = MagicMock()
    workspace.logical_root = ""
    workspace.repo_root = "/repo"
    workspace.cwd = "/repo"
    workspace.default_branch = "main"
    workspace.project_docs = {}
    workspace.volatile_text.return_value = "- branch: main\n- status: clean"
    return SimpleNamespace(
        workspace=workspace,
        repo_map=MagicMock(),
        memory=SimpleNamespace(task_summary="", recent_files=[]),
        session={"memory": {"file_summaries": {}}},
        resume_state={},
        sandbox_session=None,
        render_checkpoint_text=lambda: "",
        token_accounting=accounting,
        model_client=MagicMock(),
    )


def _real_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )


def test_workspace_state_produces_ranked_whole_chunks():
    agent = _agent()

    chunks = workspace_state_chunks(agent, agent.token_accounting)

    assert chunks
    assert chunks[0].source == "workspace_state"
    assert chunks[0].key == "workspace-identity"
    assert "repo_root: /repo" in chunks[0].text
    assert "branch: main" in chunks[1].text


def test_workspace_state_returns_empty_on_failure():
    agent = _agent()
    agent.workspace.volatile_text.side_effect = RuntimeError("boom")

    assert workspace_state_chunks(agent, agent.token_accounting) == []


def test_recall_source_security_failure_is_not_treated_as_retrieval_miss(
    monkeypatch,
):
    agent = _agent()
    monkeypatch.setattr(
        "pony.context.sources.recall_candidates",
        MagicMock(side_effect=SensitiveDataBlockedError("blocked recall")),
    )

    with pytest.raises(SensitiveDataBlockedError, match="blocked recall"):
        recalled_memory_chunks(
            agent,
            agent.token_accounting,
            "query",
            MagicMock(),
        )


def test_project_structure_filters_sensitive_paths():
    agent = _agent()
    agent.repo_map.top_level_tree.return_value = [
        {"path": ".ssh", "file_count": 2},
        {"path": "src", "file_count": 3},
    ]
    agent.repo_map.language_stats.return_value = {"python": 3}

    chunks = project_structure_chunks(agent, agent.token_accounting)
    text = "\n".join(chunk.text for chunk in chunks)

    assert ".ssh" not in text
    assert "src" in text
    assert "python=3" in text


def test_project_structure_empty_without_repo_map():
    agent = _agent()
    agent.repo_map = None

    assert project_structure_chunks(agent, agent.token_accounting) == []


def test_readme_is_dynamic_project_context_not_a_pinned_instruction(tmp_path):
    (tmp_path / "README.md").write_text("dynamic project overview\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Pinned project rule.\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )

    chunks = project_structure_chunks(agent, agent.token_accounting)
    rendered = "\n".join(chunk.text for chunk in chunks)

    assert "dynamic project overview" in rendered
    assert "Pinned project rule" not in rendered
    assert "Pinned project rule" in agent.prefix
    assert "dynamic project overview" not in agent.prefix


def test_task_working_set_contains_goal_files_and_required_checkpoint():
    agent = _agent()
    agent.memory.task_summary = "finish context allocation"
    agent.memory.recent_files = ["pony/context/renderer.py"]
    agent.session["memory"]["file_summaries"] = {
        "pony/context/renderer.py": "allocator entry point"
    }
    agent.session["checkpoints"] = {
        "current_id": "ckpt-context",
        "items": {
            "ckpt-context": {
                "checkpoint_id": "ckpt-context",
                "goal": "finish context allocation",
                "status": "in_progress",
                "next_steps": ["test"],
                "key_files": [
                    {
                        "path": "pony/context/renderer.py",
                        "summary": "allocator entry point",
                    }
                ],
            }
        },
    }

    chunks = task_working_set_chunks(agent, agent.token_accounting)
    text = "\n".join(chunk.text for chunk in chunks)

    assert chunks[0].required is True
    assert "finish context allocation" in text
    assert "allocator entry point" in text
    assert "Checkpoint: ckpt-context" in text
    assert "Next steps: test" in text


def test_task_working_set_contains_only_checkpoint_facts():
    agent = _agent()
    agent.session.update(
        checkpoints={
            "current_id": "ckpt-context",
            "items": {
                "ckpt-context": {
                    "goal": "Old checkpoint goal",
                    "status": "in_progress",
                    "blocker": "none",
                    "next_steps": ["continue"],
                }
            },
        },
    )

    chunks = task_working_set_chunks(agent, agent.token_accounting)

    checkpoint = next(chunk for chunk in chunks if chunk.key == "checkpoint-state")
    assert checkpoint.key == "checkpoint-state"
    assert checkpoint.required is True
    assert "Old checkpoint goal" in checkpoint.text
    assert all(chunk.key != "workflow-state" for chunk in chunks)
    assert all(not chunk.key.startswith("plan-pending-") for chunk in chunks)


def test_required_recovery_and_checkpoint_survive_budget_pressure():
    agent = _agent()
    agent.resume_state = {
        "status": "workspace-mismatch",
        "runtime_identity_mismatch_fields": ["cwd"],
    }
    agent.session.update(
        checkpoints={
            "current_id": "ckpt-budget",
            "items": {"ckpt-budget": {"goal": "Checkpoint fact"}},
        },
    )
    chunks = build_source_chunks(agent, "continue")
    required_tokens = sum(chunk.tokens for chunk in chunks if chunk.required)

    from pony.context.chunks import allocate_context_chunks

    allocation = allocate_context_chunks(chunks, pool_tokens=required_tokens)
    selected_keys = {chunk.key for chunk in allocation.selected}

    assert {"active-recovery", "checkpoint-state"} <= selected_keys


def test_checkpoint_context_redacts_known_secret_and_bounds_view():
    agent = _agent()
    secret = "sk-ABCDEF1234567890"
    agent.redaction_env = {"PONY_API_KEY": secret}
    agent.secret_env_names = ("PONY_API_KEY",)
    agent.session.update(
        checkpoints={
            "current_id": "ckpt-bounded",
            "items": {
                "ckpt-bounded": {
                    "goal": f"Use {secret}",
                    "blocker": "x" * 10_000,
                }
            },
        },
    )

    chunks = task_working_set_chunks(agent, agent.token_accounting)
    rendered = "\n".join(chunk.text for chunk in chunks)
    checkpoint = next(chunk for chunk in chunks if chunk.key == "checkpoint-state")

    assert secret not in rendered
    assert "<redacted>" in rendered
    assert agent.token_accounting.count_text(checkpoint.text) <= 768


def test_recovery_context_only_exists_for_actionable_state():
    agent = _agent()
    assert recovery_state_chunks(agent, agent.token_accounting) == []

    agent.resume_state = {
        "status": "workspace-mismatch",
        "runtime_identity_mismatch_fields": ["workspace_root"],
    }
    chunks = recovery_state_chunks(agent, agent.token_accounting)

    assert len(chunks) == 1
    assert chunks[0].required is True
    assert "workspace-mismatch" in chunks[0].text


def test_memory_index_uses_snapshot_documents_without_rescanning(tmp_path):
    agent = _real_agent(tmp_path)
    note = tmp_path / ".pony" / "memory" / "notes" / "cache.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\n"
        "Cache state stays stable.\n",
        encoding="utf-8",
    )
    snapshot = agent.memory_retrieval.snapshot()

    chunks = memory_index_chunks(agent, agent.token_accounting, snapshot)

    assert [chunk.key for chunk in chunks] == ["workspace/notes/cache.md"]
    assert "cache invariant" in chunks[0].text


def test_one_top_level_render_scans_memory_once_for_index_recall_and_links(
    tmp_path,
    monkeypatch,
):
    agent = _real_agent(tmp_path)
    notes = tmp_path / ".pony" / "memory" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "cache.md").write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\n"
        "Cache stays stable. See [[target]].\n",
        encoding="utf-8",
    )
    (notes / "target.md").write_text(
        "---\nname: target\ntype: reference\ndescription: linked detail\n---\n"
        "Target cache detail.\n",
        encoding="utf-8",
    )
    real_snapshot = agent.memory_retrieval.snapshot
    calls = {"count": 0}

    def counting_snapshot():
        calls["count"] += 1
        return real_snapshot()

    monkeypatch.setattr(agent.memory_retrieval, "snapshot", counting_snapshot)

    text, telemetry = render_current_user_message(agent, "explain cache")

    assert calls["count"] == 1
    assert "workspace/notes/cache.md" in text
    assert "Target cache detail" in text
    assert telemetry["context_source_allocator"]["memory_snapshot"] == "loaded"
