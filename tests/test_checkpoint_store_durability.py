from contextlib import contextmanager
import multiprocessing

import pytest

from pico.state.checkpoint_store import (
    CheckpointStore,
    CheckpointStoreError,
    source_apply_guard_present,
)
from pico.recovery.models import new_checkpoint_record, new_tool_change_record


def _checkpoint(tmp_path, checkpoint_id="ckpt_durable"):
    return new_checkpoint_record(
        checkpoint_id, "turn", "session", "run", "turn", "", str(tmp_path)
    )


def test_checkpoint_store_exposes_required_mutation_lock(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    calls = []

    @contextmanager
    def required_lock(path, *, require_lock=False):
        calls.append((path, require_lock))
        yield

    monkeypatch.setattr("pico.state.checkpoint_store.file_lock.locked_file", required_lock)
    with store.mutation_lock():
        pass

    assert calls == [(store.mutation_lock_path, True)]


def test_source_apply_guard_blocks_other_mutations_until_exact_owner_clears(tmp_path):
    store = CheckpointStore(tmp_path)
    journal_id = "apply_" + "1" * 32
    with store.mutation_lock():
        store.begin_source_apply_guard(
            journal_id=journal_id,
            sandbox_id="sandbox_" + "2" * 32,
            diff_digest="sha256:" + "3" * 64,
        )

    assert source_apply_guard_present(tmp_path) is True
    with pytest.raises(CheckpointStoreError, match="source_apply_review_required"):
        with store.mutation_lock():
            pass
    with store.mutation_lock(source_apply_journal_id=journal_id):
        store.finish_source_apply_guard(journal_id=journal_id)
    assert source_apply_guard_present(tmp_path) is False


def test_invalid_source_apply_guard_fails_closed(tmp_path):
    store = CheckpointStore(tmp_path)
    store.source_mutation_guard_path.write_bytes(b"{invalid")

    with pytest.raises(CheckpointStoreError, match="source_apply_guard_invalid"):
        with store.mutation_lock():
            pass


def test_checkpoint_rmw_rejects_status_conflict(tmp_path):
    store = CheckpointStore(tmp_path)
    record = _checkpoint(tmp_path, "ckpt_cas")
    store.write_checkpoint_record(record)

    with pytest.raises(ValueError, match="status_conflict"):
        store.update_checkpoint_record(
            "ckpt_cas",
            lambda value: {**value, "status": ""},
            expected_status="applying",
        )

    assert store.load_checkpoint_record("ckpt_cas")["status"] == ""


def test_tool_change_rmw_returns_exact_redacted_persisted_copy(tmp_path):
    sentinel = "sk-rmw-redactor-sentinel"

    def redact(value):
        value["input_summary"] = {"value": "<redacted>"}
        return value

    store = CheckpointStore(tmp_path, redactor=redact)
    record = new_tool_change_record(
        "tc_rmw", "", "turn", "write_file", "workspace_write", "owner"
    )
    store.write_tool_change_record(record)

    updated = store.update_tool_change_record(
        "tc_rmw",
        lambda value: {**value, "input_summary": {"value": sentinel}},
        expected_status="pending",
    )

    assert updated["input_summary"] == {"value": "<redacted>"}
    assert store.load_tool_change_record("tc_rmw") == updated


def test_blob_and_json_writes_fsync_file_then_parent(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    events = []
    monkeypatch.setattr(store, "_fsync_file", lambda descriptor: events.append("file"))
    monkeypatch.setattr(store, "_fsync_parent", lambda descriptor: events.append("parent"))

    store.write_blob(b"durable")
    store.write_checkpoint_record(_checkpoint(tmp_path))

    assert events == ["file", "parent", "file", "parent"]


def _reentrant_transform_worker(root, queue):
    store = CheckpointStore(root)
    store.write_checkpoint_record(_checkpoint(root, "ckpt_reentrant"))
    try:
        store.update_checkpoint_record(
            "ckpt_reentrant",
            lambda record: (store.write_blob(b"nested"), record)[1],
            expected_status="",
        )
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


def test_rmw_transform_reentry_fails_fast_instead_of_deadlocking(tmp_path):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(
        target=_reentrant_transform_worker,
        args=(tmp_path, queue),
    )
    process.start()
    process.join(timeout=3)
    if process.is_alive():
        process.terminate()
        process.join(timeout=3)
        pytest.fail("reentrant transform deadlocked")

    error_type, message = queue.get(timeout=1)
    assert error_type == "RuntimeError"
    assert "reentry" in message


def test_rmw_return_is_json_canonical_persisted_copy(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_tool_change_record(
        "tc_canonical", "", "turn", "write_file", "workspace_write", "owner"
    )
    store.write_tool_change_record(record)

    updated = store.update_tool_change_record(
        "tc_canonical",
        lambda value: {**value, "input_summary": {"tuple": ("a", "b")}},
        expected_status="pending",
    )

    assert updated["input_summary"] == {"tuple": ["a", "b"]}
    assert store.load_tool_change_record("tc_canonical") == updated


def test_store_to_mutation_lock_order_is_rejected(tmp_path):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(_checkpoint(tmp_path, "ckpt_lock_order"))

    def reverse_lock_order(record):
        with store.mutation_lock():
            return record

    with pytest.raises(RuntimeError, match="lock order violation"):
        store.update_checkpoint_record(
            "ckpt_lock_order",
            reverse_lock_order,
            expected_status="",
        )
