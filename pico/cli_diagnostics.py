"""Read-only diagnostics for Pico's explicit CLI commands."""

import getpass
import os
import stat
import sys
from pathlib import Path
from urllib import error, request
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import security as securitylib
from .cli_errors import CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .cli_output import build_inspection_redactor, print_result
from .config import (
    ENV_KEY_PATTERN,
    project_env_metadata,
    project_env_path,
    read_project_env_with_status,
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
from .security import is_secret_env_name, require_directory_no_symlink
from .safe_subprocess import build_trusted_executables
from .workspace import WorkspaceContext


def collect_status(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    pico_root = root / ".pico"
    sessions_root = pico_root / "sessions"
    runs_root = pico_root / "runs"
    checkpoint_records_root = pico_root / "checkpoints" / "records"
    config = collect_config(cwd, args)
    try:
        from .checkpoint_store import CheckpointStore
        from .recovery_manager import collect_recovery_review_items

        review_items = collect_recovery_review_items(
            CheckpointStore(root), root
        )
        active_reviews = (
            review_items["tool_changes"]
            + review_items["restore_journals"]
            + review_items["invalid_records"]
        )
        recovery_review = {
            "active_count": len(active_reviews),
            "opaque_ids": [
                item["opaque_id"]
                for item in review_items["invalid_records"]
            ],
        }
    except (OSError, RuntimeError, ValueError):
        recovery_review = {"active_count": 0, "opaque_ids": []}
    return {
        "workspace": {
            "cwd": workspace.cwd,
            "repo_root": workspace.repo_root,
            "branch": workspace.branch,
            "status": workspace.status,
        },
        "storage": {
            "sessions": _storage_exists(sessions_root),
            "runs": _storage_exists(runs_root),
            "checkpoints": _storage_exists(checkpoint_records_root),
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
        "recovery_review": recovery_review,
    }


def collect_config(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    project_env, project_env_info = _read_project_env_for_diagnostics(
        workspace.repo_root
    )
    provider = _resolve_provider(args, project_env)
    model = _resolve_model(args, provider["value"], project_env)
    api_key = _resolve_api_key(provider["value"], project_env)
    _resolve_base_url(args, provider["value"], project_env)
    return {
        "workspace": {"repo_root": workspace.repo_root},
        "project_env": project_env_info,
        "provider": provider,
        "model": model,
        "api_key": api_key,
    }


def collect_doctor(cwd, args=None, offline=False):
    try:
        workspace = WorkspaceContext.build(cwd)
    except (OSError, RuntimeError, ValueError):
        try:
            workspace = WorkspaceContext.build(cwd, executables={})
        except (OSError, RuntimeError, ValueError):
            return _unavailable_workspace_doctor(offline=offline)
    root = Path(workspace.repo_root)
    pico_root = root / ".pico"
    project_env, project_env_info = _read_project_env_for_diagnostics(
        workspace.repo_root
    )
    provider = _resolve_provider(args, project_env)
    config = {
        "provider": provider,
        "model": _resolve_model(args, provider["value"], project_env),
        "api_key": _resolve_api_key(provider["value"], project_env),
        "base_url": _resolve_base_url(args, provider["value"], project_env),
    }
    diagnostic_base_url = dict(config["base_url"])
    diagnostic_base_url["value"] = _redact_url_for_diagnostics(diagnostic_base_url["value"])
    provider_connectivity = (
        {"status": "skipped", "category": "provider_connectivity", "message": "offline mode"}
        if offline
        else check_provider_connectivity(config)
    )
    checkpoints_root = pico_root / "checkpoints"
    security = _collect_security_status(
        root,
        project_env_info,
        pico_root,
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
        "project_env": project_env_info,
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
        "security": security,
        "project_docs": {"hints": doc_hints},
    }


def _unavailable_workspace_doctor(*, offline):
    connectivity_message = "offline mode" if offline else "workspace unavailable"
    return {
        "workspace": {"status": "review_required", "repo_root": ""},
        "project_env": {
            "path": "",
            "scope": "repo_root_exact",
            "status": "review_required",
        },
        "config": {
            "status": "review_required",
            "provider": {"value": "", "source": "unavailable", "name": ""},
            "model": {"value": "", "source": "unavailable", "name": ""},
            "base_url": {"value": "", "source": "unavailable", "name": ""},
        },
        "credentials": {
            "status": "review_required",
            "api_key": {"present": False, "source": "unavailable", "name": ""},
        },
        "provider_connectivity": {
            "status": "skipped",
            "category": "provider_connectivity",
            "message": connectivity_message,
        },
        "storage": {
            "sessions": "review_required",
            "runs": "review_required",
            "checkpoints": "review_required",
        },
        "recovery_store": "review_required",
        "security": {
            "status": "review_required",
            "project_env": {"status": "review_required", "mode": ""},
            "private_storage": {"status": "review_required"},
            "trusted_executables": {
                "status": "degraded",
                "missing": ["git", "rg"],
            },
            "recovery_review": {
                "pending_count": 0,
                "applying_count": 0,
                "unreviewed_partial_count": 0,
                "invalid_mutation_count": 0,
            },
        },
        "project_docs": {"hints": []},
    }


def handle_status(cwd, args):
    data = collect_status(cwd, args)
    redactor = build_inspection_redactor(data["workspace"]["repo_root"], args)
    data["workspace"] = redactor(data["workspace"])
    data["latest"] = redactor(data["latest"])
    data["provider"] = _redact_mapping_values(data["provider"], redactor)
    return print_result("status", data, args, _render_status)


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
    data = collect_doctor(cwd, args, offline=offline)
    repo_root = data["workspace"]["repo_root"]
    if not repo_root:
        redactor = securitylib.redact_artifact
    else:
        try:
            redactor = build_inspection_redactor(repo_root, args)
        except (OSError, RuntimeError, ValueError):
            redactor = securitylib.redact_artifact
    data["workspace"] = redactor(data["workspace"])
    data["project_env"] = redactor(data["project_env"])
    data["config"] = _redact_mapping_values(data["config"], redactor)
    data["credentials"] = _redact_mapping_values(data["credentials"], redactor)
    data["provider_connectivity"] = redactor(data["provider_connectivity"])
    data["security"] = redactor(data["security"])
    data["project_docs"] = redactor(data["project_docs"])
    return print_result("doctor", data, args, _render_doctor)


def handle_config(tokens, cwd, args):
    sub = tokens[0] if tokens else ""
    rest = tokens[1:]
    if sub == "show" and not rest:
        data = collect_config(cwd, args)
        try:
            redactor = build_inspection_redactor(
                data["workspace"]["repo_root"],
                args,
            )
        except (OSError, RuntimeError, ValueError):
            redactor = securitylib.redact_artifact
        return print_result(
            "config_show",
            _redact_mapping_values(data, redactor),
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
    _, project_env = _read_project_env_for_diagnostics(root)
    try:
        redactor = build_inspection_redactor(root, args)
    except (OSError, RuntimeError, ValueError):
        redactor = securitylib.redact_artifact
    workspace_info = redactor({"repo_root": str(root)})
    project_env = redactor(project_env)
    status = "created" if name in written["added"] else "updated"
    return print_result(
        "config_set_secret",
        {
            "name": name,
            "status": status,
            "workspace": workspace_info,
            "project_env": project_env,
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
    return "ok" if _storage_exists(path) else "missing"


def _collect_security_status(root, project_env_info, pico_root):
    project_env = _project_env_security_status(
        Path(project_env_info["path"]),
        project_env_info["status"],
    )
    private_storage = _private_storage_security_status(pico_root)
    try:
        trusted_names = set(
            build_trusted_executables(
                root,
                env=os.environ,
                names=("git", "rg"),
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        trusted_names = set()
    missing = sorted(
        name
        for name in ("git", "rg")
        if name not in trusted_names
    )
    executables = {
        "status": "degraded" if missing else "ok",
        "missing": missing,
    }
    review_inspection_failed = False
    try:
        from .checkpoint_store import CheckpointStore
        from .recovery_manager import collect_recovery_review_items

        reviews = collect_recovery_review_items(CheckpointStore(root), root)
    except (OSError, RuntimeError, TypeError, ValueError):
        review_inspection_failed = True
        reviews = {
            "tool_changes": [],
            "restore_journals": [],
            "invalid_records": [],
        }
    recovery_review = {
        "pending_count": len(reviews["tool_changes"]),
        "applying_count": sum(
            item.get("status") == "applying"
            for item in reviews["restore_journals"]
        ),
        "unreviewed_partial_count": sum(
            item.get("status") == "partial" and not item.get("reviewed_at")
            for item in reviews["restore_journals"]
        ),
        "invalid_mutation_count": (
            len(reviews["invalid_records"])
            + int(review_inspection_failed)
        ),
    }
    needs_review = (
        project_env["status"] == "review_required"
        or private_storage["status"] == "review_required"
        or executables["status"] == "degraded"
        or any(recovery_review.values())
    )
    return {
        "status": "review_required" if needs_review else "ok",
        "project_env": project_env,
        "private_storage": private_storage,
        "trusted_executables": executables,
        "recovery_review": recovery_review,
    }


def _project_env_security_status(path, read_status):
    try:
        mode = Path(path).lstat().st_mode
    except FileNotFoundError:
        return {"status": "missing", "mode": ""}
    except OSError:
        return {"status": "review_required", "mode": ""}
    if not stat.S_ISREG(mode):
        return {"status": "review_required", "mode": ""}
    permission_mode = (
        f"{stat.S_IMODE(mode):04o}"
        if os.name == "posix"
        else ""
    )
    status = str(read_status)
    if permission_mode and permission_mode != "0600":
        status = "review_required"
    return {"status": status, "mode": permission_mode}


def _private_storage_security_status(root):
    root = Path(root)
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError:
        return {"status": "review_required"}
    if not stat.S_ISDIR(root_mode) or (
        os.name == "posix" and stat.S_IMODE(root_mode) != 0o700
    ):
        return {"status": "review_required"}

    errors = []
    try:
        for directory, dirnames, filenames in os.walk(
            root,
            followlinks=False,
            onerror=errors.append,
        ):
            for name, expected_type, expected_mode in (
                *((name, stat.S_ISDIR, 0o700) for name in dirnames),
                *((name, stat.S_ISREG, 0o600) for name in filenames),
            ):
                info = (Path(directory) / name).lstat()
                mode = info.st_mode
                if (
                    not expected_type(mode)
                    or (stat.S_ISREG(mode) and info.st_nlink != 1)
                    or (
                    os.name == "posix"
                    and stat.S_IMODE(mode) != expected_mode
                    )
                ):
                    return {"status": "review_required"}
    except OSError:
        return {"status": "review_required"}
    return {"status": "review_required" if errors else "ok"}


def _redact_mapping_values(data, redactor):
    return {key: redactor(value) for key, value in data.items()}


def _storage_exists(path):
    try:
        require_directory_no_symlink(path)
    except (OSError, ValueError):
        return False
    return True


def _read_project_env_for_diagnostics(root):
    try:
        return read_project_env_with_status(root)
    except (OSError, RuntimeError, ValueError):
        return {}, project_env_metadata(root, "review_required")


def _latest_json_stem(root):
    try:
        root = require_directory_no_symlink(root)
    except (OSError, ValueError):
        return None
    files = []
    for path in root.glob("*.json"):
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                files.append(path)
        except OSError:
            continue
    if not files:
        return None
    return max(files, key=lambda path: (path.lstat().st_mtime, path.name)).stem


def _latest_dir_name(root):
    try:
        root = require_directory_no_symlink(root)
    except (OSError, ValueError):
        return None
    dirs = []
    for path in root.iterdir():
        try:
            if stat.S_ISDIR(path.lstat().st_mode):
                dirs.append(path)
        except OSError:
            continue
    if not dirs:
        return None
    return max(dirs, key=lambda path: (path.lstat().st_mtime, path.name)).name


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
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        "",
        "Project environment",
        _line("path", data["project_env"]["path"]),
        _line("scope", data["project_env"]["scope"]),
        _line("status", data["project_env"]["status"]),
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
            "",
            "Workspace",
            _line("repo root", data["workspace"]["repo_root"]),
            "",
            "Project environment",
            _line("env file", data["project_env"]["path"]),
            _line("env scope", data["project_env"]["scope"]),
            _line("env status", data["project_env"]["status"]),
            _line("permission", data["permission"] or "private"),
        ]
    )


def _render_doctor(data):
    config = data["config"]
    credentials = data["credentials"]
    connectivity = data["provider_connectivity"]
    storage = data["storage"]
    security = data["security"]
    security_executables = security["trusted_executables"]
    recovery_review = security["recovery_review"]
    lines = [
        "Pico doctor — CLI health check",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("status", data["workspace"]["status"]),
        "",
        "Project environment",
        _line("path", data["project_env"]["path"]),
        _line("scope", data["project_env"]["scope"]),
        _line("status", data["project_env"]["status"]),
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
        "Security",
        _line("status", security["status"]),
        _line(
            "project env",
            " ".join(
                item
                for item in (
                    security["project_env"]["status"],
                    security["project_env"]["mode"],
                )
                if item
            ),
        ),
        _line("private store", security["private_storage"]["status"]),
        _line("executables", security_executables["status"]),
        _line("missing", ", ".join(security_executables["missing"]) or "-"),
        _line("pending", recovery_review["pending_count"]),
        _line("applying", recovery_review["applying_count"]),
        _line("partial", recovery_review["unreviewed_partial_count"]),
        _line("invalid", recovery_review["invalid_mutation_count"]),
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
