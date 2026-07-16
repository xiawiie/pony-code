import os
from pathlib import Path

from pico.config import (
    API_KEY_ENV_NAME,
    API_URL_ENV_NAME,
    read_project_env,
)
from pico.providers.factory import build_model_client
from .fixed_benchmark import FIXED_BENCHMARK_RESULT_FORMAT_VERSION, run_fixed_benchmark
from .metrics_common import _safe_mean, _safe_ratio, _validate_record_header

DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS = 16_384
PROVIDER_EXPERIMENT_FORMAT_VERSION = 1
PROVIDER_BENCHMARK_CHOICES = ("gpt", "claude", "deepseek")


_TARGETS = {
    "gpt": {
        "client_kind": "openai_responses",
        "model": "gpt-5.4",
        "model_env": "PICO_OPENAI_MODEL",
        "base_url": "https://api.openai.com/v1",
        "base_url_env": "PICO_OPENAI_API_BASE",
        "api_key_env": "PICO_OPENAI_API_KEY",
        "auth_mode": "bearer",
        "capabilities": {
            "strict_tools": True,
            "parallel_tool_control": True,
            "reasoning_replay": True,
        },
    },
    "claude": {
        "client_kind": "anthropic_messages",
        "model": "claude-sonnet-4-6",
        "model_env": "PICO_ANTHROPIC_MODEL",
        "base_url": "https://api.anthropic.com/v1",
        "base_url_env": "PICO_ANTHROPIC_API_BASE",
        "api_key_env": "PICO_ANTHROPIC_API_KEY",
        "auth_mode": "x-api-key",
        "capabilities": {
            "prompt_cache": True,
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    },
    "deepseek": {
        "client_kind": "anthropic_messages",
        "model": "deepseek-v4-flash",
        "model_env": "",
        "base_url": "https://api.deepseek.com/anthropic/v1",
        "base_url_env": API_URL_ENV_NAME,
        "api_key_env": API_KEY_ENV_NAME,
        "auth_mode": "x-api-key",
        "capabilities": {"thinking_disabled": True},
    },
}


def _normalize_provider_selection(providers=None):
    if providers is None:
        return PROVIDER_BENCHMARK_CHOICES
    requested = [providers] if isinstance(providers, str) else list(providers)
    normalized = []
    saw_all = False
    for provider in requested:
        name = str(provider).strip().lower()
        if not name:
            continue
        if name == "all":
            saw_all = True
            continue
        if name not in PROVIDER_BENCHMARK_CHOICES:
            choices = ", ".join(("all", *PROVIDER_BENCHMARK_CHOICES))
            raise ValueError(f"unknown provider: {name}. expected one of: {choices}")
        if name not in normalized:
            normalized.append(name)
    if saw_all or not normalized:
        return PROVIDER_BENCHMARK_CHOICES
    return tuple(normalized)


def _configured_value(name, project_env, process_env, default=""):
    if name:
        return project_env.get(name) or process_env.get(name) or default
    return default


def _provider_target(provider, *, project_env=None, process_env=None):
    if provider not in _TARGETS:
        raise ValueError(f"unknown provider: {provider}")
    project_env = (
        read_project_env(Path.cwd())
        if project_env is None
        else dict(project_env)
    )
    process_env = dict(os.environ if process_env is None else process_env)
    target = dict(_TARGETS[provider])
    target["model"] = _configured_value(
        target["model_env"], project_env, process_env, target["model"]
    )
    target["base_url"] = _configured_value(
        target["base_url_env"], project_env, process_env, target["base_url"]
    ).rstrip("/")
    target["api_key"] = _configured_value(
        target["api_key_env"], project_env, process_env
    )
    target["provider"] = provider
    target["capabilities"] = dict(target["capabilities"])
    target["status"] = "ready" if target["api_key"] else "blocked"
    if target["status"] == "blocked":
        target["reason"] = f"{target['api_key_env']} missing"
    return target


def _client_from_target(target, *, timeout):
    return build_model_client(
        target["client_kind"],
        model=target["model"],
        base_url=target["base_url"],
        api_key=target["api_key"],
        timeout=timeout,
        auth_mode=target["auth_mode"],
        capabilities=target["capabilities"],
        compatibility=target.get("compatibility", "standard"),
    )


def _make_provider_client(provider):
    target = _provider_target(provider)
    if target["status"] != "ready":
        raise RuntimeError(target["reason"])
    return _client_from_target(target, timeout=60)


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
    max_output_tokens=DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS,
    providers=None,
):
    benchmark_path = Path(benchmark_path)
    workspace_root = Path(workspace_root)
    artifact_root = Path(artifact_root)
    rows = []
    for provider_name in _normalize_provider_selection(providers):
        target = _provider_target(provider_name)
        if target["status"] != "ready":
            rows.append(target)
            continue

        def factory(task, workspace, target=target):
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
            result.update(provider=provider_name, model=target["model"])
            rows.append(result)
        except Exception as exc:
            rows.append(
                {
                    "provider": provider_name,
                    "status": "error",
                    "model": target["model"],
                    "reason": str(exc),
                }
            )
    return {
        "record_type": "provider_experiment_result",
        "format_version": PROVIDER_EXPERIMENT_FORMAT_VERSION,
        "providers": rows,
    }
