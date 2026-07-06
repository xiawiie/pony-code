"""Recovery command handlers for Pico's explicit CLI surface."""

import json
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_output import print_result
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager
from .workspace import WorkspaceContext  # noqa: F401


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
    if sub == "prune":
        prune_options = _parse_prune_args(rest)
        try:
            result = store.prune(
                dry_run=not prune_options["apply"],
                older_than=prune_options["older_than"],
            )
        except ValueError as exc:
            raise CliError(
                code="usage",
                message=str(exc),
                exit_code=CLI_EXIT_USAGE,
            ) from exc
        return print_result("checkpoints_prune", result, args, _render_json_body)
    raise CliError(
        code="usage",
        message="usage: pico-cli checkpoints {list | show <id> | preview-restore <id> | restore <id> [--apply] | prune [--older-than <duration>] [--apply]}",
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


def _render_checkpoints_list(records):
    lines = []
    for record in records:
        lines.append(f"{record['checkpoint_id']}\t{record['checkpoint_type']}\t{record.get('created_at', '')}")
    return "\n".join(lines)


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
        reason = str(entry.get("recovery_note", "") or entry.get("reason", "") or entry.get("change_kind", "") or "-")
        observed = str(entry.get("observed_current_hash", "") or "")
        expected = str(entry.get("expected_current_hash", "") or "")
        details = reason
        if observed:
            details += f" observed={observed[:12]}"
        if expected and decision == "conflict":
            details += f" expected={expected[:12]}"
        lines.append(f"{decision:<8}  {path:<32}  {details}")
    return "\n".join(lines)


def _render_runs_list(runs):
    return "\n".join(run["run_id"] for run in runs)


def _render_sessions_list(sessions):
    return "\n".join(session["session_id"] for session in sessions)


def _render_runs_show(data):
    sections = []
    for artifact in data["artifacts"]:
        sections.append(f"--- {artifact['name']} ---\n{artifact['content']}")
    return "\n".join(sections)


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


def _load_run_artifacts(run_dir, run_id):
    artifacts = []
    for name in ("task_state.json", "report.json", "trace.jsonl"):
        path = run_dir / name
        if path.exists():
            artifacts.append({"name": name, "content": path.read_text(encoding="utf-8")})
    return {"run_id": run_id, "artifacts": artifacts}


def _session_files(sessions_root):
    if not sessions_root.exists():
        return []
    return [
        path
        for path in sorted(sessions_root.glob("*.json"))
        if path.is_file()
    ]


def _is_restore_args(args):
    return len(args) == 1 or (len(args) == 2 and args[1] == "--apply")


def _parse_prune_args(args):
    options = {"apply": False, "older_than": None}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--apply":
            options["apply"] = True
            index += 1
            continue
        if token == "--older-than":
            if index + 1 >= len(args):
                raise _prune_usage_error()
            options["older_than"] = args[index + 1]
            index += 2
            continue
        if token.startswith("--older-than="):
            options["older_than"] = token.split("=", 1)[1]
            index += 1
            continue
        raise _prune_usage_error()
    return options


def _prune_usage_error():
    return CliError(
        code="usage",
        message="usage: pico-cli checkpoints prune [--older-than <duration>] [--apply]",
        exit_code=CLI_EXIT_USAGE,
    )


def _render_json_body(data):
    return json.dumps(data, indent=2, sort_keys=True)
