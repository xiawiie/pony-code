import json
from types import SimpleNamespace

import pytest

from pony.agent.observability import RunArtifactError, project_trace_event, validate_trace


def test_trace_contract_is_enveloped_and_low_sensitivity():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    events = [
        project_trace_event(state, "run_started", {}, created_at="now"),
        project_trace_event(state, "run_finished", {"final_answer": "secret"}, created_at="now"),
    ]
    validate_trace(events, run_id="run_trace", task_id="task_trace")
    assert "secret" not in json.dumps(events)


def test_trace_policy_decision_mapping_is_projected_and_readable():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    policy_decision = {
        "schema_version": 1,
        "decision": "allow",
        "reason_code": "policy_allowed",
        "effect_class": "read_only",
        "risk_class": "low",
        "evidence_complete": True,
        "approval": {
            "mode": "auto",
            "required": False,
            "outcome": "not_required",
        },
    }
    event = project_trace_event(
        state,
        "run_finished",
        {"policy_decision": policy_decision},
        created_at="now",
    )

    assert event["policy_decision"] == policy_decision
    assert validate_trace([event]) == [event]


def test_trace_security_event_type_is_projected_and_readable():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    started = project_trace_event(
        state,
        "tool_started",
        {"name": "memory_save", "tool_use_id": "tool_1"},
        created_at="now",
    )
    event = project_trace_event(
        state,
        "tool_executed",
        {
            "name": "memory_save",
            "tool_use_id": "tool_1",
            "tool_status": "rejected",
            "security_event_type": "read_only_block",
        },
        created_at="now",
    )
    terminal = project_trace_event(
        state,
        "run_finished",
        {"status": "completed"},
        created_at="now",
    )

    assert event["security_event_type"] == "read_only_block"
    assert validate_trace([started, event, terminal]) == [started, event, terminal]


def _tool_event(state, event, *, status=None, name="read_file", tool_use_id="tool_1"):
    payload = {"name": name, "tool_use_id": tool_use_id}
    if status is not None:
        payload["tool_status"] = status
    return project_trace_event(state, event, payload, created_at="now")


def test_trace_reader_accepts_correlated_tool_lifecycles():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    normal = [
        _tool_event(state, "tool_started"),
        _tool_event(state, "tool_executed", status="ok"),
        _tool_event(state, "tool_finished", status="ok"),
        project_trace_event(state, "run_finished", {}, created_at="now"),
    ]
    interrupted = [
        _tool_event(state, "tool_started"),
        _tool_event(state, "tool_interrupted", status="interrupted"),
        project_trace_event(state, "run_finished", {}, created_at="now"),
    ]

    assert validate_trace(normal) == normal
    assert validate_trace(interrupted) == interrupted


@pytest.mark.parametrize(
    ("event_name", "missing_field"),
    [
        ("tool_started", "name"),
        ("tool_started", "tool_use_id"),
        ("tool_executed", "name"),
        ("tool_executed", "tool_use_id"),
        ("tool_executed", "tool_status"),
        ("tool_interrupted", "tool_status"),
        ("tool_finished", "tool_status"),
    ],
)
def test_trace_reader_requires_event_specific_tool_fields(event_name, missing_field):
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = _tool_event(state, event_name, status="ok")
    event.pop(missing_field)
    events = [event, project_trace_event(state, "run_finished", {}, created_at="now")]

    with pytest.raises(RunArtifactError, match="tool event fields|tool status"):
        validate_trace(events)


@pytest.mark.parametrize(
    "tool_events",
    [
        [("tool_executed", "ok", "read_file", "tool_1")],
        [("tool_started", "ok", "read_file", "tool_1")],
        [
            ("tool_started", None, "read_file", "tool_1"),
            ("tool_executed", "interrupted", "read_file", "tool_1"),
        ],
        [
            ("tool_started", None, "read_file", "tool_1"),
            ("tool_interrupted", "ok", "read_file", "tool_1"),
        ],
        [
            ("tool_started", None, "read_file", "tool_1"),
            ("tool_executed", "ok", "write_file", "tool_1"),
        ],
        [
            ("tool_started", None, "read_file", "tool_1"),
            ("tool_executed", "ok", "read_file", "tool_1"),
            ("tool_finished", "error", "read_file", "tool_1"),
        ],
        [
            ("tool_started", None, "read_file", "tool_1"),
            ("tool_interrupted", "interrupted", "read_file", "tool_1"),
            ("tool_finished", "interrupted", "read_file", "tool_1"),
        ],
    ],
)
def test_trace_reader_rejects_invalid_tool_lifecycle_or_status(tool_events):
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    events = [
        _tool_event(state, event, status=status, name=name, tool_use_id=tool_use_id)
        for event, status, name, tool_use_id in tool_events
    ]
    events.append(project_trace_event(state, "run_finished", {}, created_at="now"))

    with pytest.raises(RunArtifactError, match="tool lifecycle"):
        validate_trace(events)


def test_trace_reader_rejects_tool_event_after_run_terminal():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    events = [
        project_trace_event(state, "run_finished", {}, created_at="now"),
        _tool_event(state, "tool_started"),
    ]

    with pytest.raises(RunArtifactError, match="after run terminal"):
        validate_trace(events)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("prompt", "TOP SECRET"),
        ("stdout", "raw output"),
        ("affected_paths", ["/Users/example/.ssh/id_rsa"]),
        ("affected_paths", [r"C:\Users\example\.ssh\id_rsa"]),
        ("affected_paths", [r"\\server\share\secret"]),
        ("unknown", True),
    ),
)
def test_trace_reader_rejects_unknown_or_sensitive_current_fields(field, value):
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = project_trace_event(
        state,
        "run_finished",
        {"status": "completed"},
        created_at="now",
    )
    event[field] = value

    with pytest.raises(RunArtifactError) as caught:
        validate_trace([event], run_id="run_trace", task_id="task_trace")

    assert caught.value.status == "incomplete"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_id", "evt_not_exact"),
        ("event", 1),
        ("created_at", "/private/time"),
        ("trace_schema_version", True),
    ),
)
def test_trace_reader_rejects_invalid_envelope_types(field, value):
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = project_trace_event(state, "run_finished", {}, created_at="now")
    event[field] = value

    with pytest.raises(RunArtifactError):
        validate_trace([event])


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("duration_ms", "not-an-int"),
        ("cache_hit", 1),
        ("changed_files", -1),
        ("request_metadata", {"api_key": 1}),
        ("request_metadata", {"/private/tool.py": 1}),
        ("request_metadata", {r"C:\Users\example\tool.py": 1}),
        ("request_metadata", {r"\\server\share\tool.py": 1}),
    ),
)
def test_trace_reader_rejects_wrong_types_ranges_and_unsafe_map_keys(
    field,
    value,
):
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = project_trace_event(
        state,
        "run_finished",
        {"status": "completed"},
        created_at="now",
    )
    event[field] = value

    with pytest.raises(RunArtifactError, match="unsafe metadata"):
        validate_trace([event])


def test_trace_projector_drops_values_outside_fixed_field_contract():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = project_trace_event(
        state,
        "run_finished",
        {
            "duration_ms": "not-an-int",
            "cache_hit": 1,
            "changed_files": -1,
            "request_metadata": {"sk-ABCDEF123456": 1},
        },
        created_at="now",
    )

    assert not (
        {"duration_ms", "cache_hit", "changed_files", "request_metadata"}
        & set(event)
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_trace_reader_rejects_non_finite_metadata(value):
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = project_trace_event(
        state,
        "run_finished",
        {"status": "completed"},
        created_at="now",
    )
    event["request_metadata"] = {"compression_ratio": value}

    with pytest.raises(RunArtifactError, match="unsafe metadata"):
        validate_trace([event])


def test_trace_reader_accepts_finite_float_metadata():
    state = SimpleNamespace(run_id="run_trace", task_id="task_trace", attempts=1)
    event = project_trace_event(
        state,
        "run_finished",
        {"request_metadata": {"compression_ratio": 0.5}},
        created_at="now",
    )

    assert validate_trace([event]) == [event]
