"""Decision-focused live evaluation for compaction value and Provider latency."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import time

from pony.agent.observability import load_run_artifacts
from pony.runtime.application import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext

from .metrics_common import _utc_timestamp
from .provider_benchmark import _client_from_target, _resolve_benchmark_target


EFFICIENCY_EVALUATION_FORMAT_VERSION = 2
TTFT_STATUS = "unavailable_non_streaming"
_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens")
_ACTIVE_FACTS = (
    "GOAL-7Q4M",
    "DECISION-CURRENT-9X2P",
    "CONSTRAINT-6D3R",
    "src/engine.py",
    "ERROR-E214",
    "TASK-U5N7",
    "NEXT-R8C2",
)
_SUPERSEDED_FACTS = ("DECISION-OLD-1A8K", "DECISION-PHASE1-4J6V")
_FOLLOWUPS = (
    ("state_snapshot", "state", _ACTIVE_FACTS, _SUPERSEDED_FACTS),
    (
        "active_decision",
        "decision",
        ("DECISION-CURRENT-9X2P",),
        _SUPERSEDED_FACTS,
    ),
    ("goal_constraint", "constraint", ("GOAL-7Q4M", "CONSTRAINT-6D3R"), ()),
    ("file_error", "failure", ("src/engine.py", "ERROR-E214"), ()),
    ("unfinished_next", "continuation", ("TASK-U5N7", "NEXT-R8C2"), ()),
    ("continuation_snapshot", "state", _ACTIVE_FACTS, _SUPERSEDED_FACTS),
)


def _provider_call_kind(system):
    text = "\n".join(
        str(block.get("text", "")) for block in system if isinstance(block, dict)
    )
    return "compaction" if "You compact coding-agent history" in text else "workload"


class _TimedProvider:
    """Record bounded timing/usage evidence without retaining payloads or responses."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        call = {
            "kind": _provider_call_kind(system),
            "completed": False,
            "provider_complete_ms": 0.0,
            "usage": {},
            "transport_attempts": None,
            "transport_retries": None,
        }
        started = time.perf_counter_ns()
        self.calls.append(call)
        try:
            response = self._inner.complete(
                system=system,
                tools=tools,
                messages=messages,
                max_tokens=max_tokens,
                cache_breakpoints=cache_breakpoints,
            )
            call["completed"] = True
            call["usage"] = dict(getattr(response, "usage", None) or {})
            return response
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            raise
        finally:
            call["provider_complete_ms"] = round(
                (time.perf_counter_ns() - started) / 1_000_000,
                3,
            )
            attempts = getattr(self._inner, "last_transport_attempts", None)
            call["transport_attempts"] = attempts if type(attempts) is int else None
            call["transport_retries"] = (
                max(0, attempts - 1) if type(attempts) is int else None
            )


def _usage_complete(calls):
    calls = list(calls)
    return bool(calls) and all(
        call.get("completed") is True
        and isinstance(call.get("usage"), dict)
        and all(
            type(call["usage"].get(key)) is int
            for key in ("input_tokens", "output_tokens")
        )
        for call in calls
    )


def _usage_total(calls):
    totals = {key: 0 for key in _USAGE_KEYS}
    for call in calls:
        usage = call.get("usage") if isinstance(call, dict) else None
        if not isinstance(usage, dict):
            continue
        for key in totals:
            value = usage.get(key)
            if type(value) is int:
                totals[key] += value
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def _provider_failure_evidence(calls):
    failed_calls = [call for call in calls if not call["completed"]]
    return {
        "provider_failures": len(failed_calls),
        "provider_error_types": sorted(
            {
                call["error_type"]
                for call in failed_calls
                if call.get("error_type")
            }
        ),
    }


def _latency_stats(values):
    values = sorted(float(value) for value in values)
    if not values:
        return {"count": 0, "min_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}

    def percentile(q):
        if len(values) == 1:
            return values[0]
        position = (len(values) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {
        "count": len(values),
        "min_ms": round(values[0], 3),
        "p50_ms": round(percentile(0.50), 3),
        "p95_ms": round(percentile(0.95), 3),
        "max_ms": round(values[-1], 3),
    }


def _message(role, text, index):
    return {
        "role": role,
        "content": text,
        "_pony_meta": {"created_at": f"2026-08-05T08:{index:02d}:00+00:00"},
    }


def _filler(label, index):
    sentence = (
        f"{label} investigation note {index}: routine evidence was checked and is not "
        "an active decision, blocker, file, error, or next step. "
    )
    return sentence * 12


def _history_phases():
    phase_one = [
        _message(
            "user",
            "The active goal is GOAL-7Q4M and constraint CONSTRAINT-6D3R. "
            + _filler("phase-one", 0),
            0,
        ),
        _message(
            "assistant",
            "The initial decision DECISION-OLD-1A8K was recorded. "
            + _filler("phase-one", 1),
            1,
        ),
        _message(
            "user",
            "Work concerns src/engine.py; the observed failure is ERROR-E214. "
            + _filler("phase-one", 2),
            2,
        ),
        _message(
            "assistant",
            "Unfinished work is TASK-U5N7 and the next step is NEXT-R8C2. "
            + _filler("phase-one", 3),
            3,
        ),
        _message("user", _filler("phase-one", 4), 4),
        _message("assistant", _filler("phase-one", 5), 5),
        _message(
            "user",
            "DECISION-OLD-1A8K is superseded by DECISION-PHASE1-4J6V. "
            + _filler("phase-one", 6),
            6,
        ),
        _message("assistant", _filler("phase-one-tail", 7), 7),
    ]
    phase_two = [
        _message("user", _filler("phase-two", 8), 8),
        _message(
            "assistant",
            "DECISION-PHASE1-4J6V is now superseded. The only active decision is "
            "DECISION-CURRENT-9X2P. "
            + _filler("phase-two", 9),
            9,
        ),
        _message(
            "user",
            "GOAL-7Q4M and CONSTRAINT-6D3R remain active. " + _filler("phase-two", 10),
            10,
        ),
        _message(
            "assistant",
            "src/engine.py still fails with ERROR-E214. " + _filler("phase-two", 11),
            11,
        ),
        _message(
            "user",
            "TASK-U5N7 remains unfinished; perform NEXT-R8C2 next. "
            + _filler("phase-two", 12),
            12,
        ),
        _message("assistant", _filler("phase-two", 13), 13),
        _message("user", _filler("recent-tail", 14), 14),
        _message("assistant", "Recent tail contains no state changes.", 15),
    ]
    return phase_one, phase_two


def _followup_prompt(case_id):
    if case_id == "state_snapshot":
        return (
            "Return the active goal, decision, constraint, file, error, unfinished task, "
            "and next step as opaque identifiers only. Do not return superseded decisions."
        )
    if case_id == "active_decision":
        return "Return only the currently active decision identifier."
    if case_id == "goal_constraint":
        return "Return only the active goal and constraint identifiers."
    if case_id == "file_error":
        return "Return only the active file path and error identifier."
    if case_id == "unfinished_next":
        return "Return only the unfinished task and next-step identifiers."
    return "Return the complete active state as opaque identifiers only; omit superseded decisions."


def _build_agent(root, recorder, store, *, session=None):
    return Pony(
        model_client=recorder,
        workspace=WorkspaceContext.build(root),
        session_store=store,
        session=session,
        options=RuntimeOptions(
            project_trusted=True,
            read_only=True,
            max_steps=4,
            max_output_tokens=512,
            allowed_tools=("read_file",),
        ),
    )


def _prepare_condition(root, client_factory, *, compacted, repeated):
    root.mkdir(parents=True)
    (root / "README.md").write_text("evaluation fixture\n", encoding="utf-8")
    store = SessionStore(root / ".pony" / "sessions")
    recorder = _TimedProvider(client_factory())
    agent = _build_agent(root, recorder, store)
    phase_one, phase_two = _history_phases()
    agent.session["messages"].extend(phase_one)
    store.save(agent.session)
    compactions = []
    if compacted and repeated:
        result = agent.compact_session(
            reason="efficiency_evaluation",
            focus="Preserve active state and supersession exactly; keep the summary under 500 tokens.",
            keep_recent_tokens=128,
        )
        compactions.append(_compaction_result(result))
        agent.session = store.load(agent.session["id"])
    agent.session["messages"].extend(phase_two)
    store.save(agent.session)
    if compacted:
        result = agent.compact_session(
            reason="efficiency_evaluation",
            focus="Preserve active state and supersession exactly; keep the summary under 500 tokens.",
            keep_recent_tokens=128,
        )
        compactions.append(_compaction_result(result))
    session = store.load(agent.session["id"])
    resumed = _build_agent(root, recorder, store, session=session)
    return resumed, recorder, compactions, len(recorder.calls)


def _compaction_result(result):
    return {
        "tokens_before": result.tokens_before,
        "tokens_after": result.tokens_after,
        "summary_tokens": result.summary_tokens,
        "tail_tokens": result.tail_tokens,
        "compression_ratio": round(result.compression_ratio, 6),
        "provider_usage": {
            key: value
            for key, value in dict(result.provider_usage or {}).items()
            if key in _USAGE_KEYS and type(value) is int
        },
    }


def _run_followups(agent, recorder, call_start):
    rows = []
    for case_id, category, required, forbidden in _FOLLOWUPS:
        before = len(recorder.calls)
        started = time.perf_counter_ns()
        answer = ""
        error_type = ""
        try:
            answer = agent.ask(_followup_prompt(case_id))
        except Exception as exc:
            error_type = type(exc).__name__
        elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
        calls = recorder.calls[before:]
        report = {}
        events = []
        if getattr(agent, "current_task_state", None) is not None:
            try:
                report, events = load_run_artifacts(
                    agent.run_store.root,
                    agent.current_task_state.run_id,
                )
            except Exception:
                report, events = {}, []
        model_turns = [event for event in events if event.get("event") == "model_turn"]
        run = report.get("run", {}) if isinstance(report, dict) else {}
        required_present = all(token in answer for token in required)
        forbidden_absent = all(token not in answer for token in forbidden)
        terminal = (
            run.get("status") == "completed"
            and run.get("stop_reason") == "final_answer_returned"
        )
        rows.append(
            {
                "case_id": case_id,
                "category": category,
                "passed": bool(
                    not error_type and required_present and forbidden_absent and terminal
                ),
                "required_present": required_present,
                "forbidden_absent": forbidden_absent,
                "terminal": terminal,
                "error_type": error_type,
                "model_calls": len(calls),
                **_provider_failure_evidence(calls),
                "usage": _usage_total(calls),
                "usage_complete": _usage_complete(calls),
                "provider_complete_ms": [
                    call["provider_complete_ms"] for call in calls if call["completed"]
                ],
                "time_to_first_action_ms": (
                    model_turns[0].get("duration_ms") if model_turns else None
                ),
                "time_to_final_ms": elapsed_ms,
                "transport_retries": sum(
                    call.get("transport_retries") or 0 for call in calls
                ),
            }
        )
        if error_type:
            break
    workload_calls = recorder.calls[call_start:]
    return {
        "scc": len(rows) == len(_FOLLOWUPS) and all(row["passed"] for row in rows),
        "completed_cases": len(rows),
        "usage": _usage_total(workload_calls),
        "usage_complete": _usage_complete(workload_calls),
        **_provider_failure_evidence(workload_calls),
        "transport_retries": sum(
            call.get("transport_retries") or 0 for call in workload_calls
        ),
        "rows": rows,
    }


def _paired_savings(baseline, compacted, summary_calls):
    horizon = min(len(baseline["rows"]), len(compacted["rows"]))
    summary_usage = _usage_total(summary_calls)
    cumulative = []
    break_even_turn = None
    for index in range(horizon):
        baseline_rows = baseline["rows"][: index + 1]
        compacted_rows = compacted["rows"][: index + 1]
        baseline_input = sum(row["usage"]["input_tokens"] for row in baseline_rows)
        compacted_input = sum(row["usage"]["input_tokens"] for row in compacted_rows)
        baseline_total = sum(row["usage"]["total_tokens"] for row in baseline_rows)
        compacted_total = summary_usage["total_tokens"] + sum(
            row["usage"]["total_tokens"] for row in compacted_rows
        )
        net_saved = baseline_total - compacted_total
        if break_even_turn is None and net_saved >= 0:
            break_even_turn = index + 1
        cumulative.append(
            {
                "turn": index + 1,
                "gross_input_saved_tokens": baseline_input - compacted_input,
                "baseline_total_tokens": baseline_total,
                "compacted_total_tokens": compacted_total,
                "net_saved_tokens": net_saved,
            }
        )
    return {
        "summary_usage": summary_usage,
        "break_even_turn": break_even_turn,
        "horizon_turns": horizon,
        "cumulative": cumulative,
        "net_saved_tokens_at_horizon": cumulative[-1]["net_saved_tokens"] if cumulative else None,
    }


def _scenario_decision(baseline, compacted, savings, summary_calls):
    if baseline["provider_failures"] or compacted["provider_failures"]:
        return "inconclusive"
    if not baseline["scc"]:
        return "inconclusive"
    if not compacted["scc"]:
        return "reject"
    if not (
        baseline["usage_complete"]
        and compacted["usage_complete"]
        and _usage_complete(summary_calls)
    ):
        return "inconclusive"
    return "accept" if savings["break_even_turn"] is not None else "reject"


def _run_compaction_trial(client_factory, *, repeated, trial_number):
    with tempfile.TemporaryDirectory(prefix="pony-efficiency-") as temp_dir:
        root = Path(temp_dir).resolve()
        (root / "README.md").write_text("evaluation fixture\n", encoding="utf-8")
        baseline_agent, baseline_recorder, _, baseline_start = _prepare_condition(
            root / "baseline",
            client_factory,
            compacted=False,
            repeated=repeated,
        )
        compacted_agent, compacted_recorder, compactions, compacted_start = (
            _prepare_condition(
                root / "compacted",
                client_factory,
                compacted=True,
                repeated=repeated,
            )
        )
        baseline = _run_followups(baseline_agent, baseline_recorder, baseline_start)
        compacted = _run_followups(compacted_agent, compacted_recorder, compacted_start)
        summary_calls = compacted_recorder.calls[:compacted_start]
        savings = _paired_savings(baseline, compacted, summary_calls)
        return {
            "trial": trial_number,
            "baseline": baseline,
            "compacted": compacted,
            "compactions": compactions,
            "savings": savings,
            "decision": _scenario_decision(
                baseline,
                compacted,
                savings,
                summary_calls,
            ),
        }


def _aggregate_compaction_decision(trials):
    decisions = [trial["decision"] for trial in trials]
    if "reject" in decisions:
        return "reject"
    minimum_valid_trials = min(2, len(trials))
    return "accept" if decisions.count("accept") >= minimum_valid_trials else "inconclusive"


def _run_compaction_scenario(client_factory, *, repeated, repetitions):
    trials = [
        _run_compaction_trial(
            client_factory,
            repeated=repeated,
            trial_number=trial_number,
        )
        for trial_number in range(1, repetitions + 1)
    ]
    decisions = [trial["decision"] for trial in trials]
    return {
        "scenario": "repeated_compaction_resume" if repeated else "single_compaction_resume",
        "repetitions": repetitions,
        "minimum_valid_trials": min(2, repetitions),
        "accepted_trials": decisions.count("accept"),
        "rejected_trials": decisions.count("reject"),
        "inconclusive_trials": decisions.count("inconclusive"),
        "decision": _aggregate_compaction_decision(trials),
        "trials": trials,
    }


def _seed_latency_history(agent):
    for index in range(12):
        agent.session["messages"].append(
            _message(
                "user" if index % 2 == 0 else "assistant",
                _filler("latency-context", index),
                index,
            )
        )
    agent.session_store.save(agent.session)


def _run_latency_trial(client_factory, workload):
    with tempfile.TemporaryDirectory(prefix="pony-latency-") as temp_dir:
        root = Path(temp_dir).resolve()
        (root / "README.md").write_text("latency fixture\n", encoding="utf-8")
        (root / "latency.txt").write_text("LATENCY-TOOL-3N8Q\n", encoding="utf-8")
        recorder = _TimedProvider(client_factory())
        store = SessionStore(root / ".pony" / "sessions")
        agent = _build_agent(root, recorder, store)
        if workload == "long_context_final":
            _seed_latency_history(agent)
            prompt = "Reply exactly LATENCY-LONG-5V7D."
            expected = "LATENCY-LONG-5V7D"
        elif workload == "tool_continuation":
            prompt = (
                "Use read_file on latency.txt, then reply with the opaque identifier "
                "from that file only."
            )
            expected = "LATENCY-TOOL-3N8Q"
        else:
            prompt = "Reply exactly LATENCY-FINAL-4K2M."
            expected = "LATENCY-FINAL-4K2M"
        started = time.perf_counter_ns()
        answer = ""
        error_type = ""
        try:
            answer = agent.ask(prompt)
        except Exception as exc:
            error_type = type(exc).__name__
        elapsed_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
        report = {}
        events = []
        if getattr(agent, "current_task_state", None) is not None:
            try:
                report, events = load_run_artifacts(
                    agent.run_store.root,
                    agent.current_task_state.run_id,
                )
            except Exception:
                report, events = {}, []
        model_turns = [event for event in events if event.get("event") == "model_turn"]
        read_calls = sum(
            event.get("event") == "tool_executed"
            and event.get("name") == "read_file"
            and event.get("tool_status") == "ok"
            for event in events
        )
        run = report.get("run", {}) if isinstance(report, dict) else {}
        terminal = (
            run.get("status") == "completed"
            and run.get("stop_reason") == "final_answer_returned"
        )
        success = bool(
            not error_type
            and expected in answer
            and terminal
            and (workload != "tool_continuation" or read_calls >= 1)
        )
        return {
            "workload": workload,
            "success": success,
            "terminal": terminal,
            "error_type": error_type,
            "model_calls": len(recorder.calls),
            **_provider_failure_evidence(recorder.calls),
            "read_file_calls": read_calls,
            "usage": _usage_total(recorder.calls),
            "usage_complete": _usage_complete(recorder.calls),
            "provider_complete_ms": [
                call["provider_complete_ms"] for call in recorder.calls if call["completed"]
            ],
            "time_to_first_action_ms": (
                model_turns[0].get("duration_ms") if model_turns else None
            ),
            "time_to_final_ms": elapsed_ms,
            "transport_attempts": sum(
                call.get("transport_attempts") or 0 for call in recorder.calls
            ),
            "transport_retries": sum(
                call.get("transport_retries") or 0 for call in recorder.calls
            ),
        }


def _run_latency_evaluation(client_factory, repetitions):
    rows = [
        _run_latency_trial(client_factory, workload)
        for _ in range(int(repetitions))
        for workload in ("short_final", "tool_continuation", "long_context_final")
    ]
    successful = [row for row in rows if row["success"]]
    complete_ms = [value for row in successful for value in row["provider_complete_ms"]]
    first_action_ms = [
        row["time_to_first_action_ms"]
        for row in successful
        if row["time_to_first_action_ms"] is not None
    ]
    final_ms = [row["time_to_final_ms"] for row in successful]
    return {
        "ttft_status": TTFT_STATUS,
        "repetitions": int(repetitions),
        "trial_count": len(rows),
        "success_rate": len(successful) / len(rows) if rows else 0.0,
        "usage_complete": bool(rows) and all(row["usage_complete"] for row in rows),
        "provider_failures": sum(row["provider_failures"] for row in rows),
        "provider_error_types": sorted(
            {
                error_type
                for row in rows
                for error_type in row["provider_error_types"]
            }
        ),
        "transport_attempts": sum(row["transport_attempts"] for row in rows),
        "transport_retries": sum(row["transport_retries"] for row in rows),
        "provider_complete_ms": _latency_stats(complete_ms),
        "time_to_first_action_ms": _latency_stats(first_action_ms),
        "time_to_final_ms": _latency_stats(final_ms),
        "workloads": {
            workload: {
                "trials": sum(row["workload"] == workload for row in rows),
                "successes": sum(
                    row["workload"] == workload and row["success"] for row in rows
                ),
                "usage_complete_trials": sum(
                    row["workload"] == workload and row["usage_complete"] for row in rows
                ),
                "provider_failures": sum(
                    row["provider_failures"]
                    for row in rows
                    if row["workload"] == workload
                ),
                "provider_error_types": sorted(
                    {
                        error_type
                        for row in rows
                        if row["workload"] == workload
                        for error_type in row["provider_error_types"]
                    }
                ),
                "transport_attempts": sum(
                    row["transport_attempts"]
                    for row in rows
                    if row["workload"] == workload
                ),
                "transport_retries": sum(
                    row["transport_retries"]
                    for row in rows
                    if row["workload"] == workload
                ),
                "provider_complete_ms": _latency_stats(
                    value
                    for row in successful
                    if row["workload"] == workload
                    for value in row["provider_complete_ms"]
                ),
                "time_to_first_action_ms": _latency_stats(
                    row["time_to_first_action_ms"]
                    for row in successful
                    if row["workload"] == workload
                    and row["time_to_first_action_ms"] is not None
                ),
                "time_to_final_ms": _latency_stats(
                    row["time_to_final_ms"]
                    for row in successful
                    if row["workload"] == workload
                ),
            }
            for workload in ("short_final", "tool_continuation", "long_context_final")
        },
        "rows": rows,
    }


def _git_provenance(root):
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return {"commit": "", "dirty": True}
    return {"commit": commit, "dirty": dirty}


def run_efficiency_evaluation(
    repo_root=None,
    *,
    compaction_repetitions=3,
    latency_repetitions=3,
    client_factory=None,
    target=None,
    source_root=None,
):
    """Run paired compaction and non-streaming latency evaluations."""
    compaction_repetitions = int(compaction_repetitions)
    latency_repetitions = int(latency_repetitions)
    if compaction_repetitions < 1 or latency_repetitions < 1:
        raise ValueError("evaluation repetitions must be positive")
    source_root = Path(source_root or Path(__file__).resolve().parents[2])
    if target is None:
        target = _resolve_benchmark_target(Path(repo_root or Path.cwd()))
    if client_factory is None:
        def client_factory():
            return _client_from_target(target, timeout=120)
    provenance = _git_provenance(source_root)
    scenarios = [
        _run_compaction_scenario(
            client_factory,
            repeated=False,
            repetitions=compaction_repetitions,
        ),
        _run_compaction_scenario(
            client_factory,
            repeated=True,
            repetitions=compaction_repetitions,
        ),
    ]
    measured_decision = (
        "accept"
        if all(item["decision"] == "accept" for item in scenarios)
        else "reject"
        if any(item["decision"] == "reject" for item in scenarios)
        else "inconclusive"
    )
    decision = "inconclusive" if provenance["dirty"] else measured_decision
    return {
        "record_type": "efficiency_evaluation_result",
        "format_version": EFFICIENCY_EVALUATION_FORMAT_VERSION,
        "captured_at": _utc_timestamp(),
        "provenance": provenance,
        "target": {
            "provider": target["provider"],
            "transport": target["transport"],
            "variant": target["variant"],
            "model": target["model"],
        },
        "claim_scope": (
            "confirmatory single-target efficiency evaluation"
            if not provenance["dirty"]
            else "non-confirmatory dirty-worktree efficiency evaluation"
        ),
        "compaction": {
            "purpose": "retain SCC while producing positive net token savings within six follow-up turns",
            "repetitions": compaction_repetitions,
            "decision_policy": (
                "reject on any paired rejection; otherwise accept with at least two "
                "accepted trials, or one when only one repetition was requested"
            ),
            "measured_decision": measured_decision,
            "decision": decision,
            "scenarios": scenarios,
        },
        "latency": {
            "purpose": "measure full non-streaming completion and action/final latency; not TTFT",
            "comparison_scope": "single configured target; compare sanitized artifacts from separate canonical repo roots",
            **_run_latency_evaluation(client_factory, latency_repetitions),
        },
    }
