"""Read-only diagnostics for Pico's explicit CLI commands."""

import getpass
import os
import stat
import sys
from pathlib import Path
from urllib import error, request
from urllib.parse import urljoin, urlsplit, urlunsplit

from .cli_errors import CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .cli_output import print_result
from .config import (
    ENV_KEY_PATTERN,
    project_env_path,
    read_project_env,
    validate_provider_base_url,
    write_project_env_assignments,
)
from .providers.defaults import (
    API_KEY_ENV_NAMES,
    BASE_URL_ENV_NAMES,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_PROVIDER,
    MODEL_ENV_NAMES,
)
from .security import is_secret_env_name
from .workspace import WorkspaceContext


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
    _resolve_base_url(args, provider["value"], project_env)
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
    doc_hints = []
    if (root / "CLAUDE.md").exists() and not (root / "AGENTS.md").exists():
        doc_hints.append(
            {
                "level": "info",
                "message": "CLAUDE.md exists but Pico only reads AGENTS.md. Consider: ln -s CLAUDE.md AGENTS.md",
            }
        )
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
        "project_docs": {"hints": doc_hints},
    }


def handle_status(cwd, args):
    return print_result("status", collect_status(cwd, args), args, _render_status)


def handle_doctor(tokens, cwd, args):
    offline = False
    if tokens == ["--offline"]:
        offline = True
    elif tokens:
        raise CliError(
            code="usage",
            message="usage: pico-cli doctor [--offline]",
            exit_code=CLI_EXIT_USAGE,
        )
    return print_result("doctor", collect_doctor(cwd, args, offline=offline), args, _render_doctor)


def handle_config(tokens, cwd, args):
    sub = tokens[0] if tokens else ""
    rest = tokens[1:]
    if sub == "show" and not rest:
        return print_result(
            "config_show",
            collect_config(cwd, args),
            args,
            _render_config,
        )
    if sub == "set-secret":
        return _handle_set_secret(rest, cwd, args)
    raise _config_usage_error()


def _config_usage_error():
    return CliError(
        code="usage",
        message="usage: pico-cli config show | pico-cli config set-secret <ENV_NAME> [--stdin]",
        exit_code=CLI_EXIT_USAGE,
    )


def _handle_set_secret(tokens, cwd, args):
    if len(tokens) not in {1, 2} or (len(tokens) == 2 and tokens[1] != "--stdin"):
        raise _config_usage_error()
    name = tokens[0]
    if not ENV_KEY_PATTERN.fullmatch(name) or not is_secret_env_name(name):
        raise CliError(
            code="usage",
            message="secret environment variable name required",
            exit_code=CLI_EXIT_USAGE,
        )

    use_stdin = len(tokens) == 2
    if not use_stdin and getattr(args, "no_input", False):
        raise CliError(
            code="usage",
            message="secret input unavailable; use --stdin",
            exit_code=CLI_EXIT_USAGE,
        )
    try:
        value = sys.stdin.read() if use_stdin else getpass.getpass(f"{name}: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise CliError(
            code="usage",
            message="secret input unavailable",
            exit_code=CLI_EXIT_USAGE,
        ) from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not value or any(char in value for char in ("\0", "\r", "\n")):
        raise CliError(
            code="usage",
            message="secret input must be one non-empty line",
            exit_code=CLI_EXIT_USAGE,
        )

    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    try:
        written = write_project_env_assignments(root, {name: value})
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="config",
            message="project environment update failed",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    env_path = project_env_path(root)
    mode = ""
    if os.name == "posix":
        mode = f"{stat.S_IMODE(env_path.stat().st_mode):04o}"
    status = "created" if name in written["added"] else "updated"
    return print_result(
        "config_set_secret",
        {
            "name": name,
            "status": status,
            "env_path": env_path.name,
            "permission": mode,
        },
        args,
        _render_set_secret,
    )


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
        result = {"value": explicit, "source": "cli", "name": "--base-url"}
    elif provider == "ollama":
        explicit_host = getattr(args, "host", None) if args is not None else None
        if explicit_host and explicit_host != DEFAULT_OLLAMA_HOST:
            result = {"value": explicit_host, "source": "cli", "name": "--host"}
        else:
            result = _base_url_from_env_or_default(provider, project_env)
    else:
        result = _base_url_from_env_or_default(provider, project_env)
    result["value"] = validate_provider_base_url(result["value"])
    return result


def _base_url_from_env_or_default(provider, project_env):
    value, source, name = _resolve_env_value(BASE_URL_ENV_NAMES.get(provider, ()), project_env)
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
    return read_project_env(start)


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


def _source_label(item):
    source = item.get("source", "")
    name = item.get("name", "")
    if source and name:
        return f"{source}:{name}"
    return source or name or "-"


def _line(label, value):
    lines = str(value).splitlines() or [""]
    rendered = [f"  {label:<14} {lines[0]}"]
    rendered.extend(f"  {'':<14} {line}" for line in lines[1:])
    return "\n".join(rendered)


def _presence_text(item):
    state = "present" if item.get("present") else "missing"
    return f"{state} ({_source_label(item)})"


def _value_with_source(item):
    return f"{item.get('value', '-') or '-'} ({_source_label(item)})"


def _ok_missing(value):
    if isinstance(value, bool):
        return "ok" if value else "missing"
    return str(value)


def _render_config(data):
    lines = [
        "Pico config — Effective configuration",
        "",
        "Provider",
        _line("provider", _value_with_source(data["provider"])),
        _line("model", _value_with_source(data["model"])),
        "",
        "Credentials",
        _line("api key", _presence_text(data["api_key"])),
    ]
    return "\n".join(lines)


def _render_set_secret(data):
    return "\n".join(
        [
            "Pico config — Secret stored",
            "",
            _line("name", data["name"]),
            _line("status", data["status"]),
            _line("env file", data["env_path"]),
            _line("permission", data["permission"] or "private"),
        ]
    )


def _render_doctor(data):
    config = data["config"]
    credentials = data["credentials"]
    connectivity = data["provider_connectivity"]
    storage = data["storage"]
    lines = [
        "Pico doctor — CLI health check",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("status", data["workspace"]["status"]),
        "",
        "Config",
        _line("provider", _value_with_source(config["provider"])),
        _line("model", _value_with_source(config["model"])),
        _line("base url", _value_with_source(config["base_url"])),
        "",
        "Credentials",
        _line("api key", _presence_text(credentials["api_key"])),
        _line("status", credentials["status"]),
        "",
        "Storage",
        _line("sessions", storage["sessions"]),
        _line("runs", storage["runs"]),
        _line("checkpoints", storage["checkpoints"]),
        _line("recovery", data["recovery_store"]),
        "",
        "Provider connectivity",
        _line("status", connectivity.get("status", "-")),
    ]
    if connectivity.get("http_status") is not None:
        lines.append(_line("http", connectivity["http_status"]))
    if connectivity.get("url"):
        lines.append(_line("url", connectivity["url"]))
    if connectivity.get("message"):
        lines.append(_line("message", connectivity["message"]))
    hints = ((data.get("project_docs") or {}).get("hints")) or []
    if hints:
        lines.append("")
        lines.append("Project docs")
        for hint in hints:
            level = hint.get("level", "info")
            message = hint.get("message", "")
            lines.append(_line(level, message))
    return "\n".join(lines)


def _render_status(data):
    lines = [
        "Pico status — Local harness state",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("cwd", data["workspace"]["cwd"]),
        _line("branch", data["workspace"]["branch"]),
        _line("git status", data["workspace"]["status"]),
        "",
        "Provider",
        _line("provider", _value_with_source(data["provider"]["provider"])),
        _line("model", _value_with_source(data["provider"]["model"])),
        _line("api key", _presence_text(data["provider"]["api_key"])),
        "",
        "Storage",
        _line("sessions", _ok_missing(data["storage"]["sessions"])),
        _line("runs", _ok_missing(data["storage"]["runs"])),
        _line("checkpoints", _ok_missing(data["storage"]["checkpoints"])),
        "",
        "Latest",
        _line("session id", data["latest"]["session_id"] or "-"),
        _line("run id", data["latest"]["run_id"] or "-"),
        _line("checkpoint id", data["latest"]["checkpoint_id"] or "-"),
    ]
    return "\n".join(lines)
