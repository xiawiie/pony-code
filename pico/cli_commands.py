"""Command handlers for Pico's explicit CLI Surface."""

from copy import copy
import getpass
from pathlib import Path
import sys

from . import security as securitylib
from .cli_errors import CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .cli_diagnostics import _line
from .cli_output import build_inspection_redactor, print_result
from .cli_session import handle_session_command
from .config import (
    API_KEY_ENV_NAME,
    API_URL_ENV_NAME,
    AUTH_MODE,
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    PROTOCOL_FAMILY,
    project_env_metadata,
    read_project_env,
    read_project_env_with_status,
    validate_api_url,
    write_project_env_assignments,
)
from .sandbox_session import source_mutation_authority
from .workspace import WorkspaceContext


ROOT_HELP = """pico — Local coding agent for repository-grounded engineering work.

USAGE:
    pico <command> [subcommand] [options]
    pico run <prompt...>

EXAMPLES:
    pico init
    pico run "inspect the failing tests"
    pico config set-secret PICO_DEEPSEEK_API_KEY
    pico --approval ask run "run the requested shell command"
    pico doctor
    pico runs summary latest
    pico checkpoints show <checkpoint-id>
    pico checkpoints pending
    pico checkpoints resolve-pending <id> [--apply]
    pico migrate status

Available Commands:
  run          Run one prompt and exit
  repl         Start interactive REPL
  status       Show local workspace state
  doctor       Check config, storage, auth, and sandbox readiness
  sandbox      Inspect and manage Docker Sandbox sessions and image readiness
  init         Configure the DeepSeek API URL and key
  config       Configuration inspection and set-secret input
  runs         Run artifact inspection
  sessions     Session inspection
  session      Inspect, compact, branch, rewind, label, or clone a Session Tree
  checkpoints  Checkpoint recovery, pending review, and resolution
  migrate      Inspect and apply explicit artifact migrations
  memory       Inspect and search memory files
  help         Help about any command

Flags:
  -h, --help       help for pico
      --format     output format for inspection commands: text or json
      --quiet      suppress non-essential human output
      --sandbox    run/repl in local Docker Sandbox (macOS arm64 only)

Security:
    Host mode provides no OS sandbox. In Sandbox mode all model-visible file tools use filtered
    staging; Source Apply requires separate review and authorization.
"""


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_session(tokens, root, args):
    """``pico session ...`` tree inspection and explicit session operations."""
    sessions_root = Path(root) / ".pico" / "sessions"

    def build_resumed_agent(session_id):
        from .cli import build_agent

        runtime_args = copy(args)
        runtime_args.resume = session_id
        return build_agent(runtime_args)

    return handle_session_command(
        list(tokens),
        sessions_root=sessions_root,
        redactor=build_inspection_redactor(root, args),
        agent_factory=build_resumed_agent,
    )


def handle_init(tokens, cwd, args):
    if tokens:
        raise _init_usage_error()
    if getattr(args, "no_input", False):
        raise CliError(
            code="usage",
            message="pico init requires interactive input",
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
        current_url = validate_api_url(
            existing.get(API_URL_ENV_NAME) or DEFAULT_API_URL
        )
        print(f"API URL [{current_url}]: ", end="", file=sys.stderr, flush=True)
        entered_url = input()
        api_url = validate_api_url(entered_url.strip() or current_url)
        existing_key = existing.get(API_KEY_ENV_NAME, "")
        key_prompt = (
            "API Key [press Enter to keep existing]: "
            if existing_key
            else "API Key: "
        )
        entered_key = getpass.getpass(key_prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise CliError(
            code="usage",
            message="interactive input unavailable",
            exit_code=CLI_EXIT_USAGE,
        ) from exc
    except ValueError as exc:
        raise CliError(
            code=str(exc),
            message=str(exc),
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    api_key = entered_key or existing_key
    if not api_key.strip() or any(
        character in api_key for character in ("\0", "\r", "\n")
    ):
        raise CliError(
            code="usage",
            message="API Key must be one non-empty line",
            exit_code=CLI_EXIT_USAGE,
        )
    assignments = {
        API_URL_ENV_NAME: api_url,
        API_KEY_ENV_NAME: api_key,
    }
    try:
        with source_mutation_authority(
            Path.home() / ".pico" / "sandboxes",
            root,
        ):
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
        "api_url": api_url,
        "model": DEFAULT_MODEL,
        "protocol": PROTOCOL_FAMILY,
        "auth_mode": AUTH_MODE,
        "updated": written["updated"],
        "added": written["added"],
        "unchanged": written["unchanged"],
        "api_key": {
            "present": True,
            "name": API_KEY_ENV_NAME,
        },
    }
    return print_result("config_init", data, args, _render_init)


def _render_init(data):
    api_key = data["api_key"]
    api_key_text = f"present ({api_key['name']})"
    changed = [*data["updated"], *data["added"]]
    lines = [
        "Pico init — Project .env configured",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        "",
        "Project environment",
        _line("env file", data["project_env"]["path"]),
        _line("env scope", data["project_env"]["scope"]),
        _line("env status", data["project_env"]["status"]),
        "",
        _line("api url", data["api_url"]),
        _line("model", data["model"]),
        _line("protocol", data["protocol"]),
        _line("auth mode", data["auth_mode"]),
        _line("api key", api_key_text),
        _line("updated", ", ".join(changed) if changed else "-"),
    ]
    return "\n".join(lines)


def _init_usage_error():
    return CliError(
        code="usage",
        message="usage: pico init",
        exit_code=CLI_EXIT_USAGE,
    )
