"""Command handlers for Pico's explicit CLI Surface."""

import json
import sys
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import collect_config, collect_doctor, collect_status
from .cli_output import format_json, success_envelope
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager


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
        record = _load_checkpoint_record(store, rest[0])
        return print_result("checkpoints_show", record, args, _render_json_body)
    if sub == "preview-restore" and len(rest) == 1:
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        plan = _preview_restore(manager, rest[0])
        return print_result("checkpoints_preview_restore", plan, args, _render_json_body)
    if sub == "restore" and _is_restore_args(rest):
        checkpoint_id = rest[0]
        apply_flag = "--apply" in rest[1:]
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        if not apply_flag:
            plan = _preview_restore(manager, checkpoint_id)
            return print_result("checkpoints_preview_restore", plan, args, _render_json_body)
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
