from pathlib import Path

from ..config import load_project_env, provider_env
from ..providers.clients import (
    AnthropicCompatibleModelClient,
    OpenAICompatibleModelClient,
)
from .fixed_benchmark import run_fixed_benchmark
from .metrics_common import _safe_mean, _safe_ratio

DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS = 2048

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
    rows = list(payload.get("rows", []))
    cached_tokens = []
    cache_hits = []
    tool_steps = []
    attempts = []
    for row in rows:
        report = row.get("report", {})
        prompt_metadata = report.get("prompt_metadata", {})
        cached_tokens.append(int(prompt_metadata.get("cached_tokens", 0) or 0))
        cache_hits.append(bool(prompt_metadata.get("cache_hit")))
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
    load_project_env(Path.cwd())
    if provider == "gpt":
        api_key = provider_env(
            "PICO_OPENAI_API_KEY",
            ("OPENAI_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "PICO_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        )
        if not api_key:
            return {"provider": provider, "status": "blocked", "reason": "PICO_OPENAI_API_KEY, OPENAI_API_KEY, or shared right.codes key missing"}
        return {
            "provider": provider,
            "status": "ready",
            "model": provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",), "gpt-5.4"),
            "base_url": provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), "https://api.openai.com/v1"),
            "api_key": api_key,
        }
    if provider == "deepseek":
        api_key = provider_env("PICO_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        if not api_key:
            return {"provider": provider, "status": "blocked", "reason": "PICO_DEEPSEEK_API_KEY or DEEPSEEK_API_KEY missing"}
        return {
            "provider": provider,
            "status": "ready",
            "model": provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",), "deepseek-v4-pro"),
            "base_url": provider_env("PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), "https://api.deepseek.com/anthropic"),
            "api_key": api_key,
        }
    api_key = provider_env(
        "PICO_ANTHROPIC_API_KEY",
        ("ANTHROPIC_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "PICO_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    if not api_key:
        return {"provider": "claude", "status": "blocked", "reason": "PICO_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY missing"}
    return {
        "provider": "claude",
        "status": "ready",
        "model": provider_env("PICO_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",), "claude-sonnet-4-6"),
        "base_url": provider_env("PICO_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), "https://www.right.codes/claude/v1"),
        "api_key": api_key,
    }


def _make_provider_client(provider):
    profile = _provider_profile(provider)
    if profile["status"] != "ready":
        raise RuntimeError(profile["reason"])
    timeout = 60
    if provider == "gpt":
        return OpenAICompatibleModelClient(
            model=profile["model"],
            base_url=profile["base_url"],
            api_key=profile["api_key"],
            temperature=0.0,
            timeout=timeout,
        )
    return AnthropicCompatibleModelClient(
        model=profile["model"],
        base_url=profile["base_url"],
        api_key=profile["api_key"],
        temperature=0.0,
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
                return OpenAICompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=0.0,
                    timeout=300,
                )

        else:

            def factory(task, workspace, profile=profile):
                del task, workspace
                return AnthropicCompatibleModelClient(
                    model=profile["model"],
                    base_url=profile["base_url"],
                    api_key=profile["api_key"],
                    temperature=0.0,
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
    return {"providers": provider_rows}
