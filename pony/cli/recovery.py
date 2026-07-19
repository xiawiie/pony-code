"""Recovery command handlers for Pony's explicit CLI surface."""

import json
import os
from pathlib import Path
import re
import stat

from pony.state.legacy_artifacts import LegacyArtifactError, LegacyCheckpointReader
from .errors import CLI_EXIT_USAGE, CliError
from .output import build_inspection_redactor, print_inspection_result
from .session import load_session_readonly
from pony.agent.observability import (
    RunArtifactError,
    load_run_summary,
    render_summary_text,
)
from pony.security.paths import require_regular_no_symlink
from pony.workspace.context import WorkspaceContext  # noqa: F401


_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def handle_checkpoints(root, tokens, args):
    redactor = build_inspection_redactor(root, args)
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    try:
        store = LegacyCheckpointReader(root)
    except (OSError, ValueError, LegacyArtifactError) as exc:
        raise _unsafe_artifact_error() from exc
    if sub == "pending" and not rest:
        data = store.review_items()
        return print_inspection_result(
            root,
            "checkpoints_pending",
            data,
            args,
            _render_pending_reviews,
            redactor=redactor,
        )
    if sub == "list" and not rest:
        try:
            records = store.list_checkpoint_records(strict=True)
        except (OSError, ValueError, LegacyArtifactError) as exc:
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
    raise CliError(
        code="usage",
        message="usage: pony checkpoints {list | show <id> | pending}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_runs(root, tokens, args):
    redactor = build_inspection_redactor(root, args)
    runs_root = Path(root) / ".pony" / "runs"
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
                data["migration_command"] = "pony migrate observability apply"
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
        message="usage: pony runs {list | show <run_id> | summary <run_id|latest>}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_sessions(root, tokens, args):
    redactor = build_inspection_redactor(root, args)
    sessions_root = Path(root) / ".pony" / "sessions"
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
        message="usage: pony sessions {list | show <session_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


def _render_pending_reviews(data):
    lines = []
    for key in (
        "tool_changes",
        "restore_journals",
        "invalid_records",
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
            hint="Run `pony checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        )

    try:
        records = store.list_checkpoint_records(strict=True)
    except (OSError, ValueError, LegacyArtifactError) as exc:
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
        hint="Run `pony checkpoints list`.",
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
            hint="Run `pony checkpoints list`.",
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
        hint=f"Run `pony {kind}s list`.",
        exit_code=CLI_EXIT_USAGE,
    )


def _unsafe_artifact_error():
    return CliError(
        code="unsafe_artifact",
        message="unsafe local artifact",
        exit_code=CLI_EXIT_USAGE,
    )


def _render_json_body(data):
    return json.dumps(data, indent=2, sort_keys=True)
