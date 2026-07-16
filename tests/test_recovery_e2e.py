from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from pico import Pico
from pico.state.session_store import SessionStore
from pico.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
from pico.runtime.options import RuntimeOptions


def build_agent(tmp_path, outputs, *, executables=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path, executables=executables)
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        options=RuntimeOptions(approval_policy="auto"),
    )


def test_agent_write_file_creates_restorable_turn_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )

    agent.ask("write note")

    records = agent.checkpoint_store.list_checkpoint_records()
    turn_records = [item for item in records if item["checkpoint_type"] == "turn"]
    assert turn_records
    assert any(
        entry["path"] == "note.txt"
        for record in turn_records
        for entry in record["file_entries"]
    )
    assert agent.current_task_state.recovery_checkpoint_id


def test_single_user_request_creates_one_turn_checkpoint_for_multiple_tool_changes(
    tmp_path,
):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"first.txt","content":"one\n"}},
            {"name": "write_file", "args": {"path":"second.txt","content":"two\n"}},
            "done",
        ],
    )

    agent.ask("write two files")

    turn_records = [
        item
        for item in agent.checkpoint_store.list_checkpoint_records()
        if item["checkpoint_type"] == "turn"
    ]
    assert len(turn_records) == 1
    checkpoint = turn_records[0]
    assert len(checkpoint["tool_change_ids"]) == 2
    assert {entry["path"] for entry in checkpoint["file_entries"]} == {
        "first.txt",
        "second.txt",
    }
    assert (
        checkpoint["checkpoint_id"] == agent.current_task_state.recovery_checkpoint_id
    )


def test_memory_save_creates_audit_without_recoverable_turn_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "memory_save", "args": {"note":"remember this"}},
            "done",
        ],
    )

    assert agent.ask("remember this") == "done"

    record = next(
        item
        for item in agent.checkpoint_store.list_tool_change_records()
        if item["tool_name"] == "memory_save"
    )
    assert record["effect_class"] == "memory_write"
    assert record["affected_paths"] == []
    assert record["file_entries"] == []
    assert not [
        item
        for item in agent.checkpoint_store.list_checkpoint_records()
        if item["checkpoint_type"] == "turn"
    ]
    assert agent.current_task_state.recovery_checkpoint_id == ""


def test_real_checkpoint_can_preview_and_apply_restore(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )
    agent.ask("write note")
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"

    plan = agent.recovery_manager.preview_restore(checkpoint_id)
    assert plan["entries"]

    result = agent.recovery_manager.apply_restore(checkpoint_id)
    assert result["restore_checkpoint_id"]
    assert (
        agent.checkpoint_store.load_checkpoint_record(result["restore_checkpoint_id"])[
            "checkpoint_type"
        ]
        == "restore"
    )
    assert not (tmp_path / "note.txt").exists()


def test_restore_existing_file_returns_to_previous_content(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {
                "name": "patch_file",
                "args": {
                    "path": "README.md",
                    "old_text": "demo\n",
                    "new_text": "changed\n",
                },
            },
            "done",
        ],
    )

    agent.ask("change readme")
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "changed\n"

    result = agent.recovery_manager.apply_restore(checkpoint_id)

    assert result["restored_paths"] == ["README.md"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "demo\n"


def test_verification_evidence_can_attach_to_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )
    agent.ask("finish")
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id

    record = agent.record_verification_evidence(
        argv=("python", "-m", "pytest", "-q"),
        risk_class="workspace_write",
        runner_executed=True,
        execution_mode="argv",
        exit_code=0,
        stdout="passed",
        stderr="",
        checkpoint_id=checkpoint_id,
    )

    checkpoint = agent.checkpoint_store.load_checkpoint_record(checkpoint_id)
    assert (
        checkpoint["verification_evidence"][0]["verification_id"]
        == record["verification_id"]
    )
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state.run_id)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        event.get("event") == "verification_recorded"
        and event.get("verification_id") == record["verification_id"]
        for event in trace_events
    )


def test_direct_verification_recording_rejects_unexecuted_or_unstructured_facts(
    tmp_path,
):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )
    agent.ask("finish")
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id

    for overrides in (
        {"runner_executed": False},
        {"execution_mode": "shell"},
        {"exit_code": None},
        {"exit_code": False},
        {"exit_code": "0"},
    ):
        facts = {
            "argv": ("pytest", "-q"),
            "risk_class": "external_effect",
            "runner_executed": True,
            "execution_mode": "argv",
            "exit_code": 0,
            "stdout": "passed",
            "stderr": "",
            "checkpoint_id": checkpoint_id,
            **overrides,
        }
        assert agent.record_verification_evidence(**facts) is None

    checkpoint = agent.checkpoint_store.load_checkpoint_record(checkpoint_id)
    assert checkpoint["verification_evidence"] == []


def test_direct_verification_recording_rejects_non_current_checkpoint_targets(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )
    agent.ask("finish")
    current_id = agent.current_task_state.recovery_checkpoint_id
    load_record = Mock(return_value={"verification_evidence": []})
    write_record = Mock()
    monkeypatch.setattr(agent.checkpoint_store, "load_checkpoint_record", load_record)
    monkeypatch.setattr(agent.checkpoint_store, "write_checkpoint_record", write_record)

    facts = {
        "argv": ("pytest", "-q"),
        "risk_class": "external_effect",
        "runner_executed": True,
        "execution_mode": "argv",
        "exit_code": 0,
        "stdout": "passed",
        "stderr": "",
    }
    assert (
        agent.record_verification_evidence(
        **facts,
        checkpoint_id="",
        )
        is None
    )
    assert (
        agent.record_verification_evidence(
        **facts,
        checkpoint_id="ckpt_foreign",
        )
        is None
    )
    agent.current_task_state = None
    assert (
        agent.record_verification_evidence(
        **facts,
        checkpoint_id=current_id,
        )
        is None
    )
    load_record.assert_not_called()
    write_record.assert_not_called()


def test_direct_verification_recording_rejects_loaded_foreign_checkpoint(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )
    agent.ask("finish")
    current_id = agent.current_task_state.recovery_checkpoint_id
    current = agent.checkpoint_store.load_checkpoint_record(current_id)
    foreign_id = "ckpt_foreign"
    foreign = deepcopy(current)
    foreign["checkpoint_id"] = foreign_id
    agent.checkpoint_store.write_checkpoint_record(foreign)
    corrupted_current = deepcopy(current)
    corrupted_current["checkpoint_id"] = foreign_id
    current_path = agent.checkpoint_store.records_dir / f"{current_id}.json"
    current_path.write_text(json.dumps(corrupted_current), encoding="utf-8")
    real_load = agent.checkpoint_store.load_checkpoint_record
    write_record = Mock(wraps=agent.checkpoint_store.write_checkpoint_record)
    monkeypatch.setattr(agent.checkpoint_store, "write_checkpoint_record", write_record)

    record = agent.record_verification_evidence(
        argv=("pytest", "-q"),
        risk_class="external_effect",
        runner_executed=True,
        execution_mode="argv",
        exit_code=0,
        stdout="passed",
        stderr="",
        checkpoint_id=current_id,
    )

    assert record is None
    with pytest.raises(ValueError, match="internal_id_mismatch"):
        real_load(current_id)
    assert real_load(foreign_id)["verification_evidence"] == []
    write_record.assert_not_called()


def test_direct_verification_recording_requires_loaded_evidence_list(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(
        tmp_path,
        [
            {"name": "write_file", "args": {"path":"note.txt","content":"after\n"}},
            "done",
        ],
    )
    agent.ask("finish")
    current_id = agent.current_task_state.recovery_checkpoint_id
    loaded = agent.checkpoint_store.load_checkpoint_record(current_id)
    loaded["verification_evidence"] = ()
    load_record = Mock(return_value=loaded)
    write_record = Mock()
    monkeypatch.setattr(agent.checkpoint_store, "load_checkpoint_record", load_record)
    monkeypatch.setattr(agent.checkpoint_store, "write_checkpoint_record", write_record)

    record = agent.record_verification_evidence(
        argv=("pytest", "-q"),
        risk_class="external_effect",
        runner_executed=True,
        execution_mode="argv",
        exit_code=0,
        stdout="passed",
        stderr="",
        checkpoint_id=current_id,
    )

    assert record is None
    assert loaded["verification_evidence"] == ()
    write_record.assert_not_called()


def test_run_shell_verification_command_attaches_evidence_to_checkpoint(tmp_path):
    command = "python -m pytest --version"
    tool_call = {
        "name": "run_shell",
        "args": {"command": command, "timeout": 20},
    }
    agent = build_agent(
        tmp_path,
        [tool_call, "done"],
        executables={"python": "/usr/bin/python3"},
    )
    agent.approval_policy = "ask"
    agent.approve = lambda name, args: True

    agent.ask("Run the verification command")

    checkpoint = agent.checkpoint_store.load_checkpoint_record(
        agent.current_task_state.recovery_checkpoint_id
    )
    evidence = checkpoint["verification_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["command"] == command
    assert evidence[0]["status"] == "failed"


def test_recovery_e2e_a_b_c_restore_review_and_undo(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / "note.txt").write_text("A", encoding="utf-8")
    first = agent.execute_tool("write_file", {"path": "note.txt", "content": "B"})
    second = agent.execute_tool("write_file", {"path": "note.txt", "content": "C"})
    checkpoint = agent.recovery_checkpoint_writer.create_turn_checkpoint(
        session_id="session",
        run_id="run",
        turn_id="turn",
        parent_checkpoint_id="",
        tool_change_ids=[
            first.metadata["tool_change_id"],
            second.metadata["tool_change_id"],
        ],
    )
    restored = agent.recovery_manager.apply_restore(checkpoint["checkpoint_id"])
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "A"
    undone = agent.recovery_manager.apply_restore(restored["restore_checkpoint_id"])
    assert undone["status"] == "applied"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "C"
