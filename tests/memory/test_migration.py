"""Session memory migration coverage retained until the Plan 3 hard cut."""


def test_legacy_session_memory_normalizes_to_v2_shape(tmp_path):
    from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".pico" / "sessions")
    legacy_session = {
        "id": "legacy-session",
        "created_at": "2026-04-07T10:00:00+00:00",
        "workspace_root": str(tmp_path),
        "history": [],
        "memory": {
            "working": {
                "task_summary": "legacy task",
                "recent_files": ["./README.md"],
            },
            "file_summaries": {
                "README.md": {"summary": "demo"},
            },
            "episodic_notes": [{"text": "legacy note"}],
            "notes": ["legacy note"],
        },
    }
    store.save(legacy_session)

    agent = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=store,
        session_id="legacy-session",
        approval_policy="auto",
    )

    assert set(agent.session["memory"]) == {"file_summaries"}
    assert "working_memory" in agent.session
    assert agent.session["working_memory"] == {
        "task_summary": "legacy task",
        "recent_files": ["README.md"],
    }
