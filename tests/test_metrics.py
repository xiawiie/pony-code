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
    assert artifact["artifact_type"] == "memory-ablation-v2"
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
    assert artifact["artifact_type"] == "recovery-ablation-v2"
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
