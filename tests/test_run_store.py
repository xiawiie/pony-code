import json
import os
import stat

import pytest

from pico import security as security_module
from pico.run_store import RunStore
from pico.task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskState


def test_run_store_creates_run_directory_and_state_file(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    run_dir = store.start_run(state)

    assert run_dir == store.run_dir(state.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(state)

    store.append_trace(state, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"})
    store.append_trace(
        state.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "request_metadata": {"request_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(state.run_id, {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"})

    lines = (store.trace_path(state.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


def test_run_store_writes_report_json(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Report the run.")
    store.start_run(state)
    state.finish_success("Done.")

    store.write_task_state(state)
    store.write_report(state, {"task_state": state.to_dict(), "stop_reason": state.stop_reason})

    report = json.loads(store.report_path(state.run_id).read_text(encoding="utf-8"))
    assert report["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert report["task_state"]["final_answer"] == "Done."


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})

    assert store.trace_path(state.run_id).exists()
    assert not store.report_path(state.run_id).exists()


def test_run_store_paths_are_private(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="private", task_id="task", user_request="private")

    run_dir = store.start_run(state)
    trace_path = store.append_trace(state, {"event": "private"})
    report_path = store.write_report(state, {"status": "done"})

    if os.name == "posix":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
        for path in (store.task_state_path(state), trace_path, report_path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_append_trace_refuses_symlink_without_touching_target(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="linked", task_id="task", user_request="linked")
    store.start_run(state)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside\n", encoding="utf-8")
    store.trace_path(state).symlink_to(outside)

    with pytest.raises(ValueError, match="regular|symlink"):
        store.append_trace(state, {"event": "must_not_land"})

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_run_store_parent_swap_cannot_redirect_record(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    original_root = tmp_path / "runs-original"
    store.root.rename(original_root)
    store.root.mkdir()
    state = TaskState.create(run_id="redirected", task_id="task", user_request="safe")

    with pytest.raises(ValueError, match="private root changed"):
        store.write_task_state(state)

    assert not (store.root / "redirected" / "task_state.json").exists()
    assert not (original_root / "redirected" / "task_state.json").exists()


def test_trace_append_rolls_back_if_hardlinked_during_write(tmp_path, monkeypatch):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="raced", task_id="task", user_request="safe")
    store.start_run(state)
    alias = tmp_path / "trace-alias.jsonl"
    real_fsync = security_module.os.fsync
    linked = False

    def link_after_write(descriptor):
        nonlocal linked
        real_fsync(descriptor)
        trace = store.trace_path(state)
        if not linked and trace.exists():
            os.link(trace, alias)
            linked = True

    monkeypatch.setattr(security_module.os, "fsync", link_after_write)

    with pytest.raises(ValueError, match="changed"):
        store.append_trace(state, {"secret": "must-not-persist"})

    assert alias.read_bytes() == b""
