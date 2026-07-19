from pathlib import Path

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.runtime.rewind import WorkspaceRewindError
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient(
            [
                {
                    "name": "write_file",
                    "arguments": {"path": "note.txt", "content": "after\n"},
                },
                "done",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )


def _write_turn(agent):
    assert agent.ask("write note") == "done"
    return next(
        entry
        for entry in agent.session_store.load_tree(agent.session["id"]).active_path
        if entry["type"] == "task_checkpoint"
    )


def test_task_checkpoint_keeps_session_resume_facts_without_recovery_artifact(tmp_path):
    entry = _write_turn(_agent(tmp_path))
    checkpoint = entry["data"]["checkpoint"]

    assert checkpoint["goal"] == "write note"
    assert checkpoint["status"] == "completed"
    assert checkpoint["modified_files"] == ["note.txt"]
    assert checkpoint["workspace_checkpoint_id"] == ""
    assert checkpoint["worktree_identity_digest"]


def test_session_only_rewind_never_changes_workspace(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)

    rewind = agent.rewind_session(entry["id"])

    assert rewind["parent_id"] == entry["id"]
    assert (Path(tmp_path) / "note.txt").read_text(encoding="utf-8") == "after\n"


def test_workspace_restore_is_explicitly_unavailable(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)

    with pytest.raises(WorkspaceRewindError, match="workspace_restore_unavailable"):
        agent.preview_workspace_rewind(entry["id"])
    with pytest.raises(WorkspaceRewindError, match="workspace_restore_unavailable"):
        agent.rewind_session(entry["id"], workspace=True, confirmed=True)

    assert (Path(tmp_path) / "note.txt").exists()
