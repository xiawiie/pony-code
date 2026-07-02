"""Command handlers for Pico's explicit CLI Surface."""

import getpass
import json
import sys
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import collect_config, collect_doctor, collect_status
from .cli_output import format_json, success_envelope
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
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager
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
  help         Help about any command

Flags:
  -h, --help       help for pico-cli
      --format     output format for inspection commands: text or json
      --quiet      suppress non-essential human output

Compatibility:
    pico-cli "prompt"      Run a one-shot prompt
    pico                   Legacy entry point; may conflict with /usr/bin/pico
"""


def print_result(kind, data, args, text_renderer):
    if getattr(args, "format", "text") == "json":
        print(format_json(success_envelope(kind, data)), end="")
        return 0

    text = text_renderer(data)
    if text and not getattr(args, "quiet", False):
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_checkpoints(root, tokens, args):
    store = CheckpointStore(root)
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        records = store.list_checkpoint_records()
        return print_result("checkpoints_list", records, args, _render_checkpoints_list)
    if sub == "show" and len(rest) == 1:
        checkpoint_id = _resolve_checkpoint_id(store, rest[0])
        record = _load_checkpoint_record(store, checkpoint_id)
        return print_result("checkpoints_show", record, args, _render_json_body)
    if sub == "preview-restore" and len(rest) == 1:
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        checkpoint_id = _resolve_checkpoint_id(store, rest[0])
        plan = _preview_restore(manager, checkpoint_id)
        return print_result("checkpoints_preview_restore", plan, args, _render_restore_plan)
    if sub == "restore" and _is_restore_args(rest):
        checkpoint_id = _resolve_checkpoint_id(store, rest[0])
        apply_flag = "--apply" in rest[1:]
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        if not apply_flag:
            plan = _preview_restore(manager, checkpoint_id)
            return print_result("checkpoints_preview_restore", plan, args, _render_restore_plan)
        result = _apply_restore(manager, checkpoint_id)
        return print_result("checkpoints_restore", result, args, _render_json_body)
    if sub == "prune" and _is_apply_only_args(rest):
        apply_flag = "--apply" in rest
        result = store.prune(dry_run=not apply_flag)
        return print_result("checkpoints_prune", result, args, _render_json_body)
    raise CliError(
        code="usage",
        message="usage: pico-cli checkpoints {list | show <id> | preview-restore <id> | restore <id> [--apply] | prune [--apply]}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_runs(root, tokens, args):
    runs_root = Path(root) / ".pico" / "runs"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        data = []
        if runs_root.exists():
            data = [{"run_id": entry.name} for entry in sorted(runs_root.iterdir()) if entry.is_dir()]
        return print_result("runs_list", data, args, _render_runs_list)
    if sub == "show" and len(rest) == 1:
        run_dir = runs_root / rest[0]
        if not run_dir.exists():
            raise CliError(
                code="run_not_found",
                message=f"unknown run: {rest[0]}",
                hint="Run `pico-cli runs list`.",
                exit_code=CLI_EXIT_USAGE,
            )
        data = _load_run_artifacts(run_dir, rest[0])
        return print_result("runs_show", data, args, _render_runs_show)
    raise CliError(
        code="usage",
        message="usage: pico-cli runs {list | show <run_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


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


def handle_sessions(root, tokens, args):
    sessions_root = Path(root) / ".pico" / "sessions"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        data = [{"session_id": path.stem} for path in _session_files(sessions_root)]
        return print_result("sessions_list", data, args, _render_sessions_list)
    if sub == "show" and len(rest) == 1:
        session_id = rest[0]
        session_paths = {path.stem: path for path in _session_files(sessions_root)}
        path = session_paths.get(session_id)
        if path is None:
            raise CliError(
                code="session_not_found",
                message=f"unknown session: {session_id}",
                hint="Run `pico-cli sessions list`.",
                exit_code=CLI_EXIT_USAGE,
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return print_result("sessions_show", data, args, _render_json_body)
    raise CliError(
        code="usage",
        message="usage: pico-cli sessions {list | show <session_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


def run_agent_once(agent, prompt_tokens):
    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        return 0
    print()
    try:
        print(agent.ask(prompt))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def run_repl(agent):
    while True:
        try:
            user_input = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            from .cli import HELP_DETAILS

            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)


def _render_checkpoints_list(records):
    lines = []
    for record in records:
        lines.append(f"{record['checkpoint_id']}\t{record['checkpoint_type']}\t{record.get('created_at', '')}")
    return "\n".join(lines)


def _is_restore_args(args):
    return len(args) == 1 or (len(args) == 2 and args[1] == "--apply")


def _is_apply_only_args(args):
    return not args or args == ["--apply"]


def _render_json_body(data):
    return json.dumps(data, indent=2, sort_keys=True)


def _render_restore_plan(plan):
    entries = list(plan.get("entries", []) or [])
    count = len(entries)
    noun = "entry" if count == 1 else "entries"
    lines = [
        f"Restore plan {plan.get('checkpoint_id', '-')} ({count} {noun})",
        "",
        "decision  path                              reason",
    ]
    for entry in entries:
        decision = str(entry.get("decision", "-") or "-")
        path = str(entry.get("path", "-") or "-")
        reason = str(entry.get("reason", "") or entry.get("change_kind", "") or "-")
        observed = str(entry.get("observed_current_hash", "") or "")
        expected = str(entry.get("expected_current_hash", "") or "")
        details = reason
        if observed:
            details += f" observed={observed[:12]}"
        if expected and decision == "conflict":
            details += f" expected={expected[:12]}"
        lines.append(f"{decision:<8}  {path:<32}  {details}")
    return "\n".join(lines)


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
    return "\n".join(lines)


def _render_runs_list(runs):
    return "\n".join(run["run_id"] for run in runs)


def _render_sessions_list(sessions):
    return "\n".join(session["session_id"] for session in sessions)


def _session_files(sessions_root):
    if not sessions_root.exists():
        return []
    return [
        path
        for path in sorted(sessions_root.glob("*.json"))
        if path.is_file()
    ]


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


def _render_runs_show(data):
    sections = []
    for artifact in data["artifacts"]:
        sections.append(f"--- {artifact['name']} ---\n{artifact['content']}")
    return "\n".join(sections)


def _load_run_artifacts(run_dir, run_id):
    artifacts = []
    for name in ("task_state.json", "report.json", "trace.jsonl"):
        path = run_dir / name
        if path.exists():
            artifacts.append({"name": name, "content": path.read_text(encoding="utf-8")})
    return {"run_id": run_id, "artifacts": artifacts}


def _resolve_checkpoint_id(store, value):
    checkpoint_id = str(value or "").strip()
    if not checkpoint_id:
        raise CliError(
            code="checkpoint_not_found",
            message="unknown checkpoint: ",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        )

    records = store.list_checkpoint_records()
    ids = [str(record.get("checkpoint_id", "")) for record in records if str(record.get("checkpoint_id", ""))]
    if checkpoint_id in ids:
        return checkpoint_id

    matches = [item for item in ids if item.startswith(checkpoint_id)]
    if len(matches) == 1 and len(checkpoint_id) >= 6:
        return matches[0]
    if len(matches) > 1:
        raise CliError(
            code="checkpoint_prefix_ambiguous",
            message=f"ambiguous checkpoint prefix: {checkpoint_id}",
            hint="Use a longer checkpoint id prefix.",
            exit_code=CLI_EXIT_USAGE,
            details={"candidates": matches},
        )
    raise CliError(
        code="checkpoint_not_found",
        message=f"unknown checkpoint: {checkpoint_id}",
        hint="Run `pico-cli checkpoints list`.",
        exit_code=CLI_EXIT_USAGE,
    )


def _load_checkpoint_record(store, checkpoint_id):
    try:
        return store.load_checkpoint_record(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _preview_restore(manager, checkpoint_id):
    try:
        return manager.preview_restore(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _apply_restore(manager, checkpoint_id):
    try:
        return manager.apply_restore(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


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
