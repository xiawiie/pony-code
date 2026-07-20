import json
from pathlib import Path

from pony.agent.observability import RunArtifactError, load_run_artifacts
from .metrics_common import (
    CONTEXT_ABLATION_FORMAT_VERSION,
    DEFAULT_CORE_REPORT_PATH,
    DEFAULT_CONTEXT_ABLATION_V2_PATH,
    DEFAULT_HARNESS_REGRESSION_V2_PATH,
    DEFAULT_MEMORY_ABLATION_V2_PATH,
    MEMORY_ABLATION_FORMAT_VERSION,
    _load_json_artifact,
    _parse_iso8601,
    _safe_mean,
    _safe_ratio,
)
from .fixed_benchmark import FIXED_BENCHMARK_RESULT_FORMAT_VERSION
from .provider_benchmark import PROVIDER_EXPERIMENT_FORMAT_VERSION
from .experiments_real import (
    run_real_context_experiment,
    run_real_memory_experiment,
    run_real_security_experiment_suite,
)
from .experiments_synthetic import (
    build_stress_agent_metrics,
    run_context_stress_matrix,
    run_large_scale_memory_experiment,
    run_memory_dependency_experiment,
    run_security_experiment_suite,
)


def aggregate_benchmark_artifact(path):
    payload = _load_json_artifact(
        path,
        "fixed_benchmark_result",
        FIXED_BENCHMARK_RESULT_FORMAT_VERSION,
    )
    rows = list(payload.get("rows", []))
    summary = dict(payload.get("summary", {}))
    task_count = int(summary.get("total_tasks", len(rows) or 0))
    tool_steps = [int(row.get("tool_steps", 0)) for row in rows]
    attempts = [int(row.get("attempts", 0)) for row in rows]
    categories = {}
    for row in rows:
        category = str(row.get("category", "")).strip()
        if not category:
            continue
        categories[category] = categories.get(category, 0) + 1
    return {
        "task_count": task_count,
        "passed": int(summary.get("passed", 0)),
        "failed": int(summary.get("failed", 0)),
        "pass_rate": float(summary.get("pass_rate", 0.0)),
        "within_budget": int(summary.get("within_budget", 0)),
        "verifier_passes": int(summary.get("verifier_passes", 0)),
        "failure_category_counts": dict(summary.get("failure_category_counts", {})),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "category_counts": categories,
        "rows": rows,
    }


def _infer_run_duration_ms(events):
    finished = next(
        (event for event in reversed(events) if event.get("event") == "run_finished"),
        None,
    )
    if finished and finished.get("run_duration_ms") is not None:
        return float(finished["run_duration_ms"])
    started = next(
        (event for event in events if event.get("event") == "run_started"), None
    )
    if not started or not finished:
        return 0.0
    start_dt = _parse_iso8601(started.get("created_at"))
    end_dt = _parse_iso8601(finished.get("created_at"))
    if start_dt is None or end_dt is None:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds() * 1000.0)


def aggregate_run_artifacts(runs_root):
    runs_root = Path(runs_root)
    run_dirs = sorted(path for path in runs_root.glob("*") if path.is_dir())
    reports = []
    tool_status_counts = {}
    tool_name_counts = {}
    security_event_counts = {}
    run_durations = []
    tool_durations = []
    prompt_durations = []
    stop_reasons = {}

    for run_dir in run_dirs:
        try:
            report, events = load_run_artifacts(runs_root, run_dir.name)
        except RunArtifactError:
            continue
        reports.append(report)
        run_durations.append(_infer_run_duration_ms(events))
        for event in events:
            if (
                event.get("event") == "prompt_built"
                and event.get("duration_ms") is not None
            ):
                prompt_durations.append(float(event["duration_ms"]))
            if event.get("event") != "tool_executed":
                continue
            tool_name = str(event.get("name", "")).strip()
            if tool_name:
                tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1
            tool_status = str(event.get("tool_status", "")).strip()
            if tool_status:
                tool_status_counts[tool_status] = (
                    tool_status_counts.get(tool_status, 0) + 1
                )
            security_event = str(event.get("security_event_type", "")).strip()
            if security_event:
                security_event_counts[security_event] = (
                    security_event_counts.get(security_event, 0) + 1
                )
            if event.get("duration_ms") is not None:
                tool_durations.append(float(event["duration_ms"]))

    tool_steps = [int(report.get("tools", {}).get("calls", 0)) for report in reports]
    attempts = [int(report.get("model", {}).get("attempts", 0)) for report in reports]
    request_messages_chars = [
        int((report.get("context") or {}).get("messages_chars", 0))
        for report in reports
    ]
    cached_tokens = [
        int((report.get("model", {}).get("usage") or {}).get("cached_tokens", 0) or 0)
        for report in reports
    ]
    cache_hits = [
        bool((report.get("model", {}).get("usage") or {}).get("cache_hit"))
        for report in reports
    ]
    input_tokens = [
        int((report.get("model", {}).get("usage") or {}).get("input_tokens", 0) or 0)
        for report in reports
    ]
    prefix_reused = [
        not bool((report.get("context") or {}).get("prefix_changed"))
        for report in reports
        if "prefix_changed" in (report.get("context") or {})
    ]
    for report in reports:
        stop_reason = str(report.get("run", {}).get("stop_reason", "")).strip()
        if stop_reason:
            stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1

    return {
        "run_count": len(reports),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "avg_request_messages_chars": _safe_mean(request_messages_chars),
        "cache_hit_rate": _safe_ratio(
            sum(1 for hit in cache_hits if hit), len(cache_hits)
        ),
        "cached_token_ratio": _safe_ratio(sum(cached_tokens), sum(input_tokens)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "prefix_reuse_rate": _safe_ratio(
            sum(1 for reused in prefix_reused if reused), len(prefix_reused)
        ),
        "tool_status_counts": tool_status_counts,
        "tool_name_counts": tool_name_counts,
        "security_event_counts": security_event_counts,
        "stop_reason_counts": stop_reasons,
        "avg_run_duration_ms": _safe_mean(run_durations),
        "avg_tool_duration_ms": _safe_mean(tool_durations),
        "avg_prompt_build_duration_ms": _safe_mean(prompt_durations),
    }


def collect_resume_metrics(
    benchmark_artifact_path,
    runs_root,
    provider_experiments=None,
    memory_repetitions=3,
    large_memory_repetitions=5,
    context_repetitions=5,
    security_repetitions=3,
    experiment_mode="synthetic",
    repo_root=None,
):
    benchmark = aggregate_benchmark_artifact(benchmark_artifact_path)
    runs = aggregate_run_artifacts(runs_root)
    experiment_mode = str(experiment_mode)
    real_provider = ""
    if experiment_mode == "real":
        memory_large = run_real_memory_experiment(
            repo_root=repo_root,
            repetitions=large_memory_repetitions,
        )
        real_provider = memory_large["provider"]
        memory = {
            name: dict(values) for name, values in memory_large["variants"].items()
        }
        context = run_real_context_experiment(
            repo_root=repo_root,
            repetitions=context_repetitions,
        )
        security = run_real_security_experiment_suite(
            repo_root=repo_root,
            repetitions=security_repetitions,
        )
        stress = {
            "compacted_request_chars": int(
                round(context["summary"]["avg_compacted_request_chars"])
            ),
            "uncompacted_request_chars": int(
                round(context["summary"]["avg_uncompacted_request_chars"])
            ),
        }
    else:
        stress = build_stress_agent_metrics()
        memory = run_memory_dependency_experiment(repetitions=memory_repetitions)
        memory_large = run_large_scale_memory_experiment(
            repetitions=large_memory_repetitions
        )
        context = run_context_stress_matrix(repetitions=context_repetitions)
        security = run_security_experiment_suite(repetitions=security_repetitions)
    provider_payload = {"providers": []}
    if provider_experiments:
        provider_payload = _load_json_artifact(
            provider_experiments,
            "provider_experiment_result",
            PROVIDER_EXPERIMENT_FORMAT_VERSION,
        )
    return {
        "experiment_mode": experiment_mode,
        "real_provider": real_provider if experiment_mode == "real" else "",
        "facts": {
            "model_backend_count": 3,
            "tool_count": 7,
            "run_artifact_count": 3,
        },
        "benchmark": benchmark,
        "runs": runs,
        "stress_ablation": stress,
        "memory_experiment": memory,
        "memory_large_experiment": memory_large,
        "context_experiment": context,
        "security_experiment": security,
        "provider_experiments": provider_payload,
        "resume_highlights": [
            f"Built a fixed benchmark harness with {benchmark['task_count']} tasks and automated pass/fail, verifier, and budget summaries.",
            f"Recorded 3 run artifacts per execution and structured runtime metadata across {runs['run_count']} aggregated runs.",
            f"Observed prompt-cache telemetry with average cached tokens of {runs['avg_cached_tokens']:.1f} and cache-hit rate of {runs['cache_hit_rate']:.2%} when available.",
            (
                f"In a real-model long-context experiment ({real_provider}), compaction shrank sent-message views from "
                f"{stress['uncompacted_request_chars']} to {stress['compacted_request_chars']} chars without deleting canonical history."
                if experiment_mode == "real"
                else f"In a synthetic long-context stress scenario, compaction shrank sent-message views from {stress['uncompacted_request_chars']} to {stress['compacted_request_chars']} chars without deleting canonical history."
            ),
            f"In the memory dependency experiment, repeated follow-up reads dropped from {memory['memory_off']['repeated_reads']} to {memory['memory_on']['repeated_reads']}.",
            f"In the large-scale memory experiment, repeated reads dropped from {memory_large['variants']['memory_off']['repeated_reads']} to {memory_large['variants']['memory_on']['repeated_reads']} across {memory_large['task_count']} tasks.",
        ],
    }


def render_resume_metrics_markdown(metrics):
    benchmark = metrics["benchmark"]
    runs = metrics["runs"]
    stress = metrics["stress_ablation"]
    memory = metrics["memory_experiment"]
    memory_large = metrics["memory_large_experiment"]
    context = metrics["context_experiment"]
    security = metrics["security_experiment"]
    provider_payload = metrics.get("provider_experiments", {})
    lines = [
        "# Pony Resume Metrics",
        "",
        "## Key Numbers",
        f"- Experiment mode: {metrics.get('experiment_mode', 'synthetic')}",
        f"- Model backends: {metrics['facts']['model_backend_count']}",
        f"- Tool types: {metrics['facts']['tool_count']}",
        f"- Fixed benchmark tasks: {benchmark['task_count']}",
        f"- Fixed benchmark pass rate: {benchmark['pass_rate']:.2%}",
        f"- Aggregated runs: {runs['run_count']}",
        f"- Average tool steps per run: {runs['avg_tool_steps']:.2f}",
        f"- Average attempts per run: {runs['avg_attempts']:.2f}",
        f"- Average sent request message chars: {runs['avg_request_messages_chars']:.2f}",
        f"- Cache hit rate: {runs['cache_hit_rate']:.2%}",
        (
            f"- Real-model sent-message chars (compacted vs uncompacted): {stress['compacted_request_chars']} / {stress['uncompacted_request_chars']}"
            if metrics.get("experiment_mode") == "real"
            else f"- Synthetic sent-message chars (compacted vs uncompacted): {stress['compacted_request_chars']} / {stress['uncompacted_request_chars']}"
        ),
        f"- Memory repeated reads (on vs off): {memory['memory_on']['repeated_reads']} / {memory['memory_off']['repeated_reads']}",
        f"- Large-scale memory tasks: {memory_large['task_count']}",
        f"- Context matrix configs: {context['config_count']}",
        f"- Security scenarios: {security['scenario_count']}",
        "",
        "## Resume Highlights",
    ]
    lines.extend(f"- {line}" for line in metrics["resume_highlights"])
    providers = provider_payload.get("providers", [])
    if providers:
        lines.extend(["", "## Provider Experiments"])
        for provider in providers:
            if provider.get("status") == "completed":
                lines.append(
                    f"- {provider['provider']}: pass_rate={provider['pass_rate']:.2%}, avg_attempts={provider['avg_attempts']:.2f}, avg_tool_steps={provider['avg_tool_steps']:.2f}, cache_hit_rate={provider['cache_hit_rate']:.2%}"
                )
            else:
                lines.append(
                    f"- {provider['provider']}: {provider['status']} ({provider.get('reason', 'unknown')})"
                )
    lines.append("")
    return "\n".join(lines)


def render_large_scale_experiment_report(metrics):
    benchmark = metrics["benchmark"]
    memory_small = metrics["memory_experiment"]
    memory_large = metrics["memory_large_experiment"]
    context = metrics["context_experiment"]
    security = metrics["security_experiment"]
    providers = metrics.get("provider_experiments", {}).get("providers", [])
    report_provider = (
        metrics.get("real_provider")
        or context.get("provider")
        or memory_large.get("provider")
        or security.get("provider")
        or "unknown"
    )
    lines = [
        "# Pony Large-Scale Experiment Report",
        "",
        "## Executive Summary",
        (
            f"- Experiment mode: real-model (provider: {report_provider})"
            if metrics.get("experiment_mode") == "real"
            else f"- Experiment mode: {metrics.get('experiment_mode', 'synthetic')}"
        ),
        f"- Fixed benchmark tasks: {benchmark['task_count']}",
        f"- Large-scale memory tasks: {memory_large['task_count']}",
        f"- Context stress configurations: {context['config_count']}",
        f"- Security scenarios: {security['scenario_count']}",
        "",
        "## Context Governance",
        (
            f"- Real-model sent-message chars ({report_provider}, compacted vs uncompacted): {metrics['stress_ablation']['compacted_request_chars']} vs {metrics['stress_ablation']['uncompacted_request_chars']}"
            if metrics.get("experiment_mode") == "real"
            else f"- Synthetic sent-message chars (compacted vs uncompacted): {metrics['stress_ablation']['compacted_request_chars']} vs {metrics['stress_ablation']['uncompacted_request_chars']}"
        ),
        f"- Average request compression ratio across context matrix: {context['summary']['avg_request_compression_ratio']:.2%}",
        f"- Max request compression ratio across context matrix: {context['summary']['max_request_compression_ratio']:.2%}",
        "",
        "## Memory Experiments",
        f"- Small memory experiment repeated reads: {memory_small['memory_on']['repeated_reads']} vs {memory_small['memory_off']['repeated_reads']}",
        f"- Large memory experiment repeated reads: {memory_large['variants']['memory_on']['repeated_reads']} vs {memory_large['variants']['memory_off']['repeated_reads']}",
        f"- Large memory experiment avg tool steps: {memory_large['variants']['memory_on']['avg_tool_steps']:.2f} vs {memory_large['variants']['memory_off']['avg_tool_steps']:.2f}",
        "",
        "## Security Experiments",
        f"- Security event counts: {json.dumps(security['security_event_counts'], sort_keys=True)}",
        f"- Tool error code counts: {json.dumps(security['tool_error_code_counts'], sort_keys=True)}",
        "",
        "## Provider Experiments",
    ]
    if providers:
        for provider in providers:
            if provider.get("status") == "completed":
                lines.append(
                    f"- {provider['provider']}: pass_rate={provider['pass_rate']:.2%}, avg_attempts={provider['avg_attempts']:.2f}, avg_tool_steps={provider['avg_tool_steps']:.2f}, cache_hit_rate={provider['cache_hit_rate']:.2%}"
                )
            else:
                lines.append(
                    f"- {provider['provider']}: {provider['status']} ({provider.get('reason', 'unknown')})"
                )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Resume-Safe Claims",
            f"- Long-context stress scenario: sent-message chars reduced from {metrics['stress_ablation']['uncompacted_request_chars']} to {metrics['stress_ablation']['compacted_request_chars']} while canonical history stayed intact.",
            f"- Large-scale memory experiment: repeated reads reduced from {memory_large['variants']['memory_off']['repeated_reads']} to {memory_large['variants']['memory_on']['repeated_reads']}.",
            f"- Platform facts: {benchmark['task_count']} benchmark tasks, {metrics['facts']['tool_count']} tool types, {metrics['facts']['run_artifact_count']} run artifacts.",
            "",
        ]
    )
    return "\n".join(lines)


def write_benchmark_core_report(
    report_path=DEFAULT_CORE_REPORT_PATH,
    harness_artifact_path=DEFAULT_HARNESS_REGRESSION_V2_PATH,
    context_artifact_path=DEFAULT_CONTEXT_ABLATION_V2_PATH,
    memory_artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH,
):
    harness = _load_json_artifact(
        harness_artifact_path,
        "fixed_benchmark_result",
        FIXED_BENCHMARK_RESULT_FORMAT_VERSION,
    )
    context = _load_json_artifact(
        context_artifact_path,
        "context_ablation_result",
        CONTEXT_ABLATION_FORMAT_VERSION,
    )
    memory = _load_json_artifact(
        memory_artifact_path,
        "memory_ablation_result",
        MEMORY_ABLATION_FORMAT_VERSION,
    )

    harness_summary = harness.get("summary", {})
    context_summary = context.get("summary", {})
    memory_variants = memory.get("variants", {})
    memory_on = memory_variants.get("memory_on", {})
    memory_off = memory_variants.get("memory_off", {})
    lines = [
        "# Pony Benchmark Core Report",
        "",
        "这轮 benchmark 只收缩到 Harness regression、context ablation 和 working memory ablation 三层，不把 provider 或 durable memory 的别的结论揉进来。",
        "",
        "## Harness Regression",
        f"- 固定 regression 任务数：{harness_summary.get('total_tasks', 0)}",
        f"- pass_rate：{harness_summary.get('pass_rate', 0.0):.2%}",
        f"- within_budget_rate：{harness_summary.get('within_budget_rate', 0.0):.2%}",
        f"- verifier_pass_rate：{harness_summary.get('verifier_pass_rate', 0.0):.2%}",
        "",
        "## Context Ablation",
        f"- 配置数：{context.get('config_count', 0)}",
        f"- avg_compacted_request_chars：{context_summary.get('avg_compacted_request_chars', 0.0):.2f}",
        f"- avg_uncompacted_request_chars：{context_summary.get('avg_uncompacted_request_chars', 0.0):.2f}",
        f"- avg_request_compression_ratio：{context_summary.get('avg_request_compression_ratio', 0.0):.2%}",
        f"- max_request_compression_ratio：{context_summary.get('max_request_compression_ratio', 0.0):.2%}",
        f"- current_request_preserved_rate：{context_summary.get('current_request_preserved_rate', 0.0):.2%}",
        f"- canonical_history_preserved_rate：{context_summary.get('canonical_history_preserved_rate', 0.0):.2%}",
        "",
        "## Working Memory Ablation",
        f"- memory_on repeated_reads：{memory_on.get('repeated_reads', 0)}",
        f"- memory_off repeated_reads：{memory_off.get('repeated_reads', 0)}",
        f"- memory_on avg_tool_steps：{memory_on.get('avg_tool_steps', 0.0):.2f}",
        f"- memory_on correct_rate：{memory_on.get('correct_rate', 0.0):.2%}",
        f"- memory_hit_rate：{memory_on.get('memory_hit_rate', 0.0):.2%}",
        "",
        "## 可以安全写进简历的指标",
        "- avg_compacted_request_chars",
        "- avg_uncompacted_request_chars",
        "- avg_request_compression_ratio",
        "- max_request_compression_ratio",
        "- repeated_reads",
        "- avg_tool_steps",
        "- correct_rate",
        "",
        "## 只适合放文档/面试展开的指标",
        "- current_request_preserved_rate",
        "- memory_hit_rate",
        "- failure_category_counts",
        "",
        "## 口径边界",
        "- Harness regression 只证明 runtime 合同稳定，不证明 provider 上限。",
        "- Context 与 memory 只证明模块收益，不和 provider benchmark 混写。",
    ]
    report_text = "\n".join(lines) + "\n"
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return report_text
