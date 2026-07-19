#!/usr/bin/env python3
"""Run Pony's existing evaluation runners and write a low-sensitivity summary."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pony.sandbox.docker import (  # noqa: E402
    DockerSandboxError,
    default_image_manifest_path,
    load_image_manifest,
)
from pony.config.environment import read_project_env  # noqa: E402
from pony.config.model import resolve_model_config  # noqa: E402
from pony.security.redaction import redact_text  # noqa: E402


BASELINE_PATH = Path("benchmarks/baselines/core-v1.json")
SUITES = (
    "core",
    "core-fast",
    "core-functional",
    "core-full",
    "sandbox",
    "sandbox-contract",
    "sandbox-real",
    "live",
)
BASELINE_SUITES = {"core", "core-full"}
PERF_RUNNERS = (
    (
        "benchmarks.perf.bench_request_build",
        ("build_request/medium",),
    ),
    (
        "benchmarks.perf.bench_security_recovery",
        (
            "security/redact_artifact/100",
            "shell/assess_corpus/50",
            "recovery/pending_reviews/200",
        ),
    ),
)
SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
MACHINE_CLASS = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
NON_PASSING_PYTEST = re.compile(
    r"(?:^|\s)\d+\s+(?:skipped|xfailed|xpassed)(?:\s|,|$)",
    re.IGNORECASE,
)
LIVE_GATES = {"behavior", "transport_cost", "security", "persistence"}
LIVE_TURN_COUNT = 5
LIVE_E2E_REPORT_FORMAT_VERSION = 3
LIVE_USAGE_DEGRADED_ASSERTIONS = {
    "turn_usage_complete",
    "all_turn_usage_complete",
}
LIVE_PROTOCOLS_BY_PROVIDER = {
    "anthropic": frozenset({"anthropic_messages"}),
    "ollama": frozenset({"ollama_chat"}),
    "openai": frozenset({"openai_chat_completions", "openai_responses"}),
}
LIVE_RESOLUTION_SOURCES = frozenset(
    {"explicit", "known_origin", "session_binding", "probe"}
)
LIVE_TURN_FIELDS = {
    "turn",
    "expected_behavior",
    "duration_ms",
    "model_attempts",
    "model_turns",
    "model_failures",
    "transport_attempts",
    "transport_retries",
    "transport_retry_reason_counts",
    "transport_evidence_complete",
    "billing_ambiguous",
    "stopped_at_step_limit",
    "terminal_status",
    "stop_reason",
    "tool_name_counts",
    "tool_status_counts",
    "error_code_counts",
    "error_code",
    "usage",
    "usage_complete",
    "assertions",
}
LIVE_REPORT_FIELDS = {
    "record_type",
    "format_version",
    "run_id",
    "provider",
    "model",
    "provider_resolution",
    "git_head",
    "aborted_reason",
    "wall_time_ms",
    "config",
    "turns",
    "global_assertions",
    "totals",
    "assertion_summary",
    "gates",
    "overall_pass",
    "artifact_security",
}
LIVE_CONFIG_FIELDS = {
    "max_model_attempts",
    "max_total_tokens",
    "request_timeout_seconds",
    "max_wall_seconds",
}
LIVE_TOTAL_FIELDS = {
    "model_attempts",
    "model_turns",
    "model_failures",
    "transport_attempts",
    "transport_retries",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
}
LIVE_TRANSPORT_GATE_FIELDS = {
    "status",
    "model_attempts",
    "model_attempt_cap",
    "transport_attempts",
    "transport_retries",
    "transport_retry_reason_counts",
    "transport_evidence_complete",
    "usage_complete",
    "billing_ambiguous",
    "input_tokens",
    "output_tokens",
}
LIVE_USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
}
MAX_FAILURE_OUTPUT = 20_000


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, choices=SUITES)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository whose .env selects the live Provider.",
    )
    return parser


def _run_command(argv, cwd):
    return subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _decode_json(text):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    payload = json.loads(text, object_pairs_hook=reject_duplicates)
    if not isinstance(payload, dict):
        raise ValueError("JSON output must be an object")
    return payload


def _architecture():
    value = platform.machine().casefold()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(value, value or "unknown")


def _machine_class(system_name):
    configured = os.environ.get("PONY_EVAL_MACHINE_CLASS", "").strip().casefold()
    value = configured or f"{system_name}-{_architecture()}"
    if MACHINE_CLASS.fullmatch(value) is None:
        raise ValueError("invalid evaluation machine class")
    return value


def _load_baseline(root, machine_class):
    payload = _decode_json((root / BASELINE_PATH).read_text(encoding="utf-8"))
    if (
        set(payload)
        != {"record_type", "format_version", "suite", "machine_class", "performance"}
        or payload.get("record_type") != "pony_evaluation_baseline"
        or type(payload.get("format_version")) is not int
        or payload["format_version"] != 1
        or payload.get("suite") != "core"
    ):
        raise ValueError("unsupported evaluation baseline")
    if payload.get("machine_class") != machine_class:
        raise ValueError(
            f"baseline machine_class={payload.get('machine_class')!r} "
            f"does not match current={machine_class!r}"
        )
    performance = payload.get("performance")
    if not isinstance(performance, dict):
        raise ValueError("invalid performance baseline")
    expected_ids = {
        scenario_id
        for _module, scenario_ids in PERF_RUNNERS
        for scenario_id in scenario_ids
    }
    if set(performance) != expected_ids:
        raise ValueError("performance baseline scenario set mismatch")
    for scenario_id, metrics in performance.items():
        if (
            not SCENARIO_ID.fullmatch(scenario_id)
            or not isinstance(metrics, dict)
            or set(metrics) != {"median_ns"}
            or type(metrics.get("median_ns")) is not int
            or metrics["median_ns"] <= 0
        ):
            raise ValueError("invalid performance baseline entry")
    return payload


def _core_fast_commands():
    python = sys.executable
    pytest = (python, "-m", "pytest", "-q")
    return (
        ("core.ruff", (python, "-m", "ruff", "check", "."), "exit"),
        (
            "core.context-budget",
            (*pytest, "tests/test_context_budget.py", "tests/test_context_snapshot.py"),
            "exit",
        ),
        (
            "core.tool-security",
            (
                *pytest,
                "tests/test_tool_policy.py",
                "tests/test_shell_security_corpus.py",
            ),
            "exit",
        ),
    )


def _core_functional_commands():
    python = sys.executable
    return (
        (
            "core.memory-quality-fake",
            (
                python,
                "benchmarks/memory_quality/run_benchmark.py",
                "--mode",
                "fake",
                "--format",
                "json",
            ),
            "exit",
        ),
        (
            "core.fixed-benchmark",
            (
                python,
                "-c",
                "from pathlib import Path; from tempfile import TemporaryDirectory; "
                "from benchmarks.evaluation.fixed_benchmark import run_harness_regression_v2; "
                "d=TemporaryDirectory(); a=run_harness_regression_v2(artifact_path=Path(d.name)/'fixed.json', workspace_root=Path(d.name)/'workspaces'); "
                "s=a['summary']; raise SystemExit(0 if s['failed']==0 and s['passed']==s['total_tasks'] and s['within_budget']==s['total_tasks'] and s['verifier_passes']==s['total_tasks'] else 1)",
            ),
            "exit",
        ),
        (
            "core.recovery-ablation",
            (
                python,
                "-c",
                "from pathlib import Path; from tempfile import TemporaryDirectory; "
                "from benchmarks.evaluation.experiments_recovery import run_recovery_ablation_v2; "
                "d=TemporaryDirectory(); a=run_recovery_ablation_v2(Path(d.name)/'recovery.json', repetitions=1); "
                "s=a['variants']['resume_enabled']['summary']; raise SystemExit(0 if s=={'resume_success_rate':1.0,'stale_reanchor_rate':1.0,'workspace_drift_detection_rate':1.0,'resume_false_accept_rate':0.0} else 1)",
            ),
            "exit",
        ),
    )


def _core_full_commands():
    python = sys.executable
    return (
        ("core.ruff", (python, "-m", "ruff", "check", "."), "exit"),
        ("core.pytest", (python, "-m", "pytest", "-q"), "exit"),
        *_core_functional_commands(),
        ("core.build", ("uv", "build", "--clear"), "exit"),
        (
            "core.distribution",
            (
                python,
                "scripts/release/verify_distribution.py",
                "--install-smoke",
            ),
            "exit",
        ),
    )


def _sandbox_contract_commands():
    python = sys.executable
    contract = (
        python,
        "-m",
        "pytest",
        "-q",
        "tests/test_docker_sandbox_session.py",
        "tests/test_docker_sandbox_runner.py",
        "tests/test_docker_sandbox_runtime.py",
        "tests/test_docker_sandbox_cli.py",
        "tests/test_sandbox_apply.py",
        "tests/test_public_api_contract.py",
    )
    return [("sandbox.contract", contract, "no_skip")]


def _sandbox_real_commands(system_name):
    python = sys.executable
    if system_name not in {"darwin", "linux"}:
        return ()
    return (
        (
            f"sandbox.real.{system_name}.readiness",
            (
                python,
                "scripts/sandbox/verify_runtime.py",
                "--require-ready",
            ),
            "exit",
        ),
        (
            f"sandbox.real.{system_name}.vertical",
            (python, "scripts/sandbox/verify_vertical.py"),
            "exit",
        ),
    )


def _execute(argv, *, runner, root):
    started = time.monotonic_ns()
    try:
        result = runner(list(argv), root)
        exit_code = int(result.returncode)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
    duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    return exit_code, stdout, stderr, duration_ms


def _emit_failure_output(scenario_id, stdout, stderr):
    output = "\n".join(part.strip() for part in (stderr, stdout) if part.strip())
    if not output:
        return
    output = redact_text(output)
    if len(output) > MAX_FAILURE_OUTPUT:
        output = "[truncated]\n" + output[-MAX_FAILURE_OUTPUT:]
    print(f"[evaluate] {scenario_id} child output:\n{output}", file=sys.stderr)


def _functional_passed(kind, exit_code, stdout):
    if exit_code != 0:
        return False
    if kind == "no_skip":
        return NON_PASSING_PYTEST.search(stdout) is None
    return True


def _row(scenario_id, passed, exit_code, duration_ms, artifact_path, metrics=None):
    row = {
        "id": scenario_id,
        "status": "pass" if passed else "fail",
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "artifact_path": artifact_path,
    }
    if metrics is not None:
        row["metrics"] = metrics
    return row


def _run_functional(commands, *, runner, root, artifact_path):
    rows = []
    for scenario_id, argv, kind in commands:
        exit_code, stdout, stderr, duration_ms = _execute(
            argv,
            runner=runner,
            root=root,
        )
        passed = _functional_passed(kind, exit_code, stdout)
        if not passed:
            _emit_failure_output(scenario_id, stdout, stderr)
        rows.append(
            _row(
                scenario_id,
                passed,
                exit_code,
                duration_ms,
                artifact_path,
            )
        )
    return rows


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def _safe_live_count_map(value):
    return (
        isinstance(value, dict)
        and len(value) <= 100
        and all(
            isinstance(key, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key)
            and _nonnegative_int(item)
            for key, item in value.items()
        )
    )


def _passing_provider_resolution(value, provider):
    if not isinstance(value, dict) or set(value) != {
        "status",
        "resolution_source",
        "protocol",
        "candidate_count",
        "model_calls",
        "usage_status",
    }:
        return False
    status = value["status"]
    resolution_source = value["resolution_source"]
    protocol = value["protocol"]
    candidate_count = value["candidate_count"]
    model_calls = value["model_calls"]
    usage_status = value["usage_status"]
    return (
        status in {"not_run", "ok"}
        and resolution_source in LIVE_RESOLUTION_SOURCES
        and protocol in LIVE_PROTOCOLS_BY_PROVIDER.get(provider, ())
        and type(candidate_count) is int
        and 0 <= candidate_count <= 3
        and type(model_calls) is int
        and 0 <= model_calls <= 6
        and usage_status in {"complete", "degraded", "not_checked"}
        and (
            (
                status == "not_run"
                and resolution_source != "probe"
                and candidate_count == 0
                and model_calls == 0
                and usage_status == "not_checked"
            )
            or (
                status == "ok"
                and resolution_source == "probe"
                and candidate_count >= 1
                and model_calls >= 2
                and usage_status in {"complete", "degraded"}
            )
        )
    )


def _passing_live_assertion(value, *, usage_degraded):
    return (
        isinstance(value, dict)
        and set(value) == {"name", "gate", "passed"}
        and isinstance(value["name"], str)
        and bool(value["name"])
        and value["gate"] in LIVE_GATES
        and (
            value["passed"] is True
            or (
                usage_degraded
                and value["passed"] is False
                and value["gate"] == "transport_cost"
                and value["name"] in LIVE_USAGE_DEGRADED_ASSERTIONS
            )
        )
    )


def _passing_live_turn(value, expected_turn, *, usage_degraded):
    usage = value.get("usage") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and set(value) == LIVE_TURN_FIELDS
        and type(value["turn"]) is int
        and value["turn"] == expected_turn
        and isinstance(value["expected_behavior"], str)
        and bool(value["expected_behavior"])
        and _nonnegative_int(value["duration_ms"])
        and type(value["model_attempts"]) is int
        and value["model_attempts"] > 0
        and type(value["model_turns"]) is int
        and 0 < value["model_turns"] <= value["model_attempts"]
        and value["model_failures"] == 0
        and type(value["transport_attempts"]) is int
        and value["transport_attempts"] > 0
        and value["transport_retries"] == 0
        and value["transport_retry_reason_counts"] == {}
        and value["transport_evidence_complete"] is True
        and type(value["usage_complete"]) is bool
        and value["billing_ambiguous"] is (not value["usage_complete"])
        and type(value["stopped_at_step_limit"]) is bool
        and value["stopped_at_step_limit"] is False
        and value["terminal_status"] == "completed"
        and value["stop_reason"] == "final_answer_returned"
        and _safe_live_count_map(value["tool_name_counts"])
        and _safe_live_count_map(value["tool_status_counts"])
        and _safe_live_count_map(value["error_code_counts"])
        and value["error_code"] == ""
        and isinstance(usage, dict)
        and set(usage) == LIVE_USAGE_FIELDS
        and all(_nonnegative_int(item) for item in usage.values())
        and (usage_degraded or value["usage_complete"] is True)
        and isinstance(value["assertions"], list)
        and bool(value["assertions"])
    )


def _passing_live_envelope(report, provider, expected_commit):
    if not isinstance(report, dict) or set(report) != LIVE_REPORT_FIELDS:
        return False
    config = report.get("config")
    totals = report.get("totals")
    summary = report.get("assertion_summary")
    artifact_security = report.get("artifact_security")
    turns = report.get("turns")
    global_assertions = report.get("global_assertions")
    return (
        report.get("record_type") == "live_e2e_report"
        and type(report.get("format_version")) is int
        and report["format_version"] == LIVE_E2E_REPORT_FORMAT_VERSION
        and isinstance(report.get("run_id"), str)
        and re.fullmatch(r"live-e2e-[0-9]+", report["run_id"])
        and report.get("provider") == provider
        and isinstance(report.get("model"), str)
        and bool(report["model"])
        and report.get("git_head") == expected_commit
        and report.get("aborted_reason") == ""
        and _nonnegative_int(report.get("wall_time_ms"))
        and report.get("overall_pass") is True
        and _passing_provider_resolution(report.get("provider_resolution"), provider)
        and isinstance(config, dict)
        and set(config) == LIVE_CONFIG_FIELDS
        and all(type(value) is int and value > 0 for value in config.values())
        and report["wall_time_ms"] <= config["max_wall_seconds"] * 1000
        and isinstance(totals, dict)
        and set(totals) == LIVE_TOTAL_FIELDS
        and all(_nonnegative_int(value) for value in totals.values())
        and totals["model_attempts"] > 0
        and totals["model_attempts"] <= config["max_model_attempts"]
        and totals["model_failures"] == 0
        and totals["transport_attempts"] > 0
        and totals["transport_retries"] == 0
        and isinstance(summary, dict)
        and set(summary) == {"total", "passed", "failed"}
        and all(_nonnegative_int(value) for value in summary.values())
        and summary["total"] > 0
        and summary["passed"] + summary["failed"] == summary["total"]
        and isinstance(artifact_security, dict)
        and set(artifact_security) == {"files_scanned", "secret_hits", "mode_failures"}
        and type(artifact_security["files_scanned"]) is int
        and artifact_security["files_scanned"] > 0
        and artifact_security["secret_hits"] == []
        and artifact_security["mode_failures"] == []
        and isinstance(turns, list)
        and len(turns) == LIVE_TURN_COUNT
        and isinstance(global_assertions, list)
        and bool(global_assertions)
    )


def _live_usage_degraded(gates):
    transport = gates.get("transport_cost") if isinstance(gates, dict) else None
    return bool(
        isinstance(transport, dict)
        and transport.get("status") == "degraded"
        and transport.get("usage_complete") is False
        and transport.get("billing_ambiguous") is True
        and transport.get("transport_evidence_complete") is True
        and transport.get("transport_retries") == 0
    )


def _passing_live_assertions(report, usage_degraded):
    assertions = list(report["global_assertions"])
    for expected_turn, turn in enumerate(report["turns"], start=1):
        if not _passing_live_turn(
            turn,
            expected_turn,
            usage_degraded=usage_degraded,
        ):
            return False
        assertions.extend(turn["assertions"])
    assertions_valid = bool(assertions) and all(
        _passing_live_assertion(item, usage_degraded=usage_degraded)
        for item in assertions
    )
    if not assertions_valid:
        return False
    gate_coverage = {item["gate"] for item in assertions} == LIVE_GATES
    transport_proved = any(
        item["gate"] == "transport_cost" and item["passed"] is True
        for item in assertions
    )
    failed_assertions = sum(item["passed"] is False for item in assertions)
    summary = report["assertion_summary"]
    return (
        gate_coverage
        and transport_proved
        and usage_degraded is (failed_assertions > 0)
        and summary["passed"] == len(assertions) - failed_assertions
        and summary["failed"] == failed_assertions
        and summary["total"] == len(assertions)
        and any(
            item["name"] == "fixture_restored_after_context_exit"
            for item in assertions
        )
    )


def _passing_live_gates(report, usage_degraded):
    gates = report["gates"]
    if not isinstance(gates, dict) or set(gates) != LIVE_GATES:
        return False
    totals = report["totals"]
    turns = report["turns"]
    config = report["config"]
    turn_totals = {
        key: sum(turn[key] for turn in turns)
        for key in (
            "model_attempts",
            "model_turns",
            "model_failures",
            "transport_attempts",
            "transport_retries",
        )
    }
    usage_totals = {
        key: sum(turn["usage"][key] for turn in turns)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    }
    behavior = gates["behavior"]
    transport = gates["transport_cost"]
    return (
        all(totals[key] == value for key, value in turn_totals.items())
        and all(totals[key] == value for key, value in usage_totals.items())
        and isinstance(behavior, dict)
        and set(behavior)
        == {"status", "model_turns", "model_failures", "turns_completed"}
        and behavior["status"] == "pass"
        and behavior["model_turns"] == totals["model_turns"]
        and behavior["model_failures"] == 0
        and behavior["turns_completed"] is True
        and isinstance(transport, dict)
        and set(transport) == LIVE_TRANSPORT_GATE_FIELDS
        and transport["status"] in {"pass", "degraded"}
        and (transport["status"] == "degraded") is usage_degraded
        and transport["model_attempts"] == totals["model_attempts"]
        and transport["model_attempt_cap"] == config["max_model_attempts"]
        and transport["transport_attempts"] == totals["transport_attempts"]
        and transport["transport_retries"] == 0
        and transport["transport_retry_reason_counts"] == {}
        and transport["transport_evidence_complete"] is True
        and transport["usage_complete"] is (not usage_degraded)
        and transport["billing_ambiguous"] is usage_degraded
        and transport["input_tokens"] == totals["input_tokens"]
        and transport["output_tokens"] == totals["output_tokens"]
        and gates["security"] == {"status": "pass"}
        and gates["persistence"] == {"status": "pass"}
    )


def _live_report_passed(report, provider, expected_commit):
    if not _passing_live_envelope(report, provider, expected_commit):
        return False
    usage_degraded = _live_usage_degraded(report["gates"])
    return _passing_live_assertions(
        report,
        usage_degraded,
    ) and _passing_live_gates(report, usage_degraded)


def _live_provider_matches(selector, provider, protocol):
    if provider not in LIVE_PROTOCOLS_BY_PROVIDER:
        return False
    if selector == "auto":
        return True
    if selector == "openai":
        return provider == "openai"
    if selector == "openai-chat":
        return provider == "openai" and protocol == "openai_chat_completions"
    if selector == "openai-responses":
        return provider == "openai" and protocol == "openai_responses"
    return selector == provider


def _run_live(provider_selector, *, runner, root, artifact_path):
    from benchmarks.live_e2e.run_live_session import load_live_report

    results = root / "benchmarks" / "live_e2e" / "results"
    before = set(results.glob("*.json")) if results.is_dir() else set()
    exit_code, stdout, stderr, duration_ms = _execute(
        (
            sys.executable,
            "benchmarks/live_e2e/run_live_session.py",
            "--repo-root",
            str(root),
        ),
        runner=runner,
        root=root,
    )
    after = set(results.glob("*.json")) if results.is_dir() else set()
    created = after - before
    passed = False
    resolved_provider = provider_selector
    expected_commit = _git_value(root, "rev-parse", "HEAD") or "unknown"
    if exit_code == 0 and len(created) == 1:
        try:
            report = load_live_report(created.pop())
            observed_provider = report.get("provider")
            resolution = report.get("provider_resolution")
            protocol = resolution.get("protocol") if isinstance(resolution, dict) else ""
            if _live_provider_matches(
                provider_selector,
                observed_provider,
                protocol,
            ):
                resolved_provider = observed_provider
                passed = _live_report_passed(
                    report,
                    resolved_provider,
                    expected_commit,
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            passed = False
    if not passed:
        _emit_failure_output(f"live.{resolved_provider}", stdout, stderr)
    return (
        [_row(f"live.{resolved_provider}", passed, exit_code, duration_ms, artifact_path)],
        resolved_provider,
    )


def _perf_regressed(actual_ns, baseline_ns):
    return actual_ns > baseline_ns * 2 and actual_ns - baseline_ns > 5_000_000


def _run_perf_process(module, scenario_ids, *, runner, root):
    exit_code, stdout, stderr, duration_ms = _execute(
        (sys.executable, "-m", module),
        runner=runner,
        root=root,
    )
    observed = {}
    duplicates = set()
    if exit_code == 0:
        try:
            payload = _decode_json(stdout)
            scenarios = payload.get("scenarios")
            if isinstance(scenarios, list):
                for item in scenarios:
                    if (
                        not isinstance(item, dict)
                        or item.get("name") not in scenario_ids
                    ):
                        continue
                    name = item["name"]
                    if name in observed:
                        duplicates.add(name)
                    observed[name] = item
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    for name in duplicates:
        observed.pop(name, None)
    return exit_code, observed, stdout, stderr, duration_ms


def _valid_perf_metrics(item):
    actual = item.get("median_ns")
    p95 = item.get("p95_ns")
    return type(actual) is int and actual > 0 and type(p95) is int and p95 >= actual


def _run_performance(baseline, *, runner, root, artifact_path):
    rows = []
    for module, scenario_ids in PERF_RUNNERS:
        first = _run_perf_process(
            module,
            scenario_ids,
            runner=runner,
            root=root,
        )
        first_exit, first_observed, _stdout, _stderr, first_duration = first
        regressed = {
            scenario_id
            for scenario_id in scenario_ids
            if first_exit == 0
            and _valid_perf_metrics(first_observed.get(scenario_id, {}))
            and _perf_regressed(
                first_observed[scenario_id]["median_ns"],
                baseline["performance"][scenario_id]["median_ns"],
            )
        }
        confirmation = (
            _run_perf_process(module, scenario_ids, runner=runner, root=root)
            if regressed
            else None
        )
        for scenario_id in scenario_ids:
            process = (
                confirmation
                if confirmation is not None and scenario_id in regressed
                else first
            )
            exit_code, observed, stdout, stderr, duration_ms = process
            if process is confirmation:
                duration_ms += first_duration
            item = observed.get(scenario_id, {})
            actual = item.get("median_ns")
            p95 = item.get("p95_ns")
            baseline_ns = baseline["performance"][scenario_id]["median_ns"]
            metrics_valid = _valid_perf_metrics(item)
            metrics = {
                "median_ns": actual if type(actual) is int else None,
                "p95_ns": p95 if type(p95) is int else None,
                "baseline_median_ns": baseline_ns,
                "confirmation_run": scenario_id in regressed,
            }
            passed = (
                exit_code == 0
                and metrics_valid
                and not _perf_regressed(actual, baseline_ns)
            )
            if not passed:
                _emit_failure_output(scenario_id, stdout, stderr)
            rows.append(
                _row(
                    scenario_id,
                    passed,
                    exit_code,
                    duration_ms,
                    artifact_path,
                    metrics,
                )
            )
    return rows


def _git_value(root, *args):
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _provenance(root, suite, provider, system_name, machine_class):
    commit = _git_value(root, "rev-parse", "HEAD")
    if commit is None or re.fullmatch(r"[0-9a-fA-F]{7,64}", commit) is None:
        commit = "unknown"
    dirty_output = _git_value(root, "status", "--porcelain")
    sandbox = {"image": "unknown", "policy": "unknown"}
    try:
        image = load_image_manifest(default_image_manifest_path())
        sandbox = {
            "image": image.image_digest,
            "policy": image.policy_digest,
        }
    except DockerSandboxError as exc:
        if exc.code != "sandbox_image_not_released":
            raise
        sandbox = {"image": "not_released", "policy": "not_released"}
    except (OSError, TypeError, ValueError):
        pass
    provenance = {
        "commit": commit.lower(),
        "dirty": "unknown" if dirty_output is None else bool(dirty_output),
        "python": platform.python_version(),
        "platform": system_name,
        "architecture": _architecture(),
        "machine_class": machine_class,
        "sandbox_image_digest": sandbox["image"],
        "sandbox_policy_digest": sandbox["policy"],
    }
    if suite in BASELINE_SUITES:
        provenance["baseline"] = BASELINE_PATH.as_posix()
    if suite == "live":
        provenance["provider"] = provider
    return provenance


def _render_markdown(payload):
    lines = [
        "# Pony evaluation",
        "",
        f"- Suite: `{payload['suite']}`",
        f"- Status: `{payload['status']}`",
        f"- Duration: `{payload['duration_ms']} ms`",
        f"- Artifact: `{payload['artifact_path']}`",
    ]
    lines.extend(
        f"- Provenance {key}: `{value}`" for key, value in payload["provenance"].items()
    )
    lines.extend(
        (
            "",
            "| Scenario | Status | Exit | Duration | Metrics | Artifact |",
            "|---|---:|---:|---:|---|---|",
        )
    )
    for row in payload["scenarios"]:
        metrics = row.get("metrics")
        metric_text = ""
        if metrics is not None:
            metric_text = ", ".join(f"{key}={value}" for key, value in metrics.items())
        lines.append(
            f"| `{row['id']}` | `{row['status']}` | {row['exit_code']} | "
            f"{row['duration_ms']} ms | {metric_text} | `{row['artifact_path']}` |"
        )
    return "\n".join(lines) + "\n"


def _validate_low_sensitivity(payload, markdown, root):
    if set(payload) != {
        "record_type",
        "format_version",
        "suite",
        "status",
        "duration_ms",
        "provenance",
        "artifact_path",
        "scenarios",
    }:
        raise ValueError("invalid evaluation artifact fields")
    if (
        payload.get("record_type") != "pony_evaluation_result"
        or type(payload.get("format_version")) is not int
        or payload["format_version"] != 1
        or payload.get("suite") not in set(SUITES)
        or payload.get("status") not in {"pass", "fail"}
        or type(payload.get("duration_ms")) is not int
        or payload["duration_ms"] < 0
    ):
        raise ValueError("invalid evaluation artifact header")
    base_provenance = {
        "commit",
        "dirty",
        "python",
        "platform",
        "architecture",
        "machine_class",
        "sandbox_image_digest",
        "sandbox_policy_digest",
    }
    expected_provenance = set(base_provenance)
    if payload["suite"] in BASELINE_SUITES:
        expected_provenance.add("baseline")
    if payload["suite"] == "live":
        expected_provenance.add("provider")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != expected_provenance:
        raise ValueError("invalid evaluation provenance")
    artifact_path = payload.get("artifact_path")
    if not isinstance(artifact_path, str) or Path(artifact_path).is_absolute():
        raise ValueError("evaluation artifact path must be relative")
    rows = payload.get("scenarios")
    if not isinstance(rows, list) or not rows:
        raise ValueError("evaluation scenarios missing")
    for row in rows:
        if not isinstance(row, dict) or not set(row) <= {
            "id",
            "status",
            "exit_code",
            "duration_ms",
            "artifact_path",
            "metrics",
        }:
            raise ValueError("invalid evaluation scenario fields")
        if (
            not isinstance(row.get("id"), str)
            or SCENARIO_ID.fullmatch(row["id"]) is None
            or row.get("status") not in {"pass", "fail"}
            or type(row.get("exit_code")) is not int
            or type(row.get("duration_ms")) is not int
            or row["duration_ms"] < 0
            or row.get("artifact_path") != artifact_path
        ):
            raise ValueError("invalid evaluation scenario")
        if "metrics" in row:
            if not isinstance(row["metrics"], dict) or set(row["metrics"]) != {
                "median_ns",
                "p95_ns",
                "baseline_median_ns",
                "confirmation_run",
            }:
                raise ValueError("invalid evaluation performance metrics")
            if type(row["metrics"]["confirmation_run"]) is not bool:
                raise ValueError("invalid evaluation performance metrics")

    forbidden_keys = {
        "args",
        "body",
        "completion",
        "memory_body",
        "memory_query",
        "prompt",
        "query",
        "result",
        "secret",
        "stderr",
        "stdout",
        "tool_result",
    }

    def scan(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden_keys:
                    raise ValueError("forbidden evaluation artifact field")
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
        elif isinstance(value, str):
            embedded_posix = re.search(r"(?:^|[^A-Za-z0-9._-])/(?!/)[^\s`]+", value)
            embedded_windows = re.search(r"(?:^|[^A-Za-z0-9._-])[A-Za-z]:[\\/]", value)
            if (
                Path(value).is_absolute()
                or PureWindowsPath(value).is_absolute()
                or embedded_posix
                or embedded_windows
            ):
                raise ValueError("absolute path in evaluation artifact")

    scan(payload)
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    secret_values = {
        value
        for name, value in os.environ.items()
        if value
        and len(value) >= 8
        and any(
            marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        )
    }
    for forbidden in {str(root), *secret_values}:
        if forbidden and (forbidden in serialized or forbidden in markdown):
            raise ValueError("forbidden content in evaluation artifact")


def _write_atomic(path, text):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _failure_message(row):
    metrics = row.get("metrics")
    if metrics is not None:
        return (
            f"current median_ns={metrics['median_ns']}; "
            f"baseline_median_ns={metrics['baseline_median_ns']}; "
            "expected median <=2x baseline or increase <=5000000ns"
        )
    return (
        f"current exit={row['exit_code']}; expected exit=0 and complete gate evidence"
    )


def run_evaluation(
    suite,
    *,
    runner=_run_command,
    root=ROOT,
    output_dir=None,
    now=None,
    system_name=None,
):
    root = Path(root).resolve()
    system_name = (system_name or platform.system()).lower()
    machine_class = _machine_class(system_name)
    started = time.monotonic_ns()
    captured_at = now or datetime.now(timezone.utc)
    timestamp = captured_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    artifact_name = f"{timestamp}-{suite}.json"
    artifact_rel = (
        Path("artifacts/eval") / artifact_name
        if output_dir is None
        else Path(artifact_name)
    )
    markdown_rel = artifact_rel.with_suffix(".md")
    artifact_ref = artifact_rel.as_posix()
    artifact_dir = (
        root / artifact_rel.parent
        if output_dir is None
        else Path(output_dir).resolve()
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if output_dir is None and not artifact_dir.resolve().is_relative_to(root):
        raise ValueError("evaluation artifact directory escapes repository")
    provider = ""
    json_path = artifact_dir / artifact_rel.name
    markdown_path = artifact_dir / markdown_rel.name
    if os.path.lexists(json_path) or os.path.lexists(markdown_path):
        raise ValueError("evaluation artifact already exists")

    if suite == "core-fast":
        rows = _run_functional(
            _core_fast_commands(),
            runner=runner,
            root=root,
            artifact_path=artifact_ref,
        )
    elif suite == "core-functional":
        rows = _run_functional(
            _core_functional_commands(),
            runner=runner,
            root=root,
            artifact_path=artifact_ref,
        )
    elif suite in BASELINE_SUITES:
        baseline = _load_baseline(root, machine_class)
        rows = _run_functional(
            _core_full_commands(),
            runner=runner,
            root=root,
            artifact_path=artifact_ref,
        )
        rows.extend(
            _run_performance(
                baseline,
                runner=runner,
                root=root,
                artifact_path=artifact_ref,
            )
        )
    elif suite == "sandbox-contract":
        rows = _run_functional(
            _sandbox_contract_commands(),
            runner=runner,
            root=root,
            artifact_path=artifact_ref,
        )
    elif suite in {"sandbox", "sandbox-real"}:
        rows = []
        if suite == "sandbox":
            rows.extend(
                _run_functional(
                    _sandbox_contract_commands(),
                    runner=runner,
                    root=root,
                    artifact_path=artifact_ref,
                )
            )
        if system_name not in {"darwin", "linux"}:
            rows.append(_row("sandbox.real.unsupported", False, 2, 0, artifact_ref))
        else:
            rows.extend(
                _run_functional(
                    _sandbox_real_commands(system_name),
                    runner=runner,
                    root=root,
                    artifact_path=artifact_ref,
                )
            )
    elif suite == "live":
        resolved = resolve_model_config(
            project_env=read_project_env(root),
            process_env=dict(os.environ),
            required=True,
        )
        provider = resolved["provider"]["value"]
        rows, provider = _run_live(
            provider,
            runner=runner,
            root=root,
            artifact_path=artifact_ref,
        )
    else:
        raise ValueError("unknown evaluation suite")

    payload = {
        "record_type": "pony_evaluation_result",
        "format_version": 1,
        "suite": suite,
        "status": "pass"
        if rows and all(row["status"] == "pass" for row in rows)
        else "fail",
        "duration_ms": max(0, (time.monotonic_ns() - started) // 1_000_000),
        "provenance": _provenance(
            root,
            suite,
            provider,
            system_name,
            machine_class,
        ),
        "artifact_path": artifact_ref,
        "scenarios": rows,
    }
    markdown = _render_markdown(payload)
    _validate_low_sensitivity(payload, markdown, root)
    _write_atomic(json_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        _write_atomic(markdown_path, markdown)
    except Exception:
        json_path.unlink(missing_ok=True)
        raise
    return payload, artifact_rel, markdown_rel


def main(argv=None, *, runner=_run_command, root=ROOT, now=None, system_name=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    selected_root = args.repo_root if args.repo_root is not None else root
    try:
        payload, artifact_path, markdown_path = run_evaluation(
            args.suite,
            runner=runner,
            root=selected_root,
            output_dir=args.output_dir,
            now=now,
            system_name=system_name,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"[evaluate] evaluation configuration is invalid: {redact_text(exc)}",
            file=sys.stderr,
        )
        return 2
    print(f"[evaluate] {payload['status']}: {artifact_path.as_posix()}")
    print(f"[evaluate] markdown: {markdown_path.as_posix()}")
    for row in payload["scenarios"]:
        if row["status"] == "fail":
            print(
                f"[evaluate] {row['id']}: {_failure_message(row)}; "
                f"artifact={row['artifact_path']}",
                file=sys.stderr,
            )
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
