import json
import sys

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_agent_write_file_creates_restorable_turn_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"note.txt","content":"after\\n"}}</tool>',
            "<final>done</final>",
        ],
    )

    agent.ask("write note")

    records = agent.checkpoint_store.list_checkpoint_records()
    turn_records = [item for item in records if item["checkpoint_type"] == "turn"]
    assert turn_records
    assert any(entry["path"] == "note.txt" for record in turn_records for entry in record["file_entries"])
    assert agent.current_task_state.recovery_checkpoint_id


def test_single_user_request_creates_one_turn_checkpoint_for_multiple_tool_changes(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"first.txt","content":"one\\n"}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"second.txt","content":"two\\n"}}</tool>',
            "<final>done</final>",
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
    assert {entry["path"] for entry in checkpoint["file_entries"]} == {"first.txt", "second.txt"}
    assert checkpoint["checkpoint_id"] == agent.current_task_state.recovery_checkpoint_id


def test_memory_save_creates_audit_without_recoverable_turn_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"memory_save","args":{"note":"remember this"}}</tool>',
            "<final>done</final>",
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
            '<tool>{"name":"write_file","args":{"path":"note.txt","content":"after\\n"}}</tool>',
            "<final>done</final>",
        ],
    )
    agent.ask("write note")
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"

    plan = agent.recovery_manager.preview_restore(checkpoint_id)
    assert plan["entries"]

    result = agent.recovery_manager.apply_restore(checkpoint_id)
    assert result["restore_checkpoint_id"]
    assert agent.checkpoint_store.load_checkpoint_record(result["restore_checkpoint_id"])["checkpoint_type"] == "restore"
    assert not (tmp_path / "note.txt").exists()


def test_restore_existing_file_returns_to_previous_content(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"patch_file","args":{"path":"README.md","old_text":"demo\\n","new_text":"changed\\n"}}</tool>',
            "<final>done</final>",
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
            '<tool>{"name":"write_file","args":{"path":"note.txt","content":"after\\n"}}</tool>',
            "<final>done</final>",
        ],
    )
    agent.ask("finish")
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id

    record = agent.record_verification_evidence(
        command="python -m pytest -q",
        risk_class="workspace_write",
        exit_code=0,
        stdout="passed",
        stderr="",
        checkpoint_id=checkpoint_id,
    )

    checkpoint = agent.checkpoint_store.load_checkpoint_record(checkpoint_id)
    assert checkpoint["verification_evidence"][0]["verification_id"] == record["verification_id"]
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state.run_id).read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("event") == "verification_recorded" and event.get("verification_id") == record["verification_id"]
        for event in trace_events
    )


def test_run_shell_verification_command_attaches_evidence_to_checkpoint(tmp_path):
    command = f"{sys.executable} -m pytest --version"
    tool_call = json.dumps({"name": "run_shell", "args": {"command": command, "timeout": 20}})
    agent = build_agent(tmp_path, [f"<tool>{tool_call}</tool>", "<final>done</final>"])

    agent.ask("Run the verification command")

    checkpoint = agent.checkpoint_store.load_checkpoint_record(agent.current_task_state.recovery_checkpoint_id)
    evidence = checkpoint["verification_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["command"] == command
    assert evidence[0]["status"] == "passed"
