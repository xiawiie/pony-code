import os
from pathlib import Path

from ..config import read_project_env, resolve_provider_config
from ..providers.anthropic_compatible import AnthropicCompatibleModelClient
from ..providers.openai_compatible import OpenAICompatibleModelClient
from ..providers.defaults import API_KEY_ENV_NAMES
from ..providers.text_protocol_adapter import TextProtocolAdapter
from .fixed_benchmark import run_fixed_benchmark
from .fixed_benchmark import FIXED_BENCHMARK_RESULT_FORMAT_VERSION
from .metrics_common import _safe_mean, _safe_ratio, _validate_record_header

DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS = 2048
PROVIDER_EXPERIMENT_FORMAT_VERSION = 1

PROVIDER_BENCHMARK_CHOICES = ("gpt", "claude", "deepseek")


def _normalize_provider_selection(providers=None):
    if providers is None:
        return PROVIDER_BENCHMARK_CHOICES
    if isinstance(providers, str):
        requested = [providers]
    else:
        requested = list(providers)

    saw_all = False
    normalized = []
    for provider in requested:
        provider_name = str(provider).strip().lower()
        if not provider_name:
            continue
        if provider_name == "all":
            saw_all = True
            continue
        if provider_name not in PROVIDER_BENCHMARK_CHOICES:
            choices = ", ".join(("all", *PROVIDER_BENCHMARK_CHOICES))
            raise ValueError(f"unknown provider: {provider_name}. expected one of: {choices}")
        if provider_name not in normalized:
            normalized.append(provider_name)

    if saw_all or not normalized:
        return PROVIDER_BENCHMARK_CHOICES
    return tuple(normalized)


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
        completion_usage_totals = report.get("completion_usage_totals", {})
        cached_tokens.append(int(completion_usage_totals.get("cached_tokens", 0) or 0))
        cache_hits.append(bool(completion_usage_totals.get("cache_hit")))
        tool_steps.append(int(row.get("tool_steps", 0)))
        attempts.append(int(row.get("attempts", 0)))
    summary = payload.get("summary", {})
    return {
        "status": "completed",
        "task_count": int(summary.get("total_tasks", len(rows))),
        "pass_rate": float(summary.get("pass_rate", 0.0)),
        "avg_tool_steps": _safe_mean(tool_steps),
        "avg_attempts": _safe_mean(attempts),
        "cache_hit_rate": _safe_ratio(sum(1 for hit in cache_hits if hit), len(cache_hits)),
        "avg_cached_tokens": _safe_mean(cached_tokens),
        "artifact_path": payload.get("_artifact_path", ""),
    }


def _provider_profile(provider):
    provider_name = {"gpt": "openai", "claude": "anthropic"}.get(provider, provider)
    config = resolve_provider_config(
        explicit={"provider": provider_name},
        project_env=read_project_env(Path.cwd()),
        process_env=dict(os.environ),
    )
    api_key = config["api_key"]["value"]
    if not api_key:
        return {
            "provider": provider,
            "status": "blocked",
            "reason": f"{', '.join(API_KEY_ENV_NAMES[provider_name])} missing",
        }
    return {
        "provider": provider,
        "status": "ready",
        "model": config["model"]["value"],
        "base_url": config["base_url"]["value"],
        "api_key": api_key,
    }


def _make_provider_client(provider):
    profile = _provider_profile(provider)
    if profile["status"] != "ready":
        raise RuntimeError(profile["reason"])
    timeout = 60
    if provider == "gpt":
        return TextProtocolAdapter(OpenAICompatibleModelClient(
            model=profile["model"],
            base_url=profile["base_url"],
            api_key=profile["api_key"],
            temperature=None,
            timeout=timeout,
        ))
    return AnthropicCompatibleModelClient(
        model=profile["model"],
        base_url=profile["base_url"],
        api_key=profile["api_key"],
        temperature=None,
        timeout=timeout,
    )


def _normalize_text(value):
    text = str(value).strip().lower()
    while text.endswith((".", "!", "?", "\"", "'")):
        text = text[:-1].strip()
    return text


def run_provider_experiments(
    benchmark_path,
    workspace_root,
    artifact_root,
    max_new_tokens=DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS,
    providers=None,
):
    benchmark_path = Path(benchmark_path)
    workspace_root = Path(workspace_root)
    artifact_root = Path(artifact_root)
    provider_rows = []
    for provider_name in _normalize_provider_selection(providers):
        profile = _provider_profile(provider_name)
        if profile["status"] != "ready":
            provider_rows.append(profile)
            continue
        if provider_name == "gpt":

            def factory(task, workspace, profile=profile):
                del task, workspace
                return TextProtocolAdapter(OpenAICompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=None,
                    timeout=300,
                ))

        else:

            def factory(task, workspace, profile=profile):
                del task, workspace
                return AnthropicCompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=None,
                    timeout=300,
                )

        artifact_path = artifact_root / f"{provider_name}-benchmark.json"
        try:
            payload = run_fixed_benchmark(
                benchmark_path=benchmark_path,
                artifact_path=artifact_path,
                workspace_root=workspace_root / provider_name,
                model_name=profile["provider"],
                model_version=profile["model"],
                max_new_tokens=max_new_tokens,
                model_client_factory=factory,
            )
            payload["_artifact_path"] = str(artifact_path)
            result = _provider_summary_from_artifact(payload)
            result["provider"] = provider_name
            result["model"] = profile["model"]
            provider_rows.append(result)
        except Exception as exc:
            provider_rows.append(
                {
                    "provider": provider_name,
                    "status": "error",
                    "model": profile["model"],
                    "reason": str(exc),
                }
            )
    return {
        "record_type": "provider_experiment_result",
        "format_version": PROVIDER_EXPERIMENT_FORMAT_VERSION,
        "providers": provider_rows,
    }
