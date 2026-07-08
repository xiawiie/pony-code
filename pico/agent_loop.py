"""Agent control loop extracted from the runtime facade."""

import logging
import time
import uuid

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .providers.response import StopReason
from .recovery_models import TRACE_RECOVERY_CHECKPOINT_CREATED
from .recovery_checkpoint_writer import (
    current_recovery_checkpoint_id,
    set_current_recovery_checkpoint_id,
)
from .task_state import TaskState
from .verification import is_verification_command, parse_run_shell_result
from .workspace import clip, now

logger = logging.getLogger("pico")


def _append_user_turn(agent, text: str):
    """Append a plain-text user turn to session["messages"] via agent.record_message."""
    msg = {"role": "user", "content": text, "_pico_meta": {"created_at": now()}}
    agent.record_message(msg)
    return msg


def _append_tool_use(agent, *, name: str, input: dict, id_hint: str | None = None) -> str:
    """Append an assistant tool_use turn. Returns the tool_use_id (generated if id_hint None)."""
    tool_use_id = id_hint or f"toolu_{uuid.uuid4().hex[:12]}"
    msg = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": input}],
        "_pico_meta": {"created_at": now(), "tool_use_id": tool_use_id},
    }
    agent.record_message(msg)
    return tool_use_id


def _append_tool_result(
    agent,
    *,
    tool_use_id: str,
    content: str,
    tool_name: str = "",
    tool_args: dict | None = None,
    digest_applied: bool = False,
    source_hash: str | None = None,
):
    """Append a tool_result message. Anthropic semantics: role="user"
    wraps the tool_result content block.

    Task 26: when the raw ``content`` exceeds the digest threshold
    (see ``pico.context.digest.should_digest``), we:

    1. Write the raw body to ``<run_dir>/tool_results/<hash>.txt`` so a
       later turn can recover the full output on demand.
    2. Replace ``content`` with the rendered digest (title + bullets +
       "raw at ..." pointer) — the agent still sees the shape of the
       result, at a fraction of the token cost.
    3. Set ``_pico_meta.digest_applied = True`` and stash
       ``source_hash`` so trace / metrics can distinguish digested
       messages from inline ones.

    When ``agent.current_run_dir`` is unavailable (e.g. mid-test), we
    still emit the digest but leave ``raw_path`` empty — no crash.
    Callers can override the auto-digest by passing
    ``digest_applied=True`` up-front (used by explicit callers that
    have already digested the content themselves).
    """
    # Lazy import to avoid the agent_loop → context.digest → ... cycle risk.
    from pico.context.digest import (
        digest_tool_result,
        render_digest_content,
        should_digest,
    )

    display_content = content
    tool_args = tool_args or {}

    # Task B3: threshold overridable via pico.toml → agent.context_config.
    cfg = getattr(agent, "context_config", None)
    if not isinstance(cfg, dict):
        cfg = {}
    threshold = int(cfg.get("digest_size_threshold", 1200))
    # Only run the digest heuristic if the caller hasn't already digested.
    if not digest_applied and should_digest(content, threshold=threshold):
        # Compute the hash first so we know where the raw would land.
        source_hash = digest_tool_result(tool_name, tool_args, content, raw_path="").source_hash
        run_dir = getattr(agent, "current_run_dir", None)
        raw_path_str = ""
        if run_dir is not None:
            try:
                raw_dir = run_dir / "tool_results"
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_path = raw_dir / f"{source_hash}.txt"
                raw_path.write_text(content, encoding="utf-8")
                raw_path_str = str(raw_path)
            except OSError as exc:
                logger.debug("raw tool_result write failed: %s", exc)
                raw_path_str = ""
        digest = digest_tool_result(tool_name, tool_args, content, raw_path=raw_path_str)
        display_content = render_digest_content(digest)
        digest_applied = True

    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": display_content}
        ],
        "_pico_meta": {
            "created_at": now(),
            "tool_use_id": tool_use_id,
            "digest_applied": digest_applied,
            "source_hash": source_hash,
        },
    }
    agent.record_message(msg)
    return msg


def _append_assistant_text(agent, text: str):
    """Append a plain-text assistant turn."""
    msg = {"role": "assistant", "content": text, "_pico_meta": {"created_at": now()}}
    agent.record_message(msg)
    return msg


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        agent._sync_working_memory()
        # Task 28: session["messages"] is the primary transcript sent to
        # the model. A parallel session["history"] dual-write is retained
        # for legacy consumers — evaluation harness, runtime.build_report,
        # and a handful of tests that inspect the flat history structure —
        # until the memory/context redesign completes and those consumers
        # migrate to v2 message inspection.
        _append_user_turn(agent, user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        # 每次 tool 执行后，如果生成了 Tool Change Record，就把 id 收集起来；
        # 一次 run 结束（无论成功、step_limit、retry_limit）时把这些 id 打包成一份
        # Turn Checkpoint，写进 .pico/checkpoints/records。
        run_tool_change_ids = []
        run_verification_evidence = []

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：build_v2 把 system / tools / messages 组装成一次 v2 请求
        # 2. 决策：让 provider.complete_v2 返回 Response（tool_use 或 text）
        # 3. 行动：如果是 tool_use，就 execute_tool 并把 tool_result 追加回 messages
        # 4. 记录：把结果写回 messages / history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足。
        while tool_steps < agent.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            # 依旧调用 _build_prompt_and_metadata 是为了两件事：
            # 1) 触发 resume_state / prefix_refresh 的副作用；
            # 2) 拿到富元数据（resume_status / budget_reductions / prompt_cache_key
            #    等）用于 trace、checkpoint 决策与报表；
            # 真正发给模型的请求由 build_v2 组装，二者独立、互不覆盖。
            _, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            request, v2_metadata = agent.context_manager.build_v2(user_message)
            # v2 特有的 metadata（system_cache_key / messages_count / breakpoints）
            # 也并入 prompt_metadata，让 trace/report 能观察 v2 请求的真实形状。
            # 但不要覆盖已存在的键（例如 prompt_cache_key）：build_v2 会把
            # `prompt_cache_key` alias 到 system_cache_key，与旧路径的 prefix_hash
            # 冲突。旧路径的 prompt_cache_key 依旧代表稳定 prefix 的哈希，
            # 是 Task 8 之前统一的 cache key 语义。
            for key, value in v2_metadata.items():
                prompt_metadata.setdefault(key, value)
            if attempts == 1:
                task_state.resume_status = prompt_metadata.get("resume_status", task_state.resume_status)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                _create_resume_checkpoint(agent, task_state, user_message, trigger="freshness_mismatch")
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                _create_resume_checkpoint(agent, task_state, user_message, trigger="workspace_mismatch")
            if prompt_metadata.get("budget_reductions"):
                _create_resume_checkpoint(agent, task_state, user_message, trigger="context_reduction")
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            model_started_at = time.monotonic()
            try:
                raw_response = agent.model_client.complete_v2(
                    system=request["system"],
                    tools=request["tools"],
                    messages=request["messages"],
                    max_tokens=agent.max_new_tokens,
                    cache_breakpoints=request["cache_control_breakpoints"],
                )
            except RuntimeError as exc:
                agent.last_completion_metadata = {}
                agent.last_prompt_metadata = prompt_metadata
                final = f"Model error: {exc}"
                task_state.stop_model_error(final)
                _finish_run(
                    agent=agent,
                    task_state=task_state,
                    user_message=user_message,
                    final=final,
                    run_started_at=run_started_at,
                    run_tool_change_ids=run_tool_change_ids,
                    run_verification_evidence=run_verification_evidence,
                    trigger="model_error",
                )
                raise
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata

            # 解析 Response.content：过滤成 text_blocks / tool_use_blocks，
            # tool_use 优先（Anthropic END_TURN 也可能同时带 text 与 tool_use，
            # 但 STOP_REASON=tool_use 意味着模型希望我们执行工具）。
            content_blocks = list(raw_response.content or [])
            text_blocks = [b for b in content_blocks if isinstance(b, dict) and b.get("type") == "text"]
            tool_use_blocks = [b for b in content_blocks if isinstance(b, dict) and b.get("type") == "tool_use"]

            has_text = any(str(b.get("text", "")).strip() for b in text_blocks)
            if tool_use_blocks:
                kind = "tool"
            elif raw_response.stop_reason == StopReason.STOP_SEQUENCE:
                # STOP_SEQUENCE 在 v2 语义里表示“这一轮没有拿出可执行的动作”
                # （FallbackAdapter 也用它承载 retry/malformed notice）。
                # 即便 text 有内容也不视为 final，避免把 retry notice 当成答案返回。
                kind = "retry"
            elif raw_response.stop_reason == StopReason.END_TURN and has_text:
                kind = "final"
            elif raw_response.stop_reason == StopReason.MAX_TOKENS and has_text:
                # 沿用旧循环对 MAX_TOKENS 的宽松处理：还有内容就落地成 final。
                kind = "final"
            else:
                kind = "retry"

            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "stop_reason": str(getattr(raw_response.stop_reason, "value", raw_response.stop_reason) or ""),
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )
            # 把一轮 model call 的 prompt 组装、请求、回包解析压成一条 model_turn，
            # 方便下游的 replay 和排查按“逻辑轮”遍历，不用挨个匹配三个事件。
            agent.emit_trace(
                task_state,
                "model_turn",
                {
                    "attempts": task_state.attempts,
                    "kind": kind,
                    "stop_reason": str(getattr(raw_response.stop_reason, "value", raw_response.stop_reason) or ""),
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                    "prompt_metadata": prompt_metadata,
                    "completion_metadata": completion_metadata,
                },
            )

            if kind == "tool":
                tool_block = tool_use_blocks[0]
                name = str(tool_block.get("name", ""))
                args = dict(tool_block.get("input", {}) or {})
                tool_use_id = _append_tool_use(
                    agent,
                    name=name,
                    input=args,
                    id_hint=tool_block.get("id"),
                )
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
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                if tool_result.metadata.get("tool_status") != "rejected":
                    tool_steps += 1
                    task_state.record_tool(name)
                tool_change_id = tool_result.metadata.get("tool_change_id") or ""
                if tool_change_id:
                    run_tool_change_ids.append(tool_change_id)
                _append_tool_result(
                    agent,
                    tool_use_id=tool_use_id,
                    content=result,
                    tool_name=name,
                    tool_args=args,
                )
                # Dual-write to legacy history for tests + runtime helpers
                # that still key on the flat structure. Task 28 kept this
                # deliberately; the surface will be retired once memory
                # experiments and legacy assertions migrate to v2.
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "tool_use_id": tool_use_id,
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),
                    },
                )
                agent.emit_trace(
                    task_state,
                    "tool_finished",
                    {
                        "name": name,
                        "tool_change_id": tool_change_id,
                        "tool_use_id": tool_use_id,
                        "tool_status": tool_result.metadata.get("tool_status", ""),
                        "affected_paths": list(tool_result.metadata.get("affected_paths", [])),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                    },
                )
                _create_resume_checkpoint(agent, task_state, user_message, trigger="tool_executed")
                verification_evidence = _verification_evidence_for_tool(
                    name,
                    args,
                    result,
                    tool_result.metadata,
                )
                if verification_evidence is not None:
                    run_verification_evidence.append(verification_evidence)
                continue

            if kind == "retry":
                # retry: log a diagnostic notice to legacy history (tests +
                # trace consumers look for it). We do NOT append to v2
                # messages — back-to-back assistant turns would violate
                # Anthropic API constraints.
                retry_text = ""
                for block in text_blocks:
                    candidate = str(block.get("text", "") or "").strip()
                    if candidate:
                        retry_text = candidate
                        break
                if not retry_text:
                    retry_text = (
                        "Runtime notice: model returned an empty response. "
                        "Reply with a tool call or a final answer."
                    )
                agent.record({"role": "assistant", "content": retry_text, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                continue

            # final path
            final_text = ""
            for block in text_blocks:
                candidate = str(block.get("text", "") or "").strip()
                if candidate:
                    final_text = candidate
                    break
            final = final_text
            _append_assistant_text(agent, final)
            # Dual-write to legacy history for report/test consumers.
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            return _finish_run(
                agent=agent,
                task_state=task_state,
                user_message=user_message,
                final=final,
                run_started_at=run_started_at,
                run_tool_change_ids=run_tool_change_ids,
                run_verification_evidence=run_verification_evidence,
                trigger="run_finished",
            )

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        _append_assistant_text(agent, final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        final_trigger = task_state.stop_reason or "run_stopped"
        return _finish_run(
            agent=agent,
            task_state=task_state,
            user_message=user_message,
            final=final,
            run_started_at=run_started_at,
            run_tool_change_ids=run_tool_change_ids,
            run_verification_evidence=run_verification_evidence,
            trigger=final_trigger,
        )


def _finish_run(
    *,
    agent,
    task_state,
    user_message,
    final,
    run_started_at,
    run_tool_change_ids,
    run_verification_evidence,
    trigger,
):
    agent.run_store.write_task_state(task_state)
    _create_resume_checkpoint(agent, task_state, user_message, trigger=trigger)
    recovery_checkpoint = _finalize_recovery_checkpoint(
        agent, task_state, run_tool_change_ids, run_verification_evidence, trigger=trigger
    )
    _emit_recovery_checkpoint_created(agent, task_state, recovery_checkpoint, trigger=trigger)
    _record_pending_verification_evidence(agent, recovery_checkpoint, run_verification_evidence)
    agent.emit_trace(
        task_state,
        "run_finished",
        {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": final,
            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
        },
    )
    agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
    return final


def _create_resume_checkpoint(agent, task_state, user_message, trigger):
    checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
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
