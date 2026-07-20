import json
from copy import deepcopy

import pytest

from benchmarks.evaluation.experiments_synthetic import (
    run_context_ablation_v2,
    run_memory_ablation_v2,
)
from benchmarks.evaluation.metrics_reports import (
    aggregate_benchmark_artifact,
    write_benchmark_core_report,
)
from benchmarks.evaluation.provider_benchmark import _resolve_benchmark_target
from pony.agent.observability import RunArtifactError, project_trace_event
from pony.state.task_state import TaskState
from pony.runtime.options import RuntimeOptions


def _current_run_report(run_id):
    return {
        "record_type": "run_report",
        "format_version": 4,
        "run": {
            "run_id": run_id,
            "task_id": "task_1",
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "duration_ms": 12,
            "commit": "",
            "dirty": False,
        },
        "model": {
            "attempts": 1,
            "turns": 1,
            "failures": 0,
            "retries": 0,
            "transport_attempts": 1,
            "transport_retries": 0,
            "evidence_complete": True,
            "attempt_origin_counts": {"initial": 1},
            "failure_reason_counts": {},
            "usage": {
                "input_tokens": 40,
                "output_tokens": 5,
                "total_tokens": 45,
                "cached_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 10,
                "cache_hit": True,
            },
        },
        "context": {"messages_chars": 42, "prefix_changed": False},
        "tools": {
            "calls": 2,
            "allowed": 2,
            "denied": 0,
            "name_counts": {"read_file": 2},
            "status_counts": {"ok": 2},
        },
        "memory": {
            "recall_candidates": 0,
            "recall_selected": 0,
            "filter_counts": {},
        },
        "effects": {"changed_files": 0, "partial_successes": 0},
        "integrity": {"writer": "current", "terminal_event_expected": True},
        "finalization": {"status": "complete", "error_count": 0},
    }


def _write_current_run(runs_root, run_id):
    report = _current_run_report(run_id)
    state = TaskState.create(
        task_id=report["run"]["task_id"],
        run_id=run_id,
        user_request="inspect",
    )
    state.attempts = report["model"]["attempts"]
    state.tool_steps = report["tools"]["calls"]
    state.finish_success("done")
    events = [
        project_trace_event(
            state,
            "run_started",
            {},
            created_at="2026-07-12T00:00:00Z",
        ),
        project_trace_event(
            state,
            "tool_started",
            {"name": "read_file", "tool_use_id": "tool_1"},
            created_at="2026-07-12T00:00:00.004Z",
        ),
        project_trace_event(
            state,
            "tool_executed",
            {
                "name": "read_file",
                "tool_use_id": "tool_1",
                "tool_status": "ok",
            },
            created_at="2026-07-12T00:00:00.008Z",
        ),
        project_trace_event(
            state,
            "tool_started",
            {"name": "read_file", "tool_use_id": "tool_2"},
            created_at="2026-07-12T00:00:00.009Z",
        ),
        project_trace_event(
            state,
            "tool_executed",
            {
                "name": "read_file",
                "tool_use_id": "tool_2",
                "tool_status": "ok",
            },
            created_at="2026-07-12T00:00:00.010Z",
        ),
        project_trace_event(
            state,
            "run_finished",
            {"status": "completed", "run_duration_ms": 12},
            created_at="2026-07-12T00:00:00.012Z",
        ),
    ]
    run_dir = runs_root / run_id
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run_dir / "task_state.json").write_text(
        json.dumps(state.to_dict()),
        encoding="utf-8",
    )
    (run_dir / "trace.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return run_dir


def test_aggregate_run_artifacts_uses_canonical_report_truth_sources(tmp_path):
    from benchmarks.evaluation.metrics_reports import aggregate_run_artifacts

    _write_current_run(tmp_path, "run_1")

    metrics = aggregate_run_artifacts(tmp_path)

    assert metrics["run_count"] == 1
    assert metrics["avg_tool_steps"] == 2
    assert metrics["avg_request_messages_chars"] == 42
    assert "avg_prompt_chars" not in metrics
    assert metrics["cache_hit_rate"] == 1.0
    assert metrics["cached_token_ratio"] == 0.25
    assert metrics["prefix_reuse_rate"] == 1.0


def test_aggregate_run_artifacts_ignores_damaged_duplicate_and_legacy_runs(
    tmp_path,
):
    from benchmarks.evaluation.metrics_reports import aggregate_run_artifacts

    _write_current_run(tmp_path, "run_current")
    damaged = _write_current_run(tmp_path, "run_damaged")
    duplicate = _write_current_run(tmp_path, "run_duplicate")
    legacy = _write_current_run(tmp_path, "run_legacy")
    invalid_task = _write_current_run(tmp_path, "run_invalid_task")
    (damaged / "trace.jsonl").write_text("not-json\n", encoding="utf-8")
    (duplicate / "report.json").write_text(
        '{"record_type":"run_report","record_type":"run_report"}',
        encoding="utf-8",
    )
    (legacy / "report.json").write_text(
        json.dumps({"tool_steps": 999, "attempts": 999}),
        encoding="utf-8",
    )
    (invalid_task / "task_state.json").write_text("null", encoding="utf-8")

    metrics = aggregate_run_artifacts(tmp_path)

    assert metrics["run_count"] == 1
    assert metrics["avg_tool_steps"] == 2
    assert metrics["avg_attempts"] == 1
    assert metrics["stop_reason_counts"] == {"final_answer_returned": 1}


def test_aggregate_run_artifacts_counts_real_rejection_security_event(tmp_path):
    from pony import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext
    from benchmarks.evaluation.metrics_reports import aggregate_run_artifacts
    from benchmarks.support.fake_provider import FakeModelClient

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient(
            [{"name": "memory_save", "args": {"note": "remember this"}}, "done"]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True, read_only=True),
    )

    assert agent.ask("remember this") == "done"
    metrics = aggregate_run_artifacts(tmp_path / ".pony" / "runs")

    assert metrics["run_count"] == 1
    assert metrics["tool_status_counts"] == {"rejected": 1}
    assert metrics["security_event_counts"] == {"read_only_block": 1}


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_fixed_result_reader_rejects_noncurrent_header_before_business(
    tmp_path, version
):
    payload = {
        "record_type": "fixed_benchmark_result",
        "format_version": version,
        "rows": "poisoned-business-shape",
        "summary": "poisoned-business-shape",
    }
    if version is None:
        payload.pop("format_version")
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="format_version"):
        aggregate_benchmark_artifact(path)


def test_fixed_result_reader_rejects_wrong_type_and_nested_duplicates(tmp_path):
    wrong_type = tmp_path / "wrong-type.json"
    wrong_type.write_text(
        json.dumps(
            {
                "record_type": "fixed_benchmark_definition",
                "format_version": 1,
                "rows": "poisoned-business-shape",
            }
        ),
        encoding="utf-8",
    )
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"record_type":"fixed_benchmark_result","format_version":1,'
        '"rows":[],"summary":{"passed":1,"passed":2}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="record_type"):
        aggregate_benchmark_artifact(wrong_type)
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_benchmark_artifact(duplicate)


def test_provider_summary_reads_cache_from_completion_usage_totals():
    from benchmarks.evaluation.provider_benchmark import _provider_summary_from_artifact

    summary = _provider_summary_from_artifact(
        {
            "record_type": "fixed_benchmark_result",
            "format_version": 1,
            "rows": [
                {
                    "report": {
                        "model": {
                            "usage": {
                            "cached_tokens": 12,
                            "cache_hit": True,
                            }
                        }
                    },
                    "tool_steps": 1,
                    "attempts": 1,
                }
            ],
            "summary": {"total_tasks": 1, "pass_rate": 1.0},
        }
    )

    assert summary["avg_cached_tokens"] == 12.0
    assert summary["cache_hit_rate"] == 1.0


def test_provider_summary_validates_fixed_result_header_before_rows():
    from benchmarks.evaluation.provider_benchmark import _provider_summary_from_artifact

    with pytest.raises(ValueError, match="format_version"):
        _provider_summary_from_artifact(
            {
                "record_type": "fixed_benchmark_result",
                "format_version": True,
                "rows": "poisoned-business-shape",
            }
        )


@pytest.mark.parametrize("corruption", ["version", "duplicate"])
def test_collect_resume_metrics_rejects_invalid_provider_artifact(
    tmp_path, monkeypatch, corruption
):
    import benchmarks.evaluation.metrics_reports as metrics_reports

    benchmark = tmp_path / "fixed.json"
    benchmark.write_text(
        json.dumps(
            {
                "record_type": "fixed_benchmark_result",
                "format_version": 1,
                "rows": [],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    provider = tmp_path / "provider.json"
    if corruption == "version":
        provider.write_text(
            json.dumps(
                {
                    "record_type": "provider_experiment_result",
                    "format_version": True,
                    "providers": "poisoned-business-shape",
                }
            ),
            encoding="utf-8",
        )
        expected = "format_version"
    else:
        provider.write_text(
            '{"record_type":"provider_experiment_result","format_version":1,'
            '"providers":[],"nested":{"key":1,"key":2}}',
            encoding="utf-8",
        )
        expected = "duplicate"
    for name in (
        "build_stress_agent_metrics",
        "run_memory_dependency_experiment",
        "run_large_scale_memory_experiment",
        "run_context_stress_matrix",
        "run_security_experiment_suite",
    ):
        monkeypatch.setattr(metrics_reports, name, lambda **kwargs: {})

    with pytest.raises(ValueError, match=expected):
        metrics_reports.collect_resume_metrics(
            benchmark,
            runs,
            provider_experiments=provider,
        )


def test_run_context_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-ablation-v2.json"

    artifact = run_context_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["record_type"] == "context_ablation_result"
    assert artifact["format_version"] == 1
    assert artifact["config_count"] == 12
    assert len(artifact["configs"]) == 12
    assert "current_request_preserved_rate" in artifact["summary"]


def test_context_ablation_compares_compacted_and_uncompacted_sent_messages(tmp_path):
    artifact = run_context_ablation_v2(
        artifact_path=tmp_path / "context.json",
        repetitions=1,
    )
    summary = artifact["summary"]
    assert (
        summary["avg_compacted_request_chars"]
        < summary["avg_uncompacted_request_chars"]
    )
    assert summary["current_request_preserved_rate"] == 1.0
    assert summary["canonical_history_preserved_rate"] == 1.0
    assert all(
        config["canonical_messages_dropped"] == 0 for config in artifact["configs"]
    )


def test_request_preview_restores_the_canonical_session(tmp_path):
    from pony import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext
    from benchmarks.evaluation.experiments_synthetic import (
        _seed_plain_messages,
        measure_request_ablation_metrics,
    )
    from benchmarks.support.fake_provider import FakeModelClient

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )
    _seed_plain_messages(agent, 4, "history", 80)
    before_session = agent.session
    before_messages = deepcopy(agent.session["messages"])

    measure_request_ablation_metrics(agent, "preview this request")

    assert agent.session is before_session
    assert agent.session["messages"] == before_messages


def test_provider_benchmark_uses_canonical_project_env(tmp_path):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PONY_PROVIDER=openai-chat",
                "PONY_API_BASE=https://gateway.example/v1",
                "PONY_MODEL=gpt-test",
                "PONY_API_KEY=sk-project",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    target = _resolve_benchmark_target(tmp_path, process_env={})

    assert target["provider"] == "openai-chat"
    assert target["api_key"] == "sk-project"
    assert target["model"] == "gpt-test"
    assert target["base_url"] == "https://gateway.example/v1"
    assert target["transport"] == "openai_chat_completions"
    assert target["auth_mode"] == "bearer"


def test_provider_benchmark_rejects_vendor_only_environment(tmp_path):
    with pytest.raises(ValueError, match="^api_base_not_configured$"):
        _resolve_benchmark_target(
            tmp_path,
            process_env={"PONY_OPENAI_API_KEY": "sk-old"},
        )


def test_provider_benchmark_rejects_unresolved_target_before_workload(tmp_path):
    with pytest.raises(ValueError, match="^provider_detection_failed$"):
        _resolve_benchmark_target(
            tmp_path,
            project_env={
                "PONY_API_BASE": "https://gateway.example/v1",
                "PONY_API_KEY": "test-key",
                "PONY_MODEL": "gateway-model",
            },
            process_env={},
        )


def test_real_memory_request_recorder_captures_native_messages():
    from benchmarks.evaluation.experiments_real import (
        _first_followup_drops_bootstrap_tool,
        _recording_provider,
    )

    class NativeProvider:
        def complete(self, **kwargs):
            return kwargs

    native = _recording_provider(NativeProvider())
    messages = [{"role": "assistant", "content": [{"id": "tu_1"}]}]
    assert (
        native.complete(
        system=[],
        tools=[],
        messages=messages,
        max_tokens=10,
        )["messages"]
        == messages
    )
    assert native.calls == [("messages", messages)]
    assert _first_followup_drops_bootstrap_tool(native, 0, "tu_1") is False
    assert _first_followup_drops_bootstrap_tool(native, 1, "tu_1") is False

    assert _first_followup_drops_bootstrap_tool(native, 0, "") is False


def test_real_followup_metrics_rejects_missing_run_artifact(tmp_path):
    from pony import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext
    from benchmarks.evaluation.experiments_real import _followup_trace_metrics
    from benchmarks.support.fake_provider import FakeModelClient

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient(["done"]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )
    agent.ask("finish")
    agent.run_store.report_path(agent.current_task_state).unlink()

    with pytest.raises(RunArtifactError, match="missing"):
        _followup_trace_metrics(agent)


def test_memory_summary_detector_requires_nonempty_working_set_line():
    from benchmarks.evaluation.experiments_synthetic import (
        _prompt_has_reusable_file_summary,
    )

    expected_line = "- facts.txt: deploy key is red"
    indexed_prompt = "\n".join(
        [
            "<pony:task_working_set>",
            "",
            expected_line,
            "</pony:task_working_set>",
        ]
    )
    assert _prompt_has_reusable_file_summary(indexed_prompt, expected_line)
    assert not _prompt_has_reusable_file_summary(indexed_prompt, "")


def test_memory_summary_detector_rejects_line_after_closed_working_set():
    from benchmarks.evaluation.experiments_synthetic import (
        _prompt_has_reusable_file_summary,
    )

    expected_line = "- facts.txt: deploy key is red"
    assert not _prompt_has_reusable_file_summary(
        "\n".join(
            [
                "<pony:task_working_set>",
                "</pony:task_working_set>",
                expected_line,
            ]
        ),
        expected_line,
    )


def test_memory_ablation_reports_no_bootstrap_drop_without_samples(tmp_path):
    from benchmarks.evaluation.experiments_real import run_real_memory_experiment
    from benchmarks.evaluation.experiments_synthetic import (
        run_large_scale_memory_experiment,
        run_memory_dependency_experiment,
    )

    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=ollama\n"
        "PONY_API_BASE=http://127.0.0.1:11434\n"
        "PONY_MODEL=qwen-test\n"
        "PONY_API_KEY=\n",
        encoding="utf-8",
    )
    variant_sets = (
        run_memory_dependency_experiment(repetitions=0),
        run_large_scale_memory_experiment(repetitions=0)["variants"],
        run_real_memory_experiment(repo_root=tmp_path, repetitions=0)["variants"],
    )
    for variants in variant_sets:
        assert all(
            not variant["bootstrap_tool_turn_dropped"] for variant in variants.values()
        )


def test_run_memory_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "memory-ablation-v2.json"

    artifact = run_memory_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["record_type"] == "memory_ablation_result"
    assert artifact["format_version"] == 1
    assert artifact["task_count"] == 12
    assert set(artifact["variants"]) == {"memory_on", "memory_off", "memory_irrelevant"}
    assert "memory_hit_rate" in artifact["variants"]["memory_on"]
    on = artifact["variants"]["memory_on"]
    off = artifact["variants"]["memory_off"]
    irrelevant = artifact["variants"]["memory_irrelevant"]
    assert on["bootstrap_tool_turn_dropped"] is True
    assert off["bootstrap_tool_turn_dropped"] is True
    assert irrelevant["bootstrap_tool_turn_dropped"] is True
    assert on["repeated_reads"] < off["repeated_reads"]
    assert on["repeated_reads"] < irrelevant["repeated_reads"]
    assert on["memory_hit_rate"] > off["memory_hit_rate"]
    assert on["memory_hit_rate"] > irrelevant["memory_hit_rate"]
    assert (
        on["correct_rate"] == off["correct_rate"] == irrelevant["correct_rate"] == 1.0
    )


def test_write_benchmark_core_report_marks_current_metrics(tmp_path):
    run_context_ablation_v2(
        tmp_path / "artifacts" / "context-ablation-v2.json", repetitions=1
    )
    run_memory_ablation_v2(
        tmp_path / "artifacts" / "memory-ablation-v2.json", repetitions=1
    )
    harness_artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"
    harness_artifact_path.write_text(
        '{"record_type":"fixed_benchmark_result","format_version":1,"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"failure_category_counts":{}}',
        encoding="utf-8",
    )

    report_path = tmp_path / "docs" / "metrics" / "pony-benchmark-core-report.md"
    report_text = write_benchmark_core_report(
        report_path=report_path,
        harness_artifact_path=harness_artifact_path,
        context_artifact_path=tmp_path / "artifacts" / "context-ablation-v2.json",
        memory_artifact_path=tmp_path / "artifacts" / "memory-ablation-v2.json",
    )

    assert report_path.exists()
    assert "可以安全写进简历的指标" in report_text
    assert "只适合放文档/面试展开的指标" in report_text
    assert "memory_hit_rate" in report_text


@pytest.mark.parametrize(
    ("name", "record_type"),
    [
        ("harness", "fixed_benchmark_result"),
        ("context", "context_ablation_result"),
        ("memory", "memory_ablation_result"),
    ],
)
def test_core_report_rejects_each_noncurrent_input_before_business(
    tmp_path, name, record_type
):
    payloads = {
        "harness": {
            "record_type": "fixed_benchmark_result",
            "format_version": 1,
            "summary": {},
        },
        "context": {
            "record_type": "context_ablation_result",
            "format_version": 1,
            "summary": {},
        },
        "memory": {
            "record_type": "memory_ablation_result",
            "format_version": 1,
            "variants": {},
        },
    }
    payloads[name] = {
        "record_type": record_type,
        "format_version": True,
        "summary": "poisoned-business-shape",
        "variants": "poisoned-business-shape",
    }
    paths = {}
    for family, payload in payloads.items():
        path = tmp_path / f"{family}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[family] = path

    with pytest.raises(ValueError, match="format_version"):
        write_benchmark_core_report(
            report_path=tmp_path / "report.md",
            harness_artifact_path=paths["harness"],
            context_artifact_path=paths["context"],
            memory_artifact_path=paths["memory"],
        )


def test_run_provider_experiments_runs_only_repo_env_target(tmp_path, monkeypatch):
    from benchmarks.evaluation.provider_benchmark import run_provider_experiments

    seen = []

    def fake_resolve_target(repo_root):
        seen.append(repo_root)
        return {
            "provider": "openai",
            "transport": "openai_responses",
            "variant": "responses",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test-key",
            "auth_mode": "bearer",
            "capabilities": {},
        }

    monkeypatch.setattr(
        "benchmarks.evaluation.provider_benchmark._resolve_benchmark_target",
        fake_resolve_target,
    )
    monkeypatch.setattr(
        "benchmarks.evaluation.provider_benchmark.run_fixed_benchmark",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline stop")),
    )

    payload = run_provider_experiments(
        benchmark_path=tmp_path / "benchmarks.json",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
        repo_root=tmp_path,
    )

    assert seen == [tmp_path]
    assert payload["record_type"] == "provider_experiment_result"
    assert payload["format_version"] == 1
    assert len(payload["providers"]) == 1
    assert payload["providers"][0] == {
        "provider": "openai",
        "variant": "responses",
        "status": "error",
        "model": "gpt-test",
        "reason": "offline stop",
    }


def test_provider_script_writes_current_provider_family(tmp_path, monkeypatch):
    from scripts.evaluation import run_provider_experiments as script

    payload = {
        "record_type": "provider_experiment_result",
        "format_version": 1,
        "providers": [],
    }
    monkeypatch.setattr(script, "run_provider_experiments", lambda **kwargs: payload)
    output = tmp_path / "providers.json"

    assert script.main(["--output-json", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_large_scale_script_versions_independent_family_outputs(tmp_path, monkeypatch):
    from scripts.evaluation import run_large_scale_experiments as script

    provider_payload = {
        "record_type": "provider_experiment_result",
        "format_version": 1,
        "providers": [],
    }
    metrics = {
        "memory_large_experiment": {"variants": {}},
        "context_experiment": {"configs": []},
        "security_experiment": {"scenarios": []},
    }
    monkeypatch.setattr(
        script, "run_provider_experiments", lambda **kwargs: provider_payload
    )
    monkeypatch.setattr(
        script, "collect_resume_metrics", lambda *args, **kwargs: metrics
    )
    monkeypatch.setattr(
        script, "render_resume_metrics_markdown", lambda payload: "resume"
    )
    monkeypatch.setattr(
        script, "render_large_scale_experiment_report", lambda payload: "report"
    )
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("provider", "resume", "memory", "context", "security")
    }

    assert (
        script.main(
        [
                "--benchmark-artifact",
                str(tmp_path / "fixed.json"),
                "--runs-root",
                str(tmp_path / "runs"),
                "--provider-output-json",
                str(paths["provider"]),
                "--resume-output-json",
                str(paths["resume"]),
                "--resume-output-markdown",
                str(tmp_path / "resume.md"),
                "--memory-output-json",
                str(paths["memory"]),
                "--context-output-json",
                str(paths["context"]),
                "--security-output-json",
                str(paths["security"]),
                "--final-report-markdown",
                str(tmp_path / "report.md"),
        ]
        )
        == 0
    )
    assert json.loads(paths["provider"].read_text(encoding="utf-8")) == provider_payload
    memory = json.loads(paths["memory"].read_text(encoding="utf-8"))
    context = json.loads(paths["context"].read_text(encoding="utf-8"))
    assert (memory["record_type"], memory["format_version"]) == (
        "memory_ablation_result",
        1,
    )
    assert (context["record_type"], context["format_version"]) == (
        "context_ablation_result",
        1,
    )
