"""Command handlers for Pico's explicit CLI Surface."""

import getpass
import sys
from pathlib import Path

from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import _line
from .cli_diagnostics import handle_config, handle_doctor, handle_status  # noqa: F401
from .cli_start import run_agent_once, run_repl  # noqa: F401
from .cli_memory import handle_memory  # noqa: F401
from .cli_output import print_result
from .cli_recovery import handle_checkpoints, handle_runs, handle_sessions  # noqa: F401
from .config import _parse_env_line
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
    pico-cli doctor
    pico-cli checkpoints preview-restore <checkpoint-id>

Available Commands:
  run          Run one prompt and exit
  repl         Start interactive REPL
  status       Show local workspace state
  doctor       Check config, storage, auth, and connectivity
  init         Create or update project .env provider config
  config       Configuration inspection
  runs         Run artifact inspection
  sessions     Session inspection
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


def handle_init(tokens, cwd, args):
    options = _parse_init_tokens(tokens)
    workspace = WorkspaceContext.build(cwd)
    env_path = Path(workspace.repo_root) / ".env"
    existing = _read_env_assignments(env_path)
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
        assignments[base_url_name] = (
            options["base_url"]
            or getattr(args, "base_url", None)
            or _host_override(args, provider)
            or existing.get(base_url_name)
            or DEFAULT_BASE_URLS.get(provider, "")
        )

    api_key_value = ""
    if api_key_name:
        api_key_value = options["api_key"]
        if api_key_value is None:
            api_key_value = existing.get(api_key_name)
        if api_key_value is None:
            api_key_value = _prompt_api_key(provider, args)
        assignments[api_key_name] = api_key_value or ""

    written = _write_env_assignments(env_path, assignments)
    data = {
        "env_path": str(env_path),
        "provider": provider,
        "updated": written["updated"],
        "added": written["added"],
        "unchanged": written["unchanged"],
        "api_key": {
            "present": bool(api_key_value),
            "name": api_key_name,
        },
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
    return "\n".join(lines)


def _parse_init_tokens(tokens):
    options = {
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key": None,
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--provider", "--model", "--base-url", "--api-key"}:
            if index + 1 >= len(tokens):
                raise _init_usage_error()
            key = token[2:].replace("-", "_")
            options[key] = tokens[index + 1]
            index += 2
            continue
        for flag in ("--provider=", "--model=", "--base-url=", "--api-key="):
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
        message="usage: pico-cli init [--provider <name>] [--model <name>] [--base-url <url>] [--api-key <key>]",
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


def _prompt_api_key(provider, args):
    if getattr(args, "no_input", False) or not sys.stdin.isatty():
        return ""
    try:
        return getpass.getpass(f"{provider} API key (leave blank to fill later): ")
    except (EOFError, KeyboardInterrupt):
        return ""


def _read_env_assignments(env_path):
    if not env_path.exists():
        return {}
    assignments = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        assignments[name] = value
    return assignments


def _write_env_assignments(env_path, assignments):
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(assignments)
    rendered = []
    updated = []
    unchanged = []
    for line in existing_lines:
        parsed = _parse_env_line(line)
        if parsed is None:
            rendered.append(line)
            continue
        name, old_value = parsed
        if name not in remaining:
            rendered.append(line)
            continue
        value = remaining.pop(name)
        rendered.append(_format_env_assignment(name, value))
        if old_value == value:
            unchanged.append(name)
        else:
            updated.append(name)

    added = list(remaining)
    if remaining:
        if rendered and rendered[-1].strip():
            rendered.append("")
        for name, value in remaining.items():
            rendered.append(_format_env_assignment(name, value))

    env_path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8")
    return {"updated": updated, "added": added, "unchanged": unchanged}


def _format_env_assignment(name, value):
    text = str(value or "")
    if "\n" in text or "\r" in text:
        raise CliError(
            code="usage",
            message=f"{name} cannot contain newlines",
            exit_code=CLI_EXIT_USAGE,
        )
    return f"{name}={text}"
