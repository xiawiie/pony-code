"""Explicit MIG-OBS and MIG-TOOL workspace migration commands."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from pico.state.checkpoint_store import CheckpointStore, _validate_tool_change_record
from .errors import CLI_EXIT_RUNTIME, CLI_EXIT_USAGE, CliError
from pico.recovery.migration import ABSENT, Migration
from pico.agent.observability import (
    MAX_RUN_ARTIFACT_BYTES,
    RunArtifactError,
    convert_legacy_observability,
    convert_observability_v2,
    load_run_summary,
    REPORT_SCHEMA_VERSION,
)
from pico.recovery.models import TOOL_CHANGE_FORMAT_VERSION
from pico.tools.subprocess import run_hardened_git
from pico.sandbox.session import source_mutation_authority
from pico.security import (
    private_directory_identity,
    read_private_text,
    write_private_bytes_atomic,
)
from pico.tools.change_converter import convert_tool_change_v1


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_json(path, *, trusted_root=None, trusted_root_identity=None):
    if isinstance(path, str):
        return json.loads(path, object_pairs_hook=_object_from_pairs)
    return json.loads(
        read_private_text(
            path,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
            max_bytes=MAX_RUN_ARTIFACT_BYTES,
        ),
        object_pairs_hook=_object_from_pairs,
    )


def _write_bytes(path, data, *, trusted_root, trusted_root_identity):
    if len(data) > MAX_RUN_ARTIFACT_BYTES:
        raise ValueError("private file too large")
    write_private_bytes_atomic(
        path,
        data,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        max_existing_bytes=MAX_RUN_ARTIFACT_BYTES,
    )


def _write_json(path, value, *, trusted_root, trusted_root_identity):
    _write_bytes(
        path,
        (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )


def _copy_tree(source, candidate):
    shutil.copytree(source, candidate)


def _build_observability(source, candidate):
    _copy_tree(source, candidate)
    migrated = 0
    for run_dir in sorted(path for path in candidate.iterdir() if path.is_dir()):
        report_path = run_dir / "report.json"
        trace_path = run_dir / "trace.jsonl"
        task_state_path = run_dir / "task_state.json"
        if not report_path.exists() or not trace_path.exists() or not task_state_path.exists():
            raise RunArtifactError(
                "incomplete", "run artifact is missing report, trace, or task state"
            )
        run_identity = private_directory_identity(run_dir)
        def read(path):
            return read_private_text(
                path,
                trusted_root=run_dir,
                trusted_root_identity=run_identity,
                max_bytes=MAX_RUN_ARTIFACT_BYTES,
            )
        report = _read_json(
            report_path,
            trusted_root=run_dir,
            trusted_root_identity=run_identity,
        )
        events = [
            _read_json(line)
            for line in read(trace_path).splitlines()
            if line.strip()
        ]
        task_state = _read_json(
            task_state_path,
            trusted_root=run_dir,
            trusted_root_identity=run_identity,
        )
        if (
            report.get("record_type") == "run_report"
            and report.get("format_version") == REPORT_SCHEMA_VERSION
        ):
            load_run_summary(candidate, run_dir.name)
            continue
        if report.get("record_type") == "run_report" and report.get(
            "format_version"
        ) == 2:
            converted_report = convert_observability_v2(report)
            converted_events = events
        else:
            converted_report, converted_events = convert_legacy_observability(
                report,
                events,
                task_state,
            )
        _write_json(
            report_path,
            converted_report,
            trusted_root=run_dir,
            trusted_root_identity=run_identity,
        )
        converted_trace = (
            "\n".join(json.dumps(event, sort_keys=True) for event in converted_events)
            + "\n"
        ).encode("utf-8")
        _write_bytes(
            trace_path,
            converted_trace,
            trusted_root=run_dir,
            trusted_root_identity=run_identity,
        )
        load_run_summary(candidate, run_dir.name)
        migrated += 1
    return migrated


def _validate_observability(path):
    count = 0
    for run_dir in sorted(path.iterdir()):
        if not run_dir.is_dir():
            continue
        load_run_summary(path, run_dir.name)
        count += 1
    return count


def _build_tool_changes(source, candidate):
    _copy_tree(source, candidate)
    migrated = 0
    candidate_identity = private_directory_identity(candidate)
    for path in sorted(candidate.glob("*.json")):
        record = _read_json(
            path,
            trusted_root=candidate,
            trusted_root_identity=candidate_identity,
        )
        if record.get("format_version") == TOOL_CHANGE_FORMAT_VERSION:
            _validate_tool_change_record(record, expected_id=path.stem)
            continue
        converted = convert_tool_change_v1(record)
        _write_json(
            path,
            converted,
            trusted_root=candidate,
            trusted_root_identity=candidate_identity,
        )
        migrated += 1
    return migrated


def _validate_tool_changes(path, checkpoints_root):
    return CheckpointStore(checkpoints_root).validate_tool_change_reference_graph(path)


def _identity(workspace):
    root = Path(workspace.repo_root)
    info = root.stat()
    commit = ""
    git = getattr(workspace, "trusted_executables", {}).get("git")
    if git:
        try:
            result = run_hardened_git(
                git,
                ["rev-parse", "HEAD"],
                cwd=root,
                check=False,
                text=True,
            )
            candidate = result.stdout.strip() if result.returncode == 0 else ""
            if len(candidate) in {40, 64} and all(
                char in "0123456789abcdef" for char in candidate
            ):
                commit = candidate
        except (OSError, RuntimeError, ValueError):
            pass
    return {
        "repo_device": info.st_dev,
        "repo_inode": info.st_ino,
        "repo_commit": commit,
        "repo_dirty": str(workspace.status).strip() not in {"", "clean"},
    }


def _migration(workspace, contract):
    pico_root = Path(workspace.repo_root) / ".pico"
    if contract == "observability":
        live = "runs"
        namespace = "observability"
        validate = _validate_observability
    else:
        live = "checkpoints/tool_changes"
        namespace = "tool_changes"
        checkpoints_root = pico_root / "checkpoints"

        def validate(path):
            return _validate_tool_changes(path, checkpoints_root)

    return Migration(
        pico_root,
        contract="run_artifacts" if contract == "observability" else "tool_changes",
        source_version=1,
        target_version=(
            REPORT_SCHEMA_VERSION if contract == "observability" else 2
        ),
        live=live,
        namespace=namespace,
        workspace_identity=_identity(workspace),
        validate=validate,
        validate_candidate=validate if contract == "tool_changes" else None,
    ), validate


def _live_schema_state(contract, live):
    """Classify the live artifact schema independently of its transaction."""
    live = Path(live)
    try:
        live.lstat()
    except FileNotFoundError:
        return ABSENT
    if live.is_symlink() or not live.is_dir():
        return "invalid"

    migration_required = False
    try:
        if contract == "observability":
            for entry in sorted(live.iterdir()):
                if entry.is_symlink():
                    return "invalid"
                if not entry.is_dir():
                    continue
                try:
                    load_run_summary(live, entry.name)
                except RunArtifactError as exc:
                    if exc.status == "migration_required":
                        migration_required = True
                        continue
                    return "invalid"
        else:
            live_identity = private_directory_identity(live)
            for path in sorted(live.glob("*.json")):
                record = _read_json(
                    path,
                    trusted_root=live,
                    trusted_root_identity=live_identity,
                )
                if isinstance(record, dict) and record.get("format_version") == 1:
                    convert_tool_change_v1(record)
                    migration_required = True
                else:
                    _validate_tool_change_record(record, expected_id=path.stem)
    except (OSError, RuntimeError, ValueError, RunArtifactError):
        return "invalid"
    return "migration_required" if migration_required else "current"


def _status_with_live_schema(migration, contract):
    journal = dict(migration.status())
    transaction_state = journal.pop("state", ABSENT)
    journal["contract"] = contract
    return {
        **journal,
        "transaction_state": transaction_state,
        "live_schema_state": _live_schema_state(contract, migration.live),
    }


def migration_preflight(workspace):
    """Reject runtime startup while either explicit migration is active."""
    pico_root = Path(workspace.repo_root) / ".pico"
    if not pico_root.exists():
        return
    for contract in ("observability", "tool_changes"):
        migration, _ = _migration(workspace, contract)
        state = migration.status().get("state", ABSENT)
        if state != ABSENT:
            raise CliError(
                code="migration_required",
                message="workspace migration requires explicit recovery",
                details={"contract": contract, "state": state},
                exit_code=CLI_EXIT_RUNTIME,
            )


def handle_migrate(workspace, tokens, args):
    if len(tokens) == 1 and tokens[0] in {"status", "apply", "abort", "recover"}:
        return {
            contract: handle_migrate(workspace, [contract, tokens[0]], args)
            for contract in ("observability", "tool_changes")
        }
    if len(tokens) != 2 or tokens[0] not in {"observability", "tool_changes"} or tokens[1] not in {"status", "apply", "abort", "recover"}:
        raise CliError(
            code="usage",
            message="usage: pico migrate <status|apply|abort|recover>",
            exit_code=CLI_EXIT_USAGE,
        )
    contract, operation = tokens
    if operation == "status" and not (Path(workspace.repo_root) / ".pico").exists():
        return {
            "contract": contract,
            "transaction_state": ABSENT,
            "live_schema_state": ABSENT,
        }
    migration, validate = _migration(workspace, contract)
    try:
        if operation == "status":
            result = _status_with_live_schema(migration, contract)
        else:
            with source_mutation_authority(
                Path.home() / ".pico" / "sandboxes",
                Path(workspace.repo_root),
            ):
                if operation == "abort":
                    result = {"state": migration.abort()}
                    return result
                if operation == "recover":
                    result = {"state": migration.recover()}
                    return result
                counter = {"value": 0}

                def builder(source, candidate):
                    counter["value"] = (
                        _build_observability(source, candidate)
                        if contract == "observability"
                        else _build_tool_changes(source, candidate)
                    )

                state = migration.apply(builder)
                result = {"state": state, "migrated": counter["value"], "validated": validate(migration.live) if state == ABSENT else 0}
    except (OSError, ValueError, RunArtifactError) as exc:
        raise CliError(
            code="migration_failed",
            message="migration failed",
            details={"reason_code": str(exc)[:200]},
            exit_code=CLI_EXIT_RUNTIME,
        ) from exc
    return result
