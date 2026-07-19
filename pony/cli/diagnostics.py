"""Read-only diagnostics for Pony's explicit CLI commands."""

import getpass
import os
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pony.security import redaction as securitylib
from .errors import (
    CLI_EXIT_CONFIG,
    CLI_EXIT_USAGE,
    CliError,
    provider_report_cli_error,
)
from .output import build_inspection_redactor, print_result
from pony.config.environment import (
    project_env_metadata,
    project_env_path,
    read_project_env_with_status,
    write_project_env_assignments,
)
from pony.config.model import API_KEY_ENV_NAME, resolve_model_config
from pony.security.paths import require_directory_no_symlink
from pony.tools.subprocess import build_trusted_executables
from pony.memory.diagnostics import collect_memory_diagnostics
from pony.workspace.context import WorkspaceContext


def _doctor_check(status, reason_code, remediation=""):
    return {
        "status": status,
        "reason_code": reason_code,
        "remediation": remediation,
    }


def _unavailable_memory_diagnostic():
    return {
        "check_id": "memory",
        "status": "unknown",
        "reason_code": "memory_diagnostics_incomplete",
        "remediation": "resolve Memory filesystem or Git access and rerun pony doctor",
        "issues": [],
    }


def collect_status(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    pony_root = root / ".pony"
    sessions_root = pony_root / "sessions"
    runs_root = pony_root / "runs"
    config = collect_config(cwd, args)
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
        },
        "model": {
            "provider": config["provider"],
            "resolved_provider": config["resolved_provider"],
            "resolution_status": config["resolution_status"],
            "resolution_source": config["resolution_source"],
            "protocol": config["protocol"],
            "api_variant": config["api_variant"],
            "model": config["model"],
            "base_url": config["base_url"],
            "auth_mode": config["auth_mode"],
            "api_key": config["api_key"],
        },
        "latest": {
            "session_id": _latest_json_stem(sessions_root),
            "run_id": _latest_dir_name(runs_root),
        },
    }


def collect_config(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    project_env, project_env_info = _read_project_env_for_diagnostics(
        workspace.repo_root
    )
    config = resolve_model_config(
        project_env=project_env,
        process_env=dict(os.environ),
        required=False,
    )
    api_key = config["api_key"]
    return {
        "workspace": {"repo_root": workspace.repo_root},
        "project_env": project_env_info,
        "provider": config["provider"],
        "resolved_provider": config["resolved_provider"],
        "resolution_status": config["resolution_status"],
        "resolution_source": config["resolution_source"],
        "protocol": config["protocol"],
        "api_variant": config["api_variant"],
        "model": config["model"],
        "auth_mode": config["auth_mode"],
        "base_url": {
            **config["base_url"],
            "value": _redact_url_for_diagnostics(config["base_url"]["value"]),
        },
        "api_key": {
            "present": bool(api_key["value"]),
            "source": api_key["source"],
            "name": api_key["name"],
        },
    }


def collect_doctor(cwd, args=None, check_api=False):
    try:
        workspace = WorkspaceContext.build(cwd)
    except (OSError, RuntimeError, ValueError):
        try:
            workspace = WorkspaceContext.build(cwd, executables={})
        except (OSError, RuntimeError, ValueError):
            return _unavailable_workspace_doctor()
    root = Path(workspace.repo_root)
    pony_root = root / ".pony"
    project_env, project_env_info = _read_project_env_for_diagnostics(
        workspace.repo_root
    )
    resolved = resolve_model_config(
        project_env=project_env,
        process_env=dict(os.environ),
        required=False,
    )
    api_key = resolved["api_key"]
    config = {
        "provider": resolved["provider"],
        "resolved_provider": resolved["resolved_provider"],
        "resolution_status": resolved["resolution_status"],
        "resolution_source": resolved["resolution_source"],
        "protocol": resolved["protocol"],
        "api_variant": resolved["api_variant"],
        "model": resolved["model"],
        "auth_mode": resolved["auth_mode"],
        "capabilities": resolved["capabilities"],
        "api_key": {
            "present": bool(api_key["value"]),
            "source": api_key["source"],
            "name": api_key["name"],
        },
        "base_url": resolved["base_url"],
    }
    diagnostic_base_url = dict(config["base_url"])
    diagnostic_base_url["value"] = _redact_url_for_diagnostics(
        diagnostic_base_url["value"]
    )
    if not check_api:
        api_check = {
            "status": "skipped",
            "category": "api_protocol",
            "message": "explicit --check-api not requested",
        }
    else:
        api_check = check_api_connectivity(
            resolved,
            args=args,
        )
    security = _collect_security_status(
        root,
        project_env_info,
        pony_root,
    )
    try:
        memory = collect_memory_diagnostics(
            root,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        TimeoutError,
    ):
        memory = _unavailable_memory_diagnostic()
    doc_hints = []
    if (root / "CLAUDE.md").exists() and not (root / "AGENTS.md").exists():
        doc_hints.append(
            {
                "level": "info",
                "message": "CLAUDE.md exists but Pony only reads AGENTS.md. Consider: ln -s CLAUDE.md AGENTS.md",
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
            "resolved_provider": config["resolved_provider"],
            "resolution_status": config["resolution_status"],
            "resolution_source": config["resolution_source"],
            "protocol": config["protocol"],
            "api_variant": config["api_variant"],
            "model": config["model"],
            "auth_mode": config["auth_mode"],
            "base_url": diagnostic_base_url,
        },
        "credentials": {
            "status": (
                "not_required"
                if not config["api_key"]["present"]
                and (
                    resolved["auth_mode"]["value"] == "none"
                    or any(
                        item.get("auth_mode") == "none"
                        for item in resolved.get("candidates", [])
                    )
                )
                else "ok"
                if config["api_key"]["present"]
                else "missing"
            ),
            "api_key": config["api_key"],
        },
        "api_check": api_check,
        "storage": {
            "sessions": _storage_status(pony_root / "sessions"),
            "runs": _storage_status(pony_root / "runs"),
        },
        "memory": memory,
        "security": security,
        "project_docs": {"hints": doc_hints},
    }


def _unavailable_workspace_doctor():
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
            "resolved_provider": {
                "value": "",
                "source": "unavailable",
                "name": "",
            },
            "resolution_status": "invalid",
            "resolution_source": "",
            "protocol": {"value": "", "source": "unavailable", "name": ""},
            "api_variant": {"value": "", "source": "unavailable", "name": ""},
            "model": {"value": "", "source": "unavailable", "name": ""},
            "auth_mode": {"value": "", "source": "unavailable", "name": ""},
            "base_url": {"value": "", "source": "unavailable", "name": ""},
        },
        "credentials": {
            "status": "review_required",
            "api_key": {"present": False, "source": "unavailable", "name": ""},
        },
        "api_check": {
            "status": "skipped",
            "category": "api_protocol",
            "message": "workspace unavailable",
        },
        "storage": {
            "sessions": "review_required",
            "runs": "review_required",
        },
        "memory": _unavailable_memory_diagnostic(),
        "security": {
            "status": "review_required",
            "project_env": {"status": "review_required", "mode": ""},
            "private_storage": {"status": "review_required"},
            "trusted_executables": {
                "status": "degraded",
                "missing": ["git", "rg"],
            },
        },
        "project_docs": {"hints": []},
    }


def handle_status(cwd, args):
    data = collect_status(cwd, args)
    redactor = build_inspection_redactor(data["workspace"]["repo_root"], args)
    data["workspace"] = redactor(data["workspace"])
    data["latest"] = redactor(data["latest"])
    data["model"] = _redact_mapping_values(data["model"], redactor)
    return print_result("status", data, args, _render_status)


def handle_doctor(tokens, cwd, args):
    if list(tokens) not in ([], ["--check-api"]):
        raise CliError(
            code="usage",
            message="usage: pony doctor [--check-api]",
            exit_code=CLI_EXIT_USAGE,
        )
    data = collect_doctor(
        cwd,
        args,
        check_api=bool(tokens),
    )
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
    data["api_check"] = redactor(data["api_check"])
    memory = data.get("memory") or _unavailable_memory_diagnostic()
    data["memory"] = redactor(
        {
        "check_id": memory.get("check_id", "memory"),
        "status": memory.get("status", "unknown"),
        "reason_code": memory.get("reason_code", "memory_diagnostics_incomplete"),
        "remediation": memory.get("remediation", ""),
            "issues": [dict(item) for item in memory.get("issues", [])],
        }
    )
    data["security"] = redactor(data["security"])
    data["project_docs"] = redactor(data["project_docs"])
    if tokens and data["api_check"].get("status") != "ok":
        raise provider_report_cli_error(data["api_check"])
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
        message="usage: pony config show | pony config set-secret <ENV_NAME> [--stdin]",
        exit_code=CLI_EXIT_USAGE,
    )


def _handle_set_secret(tokens, cwd, args):
    if len(tokens) not in {1, 2} or (len(tokens) == 2 and tokens[1] != "--stdin"):
        raise _config_usage_error()
    name = tokens[0]
    if name != API_KEY_ENV_NAME:
        raise CliError(
            code="usage",
            message=f"expected {API_KEY_ENV_NAME}",
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
    if not value.strip() or any(char in value for char in ("\0", "\r", "\n")):
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


def check_api_connectivity(config, timeout=2, args=None):
    """Run the explicit read-only Provider verification probe."""
    result = {
        "status": "failed",
        "category": "api_protocol",
        "model_calls": 0,
    }
    try:
        from pony.providers.probe import resolve_provider_client

        _client, resolved, report = resolve_provider_client(
            config,
            timeout=getattr(args, "request_timeout_seconds", timeout),
            verify_resolved=True,
        )
    except Exception as exc:
        failure = {
            **result,
            "reason_code": str(getattr(exc, "code", "api_check_failed")),
            "stage": getattr(exc, "stage", "") or "runtime",
            "protocol": getattr(exc, "protocol_family", "") or "",
        }
        if getattr(exc, "protocol_reason", None):
            failure["protocol_reason"] = exc.protocol_reason
        if getattr(exc, "http_status", None) is not None:
            failure["http_status"] = exc.http_status
        return failure
    return {
        **result,
        "status": "ok",
        "category": report["category"],
        "reason_code": "api_verified",
        "stage": report["stage"],
        "model_calls": report["model_calls"],
        "candidate_count": report["candidate_count"],
        "detected_provider": resolved["resolved_provider"]["value"],
        "protocol": resolved["protocol"]["value"],
        "native_tools": "passed",
        "tool_continuation": "passed",
        "usage_status": report["usage_status"],
        "persist_with": "pony init",
    }


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
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    except ValueError:
        return "[redacted-url]"


def _storage_status(path):
    return "ok" if _storage_exists(path) else "missing"


def _collect_security_status(root, project_env_info, pony_root):
    project_env = _project_env_security_status(
        Path(project_env_info["path"]),
        project_env_info["status"],
    )
    private_storage = _private_storage_security_status(pony_root)
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
    missing = sorted(name for name in ("git", "rg") if name not in trusted_names)
    executables = {
        "status": "degraded" if missing else "ok",
        "missing": missing,
    }
    needs_review = (
        project_env["status"] == "review_required"
        or private_storage["status"] == "review_required"
        or executables["status"] == "degraded"
    )
    return {
        "status": "review_required" if needs_review else "ok",
        "project_env": project_env,
        "private_storage": private_storage,
        "trusted_executables": executables,
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
    permission_mode = f"{stat.S_IMODE(mode):04o}" if os.name == "posix" else ""
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
                    or (os.name == "posix" and stat.S_IMODE(mode) != expected_mode)
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


def _protocol_with_resolution(data):
    if data.get("resolution_status") == "probe_required":
        return "unresolved"
    return _value_with_source(data["protocol"])


def _ok_missing(value):
    if isinstance(value, bool):
        return "ok" if value else "missing"
    return str(value)


def _render_config(data):
    lines = [
        "Pony config — Effective configuration",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        "",
        "Project environment",
        _line("path", data["project_env"]["path"]),
        _line("scope", data["project_env"]["scope"]),
        _line("status", data["project_env"]["status"]),
        "",
        "Model",
        _line("provider", _value_with_source(data["provider"])),
        _line("resolution", data["resolution_status"]),
        _line("resolution source", data["resolution_source"] or "-"),
        _line("resolved provider", _value_with_source(data["resolved_provider"])),
        _line("protocol", _protocol_with_resolution(data)),
        _line("api variant", _value_with_source(data["api_variant"])),
        _line("model", _value_with_source(data["model"])),
        _line("api url", _value_with_source(data["base_url"])),
        _line("auth mode", _value_with_source(data["auth_mode"])),
        "",
        "Credentials",
        _line("api key", _presence_text(data["api_key"])),
    ]
    return "\n".join(lines)


def _render_set_secret(data):
    return "\n".join(
        [
            "Pony config — Secret stored",
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
    connectivity = data["api_check"]
    storage = data["storage"]
    security = data["security"]
    security_executables = security["trusted_executables"]
    memory = data.get("memory", {"status": "unknown", "issues": []})
    lines = [
        "Pony doctor — CLI health check",
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
        _line("resolution", config["resolution_status"]),
        _line("resolution source", config["resolution_source"] or "-"),
        _line("resolved provider", _value_with_source(config["resolved_provider"])),
        _line("protocol", _protocol_with_resolution(config)),
        _line("api variant", _value_with_source(config["api_variant"])),
        _line("model", _value_with_source(config["model"])),
        _line("api url", _value_with_source(config["base_url"])),
        _line("auth mode", _value_with_source(config["auth_mode"])),
        "",
        "Credentials",
        _line("api key", _presence_text(credentials["api_key"])),
        _line("status", credentials["status"]),
        "",
        "Storage",
        _line("sessions", storage["sessions"]),
        _line("runs", storage["runs"]),
        "",
        "Memory",
        _line("check", memory.get("check_id", "memory")),
        _line("status", memory.get("status", "unknown")),
        _line("reason", memory.get("reason_code", "memory_diagnostics_incomplete")),
        _line("remediation", memory.get("remediation", "") or "-"),
        _line("issues", len(memory.get("issues", []))),
        *(
            _line(
                "issue",
                f"{item.get('path', '')!r} {item.get('reason_code', 'unknown')} "
                f"count={item.get('count', 0)} limit={item.get('limit', 0)}",
            )
            for item in memory.get("issues", [])
        ),
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
        "",
        "API check",
        _line("status", connectivity.get("status", "-")),
        _line("reason", connectivity.get("reason_code", "-") or "-"),
        _line("stage", connectivity.get("stage", "-") or "-"),
        _line("model calls", connectivity.get("model_calls", 0)),
    ]
    if connectivity.get("status") == "ok":
        lines.extend(
            [
                _line("detected provider", connectivity["detected_provider"]),
                _line("protocol", connectivity["protocol"]),
                _line("native tool", connectivity["native_tools"]),
                _line("tool continuation", connectivity["tool_continuation"]),
                _line(
                    "usage",
                    "unavailable"
                    if connectivity["usage_status"] == "degraded"
                    else connectivity["usage_status"],
                ),
                _line("persist with", connectivity["persist_with"]),
            ]
        )
    if connectivity.get("http_status") is not None:
        lines.append(_line("http", connectivity["http_status"]))
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
        "Pony status — Local harness state",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("cwd", data["workspace"]["cwd"]),
        _line("branch", data["workspace"]["branch"]),
        _line("git status", data["workspace"]["status"]),
        "",
        "Model",
        _line("provider", _value_with_source(data["model"]["provider"])),
        _line("resolution", data["model"]["resolution_status"]),
        _line("resolution source", data["model"]["resolution_source"] or "-"),
        _line(
            "resolved provider",
            _value_with_source(data["model"]["resolved_provider"]),
        ),
        _line("protocol", _protocol_with_resolution(data["model"])),
        _line("api variant", _value_with_source(data["model"]["api_variant"])),
        _line("model", _value_with_source(data["model"]["model"])),
        _line("api url", _value_with_source(data["model"]["base_url"])),
        _line("auth mode", _value_with_source(data["model"]["auth_mode"])),
        _line("api key", _presence_text(data["model"]["api_key"])),
        "",
        "Storage",
        _line("sessions", _ok_missing(data["storage"]["sessions"])),
        _line("runs", _ok_missing(data["storage"]["runs"])),
        "",
        "Latest",
        _line("session id", data["latest"]["session_id"] or "-"),
        _line("run id", data["latest"]["run_id"] or "-"),
    ]
    return "\n".join(lines)
