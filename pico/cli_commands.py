"""Command handlers for Pico's explicit CLI Surface."""

from pathlib import Path
import re

from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import _line
from .cli_diagnostics import handle_config, handle_doctor, handle_status  # noqa: F401
from .cli_start import run_agent_once, run_repl  # noqa: F401
from .cli_memory import handle_memory  # noqa: F401
from .cli_output import print_result
from .cli_recovery import handle_checkpoints, handle_runs, handle_sessions  # noqa: F401
from .cli_session import handle_session_command
from .config import ENV_KEY_PATTERN, _parse_env_line
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
  init         Create or update project model config
  config       Configuration inspection
  runs         Run artifact inspection
  sessions     Session inspection
  session      Session drift inspector (dual-write check)
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

_TOML_TABLE_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_session(tokens, root, args):
    """`pico-cli session {inspect} <session_id>`.

    Task A5: static, read-only dual-write drift inspector. Bridges the
    ``session["history"]`` (legacy) / ``session["messages"]`` (v2)
    invariant without a runtime assertion.
    """
    sessions_root = Path(root) / ".pico" / "sessions"
    return handle_session_command(list(tokens), sessions_root=sessions_root)


def handle_init(tokens, cwd, args):
    options = _parse_init_tokens(tokens)
    _validate_init_options(options)
    workspace = WorkspaceContext.build(cwd)
    config_path = Path(workspace.repo_root) / "pico.toml"
    env_path = Path(workspace.repo_root) / ".env"
    _write_model_toml(config_path, _render_model_toml(options))
    written = {"updated": [], "added": [], "unchanged": []}
    if options["api_key_env"] and options["api_key"] is not None:
        written = _write_env_assignments(env_path, {options["api_key_env"]: options["api_key"]})
    data = {
        "config_path": str(config_path),
        "env_path": str(env_path),
        "model": options["model"],
        "base_url": options["base_url"],
        "api": options["api"],
        "updated": written["updated"],
        "added": written["added"],
        "unchanged": written["unchanged"],
        "api_key": {
            "present": bool(options["api_key_env"] and options["api_key"] is not None),
            "name": options["api_key_env"],
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
        "Pico init — Project model configured",
        "",
        _line("config file", data["config_path"]),
        _line("env file", data["env_path"]),
        _line("model", data["model"]),
        _line("base url", data["base_url"]),
        _line("api", data["api"] or "-"),
        _line("api key", api_key_text),
        _line("updated", ", ".join(changed) if changed else "-"),
    ]
    return "\n".join(lines)


def _parse_init_tokens(tokens):
    options = {
        "model": None,
        "base_url": None,
        "api_key_env": None,
        "api_key": None,
        "api": None,
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--model", "--base-url", "--api-key-env", "--api-key", "--api"}:
            if index + 1 >= len(tokens):
                raise _init_usage_error()
            key = token[2:].replace("-", "_")
            options[key] = tokens[index + 1]
            index += 2
            continue
        for flag in ("--model=", "--base-url=", "--api-key-env=", "--api-key=", "--api="):
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
        message="usage: pico-cli init --model <name> --base-url <url> [--api-key-env <env>] [--api-key <key>] [--api <adapter>]",
        exit_code=CLI_EXIT_USAGE,
    )


def _validate_init_options(options):
    if not options["model"] or not options["base_url"]:
        raise _init_usage_error()
    api_key_env = options["api_key_env"]
    api_key = options["api_key"]
    if api_key_env is not None and not ENV_KEY_PATTERN.match(api_key_env):
        raise CliError(
            code="usage",
            message="model api_key_env must be a valid environment variable name",
            exit_code=CLI_EXIT_USAGE,
        )
    if api_key is not None and not api_key_env:
        raise CliError(
            code="usage",
            message="--api-key requires --api-key-env",
            exit_code=CLI_EXIT_USAGE,
        )
    if api_key == "":
        raise CliError(
            code="usage",
            message="--api-key cannot be empty",
            exit_code=CLI_EXIT_USAGE,
        )


def _render_model_toml(options):
    lines = ["[model]"]
    lines.append(f'name = "{_toml_escape(options["model"])}"')
    lines.append(f'base_url = "{_toml_escape(options["base_url"])}"')
    if options["api_key_env"]:
        lines.append(f'api_key_env = "{_toml_escape(options["api_key_env"])}"')
    if options["api"]:
        lines.append(f'api = "{_toml_escape(options["api"])}"')
    return "\n".join(lines) + "\n"


def _write_model_toml(config_path, model_toml):
    if not config_path.exists():
        config_path.write_text(model_toml, encoding="utf-8")
        return
    text = config_path.read_text(encoding="utf-8")
    start, end = _model_table_range(text)
    if start is None:
        separator = ""
        if text:
            separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        config_path.write_text(f"{text}{separator}{model_toml}", encoding="utf-8")
        return
    config_path.write_text(f"{text[:start]}{model_toml}{text[end:]}", encoding="utf-8")


def _model_table_range(text):
    offset = 0
    start = None
    for line in text.splitlines(keepends=True):
        table = _toml_table_name(line)
        if table is not None:
            if start is not None:
                return start, offset
            if table == "model":
                start = offset
        offset += len(line)
    if start is None:
        return None, None
    return start, len(text)


def _toml_table_name(line):
    match = _TOML_TABLE_RE.match(line.strip())
    if not match:
        return None
    return match.group(1).strip()


def _toml_escape(value):
    text = str(value or "")
    if "\n" in text or "\r" in text:
        raise CliError(
            code="usage",
            message="model config values cannot contain newlines",
            exit_code=CLI_EXIT_USAGE,
        )
    return text.replace("\\", "\\\\").replace('"', '\\"')


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
