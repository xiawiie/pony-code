"""Docker Sandbox lifecycle CLI backed by the production owners."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys

from pico.state import file_lock
from pico.state.checkpoint_store import CheckpointStore
from .errors import CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .output import print_result
from pico.sandbox.docker import (
    default_docker_config_path,
    default_image_manifest_path,
    discover_local_docker,
    DockerClient,
    DockerSandboxError,
    DockerSandboxRunner,
    local_docker_sandbox_runtime,
    load_image_manifest,
    measure_workspace,
)
from pico.sandbox.apply import (
    load_finalized_diff_artifact,
    SandboxApplyError,
    SandboxMaintenanceContext,
    SourceApplier,
    SourceApplyStore,
)
from pico.sandbox.session import (
    clear_source_apply_authority,
    read_source_apply_authority,
    SandboxSessionError,
    SandboxSessionStore,
    source_apply_control_lock_path,
)


_COMMANDS = {
    "status",
    "prepare",
    "list",
    "inspect",
    "diff",
    "apply",
    "reconcile",
    "discard",
    "prune",
}
_TERMINAL_STATES = {"applied", "discarded", "failed"}
_PENDING_TTL_SECONDS = 7 * 24 * 60 * 60


def _usage(message="usage: pico sandbox <status|prepare|list|inspect|diff|apply|reconcile|discard|prune>"):
    return CliError(code="usage", message=message, exit_code=CLI_EXIT_USAGE)


def _render(value):
    lines = []

    def visit(item, indent=""):
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, (dict, list)):
                    lines.append(f"{indent}{key}:")
                    visit(child, indent + "  ")
                else:
                    lines.append(f"{indent}{key}: {child}")
        elif isinstance(item, list):
            if not item:
                lines.append(indent + "(none)")
            for child in item:
                if isinstance(child, dict):
                    lines.append(indent + "-")
                    visit(child, indent + "  ")
                else:
                    lines.append(f"{indent}- {child}")

    visit(value)
    return "\n".join(lines) + "\n"


def _state_parent():
    return Path.home() / ".pico" / "sandboxes"


def _workspace_id(root):
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]


def _lease_active(manifest):
    return manifest["lease"] is not None


def _summary(manifest):
    return {
        "sandbox_id": manifest["sandbox_id"],
        "pico_session_id": manifest["pico_session_id"],
        "state": manifest["state"],
        "created_at": manifest["created_at"],
        "updated_at": manifest["updated_at"],
        "workspace_id": _workspace_id(manifest["source"]["root"]),
        "lease_active": _lease_active(manifest),
        "reconciliation_required": manifest["state"]
        in {"running", "applying", "cleanup_pending", "review_required"},
        "diff": {
            "status": manifest["diff"]["status"],
            "candidate_count": manifest["diff"]["candidate_count"],
            "blocked_count": manifest["diff"]["blocked_count"],
        },
        "apply_status": manifest["apply"]["status"],
        "cleanup": dict(manifest["cleanup"]),
    }


def _empty_capacity(*, unknown_count=0):
    return {
        "active_count": 0,
        "pending_count": 0,
        "cleanup_pending_count": 0,
        "staging_bytes": 0,
        "oldest_age_seconds": 0,
        "orphan_verified_count": 0,
        "orphan_unknown_count": int(unknown_count),
        "reconciliation_required_count": 0,
    }


def _not_ready_status(reason_code, capacity):
    return {
        "record_type": "docker_sandbox_status",
        "format_version": 1,
        "status": "not_ready",
        "reason_code": str(reason_code),
        "platform_profile": "unknown",
        "client_version": "",
        "server_version": "",
        "api_version": "",
        "server_os": "",
        "server_arch": "",
        "endpoint_kind": "local_unix",
        "security": {
            "rootless": False,
            "seccomp": "unknown",
            "cgroup_limits": False,
            "eci": "unknown",
        },
        "image": {
            "present": False,
            "digest_match": False,
            "platform_match": False,
        },
        "network_performed": False,
        "mutation_performed": False,
        "capacity": capacity,
    }


def _local_runtime_status():
    try:
        image, authorization = local_docker_sandbox_runtime()
    except (DockerSandboxError, OSError) as exc:
        reason_code = getattr(
            exc,
            "code",
            "sandbox_runtime_authorization_invalid",
        )
        return None, {
            "status": "blocked",
            "kind": "local",
            "reason_code": reason_code,
        }
    return image, {
        "status": "enabled",
        "kind": authorization.attestation_kind,
        "reason_code": "local_authorization_verified",
    }


def sandbox_status_payload():
    image, runtime_authorization = _local_runtime_status()
    try:
        capacity = _capacity(_store().inventory())
    except (SandboxSessionError, OSError):
        payload = _not_ready_status(
            "sandbox_state_invalid",
            _empty_capacity(unknown_count=1),
        )
        payload["runtime_authorization"] = runtime_authorization
        return payload
    if image is None:
        payload = _not_ready_status(
            runtime_authorization["reason_code"],
            capacity,
        )
        payload["runtime_authorization"] = runtime_authorization
        return payload
    try:
        cli, endpoint = discover_local_docker()
        client = DockerClient(cli, endpoint, default_docker_config_path())
        payload = client.status(image)
    except DockerSandboxError as exc:
        payload = _not_ready_status(exc.code, capacity)
        payload["runtime_authorization"] = runtime_authorization
        return payload
    payload["capacity"] = capacity
    payload["runtime_authorization"] = runtime_authorization
    return payload


def _prepare_payload():
    try:
        image, authorization = local_docker_sandbox_runtime()
    except DockerSandboxError as exc:
        raise CliError(
            code=exc.code,
            message="sandbox local authorization is invalid",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    try:
        cli, endpoint = discover_local_docker()
        prepared = DockerClient(
            cli,
            endpoint,
            default_docker_config_path(),
        ).prepare(image)
    except DockerSandboxError as exc:
        raise CliError(
            code=exc.code,
            message="sandbox image preparation failed",
            details={"reason_code": exc.code},
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    prepared["runtime_authorization"] = {
        "status": "enabled",
        "kind": authorization.attestation_kind,
        "reason_code": "local_authorization_verified",
    }
    return prepared


def _store():
    return SandboxSessionStore(_state_parent())


def _reconciliation_runner(store):
    image = load_image_manifest(default_image_manifest_path())
    cli, endpoint = discover_local_docker()
    client = DockerClient(cli, endpoint, default_docker_config_path())
    return DockerSandboxRunner(client, store, image)


def _inventory(store):
    try:
        return store.inventory()
    except (OSError, SandboxSessionError) as exc:
        code = getattr(exc, "code", "sandbox_state_invalid")
        raise CliError(
            code=code,
            message="sandbox state is invalid",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc


def _capacity(inventory):
    manifests = inventory["manifests"]
    unknown = int(inventory["unknown_count"])
    active_states = {"creating", "ready", "running", "applying", "discarding"}
    pending_states = {"pending_review", "review_required", "failed"}
    active = sum(item["state"] in active_states for item in manifests)
    pending = sum(item["state"] in pending_states for item in manifests)
    cleanup_pending = sum(
        item["state"] == "cleanup_pending"
        or item["cleanup"]["status"] == "pending"
        for item in manifests
    )
    reconciliation = sum(
        item["active_call"] is not None
        or item["state"]
        in {"running", "applying", "cleanup_pending", "review_required"}
        for item in manifests
    )
    staging_bytes = 0
    oldest_age = 0
    verified_orphans = 0
    for manifest in manifests:
        workspace = Path(manifest["execution"]["root"])
        try:
            info = workspace.lstat()
        except FileNotFoundError:
            continue
        if (
            workspace.is_symlink()
            or not workspace.is_dir()
            or (info.st_dev, info.st_ino)
            != (
                manifest["execution"]["device"],
                manifest["execution"]["inode"],
            )
        ):
            unknown += 1
            continue
        try:
            staging_bytes += measure_workspace(workspace)["allocated_bytes"]
        except (DockerSandboxError, OSError):
            unknown += 1
            continue
        oldest_age = max(oldest_age, _age_seconds(manifest["created_at"]))
        if manifest["state"] in {"applied", "discarded", "cleanup_pending"}:
            verified_orphans += 1
    return {
        "active_count": active,
        "pending_count": pending,
        "cleanup_pending_count": cleanup_pending,
        "staging_bytes": staging_bytes,
        "oldest_age_seconds": oldest_age,
        "orphan_verified_count": verified_orphans,
        "orphan_unknown_count": unknown,
        "reconciliation_required_count": reconciliation,
    }


def _find(store, sandbox_id):
    try:
        return store.find(sandbox_id)
    except (OSError, SandboxSessionError) as exc:
        code = getattr(exc, "code", "sandbox_state_invalid")
        raise CliError(
            code=code,
            message="sandbox session not found",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc


def _confirm(args, action, sandbox_id, *, yes):
    if yes:
        return
    if getattr(args, "no_input", False):
        raise CliError(
            code="confirmation_required",
            message=f"sandbox {action} requires confirmation",
            hint=f"Re-run with `--yes` after reviewing sandbox {sandbox_id}.",
            exit_code=CLI_EXIT_USAGE,
        )
    try:
        answer = input(f"{action} sandbox {sandbox_id}? [y/N] ")
    except EOFError as exc:
        raise CliError(
            code="confirmation_required",
            message=f"sandbox {action} requires confirmation",
            exit_code=CLI_EXIT_USAGE,
        ) from exc
    if answer.strip().casefold() not in {"y", "yes"}:
        raise CliError(
            code="confirmation_declined",
            message=f"sandbox {action} cancelled",
            exit_code=CLI_EXIT_USAGE,
        )


def _apply_review(session):
    artifact, digest = load_finalized_diff_artifact(session)
    counts = artifact["counts"]
    blocked_count = sum(
        counts.get(name, 0)
        for name in ("blocked_sensitive", "blocked_size", "blocked_type")
    )
    risky = [
        {
            "path": entry["path"],
            "classification": entry["classification"],
        }
        for entry in artifact["entries"]
        if entry["classification"] != "candidate"
    ]
    return {
        "sandbox_id": session.sandbox_id,
        "diff_digest": digest,
        "source_root": session.manifest["source"]["root"],
        "candidate_count": counts.get("candidate", 0)
        + counts.get("high_risk_candidate", 0),
        "candidate_bytes": artifact["candidate_bytes"],
        "change_counts": {
            name: sum(
                entry["change_kind"] == name for entry in artifact["entries"]
            )
            for name in ("created", "modified", "deleted", "type_changed")
        },
        "high_risk_count": counts.get("high_risk_candidate", 0),
        "blocked_count": blocked_count,
        "high_risk_or_blocked_paths": risky[:10],
    }


def _display_apply_review(review):
    print(_render({"apply_review": review}), end="", file=sys.stderr)


def _release_if_owned(store, state_root):
    try:
        current = store.inspect(state_root)
    except (OSError, SandboxSessionError):
        return
    lease = current.manifest["lease"]
    if lease is None:
        return
    try:
        store.release(state_root, lease["owner_nonce"])
    except (OSError, SandboxSessionError):
        pass


def _cleanup_source_apply_artifacts(session):
    apply = session.manifest["apply"]
    source_root = Path(session.manifest["source"]["root"])
    store = SandboxSessionStore(session.state_root.parent.parent)
    authority = read_source_apply_authority(store.parent, source_root)
    if apply["status"] not in {"apply_applied", "apply_failed_rolled_back"}:
        if authority is not None:
            if (
                authority["sandbox_id"] != session.sandbox_id
                or authority["state_root"] != str(session.state_root)
                or authority["diff_digest"] != session.manifest["diff"]["digest"]
            ):
                raise SandboxApplyError("sandbox_state_invalid")
            raise SandboxApplyError("source_apply_review_required")
        return
    sidecar = session.manifest["sidecar"]
    if sidecar is None or not apply["journal_id"]:
        raise SandboxApplyError("sandbox_apply_cleanup_failed")
    with file_lock.locked_file(
        source_apply_control_lock_path(store.parent, source_root),
        require_lock=True,
    ):
        authority = read_source_apply_authority(store.parent, source_root)
        source = session.manifest["source"]
        if authority is not None and any(
            authority[name] != value
            for name, value in {
                "source_root": source["root"],
                "source_device": source["device"],
                "source_inode": source["inode"],
                "sandbox_id": session.sandbox_id,
                "state_root": str(session.state_root),
                "journal_id": apply["journal_id"],
                "diff_digest": session.manifest["diff"]["digest"],
            }.items()
        ):
            raise SandboxApplyError("sandbox_state_invalid")
        mutation_store = CheckpointStore(source_root)
        with mutation_store.mutation_lock(source_apply_journal_id=apply["journal_id"]):
            guard = mutation_store.source_apply_guard()
            if authority is None and guard is not None:
                raise SandboxApplyError("sandbox_state_invalid")
            cleanup = SourceApplyStore(session.state_root).cleanup_terminal_blobs(
                apply["journal_id"]
            )
            if not cleanup["complete"]:
                raise SandboxApplyError("sandbox_apply_cleanup_failed")
            if authority is None:
                return
            mutation_store.finish_source_apply_guard(
                journal_id=apply["journal_id"]
            )
            clear_source_apply_authority(
                store.parent,
                source_root,
                expected_authority=authority,
            )


def _apply(store, session, diff_digest):
    try:
        acquired = store.acquire(session.state_root)
        context = SandboxMaintenanceContext(store, acquired)
        applier = SourceApplier(context, context.observer())
        if acquired.state == "applying":
            return applier.reconcile()
        return applier.apply(diff_digest)
    except (OSError, SandboxApplyError, SandboxSessionError) as exc:
        code = getattr(exc, "code", "sandbox_apply_failed")
        raise CliError(
            code=code,
            message="sandbox apply failed",
            details={"reason_code": code},
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    finally:
        _release_if_owned(store, session.state_root)


def _reconcile_source_apply(store, source_root):
    source_root = Path(source_root).absolute()
    lock_path = source_apply_control_lock_path(store.parent, source_root)
    try:
        with file_lock.locked_file(lock_path, require_lock=True):
            authority = read_source_apply_authority(store.parent, source_root)
            if authority is None:
                raise SandboxApplyError("sandbox_apply_not_reconcilable")
            apply_store = SourceApplyStore(authority["state_root"])
            try:
                journal = apply_store.load_journal(
                    authority["journal_id"],
                    sandbox_id=authority["sandbox_id"],
                )
            except FileNotFoundError:
                raise SandboxApplyError("sandbox_apply_journal_invalid") from None
            if (
                journal["diff_digest"] != authority["diff_digest"]
                or journal["source"]
                != {
                    "root": authority["source_root"],
                    "device": authority["source_device"],
                    "inode": authority["source_inode"],
                }
                or journal["status"]
                not in {"applying", "apply_review_required"}
            ):
                raise SandboxApplyError("sandbox_apply_journal_invalid")
            session = store.reconcile_source_apply_authority(
                source_root,
                authority,
                journal_status=journal["status"],
            )
            journal = apply_store.require_review(
                journal["journal_id"],
                sandbox_id=authority["sandbox_id"],
            )
            return {
                "sandbox_id": session.sandbox_id,
                "state": session.state,
                "apply_status": session.manifest["apply"]["status"],
                "journal_id": journal["journal_id"],
                "journal_status": journal["status"],
            }
    except (OSError, SandboxApplyError, SandboxSessionError) as exc:
        code = getattr(exc, "code", "sandbox_apply_failed")
        raise CliError(
            code=code,
            message="sandbox apply reconciliation failed",
            details={"reason_code": code},
            exit_code=CLI_EXIT_CONFIG,
        ) from exc


def _discard(store, session):
    try:
        acquired = store.acquire(session.state_root)
        _cleanup_source_apply_artifacts(acquired)
        return _summary(store.discard(acquired.state_root).manifest)
    except (OSError, SandboxApplyError, SandboxSessionError) as exc:
        code = getattr(exc, "code", "sandbox_discard_failed")
        raise CliError(
            code=code,
            message="sandbox discard failed",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    finally:
        _release_if_owned(store, session.state_root)


def _age_seconds(timestamp):
    try:
        value = datetime.fromisoformat(timestamp)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _prune(store, *, apply):
    inventory = _inventory(store)
    manifests = inventory["manifests"]
    if apply and inventory["unknown_count"]:
        raise CliError(
            code="sandbox_prune_refused",
            message="sandbox prune refused unknown state",
            details={"unknown_count": inventory["unknown_count"]},
            exit_code=CLI_EXIT_CONFIG,
        )
    outcomes = []
    if apply:
        active_ids = [
            manifest["sandbox_id"]
            for manifest in manifests
            if manifest["active_call"] is not None
        ]
        runner = None
        runner_error = None
        if active_ids:
            try:
                runner = _reconciliation_runner(store)
                if runner is None:
                    raise DockerSandboxError("container_reconciliation_failed")
            except (DockerSandboxError, OSError) as exc:
                runner_error = getattr(exc, "code", "container_reconciliation_failed")
        for sandbox_id in active_ids:
            session = store.find(sandbox_id)
            try:
                if runner is None:
                    raise DockerSandboxError(runner_error)
                acquired = store.acquire(session.state_root)
                reconciled = runner.reconcile_session(acquired)
                outcomes.append(
                    {
                        "sandbox_id": sandbox_id,
                        "state": reconciled.state,
                        "status": (
                            "reconciled"
                            if reconciled.manifest["active_call"] is None
                            else "reconciliation_required"
                        ),
                    }
                )
            except (DockerSandboxError, OSError, SandboxSessionError) as exc:
                outcomes.append(
                    {
                        "sandbox_id": sandbox_id,
                        "status": getattr(
                            exc,
                            "code",
                            "container_reconciliation_failed",
                        ),
                    }
                )
            finally:
                _release_if_owned(store, session.state_root)
        inventory = _inventory(store)
        manifests = inventory["manifests"]

    candidates = []
    reconciliation_required = 0
    for manifest in manifests:
        if manifest["active_call"] is not None:
            reconciliation_required += 1
            continue
        expired = (
            manifest["state"] in {"pending_review", "review_required", "failed"}
            and not _lease_active(manifest)
            and _age_seconds(manifest["updated_at"]) >= _PENDING_TTL_SECONDS
        )
        terminal_cleanup = (
            manifest["state"] in {"applied", "cleanup_pending"}
            and manifest["cleanup"]["status"] != "complete"
        )
        if expired or terminal_cleanup:
            candidates.append(manifest["sandbox_id"])
    if apply:
        for sandbox_id in candidates:
            session = store.find(sandbox_id)
            try:
                if session.state == "cleanup_pending":
                    acquired = store.acquire(session.state_root)
                    store.resume_cleanup(acquired.state_root)
                    outcomes.append({"sandbox_id": sandbox_id, "status": "cleaned"})
                elif session.state == "applied":
                    _cleanup_source_apply_artifacts(session)
                    store.cleanup_applied(session.state_root)
                    outcomes.append({"sandbox_id": sandbox_id, "status": "cleaned"})
                else:
                    acquired = store.acquire(session.state_root)
                    _cleanup_source_apply_artifacts(acquired)
                    store.discard(acquired.state_root)
                    outcomes.append({"sandbox_id": sandbox_id, "status": "discarded"})
            except (OSError, SandboxApplyError, SandboxSessionError) as exc:
                outcomes.append(
                    {
                        "sandbox_id": sandbox_id,
                        "status": getattr(exc, "code", "sandbox_cleanup_failed"),
                    }
                )
            finally:
                _release_if_owned(store, session.state_root)
    return {
        "dry_run": not apply,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "outcomes": outcomes,
        "refused_unknown_count": inventory["unknown_count"],
        "reconciliation_required_count": reconciliation_required,
    }


def handle_sandbox(args, tokens):
    if not tokens or tokens[0] not in _COMMANDS:
        raise _usage()
    command, rest = tokens[0], list(tokens[1:])
    if command in {"status", "prepare", "list"} and rest:
        raise _usage(f"usage: pico sandbox {command}")
    if command in {"inspect", "diff"} and len(rest) != 1:
        raise _usage(f"usage: pico sandbox {command} <sandbox-id>")
    if command in {"apply", "discard"}:
        if not rest or len(rest) > 2 or any(item != "--yes" for item in rest[1:]):
            raise _usage(f"usage: pico sandbox {command} <sandbox-id> [--yes]")
    if command == "reconcile" and rest not in ([], ["--yes"]):
        raise _usage("usage: pico sandbox reconcile [--yes]")
    if command == "prune":
        if len(rest) > 1 or rest and rest[0] not in {"--dry-run", "--apply"}:
            raise _usage("usage: pico sandbox prune [--dry-run|--apply]")

    if command == "status":
        payload = sandbox_status_payload()
    elif command == "prepare":
        payload = _prepare_payload()
    elif command == "list":
        store = _store()
        inventory = _inventory(store)
        payload = {
            "sessions": [_summary(item) for item in inventory["manifests"]],
            "capacity": _capacity(inventory),
        }
    elif command == "prune":
        payload = _prune(_store(), apply=rest == ["--apply"])
    elif command == "reconcile":
        _confirm(args, command, "source apply", yes="--yes" in rest)
        payload = _reconcile_source_apply(_store(), args.cwd)
    else:
        store = _store()
        session = _find(store, rest[0])
        if command == "inspect":
            payload = _summary(session.manifest)
        elif command == "diff":
            try:
                artifact, digest = load_finalized_diff_artifact(session)
            except SandboxApplyError as exc:
                raise CliError(
                    code=exc.code,
                    message="sandbox diff unavailable",
                    exit_code=CLI_EXIT_CONFIG,
                ) from exc
            payload = {
                "sandbox_id": session.sandbox_id,
                "diff_digest": digest,
                "counts": artifact["counts"],
                "candidate_bytes": artifact["candidate_bytes"],
                "entries": [
                    {
                        "path": item["path"],
                        "change_kind": item["change_kind"],
                        "classification": item["classification"],
                    }
                    for item in artifact["entries"]
                ],
                "rendered": artifact["rendered"],
            }
        else:
            if command == "apply":
                try:
                    review = _apply_review(session)
                except SandboxApplyError as exc:
                    raise CliError(
                        code=exc.code,
                        message="sandbox apply review unavailable",
                        exit_code=CLI_EXIT_CONFIG,
                    ) from exc
                _display_apply_review(review)
                _confirm(
                    args,
                    command,
                    session.sandbox_id,
                    yes="--yes" in rest[1:],
                )
                payload = _apply(store, session, review["diff_digest"])
            else:
                _confirm(
                    args,
                    command,
                    session.sandbox_id,
                    yes="--yes" in rest[1:],
                )
                payload = _discard(store, session)
    return print_result("docker_sandbox_" + command, payload, args, _render)
