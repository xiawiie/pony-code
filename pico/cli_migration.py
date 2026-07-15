"""Explicit MIG-OBS and MIG-TOOL workspace migration commands."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from .checkpoint_store import CheckpointStore, _validate_tool_change_record
from .cli_errors import CLI_EXIT_RUNTIME, CLI_EXIT_USAGE, CliError
from .migration import ABSENT, Migration
from .observability import (
    MAX_RUN_ARTIFACT_BYTES,
    RunArtifactError,
    convert_legacy_observability,
    convert_observability_v2,
    load_run_summary,
    REPORT_SCHEMA_VERSION,
)
from .recovery_models import TOOL_CHANGE_FORMAT_VERSION
from .safe_subprocess import run_hardened_git
from .sandbox_session import source_mutation_authority
from .security import (
    private_directory_identity,
    read_private_text,
    write_private_bytes_atomic,
)
from .tool_change_converter import convert_tool_change_v1


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
        return {"state": ABSENT, "contract": contract}
    migration, validate = _migration(workspace, contract)
    try:
        if operation == "status":
            result = migration.status()
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
