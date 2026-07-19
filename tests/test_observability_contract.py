import json
from types import SimpleNamespace

import pytest

import pony.agent.observability as observability_module
from pony.agent.observability import (
    RunArtifactError,
    load_run_summary,
    project_trace_event,
    validate_report,
    validate_trace,
)


def _state():
    return SimpleNamespace(run_id="run_1", task_id="task_1", attempts=2)


def _report(run_id="run_1"):
    return {
        "record_type": "run_report",
        "format_version": 4,
        "run": {"run_id": run_id, "task_id": "task_1", "status": "completed", "stop_reason": "final_answer_returned", "duration_ms": 12, "commit": "", "dirty": False},
        "model": {
            "attempts": 2, "turns": 2, "failures": 0, "retries": 0,
            "transport_attempts": 2, "transport_retries": 0,
            "evidence_complete": True, "attempt_origin_counts": {},
            "failure_reason_counts": {},
            "usage": {
                "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                "cached_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "cache_hit": False,
            },
        },
        "context": {},
        "tools": {"calls": 0, "allowed": 0, "denied": 0, "name_counts": {}, "status_counts": {}},
        "memory": {"recall_candidates": 0, "recall_selected": 0, "filter_counts": {}},
        "effects": {"changed_files": 0, "partial_successes": 0},
        "integrity": {"writer": "current", "terminal_event_expected": True},
        "finalization": {"status": "complete", "error_count": 0},
    }


def test_trace_projector_envelopes_and_drops_forbidden_content():
    event = project_trace_event(
        _state(),
        "tool_executed",
        {
            "name": "run_shell",
            "args": {"command": "secret"},
            "result": "secret",
            "stdout": "secret",
            "duration_ms": 3,
            "transport_attempts": 1,
            "transport_retries": 0,
            "transport_evidence_complete": True,
        },
        created_at="2026-07-12T00:00:00Z",
    )
    assert event["trace_schema_version"] == 1
    assert event["event_id"].startswith("evt_")
    assert event["run_id"] == "run_1"
    assert event["task_id"] == "task_1"
    assert event["attempt"] == 2
    assert event["transport_attempts"] == 1
    assert event["transport_retries"] == 0
    assert event["transport_evidence_complete"] is True
    serialized = json.dumps(event)
    assert "secret" not in serialized
    assert not ({"args", "result", "stdout"} & event.keys())


def test_trace_projector_drops_secret_shaped_and_absolute_metadata():
    event = project_trace_event(
        _state(),
        "run_finished",
        {
            "reason": "sk-sensitive123",
            "stop_reason": "/private/workspace",
            "status": "completed",
        },
        created_at="2026-07-12T00:00:00Z",
    )

    assert event["status"] == "completed"
    assert "reason" not in event
    assert "stop_reason" not in event


def test_trace_projector_preserves_transport_evidence():
    event = project_trace_event(
        _state(),
        "model_turn",
        {
            "transport_attempts": 1,
            "transport_retries": 0,
            "transport_evidence_complete": True,
        },
        created_at="2026-07-12T00:00:00Z",
    )

    assert event["transport_attempts"] == 1
    assert event["transport_retries"] == 0
    assert event["transport_evidence_complete"] is True


def test_trace_reader_drops_retired_sandbox_evidence():
    started = project_trace_event(
        _state(),
        "tool_started",
        {"name": "run_shell", "tool_use_id": "tool_1"},
        created_at="2026-07-12T00:00:00Z",
    )
    executed = project_trace_event(
        _state(),
        "tool_executed",
        {
            "name": "run_shell",
            "tool_use_id": "tool_1",
            "tool_status": "ok",
            "sandbox_outcome": "completed",
        },
        created_at="2026-07-12T00:00:01Z",
    )
    finished = project_trace_event(
        _state(),
        "tool_finished",
        {
            "name": "run_shell",
            "tool_use_id": "tool_1",
            "tool_status": "ok",
        },
        created_at="2026-07-12T00:00:02Z",
    )
    terminal = project_trace_event(
        _state(),
        "run_finished",
        {"status": "completed"},
        created_at="2026-07-12T00:00:03Z",
    )

    assert validate_trace([started, executed, finished, terminal]) is not None


def test_report_reader_is_current_only():
    with pytest.raises(RunArtifactError) as exc:
        validate_report({"run_id": "run_1"})
    assert exc.value.status == "migration_required"


def test_report_reader_rejects_non_integer_schema_version():
    report = _report()
    report["format_version"] = 2.0

    with pytest.raises(RunArtifactError, match="schema migration required"):
        validate_report(report)


def test_report_contract_has_no_transitional_or_content_fields():
    report = _report()
    validate_report(report)
    assert set(report) == {
        "record_type", "format_version", "run", "model", "context", "tools",
        "memory", "effects", "integrity", "finalization",
    }
    assert not {
        "final_answer", "task_state", "working_memory", "run_id",
        "completion_usage_totals", "model_execution", "last_request_metadata",
    } & report.keys()


def test_report_reader_rejects_content_metadata_and_invalid_counters():
    report = _report()
    report["context"] = {"prompt": "raw secret"}
    with pytest.raises(RunArtifactError):
        validate_report(report)


    report = _report()
    report["tools"]["calls"] = "1"
    with pytest.raises(RunArtifactError):
        validate_report(report)


@pytest.mark.parametrize(
    ("status", "stop_reason"),
    [
        ("completed", "final_answer_returned"),
        ("stopped", "step_limit_reached"),
        ("stopped", "retry_limit_reached"),
        ("stopped", "interrupted"),
        ("failed", "model_error"),
        ("failed", "persistence_error"),
        ("failed", "runtime_error"),
    ],
)
def test_report_reader_accepts_current_terminal_state_pairs(status, stop_reason):
    report = _report()
    report["run"].update(status=status, stop_reason=stop_reason)

    assert validate_report(report) is report


@pytest.mark.parametrize(
    ("status", "stop_reason"),
    [
        ("running", ""),
        ("unknown", "final_answer_returned"),
        ("completed", "unknown"),
        ("completed", "runtime_error"),
        ("stopped", "model_error"),
        ("failed", "interrupted"),
    ],
)
def test_report_reader_rejects_invalid_terminal_state_pairs(status, stop_reason):
    report = _report()
    report["run"].update(status=status, stop_reason=stop_reason)

    with pytest.raises(RunArtifactError, match="terminal state"):
        validate_report(report)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("run", "stop_reason", "sk-sensitive123"),
        ("model", "transport_attempts", "/private/workspace"),
    ],
)
def test_report_reader_rejects_secret_or_absolute_metadata(
    section,
    field,
    value,
):
    report = _report()
    report[section][field] = value

    with pytest.raises(RunArtifactError, match="unsafe metadata"):
        validate_report(report)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("run", "duration_ms", "not-an-int"),
        ("run", "dirty", "yes"),
        ("model", "evidence_complete", 1),
        ("model", "attempt_origin_counts", []),
        ("tools", "allowed", -1),
    ],
)
def test_report_reader_rejects_wrong_fixed_field_types(section, field, value):
    report = _report()
    report[section][field] = value

    with pytest.raises(RunArtifactError):
        validate_report(report)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "/private/tool.py",
        r"C:\Users\example\tool.py",
        r"\\server\share\tool.py",
    ],
)
def test_report_reader_rejects_unsafe_count_map_keys(key):
    report = _report()
    report["tools"]["name_counts"] = {key: 1}

    with pytest.raises(RunArtifactError, match="unsafe metadata"):
        validate_report(report)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_report_reader_rejects_non_finite_metadata(value):
    report = _report()
    report["context"] = {"compression_ratio": value}

    with pytest.raises(RunArtifactError, match="unsafe metadata"):
        validate_report(report)


def test_report_reader_accepts_finite_float_metadata():
    report = _report()
    report["context"] = {"compression_ratio": 0.5}

    assert validate_report(report) is report


def test_load_summary_requires_all_current_artifacts(tmp_path):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps(_report()), encoding="utf-8")
    (run_dir / "task_state.json").write_text(json.dumps({"run_id": "run_1", "task_id": "task_1"}), encoding="utf-8")
    with pytest.raises(RunArtifactError) as exc:
        load_run_summary(tmp_path, "run_1")
    assert exc.value.status == "incomplete"


def test_load_summary_bounds_each_current_artifact(tmp_path, monkeypatch):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    (run_dir / "report.json").write_text("{}" * 5, encoding="utf-8")
    (run_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "task_state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(observability_module, "MAX_RUN_ARTIFACT_BYTES", 8)

    with pytest.raises(RunArtifactError, match="damaged"):
        load_run_summary(tmp_path, "run_1")


def test_load_summary_latest_uses_same_structured_payload(tmp_path):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    report = _report()
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "task_state.json").write_text(
        json.dumps({
            "run_id": "run_1",
            "task_id": "task_1",
            "status": report["run"]["status"],
            "stop_reason": report["run"]["stop_reason"],
            "attempts": report["model"]["attempts"],
        }),
        encoding="utf-8",
    )
    event = project_trace_event(_state(), "run_finished", {"status": "completed"}, created_at="now")
    (run_dir / "trace.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert load_run_summary(tmp_path, "latest") == report


def test_load_summary_rejects_symlinked_run_directory(tmp_path):
    runs_root = tmp_path / "runs"
    outside = tmp_path / "outside"
    runs_root.mkdir()
    outside.mkdir()
    report = _report()
    (outside / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (outside / "task_state.json").write_text(
        json.dumps({"run_id": "run_1", "task_id": "task_1"}),
        encoding="utf-8",
    )
    event = project_trace_event(
        _state(), "run_finished", {"status": "completed"}, created_at="now"
    )
    (outside / "trace.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (runs_root / "run_1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunArtifactError) as exc:
        load_run_summary(runs_root, "run_1")

    assert exc.value.status == "incomplete"
