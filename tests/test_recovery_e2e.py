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
