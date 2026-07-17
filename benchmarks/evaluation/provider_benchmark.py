"""Run one provider benchmark from the target repository's ``.env``."""

import os
from pathlib import Path

from pony.config.environment import read_project_env
from pony.config.model import resolve_model_config
from pony.providers.factory import build_transport_client

from .fixed_benchmark import FIXED_BENCHMARK_RESULT_FORMAT_VERSION, run_fixed_benchmark
from .metrics_common import _safe_mean, _safe_ratio, _validate_record_header


DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS = 16_384
PROVIDER_EXPERIMENT_FORMAT_VERSION = 1


def _resolve_benchmark_target(
    repo_root,
    *,
    project_env=None,
    process_env=None,
):
    """Resolve the single benchmark target through Pony's product config path."""
    root = Path(repo_root)
    project_values = (
        read_project_env(root) if project_env is None else dict(project_env)
    )
    resolved = resolve_model_config(
        project_env=project_values,
        process_env=dict(os.environ if process_env is None else process_env),
        required=True,
    )
    return {
        "provider": resolved["provider"]["value"],
        "transport": resolved["protocol"]["value"],
        "variant": resolved["api_variant"]["value"],
        "model": resolved["model"]["value"],
        "base_url": resolved["base_url"]["value"],
        "api_key": resolved["api_key"]["value"],
        "auth_mode": resolved["auth_mode"]["value"],
        "capabilities": dict(resolved["capabilities"]),
    }


def _client_from_target(target, *, timeout):
    return build_transport_client(
        target["transport"],
        model=target["model"],
        base_url=target["base_url"],
        api_key=target["api_key"],
        timeout=timeout,
        auth_mode=target["auth_mode"],
        capabilities=target["capabilities"],
    )


def _make_provider_client(repo_root=None, *, timeout=60, process_env=None):
    target = _resolve_benchmark_target(
        Path.cwd() if repo_root is None else repo_root,
        process_env=process_env,
    )
    return _client_from_target(target, timeout=timeout)


def _provider_summary_from_artifact(payload):
    _validate_record_header(
        payload,
        "fixed_benchmark_result",
        FIXED_BENCHMARK_RESULT_FORMAT_VERSION,
    )
    rows = list(payload.get("rows", []))
    cached_tokens = []
    cache_hits = []
    tool_steps = []
    attempts = []
    for row in rows:
        report = row.get("report", {})
        totals = report.get("model", {}).get("usage", {})
        cached_tokens.append(int(totals.get("cached_tokens", 0) or 0))
        cache_hits.append(bool(totals.get("cache_hit")))
        tool_steps.append(int(row.get("tool_steps", 0)))
        attempts.append(int(row.get("attempts", 0)))
    summary = payload.get("summary", {})
    return {
        "status": "completed",
        "task_count": int(summary.get("total_tasks", len(rows))),
        "pass_rate": float(summary.get("pass_rate", 0.0)),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "cache_hit_rate": _safe_ratio(sum(cache_hits), len(cache_hits)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "artifact_path": payload.get("_artifact_path", ""),
    }


def _normalize_text(value):
    text = str(value).strip().lower()
    while text.endswith((".", "!", "?", '"', "'")):
        text = text[:-1].strip()
    return text


def run_provider_experiments(
    benchmark_path,
    workspace_root,
    artifact_root,
    *,
    repo_root=None,
    max_output_tokens=DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS,
):
    """Benchmark only the Provider selected by ``repo_root/.env``."""
    benchmark_path = Path(benchmark_path)
    workspace_root = Path(workspace_root)
    artifact_root = Path(artifact_root)
    root = Path.cwd() if repo_root is None else Path(repo_root)
    target = _resolve_benchmark_target(root)
    provider_name = target["provider"]

    def factory(task, workspace):
        del task, workspace
        return _client_from_target(target, timeout=300)

    artifact_path = artifact_root / f"{provider_name}-benchmark.json"
    try:
        payload = run_fixed_benchmark(
            benchmark_path=benchmark_path,
            artifact_path=artifact_path,
            workspace_root=workspace_root / provider_name,
            model_name=provider_name,
            model_version=target["model"],
            max_output_tokens=max_output_tokens,
            model_client_factory=factory,
        )
        payload["_artifact_path"] = str(artifact_path)
        result = _provider_summary_from_artifact(payload)
        result.update(
            provider=provider_name,
            variant=target["variant"],
            model=target["model"],
        )
    except Exception as exc:
        result = {
            "provider": provider_name,
            "variant": target["variant"],
            "status": "error",
            "model": target["model"],
            "reason": str(exc),
        }
    return {
        "record_type": "provider_experiment_result",
        "format_version": PROVIDER_EXPERIMENT_FORMAT_VERSION,
        "providers": [result],
    }
