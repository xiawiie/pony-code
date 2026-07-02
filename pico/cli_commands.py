"""Command handlers for Pico's explicit CLI Surface."""

import json
import sys
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import collect_config, collect_status
from .cli_output import format_json, success_envelope
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager


def print_result(kind, data, args, text_renderer):
    if getattr(args, "format", "text") == "json":
        print(format_json(success_envelope(kind, data)), end="")
        return 0

    text = text_renderer(data)
    if text:
        print(text, end="" if text.endswith("\n") else "\n")
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
        message="usage: pico checkpoints {list | show <id> | preview-restore <id> | restore <id> [--apply] | prune [--apply]}",
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
                hint="Run `pico runs list`.",
                exit_code=CLI_EXIT_USAGE,
            )
        data = _load_run_artifacts(run_dir, rest[0])
        return print_result("runs_show", data, args, _render_runs_show)
    raise CliError(
        code="usage",
        message="usage: pico runs {list | show <run_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_status(cwd, args):
    return print_result("status", collect_status(cwd, args), args, _render_status)


def handle_config(tokens, cwd, args):
    sub = tokens[0] if tokens else ""
    rest = tokens[1:]
    if sub == "show" and not rest:
        return print_result(
            "config_show",
            collect_config(cwd, args),
            args,
            _render_json_body,
        )
    raise CliError(
        code="usage",
        message="usage: pico config show",
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
                hint="Run `pico sessions list`.",
                exit_code=CLI_EXIT_USAGE,
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return print_result("sessions_show", data, args, _render_json_body)
    raise CliError(
        code="usage",
        message="usage: pico sessions {list | show <session_id>}",
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
        f"workspace: {data['workspace']['repo_root']}",
        f"provider: {data['provider']['provider']['value']}",
        f"model: {data['provider']['model']['value']}",
        "storage:",
        f"  sessions: {data['storage']['sessions']}",
        f"  runs: {data['storage']['runs']}",
        f"  checkpoints: {data['storage']['checkpoints']}",
        "latest:",
        f"  session_id: {data['latest']['session_id'] or '-'}",
        f"  run_id: {data['latest']['run_id'] or '-'}",
        f"  checkpoint_id: {data['latest']['checkpoint_id'] or '-'}",
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
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _preview_restore(manager, checkpoint_id):
    try:
        return manager.preview_restore(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _apply_restore(manager, checkpoint_id):
    try:
        return manager.apply_restore(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc
