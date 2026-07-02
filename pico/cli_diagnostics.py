"""Read-only diagnostics for Pico's explicit CLI commands."""

import os
from pathlib import Path

from .config import load_project_env
from .workspace import WorkspaceContext


DEFAULT_PROVIDER = "deepseek"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")

MODEL_ENV_NAMES = {
    "openai": ("PICO_OPENAI_MODEL", "OPENAI_MODEL"),
    "anthropic": ("PICO_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
    "deepseek": ("PICO_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
}
DEFAULT_MODELS = {
    "ollama": DEFAULT_OLLAMA_MODEL,
    "openai": DEFAULT_OPENAI_MODEL,
    "anthropic": DEFAULT_ANTHROPIC_MODEL,
    "deepseek": DEFAULT_DEEPSEEK_MODEL,
}
API_KEY_ENV_NAMES = {
    "openai": (
        "PICO_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "PICO_RIGHT_CODES_API_KEY",
        "RIGHT_CODES_API_KEY",
        "PICO_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
    ),
    "anthropic": (
        "PICO_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "PICO_RIGHT_CODES_API_KEY",
        "RIGHT_CODES_API_KEY",
        "PICO_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    ),
    "deepseek": ("PICO_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    "ollama": (),
}


def collect_status(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    pico_root = root / ".pico"
    sessions_root = pico_root / "sessions"
    runs_root = pico_root / "runs"
    checkpoint_records_root = pico_root / "checkpoints" / "records"
    config = collect_config(cwd, args)
    return {
        "workspace": {
            "cwd": workspace.cwd,
            "repo_root": workspace.repo_root,
            "branch": workspace.branch,
            "status": workspace.status,
        },
        "storage": {
            "sessions": sessions_root.exists(),
            "runs": runs_root.exists(),
            "checkpoints": checkpoint_records_root.exists(),
        },
        "provider": {
            "provider": config["provider"],
            "model": config["model"],
            "api_key": config["api_key"],
        },
        "latest": {
            "session_id": _latest_json_stem(sessions_root),
            "run_id": _latest_dir_name(runs_root),
            "checkpoint_id": _latest_json_stem(checkpoint_records_root),
        },
    }


def collect_config(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    project_env = load_project_env(workspace.repo_root)
    provider = _resolve_provider(args, project_env)
    model = _resolve_model(args, provider["value"], project_env)
    api_key = _resolve_api_key(provider["value"], project_env)
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
    }


def _resolve_provider(args, project_env):
    explicit = getattr(args, "provider", None) if args is not None else None
    if explicit:
        return {"value": explicit, "source": "cli", "name": "--provider"}
    value, source, name = _resolve_env_value(("PICO_PROVIDER",), project_env)
    if value:
        return {"value": value, "source": source, "name": name}
    return {
        "value": DEFAULT_PROVIDER,
        "source": "default",
        "name": "DEFAULT_PROVIDER",
    }


def _resolve_model(args, provider, project_env):
    explicit = getattr(args, "model", None) if args is not None else None
    if explicit:
        return {"value": explicit, "source": "cli", "name": "--model"}
    env_names = MODEL_ENV_NAMES.get(provider, ())
    value, source, name = _resolve_env_value(env_names, project_env)
    if value:
        return {"value": value, "source": source, "name": name}
    default_value = DEFAULT_MODELS.get(provider, "")
    return {
        "value": default_value,
        "source": "default",
        "name": f"DEFAULT_{provider.upper()}_MODEL" if provider else "",
    }


def _resolve_api_key(provider, project_env):
    env_names = API_KEY_ENV_NAMES.get(provider, ())
    value, source, name = _resolve_env_value(env_names, project_env)
    if value:
        return {"present": True, "source": source, "name": name}
    return {"present": False, "source": "unset", "name": ""}


def _resolve_env_value(env_names, project_env):
    for name in env_names:
        value = project_env.get(name)
        if value:
            return value, "project_env", name
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value, "environment", name
    return "", "unset", ""


def _latest_json_stem(root):
    if not root.exists():
        return None
    files = [path for path in root.glob("*.json") if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: (path.stat().st_mtime, path.name)).stem


def _latest_dir_name(root):
    if not root.exists():
        return None
    dirs = [path for path in root.iterdir() if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: (path.stat().st_mtime, path.name)).name
