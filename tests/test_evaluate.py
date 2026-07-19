from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.evaluation import evaluate


BASELINE = {
    "record_type": "pony_evaluation_baseline",
    "format_version": 1,
    "suite": "core",
    "machine_class": "test-machine",
    "performance": {
        "build_request/medium": {"median_ns": 2_000_000},
        "security/redact_artifact/100": {"median_ns": 40_000_000},
        "shell/assess_corpus/50": {"median_ns": 1_500_000},
    },
}


def _write_baseline(root):
    path = root / "benchmarks" / "baselines" / "core-v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(BASELINE), encoding="utf-8")


def _perf_output(module, medians=None):
    medians = medians or {
        scenario_id: metrics["median_ns"]
        for scenario_id, metrics in BASELINE["performance"].items()
    }
    expected = dict(evaluate.PERF_RUNNERS)[module]
    return json.dumps(
        {
            "scenarios": [
                {
                    "name": scenario_id,
                    "median_ns": medians[scenario_id],
                    "p95_ns": medians[scenario_id] + 1,
                }
                for scenario_id in expected
            ]
        }
    )


def _live_report(*, git_head="unknown"):
    assertions = {
        "transport": {"name": "transport_ok", "gate": "transport_cost", "passed": True},
        "security": {"name": "security_ok", "gate": "security", "passed": True},
        "persistence": {
            "name": "persistence_ok",
            "gate": "persistence",
            "passed": True,
        },
        "fixture": {
            "name": "fixture_restored_after_context_exit",
            "gate": "persistence",
            "passed": True,
        },
    }
    totals = {
        "model_attempts": 5,
        "model_turns": 5,
        "model_failures": 0,
        "transport_attempts": 5,
        "transport_retries": 0,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    return {
        "record_type": "live_e2e_report",
        "format_version": 3,
        "run_id": "live-e2e-1",
        "provider": "openai",
        "model": "test-model",
        "provider_resolution": {
            "status": "not_run",
            "resolution_source": "explicit",
            "protocol": "openai_responses",
            "candidate_count": 0,
            "model_calls": 0,
            "usage_status": "not_checked",
        },
        "git_head": git_head,
        "aborted_reason": "",
        "wall_time_ms": 10,
        "config": {
            "max_model_attempts": 15,
            "max_total_tokens": 200_000,
            "request_timeout_seconds": 300,
            "max_wall_seconds": 900,
        },
        "turns": [
            {
                "turn": turn,
                "expected_behavior": expected_behavior,
                "duration_ms": 1,
                "model_attempts": 1,
                "model_turns": 1,
                "model_failures": 0,
                "transport_attempts": 1,
                "transport_retries": 0,
                "transport_retry_reason_counts": {},
                "transport_evidence_complete": True,
                "billing_ambiguous": False,
                "stopped_at_step_limit": False,
                "terminal_status": "completed",
                "stop_reason": "final_answer_returned",
                "tool_name_counts": {},
                "tool_status_counts": {},
                "error_code_counts": {},
                "error_code": "",
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "total_tokens": 3,
                    "cached_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "usage_complete": True,
                "assertions": [
                    {
                        "name": f"dynamic_behavior_ok_{turn}",
                        "gate": "behavior",
                        "passed": True,
                    }
                ],
            }
            for turn, expected_behavior in enumerate(
                (
                    "recall_triggered",
                    "provider_tool_roundtrip",
                    "source_pool_bounded",
                    "history_compacted",
                    "cache_anchor_verified",
                ),
                start=1,
            )
        ],
        "global_assertions": [
            assertions["transport"],
            assertions["security"],
            assertions["persistence"],
            assertions["fixture"],
        ],
        "totals": totals,
        "assertion_summary": {"total": 9, "passed": 9, "failed": 0},
        "gates": {
            "behavior": {
                "status": "pass",
                "model_turns": 5,
                "model_failures": 0,
                "turns_completed": True,
            },
            "transport_cost": {
                "status": "pass",
                "model_attempts": 5,
                "model_attempt_cap": 15,
                "transport_attempts": 5,
                "transport_retries": 0,
                "transport_retry_reason_counts": {},
                "transport_evidence_complete": True,
                "usage_complete": True,
                "billing_ambiguous": False,
                "input_tokens": 10,
                "output_tokens": 5,
            },
            "security": {"status": "pass"},
            "persistence": {"status": "pass"},
        },
        "overall_pass": True,
        "artifact_security": {
            "files_scanned": 1,
            "secret_hits": [],
            "mode_failures": [],
        },
    }


def test_core_uses_injected_runners_and_writes_only_low_sensitivity_fields(
    tmp_path, monkeypatch
):
    _write_baseline(tmp_path)
    monkeypatch.setenv("PONY_EVAL_MACHINE_CLASS", "test-machine")
    calls = []
    secret_output = "prompt=private tool result stdout stderr /Users/private/repo"

    def runner(argv, cwd):
        calls.append((argv, cwd))
        module = argv[-1] if argv[:2] == [evaluate.sys.executable, "-m"] else ""
        stdout = (
            _perf_output(module)
            if module in dict(evaluate.PERF_RUNNERS)
            else secret_output
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=secret_output)

    payload, json_rel, markdown_rel = evaluate.run_evaluation(
        "core",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
        system_name="linux",
    )

    assert payload["status"] == "pass"
    assert json_rel == Path("artifacts/eval/20260712T000000000000Z-core.json")
    assert markdown_rel == json_rel.with_suffix(".md")
    assert {row["id"] for row in payload["scenarios"]} >= {
        "core.ruff",
        "core.pytest",
        "core.memory-quality-fake",
        "core.fixed-benchmark",
        "core.build",
        "core.distribution",
        *BASELINE["performance"],
    }
    assert any(
        "benchmarks/memory_quality/run_benchmark.py" in argv for argv, _ in calls
    )
    assert any(argv[:2] == ["uv", "build"] for argv, _ in calls)
    assert {module for module, _ids in evaluate.PERF_RUNNERS} <= {
        argv[-1] for argv, _ in calls
    }
    assert (
        sum(argv[:3] == [evaluate.sys.executable, "-m", "pytest"] for argv, _ in calls)
        == 1
    )

    serialized = (tmp_path / json_rel).read_text(encoding="utf-8")
    markdown = (tmp_path / markdown_rel).read_text(encoding="utf-8")
    for forbidden in (
        secret_output,
        "/Users/private/repo",
        "prompt=private",
        "tool result",
    ):
        assert forbidden not in serialized
        assert forbidden not in markdown
    assert not Path(payload["artifact_path"]).is_absolute()
    assert set(payload) == {
        "record_type",
        "format_version",
        "suite",
        "status",
        "duration_ms",
        "provenance",
        "artifact_path",
        "scenarios",
    }
    for row in payload["scenarios"]:
        assert set(row) <= {
            "id",
            "status",
            "exit_code",
            "duration_ms",
            "artifact_path",
            "metrics",
        }


def test_perf_gate_requires_both_double_baseline_and_five_ms_increase():
    baseline = 1_000_000

    assert evaluate._perf_regressed(baseline * 2, baseline) is False
    assert evaluate._perf_regressed(baseline + 5_000_000, baseline) is False
    assert evaluate._perf_regressed(baseline + 5_000_001, baseline) is True

    message = evaluate._failure_message(
        {
            "metrics": {
                "median_ns": 9_000_001,
                "p95_ns": 9_000_002,
                "baseline_median_ns": 2_000_000,
            }
        }
    )
    assert "median_ns=9000001" in message
    assert "baseline_median_ns=2000000" in message


def test_injected_perf_regression_fails_named_scenario(tmp_path, monkeypatch):
    _write_baseline(tmp_path)
    monkeypatch.setenv("PONY_EVAL_MACHINE_CLASS", "test-machine")
    calls = []
    regressed = {
        scenario_id: metrics["median_ns"]
        for scenario_id, metrics in BASELINE["performance"].items()
    }
    regressed["build_request/medium"] = 9_000_001

    def runner(argv, cwd):
        del cwd
        calls.append(argv)
        module = argv[-1] if argv[:2] == [evaluate.sys.executable, "-m"] else ""
        stdout = (
            _perf_output(module, regressed)
            if module in dict(evaluate.PERF_RUNNERS)
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    payload, _json_path, _markdown_path = evaluate.run_evaluation(
        "core",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 1, tzinfo=timezone.utc),
    )

    row = next(
        item for item in payload["scenarios"] if item["id"] == "build_request/medium"
    )
    assert payload["status"] == "fail"
    assert row["status"] == "fail"
    assert row["exit_code"] == 0
    assert row["metrics"] == {
        "median_ns": 9_000_001,
        "p95_ns": 9_000_002,
        "baseline_median_ns": 2_000_000,
        "confirmation_run": True,
    }
    assert sum(argv[-1] == "benchmarks.perf.bench_request_build" for argv in calls) == 2


def test_perf_regression_gets_one_confirmation_run_and_can_recover():
    calls = {}

    def runner(argv, cwd):
        del cwd
        module = argv[-1]
        calls[module] = calls.get(module, 0) + 1
        medians = {
            scenario_id: metrics["median_ns"]
            for scenario_id, metrics in BASELINE["performance"].items()
        }
        if module == "benchmarks.perf.bench_request_build" and calls[module] == 1:
            medians["build_request/medium"] = 9_000_001
        return SimpleNamespace(
            returncode=0,
            stdout=_perf_output(module, medians),
            stderr="",
        )

    rows = evaluate._run_performance(
        BASELINE,
        runner=runner,
        root=Path.cwd(),
        artifact_path="artifacts/eval/result.json",
    )

    assert all(row["status"] == "pass" for row in rows)
    assert calls["benchmarks.perf.bench_request_build"] == 2
    assert calls["benchmarks.perf.bench_security"] == 1
    recovered = next(row for row in rows if row["id"] == "build_request/medium")
    assert recovered["metrics"]["confirmation_run"] is True


def test_core_rejects_mismatched_machine_before_running(tmp_path, monkeypatch):
    _write_baseline(tmp_path)
    monkeypatch.setenv("PONY_EVAL_MACHINE_CLASS", "other-machine")
    called = False

    def runner(argv, cwd):
        nonlocal called
        del argv, cwd
        called = True

    with pytest.raises(ValueError, match="baseline machine_class"):
        evaluate.run_evaluation("core-full", runner=runner, root=tmp_path)

    assert called is False


def test_core_functional_runs_without_a_performance_baseline(tmp_path):
    calls = []
    repo = tmp_path / "repo"
    output = tmp_path / "gate-output"
    repo.mkdir()

    def runner(argv, cwd):
        calls.append((argv, cwd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    payload, _json_path, _markdown_path = evaluate.run_evaluation(
        "core-functional",
        runner=runner,
        root=repo,
        output_dir=output,
        now=datetime(2026, 7, 12, 4, tzinfo=timezone.utc),
        system_name="linux",
    )

    assert payload["status"] == "pass"
    assert "baseline" not in payload["provenance"]
    assert [row["id"] for row in payload["scenarios"]] == [
        item[0] for item in evaluate._core_functional_commands()
    ]
    assert not any(argv[-1] in dict(evaluate.PERF_RUNNERS) for argv, _cwd in calls)
    assert payload["artifact_path"] == "20260712T040000000000Z-core-functional.json"
    assert (output / payload["artifact_path"]).is_file()
    assert not (repo / "artifacts").exists()



def test_logical_suites_split_fast_full_and_live_work():
    assert set(evaluate.SUITES) >= {
        "core-fast",
        "core-functional",
        "core-full",
        "live",
    }
    assert not {"sandbox", "sandbox-contract", "sandbox-real"} & set(evaluate.SUITES)
    assert [item[0] for item in evaluate._core_full_commands()] == [
        "core.ruff",
        "core.pytest",
        "core.memory-quality-fake",
        "core.fixed-benchmark",
        "core.build",
        "core.distribution",
    ]
    assert [item[0] for item in evaluate._core_functional_commands()] == [
        "core.memory-quality-fake",
        "core.fixed-benchmark",
    ]
    assert [item[0] for item in evaluate._core_fast_commands()] == [
        "core.ruff",
        "core.context-budget",
        "core.tool-security",
    ]



def test_pr_fast_suite_does_not_require_baseline(tmp_path):
    calls = []

    def runner(argv, cwd):
        calls.append((argv, cwd))
        return SimpleNamespace(returncode=0, stdout="3 passed", stderr="")

    fast, _, _ = evaluate.run_evaluation(
        "core-fast",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 5, tzinfo=timezone.utc),
    )
    assert fast["status"] == "pass"
    assert "baseline" not in fast["provenance"]
    assert len(calls) == len(evaluate._core_fast_commands())
    assert not any("srt_feasibility.py" in argv for argv, _ in calls)


def test_failed_child_output_is_redacted_and_not_stored(capsys, monkeypatch):
    monkeypatch.setenv("PONY_TEST_SECRET_TOKEN", "secret-value-123")
    rows = evaluate._run_functional(
        (("core.test", ("false",), "exit"),),
        runner=lambda _argv, _cwd: SimpleNamespace(
            returncode=1,
            stdout="pytest failed at test_named_scenario",
            stderr="token=secret-value-123",
        ),
        root=Path.cwd(),
        artifact_path="artifacts/eval/result.json",
    )

    output = capsys.readouterr().err
    assert rows[0]["status"] == "fail"
    assert "test_named_scenario" in output
    assert "secret-value-123" not in output
    assert "stdout" not in rows[0] and "stderr" not in rows[0]


def _write_live_env(root):
    (root / ".env").write_text(
        "PONY_PROVIDER=openai\n"
        "PONY_API_BASE=https://api.openai.com/v1\n"
        "PONY_MODEL=test-model\n"
        "PONY_API_KEY=test-key\n",
        encoding="utf-8",
    )


def test_live_requires_repo_env_before_calling_runner(tmp_path):
    called = False

    def runner(argv, cwd):
        nonlocal called
        del argv, cwd
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert evaluate.main(["--suite", "live"], runner=runner, root=tmp_path) == 2
    assert called is False


def test_live_provider_is_forwarded_to_existing_runner_without_output_leak(tmp_path):
    calls = []
    _write_live_env(tmp_path)

    def runner(argv, cwd):
        calls.append((argv, cwd))
        results = cwd / "benchmarks" / "live_e2e" / "results"
        results.mkdir(parents=True)
        (results / "live.json").write_text(
            json.dumps(_live_report()),
            encoding="utf-8",
        )
        return SimpleNamespace(
            returncode=0,
            stdout="provider prompt and tool result",
            stderr="private transport output",
        )

    payload, json_rel, _markdown_rel = evaluate.run_evaluation(
        "live",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 3, tzinfo=timezone.utc),
    )

    assert calls[0][0][-2:] == ["--repo-root", str(tmp_path.resolve())]
    serialized = (tmp_path / json_rel).read_text(encoding="utf-8")
    assert payload["status"] == "pass"
    assert "provider prompt" not in serialized
    assert "tool result" not in serialized
    assert "transport output" not in serialized


@pytest.mark.parametrize(
    "selector",
    (None, "auto", "openai", "openai-chat", "openai-responses"),
)
def test_live_evaluator_uses_the_resolved_openai_provider(selector, tmp_path):
    env_lines = [
        "PONY_API_BASE=https://gateway.example/v1",
        "PONY_MODEL=test-model",
        "PONY_API_KEY=test-key",
    ]
    if selector is not None:
        env_lines.insert(0, f"PONY_PROVIDER={selector}")
    (tmp_path / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    def runner(_argv, cwd):
        report = _live_report()
        if selector in {"openai-chat", "openai-responses"}:
            report["provider_resolution"]["protocol"] = (
                "openai_chat_completions"
                if selector == "openai-chat"
                else "openai_responses"
            )
        else:
            report["provider_resolution"] = {
                "status": "ok",
                "resolution_source": "probe",
                "protocol": "openai_chat_completions",
                "candidate_count": 1,
                "model_calls": 2,
                "usage_status": "complete",
            }
        results = cwd / "benchmarks" / "live_e2e" / "results"
        results.mkdir(parents=True)
        (results / "live.json").write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    payload, _json_rel, _markdown_rel = evaluate.run_evaluation(
        "live",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 4, tzinfo=timezone.utc),
    )

    assert payload["status"] == "pass"
    assert payload["provenance"]["provider"] == "openai"
    assert payload["scenarios"][0]["id"] == "live.openai"


def test_live_evaluator_rejects_a_forced_protocol_mismatch(tmp_path):
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=openai-responses\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_MODEL=test-model\n"
        "PONY_API_KEY=test-key\n",
        encoding="utf-8",
    )

    def runner(_argv, cwd):
        report = _live_report()
        report["provider_resolution"]["protocol"] = "openai_chat_completions"
        results = cwd / "benchmarks" / "live_e2e" / "results"
        results.mkdir(parents=True)
        (results / "live.json").write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    payload, _json_rel, _markdown_rel = evaluate.run_evaluation(
        "live",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 4, tzinfo=timezone.utc),
    )

    assert payload["status"] == "fail"


def test_live_report_requires_exact_head_and_required_gate_evidence():
    report = _live_report(git_head="wrong")
    assert evaluate._live_report_passed(report, "openai", "expected") is False

    report = _live_report(git_head="expected")
    del report["artifact_security"]
    assert evaluate._live_report_passed(report, "openai", "expected") is False

    report = _live_report(git_head="expected")
    report["global_assertions"] = [
        item
        for item in report["global_assertions"]
        if item["name"] != "fixture_restored_after_context_exit"
    ]
    report["assertion_summary"] = {"total": 8, "passed": 8, "failed": 0}
    assert evaluate._live_report_passed(report, "openai", "expected") is False


def test_live_report_requires_scanned_artifacts_and_exact_five_turn_envelopes():
    report = _live_report(git_head="expected")
    report["artifact_security"]["files_scanned"] = 0
    assert evaluate._live_report_passed(report, "openai", "expected") is False

    missing = _live_report(git_head="expected")
    missing["turns"].pop(2)
    duplicate = _live_report(git_head="expected")
    duplicate["turns"][2]["turn"] = 2
    unexpected = _live_report(git_head="expected")
    unexpected["turns"][2]["turn"] = 6
    for poisoned in (missing, duplicate, unexpected):
        assert evaluate._live_report_passed(poisoned, "openai", "expected") is False

    missing_field = _live_report(git_head="expected")
    del missing_field["turns"][0]["duration_ms"]
    extra_field = _live_report(git_head="expected")
    extra_field["turns"][0]["unexpected"] = True
    empty_assertions = _live_report(git_head="expected")
    empty_assertions["turns"][0]["assertions"] = []
    invalid_value = _live_report(git_head="expected")
    invalid_value["turns"][0]["usage_complete"] = False
    for poisoned in (missing_field, extra_field, empty_assertions, invalid_value):
        assert evaluate._live_report_passed(poisoned, "openai", "expected") is False


def test_live_report_keeps_assertion_names_dynamic():
    report = _live_report(git_head="expected")
    for turn in report["turns"]:
        turn["assertions"][0]["name"] = f"renamed_{turn['turn']}"
    report["global_assertions"][0]["name"] = "renamed_transport"
    report["global_assertions"][1]["name"] = "renamed_security"
    report["global_assertions"][2]["name"] = "renamed_persistence"

    assert evaluate._live_report_passed(report, "openai", "expected") is True


def test_live_report_accepts_usage_only_degradation():
    report = _live_report(git_head="expected")
    report["turns"][0]["usage_complete"] = False
    report["turns"][0]["billing_ambiguous"] = True
    report["turns"][0]["assertions"].append(
        {
            "name": "turn_usage_complete",
            "gate": "transport_cost",
            "passed": False,
        }
    )
    report["global_assertions"].append(
        {
            "name": "all_turn_usage_complete",
            "gate": "transport_cost",
            "passed": False,
        }
    )
    report["assertion_summary"] = {"total": 11, "passed": 9, "failed": 2}
    report["gates"]["transport_cost"].update(
        status="degraded",
        usage_complete=False,
        billing_ambiguous=True,
    )

    assert evaluate._live_report_passed(report, "openai", "expected") is True


def test_live_report_rejects_unproved_usage_degradation():
    report = _live_report(git_head="expected")
    report["turns"][0]["usage_complete"] = False
    report["turns"][0]["billing_ambiguous"] = True
    report["gates"]["transport_cost"].update(
        status="degraded",
        usage_complete=False,
        billing_ambiguous=True,
    )

    assert evaluate._live_report_passed(report, "openai", "expected") is False


@pytest.mark.parametrize("value", [None, 1, {}, {"unexpected": True}])
def test_live_report_rejects_malformed_provider_resolution(value):
    report = _live_report(git_head="expected")
    report["provider_resolution"] = value

    assert evaluate._live_report_passed(report, "openai", "expected") is False


@pytest.mark.parametrize(
    "updates",
    [
        {"protocol": "anthropic_messages"},
        {"resolution_source": "probe"},
        {
            "status": "ok",
            "resolution_source": "explicit",
            "candidate_count": 1,
            "model_calls": 2,
            "usage_status": "complete",
        },
    ],
)
def test_live_report_rejects_inconsistent_provider_resolution(updates):
    report = _live_report(git_head="expected")
    report["provider_resolution"].update(updates)

    assert evaluate._live_report_passed(report, "openai", "expected") is False


def test_live_report_requires_positive_assertion_for_every_gate():
    report = _live_report(git_head="expected")
    report["global_assertions"] = [
        item
        for item in report["global_assertions"]
        if item["gate"] != "transport_cost"
    ]
    report["assertion_summary"] = {"total": 8, "passed": 8, "failed": 0}

    assert evaluate._live_report_passed(report, "openai", "expected") is False


def test_live_exit_zero_without_current_v3_report_fails(tmp_path):
    _write_live_env(tmp_path)
    payload, _json_rel, _markdown_rel = evaluate.run_evaluation(
        "live",
        runner=lambda _argv, _cwd: SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        ),
        root=tmp_path,
        now=datetime(2026, 7, 12, 4, tzinfo=timezone.utc),
    )

    assert payload["status"] == "fail"
    assert payload["scenarios"][0]["status"] == "fail"


def test_artifact_scan_rejects_forbidden_fields_absolute_paths_and_secrets(
    tmp_path, monkeypatch
):
    payload = {
        "record_type": "pony_evaluation_result",
        "format_version": 1,
        "suite": "core",
        "status": "pass",
        "duration_ms": 1,
        "provenance": {
            "commit": "unknown",
            "dirty": "unknown",
            "python": "3.12.0",
            "platform": "linux",
            "architecture": "x86_64",
            "machine_class": "linux-x86_64",
            "baseline": "benchmarks/baselines/core-v1.json",
        },
        "artifact_path": "artifacts/eval/result.json",
        "scenarios": [
            {
                "id": "core.test",
                "status": "pass",
                "exit_code": 0,
                "duration_ms": 1,
                "artifact_path": "artifacts/eval/result.json",
            }
        ],
    }

    forbidden_field = json.loads(json.dumps(payload))
    forbidden_field["scenarios"][0]["stdout"] = "private"
    with pytest.raises(ValueError, match="scenario fields"):
        evaluate._validate_low_sensitivity(forbidden_field, "", tmp_path)

    absolute_path = json.loads(json.dumps(payload))
    absolute_path["provenance"]["machine_class"] = str(tmp_path)
    with pytest.raises(ValueError, match="absolute path"):
        evaluate._validate_low_sensitivity(absolute_path, "", tmp_path)

    embedded_path = json.loads(json.dumps(payload))
    embedded_path["provenance"]["machine_class"] = "canary /Users/private/file suffix"
    with pytest.raises(ValueError, match="absolute path"):
        evaluate._validate_low_sensitivity(embedded_path, "", tmp_path)

    monkeypatch.setenv("PONY_TEST_SECRET_TOKEN", "secret-value-123")
    leaked_secret = json.loads(json.dumps(payload))
    leaked_secret["provenance"]["machine_class"] = "secret-value-123"
    with pytest.raises(ValueError, match="forbidden content"):
        evaluate._validate_low_sensitivity(leaked_secret, "", tmp_path)
