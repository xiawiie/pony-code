from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import docker_sandbox_release as sandbox_release
from scripts import evaluate
from tests.release_authority_fixture import runtime_case_rows


BASELINE = {
    "record_type": "pico_evaluation_baseline",
    "format_version": 1,
    "suite": "core",
    "machine_class": "test-machine",
    "performance": {
        "build_request/medium": {"median_ns": 2_000_000},
        "security/redact_artifact/100": {"median_ns": 40_000_000},
        "shell/assess_corpus/50": {"median_ns": 1_500_000},
        "recovery/pending_reviews/200": {"median_ns": 4_500_000},
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
        "persistence": {"name": "persistence_ok", "gate": "persistence", "passed": True},
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
        "format_version": 2,
        "run_id": "live-e2e-1",
        "provider": "deepseek",
        "model": "test-model",
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
            "behavior": {"status": "pass", "model_turns": 5, "model_failures": 0},
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
    monkeypatch.setenv("PICO_EVAL_MACHINE_CLASS", "test-machine")
    calls = []
    secret_output = "prompt=private tool result stdout stderr /Users/private/repo"

    def runner(argv, cwd):
        calls.append((argv, cwd))
        module = argv[-1] if argv[:2] == [evaluate.sys.executable, "-m"] else ""
        stdout = _perf_output(module) if module in dict(evaluate.PERF_RUNNERS) else secret_output
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
        "core.recovery-ablation",
        "core.build",
        "core.distribution",
        *BASELINE["performance"],
    }
    assert any("benchmarks/memory_quality/run_benchmark.py" in argv for argv, _ in calls)
    assert any(argv[:2] == ["uv", "build"] for argv, _ in calls)
    assert {module for module, _ids in evaluate.PERF_RUNNERS} <= {
        argv[-1] for argv, _ in calls
    }
    assert sum(argv[:3] == [evaluate.sys.executable, "-m", "pytest"] for argv, _ in calls) == 1

    serialized = (tmp_path / json_rel).read_text(encoding="utf-8")
    markdown = (tmp_path / markdown_rel).read_text(encoding="utf-8")
    for forbidden in (secret_output, "/Users/private/repo", "prompt=private", "tool result"):
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
    monkeypatch.setenv("PICO_EVAL_MACHINE_CLASS", "test-machine")
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
        stdout = _perf_output(module, regressed) if module in dict(evaluate.PERF_RUNNERS) else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    payload, _json_path, _markdown_path = evaluate.run_evaluation(
        "core",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 1, tzinfo=timezone.utc),
    )

    row = next(item for item in payload["scenarios"] if item["id"] == "build_request/medium")
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
    assert calls["benchmarks.perf.bench_security_recovery"] == 1
    recovered = next(row for row in rows if row["id"] == "build_request/medium")
    assert recovered["metrics"]["confirmation_run"] is True


def test_core_rejects_mismatched_machine_before_running(tmp_path, monkeypatch):
    _write_baseline(tmp_path)
    monkeypatch.setenv("PICO_EVAL_MACHINE_CLASS", "other-machine")
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

    def runner(argv, cwd):
        calls.append((argv, cwd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    payload, _json_path, _markdown_path = evaluate.run_evaluation(
        "core-functional",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 4, tzinfo=timezone.utc),
        system_name="linux",
    )

    assert payload["status"] == "pass"
    assert "baseline" not in payload["provenance"]
    assert [row["id"] for row in payload["scenarios"]] == [
        item[0] for item in evaluate._core_full_commands()
    ]
    assert not any(argv[-1] in dict(evaluate.PERF_RUNNERS) for argv, _cwd in calls)


def test_logical_suites_split_fast_full_contract_and_real_work():
    assert set(evaluate.SUITES) >= {
        "core-fast",
        "core-functional",
        "core-full",
        "sandbox-contract",
        "sandbox-real",
        "live",
    }
    contract = evaluate._sandbox_contract_commands()[0][1]
    assert "tests/test_docker_sandbox_session.py" in contract
    assert "tests/test_docker_sandbox_runner.py" in contract
    assert "tests/test_docker_sandbox_runtime.py" in contract
    assert "tests/test_docker_sandbox_cli.py" in contract
    assert "tests/test_sandbox_apply.py" in contract
    assert not any("test_sandbox_toolchain.py" in item for item in contract)
    assert "-k" not in contract
    assert [item[0] for item in evaluate._core_full_commands()] == [
        "core.ruff",
        "core.pytest",
        "core.memory-quality-fake",
        "core.fixed-benchmark",
        "core.recovery-ablation",
        "core.build",
        "core.distribution",
    ]
    assert [item[0] for item in evaluate._core_fast_commands()] == [
        "core.ruff",
        "core.context-budget",
        "core.tool-security",
    ]


def test_sandbox_real_forwards_required_external_fixtures(monkeypatch, tmp_path):
    mount_fixture = tmp_path / "mount-fixture"
    device_fixture = tmp_path / "device-fixture"
    monkeypatch.setenv("PICO_SANDBOX_MOUNT_FIXTURE", str(mount_fixture))
    monkeypatch.setenv("PICO_SANDBOX_DEVICE_FIXTURE", str(device_fixture))

    commands = evaluate._sandbox_real_commands("linux")
    vertical = commands[1][1]

    assert vertical[vertical.index("--mount-fixture-source") + 1] == str(
        mount_fixture
    )
    assert vertical[vertical.index("--device-fixture-source") + 1] == str(
        device_fixture
    )


def test_pr_suites_do_not_require_baseline_or_real_sandbox(tmp_path):
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
    contract, _, _ = evaluate.run_evaluation(
        "sandbox-contract",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 6, tzinfo=timezone.utc),
    )

    assert fast["status"] == contract["status"] == "pass"
    assert "baseline" not in fast["provenance"]
    assert len(calls) == len(evaluate._core_fast_commands()) + 1
    assert not any("srt_feasibility.py" in argv for argv, _ in calls)


def test_failed_child_output_is_redacted_and_not_stored(capsys, monkeypatch):
    monkeypatch.setenv("PICO_TEST_SECRET_TOKEN", "secret-value-123")
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


def test_sandbox_contract_rejects_skip_and_xfail_summaries():
    for output in ("1 passed, 1 skipped", "1 passed, 1 xfailed", "1 xpassed"):
        assert evaluate._functional_passed("no_skip", 0, output) is False


def test_sandbox_fails_on_pytest_skip_and_invalid_vertical(tmp_path):
    calls = 0

    def runner(argv, cwd):
        nonlocal calls
        del cwd
        calls += 1
        if "pytest" in argv:
            return SimpleNamespace(returncode=0, stdout="1 passed, 1 skipped", stderr="")
        if "docker_sandbox_release.py" in argv:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    payload, _json_path, _markdown_path = evaluate.run_evaluation(
        "sandbox",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 2, tzinfo=timezone.utc),
        system_name="darwin",
    )

    assert calls == 3
    assert payload["status"] == "fail"
    assert [row["status"] for row in payload["scenarios"]] == [
        "fail",
        "pass",
        "fail",
    ]


def test_mandatory_sandbox_artifacts_require_exact_complete_schema():
    assert evaluate._docker_vertical_passed("{}", "darwin") is False

    expected = sandbox_release.validate_expected_manifest(
        {
            "record_type": "docker_sandbox_release_expected",
            "format_version": 1,
            "release_nonce": "a" * 64,
            "commit": "b" * 40,
            "distribution_sha256": "sha256:" + "1" * 64,
            "sdist_sha256": "sha256:" + "6" * 64,
            "image_set_digest": "sha256:" + "5" * 64,
            "images": [
                {
                    "platform": "linux/arm64",
                    "architecture": "arm64",
                    "image_digest": "sha256:" + "3" * 64,
                    "image_id": "sha256:" + "7" * 64,
                    "registry_reference": (
                        "registry.example/pico@sha256:" + "3" * 64
                    ),
                },
                {
                    "platform": "linux/amd64",
                    "architecture": "amd64",
                    "image_digest": "sha256:" + "8" * 64,
                    "image_id": "sha256:" + "9" * 64,
                    "registry_reference": (
                        "registry.example/pico@sha256:" + "8" * 64
                    ),
                },
            ],
            "policy_digest": "sha256:" + "4" * 64,
            "corpus_digest": sandbox_release.CORPUS_DIGEST,
            "jobs": [dict(job) for job in sandbox_release.expected_release_jobs()],
        }
    )
    job = expected["jobs"][0]
    artifact = sandbox_release._base_artifact(
        expected["distribution_sha256"],
        "sha256:" + "2" * 64,
        SimpleNamespace(
            image_set_digest=expected["image_set_digest"],
            reference=expected["images"][0]["image_digest"],
            policy_digest=expected["policy_digest"],
        ),
    )
    artifact.update(
        {
            "status": "passed",
            "reason_code": "mandatory_checks_passed",
            "platform": job["platform"],
            "architecture": job["architecture"],
            "engine_profile": job["engine_profile"],
            "mandatory_passed": len(sandbox_release.MANDATORY_CHECK_IDS),
            "mandatory_failed": 0,
            "container_calls": 1,
            "target_started_count": 1,
            "release_binding": sandbox_release.release_binding(
                expected,
                job["job_id"],
            ),
        }
    )
    for check in artifact["checks"]:
        check.update(status="pass", reason_code="verified")
    sandbox_release._set_case_evidence(
        artifact,
        "complete",
        "verified",
        runtime_case_rows(),
    )

    serialized = json.dumps(artifact)
    assert evaluate._docker_vertical_passed(serialized, "darwin") is True
    assert evaluate._docker_vertical_passed(serialized, "linux") is False

    artifact["unexpected"] = True
    assert evaluate._docker_vertical_passed(json.dumps(artifact), "darwin") is False


def test_live_requires_provider_before_calling_runner(tmp_path):
    called = False

    def runner(argv, cwd):
        nonlocal called
        del argv, cwd
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(SystemExit) as raised:
        evaluate.main(["--suite", "live"], runner=runner, root=tmp_path)

    assert raised.value.code == 2
    assert called is False


def test_live_provider_is_forwarded_to_existing_runner_without_output_leak(tmp_path):
    calls = []

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
        "deepseek",
        runner=runner,
        root=tmp_path,
        now=datetime(2026, 7, 12, 3, tzinfo=timezone.utc),
    )

    assert calls[0][0][-2:] == ["--provider", "deepseek"]
    serialized = (tmp_path / json_rel).read_text(encoding="utf-8")
    assert payload["status"] == "pass"
    assert "provider prompt" not in serialized
    assert "tool result" not in serialized
    assert "transport output" not in serialized


def test_live_report_requires_exact_head_and_required_gate_evidence():
    report = _live_report(git_head="wrong")
    assert evaluate._live_report_passed(report, "deepseek", "expected") is False

    report = _live_report(git_head="expected")
    del report["artifact_security"]
    assert evaluate._live_report_passed(report, "deepseek", "expected") is False

    report = _live_report(git_head="expected")
    report["global_assertions"] = [
        item
        for item in report["global_assertions"]
        if item["name"] != "fixture_restored_after_context_exit"
    ]
    report["assertion_summary"] = {"total": 8, "passed": 8, "failed": 0}
    assert evaluate._live_report_passed(report, "deepseek", "expected") is False


def test_live_report_requires_scanned_artifacts_and_exact_five_turn_envelopes():
    report = _live_report(git_head="expected")
    report["artifact_security"]["files_scanned"] = 0
    assert evaluate._live_report_passed(report, "deepseek", "expected") is False

    missing = _live_report(git_head="expected")
    missing["turns"].pop(2)
    duplicate = _live_report(git_head="expected")
    duplicate["turns"][2]["turn"] = 2
    unexpected = _live_report(git_head="expected")
    unexpected["turns"][2]["turn"] = 6
    for poisoned in (missing, duplicate, unexpected):
        assert evaluate._live_report_passed(poisoned, "deepseek", "expected") is False

    missing_field = _live_report(git_head="expected")
    del missing_field["turns"][0]["duration_ms"]
    extra_field = _live_report(git_head="expected")
    extra_field["turns"][0]["unexpected"] = True
    empty_assertions = _live_report(git_head="expected")
    empty_assertions["turns"][0]["assertions"] = []
    invalid_value = _live_report(git_head="expected")
    invalid_value["turns"][0]["usage_complete"] = False
    for poisoned in (missing_field, extra_field, empty_assertions, invalid_value):
        assert evaluate._live_report_passed(poisoned, "deepseek", "expected") is False


def test_live_report_keeps_assertion_names_dynamic():
    report = _live_report(git_head="expected")
    for turn in report["turns"]:
        turn["assertions"][0]["name"] = f"renamed_{turn['turn']}"
    report["global_assertions"][0]["name"] = "renamed_transport"
    report["global_assertions"][1]["name"] = "renamed_security"
    report["global_assertions"][2]["name"] = "renamed_persistence"

    assert evaluate._live_report_passed(report, "deepseek", "expected") is True


def test_live_exit_zero_without_current_v2_report_fails(tmp_path):
    payload, _json_rel, _markdown_rel = evaluate.run_evaluation(
        "live",
        "deepseek",
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
        "record_type": "pico_evaluation_result",
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
            "sandbox_image_digest": "sha256:" + "1" * 64,
            "sandbox_policy_digest": "sha256:" + "2" * 64,
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

    monkeypatch.setenv("PICO_TEST_SECRET_TOKEN", "secret-value-123")
    leaked_secret = json.loads(json.dumps(payload))
    leaked_secret["provenance"]["machine_class"] = "secret-value-123"
    with pytest.raises(ValueError, match="forbidden content"):
        evaluate._validate_low_sensitivity(leaked_secret, "", tmp_path)
