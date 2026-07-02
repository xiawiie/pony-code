from pico.checkpoint_store import CheckpointStore
from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.tool_change_recorder import ToolChangeRecorder


def test_finalize_records_success_and_error_states(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store)
    pending = recorder.start("", "task_1", "write_file", "workspace_write", {"path": "a.txt"})

    finalized = recorder.finalize(
        pending["tool_change_id"],
        status="error",
        affected_paths=["a.txt"],
        file_entries=[],
        error={"code": "tool_failed", "message": "boom"},
    )

    assert finalized["status"] == "error"
    assert finalized["ended_at"]
    assert finalized["error"]["code"] == "tool_failed"


def test_mark_interrupted_only_changes_pending_records(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store)
    pending = recorder.start("", "task_1", "run_shell", "workspace_write", {})
    done = recorder.start("", "task_1", "write_file", "workspace_write", {})
    recorder.finalize(done["tool_change_id"], "finalized", [], [])

    interrupted = recorder.mark_interrupted_pending()

    assert [item["tool_change_id"] for item in interrupted] == [pending["tool_change_id"]]
    assert store.load_tool_change_record(done["tool_change_id"])["status"] == "finalized"


def test_runtime_marks_existing_pending_tool_changes_interrupted_on_startup(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store).start("", "task_1", "run_shell", "workspace_write", {})

    Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )

    assert store.load_tool_change_record(pending["tool_change_id"])["status"] == "interrupted"


def test_runtime_startup_does_not_interrupt_owned_pending_tool_changes(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = CheckpointStore(tmp_path)
    active = ToolChangeRecorder(store, owner_id="active-runtime").start(
        "",
        "task_active",
        "run_shell",
        "workspace_write",
        {},
    )
    legacy = ToolChangeRecorder(store).start("", "task_legacy", "run_shell", "workspace_write", {})

    Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )

    assert store.load_tool_change_record(active["tool_change_id"])["status"] == "pending"
    assert store.load_tool_change_record(legacy["tool_change_id"])["status"] == "interrupted"
