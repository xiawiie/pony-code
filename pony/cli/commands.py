"""Command handlers for Pony's explicit CLI Surface."""

from copy import copy
import getpass
from pathlib import Path
import sys

from pony.security import redaction as securitylib
from pony.providers.probe import resolve_provider_client
from .errors import CLI_EXIT_CONFIG, CLI_EXIT_RUNTIME, CLI_EXIT_USAGE, CliError
from .diagnostics import _line
from .output import build_inspection_redactor, print_inspection_result, print_result
from .session import (
    handle_session_command,
    render_session_inspection,
    session_inspection_data,
)
from pony.config.model import (
    API_BASE_ENV_NAME,
    API_KEY_ENV_NAME,
    DEFAULT_PROVIDER,
    MODEL_ENV_NAME,
    PROVIDER_ENV_NAME,
    resolve_model_config,
    validate_api_base,
)
from pony.config.environment import (
    project_env_metadata,
    read_project_env,
    read_project_env_with_status,
    write_project_env_assignments,
)
from pony.state.session_store import SessionFormatError
from pony.workspace.context import WorkspaceContext


ROOT_HELP = """pony — Local coding agent for repository-grounded engineering work.

USAGE:
    pony [global options]
    pony <command> [subcommand] [options]
    pony run <prompt...>

EXAMPLES:
    pony
    pony init
    pony run "inspect the failing tests"
    pony --permission-mode plan run "inspect and plan the change"
    pony config set-secret PONY_API_KEY
    pony --permission-mode manual run "run the requested shell command"
    pony doctor
    pony runs summary latest
    pony checkpoints show <checkpoint-id>
    pony checkpoints pending
    pony agents list
    pony agents merge <agent-id>
    pony migrate status

Available Commands:
  run          Run one prompt and exit
  repl         Start the interactive TUI (also the default for bare `pony`)
  status       Show local workspace state
  doctor       Check config, storage, and auth readiness
  init         Configure provider, API base, key, and model in .env
  config       Configuration inspection and set-secret input
  runs         Run artifact inspection
  sessions     Session inspection
  session      Inspect, compact, branch, rewind, label, or clone a Session Tree
  checkpoints  Read-only legacy checkpoint inspection
  migrate      Inspect and apply explicit artifact migrations
  memory       Inspect and search memory files
  agents       Inspect, merge, or clean up isolated worktree agents
  help         Help about any command

Flags:
  -h, --help       help for pony
      --version    show installed Pony version
      --format     output format for inspection commands: text or json
      --quiet      suppress non-essential human output
      --no-color   disable terminal colors
      --permission-mode  permission mode: acceptEdits, auto, bypassPermissions,
                         manual, dontAsk, or plan
      --allowed-tools    exact tool names to allow for this Session
      --disallowed-tools exact tool names to deny for this Session
      --allow-dangerously-skip-permissions
                         make bypassPermissions selectable for this process

Security:
    Pony runs tools in the trusted workspace and enforces permission, path, and secret checks.
"""


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_session(tokens, root, args):
    """``pony session ...`` tree inspection and explicit session operations."""
    sessions_root = Path(root) / ".pony" / "sessions"
    if tokens and tokens[0] == "inspect":
        if len(tokens) != 2:
            raise CliError(
                code="usage",
                message="usage: pony session inspect <session-id|latest>",
                exit_code=CLI_EXIT_USAGE,
            )
        try:
            data = session_inspection_data(tokens[1], sessions_root)
        except FileNotFoundError as exc:
            raise CliError(
                code="session_not_found",
                message="unknown session",
                hint="Run `pony sessions list`.",
                exit_code=CLI_EXIT_USAGE,
            ) from exc
        except (OSError, ValueError, SessionFormatError) as exc:
            code = getattr(exc, "code", "")
            raise CliError(
                code=code or "unsafe_artifact",
                message=str(exc) if code else "unsafe local artifact",
                exit_code=CLI_EXIT_RUNTIME,
            ) from exc
        return print_inspection_result(
            root,
            "session_inspect",
            data,
            args,
            render_session_inspection,
            redactor=build_inspection_redactor(root, args),
        )

    def build_resumed_agent(session_id):
        from . import build_agent

        runtime_args = copy(args)
        runtime_args.resume = session_id
        return build_agent(runtime_args)

    try:
        return handle_session_command(
            list(tokens),
            sessions_root=sessions_root,
            redactor=build_inspection_redactor(root, args),
            agent_factory=build_resumed_agent,
            raise_typed_errors=True,
        )
    except SessionFormatError as exc:
        code = getattr(exc, "code", "")
        raise CliError(
            code=code or "unsafe_artifact",
            message=str(exc) if code else "unsafe local artifact",
            exit_code=CLI_EXIT_RUNTIME,
        ) from exc


def handle_init(tokens, cwd, args):
    if tokens:
        raise _init_usage_error()
    if getattr(args, "no_input", False):
        raise CliError(
            code="usage",
            message="pony init requires interactive input",
            exit_code=CLI_EXIT_USAGE,
        )
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    try:
        existing = read_project_env(root)
    except (OSError, ValueError) as exc:
        raise CliError(
            code="config",
            message="project environment read failed",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    try:
        existing_base = existing.get(API_BASE_ENV_NAME, "")
        if existing_base:
            validate_api_base(existing_base)
        current_provider = str(
            existing.get(PROVIDER_ENV_NAME) or DEFAULT_PROVIDER
        ).strip().casefold()
        try:
            resolve_model_config(
                project_env={PROVIDER_ENV_NAME: current_provider},
                process_env={},
                required=False,
            )
        except ValueError:
            current_provider = DEFAULT_PROVIDER
        print(
            f"Provider [{current_provider}]: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        provider = input().strip().casefold() or current_provider
        defaults = resolve_model_config(
            project_env={PROVIDER_ENV_NAME: provider},
            process_env={},
            required=False,
        )
        same_provider = provider == current_provider

        current_base = (
            existing_base if same_provider else ""
        ) or defaults["base_url"]["value"]
        base_prompt = f"API Base [{current_base}]: " if current_base else "API Base: "
        print(base_prompt, end="", file=sys.stderr, flush=True)
        api_base = validate_api_base(input().strip() or current_base)

        defaults = resolve_model_config(
            project_env={
                PROVIDER_ENV_NAME: provider,
                API_BASE_ENV_NAME: api_base,
            },
            process_env={},
            required=False,
        )
        current_model = (
            existing.get(MODEL_ENV_NAME) if same_provider else ""
        ) or defaults["model"]["value"]
        model_prompt = f"Model [{current_model}]: " if current_model else "Model: "
        print(model_prompt, end="", file=sys.stderr, flush=True)
        model = input().strip() or current_model

        existing_key = existing.get(API_KEY_ENV_NAME, "")
        key_prompt = (
            "API Key [press Enter to keep existing]: "
            if existing_key
            else "API Key [optional for auth mode none]: "
        )
        entered_key = getpass.getpass(key_prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise CliError(
            code="usage",
            message="interactive input unavailable",
            exit_code=CLI_EXIT_USAGE,
        ) from exc
    except ValueError as exc:
        raise _init_config_error(exc) from exc
    api_key = entered_key or existing_key
    if any(character in api_key for character in ("\0", "\r", "\n")):
        raise CliError(
            code="usage",
            message="API Key must be one line",
            exit_code=CLI_EXIT_USAGE,
        )
    assignments = {
        PROVIDER_ENV_NAME: provider,
        API_BASE_ENV_NAME: api_base,
        MODEL_ENV_NAME: model,
        API_KEY_ENV_NAME: api_key,
    }
    try:
        resolved = resolve_model_config(
            project_env=assignments,
            process_env={},
            required=True,
        )
    except ValueError as exc:
        raise _init_config_error(exc) from exc
    probe_report = {
        "status": "not_run",
        "model_calls": 0,
        "usage_status": "not_checked",
    }
    if provider in {"auto", "openai"}:
        print("\nDetecting provider...", file=sys.stderr, flush=True)
        _client, resolved, probe_report = resolve_provider_client(
            resolved,
            timeout=args.request_timeout_seconds,
            verify_resolved=True,
        )
        assignments[PROVIDER_ENV_NAME] = resolved["resolved_provider"]["value"]
    try:
        written = write_project_env_assignments(root, assignments)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="config",
            message="project environment update failed",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    try:
        _, project_env = read_project_env_with_status(root, warn=False)
    except (OSError, RuntimeError, ValueError):
        project_env = project_env_metadata(root, "review_required")
    try:
        redactor = build_inspection_redactor(root, args)
    except (OSError, RuntimeError, ValueError):
        redactor = securitylib.redact_artifact
    workspace_info = redactor({"repo_root": str(root)})
    project_env = redactor(project_env)
    data = {
        "workspace": workspace_info,
        "project_env": project_env,
        "provider": assignments[PROVIDER_ENV_NAME],
        "api_base": resolved["base_url"]["value"],
        "model": resolved["model"]["value"],
        "api_variant": resolved["api_variant"]["value"],
        "protocol": resolved["protocol"]["value"],
        "auth_mode": resolved["auth_mode"]["value"],
        "detection": {
            "status": probe_report["status"],
            "native_tools": (
                "passed" if probe_report["status"] == "ok" else "not_checked"
            ),
            "tool_continuation": (
                "passed" if probe_report["status"] == "ok" else "not_checked"
            ),
            "usage": probe_report["usage_status"],
            "model_calls": probe_report["model_calls"],
        },
        "updated": written["updated"],
        "added": written["added"],
        "unchanged": written["unchanged"],
        "api_key": {
            "present": bool(api_key),
            "name": API_KEY_ENV_NAME,
        },
    }
    return print_result("config_init", data, args, _render_init)


def _render_init(data):
    api_key = data["api_key"]
    api_key_text = (
        f"present ({api_key['name']})"
        if api_key["present"]
        else f"not set ({api_key['name']})"
    )
    changed = [*data["updated"], *data["added"]]
    lines = [
        "Pony init — Project .env configured",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        "",
        "Project environment",
        _line("env file", data["project_env"]["path"]),
        _line("env scope", data["project_env"]["scope"]),
        _line("env status", data["project_env"]["status"]),
        "",
        _line("provider", data["provider"]),
        _line("model", data["model"]),
        _line("api base", data["api_base"]),
        _line("api variant", data["api_variant"]),
        _line("protocol", data["protocol"]),
        _line("auth mode", data["auth_mode"]),
        _line("api key", api_key_text),
        _line("updated", ", ".join(changed) if changed else "-"),
    ]
    detection = data["detection"]
    if detection["status"] == "ok":
        lines.extend(
            [
                "",
                "Provider verification",
                _line("detected", data["provider"]),
                _line("native tools", detection["native_tools"]),
                _line("tool continuation", detection["tool_continuation"]),
                _line(
                    "usage",
                    "unavailable"
                    if detection["usage"] == "degraded"
                    else detection["usage"],
                ),
            ]
        )
    return "\n".join(lines)


def _init_config_error(error):
    code = str(error)
    message = {
        "provider_endpoint_conflict": "Provider conflicts with API Base",
        "provider_invalid": "Invalid Provider value",
    }.get(code, code)
    return CliError(
        code=code,
        message=message,
        hint=(
            "Choose auto or a Provider matching the API Base."
            if code.startswith("provider_")
            else ""
        ),
        exit_code=CLI_EXIT_CONFIG,
    )


def _init_usage_error():
    return CliError(
        code="usage",
        message="usage: pony init",
        exit_code=CLI_EXIT_USAGE,
    )
