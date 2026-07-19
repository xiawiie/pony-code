import hashlib

import pytest

from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
from pony.state.checkpoint_store import CheckpointStore
from pony.tools.change_recorder import ToolChangeRecorder
from pony.runtime.options import RuntimeOptions


def test_finalize_records_success_and_error_states(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store)
    pending = recorder.start(
        "", "task_1", "write_file", "workspace_write", {"path": "a.txt"}
    )

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

    assert [item["tool_change_id"] for item in interrupted] == [
        pending["tool_change_id"]
    ]
    assert (
        store.load_tool_change_record(done["tool_change_id"])["status"] == "finalized"
    )


def test_runtime_marks_existing_pending_tool_changes_interrupted_on_startup(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store).start(
        "", "task_1", "run_shell", "workspace_write", {}
    )

    Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )

    assert (
        store.load_tool_change_record(pending["tool_change_id"])["status"] == "pending"
    )


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
    legacy = ToolChangeRecorder(store).start(
        "", "task_legacy", "run_shell", "workspace_write", {}
    )

    Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )

    assert (
        store.load_tool_change_record(active["tool_change_id"])["status"] == "pending"
    )
    assert (
        store.load_tool_change_record(legacy["tool_change_id"])["status"] == "pending"
    )


def test_finalize_requires_matching_owner_and_pending_status(tmp_path):
    store = CheckpointStore(tmp_path)
    owner = ToolChangeRecorder(store, owner_id="owner-a")
    foreign = ToolChangeRecorder(store, owner_id="owner-b")
    pending = owner.start("", "turn-1", "write_file", "workspace_write", {})

    with pytest.raises(ValueError, match="owner_mismatch"):
        foreign.finalize(pending["tool_change_id"], "finalized")

    owner.finalize(pending["tool_change_id"], "finalized")
    with pytest.raises(ValueError, match="status_conflict"):
        owner.finalize(pending["tool_change_id"], "error")


def test_pending_review_includes_same_and_foreign_owner(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    current = recorder.start("", "turn-1", "memory_save", "memory_write", {})
    foreign = ToolChangeRecorder(store, owner_id="owner-b").start(
        "", "turn-2", "write_file", "workspace_write", {}
    )
    store.update_tool_change_record(
        current["tool_change_id"],
        lambda record: {**record, "reviewed_at": "invalid-pending-review"},
        expected_status="pending",
    )

    ids = {item["tool_change_id"] for item in recorder.pending_recovery_reviews()}
    assert ids == {current["tool_change_id"], foreign["tool_change_id"]}


def test_partial_success_requires_explicit_review(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    record = recorder.start("", "turn-1", "write_file", "workspace_write", {})
    recorder.finalize(record["tool_change_id"], "partial_success")

    assert [item["tool_change_id"] for item in recorder.pending_recovery_reviews()] == [
        record["tool_change_id"]
    ]

    reviewed = recorder.resolve_pending(
        record["tool_change_id"],
        reviewed_by="operator",
        review_reason="workspace inspected",
    )
    assert reviewed["status"] == "partial_success"
    assert reviewed["reviewed_at"]
    assert recorder.pending_recovery_reviews() == []


def test_start_persists_prepared_state_and_recovery_context(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    prepared = [
        {
        "path": "note.txt",
        "before_exists": False,
        "before_blob_ref": "",
        "before_hash": "",
        "before_mode": None,
        }
    ]
    context = {"observer_mode": "filesystem", "git_head": "abc123"}

    record = recorder.start(
        "",
        "turn-1",
        "write_file",
        "workspace_write",
        {"path": "note.txt"},
        prepared_file_entries=prepared,
        recovery_context=context,
    )

    assert record["prepared_file_entries"] == prepared
    assert record["recovery_context"] == context


def test_resolve_pending_writes_reviewed_interrupted_transition(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    pending = recorder.start("", "turn-1", "write_file", "workspace_write", {})

    resolved = recorder.resolve_pending(
        pending["tool_change_id"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )

    assert resolved["status"] == "interrupted"
    assert resolved["reviewed_by"] == "cli"
    assert resolved["review_reason"] == "explicit_cli_resolution"
    assert resolved["reviewed_at"]


def test_resolve_pending_rejects_changed_preview_hash(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    pending = recorder.start("", "turn-1", "write_file", "workspace_write", {})
    _, raw = store.load_tool_change_record_snapshot(pending["tool_change_id"])
    record = store.load_tool_change_record(pending["tool_change_id"])
    record["review_reason"] = "changed"
    store.write_tool_change_record(record)
    with pytest.raises(ValueError, match="record_changed"):
        recorder.resolve_pending(
            pending["tool_change_id"],
            reviewed_by="cli",
            review_reason="explicit_cli_resolution",
            expected_record_hash=hashlib.sha256(raw).hexdigest(),
        )
    assert (
        store.load_tool_change_record(pending["tool_change_id"])["status"] == "pending"
    )


def test_start_returns_canonical_redacted_persisted_copy(tmp_path):
    sentinel = "sk-start-redactor-sentinel"

    def redact(value):
        value["input_summary"] = {"value": "<redacted>", "tuple": ("a", "b")}
        return value

    store = CheckpointStore(tmp_path, redactor=redact)
    recorder = ToolChangeRecorder(store, owner_id="owner")

    started = recorder.start(
        "", "turn", "write_file", "workspace_write", {"value": sentinel}
    )

    assert sentinel not in str(started)
    assert started["input_summary"]["tuple"] == ["a", "b"]
    assert store.load_tool_change_record(started["tool_change_id"]) == started
