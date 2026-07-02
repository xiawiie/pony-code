"""Read-only diagnostics for Pico's explicit CLI commands."""

import os
from pathlib import Path
from urllib import error, request
from urllib.parse import urljoin, urlsplit, urlunsplit

from .config import _parse_env_line, find_project_env
from .workspace import WorkspaceContext


DEFAULT_PROVIDER = "deepseek"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
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
BASE_URL_ENV_NAMES = {
    "openai": ("PICO_OPENAI_API_BASE", "OPENAI_API_BASE"),
    "anthropic": ("PICO_ANTHROPIC_API_BASE", "ANTHROPIC_API_BASE"),
    "deepseek": ("PICO_DEEPSEEK_API_BASE", "DEEPSEEK_API_BASE"),
    "ollama": ("PICO_OLLAMA_HOST",),
}
DEFAULT_BASE_URLS = {
    "ollama": DEFAULT_OLLAMA_HOST,
    "openai": DEFAULT_OPENAI_BASE_URL,
    "anthropic": DEFAULT_ANTHROPIC_BASE_URL,
    "deepseek": DEFAULT_DEEPSEEK_BASE_URL,
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
    project_env = _read_project_env(workspace.repo_root)
    provider = _resolve_provider(args, project_env)
    model = _resolve_model(args, provider["value"], project_env)
    api_key = _resolve_api_key(provider["value"], project_env)
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
    }


def collect_doctor(cwd, args=None, offline=False):
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    pico_root = root / ".pico"
    project_env = _read_project_env(workspace.repo_root)
    config = collect_config(cwd, args)
    config["base_url"] = _resolve_base_url(args, config["provider"]["value"], project_env)
    diagnostic_base_url = dict(config["base_url"])
    diagnostic_base_url["value"] = _redact_url_for_diagnostics(diagnostic_base_url["value"])
    provider_connectivity = (
        {"status": "skipped", "category": "provider_connectivity", "message": "offline mode"}
        if offline
        else check_provider_connectivity(config)
    )
    checkpoints_root = pico_root / "checkpoints"
    return {
        "workspace": {
            "status": "ok",
            "repo_root": workspace.repo_root,
        },
        "config": {
            "status": "ok",
            "provider": config["provider"],
            "model": config["model"],
            "base_url": diagnostic_base_url,
        },
        "credentials": {
            "status": "ok" if config["api_key"]["present"] or config["provider"]["value"] == "ollama" else "missing",
            "api_key": config["api_key"],
        },
        "provider_connectivity": provider_connectivity,
        "storage": {
            "sessions": _storage_status(pico_root / "sessions"),
            "runs": _storage_status(pico_root / "runs"),
            "checkpoints": _storage_status(checkpoints_root / "records"),
        },
        "recovery_store": _storage_status(checkpoints_root),
    }


def check_provider_connectivity(config, timeout=2):
    provider = config["provider"]["value"]
    base_url = config.get("base_url", {}).get("value", "")
    url = _connectivity_url(provider, base_url)
    diagnostic_url = _redact_url_for_diagnostics(url)
    try:
        response = request.urlopen(url, timeout=timeout)
        with response:
            return {
                "status": "ok",
                "category": "provider_connectivity",
                "url": diagnostic_url,
                "http_status": response.status,
            }
    except error.HTTPError as exc:
        status = "ok" if exc.code in {401, 403, 405} else "error"
        return {
            "status": status,
            "category": "provider_connectivity",
            "url": diagnostic_url,
            "http_status": exc.code,
            "message": f"provider endpoint responded with HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "category": "provider_connectivity",
            "url": diagnostic_url,
            "message": f"{type(exc).__name__}: provider connectivity check failed",
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


def _resolve_base_url(args, provider, project_env):
    explicit = getattr(args, "base_url", None) if args is not None else None
    if explicit:
        return {"value": explicit, "source": "cli", "name": "--base-url"}
    if provider == "ollama":
        explicit_host = getattr(args, "host", None) if args is not None else None
        if explicit_host and explicit_host != DEFAULT_OLLAMA_HOST:
            return {"value": explicit_host, "source": "cli", "name": "--host"}
    env_names = BASE_URL_ENV_NAMES.get(provider, ())
    value, source, name = _resolve_env_value(env_names, project_env)
    if value:
        return {"value": value, "source": source, "name": name}
    return {
        "value": DEFAULT_BASE_URLS.get(provider, ""),
        "source": "default",
        "name": f"DEFAULT_{provider.upper()}_BASE_URL" if provider != "ollama" else "DEFAULT_OLLAMA_HOST",
    }


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


def _connectivity_url(provider, base_url):
    if provider == "ollama":
        return urljoin(base_url.rstrip("/") + "/", "api/tags")
    return base_url


def _redact_url_for_diagnostics(value):
    text = str(value or "")
    try:
        parts = urlsplit(text)
        if not parts.scheme or not parts.netloc:
            if "@" in text:
                return "[redacted-url]"
            return urlunsplit(("", "", parts.path, "", ""))
        host = parts.hostname
        if not host:
            return "[redacted-url]"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "[redacted-url]"


def _storage_status(path):
    return "ok" if path.exists() else "missing"


def _read_project_env(start):
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
    return loaded


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
