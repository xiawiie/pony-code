import json
import os
from copy import deepcopy
from unittest.mock import patch

import pytest

from pico.evaluation.experiments_recovery import (
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
)
from pico.evaluation.metrics_reports import (
    aggregate_benchmark_artifact,
    write_benchmark_core_report,
)
from pico.evaluation.provider_benchmark import _provider_profile


def test_aggregate_run_artifacts_uses_canonical_report_truth_sources(tmp_path):
    from pico.evaluation.metrics_reports import aggregate_run_artifacts

    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "tool_steps": 2,
                "attempts": 1,
                "last_request_metadata": {
                    "messages_chars": 42,
                    "prefix_changed": False,
                },
                "completion_usage_totals": {
                    "cached_tokens": 10,
                    "cache_hit": True,
                    "input_tokens": 40,
                },
            }
        ),
        encoding="utf-8",
    )

    metrics = aggregate_run_artifacts(tmp_path)

    assert metrics["avg_request_messages_chars"] == 42
    assert "avg_prompt_chars" not in metrics
    assert metrics["cache_hit_rate"] == 1.0
    assert metrics["cached_token_ratio"] == 0.25
    assert metrics["prefix_reuse_rate"] == 1.0


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
    from pico.evaluation.provider_benchmark import _provider_summary_from_artifact

    summary = _provider_summary_from_artifact(
        {
            "record_type": "fixed_benchmark_result",
            "format_version": 1,
            "rows": [
                {
                    "report": {
                        "completion_usage_totals": {
                            "cached_tokens": 12,
                            "cache_hit": True,
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
    from pico.evaluation.provider_benchmark import _provider_summary_from_artifact

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
    import pico.evaluation.metrics_reports as metrics_reports

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


def test_context_ablation_compares_bounded_and_unbounded_sent_messages(tmp_path):
    artifact = run_context_ablation_v2(
        artifact_path=tmp_path / "context.json",
        repetitions=1,
    )
    summary = artifact["summary"]
    assert summary["avg_bounded_request_chars"] < summary["avg_unbounded_request_chars"]
    assert summary["current_request_preserved_rate"] == 1.0
    assert all(
        config["bounded_dropped_messages"] > 0
        for config in artifact["configs"]
        if config["history_level"] == "long"
    )


def test_request_preview_restores_the_canonical_session(tmp_path):
    from pico import Pico, SessionStore, WorkspaceContext
    from pico.evaluation.experiments_synthetic import (
        _seed_plain_messages,
        measure_request_ablation_metrics,
    )
    from pico.providers.fake import FakeModelClient

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )
    _seed_plain_messages(agent, 4, "history", 80)
    before_session = agent.session
    before_messages = deepcopy(agent.session["messages"])

    measure_request_ablation_metrics(agent, "preview this request")

    assert agent.session is before_session
    assert agent.session["messages"] == before_messages


def test_provider_profile_loads_project_env_before_reading_deepseek_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "PICO_DEEPSEEK_API_KEY=sk-project-deepseek",
                "PICO_DEEPSEEK_MODEL=deepseek-v4-pro",
                "PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "DEEPSEEK_API_KEY": "sk-legacy-deepseek",
            "DEEPSEEK_MODEL": "legacy-deepseek-model",
            "DEEPSEEK_API_BASE": "https://legacy.deepseek.example/anthropic",
        },
        clear=True,
    ):
        profile = _provider_profile("deepseek")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-project-deepseek"
    assert profile["model"] == "deepseek-v4-pro"
    assert profile["base_url"] == "https://api.deepseek.com/anthropic"


def test_provider_profile_uses_shared_key_for_gpt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with patch.dict(os.environ, {"PICO_API_KEY": "sk-shared"}, clear=True):
        profile = _provider_profile("gpt")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-shared"
    assert profile["model"] == "gpt-5.4"


def test_real_memory_request_recorder_captures_structured_and_text_input():
    from pico.evaluation.experiments_real import (
        _first_followup_drops_bootstrap_tool,
        _recording_provider,
    )
    from pico.providers.text_protocol_adapter import TextProtocolAdapter

    class NativeProvider:
        def complete(self, **kwargs):
            return kwargs

    native = _recording_provider(NativeProvider())
    messages = [{"role": "assistant", "content": [{"id": "tu_1"}]}]
    assert native.complete(
        system=[],
        tools=[],
        messages=messages,
        max_tokens=10,
    )["messages"] == messages
    assert native.calls == [("messages", messages)]
    assert _first_followup_drops_bootstrap_tool(native, 0, "tu_1") is False
    assert _first_followup_drops_bootstrap_tool(native, 1, "tu_1") is False

    class TextProvider:
        last_completion_metadata = {}

        def complete_text(self, prompt, max_tokens):
            del max_tokens
            return prompt

    text_adapter = _recording_provider(TextProtocolAdapter(TextProvider()))
    text_adapter.complete(
        system=[],
        tools=[],
        messages=[{"role": "user", "content": "record the wire prompt"}],
        max_tokens=10,
    )
    assert text_adapter.calls[0][0] == "prompt"
    assert "record the wire prompt" in text_adapter.calls[0][1]
    assert _first_followup_drops_bootstrap_tool(text_adapter, 0, "tu_1") is True
    assert _first_followup_drops_bootstrap_tool(text_adapter, 0, "") is False


def test_memory_summary_detector_requires_nonempty_index_bound_line():
    from pico.evaluation.experiments_synthetic import _prompt_has_reusable_file_summary

    expected_line = "facts.txt -> deploy key is red"
    indexed_prompt = "\n".join(
        [
            "<pico:memory_index>",
            "Recent working file summaries:",
            "",
            expected_line,
            "</pico:memory_index>",
        ]
    )
    assert _prompt_has_reusable_file_summary(indexed_prompt, expected_line)
    assert not _prompt_has_reusable_file_summary(indexed_prompt, "")


def test_memory_summary_detector_rejects_line_after_closed_index():
    from pico.evaluation.experiments_synthetic import _prompt_has_reusable_file_summary

    expected_line = "facts.txt -> deploy key is red"
    assert not _prompt_has_reusable_file_summary(
        "\n".join(
            [
                "<pico:memory_index>",
                "</pico:memory_index>",
                "Recent working file summaries:",
                expected_line,
            ]
        ),
        expected_line,
    )


def test_memory_ablation_reports_no_bootstrap_drop_without_samples():
    from pico.evaluation.experiments_real import run_real_memory_experiment
    from pico.evaluation.experiments_synthetic import (
        run_large_scale_memory_experiment,
        run_memory_dependency_experiment,
    )

    variant_sets = (
        run_memory_dependency_experiment(repetitions=0),
        run_large_scale_memory_experiment(repetitions=0)["variants"],
        run_real_memory_experiment(repetitions=0)["variants"],
    )
    for variants in variant_sets:
        assert all(
            not variant["bootstrap_tool_turn_dropped"]
            for variant in variants.values()
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
    assert on["correct_rate"] == off["correct_rate"] == irrelevant["correct_rate"] == 1.0


def test_run_recovery_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "recovery-ablation-v2.json"

    artifact = run_recovery_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["record_type"] == "recovery_ablation_result"
    assert artifact["format_version"] == 1
    assert artifact["task_count"] == 8
    assert set(artifact["variants"]) == {"resume_enabled", "resume_disabled"}
    assert set(artifact["variants"]["resume_enabled"]["summary"]) >= {
        "resume_success_rate",
        "stale_reanchor_rate",
        "workspace_drift_detection_rate",
        "resume_false_accept_rate",
    }


def test_write_benchmark_core_report_marks_resume_safe_metrics(tmp_path):
    run_context_ablation_v2(tmp_path / "artifacts" / "context-ablation-v2.json", repetitions=1)
    run_memory_ablation_v2(tmp_path / "artifacts" / "memory-ablation-v2.json", repetitions=1)
    run_recovery_ablation_v2(tmp_path / "artifacts" / "recovery-ablation-v2.json", repetitions=1)
    harness_artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"
    harness_artifact_path.write_text(
        '{"record_type":"fixed_benchmark_result","format_version":1,"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"failure_category_counts":{}}',
        encoding="utf-8",
    )

    report_path = tmp_path / "docs" / "metrics" / "pico-benchmark-core-report.md"
    report_text = write_benchmark_core_report(
        report_path=report_path,
        harness_artifact_path=harness_artifact_path,
        context_artifact_path=tmp_path / "artifacts" / "context-ablation-v2.json",
        memory_artifact_path=tmp_path / "artifacts" / "memory-ablation-v2.json",
        recovery_artifact_path=tmp_path / "artifacts" / "recovery-ablation-v2.json",
    )

    assert report_path.exists()
    assert "可以安全写进简历的指标" in report_text
    assert "只适合放文档/面试展开的指标" in report_text
    assert "resume_success_rate" in report_text
    assert "memory_hit_rate" in report_text


@pytest.mark.parametrize(
    ("name", "record_type"),
    [
        ("harness", "fixed_benchmark_result"),
        ("context", "context_ablation_result"),
        ("memory", "memory_ablation_result"),
        ("recovery", "recovery_ablation_result"),
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
        "recovery": {
            "record_type": "recovery_ablation_result",
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
            recovery_artifact_path=paths["recovery"],
        )


def test_provider_selection_normalizes_default_all_and_single_provider():
    from pico.evaluation.provider_benchmark import _normalize_provider_selection

    assert _normalize_provider_selection(None) == ("gpt", "claude", "deepseek")
    assert _normalize_provider_selection("all") == ("gpt", "claude", "deepseek")
    assert _normalize_provider_selection("deepseek") == ("deepseek",)
    assert _normalize_provider_selection(["gpt", "deepseek"]) == ("gpt", "deepseek")


def test_provider_selection_rejects_unknown_provider():
    from pico.evaluation.provider_benchmark import _normalize_provider_selection

    with pytest.raises(ValueError, match="unknown provider"):
        _normalize_provider_selection("openai")
    with pytest.raises(ValueError, match="unknown provider"):
        _normalize_provider_selection(["all", "openai"])


def test_run_provider_experiments_targets_selected_provider(tmp_path, monkeypatch):
    from pico.evaluation.provider_benchmark import run_provider_experiments

    seen = []

    def fake_provider_profile(provider):
        seen.append(provider)
        return {
            "provider": provider,
            "status": "blocked",
            "reason": f"{provider} key missing",
        }

    monkeypatch.setattr(
        "pico.evaluation.provider_benchmark._provider_profile",
        fake_provider_profile,
    )

    payload = run_provider_experiments(
        benchmark_path=tmp_path / "benchmarks.json",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
        providers="deepseek",
    )

    assert seen == ["deepseek"]
    assert payload == {
        "record_type": "provider_experiment_result",
        "format_version": 1,
        "providers": [
            {
                "provider": "deepseek",
                "status": "blocked",
                "reason": "deepseek key missing",
            }
        ]
    }


def test_run_provider_experiments_default_keeps_three_provider_order(tmp_path, monkeypatch):
    from pico.evaluation.provider_benchmark import run_provider_experiments

    seen = []

    def fake_provider_profile(provider):
        seen.append(provider)
        return {
            "provider": provider,
            "status": "blocked",
            "reason": f"{provider} key missing",
        }

    monkeypatch.setattr(
        "pico.evaluation.provider_benchmark._provider_profile",
        fake_provider_profile,
    )

    payload = run_provider_experiments(
        benchmark_path=tmp_path / "benchmarks.json",
        workspace_root=tmp_path / "workspaces",
        artifact_root=tmp_path / "artifacts",
    )

    assert seen == ["gpt", "claude", "deepseek"]
    assert payload["record_type"] == "provider_experiment_result"
    assert payload["format_version"] == 1
    assert [row["provider"] for row in payload["providers"]] == [
        "gpt",
        "claude",
        "deepseek",
    ]


def test_provider_script_writes_current_provider_family(tmp_path, monkeypatch):
    from scripts import run_provider_experiments as script

    payload = {
        "record_type": "provider_experiment_result",
        "format_version": 1,
        "providers": [],
    }
    monkeypatch.setattr(script, "run_provider_experiments", lambda **kwargs: payload)
    output = tmp_path / "providers.json"

    assert script.main(["--output-json", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_large_scale_script_versions_independent_family_outputs(
    tmp_path, monkeypatch
):
    from scripts import run_large_scale_experiments as script

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
    monkeypatch.setattr(script, "collect_resume_metrics", lambda *args, **kwargs: metrics)
    monkeypatch.setattr(script, "render_resume_metrics_markdown", lambda payload: "resume")
    monkeypatch.setattr(script, "render_large_scale_experiment_report", lambda payload: "report")
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("provider", "resume", "memory", "context", "security")
    }

    assert script.main(
        [
            "--benchmark-artifact", str(tmp_path / "fixed.json"),
            "--runs-root", str(tmp_path / "runs"),
            "--provider-output-json", str(paths["provider"]),
            "--resume-output-json", str(paths["resume"]),
            "--resume-output-markdown", str(tmp_path / "resume.md"),
            "--memory-output-json", str(paths["memory"]),
            "--context-output-json", str(paths["context"]),
            "--security-output-json", str(paths["security"]),
            "--final-report-markdown", str(tmp_path / "report.md"),
        ]
    ) == 0
    assert json.loads(paths["provider"].read_text(encoding="utf-8")) == provider_payload
    memory = json.loads(paths["memory"].read_text(encoding="utf-8"))
    context = json.loads(paths["context"].read_text(encoding="utf-8"))
    assert (memory["record_type"], memory["format_version"]) == (
        "memory_ablation_result", 1
    )
    assert (context["record_type"], context["format_version"]) == (
        "context_ablation_result", 1
    )
