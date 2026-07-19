import json

import pytest

from pony.cli.migration import _build_observability
from pony.agent.observability import (
    convert_observability_v2,
    RunArtifactError,
    convert_legacy_observability,
    validate_report,
    validate_trace,
)


def _current_report():
    report, _events = convert_legacy_observability(
        _legacy_report(),
        [{"event": "run_finished", "created_at": "now"}],
        _task_state(),
    )
    return report


def _current_events():
    _report, events = convert_legacy_observability(
        _legacy_report(),
        [{"event": "run_finished", "created_at": "now"}],
        _task_state(),
    )
    return events


def _task_state(**overrides):
    state = {
        "run_id": "run_1",
        "task_id": "task_1",
        "user_request": "inspect",
        "status": "completed",
        "tool_steps": 0,
        "attempts": 1,
        "last_tool": "",
        "stop_reason": "final_answer_returned",
        "final_answer": "done",
        "checkpoint_id": "",
        "resume_status": "",
        "recovery_checkpoint_id": "",
    }
    state.update(overrides)
    return state


def _legacy_report(**overrides):
    report = {
        "run_id": "run_1",
        "task_id": "task_1",
        "status": "completed",
        "stop_reason": "final_answer_returned",
        "attempts": 1,
        "tool_steps": 0,
    }
    report.update(overrides)
    return report


def test_legacy_observability_converter_outputs_current_contract():
    report, events = convert_legacy_observability(
        {
            **_legacy_report(),
            "final_answer": "secret",
        },
        [{"event": "run_finished", "created_at": "now", "final_answer": "secret"}],
        _task_state(),
    )
    validate_report(report, run_id="run_1")
    validate_trace(events, run_id="run_1", task_id="task_1")
    assert "final_answer" not in report


def test_observability_v2_converter_removes_inactive_sandbox_contract():
    report = _current_report()
    report["format_version"] = 2
    report["sandbox"] = {
        "active": False,
        "calls": 0,
        "host_fallback_count": 0,
        "outcome_counts": {},
    }

    converted = convert_observability_v2(report)

    validate_report(converted, run_id="run_1")
    assert converted["format_version"] == 4
    assert "sandbox" not in converted


def test_observability_v2_converter_rejects_active_legacy_sandbox():
    report = _current_report()
    report["format_version"] = 2
    report["sandbox"] = {
        "active": True,
        "calls": 1,
        "host_fallback_count": 0,
        "outcome_counts": {"completed": 1},
    }

    with pytest.raises(RunArtifactError, match="ambiguous"):
        convert_observability_v2(report)


def test_observability_v3_converter_rejects_inactive_sandbox_with_effect_evidence():
    report = _current_report()
    report.update(
        format_version=3,
        sandbox={
            "active": False,
            "implementation": "none",
            "session_state": "not_applicable",
            "engine_profile": "not_applicable",
            "image_digest": "",
            "policy_digest": "",
            "network_mode": "not_applicable",
            "source_mounted": False,
            "state_mounted": False,
            "container_calls": 1,
            "target_started_count": 1,
            "outcome_counts": {"completed": 1},
            "cleanup_failure_count": 0,
            "host_fallback_count": 0,
            "diff": {"candidates": 0, "blocked": 0, "generated": 0},
            "apply_status": "not_applicable",
        },
        recovery={"checkpoint_id": "", "status": "", "review_required": False},
    )
    report["effects"]["recovery_review_required"] = False

    with pytest.raises(RunArtifactError, match="ambiguous"):
        convert_observability_v2(report)


def test_observability_migration_keeps_current_v3_bytes_unchanged(tmp_path):
    source = tmp_path / "source"
    run_dir = source / "run_1"
    run_dir.mkdir(parents=True)
    report = _current_report()
    report_bytes = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    trace_bytes = (
        "\n".join(json.dumps(event, sort_keys=True) for event in _current_events())
        + "\n"
    ).encode()
    task_state_bytes = (json.dumps(_task_state(), sort_keys=True) + "\n").encode()
    (run_dir / "report.json").write_bytes(report_bytes)
    (run_dir / "trace.jsonl").write_bytes(trace_bytes)
    (run_dir / "task_state.json").write_bytes(task_state_bytes)

    migrated = _build_observability(source, tmp_path / "candidate")
    candidate = tmp_path / "candidate" / "run_1"

    assert migrated == 0
    assert (candidate / "report.json").read_bytes() == report_bytes
    assert (candidate / "trace.jsonl").read_bytes() == trace_bytes
    assert (candidate / "task_state.json").read_bytes() == task_state_bytes


def test_observability_migration_upgrades_inactive_v2_tree(tmp_path):
    source = tmp_path / "source"
    run_dir = source / "run_1"
    run_dir.mkdir(parents=True)
    report = _current_report()
    report["format_version"] = 2
    report["sandbox"] = {
        "active": False,
        "calls": 0,
        "host_fallback_count": 0,
        "outcome_counts": {},
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in _current_events()) + "\n",
        encoding="utf-8",
    )
    (run_dir / "task_state.json").write_text(
        json.dumps(_task_state()),
        encoding="utf-8",
    )

    assert _build_observability(source, tmp_path / "candidate") == 1
    converted = json.loads(
        (tmp_path / "candidate" / "run_1" / "report.json").read_text(
            encoding="utf-8"
        )
    )
    assert converted["format_version"] == 4
    assert "sandbox" not in converted


@pytest.mark.parametrize(
    ("task_state", "message"),
    [
        (None, "missing report, trace, or task state"),
        ({"run_id": "other", "task_id": "task_1"}, "task state identity mismatch"),
        ({"run_id": "run_1", "task_id": "other"}, "task state identity mismatch"),
    ],
)
def test_observability_migration_requires_matching_task_state(
    tmp_path, task_state, message
):
    source = tmp_path / "source"
    run_dir = source / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "run_id": "run_1",
                "task_id": "task_1",
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "attempts": 1,
                "tool_steps": 0,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "trace.jsonl").write_text(
        json.dumps({"event": "run_finished", "created_at": "now"}) + "\n",
        encoding="utf-8",
    )
    if task_state is not None:
        (run_dir / "task_state.json").write_text(
            json.dumps(task_state), encoding="utf-8"
        )

    with pytest.raises(RunArtifactError, match=message):
        _build_observability(source, tmp_path / "candidate")


def test_legacy_observability_converter_preserves_tool_summary():
    report, events = convert_legacy_observability(
        _legacy_report(attempts=2, tool_steps=1),
        [
            {
                "event": "tool_started",
                "created_at": "now",
                "name": "read_file",
                "tool_use_id": "tool_1",
            },
            {
                "event": "tool_executed",
                "created_at": "now",
                "name": "read_file",
                "tool_status": "ok",
                "tool_use_id": "tool_1",
            },
            {
                "event": "tool_started",
                "created_at": "now",
                "name": "write_file",
                "tool_use_id": "tool_2",
            },
            {
                "event": "tool_executed",
                "created_at": "now",
                "name": "write_file",
                "tool_status": "rejected",
                "tool_use_id": "tool_2",
            },
            {"event": "run_finished", "created_at": "now"},
        ],
        _task_state(attempts=2, tool_steps=1),
    )

    assert report["tools"] == {
        "calls": 2,
        "allowed": 1,
        "denied": 1,
        "name_counts": {"read_file": 1, "write_file": 1},
        "status_counts": {"ok": 1, "rejected": 1},
    }
    validate_trace(events, run_id="run_1", task_id="task_1")


def test_legacy_observability_converter_preserves_interrupted_tool():
    report, events = convert_legacy_observability(
        _legacy_report(status="stopped", stop_reason="interrupted"),
        [
            {
                "event": "tool_started",
                "created_at": "now",
                "name": "write_file",
                "tool_use_id": "tool_1",
            },
            {
                "event": "tool_interrupted",
                "created_at": "now",
                "name": "write_file",
                "tool_use_id": "tool_1",
                "tool_status": "interrupted",
            },
            {"event": "run_finished", "created_at": "now"},
        ],
        _task_state(status="stopped", stop_reason="interrupted"),
    )

    assert report["tools"]["calls"] == 1
    assert report["tools"]["allowed"] == 1
    assert report["tools"]["denied"] == 0
    assert report["tools"]["status_counts"] == {"interrupted": 1}
    validate_trace(events, run_id="run_1", task_id="task_1")


@pytest.mark.parametrize(
    "task_state",
    [
        {"run_id": "run_1", "task_id": "task_1"},
        _task_state(attempts=2),
    ],
)
def test_legacy_observability_converter_rejects_ambiguous_task_state(task_state):
    with pytest.raises(RunArtifactError, match="ambiguous|attempts mismatch"):
        convert_legacy_observability(
            _legacy_report(),
            [{"event": "run_finished", "created_at": "now"}],
            task_state,
        )


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
def test_legacy_observability_converter_rejects_invalid_terminal_state(
    status,
    stop_reason,
):
    with pytest.raises(RunArtifactError, match="terminal state"):
        convert_legacy_observability(
            _legacy_report(status=status, stop_reason=stop_reason),
            [{"event": "run_finished", "created_at": "now"}],
            _task_state(status=status, stop_reason=stop_reason),
        )


@pytest.mark.parametrize(
    "events",
    [
        [{"event": "tool_executed", "created_at": "now", "name": "read_file"}],
        [{
            "event": "tool_executed",
            "created_at": "now",
            "name": "read_file",
            "tool_status": "unknown",
        }],
        [{
            "event": "tool_started",
            "created_at": "now",
            "name": "read_file",
            "tool_use_id": "tool_1",
        }],
        [
            {
                "event": "tool_started",
                "created_at": "now",
                "name": "read_file",
                "tool_use_id": "tool_1",
            },
            {
                "event": "tool_interrupted",
                "created_at": "now",
                "name": "read_file",
                "tool_use_id": "tool_1",
                "tool_status": "ok",
            },
        ],
        [
            {
                "event": "tool_executed",
                "created_at": "now",
                "name": "read_file",
                "tool_use_id": "tool_1",
                "tool_status": "ok",
            },
            {
                "event": "tool_started",
                "created_at": "now",
                "name": "read_file",
                "tool_use_id": "tool_1",
            },
        ],
    ],
)
def test_legacy_observability_converter_rejects_ambiguous_tool_trace(events):
    with pytest.raises(RunArtifactError, match="ambiguous"):
        convert_legacy_observability(
            _legacy_report(status="failed", stop_reason="interrupted"),
            events,
            _task_state(status="failed", stop_reason="interrupted"),
        )
