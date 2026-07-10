"""Agent control loop extracted from the runtime facade."""

from copy import deepcopy
import logging
import os
import stat
import time
import uuid

from .action_codec import FinalAction, RetryAction, ToolAction, decode_action
from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .context.renderer import render_current_user_message
from .messages import append_messages, make_tool_pair
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
from .verification import is_verification_command, parse_run_shell_result
from .workspace import clip, now

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


def _empty_usage_totals():
    return {**{key: 0 for key in _USAGE_SUM_KEYS}, "cache_hit": False}


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


class SessionCommitError(RuntimeError):
    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def _commit_session(agent, *, messages=()):
    candidate = agent.redact_artifact(deepcopy(agent.session))
    safe_messages = tuple(agent.redact_artifact(message) for message in messages)
    candidate["messages"] = append_messages(candidate.get("messages", []), *safe_messages)
    try:
        saved_path = agent.session_store.save(candidate)
    except Exception as exc:
        raise SessionCommitError(exc) from exc
    agent.session = candidate
    agent.session_path = saved_path
    agent.memory = type(agent.memory).from_dict(
        candidate.get("working_memory"),
        workspace_root=agent.root,
    )


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
       "raw at ..." pointer) — the agent still sees the shape of the
       result, at a fraction of the token cost.
    3. Return ``digest_applied`` and ``source_hash`` so the atomic pair
       commit can distinguish digested messages from inline ones.

    When ``agent.current_run_dir`` is unavailable (e.g. mid-test), we
    still emit the digest but leave ``raw_path`` empty — no crash.
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
        # summarizer runs exactly once); then update raw_path on the
        # dataclass via dataclasses.replace after we know where we wrote.
        from dataclasses import replace as _dc_replace
        digest = digest_tool_result(tool_name, tool_args, safe_content, raw_path="")
        source_hash = digest.source_hash
        run_dir = getattr(agent, "current_run_dir", None)
        raw_path_str = ""
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
                raw_path_str = str(raw_path)
            except (OSError, ValueError) as exc:
                logger.debug("raw tool_result write failed: %s", type(exc).__name__)
                raw_path_str = ""
        if raw_path_str:
            digest = _dc_replace(digest, raw_path=raw_path_str)
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

        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.last_request_metadata = {}
        # 每次 tool 执行后，如果生成了 Tool Change Record，就把 id 收集起来；
        # 一次 run 结束（无论成功、step_limit、retry_limit）时把这些 id 打包成一份
        # Turn Checkpoint，写进 .pico/checkpoints/records。
        run_tool_change_ids = []
        run_verification_evidence = []
        completion_usage_totals = _empty_usage_totals()
        try:
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
            injection_snapshot, injection_telemetry = render_current_user_message(
                agent,
                user_message,
            )
            pending_runtime_feedback = ""
            context_reduction_checkpoint_created = False

            tool_steps = 0
            attempts = 0
            max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

            while tool_steps < agent.max_steps and attempts < max_attempts:
                attempts += 1
                task_state.record_attempt()
                agent.run_store.write_task_state(task_state)
                prompt_started_at = time.monotonic()
                request, request_metadata = agent.context_manager.build_v2(
                    injection_snapshot=injection_snapshot,
                    injection_telemetry=injection_telemetry,
                    preflight_metadata=preflight_metadata,
                    runtime_feedback=pending_runtime_feedback,
                )
                pending_runtime_feedback = ""
                agent.last_request_metadata = dict(request_metadata)
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
                        "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
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
                        "request_metadata": request_metadata,
                    },
                )
                try:
                    raw_response = agent.model_client.complete_v2(
                        system=request["system"],
                        tools=request["tools"],
                        messages=request["messages"],
                        max_tokens=agent.max_new_tokens,
                        cache_breakpoints=request["cache_control_breakpoints"],
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    final = _RUNTIME_TERMINAL_TEXT["model_error"]
                    task_state.stop_model_error(final)
                    _finalize_run(
                        agent=agent,
                        task_state=task_state,
                        user_message=user_message,
                        final=final,
                        run_started_at=run_started_at,
                        run_tool_change_ids=run_tool_change_ids,
                        run_verification_evidence=run_verification_evidence,
                        completion_usage_totals=completion_usage_totals,
                        trigger="model_error",
                        terminal_message=_runtime_terminal_message("model_error"),
                        primary_exception=exc,
                    )
                    raise
                completion_usage = dict(raw_response.usage or {})
                _add_usage(completion_usage_totals, completion_usage)
                action = decode_action(raw_response)
                action_payload = _action_trace_payload(action)
                agent.emit_trace(
                    task_state,
                    "action_decoded",
                    {
                        **action_payload,
                        "request_metadata": request_metadata,
                    },
                )
                # 把一轮 model call 的 prompt 组装、请求、回包解析压成一条 model_turn，
                # 方便下游的 replay 和排查按“逻辑轮”遍历，不用挨个匹配三个事件。
                agent.emit_trace(
                    task_state,
                    "model_turn",
                    {
                        "attempts": task_state.attempts,
                        "tool_steps": task_state.tool_steps,
                        "stop_reason": str(
                            getattr(
                                raw_response.stop_reason,
                                "value",
                                raw_response.stop_reason,
                            )
                        ),
                        "request_metadata": request_metadata,
                        "completion_usage": completion_usage,
                        **action_payload,
                        "duration_ms": int(
                            (time.monotonic() - prompt_started_at) * 1000
                        ),
                    },
                )

                if isinstance(action, ToolAction):
                    name = action.name
                    args = action.arguments
                    tool_use_id = action.tool_use_id or f"toolu_{uuid.uuid4().hex[:12]}"
                    tool_started_at = time.monotonic()
                    agent.emit_trace(
                        task_state,
                        "tool_started",
                        {
                            "name": name,
                            "args": args,
                            "tool_use_id": tool_use_id,
                        },
                    )
                    working_memory_before = deepcopy(agent.memory)
                    file_summaries_before = deepcopy(
                        agent.session["memory"]["file_summaries"]
                    )
                    tool_result = agent.execute_tool(name, args)
                    result = tool_result.content
                    metadata = dict(tool_result.metadata or {})
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
                        _commit_session(
                            agent,
                            messages=pair,
                        )
                    except SessionCommitError:
                        agent.memory = working_memory_before
                        agent._sync_working_memory()
                        agent.session["memory"]["file_summaries"] = file_summaries_before
                        raise
                    if metadata.get("tool_status") != "rejected":
                        tool_steps += 1
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
                            "affected_paths": list(
                                metadata.get("affected_paths", [])
                            ),
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
                    verification_evidence = _verification_evidence_for_tool(
                        name,
                        args,
                        result,
                        metadata,
                    )
                    if verification_evidence is not None:
                        run_verification_evidence.append(verification_evidence)
                    continue

                if isinstance(action, RetryAction):
                    pending_runtime_feedback = action.notice
                    agent.run_store.write_task_state(task_state)
                    continue

                # final path
                final = action.text
                _commit_session(
                    agent,
                    messages=(_plain_message("assistant", final),),
                )
                task_state.finish_success(final)
                return _finalize_run(
                    agent=agent,
                    task_state=task_state,
                    user_message=user_message,
                    final=final,
                    run_started_at=run_started_at,
                    run_tool_change_ids=run_tool_change_ids,
                    run_verification_evidence=run_verification_evidence,
                    completion_usage_totals=completion_usage_totals,
                    trigger="run_finished",
                )

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
            final_trigger = task_state.stop_reason or "run_stopped"
            return _finalize_run(
                agent=agent,
                task_state=task_state,
                user_message=user_message,
                final=final,
                run_started_at=run_started_at,
                run_tool_change_ids=run_tool_change_ids,
                run_verification_evidence=run_verification_evidence,
                completion_usage_totals=completion_usage_totals,
                trigger=final_trigger,
            )
        except KeyboardInterrupt as exc:
            if task_state.status == STATUS_RUNNING:
                final = _RUNTIME_TERMINAL_TEXT["interrupted"]
                task_state.stop_interrupted(final)
                _finalize_run(
                    agent=agent,
                    task_state=task_state,
                    user_message=user_message,
                    final=final,
                    run_started_at=run_started_at,
                    run_tool_change_ids=run_tool_change_ids,
                    run_verification_evidence=run_verification_evidence,
                    completion_usage_totals=completion_usage_totals,
                    trigger="interrupted",
                    terminal_message=_runtime_terminal_message("interrupted"),
                    primary_exception=exc,
                )
            raise
        except SessionCommitError as exc:
            if task_state.stop_reason != STOP_REASON_PERSISTENCE_ERROR:
                final = _RUNTIME_TERMINAL_TEXT["persistence_error"]
                task_state.stop_persistence_error(final)
                _finalize_run(
                    agent=agent,
                    task_state=task_state,
                    user_message=user_message,
                    final=final,
                    run_started_at=run_started_at,
                    run_tool_change_ids=run_tool_change_ids,
                    run_verification_evidence=run_verification_evidence,
                    completion_usage_totals=completion_usage_totals,
                    trigger="persistence_error",
                    terminal_message=_runtime_terminal_message("persistence_error"),
                    primary_exception=exc.cause,
                )
            raise exc.cause
        except Exception as exc:
            if task_state.status == STATUS_RUNNING:
                final = _RUNTIME_TERMINAL_TEXT["runtime_error"]
                task_state.stop_runtime_error(final)
                _finalize_run(
                    agent=agent,
                    task_state=task_state,
                    user_message=user_message,
                    final=final,
                    run_started_at=run_started_at,
                    run_tool_change_ids=run_tool_change_ids,
                    run_verification_evidence=run_verification_evidence,
                    completion_usage_totals=completion_usage_totals,
                    trigger="runtime_error",
                    terminal_message=_runtime_terminal_message("runtime_error"),
                    primary_exception=exc,
                )
            raise


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
            finalization_errors.append(
                f"{label}: {type(stored_exception).__name__}: {stored_exception}"[:300]
            )
            logger.exception("run finalization step failed: %s", label)
            return None

    if terminal_message:
        attempt(
            "terminal_message",
            lambda: _commit_session(
                agent,
                messages=(terminal_message,),
            ),
        )
    attempt("task_state_write", lambda: agent.run_store.write_task_state(task_state))
    attempt(
        "resume_checkpoint",
        lambda: _create_resume_checkpoint(
            agent,
            task_state,
            user_message,
            trigger=trigger,
        ),
    )
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
    errors_before_report = len(finalization_errors)
    report = attempt(
        "report_build",
        lambda: agent.build_report(
            task_state,
            completion_usage_totals=completion_usage_totals,
        ),
    )
    if report is not None:
        report["finalization_errors"] = list(finalization_errors)
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
        except Exception:
            logger.exception("run finalization failure trace could not be written")
    if primary_exception is None and finalization_exceptions:
        raise finalization_exceptions[0]
    return final


def _create_resume_checkpoint(agent, task_state, user_message, trigger):
    try:
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
    except OSError as exc:
        raise SessionCommitError(exc) from exc
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
    agent.session_path = agent.session_store.save(agent.session)
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


def _verification_evidence_for_tool(name, args, result, metadata):
    if name != "run_shell":
        return None
    command = str(args.get("command", "")).strip()
    if not is_verification_command(command):
        return None
    parsed = parse_run_shell_result(result)
    return {
        "command": command,
        "risk_class": metadata.get("command_risk_class", ""),
        "exit_code": parsed["exit_code"],
        "stdout": parsed["stdout"],
        "stderr": parsed["stderr"],
    }
