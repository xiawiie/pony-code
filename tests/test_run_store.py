import json
import os
import stat
import threading
import time

import pytest

import pony.state.run_store as run_store_module
from pony.security import private_files as security_module
from pony.state.run_store import RunStore
from pony.state.task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskState


def test_run_store_creates_run_directory_and_state_file(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="run_001", task_id="task_001", user_request="Inspect the repo."
    )

    run_dir = store.start_run(state)

    assert run_dir == store.run_dir(state.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="run_002", task_id="task_002", user_request="Trace the run."
    )
    store.start_run(state)

    store.append_trace(
        state, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"}
    )
    store.append_trace(
        state.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "request_metadata": {"request_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(
        state.run_id,
        {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"},
    )

    lines = (store.trace_path(state.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


def test_run_store_rejects_trace_append_past_artifact_limit(tmp_path, monkeypatch):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="bounded_trace", task_id="task", user_request="safe"
    )
    store.start_run(state)
    first = {"event": "run_started"}
    store.append_trace(state, first)
    canonical = store.trace_path(state).read_bytes()
    monkeypatch.setattr(run_store_module, "MAX_RUN_ARTIFACT_BYTES", len(canonical))
    monkeypatch.setattr(
        security_module.os,
        "ftruncate",
        lambda *_args: pytest.fail("bounded append attempted rollback write"),
    )

    with pytest.raises(ValueError, match="private file too large"):
        store.append_trace(state, {"event": "run_finished"})

    assert store.trace_path(state).read_bytes() == canonical


def test_run_store_serializes_concurrent_bounded_trace_appends(
    tmp_path,
    monkeypatch,
):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="concurrent_trace", task_id="task", user_request="safe"
    )
    store.start_run(state)
    event = {"event": "tool_executed", "name": "read_file"}
    rendered = (json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode()
    monkeypatch.setattr(run_store_module, "MAX_RUN_ARTIFACT_BYTES", len(rendered))
    entered = threading.Event()
    release = threading.Event()
    original_append = run_store_module.append_private_bytes
    results = []

    def delayed_append(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            release.wait(timeout=5)
        return original_append(*args, **kwargs)

    def append():
        try:
            store.append_trace(state, event)
            results.append("written")
        except ValueError as exc:
            results.append(str(exc))

    monkeypatch.setattr(run_store_module, "append_private_bytes", delayed_append)
    first = threading.Thread(target=append)
    second = threading.Thread(target=append)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.05)
    assert second.is_alive()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert sorted(results) == ["private file too large", "written"]
    assert store.trace_path(state).read_bytes() == rendered


def test_run_store_trace_append_requires_cross_process_lock(tmp_path, monkeypatch):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(run_id="locked_trace", task_id="task", user_request="safe")
    store.start_run(state)
    real_lock = run_store_module.file_lock.locked_file
    calls = []

    def require_lock(path, **kwargs):
        calls.append((path, kwargs))
        return real_lock(path, **kwargs)

    monkeypatch.setattr(run_store_module.file_lock, "locked_file", require_lock)

    store.append_trace(state, {"event": "run_started"})

    assert calls == [(store.trace_lock_path(state), {"require_lock": True})]


def test_run_store_writes_report_json(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="run_003", task_id="task_003", user_request="Report the run."
    )
    store.start_run(state)
    state.finish_success("Done.")

    store.write_task_state(state)
    store.write_report(
        state, {"task_state": state.to_dict(), "stop_reason": state.stop_reason}
    )

    report = json.loads(store.report_path(state.run_id).read_text(encoding="utf-8"))
    assert report["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert report["task_state"]["final_answer"] == "Done."


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="run_004", task_id="task_004", user_request="Crash before finalize."
    )

    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})

    assert store.trace_path(state.run_id).exists()
    assert not store.report_path(state.run_id).exists()


@pytest.mark.parametrize("artifact", ("task_state", "report"))
def test_run_store_load_rejects_duplicate_json_keys(tmp_path, artifact):
    store = RunStore(tmp_path / ".pony" / "runs")
    run_dir = store.run_dir("duplicate")
    run_dir.mkdir(mode=0o700)
    path = (
        store.task_state_path("duplicate")
        if artifact == "task_state"
        else store.report_path("duplicate")
    )
    path.write_text('{"value": 1, "value": 2}', encoding="utf-8")

    loader = store.load_task_state if artifact == "task_state" else store.load_report
    with pytest.raises(ValueError, match="duplicate artifact key"):
        loader("duplicate")


@pytest.mark.parametrize("artifact", ("task_state", "report"))
def test_run_store_load_rejects_oversized_artifact(tmp_path, monkeypatch, artifact):
    store = RunStore(tmp_path / ".pony" / "runs")
    monkeypatch.setattr(run_store_module, "MAX_RUN_ARTIFACT_BYTES", 8)
    run_dir = store.run_dir("oversized")
    run_dir.mkdir(mode=0o700)
    path = (
        store.task_state_path("oversized")
        if artifact == "task_state"
        else store.report_path("oversized")
    )
    path.write_bytes(b"x" * 9)

    loader = store.load_task_state if artifact == "task_state" else store.load_report
    with pytest.raises(ValueError, match="too large"):
        loader("oversized")

    assert path.read_bytes() == b"x" * 9


def test_run_store_write_bounds_existing_artifact_before_backup(
    tmp_path,
    monkeypatch,
):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(
        run_id="oversized_existing", task_id="task", user_request="ok"
    )
    rendered = (json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    monkeypatch.setattr(run_store_module, "MAX_RUN_ARTIFACT_BYTES", len(rendered))
    path = store.task_state_path(state)
    path.parent.mkdir(mode=0o700)
    original = b"x" * (len(rendered) + 1)
    path.write_bytes(original)

    with pytest.raises(ValueError, match="private file too large"):
        store.write_task_state(state)

    assert path.read_bytes() == original
    assert not list(path.parent.glob(".*.bak"))


@pytest.mark.parametrize("artifact", ("task_state", "report"))
def test_run_store_write_rejects_oversized_artifact_without_replacing_canonical(
    tmp_path,
    monkeypatch,
    artifact,
):
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(run_id="bounded", task_id="task", user_request="ok")
    report = {"status": "ok"}
    if artifact == "task_state":
        path = store.task_state_path(state)
        expected = state.to_dict()
    else:
        path = store.report_path(state)
        expected = dict(report)
        monkeypatch.setattr(
            run_store_module,
            "validate_report",
            lambda value, *, run_id: value,
        )

    def write():
        if artifact == "task_state":
            return store.write_task_state(state)
        return store.write_report(state, report)

    def load():
        if artifact == "task_state":
            return store.load_task_state(state)
        return store.load_report(state)

    write()
    canonical = path.read_bytes()
    monkeypatch.setattr(
        run_store_module,
        "MAX_RUN_ARTIFACT_BYTES",
        len(canonical),
    )

    assert write() == path
    assert load() == expected

    if artifact == "task_state":
        state.user_request = "x" * (len(canonical) + 1)
    else:
        report["status"] = "x" * (len(canonical) + 1)
    with pytest.raises(ValueError, match="private file too large"):
        write()

    assert path.read_bytes() == canonical


def test_run_store_paths_are_private(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
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
    store = RunStore(tmp_path / ".pony" / "runs")
    state = TaskState.create(run_id="linked", task_id="task", user_request="linked")
    store.start_run(state)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside\n", encoding="utf-8")
    store.trace_path(state).symlink_to(outside)

    with pytest.raises(ValueError, match="regular|symlink"):
        store.append_trace(state, {"event": "must_not_land"})

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_run_store_parent_swap_cannot_redirect_record(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    original_root = tmp_path / "runs-original"
    store.root.rename(original_root)
    store.root.mkdir()
    state = TaskState.create(run_id="redirected", task_id="task", user_request="safe")

    with pytest.raises(ValueError, match="private root changed"):
        store.write_task_state(state)

    assert not (store.root / "redirected" / "task_state.json").exists()
    assert not (original_root / "redirected" / "task_state.json").exists()


def test_trace_append_rolls_back_if_hardlinked_during_write(tmp_path, monkeypatch):
    store = RunStore(tmp_path / ".pony" / "runs")
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
