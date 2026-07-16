from pathlib import Path
from types import SimpleNamespace

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.runtime import (
    WorkspaceRewindConfirmationRequired,
    WorkspaceRewindError,
)


def _agent(tmp_path, outputs=None, *, store=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient(outputs or []),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=store or SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def _write_turn(agent):
    agent.model_client.outputs.extend(
        [
            {
                "name": "write_file",
                "arguments": {"path": "note.txt", "content": "after\n"},
            },
            "done",
        ]
    )
    assert agent.ask("write note") == "done"
    task_entries = [
        entry
        for entry in agent.session_store.load_tree(agent.session["id"]).active_path
        if entry["type"] == "task_checkpoint"
    ]
    assert len(task_entries) == 1
    return task_entries[0]


def test_task_checkpoint_links_session_context_files_and_recovery(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)
    checkpoint = entry["data"]["checkpoint"]

    assert checkpoint["goal"] == "write note"
    assert checkpoint["status"] == "completed"
    assert checkpoint["modified_files"] == ["note.txt"]
    assert checkpoint["workspace_checkpoint_id"]
    assert checkpoint["worktree_identity_digest"]
    assert checkpoint["context_usage"]["input_limit"] > 0
    assert checkpoint["next_steps"]


def test_session_only_rewind_never_changes_workspace(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)

    rewind = agent.rewind_session(entry["id"])

    assert rewind["parent_id"] == entry["id"]
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"


def test_workspace_rewind_requires_preview_and_one_confirmation(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)

    preview = agent.preview_workspace_rewind(entry["id"])
    assert preview["status"] == "ready"
    assert preview["decision_counts"] == {"restore": 1}

    with pytest.raises(WorkspaceRewindConfirmationRequired) as captured:
        agent.rewind_session(entry["id"], workspace=True)

    assert captured.value.preview["target_entry_id"] == entry["id"]
    assert (tmp_path / "note.txt").exists()


def test_confirmed_workspace_rewind_restores_then_branches(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)

    result = agent.rewind_session(
        entry["id"],
        workspace=True,
        confirmed=True,
    )

    assert not (tmp_path / "note.txt").exists()
    assert result.restore_result["status"] == "applied"
    assert result.rewind_entry["parent_id"] == entry["id"]
    assert result.rewind_entry["data"]["workspace_checkpoint_id"]
    assert result.rewind_entry["data"]["restore_checkpoint_id"]
    assert agent.session_store.load_rewind_intent(agent.session["id"]) is None


def test_workspace_conflict_does_not_move_session_leaf(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)
    old_leaf = agent.session_store.load_tree(agent.session["id"]).leaf_id
    (tmp_path / "note.txt").write_text("external\n", encoding="utf-8")

    preview = agent.preview_workspace_rewind(entry["id"])
    assert preview["status"] == "conflicted"
    with pytest.raises(WorkspaceRewindError, match="not applicable"):
        agent.rewind_session(entry["id"], workspace=True, confirmed=True)

    assert agent.session_store.load_tree(agent.session["id"]).leaf_id == old_leaf
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "external\n"


def test_resume_reconciles_restore_success_after_session_append_crash(
    tmp_path,
    monkeypatch,
):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)
    session_id = agent.session["id"]

    def fail_rewind(*_args, **_kwargs):
        raise OSError("simulated session append crash")

    monkeypatch.setattr(agent.session_store, "rewind", fail_rewind)
    with pytest.raises(OSError, match="session append crash"):
        agent.rewind_session(entry["id"], workspace=True, confirmed=True)

    assert not (tmp_path / "note.txt").exists()
    intent = agent.session_store.load_rewind_intent(session_id)
    assert intent["state"] == "restored"

    resumed_store = SessionStore(Path(tmp_path) / ".pico" / "sessions")
    resumed = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=resumed_store,
        session_id=session_id,
        approval_policy="auto",
    )

    tree = resumed.session_store.load_tree(session_id)
    rewind = next(
        item for item in reversed(tree.active_path) if item["type"] == "rewind"
    )
    assert rewind["parent_id"] == entry["id"]
    assert resumed.session_store.load_rewind_intent(session_id) is None


def test_prepared_rewind_intent_matches_exact_restore_operation_after_crash(
    tmp_path,
    monkeypatch,
):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)
    checkpoint = entry["data"]["checkpoint"]
    session_id = agent.session["id"]
    original_write = agent.session_store.write_rewind_intent
    writes = 0

    def fail_before_recording_restore_id(target_session_id, intent):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated restored-intent crash")
        return original_write(target_session_id, intent)

    monkeypatch.setattr(
        agent.session_store,
        "write_rewind_intent",
        fail_before_recording_restore_id,
    )
    with pytest.raises(OSError, match="restored-intent crash"):
        agent.rewind_session(entry["id"], workspace=True, confirmed=True)

    assert not (tmp_path / "note.txt").exists()
    prepared = agent.session_store.load_rewind_intent(session_id)
    assert prepared["state"] == "prepared"
    assert prepared["operation_id"].startswith("rewind_")

    # A newer audit for the same Recovery checkpoint must not be mistaken for
    # this rewind's restore operation.
    agent.recovery_manager.apply_restore(checkpoint["workspace_checkpoint_id"])

    resumed_store = SessionStore(Path(tmp_path) / ".pico" / "sessions")
    resumed = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=resumed_store,
        session_id=session_id,
        approval_policy="auto",
    )

    rewind = next(
        item
        for item in reversed(resumed.session_store.load_tree(session_id).active_path)
        if item["type"] == "rewind"
    )
    assert rewind["parent_id"] == entry["id"]
    assert resumed.session_store.load_rewind_intent(session_id) is None


def test_mid_turn_entry_reports_legal_checkpoint_candidates(tmp_path):
    agent = _agent(tmp_path)
    checkpoint_entry = _write_turn(agent)
    message_entry = next(
        entry
        for entry in agent.session_store.load_tree(agent.session["id"]).active_path
        if entry["type"] == "message"
    )

    with pytest.raises(WorkspaceRewindError, match="nearest legal candidates") as error:
        agent.preview_workspace_rewind(message_entry["id"])

    assert checkpoint_entry["id"] in str(error.value)


def test_finalized_sandbox_blocks_session_only_rewind(tmp_path):
    agent = _agent(tmp_path)
    entry = _write_turn(agent)
    agent.docker_sandbox = True
    agent.sandbox_session = SimpleNamespace(manifest={"state": "pending_review"})

    with pytest.raises(WorkspaceRewindError, match="forbidden"):
        agent.rewind_session(entry["id"])
