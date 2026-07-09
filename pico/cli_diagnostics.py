"""Read-only diagnostics for Pico's explicit CLI commands."""

from contextlib import contextmanager
import os
from pathlib import Path
from urllib import error, request
from urllib.parse import urljoin, urlsplit, urlunsplit

from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_output import print_result
from .config import load_pico_toml_full, read_project_env
from .model_config import DEFAULT_BASE_URL, DEFAULT_MODEL_NAME, load_model_connection
from .model_resolver import resolve_model_connection
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
        "model": config["model"],
        "latest": {
            "session_id": _latest_json_stem(sessions_root),
            "run_id": _latest_dir_name(runs_root),
            "checkpoint_id": _latest_json_stem(checkpoint_records_root),
        },
    }


def collect_config(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    model = _collect_model_connection(workspace.repo_root)
    return {"model": _public_model(model)}


def collect_doctor(cwd, args=None, offline=False):
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    pico_root = root / ".pico"
    checkpoints_root = pico_root / "checkpoints"
    model = _collect_model_connection(workspace.repo_root)
    public_model = _public_model(model)
    model_connectivity = (
        {"status": "skipped", "category": "model_connectivity", "message": "offline mode"}
        if offline
        else check_model_connectivity({"model": model})
    )
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
            "status": public_model["status"],
            "model": public_model,
        },
        "credentials": {
            "status": _credential_status(public_model),
            "api_key_env": public_model["api_key_env"],
            "api_key_present": public_model["api_key_present"],
        },
        "model_connectivity": model_connectivity,
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
    raise CliError(
        code="usage",
        message="usage: pico-cli config show",
        exit_code=CLI_EXIT_USAGE,
    )


def check_model_connectivity(config, timeout=2):
    model = config.get("model", {})
    api = model.get("api", "")
    base_url = model.get("_base_url_raw") or model.get("base_url", "")
    url = _connectivity_url(api, base_url)
    diagnostic_url = _redact_url_for_diagnostics(url)
    try:
        response = request.urlopen(url, timeout=timeout)
        with response:
            return {
                "status": "ok",
                "category": "model_connectivity",
                "url": diagnostic_url,
                "http_status": response.status,
            }
    except error.HTTPError as exc:
        status = "ok" if exc.code in {401, 403, 405} else "error"
        return {
            "status": status,
            "category": "model_connectivity",
            "url": diagnostic_url,
            "http_status": exc.code,
            "message": f"model endpoint responded with HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "category": "model_connectivity",
            "url": diagnostic_url,
            "message": f"{type(exc).__name__}: model connectivity check failed",
        }


def _collect_model_connection(workspace_root):
    project_env = _read_project_env(workspace_root)
    connection = None
    with _temporary_project_env(project_env):
        try:
            connection = load_model_connection(workspace_root)
            resolved = resolve_model_connection(connection)
        except Exception as exc:
            if connection is not None:
                return _model_error_from_connection(connection, exc)
            return _model_error_from_raw(workspace_root, project_env, exc)
    return {
        "status": "ok",
        "name": resolved.name,
        "base_url": _redact_url_for_diagnostics(resolved.base_url),
        "api_key_env": resolved.api_key_env,
        "api_key_present": bool(resolved.api_key),
        "api": resolved.api,
        "adapter": resolved.adapter_class,
        "native_tools": resolved.native_tools,
        "prompt_cache": resolved.prompt_cache,
        "_base_url_raw": resolved.base_url,
    }


def _model_error_from_connection(connection, exc):
    return {
        "status": "error",
        "name": connection.name,
        "base_url": _redact_url_for_diagnostics(connection.base_url),
        "api_key_env": connection.api_key_env,
        "api_key_present": bool(connection.api_key),
        "api": connection.api or "",
        "adapter": "",
        "native_tools": False,
        "prompt_cache": False,
        "message": _safe_error_message(exc, connection.base_url),
        "_base_url_raw": connection.base_url,
    }


def _model_error_from_raw(workspace_root, project_env, exc):
    raw = _raw_model_options(workspace_root)
    name = _raw_string(raw.get("name"), DEFAULT_MODEL_NAME) or DEFAULT_MODEL_NAME
    base_url = (_raw_string(raw.get("base_url"), DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    api_key_env = _raw_string(raw.get("api_key_env"))
    api_key_present = bool(api_key_env and (project_env.get(api_key_env) or os.environ.get(api_key_env)))
    return {
        "status": "error",
        "name": name,
        "base_url": _redact_url_for_diagnostics(base_url),
        "api_key_env": api_key_env,
        "api_key_present": api_key_present,
        "api": _raw_string(raw.get("api")),
        "adapter": "",
        "native_tools": False,
        "prompt_cache": False,
        "message": _safe_error_message(exc, base_url),
        "_base_url_raw": base_url,
    }


def _raw_model_options(workspace_root):
    data = load_pico_toml_full(workspace_root)
    model = data.get("model")
    return model if isinstance(model, dict) else {}


def _raw_string(raw, default=""):
    if raw is None:
        return default
    if not isinstance(raw, str):
        return default
    return raw.strip()


def _safe_error_message(exc, base_url):
    message = str(exc)
    if base_url:
        message = message.replace(str(base_url), _redact_url_for_diagnostics(base_url))
    return message


def _public_model(model):
    return {key: value for key, value in model.items() if not key.startswith("_")}


def _credential_status(model):
    if not model.get("api_key_env"):
        return "ok"
    return "ok" if model.get("api_key_present") else "missing"


@contextmanager
def _temporary_project_env(project_env):
    missing = object()
    previous = {}
    try:
        for name, value in project_env.items():
            previous[name] = os.environ.get(name, missing)
            os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is missing:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _connectivity_url(api, base_url):
    if api == "ollama":
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


def _line(label, value):
    lines = str(value).splitlines() or [""]
    rendered = [f"  {label:<14} {lines[0]}"]
    rendered.extend(f"  {'':<14} {line}" for line in lines[1:])
    return "\n".join(rendered)


def _present_text(value):
    return "present" if value else "missing"


def _bool_text(value):
    return "yes" if value else "no"


def _render_model_lines(model):
    lines = [
        _line("status", model.get("status", "-")),
        _line("name", model.get("name") or "-"),
        _line("base url", model.get("base_url") or "-"),
        _line("api", model.get("api") or "-"),
        _line("adapter", model.get("adapter") or "-"),
        _line("api key env", model.get("api_key_env") or "-"),
        _line("api key", _present_text(model.get("api_key_present"))),
        _line("native tools", _bool_text(model.get("native_tools"))),
        _line("prompt cache", _bool_text(model.get("prompt_cache"))),
    ]
    if model.get("message"):
        lines.append(_line("message", model["message"]))
    return lines


def _ok_missing(value):
    if isinstance(value, bool):
        return "ok" if value else "missing"
    return str(value)


def _render_config(data):
    lines = [
        "Pico config — Effective configuration",
        "",
        "Model",
        *_render_model_lines(data["model"]),
    ]
    return "\n".join(lines)


def _render_doctor(data):
    config = data["config"]
    model = config["model"]
    credentials = data["credentials"]
    connectivity = data["model_connectivity"]
    storage = data["storage"]
    lines = [
        "Pico doctor — CLI health check",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("status", data["workspace"]["status"]),
        "",
        "Config",
        _line("status", config["status"]),
        "",
        "Model",
        *_render_model_lines(model),
        "",
        "Credentials",
        _line("api key env", credentials["api_key_env"] or "-"),
        _line("api key", _present_text(credentials["api_key_present"])),
        _line("status", credentials["status"]),
        "",
        "Storage",
        _line("sessions", storage["sessions"]),
        _line("runs", storage["runs"]),
        _line("checkpoints", storage["checkpoints"]),
        _line("recovery", data["recovery_store"]),
        "",
        "Model connectivity",
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
        "Model",
        *_render_model_lines(data["model"]),
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
