"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .recovery_checkpoint_writer import (
    current_recovery_checkpoint_id,
    set_current_recovery_checkpoint_id,
)
from .task_state import TaskState
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
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

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < agent.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "checkpoint_kind": "resume_summary",
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "checkpoint_kind": "resume_summary",
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "checkpoint_kind": "resume_summary",
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            raw = agent.model_client.complete(
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            kind, payload = agent.parse(raw)
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
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
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                    "prompt_metadata": prompt_metadata,
                    "completion_metadata": completion_metadata,
                },
            )

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                task_state.record_tool(name)
                tool_started_at = time.monotonic()
                agent.emit_trace(
                    task_state,
                    "tool_started",
                    {
                        "name": name,
                        "args": args,
                    },
                )
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                tool_change_id = tool_result.metadata.get("tool_change_id") or ""
                if tool_change_id:
                    run_tool_change_ids.append(tool_change_id)
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
                        "tool_status": tool_result.metadata.get("tool_status", ""),
                        "affected_paths": list(tool_result.metadata.get("affected_paths", [])),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "checkpoint_kind": "resume_summary",
                        "trigger": "tool_executed",
                    },
                )
                recovery_checkpoint = _finalize_recovery_checkpoint(
                    agent, task_state, run_tool_change_ids, trigger="tool_executed"
                )
                if recovery_checkpoint is not None:
                    agent.emit_trace(
                        task_state,
                        "checkpoint_created",
                        {
                            "checkpoint_id": recovery_checkpoint["checkpoint_id"],
                            "recovery_checkpoint_id": recovery_checkpoint["checkpoint_id"],
                            "checkpoint_kind": "recovery",
                            "checkpoint_type": "turn",
                            "trigger": "tool_executed",
                        },
                    )
                continue

            if kind == "retry":
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                continue

            final = (payload or raw).strip()
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "checkpoint_kind": "resume_summary",
                    "trigger": "run_finished",
                },
            )
            recovery_checkpoint = _finalize_recovery_checkpoint(
                agent, task_state, run_tool_change_ids, trigger="run_finished"
            )
            if recovery_checkpoint is not None:
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": recovery_checkpoint["checkpoint_id"],
                        "recovery_checkpoint_id": recovery_checkpoint["checkpoint_id"],
                        "checkpoint_kind": "recovery",
                        "checkpoint_type": "turn",
                        "trigger": "run_finished",
                    },
                )
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

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "checkpoint_kind": "resume_summary",
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        recovery_checkpoint = _finalize_recovery_checkpoint(
            agent, task_state, run_tool_change_ids, trigger=task_state.stop_reason or "run_stopped"
        )
        if recovery_checkpoint is not None:
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": recovery_checkpoint["checkpoint_id"],
                    "recovery_checkpoint_id": recovery_checkpoint["checkpoint_id"],
                    "checkpoint_kind": "recovery",
                    "checkpoint_type": "turn",
                    "trigger": task_state.stop_reason or "run_stopped",
                },
            )
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


def _finalize_recovery_checkpoint(agent, task_state, run_tool_change_ids, trigger):
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
