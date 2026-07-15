"""Agent control loop extracted from the runtime facade."""

from copy import deepcopy
from dataclasses import replace
import json
import logging
import os
import stat
import time
import uuid

from .action_codec import FinalAction, RetryAction, ToolAction, decode_action
from .providers._shared import _ProviderFailure
from . import security as securitylib
from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .context.renderer import build_injection_snapshot
from .messages import append_messages, make_tool_pair
from .recovery_policy import assess_command
from .recovery_models import TRACE_RECOVERY_CHECKPOINT_CREATED
from .recovery_checkpoint_writer import (
    current_recovery_checkpoint_id,
    set_current_recovery_checkpoint_id,
)
from .security import ensure_private_dir, require_regular_no_symlink
from .task_state import (
    STOP_REASON_PERSISTENCE_ERROR,
    STATUS_RUNNING,
    TaskState,
)
from .workspace import clip, now
from .tool_executor import (
    ToolExecutionResult,
    _EFFECT_CLASS_BY_TOOL,
    _add_command_policy,
    _command_approval_metadata,
    _effect_class,
    _metadata,
)

logger = logging.getLogger("pico")


_RUNTIME_TERMINAL_TEXT = {
    "model_error": "The model request failed. This turn was stopped.",
    "interrupted": "This turn was interrupted before completion.",
    "persistence_error": "This turn stopped because session state could not be saved.",
    "runtime_error": "This turn stopped because the runtime failed.",
}


_USAGE_SUM_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

_ATTEMPT_ORIGINS = ("initial", "tool_followup", "retry_action", "model_retry")
_MODEL_RETRY_DELAYS = (0.5, 1.0)


def _empty_usage_totals():
    return {**{key: 0 for key in _USAGE_SUM_KEYS}, "cache_hit": False}


def _empty_model_execution():
    return {
        "model_attempts": 0,
        "model_turns": 0,
        "model_failures": 0,
        "model_retries": 0,
        "attempt_origin_counts": {origin: 0 for origin in _ATTEMPT_ORIGINS},
        "transport_attempts": 0,
        "transport_retries": 0,
        "transport_evidence_complete": True,
        "failure_reason_counts": {},
        "tool_report": {
            "calls": 0,
            "allowed": 0,
            "denied": 0,
            "name_counts": {},
            "status_counts": {},
            "changed_paths": [],
            "partial_successes": 0,
            "recovery_review_required": False,
            "sandbox_calls": 0,
            "sandbox_target_started_count": 0,
            "sandbox_outcome_counts": {},
            "sandbox_cleanup_failure_count": 0,
            "host_fallback_count": 0,
        },
    }


def _transport_evidence(model_client):
    attempts = getattr(model_client, "last_transport_attempts", None)
    if type(attempts) is not int or attempts < 0:
        return None, None, False
    return attempts, max(0, attempts - 1), True


def _record_transport(model_execution, model_client):
    attempts, retries, complete = _transport_evidence(model_client)
    if not complete:
        model_execution["transport_evidence_complete"] = False
        return attempts, retries, False
    model_execution["transport_attempts"] += attempts
    model_execution["transport_retries"] += retries
    return attempts, retries, True


def _record_model_failure(
    agent,
    task_state,
    model_execution,
    *,
    attempt_origin,
    outcome,
    failure_phase,
    error,
):
    attempts, retries, complete = _record_transport(
        model_execution,
        agent.model_client,
    )
    reason = (
        error.code
        if isinstance(error, _ProviderFailure)
        else "response_processing" if failure_phase == "response_processing" else "provider_error"
    )
    model_execution["model_failures"] += 1
    reasons = model_execution["failure_reason_counts"]
    reasons[reason] = reasons.get(reason, 0) + 1
    try:
        agent.emit_trace(
            task_state,
            "model_failed",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "attempt_origin": attempt_origin,
                "outcome": outcome,
                "failure_phase": failure_phase,
                "reason_code": reason,
                "transport_attempts": attempts,
                "transport_retries": retries,
                "transport_evidence_complete": complete,
            },
        )
    except BaseException as trace_error:
        logger.debug(
            "model failure trace could not be written (%s)",
            type(trace_error).__name__,
        )


def _add_usage(totals, usage):
    usage = dict(usage or {})
    for key in _USAGE_SUM_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            totals[key] += value
    total_tokens = usage.get("total_tokens")
    if not isinstance(total_tokens, int) or isinstance(total_tokens, bool):
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
        ):
            totals["total_tokens"] += input_tokens + output_tokens
    totals["cache_hit"] = totals["cache_hit"] or bool(usage.get("cache_hit"))
    return totals


def _action_trace_payload(action):
    if isinstance(action, ToolAction):
        return {
            "action_type": "tool",
            "origin": action.origin,
            "ignored_tool_count": action.ignored_tool_count,
        }
    if isinstance(action, FinalAction):
        return {
            "action_type": "final",
            "origin": action.origin,
            "truncated": action.truncated,
        }
    return {
        "action_type": "retry",
        "origin": action.origin,
        "reason_code": action.reason_code,
        "excerpt": action.excerpt,
    }


def _safe_tool_name(agent, name):
    return name if name in agent.tools else "unknown_tool"


def _sanitize_action(agent, action):
    if isinstance(action, FinalAction):
        return replace(action, text=agent.redact_text(action.text)), None
    if isinstance(action, RetryAction):
        return replace(
            action,
            notice=agent.redact_text(action.notice),
            excerpt=agent.redact_text(action.excerpt),
        ), None

    action = replace(action, name=_safe_tool_name(agent, action.name))

    original_arguments = action.arguments
    safe_arguments = agent.redact_artifact(original_arguments)
    serialized = json.dumps(
        original_arguments,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    original_contains_secret = securitylib.contains_secret_material(
        serialized,
        env=agent.redaction_env,
        secret_env_names=agent.secret_env_names,
    )
    blocked = safe_arguments != original_arguments or original_contains_secret
    if not blocked:
        return action, None
    safe_serialized = json.dumps(
        safe_arguments,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    if securitylib.contains_secret_material(
        safe_serialized,
        env=agent.redaction_env,
        secret_env_names=agent.secret_env_names,
    ):
        safe_arguments = {}
    tool = agent.tools.get(action.name)
    effect_class = (
        "workspace_write"
        if tool is None and action.name not in _EFFECT_CLASS_BY_TOOL
        else _effect_class(action.name, bool(tool and tool["risky"]))
    )
    metadata = _metadata(
        "rejected",
        effect_class=effect_class,
        tool_error_code="sensitive_content_block",
        security_event_type="sensitive_access_block",
        risk_level="high",
    )
    if action.name == "run_shell":
        assessment = assess_command(
            str((original_arguments or {}).get("command", "")),
            agent.root,
        )
        _add_command_policy(
            metadata,
            assessment["risk_class"],
            _command_approval_metadata(
                assessment,
                agent.approval_policy,
                "blocked",
            ),
        )
    result = ToolExecutionResult(
        content="error: sensitive_content_block",
        metadata=metadata,
    )
    return replace(action, arguments=safe_arguments), result


class SessionCommitError(RuntimeError):
    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause
        self.committed = bool(getattr(cause, "committed", False))


def _block_session_writes(agent, cause):
    if getattr(cause, "committed", False):
        agent._session_write_blocked_cause = cause


def _session_writes_blocked(agent):
    return getattr(agent, "_session_write_blocked_cause", None) is not None


def _adopt_session(agent, session, path):
    agent.session = session
    agent.session_path = path
    agent.memory = type(agent.memory).from_dict(
        session.get("working_memory"),
        workspace_root=agent.root,
    )


def _commit_session(agent, *, messages=()):
    blocked_cause = getattr(agent, "_session_write_blocked_cause", None)
    if blocked_cause is not None:
        raise SessionCommitError(blocked_cause)
    candidate = agent.redact_artifact(deepcopy(agent.session))
    safe_messages = tuple(agent.redact_artifact(message) for message in messages)
    candidate["messages"] = append_messages(candidate.get("messages", []), *safe_messages)
    try:
        saved_path = agent.session_store.save(candidate)
    except Exception as exc:
        if getattr(exc, "committed", False):
            try:
                persisted = agent.session_store.load(candidate["id"])
            except Exception:
                _block_session_writes(agent, exc)
                raise SessionCommitError(exc) from exc
            _adopt_session(
                agent,
                persisted,
                agent.session_store.path_for(candidate["id"]),
            )
            if persisted == candidate:
                return
        raise SessionCommitError(exc) from exc
    _adopt_session(agent, candidate, saved_path)


def _plain_message(role, text, *, origin=""):
    meta = {"created_at": now()}
    if origin:
        meta["origin"] = origin
    return {"role": role, "content": str(text), "_pico_meta": meta}


def _runtime_terminal_message(stop_reason):
    return _plain_message(
        "assistant",
        _RUNTIME_TERMINAL_TEXT[stop_reason],
        origin="runtime_terminal",
    )


def _prepare_tool_result(
    agent,
    *,
    content: str,
    tool_name: str = "",
    tool_args: dict | None = None,
    digest_applied: bool = False,
    source_hash: str | None = None,
):
    """Prepare a tool result for a later atomic pair commit.

    Task 26: when the raw ``content`` exceeds the digest threshold
    (see ``pico.context.digest.should_digest``), we:

    1. Write the raw body to ``<run_dir>/tool_results/<hash>.txt`` so a
       later turn can recover the full output on demand.
    2. Replace ``content`` with the rendered digest (title + bullets +
       content SHA-256 and logical raw-result id) — the agent still sees
       the shape of the result without learning a Project State host path.
    3. Return ``digest_applied`` and ``source_hash`` so the atomic pair
       commit can distinguish digested messages from inline ones.

    When ``agent.current_run_dir`` is unavailable (e.g. mid-test), we
    still emit the digest without a ``raw_result_id`` — no crash.
    Callers can override the auto-digest by passing
    ``digest_applied=True`` up-front (used by explicit callers that
    have already digested the content themselves).
    """
    safe_content = str(agent.redact_text(content))

    # Lazy import to avoid the agent_loop → context.digest → ... cycle risk.
    from pico.context.digest import (
        digest_tool_result,
        render_digest_content,
        should_digest,
    )

    display_content = safe_content
    tool_args = tool_args or {}

    # Task B3: threshold overridable via pico.toml → agent.context_config.
    cfg = getattr(agent, "context_config", None)
    if not isinstance(cfg, dict):
        cfg = {}
    threshold = int(cfg.get("digest_size_threshold", 1200))
    # Only run the digest heuristic if the caller hasn't already digested.
    if not digest_applied and should_digest(safe_content, threshold=threshold):
        # Task D1: single-call digest. Compute the digest once (per-tool
        # summarizer runs exactly once); then attach a logical result id
        # after the content-addressed body is durably written.
        from dataclasses import replace as _dc_replace
        digest = digest_tool_result(tool_name, tool_args, safe_content)
        source_hash = digest.source_hash
        run_dir = getattr(agent, "current_run_dir", None)
        raw_result_id = ""
        if run_dir is not None:
            try:
                raw_dir = ensure_private_dir(run_dir / "tool_results")
                raw_path = raw_dir / f"{source_hash}.txt"
                checked_path = require_regular_no_symlink(raw_path, allow_missing=True)
                try:
                    before = checked_path.lstat()
                except FileNotFoundError:
                    before = None
                if before is not None and not stat.S_ISREG(before.st_mode):
                    raise ValueError("raw tool result changed")
                if before is not None and before.st_nlink != 1:
                    raise ValueError("raw tool result changed")
                flags = os.O_WRONLY
                if before is None:
                    flags |= os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(checked_path, flags, 0o600)
                try:
                    opened = os.fstat(descriptor)
                    current = os.stat(checked_path, follow_symlinks=False)
                    identity = (opened.st_dev, opened.st_ino)
                    if opened.st_nlink != 1:
                        raise ValueError("raw tool result changed")
                    if not stat.S_ISREG(opened.st_mode) or (
                        current.st_dev,
                        current.st_ino,
                    ) != identity:
                        raise ValueError("raw tool result changed")
                    if before is not None and (
                        before.st_dev,
                        before.st_ino,
                    ) != identity:
                        raise ValueError("raw tool result changed")
                    os.fchmod(descriptor, 0o600)
                    os.ftruncate(descriptor, 0)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        descriptor = -1
                        handle.write(safe_content)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                raw_result_id = f"tool_result:{source_hash}"
            except (OSError, ValueError) as exc:
                logger.debug("raw tool_result write failed: %s", type(exc).__name__)
                raw_result_id = ""
        if raw_result_id:
            digest = _dc_replace(digest, raw_result_id=raw_result_id)
        display_content = render_digest_content(digest)
        digest_applied = True

    return display_content, {
        "digest_applied": digest_applied,
        "source_hash": source_hash,
    }


def _run_turn_preflight(agent, user_message):
    refresh = agent.refresh_prefix()
    agent.resume_state = agent.evaluate_resume_state()
    metadata = {
        "prefix_chars": len(agent.prefix),
        "workspace_chars": len(agent.workspace.text()),
        "memory_chars": len(agent.memory_text()),
        "request_chars": len(str(user_message)),
        "tool_count": len(agent.tools),
        "workspace_docs": len(agent.workspace.project_docs),
        "recent_commits": len(agent.workspace.recent_commits),
        "workspace_fingerprint": agent.prefix_state.workspace_fingerprint,
        "tool_signature": agent.prefix_state.tool_signature,
        "workspace_changed": refresh["workspace_changed"],
        "prefix_changed": refresh["prefix_changed"],
        "resume_status": agent.resume_state.get("status", CHECKPOINT_NONE_STATUS),
        "stale_summary_invalidations": int(
            agent.resume_state.get("stale_summary_invalidations", 0)
        ),
        "stale_paths": list(agent.resume_state.get("stale_paths", [])),
        "runtime_identity_mismatch_fields": list(
            agent.resume_state.get("runtime_identity_mismatch_fields", [])
        ),
    }
    metadata.update(agent.detected_secret_env_summary())
    return metadata


def _start_agent_run(agent, task_state, user_message):
    agent.current_run_dir = agent.run_store.start_run(task_state)
    agent.emit_trace(
        task_state,
        "run_started",
        {
            "task_id": task_state.task_id,
            "user_request": clip(user_message, 300),
        },
    )
    preflight_metadata = _run_turn_preflight(agent, user_message)
    injection_snapshot, injection_telemetry = build_injection_snapshot(
        agent,
        user_message,
    )
    return preflight_metadata, injection_snapshot, injection_telemetry


def _build_attempt_request(
    agent,
    task_state,
    user_message,
    attempts,
    runtime_feedback,
    injection_snapshot,
    injection_telemetry,
    preflight_metadata,
    context_reduction_checkpoint_created,
    attempt_origin,
    model_execution,
):
    task_state.record_attempt()
    model_execution["model_attempts"] += 1
    model_execution["attempt_origin_counts"][attempt_origin] += 1
    if attempt_origin == "model_retry":
        model_execution["model_retries"] += 1
    agent.run_store.write_task_state(task_state)
    prompt_started_at = time.monotonic()
    request, request_metadata = agent.context_manager.build_request(
        injection_snapshot=injection_snapshot,
        injection_telemetry=injection_telemetry,
        preflight_metadata=preflight_metadata,
        runtime_feedback=runtime_feedback,
    )
    recall_paths = list(request_metadata.pop("recall_commit_paths", []) or [])
    agent.last_request_metadata = dict(request_metadata)
    if recall_paths:
        recent = list(agent.session.get("recently_recalled") or [])
        skip_turns = int((getattr(agent, "context_config", {}) or {}).get("recall", {}).get("skip_recent_turns", 2))
        agent.session["recently_recalled"] = (recent + [recall_paths])[-(skip_turns + 1):]
        _commit_session(agent)
    if attempts == 1:
        task_state.resume_status = request_metadata.get(
            "resume_status",
            task_state.resume_status,
        )
    agent.emit_trace(
        task_state,
        "prompt_built",
        {
            "request_metadata": request_metadata,
            "duration_ms": int(
                (time.monotonic() - prompt_started_at) * 1000
            ),
        },
    )
    if (
        attempts == 1
        and request_metadata.get("resume_status")
        == CHECKPOINT_PARTIAL_STALE_STATUS
    ):
        _create_resume_checkpoint(
            agent,
            task_state,
            user_message,
            trigger="freshness_mismatch",
        )
    elif (
        attempts == 1
        and request_metadata.get("resume_status")
        == CHECKPOINT_WORKSPACE_MISMATCH_STATUS
    ):
        agent.emit_trace(
            task_state,
            "runtime_identity_mismatch",
            {
                "fields": list(
                    request_metadata.get(
                        "runtime_identity_mismatch_fields",
                        [],
                    )
                ),
            },
        )
        _create_resume_checkpoint(
            agent,
            task_state,
            user_message,
            trigger="workspace_mismatch",
        )
    if (
        request_metadata["dropped_messages"] > 0
        and not context_reduction_checkpoint_created
    ):
        _create_resume_checkpoint(
            agent,
            task_state,
            user_message,
            trigger="context_reduction",
        )
        context_reduction_checkpoint_created = True
    agent.emit_trace(
        task_state,
        "model_requested",
        {
            "attempts": task_state.attempts,
            "tool_steps": task_state.tool_steps,
            "attempt_origin": attempt_origin,
            "request_metadata": request_metadata,
        },
    )
    return (
        request,
        request_metadata,
        prompt_started_at,
        context_reduction_checkpoint_created,
    )


def _complete_model_attempt(
    agent,
    task_state,
    request,
    request_metadata,
    prompt_started_at,
    completion_usage_totals,
    model_execution,
    attempt_origin,
):
    try:
        response = agent.model_client.complete(
            system=request["system"],
            tools=request["tools"],
            messages=request["messages"],
            max_tokens=agent.max_new_tokens,
            cache_breakpoints=request["cache_control_breakpoints"],
        )
    except KeyboardInterrupt as exc:
        _record_model_failure(
            agent,
            task_state,
            model_execution,
            attempt_origin=attempt_origin,
            outcome="interrupted",
            failure_phase="provider_complete",
            error=exc,
        )
        raise
    except Exception as exc:
        _record_model_failure(
            agent,
            task_state,
            model_execution,
            attempt_origin=attempt_origin,
            outcome="error",
            failure_phase="provider_complete",
            error=exc,
        )
        return None, None, exc

    completion_usage = dict(response.usage or {})
    _add_usage(completion_usage_totals, completion_usage)
    try:
        action, blocked_tool_result = _sanitize_action(
            agent,
            decode_action(response),
        )
    except Exception as exc:
        _record_model_failure(
            agent,
            task_state,
            model_execution,
            attempt_origin=attempt_origin,
            outcome="error",
            failure_phase="response_processing",
            error=exc,
        )
        raise
    transport_attempts, transport_retries, evidence_complete = _transport_evidence(
        agent.model_client,
    )
    action_payload = _action_trace_payload(action)
    try:
        agent.emit_trace(
            task_state,
            "action_decoded",
            {
                **action_payload,
                "request_metadata": request_metadata,
            },
        )
        agent.emit_trace(
            task_state,
            "model_turn",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "attempt_origin": attempt_origin,
                "stop_reason": str(
                    getattr(response.stop_reason, "value", response.stop_reason)
                ),
                "request_metadata": request_metadata,
                "completion_usage": completion_usage,
                "transport_attempts": transport_attempts,
                "transport_retries": transport_retries,
                "transport_evidence_complete": evidence_complete,
                **action_payload,
                "duration_ms": int(
                    (time.monotonic() - prompt_started_at) * 1000
                ),
            },
        )
    except Exception as exc:
        _record_model_failure(
            agent,
            task_state,
            model_execution,
            attempt_origin=attempt_origin,
            outcome="error",
            failure_phase="response_processing",
            error=exc,
        )
        raise
    _record_transport(model_execution, agent.model_client)
    model_execution["model_turns"] += 1
    return action, blocked_tool_result, None


def _record_tool_report(agent, name, metadata, model_execution):
    tool_report = model_execution["tool_report"]
    tool_status = str(metadata["tool_status"])
    tool_report["calls"] += 1
    tool_report["allowed" if tool_status != "rejected" else "denied"] += 1
    tool_report["name_counts"][name] = (
        tool_report["name_counts"].get(name, 0) + 1
    )
    tool_report["status_counts"][tool_status] = (
        tool_report["status_counts"].get(tool_status, 0) + 1
    )
    for path in metadata.get("affected_paths", []):
        if isinstance(path, str) and path not in tool_report["changed_paths"]:
            tool_report["changed_paths"].append(path)
    if tool_status == "partial_success":
        tool_report["partial_successes"] += 1
    if (
        metadata.get("recovery_review_required") is True
        or metadata.get("tool_error_code") == "recovery_review_required"
        or tool_status in {"interrupted", "partial_success"}
    ):
        tool_report["recovery_review_required"] = True

    sandbox = metadata.get("sandbox")
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    sandbox_status = str(sandbox.get("status", "") or "")
    sandbox_outcome_observed = sandbox_status not in {
        "",
        "not_applicable",
        "not_started",
        "pending",
    }
    if sandbox_outcome_observed:
        tool_report["sandbox_calls"] += 1
        outcome_counts = tool_report["sandbox_outcome_counts"]
        outcome_counts[sandbox_status] = outcome_counts.get(sandbox_status, 0) + 1
        if sandbox.get("target_started") is True:
            tool_report["sandbox_target_started_count"] += 1
        if sandbox.get("cleanup_status") not in {
            None,
            "",
            "completed",
            "not_applicable",
            "pending",
        }:
            tool_report["sandbox_cleanup_failure_count"] += 1
    command_approval = metadata.get("command_approval")
    runner_executed = (
        isinstance(command_approval, dict)
        and command_approval.get("runner_executed") is True
    )
    execution_plane = str(sandbox.get("execution_plane", "") or "")
    if (
        name == "run_shell"
        and agent.sandbox_context is not None
        and (
            execution_plane == "host"
            or (runner_executed and execution_plane != "sandbox")
        )
    ):
        tool_report["host_fallback_count"] += 1


def _sandbox_trace_payload(metadata):
    sandbox = metadata.get("sandbox")
    if not isinstance(sandbox, dict) or sandbox.get("status") in {
        None,
        "",
        "not_applicable",
        "not_started",
        "pending",
    }:
        return {}
    payload = {
        "sandbox_outcome": sandbox.get("status"),
        "execution_plane": sandbox.get("execution_plane") or "unknown",
        "cleanup_status": sandbox.get("cleanup_status") or "unknown",
        "sandbox_wrapper_status": sandbox.get("wrapper_status"),
        "sandbox_error_code": sandbox.get("error_code"),
        "sandbox_call_id": sandbox.get("call_id"),
        "execution_plan_digest": sandbox.get("execution_plan_digest"),
        "logical_intent_digest": sandbox.get("logical_intent_digest"),
        "policy_digest": sandbox.get("policy_digest"),
        "target_started": sandbox.get("target_started") is True,
        "timed_out": sandbox.get("timed_out"),
        "residue_detected": sandbox.get("residue_detected"),
        "container_created": sandbox.get("container_created"),
        "runner_executed": sandbox.get("runner_executed"),
        "stdout_bytes": sandbox.get("stdout_bytes"),
        "stderr_bytes": sandbox.get("stderr_bytes"),
        "stdout_truncated": sandbox.get("stdout_truncated"),
        "stderr_truncated": sandbox.get("stderr_truncated"),
        "exit_code": sandbox.get("exit_code"),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _apply_tool_action(
    agent,
    task_state,
    user_message,
    action,
    blocked_tool_result,
    run_tool_change_ids,
    run_verification_evidence,
    model_execution,
):
    name = action.name
    args = action.arguments
    tool_use_id = action.tool_use_id or f"toolu_{uuid.uuid4().hex[:12]}"
    tool_started_at = time.monotonic()
    agent.emit_trace(
        task_state,
        "tool_started",
        {"name": name, "args": args, "tool_use_id": tool_use_id},
    )
    working_memory_before = deepcopy(agent.memory)
    file_summaries_before = deepcopy(
        agent.session["memory"]["file_summaries"]
    )
    if blocked_tool_result is None:
        agent._last_tool_result_metadata = {}
        try:
            tool_result = agent.execute_tool(name, args)
        except BaseException:
            metadata = dict(agent._last_tool_result_metadata or {})
            if metadata.get("tool_status"):
                _record_tool_report(agent, name, metadata, model_execution)
                tool_change_id = str(metadata.get("tool_change_id", "") or "")
                if (
                    tool_change_id
                    and metadata.get("effect_class") == "workspace_write"
                ):
                    run_tool_change_ids.append(tool_change_id)
                try:
                    agent.emit_trace(
                        task_state,
                        "tool_interrupted",
                        {
                            "name": name,
                            "tool_use_id": tool_use_id,
                            "tool_change_id": tool_change_id,
                            "tool_status": metadata["tool_status"],
                            "affected_paths": list(
                                metadata.get("affected_paths", [])
                            ),
                            **_sandbox_trace_payload(metadata),
                        },
                    )
                except BaseException:
                    logger.debug("tool_interrupted trace failed", exc_info=True)
            raise
    else:
        tool_result = blocked_tool_result
        agent._last_tool_result_metadata = dict(tool_result.metadata)
    result = tool_result.content
    metadata = dict(tool_result.metadata or {})
    _record_tool_report(agent, name, metadata, model_execution)
    verification_evidence = _verification_evidence_for_tool(name, metadata)
    if verification_evidence is not None:
        run_verification_evidence.append(verification_evidence)
    tool_change_id = str(metadata.get("tool_change_id", "") or "")
    effect_class = str(metadata["effect_class"])
    if tool_change_id and effect_class == "workspace_write":
        run_tool_change_ids.append(tool_change_id)

    display_result, digest_meta = _prepare_tool_result(
        agent,
        content=result,
        tool_name=name,
        tool_args=args,
    )
    if blocked_tool_result is not None:
        digest_meta.update(
            {
                "tool_error_code": metadata["tool_error_code"],
                "security_event_type": metadata["security_event_type"],
            }
        )
    pair = make_tool_pair(
        name=name,
        arguments=args,
        tool_use_id=tool_use_id,
        result_content=display_result,
        created_at=now(),
        tool_status=metadata["tool_status"],
        effect_class=effect_class,
        tool_change_id=tool_change_id,
        result_meta=digest_meta,
    )
    try:
        _commit_session(agent, messages=pair)
    except SessionCommitError as exc:
        if not exc.committed:
            agent.memory = working_memory_before
            agent._sync_working_memory()
            agent.session["memory"]["file_summaries"] = file_summaries_before
        raise

    consumed_step = metadata.get("tool_status") != "rejected"
    if consumed_step:
        task_state.record_tool(name)
    agent.run_store.write_task_state(task_state)
    agent.emit_trace(
        task_state,
        "tool_executed",
        {
            "name": name,
            "args": args,
            "result": clip(result, 500),
            "tool_use_id": tool_use_id,
            "duration_ms": int(
                (time.monotonic() - tool_started_at) * 1000
            ),
            **metadata,
            **_sandbox_trace_payload(metadata),
        },
    )
    agent.emit_trace(
        task_state,
        "tool_finished",
        {
            "name": name,
            "tool_change_id": tool_change_id,
            "tool_use_id": tool_use_id,
            "tool_status": metadata.get("tool_status", ""),
            "affected_paths": list(metadata.get("affected_paths", [])),
            "duration_ms": int(
                (time.monotonic() - tool_started_at) * 1000
            ),
        },
    )
    _create_resume_checkpoint(
        agent,
        task_state,
        user_message,
        trigger="tool_executed",
    )
    return int(consumed_step)


def _run_agent_attempts(
    agent,
    task_state,
    user_message,
    run_tool_change_ids,
    run_verification_evidence,
    completion_usage_totals,
    model_execution,
):
    (
        preflight_metadata,
        injection_snapshot,
        injection_telemetry,
    ) = _start_agent_run(agent, task_state, user_message)
    runtime_feedback = ""
    context_reduction_checkpoint_created = False
    tool_steps = 0
    attempts = 0
    attempt_origin = "initial"
    model_retry_count = 0
    retry_action_count = 0
    max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

    while tool_steps < agent.max_steps and attempts < max_attempts:
        attempts += 1
        (
            request,
            request_metadata,
            prompt_started_at,
            context_reduction_checkpoint_created,
        ) = _build_attempt_request(
            agent,
            task_state,
            user_message,
            attempts,
            runtime_feedback,
            injection_snapshot,
            injection_telemetry,
            preflight_metadata,
            context_reduction_checkpoint_created,
            attempt_origin,
            model_execution,
        )
        action, blocked_result, model_error = _complete_model_attempt(
            agent,
            task_state,
            request,
            request_metadata,
            prompt_started_at,
            completion_usage_totals,
            model_execution,
            attempt_origin,
        )
        if model_error is not None:
            if (
                isinstance(model_error, _ProviderFailure)
                and model_error.retryable
                and model_retry_count < len(_MODEL_RETRY_DELAYS)
                and attempts < max_attempts
            ):
                time.sleep(_MODEL_RETRY_DELAYS[model_retry_count])
                model_retry_count += 1
                attempt_origin = "model_retry"
                continue
            final = _RUNTIME_TERMINAL_TEXT["model_error"]
            task_state.stop_model_error(final)
            return (
                final,
                "model_error",
                _runtime_terminal_message("model_error"),
                model_error,
            )
        model_retry_count = 0
        runtime_feedback = ""
        if isinstance(action, ToolAction):
            tool_steps += _apply_tool_action(
                agent,
                task_state,
                user_message,
                action,
                blocked_result,
                run_tool_change_ids,
                run_verification_evidence,
                model_execution,
            )
            attempt_origin = "tool_followup"
            continue
        if isinstance(action, RetryAction):
            if retry_action_count >= 1:
                final = (
                    "Stopped after repeated malformed model responses without "
                    "a valid tool call or final answer."
                )
                task_state.stop_retry_limit(final)
                _commit_session(
                    agent,
                    messages=(_plain_message("assistant", final),),
                )
                return final, task_state.stop_reason, None, None
            retry_action_count += 1
            runtime_feedback = action.notice
            agent.run_store.write_task_state(task_state)
            attempt_origin = "retry_action"
            continue

        final = agent.redact_text(action.text)
        _commit_session(
            agent,
            messages=(_plain_message("assistant", final),),
        )
        task_state.finish_success(final)
        return final, "run_finished", None, None

    if attempts >= max_attempts and tool_steps < agent.max_steps:
        final = (
            "Stopped after too many malformed model responses without a valid "
            "tool call or final answer."
        )
        task_state.stop_retry_limit(final)
    else:
        final = "Stopped after reaching the step limit without a final answer."
        task_state.stop_step_limit(final)
    _commit_session(
        agent,
        messages=(_plain_message("assistant", final),),
    )
    return final, task_state.stop_reason or "run_stopped", None, None


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
        agent = self.agent
        user_message = agent.redact_text(user_message)
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        agent._sync_working_memory()
        try:
            _commit_session(
                agent,
                messages=(_plain_message("user", user_message),),
            )
        except SessionCommitError as exc:
            raise exc.cause

        task_state = TaskState.create(
            run_id=agent.new_run_id(),
            task_id=agent.new_task_id(),
            user_request=user_message,
        )
        task_state.resume_status = agent.resume_state.get(
            "status",
            CHECKPOINT_NONE_STATUS,
        )
        agent.current_task_state = task_state
        agent.last_request_metadata = {}
        run_tool_change_ids = []
        run_verification_evidence = []
        completion_usage_totals = _empty_usage_totals()
        model_execution = _empty_model_execution()
        try:
            outcome = _run_agent_attempts(
                agent,
                task_state,
                user_message,
                run_tool_change_ids,
                run_verification_evidence,
                completion_usage_totals,
                model_execution,
            )
        except KeyboardInterrupt as primary:
            final = _RUNTIME_TERMINAL_TEXT["interrupted"]
            task_state.stop_interrupted(final)
            outcome = (
                final,
                "interrupted",
                _runtime_terminal_message("interrupted"),
                primary,
            )
        except SessionCommitError as exc:
            primary = exc.cause
            final = _RUNTIME_TERMINAL_TEXT["persistence_error"]
            if task_state.stop_reason != STOP_REASON_PERSISTENCE_ERROR:
                task_state.stop_persistence_error(final)
            outcome = (
                final,
                "persistence_error",
                _runtime_terminal_message("persistence_error"),
                primary,
            )
        except Exception as primary:
            final = _RUNTIME_TERMINAL_TEXT["runtime_error"]
            if task_state.status == STATUS_RUNNING:
                task_state.stop_runtime_error(final)
            outcome = (
                final,
                "runtime_error",
                _runtime_terminal_message("runtime_error"),
                primary,
            )

        final, trigger, terminal_message, primary = outcome
        result = _finalize_run(
            agent=agent,
            task_state=task_state,
            user_message=user_message,
            final=final,
            run_started_at=run_started_at,
            run_tool_change_ids=run_tool_change_ids,
            run_verification_evidence=run_verification_evidence,
            completion_usage_totals=completion_usage_totals,
            model_execution=model_execution,
            trigger=trigger,
            terminal_message=terminal_message,
            primary_exception=primary,
        )
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)
        return result


def _finalize_run(
    *,
    agent,
    task_state,
    user_message,
    final,
    run_started_at,
    run_tool_change_ids,
    run_verification_evidence,
    completion_usage_totals,
    model_execution,
    trigger,
    terminal_message=None,
    primary_exception=None,
):
    finalization_errors = []
    finalization_exceptions = []

    def attempt(label, operation):
        try:
            return operation()
        except Exception as exc:
            stored_exception = exc.cause if isinstance(exc, SessionCommitError) else exc
            finalization_exceptions.append(stored_exception)
            safe_message = agent.redact_text(str(stored_exception))
            finalization_errors.append(
                f"{label}: {type(stored_exception).__name__}: {safe_message}"[:300]
            )
            logger.debug(
                "run finalization step failed: %s (%s)",
                label,
                type(stored_exception).__name__,
            )
            return None

    if (
        terminal_message
        and not getattr(primary_exception, "committed", False)
        and not _session_writes_blocked(agent)
    ):
        attempt(
            "terminal_message",
            lambda: _commit_session(
                agent,
                messages=(terminal_message,),
            ),
        )
    attempt("task_state_write", lambda: agent.run_store.write_task_state(task_state))
    if not _session_writes_blocked(agent):
        attempt(
            "resume_checkpoint",
            lambda: _create_resume_checkpoint(
                agent,
                task_state,
                user_message,
                trigger=trigger,
            ),
        )
    recovery_checkpoint = None
    if not _session_writes_blocked(agent):
        recovery_checkpoint = attempt(
            "recovery_checkpoint",
            lambda: _finalize_recovery_checkpoint(
                agent,
                task_state,
                run_tool_change_ids,
                run_verification_evidence,
                trigger=trigger,
            ),
        )
    if recovery_checkpoint is not None:
        attempt(
            "recovery_checkpoint_trace",
            lambda: _emit_recovery_checkpoint_created(
                agent,
                task_state,
                recovery_checkpoint,
                trigger=trigger,
            ),
        )
        attempt(
            "verification_evidence",
            lambda: _record_pending_verification_evidence(
                agent,
                recovery_checkpoint,
                run_verification_evidence,
            ),
        )
    run_finished = attempt(
        "run_finished",
        lambda: agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                "finalization_errors": list(finalization_errors),
            },
        ),
    )
    model_execution["run_duration_ms"] = int(
        (time.monotonic() - run_started_at) * 1000
    )
    errors_before_report = len(finalization_errors)
    report = attempt(
        "report_build",
        lambda: agent.build_report(
            task_state,
            completion_usage_totals=completion_usage_totals,
            model_execution=model_execution,
        ),
    )
    if report is not None:
        report["finalization"] = {
            "status": "complete" if not finalization_errors else "incomplete",
            "error_count": len(finalization_errors),
        }
        attempt(
            "report_write",
            lambda: agent.run_store.write_report(
                task_state,
                agent.redact_artifact(report),
            ),
        )
    if run_finished is not None and len(finalization_errors) > errors_before_report:
        try:
            agent.emit_trace(
                task_state,
                "finalization_failed",
                {"finalization_errors": list(finalization_errors)},
            )
        except Exception as exc:
            logger.debug(
                "run finalization failure trace could not be written (%s)",
                type(exc).__name__,
            )
    if primary_exception is None and finalization_exceptions:
        raise finalization_exceptions[0]
    return final


def _create_resume_checkpoint(agent, task_state, user_message, trigger):
    try:
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
    except Exception as exc:
        _block_session_writes(agent, exc)
        if isinstance(exc, OSError) or getattr(exc, "committed", False):
            raise SessionCommitError(exc) from exc
        raise
    agent.run_store.write_task_state(task_state)
    agent.emit_trace(
        task_state,
        "checkpoint_created",
        {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "checkpoint_kind": "resume_summary",
            "trigger": trigger,
        },
    )
    return checkpoint


def _emit_recovery_checkpoint_created(agent, task_state, recovery_checkpoint, trigger):
    if recovery_checkpoint is None:
        return
    agent.emit_trace(
        task_state,
        TRACE_RECOVERY_CHECKPOINT_CREATED,
        {
            "checkpoint_id": recovery_checkpoint["checkpoint_id"],
            "recovery_checkpoint_id": recovery_checkpoint["checkpoint_id"],
            "checkpoint_kind": "recovery",
            "checkpoint_type": "turn",
            "trigger": trigger,
        },
    )


def _finalize_recovery_checkpoint(agent, task_state, run_tool_change_ids, run_verification_evidence, trigger):
    """把当前累计到的 Tool Change 打包成一份 Turn Checkpoint。

    只有真的有 Tool Change 时才写，避免为纯回答型 turn 产生空 checkpoint。
    写完后：
      - 把 checkpoint_id 记到 task_state.recovery_checkpoint_id
      - 更新 session.recovery.current_checkpoint_id
      - 清空累计列表，防止下一个 turn 重复写
    """
    if not run_tool_change_ids:
        return None
    ids_to_link = list(run_tool_change_ids)
    parent_checkpoint = current_recovery_checkpoint_id(agent.session)
    record = agent.recovery_checkpoint_writer.create_turn_checkpoint(
        session_id=agent.session["id"],
        run_id=task_state.run_id,
        turn_id=task_state.task_id,
        parent_checkpoint_id=parent_checkpoint,
        tool_change_ids=ids_to_link,
        verification_evidence=[],
    )
    task_state.recovery_checkpoint_id = record["checkpoint_id"]
    set_current_recovery_checkpoint_id(agent.session, record["checkpoint_id"])
    try:
        agent.session_path = agent.session_store.save(agent.session)
    except Exception as exc:
        _block_session_writes(agent, exc)
        raise
    agent.run_store.write_task_state(task_state)
    run_tool_change_ids.clear()
    return record


def _record_pending_verification_evidence(agent, recovery_checkpoint, run_verification_evidence):
    if recovery_checkpoint is None:
        return
    for evidence in list(run_verification_evidence or []):
        agent.record_verification_evidence(
            checkpoint_id=recovery_checkpoint["checkpoint_id"],
            **evidence,
        )
    if run_verification_evidence is not None:
        run_verification_evidence.clear()


def _verification_evidence_for_tool(name, metadata):
    if name != "run_shell" or not isinstance(metadata, dict):
        return None
    evidence = metadata.get("verification_evidence")
    if not isinstance(evidence, dict):
        return None
    return deepcopy(evidence)
