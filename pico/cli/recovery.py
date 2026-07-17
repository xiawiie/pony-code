"""Recovery command handlers for Pico's explicit CLI surface."""

import json
import hashlib
import os
from pathlib import Path
import re
import stat

from pico.state.checkpoint_store import CheckpointStore
from .errors import CLI_EXIT_RUNTIME, CLI_EXIT_USAGE, CliError
from .output import build_inspection_redactor, print_inspection_result
from .session import load_session_readonly
from pico.recovery.checkpoint_writer import RecoveryCheckpointWriter
from pico.recovery.manager import RecoveryManager, collect_recovery_review_items
from pico.sandbox.session import (
    read_source_apply_authority,
    source_apply_control_lock_path,
)
from pico.agent.observability import (
    RunArtifactError,
    load_run_summary,
    render_summary_text,
)
from pico.security.paths import require_regular_no_symlink
from pico.tools.change_recorder import ToolChangeRecorder
from pico.workspace.context import WorkspaceContext  # noqa: F401


_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def handle_checkpoints(root, tokens, args):
    redactor = build_inspection_redactor(root, args)
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    read_only = (
        sub in {"pending", "list", "show", "preview-restore"}
        or sub == "restore"
        and _is_restore_args(rest)
        and "--apply" not in rest[1:]
        or sub == "resolve-pending"
        and _is_resolve_pending_args(rest)
        and "--apply" not in rest[1:]
        or sub == "prune"
        and "--apply" not in rest
    )
    try:
        sandbox_parent = Path.home() / ".pico" / "sandboxes"
        store = CheckpointStore(
            root,
            redactor=redactor,
            source_apply_authority=lambda: read_source_apply_authority(
                sandbox_parent,
                root,
            ),
            source_apply_control_lock=source_apply_control_lock_path(
                sandbox_parent,
                root,
            ),
            read_only=read_only,
        )
    except (OSError, ValueError) as exc:
        raise _unsafe_artifact_error() from exc
    if sub == "pending" and not rest:
        data = collect_recovery_review_items(store, root)
        return print_inspection_result(
            root,
            "checkpoints_pending",
            data,
            args,
            _render_pending_reviews,
            redactor=redactor,
        )
    if sub == "resolve-pending" and _is_resolve_pending_args(rest):
        result = _resolve_pending_record(
            store,
            root,
            rest[0],
            apply_flag="--apply" in rest[1:],
        )
        return print_inspection_result(
            root,
            "checkpoints_resolve_pending",
            result,
            args,
            _render_json_body,
            redactor=redactor,
        )
    if sub == "list" and not rest:
        try:
            records = store.list_checkpoint_records(strict=True)
        except (OSError, ValueError) as exc:
            raise _unsafe_artifact_error() from exc
        return print_inspection_result(
            root,
            "checkpoints_list",
            records,
            args,
            _render_checkpoints_list,
            redactor=redactor,
        )
    if sub == "show" and len(rest) == 1:
        checkpoint_id = _resolve_checkpoint_id(store, rest[0], redactor=redactor)
        record = _load_checkpoint_record(store, checkpoint_id, redactor=redactor)
        return print_inspection_result(
            root,
            "checkpoints_show",
            record,
            args,
            _render_json_body,
            redactor=redactor,
        )
    if sub == "preview-restore" and len(rest) == 1:
        manager = RecoveryManager(
            store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root)
        )
        checkpoint_id = _resolve_checkpoint_id(store, rest[0], redactor=redactor)
        plan = _preview_restore(manager, checkpoint_id, redactor=redactor)
        return print_inspection_result(
            root,
            "checkpoints_preview_restore",
            plan,
            args,
            _render_restore_plan,
            redactor=redactor,
        )
    if sub == "restore" and _is_restore_args(rest):
        checkpoint_id = _resolve_checkpoint_id(store, rest[0], redactor=redactor)
        apply_flag = "--apply" in rest[1:]
        manager = RecoveryManager(
            store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root)
        )
        if not apply_flag:
            plan = _preview_restore(manager, checkpoint_id, redactor=redactor)
            return print_inspection_result(
                root,
                "checkpoints_preview_restore",
                plan,
                args,
                _render_restore_plan,
                redactor=redactor,
            )
        result = _apply_restore(manager, checkpoint_id, redactor=redactor)
        exit_code = print_inspection_result(
            root,
            "checkpoints_restore",
            result,
            args,
            _render_json_body,
            redactor=redactor,
        )
        return (
            CLI_EXIT_RUNTIME
            if result.get("status") in {"blocked", "failed", "partial"}
            else exit_code
        )
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
        return print_inspection_result(
            root,
            "checkpoints_prune",
            result,
            args,
            _render_json_body,
            redactor=redactor,
        )
    raise CliError(
        code="usage",
        message="usage: pico checkpoints {list | show <id> | pending | resolve-pending <id> [--apply] | preview-restore <id> | restore <id> [--apply] | prune [--older-than <duration>] [--apply]}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_runs(root, tokens, args):
    redactor = build_inspection_redactor(root, args)
    runs_root = Path(root) / ".pico" / "runs"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        checked_root = _inspection_directory(runs_root, allow_missing=True)
        data = (
            []
            if checked_root is None
            else [
            {"run_id": entry.name}
            for entry in sorted(checked_root.iterdir())
            if stat.S_ISDIR(entry.lstat().st_mode)
        ]
        )
        return print_inspection_result(
            root,
            "runs_list",
            data,
            args,
            _render_runs_list,
            redactor=redactor,
        )
    if sub == "summary" and len(rest) == 1:
        requested_id = rest[0]
        if requested_id != "latest":
            requested_id = _inspection_id(requested_id, kind="run")
        try:
            data = load_run_summary(runs_root, requested_id)
        except FileNotFoundError as exc:
            raise _not_found_error("run") from exc
        except RunArtifactError as exc:
            data = {
                "summary_status": exc.status,
                "requested_run_id": requested_id,
                "reason": str(exc),
            }
            if exc.status == "migration_required":
                data["migration_command"] = "pico migrate observability apply"
        return print_inspection_result(
            root,
            "runs_summary",
            data,
            args,
            _render_run_summary,
            redactor=redactor,
        )
    if sub == "show" and len(rest) == 1:
        run_id = _inspection_id(rest[0], kind="run")
        run_dir = _inspection_directory(runs_root / run_id, allow_missing=True)
        if run_dir is None:
            raise _not_found_error("run")
        data = _load_run_artifacts(run_dir, run_id, redactor)
        return print_inspection_result(
            root,
            "runs_show",
            data,
            args,
            _render_runs_show,
            redactor=redactor,
        )
    raise CliError(
        code="usage",
        message="usage: pico runs {list | show <run_id> | summary <run_id|latest>}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_sessions(root, tokens, args):
    redactor = build_inspection_redactor(root, args)
    sessions_root = Path(root) / ".pico" / "sessions"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        data = [{"session_id": path.stem} for path in _session_files(sessions_root)]
        return print_inspection_result(
            root,
            "sessions_list",
            data,
            args,
            _render_sessions_list,
            redactor=redactor,
        )
    if sub == "show" and len(rest) == 1:
        session_id = _inspection_id(rest[0], kind="session")
        session_paths = {path.stem: path for path in _session_files(sessions_root)}
        path = session_paths.get(session_id)
        if path is None:
            raise _not_found_error("session")
        try:
            if path.suffix == ".jsonl":
                _storage, data, _tree = load_session_readonly(
                    session_id,
                    sessions_root,
                )
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _unsafe_artifact_error() from exc
        return print_inspection_result(
            root,
            "sessions_show",
            data,
            args,
            _render_json_body,
            redactor=redactor,
        )
    raise CliError(
        code="usage",
        message="usage: pico sessions {list | show <session_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


def _is_resolve_pending_args(rest):
    return len(rest) == 1 or (len(rest) == 2 and rest[1] == "--apply")


def _resolve_pending_record(store, root, record_id, *, apply_flag):
    record_id = str(record_id or "")
    items = collect_recovery_review_items(store, root)
    invalid = next(
        (
            item
            for item in items["invalid_records"]
            if item.get("opaque_id") == record_id
        ),
        None,
    )
    tool_item = next(
        (
            item
            for item in items["tool_changes"]
            if item.get("tool_change_id") == record_id
        ),
        None,
    )
    restore_item = next(
        (
            item
            for item in items["restore_journals"]
            if item.get("checkpoint_id") == record_id
        ),
        None,
    )
    matches = [
        (kind, item)
        for kind, item in (
            ("invalid", invalid),
            ("tool_change", tool_item),
            ("restore", restore_item),
        )
        if item is not None
    ]
    if len(matches) > 1:
        raise CliError(
            code="recovery_review_ambiguous",
            message="ambiguous recovery review item",
            hint="Resolve the conflicting local records before retrying.",
            exit_code=CLI_EXIT_USAGE,
        )
    if not matches:
        raise CliError(
            code="recovery_review_not_found",
            message="unknown recovery review item",
            hint="Run `pico checkpoints pending`.",
            exit_code=CLI_EXIT_USAGE,
        )
    kind, _ = matches[0]
    if kind == "invalid":
        if not apply_flag:
            return dict(invalid)
        return store.quarantine_invalid_record(
            record_id, expected_raw_hash=invalid["raw_hash"]
        )
    if kind == "tool_change":
        record, raw = store.load_tool_change_record_snapshot(record_id)
        record_hash = hashlib.sha256(raw).hexdigest()
        if not apply_flag:
            return {
                **tool_item,
                "record_hash": record_hash,
            }
        return ToolChangeRecorder(store, owner_id="cli").resolve_pending(
            record_id,
            reviewed_by="cli",
            review_reason="explicit_cli_resolution",
            expected_record_hash=record_hash,
        )
    if kind == "restore":
        manager = RecoveryManager(
            store,
            root,
            checkpoint_writer=RecoveryCheckpointWriter(store, root),
        )
        preview = manager.preview_restore_journal_resolution(record_id)
        if not apply_flag:
            return preview
        return manager.resolve_restore_journal(
            record_id,
            expected_record_hash=preview["record_hash"],
            reviewed_by="cli",
            review_reason="explicit_cli_resolution",
        )
    raise AssertionError("unreachable recovery review kind")


def _render_pending_reviews(data):
    lines = []
    for key in (
        "tool_changes",
        "restore_journals",
        "invalid_records",
        "quarantined_records",
    ):
        for item in data.get(key, []):
            item_id = (
                item.get("tool_change_id")
                or item.get("checkpoint_id")
                or item.get("opaque_id", "-")
            )
            lines.append(f"{key}\t{item_id}\t{item.get('status', '')}")
    return "\n".join(lines)


def _render_checkpoints_list(records):
    lines = []
    for record in records:
        lines.append(
            f"{record['checkpoint_id']}\t{record['checkpoint_type']}\t{record.get('created_at', '')}"
        )
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
        reason = str(
            entry.get("recovery_note", "")
            or entry.get("reason", "")
            or entry.get("change_kind", "")
            or "-"
        )
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


def _render_run_summary(data):
    if "summary_status" in data:
        text = f"{data['summary_status']}: {data['reason']}"
        if data.get("migration_command"):
            text += f"\nRun `{data['migration_command']}`."
        return text
    return render_summary_text(data)


def _render_runs_show(data):
    sections = []
    for artifact in data["artifacts"]:
        sections.append(f"--- {artifact['name']} ---\n{artifact['content']}")
    return "\n".join(sections)


def _resolve_checkpoint_id(store, value, *, redactor=None):
    checkpoint_id = str(value or "").strip()
    display_id = redactor(checkpoint_id) if redactor is not None else checkpoint_id
    if not checkpoint_id:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {display_id}",
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        )

    try:
        records = store.list_checkpoint_records(strict=True)
    except (OSError, ValueError) as exc:
        raise _unsafe_artifact_error() from exc
    ids = []
    for record in records:
        candidate = str(record.get("checkpoint_id", ""))
        if candidate and _ARTIFACT_ID_RE.fullmatch(candidate):
            ids.append(candidate)
    if checkpoint_id in ids:
        return checkpoint_id

    matches = [item for item in ids if item.startswith(checkpoint_id)]
    if len(matches) == 1 and len(checkpoint_id) >= 6:
        return matches[0]
    if len(matches) > 1:
        display_matches = redactor(matches) if redactor is not None else matches
        raise CliError(
            code="checkpoint_prefix_ambiguous",
            message=f"ambiguous checkpoint prefix: {display_id}",
            hint="Use a longer checkpoint id prefix.",
            exit_code=CLI_EXIT_USAGE,
            details={"candidates": display_matches},
        )
    raise CliError(
        code="checkpoint_not_found",
        message=f"unknown checkpoint: {display_id}",
        hint="Run `pico checkpoints list`.",
        exit_code=CLI_EXIT_USAGE,
    )


def _load_checkpoint_record(store, checkpoint_id, *, redactor=None):
    try:
        return store.load_checkpoint_record(checkpoint_id)
    except FileNotFoundError as exc:
        display_id = redactor(checkpoint_id) if redactor is not None else checkpoint_id
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {display_id}",
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _preview_restore(manager, checkpoint_id, *, redactor=None):
    try:
        return manager.preview_restore(checkpoint_id)
    except FileNotFoundError as exc:
        display_id = redactor(checkpoint_id) if redactor is not None else checkpoint_id
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {display_id}",
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _apply_restore(manager, checkpoint_id, *, redactor=None):
    try:
        return manager.apply_restore(checkpoint_id)
    except FileNotFoundError as exc:
        display_id = redactor(checkpoint_id) if redactor is not None else checkpoint_id
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {display_id}",
            hint="Run `pico checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _load_run_artifacts(run_dir, run_id, redactor):
    artifacts = []
    for name in ("task_state.json", "report.json", "trace.jsonl"):
        path = _inspection_file(run_dir / name)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if name == "trace.jsonl":
                lines = [
                    json.dumps(
                        redactor(json.loads(line)),
                        sort_keys=True,
                        ensure_ascii=True,
                    )
                    for line in text.splitlines()
                    if line.strip()
                ]
                content = "\n".join(lines) + ("\n" if lines else "")
            else:
                content = (
                    json.dumps(
                    redactor(json.loads(text)),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                    )
                    + "\n"
                )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _unsafe_artifact_error() from exc
        artifacts.append({"name": name, "content": content})
    return {"run_id": run_id, "artifacts": artifacts}


def _session_files(sessions_root):
    sessions_root = _inspection_directory(sessions_root, allow_missing=True)
    if sessions_root is None:
        return []
    files = {}
    for path in sorted(sessions_root.iterdir()):
        if path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                safe = require_regular_no_symlink(path)
                session_id = safe.stem
                if session_id not in files or safe.suffix == ".jsonl":
                    files[session_id] = safe
        except (OSError, ValueError):
            continue
    return [files[key] for key in sorted(files)]


def _inspection_id(value, *, kind):
    value = str(value or "")
    if not _ARTIFACT_ID_RE.fullmatch(value):
        raise _not_found_error(kind)
    return value


def _inspection_directory(path, *, allow_missing=False):
    target = Path(os.path.abspath(os.fspath(path)))
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        except OSError as exc:
            raise _unsafe_artifact_error() from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise _unsafe_artifact_error()
    return target


def _inspection_file(path):
    try:
        return require_regular_no_symlink(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise _unsafe_artifact_error() from exc


def _not_found_error(kind):
    return CliError(
        code=f"{kind}_not_found",
        message=f"unknown {kind}",
        hint=f"Run `pico {kind}s list`.",
        exit_code=CLI_EXIT_USAGE,
    )


def _unsafe_artifact_error():
    return CliError(
        code="unsafe_artifact",
        message="unsafe local artifact",
        exit_code=CLI_EXIT_USAGE,
    )


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
        message="usage: pico checkpoints prune [--older-than <duration>] [--apply]",
        exit_code=CLI_EXIT_USAGE,
    )


def _render_json_body(data):
    return json.dumps(data, indent=2, sort_keys=True)
