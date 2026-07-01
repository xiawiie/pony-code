from pico.checkpoint_store import CheckpointStore
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
