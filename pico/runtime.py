"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import checkpoint as checkpointlib
from . import model_output_parser
from . import workspace_snapshot
from .features import memory as memorylib
from . import security as securitylib
from .checkpoint_store import CheckpointStore
from .context_manager import ContextManager
from .checkpoint import CHECKPOINT_NONE_STATUS
from .memory.block_store import BlockStore
from .memory.retrieval import Retrieval
from .prompt_prefix import build_prompt_prefix, tool_signature
from .repo_map import RepoMap
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager
from .run_store import RunStore
from .session_store import SessionStore
from .tool_change_recorder import ToolChangeRecorder
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from . import tools as toolkit
from .verification import new_verification_record
from .workspace import MAX_HISTORY, WorkspaceContext, clip, now
from .workspace_observer import WorkspaceObserver

DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_NEW_TOKENS = 2048
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}
__all__ = ["Pico", "SessionStore"]


# --- Inlined working-memory helper -----------------------------------------
# Task 8 retired `pico/working_memory.py` as a standalone module. The tiny
# task_summary + recent_files state it carried is still consumed internally by
# `checkpoint.py` (recent_files → checkpoint key_files) and the REPL /memory
# command, so we keep the same object shape here as a runtime-private helper.
# External callers should treat `agent.memory` as an implementation detail;
# the durable session-level record lives at `session["working_memory"]`.

_WORKING_TASK_SUMMARY_LIMIT = 300
_WORKING_RECENT_FILES_LIMIT = 8


def _working_truncate(text, limit):
    return str(text)[:limit]


def _working_normalize_task_summary(summary, limit):
    if summary is None:
        return ""
    return _working_truncate(str(summary).strip(), limit)


def _working_ensure_file_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _working_dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


class WorkingMemory:
    TASK_SUMMARY_LIMIT = _WORKING_TASK_SUMMARY_LIMIT
    RECENT_FILES_LIMIT = _WORKING_RECENT_FILES_LIMIT

    def __init__(self, task_summary="", recent_files=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.task_summary = _working_normalize_task_summary(task_summary, self.TASK_SUMMARY_LIMIT)
        self.recent_files = _working_dedupe_preserve_order(
            [self.canonical_path(path).strip() for path in _working_ensure_file_list(recent_files or [])]
        )[: self.RECENT_FILES_LIMIT]

    def to_dict(self):
        return {
            "task_summary": self.task_summary,
            "recent_files": list(self.recent_files),
        }

    @classmethod
    def from_dict(cls, data, workspace_root=None):
        if not isinstance(data, dict):
            return cls(workspace_root=workspace_root)

        source = data
        if isinstance(data.get("working"), dict):
            source = data["working"]

        task_summary = source.get("task_summary", source.get("task", ""))
        if not isinstance(task_summary, str):
            task_summary = ""
        recent_files = source.get("recent_files", source.get("files", []))
        return cls(task_summary=task_summary, recent_files=recent_files, workspace_root=workspace_root)

    def canonical_path(self, path):
        return memorylib.canonicalize_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        self.task_summary = _working_normalize_task_summary(summary, self.TASK_SUMMARY_LIMIT)
        return self

    def remember_file(self, path):
        path = self.canonical_path(path).strip()
        if not path:
            return self
        self.recent_files = [item for item in self.recent_files if item != path]
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[: self.RECENT_FILES_LIMIT]
        return self


def build_report_checkpoint_metadata(task_state, last_prompt_metadata):
    """Return a dict fragment to merge into report prompt_metadata.

    Preserves the invariant that the initial prompt-time resume_status is kept
    under last_prompt_resume_status when a later task_state.resume_status is
    promoted into report metadata.
    """
    fragment = dict(last_prompt_metadata)
    if task_state.resume_status:
        fragment.setdefault(
            "last_prompt_resume_status",
            fragment.get("resume_status", ""),
        )
        fragment["resume_status"] = task_state.resume_status
    return fragment


class Pico:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=DEFAULT_MAX_STEPS,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        depth=0,
        max_depth=1,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
    ):
        # v2 迁移：模型后端约定的接口是 `complete_v2(system, tools, messages, ...)`。
        # 不支持这个方法的老 provider（FakeModelClient / OllamaModelClient /
        # OpenAICompatibleModelClient / 老的 Anthropic 客户端等）在这里统一被
        # FallbackAdapter 包一层：外部看起来仍然是 complete_v2，内部把 v2 请求
        # 拍扁成 <tool>/<final> XML prompt，再让老 provider.complete() 处理。
        if not hasattr(model_client, "complete_v2"):
            from .providers.fallback_adapter import FallbackAdapter

            model_client = FallbackAdapter(model_client)
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".pico" / "runs")
        if hasattr(self.run_store, "set_redactor"):
            self.run_store.set_redactor(self.redact_artifact)
        if hasattr(self.session_store, "set_redactor"):
            self.session_store.set_redactor(self.redact_artifact)
        # 可恢复编辑（recoverable editing）的组件在这里就位。
        # 它们和 resume-summary 用的 `checkpointlib` 是两条独立的通路：
        # CheckpointStore 落在 .pico/checkpoints/ 下，专门记 turn/restore/manual 类型。
        self.checkpoint_store = CheckpointStore(self.root)
        self.tool_change_owner_id = "runtime_" + uuid.uuid4().hex[:12]
        self.tool_change_recorder = ToolChangeRecorder(self.checkpoint_store, owner_id=self.tool_change_owner_id)
        self.interrupted_tool_changes = (
            self.tool_change_recorder.mark_interrupted_pending(legacy_only=True)
            if self.depth == 0
            else []
        )
        self.recovery_checkpoint_writer = RecoveryCheckpointWriter(self.checkpoint_store, self.root)
        self.recovery_manager = RecoveryManager(self.checkpoint_store, self.root)
        self.workspace_observer = WorkspaceObserver(self.root)
        # ADR-0034: pico.toml 里 `[policy] max_blob_size` 是唯一在第一阶段生效的覆盖项。
        # 构造期解析一次并缓存，后续 snapshot_eligibility 调用都读这个值。
        from .config import project_max_blob_size

        self.project_max_blob_size = project_max_blob_size(self.root)
        # Task B2: gather context/memory config from pico.toml. Downstream
        # subsystems read via `self.agent.context_config[...]` with defaults
        # already baked in by the helper functions. Must be populated BEFORE
        # ContextManager is constructed below so build_v2 sees the overrides.
        from .config import (
            context_digest_size_threshold,
            context_history_floor_messages,
            context_history_soft_cap,
            context_injection_budget_ratio,
            context_system_tools_hard_cap,
            memory_field_boosts,
            memory_link_config,
            memory_recall_config,
        )

        self.context_config = {
            "history_soft_cap": context_history_soft_cap(self.root),
            "history_floor_messages": context_history_floor_messages(self.root),
            "injection_budget_ratio": context_injection_budget_ratio(self.root),
            "system_tools_hard_cap": context_system_tools_hard_cap(self.root),
            "digest_size_threshold": context_digest_size_threshold(self.root),
            "recall": memory_recall_config(self.root),
            "field_boosts": memory_field_boosts(self.root),
            "link_config": memory_link_config(self.root),
        }
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "schema_version": 2,
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "messages": [],
            "working_memory": {"task_summary": "", "recent_files": []},
            "memory": {"file_summaries": {}},
        }
        self._ensure_session_shape()
        self.memory = WorkingMemory.from_dict(self.session.get("working_memory"), workspace_root=self.root)
        self._sync_working_memory()
        # v2 memory subsystem: BlockStore/Retrieval/RepoMap 在 tool_context 里被 wire 给 memory/repo_lookup 工具
        workspace_memory_root = self.root / ".pico" / "memory"
        user_memory_root = Path.home() / ".pico" / "memory"
        self.memory_store = BlockStore(
            workspace_root=workspace_memory_root,
            user_root=user_memory_root,
        )
        self.memory_retrieval = Retrieval(
            self.memory_store,
            config={
                "field_boosts": self.context_config["field_boosts"],
                "link_config": self.context_config["link_config"],
            },
        )
        self.repo_map = RepoMap(repo_root=self.root)
        # 后台起首次扫描；tool_repo_lookup 自己也会在首次使用时 refresh_if_stale。
        # 只在顶层 Pico 起扫描；delegate（depth > 0）走 refresh_if_stale 惰性路径，
        # 避免一次 REPL 请求触发 N 个并发全仓 walk。
        if self.depth == 0:
            threading.Thread(target=self.repo_map.scan, daemon=True).start()
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        if not isinstance(self.session.get("history"), list):
            self.session["history"] = []
        if not isinstance(self.session.get("messages"), list):
            self.session["messages"] = []
        if not isinstance(self.session.get("recently_recalled"), list):
            self.session["recently_recalled"] = []
        existing_memory = self.session.get("memory")
        if not isinstance(existing_memory, dict):
            existing_memory = {}
        working_source = self.session.get("working_memory") or existing_memory or {}
        self.session["working_memory"] = WorkingMemory.from_dict(working_source, workspace_root=self.root).to_dict()
        self.session["memory"] = {
            "file_summaries": memorylib.normalize_file_summaries_dict(
                existing_memory.get("file_summaries", {}),
                workspace_root=self.root,
            )
        }
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        return checkpointlib.current_runtime_identity(self)

    def checkpoint_state(self):
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        summaries = self.session["memory"]["file_summaries"]
        invalidated = memorylib.invalidate_stale_file_summaries_dict(summaries, self.root)
        self.session["memory"] = {"file_summaries": summaries}
        return invalidated

    def _sync_working_memory(self):
        self.session["working_memory"] = self.memory.to_dict()
        return self.session["working_memory"]

    def evaluate_resume_state(self):
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpointlib.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return toolkit.build_tool_registry(self.tool_context())

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names()
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(self.allowed_tools)
        return {
            name: tool
            for name, tool in tools.items()
            if name in allowed
        }

    def tool_signature(self):
        return tool_signature(self.tools)

    def build_prefix(self):
        return build_prompt_prefix(workspace=self.workspace, tools=self.tools)

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return json.dumps(self.memory.to_dict(), sort_keys=True)

    def history_text(self):
        """Render the legacy session["history"] as a compact transcript.

        This method is transitional — it reads from ``session["history"]``
        (the flat, pre-v2 shape) rather than ``session["messages"]``.
        Kept for ``build_report`` and evaluation-harness compatibility;
        v2 telemetry uses ``metadata["messages_tokens"]`` and structured
        messages instead. Returns "- empty" (not "") when history is
        empty to distinguish "no runs yet" from "runs with no output".
        """
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(self.redact_artifact(item))
        self.session_path = self.session_store.save(self.session)

    def record_message(self, msg):
        """Append a v2-shaped message dict to session["messages"] and persist."""
        self.session["messages"].append(self.redact_artifact(msg))
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(secret_env_names=self.secret_env_names)

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(secret_env_names=self.secret_env_names)

    def secret_env_summary(self):
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(secret_env_names=self.secret_env_names)

    def redact_text(self, text):
        return securitylib.redact_text(text, secret_env_names=self.secret_env_names)

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(value, key=key, secret_env_names=self.secret_env_names)

    def shell_env(self):
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                # Task 8 consolidated the four synonyms (base/stable/prefix_hash + prompt_cache_key)
                # into a single `system_cache_key`. `prompt_cache_key` is kept as a one-release
                # alias so providers reaching for the old name keep working.
                "system_cache_key": metadata.get("system_cache_key", self.prefix_state.hash),
                "prompt_cache_key": metadata.get(
                    "prompt_cache_key",
                    metadata.get("system_cache_key", self.prefix_state.hash),
                ),
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def emit_trace(self, task_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(task_state, payload)
        return payload

    def capture_workspace_snapshot(self):
        return workspace_snapshot.capture_workspace_snapshot(self.root)

    @staticmethod
    def diff_workspace_snapshots(before, after):
        return workspace_snapshot.diff_workspace_snapshots(before, after)

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpointlib.create_checkpoint(self, task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        if not isinstance(args, dict):
            return
        result = self.redact_text(result)
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
            self._sync_working_memory()
        summaries = self.session["memory"]["file_summaries"]
        if name == "read_file":
            summary = memorylib.summarize_read_result(result)
            memorylib.set_file_summary_dict(summaries, canonical_path, summary, workspace_root=self.root)
        elif name in {"write_file", "patch_file"}:
            memorylib.invalidate_file_summary_dict(summaries, canonical_path, workspace_root=self.root)
        self.session["memory"] = {"file_summaries": summaries}

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    def execute_tool(self, name, args):
        result = self.tool_executor.execute(name, args)
        self._last_tool_result_metadata = dict(result.metadata)
        return result

    def record_verification_evidence(
        self,
        command,
        risk_class,
        exit_code,
        stdout,
        stderr,
        checkpoint_id="",
        trace_event_id="",
    ):
        """在指定 checkpoint 上附加一条 Verification Evidence。

        - 如果 checkpoint_id 为空，就挂在当前 turn 的 recovery checkpoint 上；
        - 记录同时写入 checkpoint record，并在 trace 里补一条 verification_recorded 事件。
        """
        target_id = str(checkpoint_id or "")
        if not target_id and self.current_task_state is not None:
            target_id = self.current_task_state.recovery_checkpoint_id
        record = new_verification_record(
            command=command,
            risk_class=risk_class,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            affected_checkpoint_id=target_id,
            trace_event_id=trace_event_id,
        )
        if target_id:
            checkpoint = self.checkpoint_store.load_checkpoint_record(target_id)
            checkpoint.setdefault("verification_evidence", []).append(record)
            self.checkpoint_store.write_checkpoint_record(checkpoint)
        if self.current_task_state is not None:
            self.emit_trace(
                self.current_task_state,
                "verification_recorded",
                {
                    "verification_id": record["verification_id"],
                    "command": record["command"],
                    "status": record["status"],
                    "checkpoint_id": target_id,
                },
            )
        return record

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是“模型会不会想调用工具”，而是
        “平台有没有在执行前把边界守住”。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的“模型决定要调用工具”之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        return self.execute_tool(name, args).content

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 不只挡 A-A-A，也挡 A-B-A-B-A 这种短窗口拉锯。
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if not tool_events:
            return False
        repeated_count = sum(
            1
            for item in tool_events[-6:]
            if item["name"] == name and item["args"] == args
        )
        return repeated_count >= 2

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        prompt_metadata = build_report_checkpoint_metadata(task_state, self.last_prompt_metadata)
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "task_state": task_state.to_dict(),
            "prompt_metadata": prompt_metadata,
            "working_memory": self.memory.to_dict(),
            "redacted_env": self.detected_secret_env_summary(),
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self.tool_context(), name, args)

    def tool_context(self):
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_delegate=self.spawn_delegate,
            memory_store=self.memory_store,
            memory_retrieval=self.memory_retrieval,
            repo_map=self.repo_map,
        )

    def spawn_delegate(self, args):
        task = str(args.get("task", "")).strip()
        child = Pico(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.session_store,
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.memory.set_task_summary(task)
        child._sync_working_memory()
        return "delegate_result:\n" + child.ask(task)

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self.tool_context(), args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self.tool_context(), args)

    def tool_search(self, args):
        return toolkit.tool_search(self.tool_context(), args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self.tool_context(), args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self.tool_context(), args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self.tool_context(), args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self.tool_context(), args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
        “这是工具调用”还是“这是最终答案”。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        return model_output_parser.parse_model_output(raw)

    @staticmethod
    def retry_notice(problem=None):
        return model_output_parser.retry_notice(problem)

    @staticmethod
    def parse_xml_tool(raw):
        return model_output_parser.parse_xml_tool(raw)

    @staticmethod
    def parse_attrs(text):
        return model_output_parser.parse_attrs(text)

    @staticmethod
    def extract(text, tag):
        return model_output_parser.extract(text, tag)

    @staticmethod
    def extract_raw(text, tag):
        return model_output_parser.extract_raw(text, tag)

    def reset(self):
        self.session["history"] = []
        self.memory = WorkingMemory(workspace_root=self.root)
        self._sync_working_memory()
        self.session["memory"] = {"file_summaries": {}}
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved
