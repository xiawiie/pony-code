"""Command handlers for Pico's explicit CLI Surface."""

from pathlib import Path

from .cli_errors import CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .cli_diagnostics import _line
from .cli_diagnostics import handle_config, handle_doctor, handle_status  # noqa: F401
from .cli_start import run_agent_once, run_repl  # noqa: F401
from .cli_memory import handle_memory  # noqa: F401
from .cli_output import print_result
from .cli_recovery import handle_checkpoints, handle_runs, handle_sessions  # noqa: F401
from .cli_session import handle_session_command
from .config import (
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
    DEFAULT_PROVIDER,
    MODEL_ENV_NAMES,
    PROVIDER_CHOICES,
)
from .workspace import WorkspaceContext


ROOT_HELP = """pico-cli — Local coding agent for repository-grounded engineering work.

USAGE:
    pico-cli <command> [subcommand] [options]
    pico-cli run <prompt...>

EXAMPLES:
    pico-cli run "inspect the failing tests"
    pico-cli config set-secret PICO_DEEPSEEK_API_KEY
    pico-cli doctor
    pico-cli checkpoints preview-restore <checkpoint-id>

Available Commands:
  run          Run one prompt and exit
  repl         Start interactive REPL
  status       Show local workspace state
  doctor       Check config, storage, auth, and connectivity
  init         Create or update project .env provider config
  config       Configuration inspection and secret input
  runs         Run artifact inspection
  sessions     Session inspection
  session      Session v3 invariant inspector
  checkpoints  Checkpoint recovery inspection
  memory       Memory files inspection & migration
  help         Help about any command

Flags:
  -h, --help       help for pico-cli
      --format     output format for inspection commands: text or json
      --quiet      suppress non-essential human output

Compatibility:
    pico-cli "prompt"      Run a one-shot prompt
    pico                   Legacy entry point; may conflict with /usr/bin/pico
"""


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_session(tokens, root, args):
    """`pico-cli session {inspect} <session_id>`.

    Static, read-only inspector for the canonical v3 message invariant.
    """
    sessions_root = Path(root) / ".pico" / "sessions"
    return handle_session_command(list(tokens), sessions_root=sessions_root)


def handle_init(tokens, cwd, args):
    options = _parse_init_tokens(tokens)
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    env_path = project_env_path(root)
    try:
        existing = read_project_env(root)
    except (OSError, ValueError) as exc:
        raise CliError(
            code="config",
            message="project environment read failed",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    provider = options["provider"] or getattr(args, "provider", None) or existing.get("PICO_PROVIDER") or DEFAULT_PROVIDER
    if provider not in PROVIDER_CHOICES:
        raise CliError(
            code="usage",
            message=f"unknown provider: {provider}",
            hint=f"Expected one of: {', '.join(PROVIDER_CHOICES)}.",
            exit_code=CLI_EXIT_USAGE,
        )

    assignments = {"PICO_PROVIDER": provider}
    model_name = _primary_env_name(MODEL_ENV_NAMES, provider)
    base_url_name = _primary_env_name(BASE_URL_ENV_NAMES, provider)
    api_key_name = _primary_env_name(API_KEY_ENV_NAMES, provider)

    if model_name:
        assignments[model_name] = (
            options["model"]
            or getattr(args, "model", None)
            or existing.get(model_name)
            or DEFAULT_MODELS.get(provider, "")
        )
    if base_url_name:
        base_url = (
            options["base_url"]
            or getattr(args, "base_url", None)
            or _host_override(args, provider)
            or existing.get(base_url_name)
            or DEFAULT_BASE_URLS.get(provider, "")
        )
        assignments[base_url_name] = validate_provider_base_url(base_url)

    api_key_present = bool(api_key_name and existing.get(api_key_name))
    try:
        written = write_project_env_assignments(root, assignments)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="config",
            message="project environment update failed",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    data = {
        "env_path": env_path.name,
        "provider": provider,
        "updated": written["updated"],
        "added": written["added"],
        "unchanged": written["unchanged"],
        "api_key": {
            "present": api_key_present,
            "name": api_key_name,
        },
        "set_secret_command": (
            f"pico-cli config set-secret {api_key_name}"
            if api_key_name and not api_key_present
            else ""
        ),
    }
    return print_result("config_init", data, args, _render_init)


def _render_init(data):
    api_key = data["api_key"]
    if api_key["name"]:
        api_key_text = f"{'present' if api_key['present'] else 'missing'} ({api_key['name']})"
    else:
        api_key_text = "not required"
    changed = [*data["updated"], *data["added"]]
    lines = [
        "Pico init — Project .env configured",
        "",
        _line("env file", data["env_path"]),
        _line("provider", data["provider"]),
        _line("api key", api_key_text),
        _line("updated", ", ".join(changed) if changed else "-"),
    ]
    if data.get("set_secret_command"):
        lines.extend(("", _line("next", data["set_secret_command"])))
    return "\n".join(lines)


def _parse_init_tokens(tokens):
    options = {
        "provider": None,
        "model": None,
        "base_url": None,
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--provider", "--model", "--base-url"}:
            if index + 1 >= len(tokens):
                raise _init_usage_error()
            key = token[2:].replace("-", "_")
            options[key] = tokens[index + 1]
            index += 2
            continue
        for flag in ("--provider=", "--model=", "--base-url="):
            if token.startswith(flag):
                key = flag[2:-1].replace("-", "_")
                options[key] = token[len(flag):]
                break
        else:
            raise _init_usage_error()
        index += 1
    return options


def _init_usage_error():
    return CliError(
        code="usage",
        message="usage: pico-cli init [--provider <name>] [--model <name>] [--base-url <url>]",
        exit_code=CLI_EXIT_USAGE,
    )


def _primary_env_name(mapping, provider):
    names = mapping.get(provider, ())
    return names[0] if names else ""


def _host_override(args, provider):
    if provider != "ollama":
        return None
    host = getattr(args, "host", None)
    if host and host != DEFAULT_BASE_URLS.get("ollama"):
        return host
    return None
