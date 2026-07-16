"""Current-only low-sensitivity trace, report, and run-summary contracts."""

import json
import math
import os
import re
import stat
from types import SimpleNamespace
import uuid
from pathlib import Path, PureWindowsPath

from .security import (
    looks_secret_shaped_text,
    private_directory_identity,
    read_private_text,
)

TRACE_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 3
MAX_RUN_ARTIFACT_BYTES = 8 * 1024 * 1024
_TERMINAL_EVENTS = {"run_finished"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TRACE_ID_RE = re.compile(r"^evt_[0-9a-f]{32}$")
_TRACE_ENVELOPE_FIELDS = {
    "trace_schema_version",
    "event_id",
    "event",
    "created_at",
    "run_id",
    "task_id",
}
_TRACE_STRING_FIELDS = {
    "status", "stop_reason", "reason", "kind", "origin", "attempt_origin",
    "action_type", "name", "tool_use_id", "tool_change_id", "checkpoint_id",
    "trigger", "verification_id", "tool_status", "execution_mode", "risk_class",
    "risk_level", "command_risk_class", "effect_class", "reason_code",
    "tool_error_code", "security_event_type", "change_kind",
    "sandbox_outcome", "execution_plane", "cleanup_status",
    "sandbox_wrapper_status", "sandbox_error_code", "sandbox_call_id",
    "execution_plan_digest", "logical_intent_digest", "policy_digest",
}
_TRACE_COUNT_FIELDS = {
    "duration_ms", "run_duration_ms", "attempts", "attempt",
    "tool_steps", "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "changed_files", "stdout_bytes", "stderr_bytes",
}
_TRACE_OPTIONAL_COUNT_FIELDS = {"transport_attempts", "transport_retries"}
_TRACE_BOOL_FIELDS = {
    "cache_hit", "runner_executed", "read_only", "approval_required", "approved",
    "denied", "workspace_changed", "recovery_review_required",
    "target_started", "timed_out", "residue_detected", "container_created",
    "stdout_truncated", "stderr_truncated",
    "transport_evidence_complete",
}
_TRACE_STRING_LIST_FIELDS = {"fields", "finalization_errors", "affected_paths"}
_TRACE_LIST_FIELDS = {"diff_summary"}
_TRACE_MAPPING_FIELDS = {"request_metadata", "policy_decision"}
_TRACE_USAGE_FIELDS = {"completion_usage"}
_TRACE_OPTIONAL_INT_FIELDS = {"exit_code"}
_SAFE_TRACE_FIELDS = (
    _TRACE_STRING_FIELDS
    | _TRACE_COUNT_FIELDS
    | _TRACE_OPTIONAL_COUNT_FIELDS
    | _TRACE_BOOL_FIELDS
    | _TRACE_STRING_LIST_FIELDS
    | _TRACE_LIST_FIELDS
    | _TRACE_MAPPING_FIELDS
    | _TRACE_USAGE_FIELDS
    | _TRACE_OPTIONAL_INT_FIELDS
)
_USAGE_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens", "cache_hit",
}
_FORBIDDEN_METADATA_KEYS = {
    "prompt", "completion", "args", "result", "stdout", "stderr",
    "query", "body", "content", "final_answer", "working_memory",
}
_TOOL_STATUSES = {"ok", "error", "partial_success", "rejected", "interrupted"}
_TERMINAL_STATE_PAIRS = {
    ("completed", "final_answer_returned"),
    ("stopped", "step_limit_reached"),
    ("stopped", "retry_limit_reached"),
    ("stopped", "interrupted"),
    ("failed", "model_error"),
    ("failed", "persistence_error"),
    ("failed", "runtime_error"),
}


class RunArtifactError(ValueError):
    """A run artifact is not readable under the current contract."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def project_trace_event(task_state, event, payload, *, created_at):
    """Build the safe Trace v1 envelope without copying free-form content."""
    projected = {
        key: value for key, value in dict(payload or {}).items()
        if key in _SAFE_TRACE_FIELDS and _trace_field_safe(key, value)
    }
    projected.update({
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "event_id": "evt_" + uuid.uuid4().hex,
        "event": str(event),
        "created_at": str(created_at),
        "run_id": str(task_state.run_id),
        "task_id": str(task_state.task_id),
    })
    attempt = getattr(task_state, "attempts", 0)
    if (str(event).startswith("model_") or event in {"prompt_built", "tool_executed"}) and attempt:
        projected["attempt"] = attempt
    return projected


def _safe_string(value, *, max_length=200):
    return (
        type(value) is str
        and len(value) <= max_length
        and not value.casefold().startswith(("/", "file://"))
        and not PureWindowsPath(value).is_absolute()
        and not looks_secret_shaped_text(value)
    )


def _safe_mapping_key(value):
    return (
        _safe_string(value, max_length=100)
        and value.casefold() not in _FORBIDDEN_METADATA_KEYS
    )


def _safe_metadata(value, *, key=""):
    if str(key).casefold() in _FORBIDDEN_METADATA_KEYS:
        return False
    if value is None or type(value) in {bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, str):
        return _safe_string(value)
    if isinstance(value, list):
        return len(value) <= 100 and all(_safe_metadata(item, key=key) for item in value)
    if isinstance(value, dict):
        return len(value) <= 100 and all(
            _safe_mapping_key(child_key)
            and _safe_metadata(item, key=child_key)
            for child_key, item in value.items()
        )
    return False


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def _usage_metadata(value, *, exact, allow_none=False):
    if not isinstance(value, dict):
        return False
    keys = set(value)
    valid_keys = keys == _USAGE_FIELDS if exact else keys <= _USAGE_FIELDS
    if not valid_keys:
        return False
    return all(
        (type(item) is bool)
        if key == "cache_hit"
        else (item is None and allow_none) or _nonnegative_int(item)
        for key, item in value.items()
    )


def _trace_field_safe(key, value):
    if key in _TRACE_STRING_FIELDS:
        return _safe_string(value)
    if key in _TRACE_COUNT_FIELDS:
        return _nonnegative_int(value)
    if key in _TRACE_OPTIONAL_COUNT_FIELDS:
        return value is None or _nonnegative_int(value)
    if key in _TRACE_BOOL_FIELDS:
        return type(value) is bool
    if key in _TRACE_STRING_LIST_FIELDS:
        return (
            isinstance(value, list)
            and len(value) <= 100
            and all(_safe_string(item) for item in value)
        )
    if key in _TRACE_LIST_FIELDS:
        return isinstance(value, list) and _safe_metadata(value, key=key)
    if key in _TRACE_MAPPING_FIELDS:
        return isinstance(value, dict) and _safe_metadata(value, key=key)
    if key in _TRACE_USAGE_FIELDS:
        return _usage_metadata(value, exact=False, allow_none=True)
    if key in _TRACE_OPTIONAL_INT_FIELDS:
        return value is None or type(value) is int
    return False


def validate_trace(events, *, run_id=None, task_id=None):
    if not isinstance(events, list) or not events:
        raise RunArtifactError("incomplete", "trace is missing or empty")
    event_ids = set()
    terminal = 0
    run_finished = False
    pending_tools = {}
    terminal_tools = {}
    finished_tools = set()
    for event in events:
        if not isinstance(event, dict) or not _TRACE_ENVELOPE_FIELDS.issubset(event):
            raise RunArtifactError("migration_required", "trace uses a legacy contract")
        if set(event) - _TRACE_ENVELOPE_FIELDS - _SAFE_TRACE_FIELDS:
            raise RunArtifactError("incomplete", "trace contains unknown fields")
        if (
            type(event["trace_schema_version"]) is not int
            or event["trace_schema_version"] != TRACE_SCHEMA_VERSION
        ):
            raise RunArtifactError("migration_required", "trace schema migration required")
        if (
            not isinstance(event["event_id"], str)
            or _TRACE_ID_RE.fullmatch(event["event_id"]) is None
            or any(
                not isinstance(event[field], str)
                or not event[field]
                or not _safe_metadata(event[field], key=field)
                for field in ("event", "created_at", "run_id", "task_id")
            )
            or any(
                not _trace_field_safe(key, value)
                for key, value in event.items()
                if key not in _TRACE_ENVELOPE_FIELDS
            )
        ):
            raise RunArtifactError("incomplete", "trace contains unsafe metadata")
        if event["event_id"] in event_ids:
            raise RunArtifactError("incomplete", "duplicate trace event id")
        event_ids.add(event["event_id"])
        if run_id and event["run_id"] != run_id:
            raise RunArtifactError("incomplete", "trace run id mismatch")
        if task_id and event["task_id"] != task_id:
            raise RunArtifactError("incomplete", "trace task id mismatch")
        event_name = event["event"]
        if event_name in _TERMINAL_EVENTS:
            terminal += 1
            run_finished = True
            continue
        if event_name not in {
            "tool_started", "tool_executed", "tool_interrupted", "tool_finished",
        }:
            continue
        if run_finished:
            raise RunArtifactError("incomplete", "trace contains tool event after run terminal")
        tool_use_id = event.get("tool_use_id", "")
        name = event.get("name", "")
        if not tool_use_id or not name:
            raise RunArtifactError("incomplete", "trace tool event fields are incomplete")
        if "sandbox_outcome" in event:
            if (
                not {
                    "execution_plane",
                    "cleanup_status",
                    "target_started",
                }.issubset(event)
                or event["execution_plane"] not in {"sandbox", "host", "unknown"}
                or not event["cleanup_status"]
                or event["execution_plane"] == "sandbox"
                and not {
                    "runner_executed",
                    "execution_plan_digest",
                    "logical_intent_digest",
                    "policy_digest",
                }.issubset(event)
            ):
                raise RunArtifactError(
                    "incomplete",
                    "trace sandbox fields are incomplete",
                )
        if event_name == "tool_started":
            if (
                "tool_status" in event
                or tool_use_id in pending_tools
                or tool_use_id in terminal_tools
            ):
                raise RunArtifactError("incomplete", "trace tool lifecycle is invalid")
            pending_tools[tool_use_id] = name
            continue
        tool_status = event.get("tool_status", "")
        if tool_status not in _TOOL_STATUSES:
            raise RunArtifactError("incomplete", "trace tool status is invalid")
        if event_name == "tool_finished":
            terminal_event = terminal_tools.get(tool_use_id)
            if (
                terminal_event is None
                or terminal_event != (name, tool_status, "tool_executed")
                or tool_use_id in finished_tools
            ):
                raise RunArtifactError("incomplete", "trace tool lifecycle is invalid")
            finished_tools.add(tool_use_id)
            continue
        if (
            tool_use_id in terminal_tools
            or pending_tools.pop(tool_use_id, None) != name
            or event_name == "tool_interrupted" and tool_status != "interrupted"
            or event_name == "tool_executed" and tool_status == "interrupted"
        ):
            raise RunArtifactError("incomplete", "trace tool lifecycle is invalid")
        terminal_tools[tool_use_id] = (name, tool_status, event_name)
    if terminal != 1:
        raise RunArtifactError("incomplete", "trace must contain exactly one terminal event")
    if pending_tools:
        raise RunArtifactError("incomplete", "trace tool lifecycle is incomplete")
    return events


def _count_map(value):
    return (
        isinstance(value, dict)
        and len(value) <= 100
        and all(
            _safe_mapping_key(key) and _nonnegative_int(item)
            for key, item in value.items()
        )
    )


def validate_report(report, *, run_id=None):
    required = {
        "record_type", "format_version", "run", "model", "context", "tools",
        "memory", "sandbox", "effects", "recovery", "integrity", "finalization",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise RunArtifactError("migration_required", "report uses a legacy contract")
    if (
        type(report["record_type"]) is not str
        or report["record_type"] != "run_report"
        or type(report["format_version"]) is not int
        or report["format_version"] != REPORT_SCHEMA_VERSION
    ):
        raise RunArtifactError("migration_required", "report schema migration required")
    section_fields = {
        "run": {"run_id", "task_id", "status", "stop_reason", "duration_ms", "commit", "dirty"},
        "model": {
            "attempts", "turns", "failures", "retries", "transport_attempts",
            "transport_retries", "evidence_complete", "attempt_origin_counts",
            "failure_reason_counts", "usage",
        },
        "tools": {"calls", "allowed", "denied", "name_counts", "status_counts"},
        "memory": {"recall_candidates", "recall_selected", "filter_counts"},
        "sandbox": {
            "active", "implementation", "session_state", "engine_profile",
            "image_digest", "policy_digest", "network_mode", "source_mounted",
            "state_mounted", "container_calls", "target_started_count",
            "outcome_counts", "cleanup_failure_count", "host_fallback_count",
            "diff", "apply_status",
        },
        "effects": {"changed_files", "partial_successes", "recovery_review_required"},
        "recovery": {"checkpoint_id", "status", "review_required"},
        "integrity": {"writer", "terminal_event_expected"},
        "finalization": {"status", "error_count"},
    }
    for section, fields in section_fields.items():
        if not isinstance(report[section], dict) or set(report[section]) != fields:
            raise RunArtifactError("migration_required", f"report {section} schema mismatch")
    if not _safe_metadata(report, key="report"):
        raise RunArtifactError("incomplete", "report contains unsafe metadata")
    if not _usage_metadata(report["model"]["usage"], exact=True):
        raise RunArtifactError("migration_required", "report usage schema mismatch")
    if not isinstance(report["context"], dict) or not _safe_metadata(report["context"], key="context"):
        raise RunArtifactError("migration_required", "report context schema mismatch")
    counters = {
        "model": ("attempts", "turns", "failures", "retries"),
        "tools": ("calls", "allowed", "denied"),
        "memory": ("recall_candidates", "recall_selected"),
        "sandbox": (
            "container_calls", "target_started_count", "cleanup_failure_count",
            "host_fallback_count",
        ),
        "effects": ("changed_files", "partial_successes"),
        "finalization": ("error_count",),
    }
    for section, fields in counters.items():
        if any(not _nonnegative_int(report[section][field]) for field in fields):
            raise RunArtifactError("incomplete", f"report {section} counter invalid")
    for field in ("transport_attempts", "transport_retries"):
        value = report["model"][field]
        if value is not None and not _nonnegative_int(value):
            raise RunArtifactError("incomplete", f"report {field} invalid")
    run = report["run"]
    if (
        any(type(run[field]) is not str for field in ("run_id", "task_id", "status", "stop_reason", "commit"))
        or not all(run[field] for field in ("run_id", "task_id", "status"))
        or not _nonnegative_int(run["duration_ms"])
        or type(run["dirty"]) is not bool
    ):
        raise RunArtifactError("incomplete", "report run fields invalid")
    if (run["status"], run["stop_reason"]) not in _TERMINAL_STATE_PAIRS:
        raise RunArtifactError("incomplete", "report terminal state is invalid")
    model = report["model"]
    if (
        type(model["evidence_complete"]) is not bool
        or not _count_map(model["attempt_origin_counts"])
        or not _count_map(model["failure_reason_counts"])
    ):
        raise RunArtifactError("incomplete", "report model fields invalid")
    tools = report["tools"]
    if (
        not _count_map(tools["name_counts"])
        or not _count_map(tools["status_counts"])
        or tools["allowed"] + tools["denied"] != tools["calls"]
    ):
        raise RunArtifactError("incomplete", "report tool fields invalid")
    if not _count_map(report["memory"]["filter_counts"]):
        raise RunArtifactError("incomplete", "report memory fields invalid")
    sandbox = report["sandbox"]
    sandbox_diff = sandbox["diff"]
    public_digest = re.compile(r"^sha256:[0-9a-f]{16}$")
    if (
        type(sandbox["active"]) is not bool
        or any(
            type(sandbox[field]) is not str
            for field in (
                "implementation", "session_state", "engine_profile",
                "image_digest", "policy_digest", "network_mode", "apply_status",
            )
        )
        or type(sandbox["source_mounted"]) is not bool
        or type(sandbox["state_mounted"]) is not bool
        or sandbox["source_mounted"]
        or sandbox["state_mounted"]
        or not _count_map(sandbox["outcome_counts"])
        or not isinstance(sandbox_diff, dict)
        or set(sandbox_diff) != {"candidates", "blocked", "generated"}
        or any(not _nonnegative_int(value) for value in sandbox_diff.values())
        or sandbox["target_started_count"] > sandbox["container_calls"]
        or sandbox["cleanup_failure_count"] > sandbox["container_calls"]
        or sandbox["host_fallback_count"] > sandbox["container_calls"]
        or sum(sandbox["outcome_counts"].values()) != sandbox["container_calls"]
    ):
        raise RunArtifactError("incomplete", "report sandbox fields invalid")
    if sandbox["active"]:
        if (
            sandbox["implementation"] != "docker_container"
            or sandbox["session_state"] == "not_applicable"
            or sandbox["engine_profile"] == "not_applicable"
            or public_digest.fullmatch(sandbox["image_digest"]) is None
            or public_digest.fullmatch(sandbox["policy_digest"]) is None
            or sandbox["network_mode"] != "none"
            or sandbox["apply_status"] == "not_applicable"
        ):
            raise RunArtifactError("incomplete", "report sandbox fields invalid")
    elif sandbox != {
        "active": False,
        "implementation": "none",
        "session_state": "not_applicable",
        "engine_profile": "not_applicable",
        "image_digest": "",
        "policy_digest": "",
        "network_mode": "not_applicable",
        "source_mounted": False,
        "state_mounted": False,
        "container_calls": 0,
        "target_started_count": 0,
        "outcome_counts": {},
        "cleanup_failure_count": 0,
        "host_fallback_count": 0,
        "diff": {"candidates": 0, "blocked": 0, "generated": 0},
        "apply_status": "not_applicable",
    }:
        raise RunArtifactError("incomplete", "report sandbox fields invalid")
    effects = report["effects"]
    recovery = report["recovery"]
    integrity = report["integrity"]
    finalization = report["finalization"]
    if type(effects["recovery_review_required"]) is not bool:
        raise RunArtifactError("incomplete", "report effects fields invalid")
    if (
        any(type(recovery[field]) is not str for field in ("checkpoint_id", "status"))
        or type(recovery["review_required"]) is not bool
    ):
        raise RunArtifactError("incomplete", "report recovery fields invalid")
    if (
        type(integrity["writer"]) is not str
        or not integrity["writer"]
        or type(integrity["terminal_event_expected"]) is not bool
    ):
        raise RunArtifactError("incomplete", "report integrity fields invalid")
    if type(finalization["status"]) is not str or not finalization["status"]:
        raise RunArtifactError("incomplete", "report finalization fields invalid")
    if run_id and report["run"].get("run_id") != run_id:
        raise RunArtifactError("incomplete", "report run id mismatch")
    return report


def _decode_json(text):
    def object_from_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate artifact key")
            value[key] = item
        return value

    return json.loads(text, object_pairs_hook=object_from_pairs)


def load_run_artifacts(runs_root, run_id):
    root = Path(runs_root)
    latest = run_id == "latest"
    if latest:
        try:
            root.lstat()
        except FileNotFoundError:
            raise FileNotFoundError("no runs") from None
    try:
        root_identity = private_directory_identity(root)
        if latest:
            candidates = []
            with os.scandir(root) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and _RUN_ID_RE.fullmatch(entry.name):
                        candidates.append((info.st_mtime_ns, entry.name))
            if not candidates:
                raise FileNotFoundError("no runs")
            run_id = max(candidates)[1]
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("invalid run id")
        run_dir = root / run_id
        run_identity = private_directory_identity(run_dir)
        if private_directory_identity(root) != root_identity:
            raise ValueError("run root changed")

        def read(name):
            return read_private_text(
                run_dir / name,
                trusted_root=run_dir,
                trusted_root_identity=run_identity,
                max_bytes=MAX_RUN_ARTIFACT_BYTES,
            )

        report = _decode_json(read("report.json"))
        events = [
            _decode_json(line)
            for line in read("trace.jsonl").splitlines()
            if line.strip()
        ]
        task = _decode_json(read("task_state.json"))
    except FileNotFoundError as exc:
        if latest and str(exc) == "no runs":
            raise
        raise RunArtifactError("incomplete", "required run artifact is missing") from exc
    except (OSError, RuntimeError, UnicodeDecodeError, ValueError) as exc:
        raise RunArtifactError("incomplete", "run artifact is damaged") from exc
    validate_report(report, run_id=run_id)
    task_id = report["run"].get("task_id")
    if (
        not isinstance(task, dict)
        or task.get("run_id") != run_id
        or task.get("task_id") != task_id
    ):
        raise RunArtifactError("incomplete", "task state identity mismatch")
    validate_trace(events, run_id=run_id, task_id=task_id)
    if (
        task.get("status") != report["run"]["status"]
        or task.get("stop_reason") != report["run"]["stop_reason"]
        or task.get("attempts") != report["model"]["attempts"]
    ):
        raise RunArtifactError("incomplete", "task state summary mismatch")
    tool_events = [
        event
        for event in events
        if event["event"] in {"tool_executed", "tool_interrupted"}
    ]
    name_counts = {}
    status_counts = {}
    for event in tool_events:
        name = event.get("name", "")
        status = event.get("tool_status", "")
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
    if (
        len(tool_events) != report["tools"]["calls"]
        or name_counts != report["tools"]["name_counts"]
        or status_counts != report["tools"]["status_counts"]
    ):
        raise RunArtifactError("incomplete", "trace tool summary mismatch")
    return report, events


def load_run_summary(runs_root, run_id):
    report, _events = load_run_artifacts(runs_root, run_id)
    return report


def convert_legacy_observability(report, events, task_state):
    """Pure MIG-OBS converter; callers must provide transactional cutover."""
    if not isinstance(report, dict) or "run_id" not in report or "task_id" not in report:
        raise RunArtifactError("migration_required", "legacy report is ambiguous")
    if (
        isinstance(task_state, dict)
        and (
            task_state.get("run_id") != report["run_id"]
            or task_state.get("task_id") != report["task_id"]
        )
    ):
        raise RunArtifactError("incomplete", "task state identity mismatch")
    task_string_fields = {
        "run_id", "task_id", "user_request", "status", "last_tool",
        "stop_reason", "final_answer", "checkpoint_id", "resume_status",
        "recovery_checkpoint_id",
    }
    task_count_fields = {"attempts", "tool_steps"}
    required_task_fields = task_string_fields | task_count_fields
    if (
        not isinstance(task_state, dict)
        or set(task_state) != required_task_fields
        or any(
            type(task_state[field]) is not str
            for field in task_string_fields
        )
        or any(
            type(task_state[field]) is not int or task_state[field] < 0
            for field in task_count_fields
        )
    ):
        raise RunArtifactError("migration_required", "legacy task state is ambiguous")
    if (task_state["status"], task_state["stop_reason"]) not in _TERMINAL_STATE_PAIRS:
        raise RunArtifactError("migration_required", "legacy task state terminal state is ambiguous")
    for field in ("run_id", "task_id", "status", "stop_reason", "attempts", "tool_steps"):
        if report.get(field) != task_state[field]:
            raise RunArtifactError("incomplete", f"legacy {field} mismatch")
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_hit": False,
        **dict(report.get("completion_usage_totals") or {}),
    }
    execution = dict(report.get("model_execution") or {})
    task = SimpleNamespace(
        run_id=task_state["run_id"],
        task_id=task_state["task_id"],
        attempts=task_state["attempts"],
    )
    legacy_events = list(events or [])
    safe_events = [
        project_trace_event(
            task,
            event.get("event", "legacy_event"),
            event,
            created_at=event.get("created_at", ""),
        )
        for event in legacy_events
        if isinstance(event, dict)
    ]
    if len(safe_events) != len(legacy_events):
        raise RunArtifactError("migration_required", "legacy trace is ambiguous")
    tool_events = [
        event
        for event in safe_events
        if event["event"] in {"tool_executed", "tool_interrupted"}
    ]
    try:
        validate_trace(
            safe_events,
            run_id=task_state["run_id"],
            task_id=task_state["task_id"],
        )
    except RunArtifactError:
        raise RunArtifactError("migration_required", "legacy tool trace is ambiguous")
    name_counts = {}
    status_counts = {}
    for event in tool_events:
        name = event["name"]
        status = event["tool_status"]
        name_counts[name] = name_counts.get(name, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
    denied = status_counts.get("rejected", 0)
    allowed = len(tool_events) - denied
    consumed_steps = sum(
        event["event"] == "tool_executed"
        and event["tool_status"] != "rejected"
        for event in tool_events
    )
    if consumed_steps != task_state["tool_steps"]:
        raise RunArtifactError("incomplete", "legacy tool trace count mismatch")
    converted = {
        "record_type": "run_report",
        "format_version": REPORT_SCHEMA_VERSION,
        "run": {
            "run_id": task_state["run_id"],
            "task_id": task_state["task_id"],
            "status": task_state["status"],
            "stop_reason": task_state["stop_reason"],
            "duration_ms": int((execution.get("run_duration_ms", 0) or 0)),
            "commit": "",
            "dirty": False,
        },
        "model": {
            "attempts": task_state["attempts"],
            "turns": int(execution.get("model_turns", 0) or 0),
            "failures": int(execution.get("model_failures", 0) or 0),
            "retries": int(execution.get("model_retries", 0) or 0),
            "transport_attempts": execution.get("transport_attempts"),
            "transport_retries": execution.get("transport_retries"),
            "evidence_complete": bool(execution.get("transport_evidence_complete", False)),
            "attempt_origin_counts": dict(execution.get("attempt_origin_counts") or {}),
            "failure_reason_counts": dict(execution.get("failure_reason_counts") or {}),
            "usage": usage,
        },
        "context": dict(report.get("last_request_metadata") or {}),
        "tools": {
            "calls": len(tool_events),
            "allowed": allowed,
            "denied": denied,
            "name_counts": name_counts,
            "status_counts": status_counts,
        },
        "memory": {"recall_candidates": 0, "recall_selected": 0, "filter_counts": {}},
        "sandbox": {
            "active": False,
            "implementation": "none",
            "session_state": "not_applicable",
            "engine_profile": "not_applicable",
            "image_digest": "",
            "policy_digest": "",
            "network_mode": "not_applicable",
            "source_mounted": False,
            "state_mounted": False,
            "container_calls": 0,
            "target_started_count": 0,
            "outcome_counts": {},
            "cleanup_failure_count": 0,
            "host_fallback_count": 0,
            "diff": {"candidates": 0, "blocked": 0, "generated": 0},
            "apply_status": "not_applicable",
        },
        "effects": {"changed_files": 0, "partial_successes": 0, "recovery_review_required": False},
        "recovery": {
            "checkpoint_id": str(report.get("checkpoint_id", "")),
            "status": str(report.get("resume_status", "")),
            "review_required": False,
        },
        "integrity": {"writer": "migration", "terminal_event_expected": True},
        "finalization": {"status": "migrated", "error_count": 0},
    }
    validate_report(converted)
    return converted, safe_events


def convert_observability_v2(report):
    """Upgrade an inactive Report v2 to the exact current Report contract."""
    if (
        not isinstance(report, dict)
        or report.get("record_type") != "run_report"
        or report.get("format_version") != 2
        or set(report.get("sandbox", {}))
        != {"active", "calls", "host_fallback_count", "outcome_counts"}
        or report["sandbox"] != {
            "active": False,
            "calls": 0,
            "host_fallback_count": 0,
            "outcome_counts": {},
        }
    ):
        raise RunArtifactError(
            "migration_required",
            "Report v2 sandbox evidence is ambiguous",
        )
    converted = dict(report)
    converted["format_version"] = REPORT_SCHEMA_VERSION
    converted["sandbox"] = {
        "active": False,
        "implementation": "none",
        "session_state": "not_applicable",
        "engine_profile": "not_applicable",
        "image_digest": "",
        "policy_digest": "",
        "network_mode": "not_applicable",
        "source_mounted": False,
        "state_mounted": False,
        "container_calls": 0,
        "target_started_count": 0,
        "outcome_counts": {},
        "cleanup_failure_count": 0,
        "host_fallback_count": 0,
        "diff": {"candidates": 0, "blocked": 0, "generated": 0},
        "apply_status": "not_applicable",
    }
    validate_report(converted)
    return converted


def render_summary_text(summary):
    run = summary["run"]
    model = summary["model"]
    usage = model["usage"]
    tools = summary["tools"]
    effects = summary["effects"]
    return "\n".join((
        f"Run {run['run_id']}",
        f"status: {run['status']} ({run.get('stop_reason') or '-'})",
        f"duration_ms: {run.get('duration_ms', 0)}",
        f"model: {model.get('attempts', 0)} attempts, {usage.get('input_tokens', 0)} input / {usage.get('output_tokens', 0)} output tokens",
        f"tools: {tools.get('calls', 0)} calls, {tools.get('denied', 0)} denied",
        f"effects: {effects.get('changed_files', 0)} changed files, recovery_review_required={str(bool(effects.get('recovery_review_required'))).lower()}",
    ))
