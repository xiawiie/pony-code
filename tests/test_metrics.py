import json
import os
from unittest.mock import patch

import pytest

from pico.evaluation.metrics import (
    _provider_profile,
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
)


def test_metrics_module_splits_report_and_experiment_entrypoints():
    from pico.evaluation.metrics import run_context_ablation_v2 as compat_context_ablation
    from pico.evaluation.metrics import write_benchmark_core_report as compat_core_report
    from pico.evaluation.metrics_experiments import run_context_ablation_v2 as experiment_context_ablation
    from pico.evaluation.metrics_reports import write_benchmark_core_report as report_core_report

    assert compat_context_ablation is experiment_context_ablation
    assert compat_core_report is report_core_report


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


def test_provider_summary_reads_cache_from_completion_usage_totals():
    from pico.evaluation.provider_benchmark import _provider_summary_from_artifact

    summary = _provider_summary_from_artifact(
        {
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


def test_run_context_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-ablation-v2.json"

    artifact = run_context_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "context-ablation-v2"
    assert artifact["config_count"] == 12
    assert len(artifact["configs"]) == 12
    assert "current_request_preserved_rate" in artifact["summary"]


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


def test_provider_profile_uses_right_codes_shared_key_for_gpt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with patch.dict(os.environ, {"PICO_RIGHT_CODES_API_KEY": "sk-right-codes"}, clear=True):
        profile = _provider_profile("gpt")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-right-codes"
    assert profile["model"] == "gpt-5.4"


@pytest.mark.legacy_string_path
@pytest.mark.skip(reason="synthetic memory experiment reads the legacy flattened prompt for file-summary line; needs v2 message inspection")
def test_run_memory_ablation_v2_writes_expected_artifact(tmp_path):
    # TODO(P3 cleanup): _MemoryExperimentModelClient.complete() scans the
    # flattened prompt for "<file> -> <fact>" markers produced by
    # ContextManager.build() history compression. Once memory summaries are
    # threaded through v2 messages / system prefix, this experiment should
    # inspect session["messages"] directly.
    artifact_path = tmp_path / "artifacts" / "memory-ablation-v2.json"

    artifact = run_memory_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "memory-ablation-v2"
    assert artifact["task_count"] == 12
    assert set(artifact["variants"]) == {"memory_on", "memory_off", "memory_irrelevant"}
    assert "memory_hit_rate" in artifact["variants"]["memory_on"]
    assert artifact["variants"]["memory_on"]["repeated_reads"] == 0
    assert artifact["variants"]["memory_on"]["memory_hit_rate"] == 1.0
    assert artifact["variants"]["memory_off"]["repeated_reads"] > artifact["variants"]["memory_on"]["repeated_reads"]
    assert artifact["variants"]["memory_irrelevant"]["repeated_reads"] > artifact["variants"]["memory_on"]["repeated_reads"]
    assert artifact["variants"]["memory_off"]["memory_hit_rate"] < artifact["variants"]["memory_on"]["memory_hit_rate"]
    assert artifact["variants"]["memory_irrelevant"]["memory_hit_rate"] < artifact["variants"]["memory_on"]["memory_hit_rate"]


def test_run_recovery_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "recovery-ablation-v2.json"

    artifact = run_recovery_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "recovery-ablation-v2"
    assert artifact["task_count"] == 10
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
        '{"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"failure_category_counts":{}}',
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
    assert [row["provider"] for row in payload["providers"]] == [
        "gpt",
        "claude",
        "deepseek",
    ]
