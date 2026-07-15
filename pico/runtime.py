"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

from copy import deepcopy
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from . import checkpoint as checkpointlib
from . import session_store as sessionstorelib
from . import workspace_snapshot
from .features import memory as memorylib
from . import security as securitylib
from .checkpoint_store import CheckpointStore
from .context_manager import ContextManager
from .docker_sandbox import DockerSandboxContext
from .memory.block_store import BlockStore
from .memory.retrieval import Retrieval
from .prompt_prefix import build_prompt_prefix, tool_signature
from .repo_map import RepoMap
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager
from .run_store import RunStore
from .observability import REPORT_SCHEMA_VERSION, project_trace_event
from .sandbox_apply import StagingObserver
from .sandbox_session import (
    read_source_apply_authority,
    SandboxSessionError,
    source_apply_control_lock_path,
)
from .session_store import SESSION_FORMAT_VERSION, SESSION_RECORD_TYPE
from .tool_change_recorder import ToolChangeRecorder
from .tool_context import ToolContext
from .tool_executor import ToolExecutionResult, ToolExecutor
from . import tools as toolkit
from .config import load_pico_toml, read_project_env
from .verification import new_verification_record
from .workspace import WorkspaceContext, now
from .workspace_observer import WorkspaceObserver

DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "PICO_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_NEW_TOKENS = 2048
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
}
SANDBOX_WORKSPACE_BRANCH = "pico-sandbox"
SANDBOX_WORKSPACE_STATUS = "sandbox_execution_state_unknown"
_DEVELOPMENT_RUNTIME_SEAL = object()
_SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _configured_redaction_names(explicit_names=(), *env_sources):
    names = set(DEFAULT_SECRET_ENV_NAMES)
    names.update(str(name).upper() for name in (explicit_names or ()))
    for source in env_sources:
        names.update(
            item.strip().upper()
            for item in source.get(_SECRET_ENV_NAMES_VAR, "").split(",")
            if item.strip()
        )
    return frozenset(names)


def _artifact_redactor(redaction_env, secret_env_names):
    return lambda value: securitylib.redact_artifact(
        value,
        env=redaction_env,
        secret_env_names=secret_env_names,
    )


def _build_redaction_snapshot(
    workspace_root,
    *,
    secret_env_names=(),
    process_env=None,
    project_env=None,
    warn=True,
):
    process_values = dict(os.environ if process_env is None else process_env)
    project_values = (
        read_project_env(workspace_root, warn=warn)
        if project_env is None
        else dict(project_env)
    )
    configured_names = _configured_redaction_names(
        secret_env_names,
        process_values,
        project_values,
    )
    merged = dict(process_values)
    for index, (name, value) in enumerate(project_values.items()):
        if (
            name in merged
            and merged[name] != value
            and securitylib.is_secret_env_name(name, configured_names)
        ):
            collision_name = f"PICO_REDACTION_COLLISION_{index}_SECRET"
            suffix = 0
            while collision_name in merged or collision_name in project_values:
                suffix += 1
                collision_name = (
                    f"PICO_REDACTION_COLLISION_{index}_{suffix}_SECRET"
                )
            merged[collision_name] = merged[name]
        merged[name] = value
    redaction_env = MappingProxyType(merged)
    return (
        redaction_env,
        configured_names,
        _artifact_redactor(redaction_env, configured_names),
    )


def _freeze_redaction_snapshot(
    redaction_env,
    secret_env_names=(),
    *,
    trusted=False,
):
    snapshot = (
        redaction_env
        if trusted and isinstance(redaction_env, MappingProxyType)
        else MappingProxyType(dict(redaction_env))
    )
    configured_names = _configured_redaction_names(secret_env_names, snapshot)
    return snapshot, configured_names, _artifact_redactor(snapshot, configured_names)


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


def build_report_request_metadata(task_state, last_request_metadata):
    """Return a dict fragment to merge into report last_request_metadata.

    Preserves the invariant that the initial prompt-time resume_status is kept
    under last_prompt_resume_status when a later task_state.resume_status is
    promoted into report metadata.
    """
    fragment = dict(last_request_metadata)
    if not fragment:
        return fragment
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
        redaction_env=None,
        feature_flags=None,
        allowed_tools=None,
        _trusted_redaction_env=False,
        _trusted_executables=None,
        sandbox_context=None,
        project_config=None,
        session_id=None,
        _development_runtime_seal=None,
    ):
        self.model_client = model_client
        if sandbox_context is not None and not isinstance(
            sandbox_context,
            DockerSandboxContext,
        ):
            raise ValueError("sandbox_context must be a DockerSandboxContext")
        self.sandbox_context = sandbox_context
        self.docker_sandbox = isinstance(sandbox_context, DockerSandboxContext)
        self._docker_sandbox_development = False
        if self.docker_sandbox:
            try:
                authorization = sandbox_context.authorization.verify(
                    sandbox_context.runner.image
                )
            except Exception as exc:
                raise ValueError("docker sandbox runtime authorization invalid") from exc
            if authorization.attestation_kind == "development":
                if _development_runtime_seal is not _DEVELOPMENT_RUNTIME_SEAL:
                    raise ValueError(
                        "docker sandbox requires product or candidate authorization"
                    )
                self._docker_sandbox_development = True
            elif authorization.attestation_kind not in {
                "local",
                "product",
                "candidate",
            }:
                raise ValueError(
                    "docker sandbox requires local, product, or candidate authorization"
                )
            self.source_root = sandbox_context.source_root
            self.execution_root = sandbox_context.execution_root
            self.project_state_root = sandbox_context.project_state_root
            self.sandbox_session = sandbox_context.sandbox_session
            if (
                Path(workspace.repo_root) != self.execution_root
                or workspace.logical_root != sandbox_context.logical_root
                or redaction_env is None
                or project_config is None
            ):
                raise ValueError("docker sandbox runtime context is incomplete")
            workspace = WorkspaceContext(
                cwd=workspace.cwd,
                repo_root=workspace.repo_root,
                branch=SANDBOX_WORKSPACE_BRANCH,
                default_branch=SANDBOX_WORKSPACE_BRANCH,
                status=SANDBOX_WORKSPACE_STATUS,
                recent_commits=[],
                project_docs=dict(workspace.project_docs),
                trusted_executables=workspace.trusted_executables,
                logical_root=sandbox_context.logical_root,
            )
        else:
            self.source_root = Path(workspace.repo_root)
            self.execution_root = self.source_root
            self.project_state_root = self.source_root / ".pico"
            self.sandbox_session = None
        self.workspace = workspace
        # Existing tool code treats root as the model-visible execution root.
        self.root = self.execution_root
        executable_source = (
            workspace.trusted_executables
            if _trusted_executables is None
            else _trusted_executables
        )
        if self.docker_sandbox:
            executable_source = {
                name: path
                for name, path in dict(executable_source or {}).items()
                if name != "git"
            }
        self.trusted_executables = MappingProxyType(dict(executable_source or {}))
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        if redaction_env is None:
            redaction_env, configured_names, _ = _build_redaction_snapshot(
                self.source_root,
                secret_env_names=secret_env_names,
            )
        else:
            redaction_env, configured_names, _ = _freeze_redaction_snapshot(
                redaction_env,
                secret_env_names,
                trusted=_trusted_redaction_env,
            )
        self.redaction_env = redaction_env
        self.secret_env_names = configured_names
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            if not feature_flags.keys() <= DEFAULT_FEATURE_FLAGS.keys():
                raise ValueError("unsupported feature flag")
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        self.run_store = run_store or RunStore(self.project_state_root / "runs")
        redactor = _artifact_redactor(
            self.redaction_env,
            self.secret_env_names,
        )
        if (
            hasattr(self.run_store, "set_redactor")
            and (self.depth == 0 or not getattr(self.run_store, "_redactor_configured", False))
        ):
            self.run_store.set_redactor(redactor)
        if (
            hasattr(self.session_store, "set_redactor")
            and (self.depth == 0 or not getattr(self.session_store, "_redactor_configured", False))
        ):
            self.session_store.set_redactor(redactor)
        # 可恢复编辑（recoverable editing）的组件在这里就位。
        # 它们和 resume-summary 用的 `checkpointlib` 是两条独立的通路：
        # CheckpointStore 落在 .pico/checkpoints/ 下，专门记 turn/restore/manual 类型。
        checkpoint_root = self.source_root
        if self.docker_sandbox:
            checkpoint_root = (
                self.sandbox_context.sandbox_state_root
                / "recovery"
                / ".pico"
                / "checkpoints"
            )
        source_apply_authority = None
        source_apply_control_lock = None
        if not self.docker_sandbox:
            def source_apply_authority():
                return read_source_apply_authority(
                    Path.home() / ".pico" / "sandboxes",
                    self.source_root,
                )
            source_apply_control_lock = source_apply_control_lock_path(
                Path.home() / ".pico" / "sandboxes",
                self.source_root,
            )
        self.checkpoint_store = CheckpointStore(
            checkpoint_root,
            redactor=redactor,
            source_apply_authority=source_apply_authority,
            source_apply_control_lock=source_apply_control_lock,
        )
        self.tool_change_owner_id = "runtime_" + uuid.uuid4().hex[:12]
        self.tool_change_recorder = ToolChangeRecorder(self.checkpoint_store, owner_id=self.tool_change_owner_id)
        self.interrupted_tool_changes = []
        self.recovery_checkpoint_writer = RecoveryCheckpointWriter(self.checkpoint_store, self.root)
        self.recovery_manager = RecoveryManager(self.checkpoint_store, self.root)
        if self.docker_sandbox:
            self.workspace_observer = StagingObserver(
                self.sandbox_context,
                self.checkpoint_store,
                redaction_env=self.redaction_env,
                secret_env_names=self.secret_env_names,
            )
            self.workspace_observer.ensure_baseline(
                resumed=self.sandbox_context.resumed or self.depth > 0
            )
        else:
            self.workspace_observer = WorkspaceObserver(
                self.root,
                executables=self.trusted_executables,
            )
        project_config = (
            load_pico_toml(self.source_root)
            if project_config is None
            else deepcopy(project_config)
        )
        self.project_config = deepcopy(project_config)
        context_config = project_config["context"]
        memory_config = project_config["memory"]
        retrieval_config = memory_config["retrieval"]
        self.project_max_blob_size = project_config["policy"]["max_blob_size"]
        self.context_config = {
            "history_soft_cap": context_config["history_soft_cap"],
            "history_floor_messages": context_config["history_floor_messages"],
            "injection_budget_ratio": context_config["injection_budget_ratio"],
            "system_tools_hard_cap": context_config["system_tools_hard_cap"],
            "total_budget_hard_cap": context_config["total_budget_hard_cap"],
            "digest_size_threshold": context_config["digest"]["size_threshold_chars"],
            "recall": memory_config["recall"],
            "field_boosts": retrieval_config["field_boost"],
            "link_config": (
                retrieval_config["link"]["max_added"],
                retrieval_config["link"]["decay"],
            ),
        }
        if session is None:
            new_session_id = session_id or self.new_session_id()
            if (
                self.docker_sandbox
                and self.depth == 0
                and new_session_id
                != self.sandbox_session.manifest["pico_session_id"]
            ):
                raise ValueError("sandbox session binding mismatch")
            self.session = {
                "record_type": SESSION_RECORD_TYPE,
                "format_version": SESSION_FORMAT_VERSION,
                "id": new_session_id,
                "created_at": now(),
                "workspace_root": str(self.source_root),
                "messages": [],
                "recently_recalled": [],
                "working_memory": {"task_summary": "", "recent_files": []},
                "memory": {"file_summaries": {}},
                "checkpoints": {"current_id": "", "items": {}},
                "runtime_identity": {},
                "resume_state": {},
                "recovery": {"current_checkpoint_id": ""},
            }
        else:
            self.session = self.redact_artifact(deepcopy(session))
            if (
                self.docker_sandbox
                and (
                    self.session.get("workspace_root") != str(self.source_root)
                    or self.depth == 0
                    and self.session.get("id")
                    != self.sandbox_session.manifest["pico_session_id"]
                )
            ):
                raise ValueError("sandbox session binding mismatch")
            identities = [self.session.get("runtime_identity", {})]
            items = self.session.get("checkpoints", {}).get("items", {})
            if isinstance(items, dict):
                identities.extend(
                    checkpoint.get("runtime_identity", {})
                    for checkpoint in items.values()
                    if isinstance(checkpoint, dict)
                )
            for identity in identities:
                if not isinstance(identity, dict):
                    continue
                identity_flags = identity.get("feature_flags", {})
                if not isinstance(identity_flags, dict) or not identity_flags.keys() <= DEFAULT_FEATURE_FLAGS.keys():
                    raise ValueError("unsupported runtime identity feature flag")
        self._ensure_session_shape()
        self.memory = WorkingMemory.from_dict(self.session.get("working_memory"), workspace_root=self.root)
        self._sync_working_memory()
        # v2 memory subsystem: BlockStore/Retrieval/RepoMap 在 tool_context 里被 wire 给 memory/repo_lookup 工具
        workspace_memory_root = self.project_state_root / "memory"
        user_memory_root = Path.home() / ".pico" / "memory"
        self.memory_store = BlockStore(
            workspace_root=workspace_memory_root,
            user_root=user_memory_root,
            redaction_env=self.redaction_env,
            secret_env_names=self.secret_env_names,
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
        self.last_request_metadata = {}
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        redaction_env = kwargs.pop("redaction_env", None)
        trusted_redaction_env = kwargs.pop("_trusted_redaction_env", False)
        secret_env_names = kwargs.get("secret_env_names", ())
        sandbox_context = kwargs.get("sandbox_context")
        source_root = (
            sandbox_context.source_root
            if isinstance(sandbox_context, DockerSandboxContext)
            else workspace.repo_root
        )
        if isinstance(sandbox_context, DockerSandboxContext) and redaction_env is None:
            raise ValueError("docker sandbox redaction snapshot is required")
        if redaction_env is None:
            redaction_env, configured_names, redactor = _build_redaction_snapshot(
                source_root,
                secret_env_names=secret_env_names,
            )
            trusted_redaction_env = True
        else:
            redaction_env, configured_names, redactor = _freeze_redaction_snapshot(
                redaction_env,
                secret_env_names,
                trusted=trusted_redaction_env,
            )
            trusted_redaction_env = True
        session_store.set_redactor(redactor)
        kwargs["redaction_env"] = redaction_env
        kwargs["_trusted_redaction_env"] = trusted_redaction_env
        kwargs["secret_env_names"] = configured_names
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    @classmethod
    def _for_docker_sandbox_development(cls, **kwargs):
        kwargs["_development_runtime_seal"] = _DEVELOPMENT_RUNTIME_SEAL
        return cls(**kwargs)

    @classmethod
    def _from_session_for_docker_sandbox_development(
        cls,
        model_client,
        workspace,
        session_store,
        session_id,
        **kwargs,
    ):
        kwargs["_development_runtime_seal"] = _DEVELOPMENT_RUNTIME_SEAL
        return cls.from_session(
            model_client,
            workspace,
            session_store,
            session_id,
            **kwargs,
        )

    def _ensure_session_shape(self):
        if (
            self.session.get("record_type") != SESSION_RECORD_TYPE
            or type(self.session.get("format_version")) is not int
            or self.session["format_version"] != SESSION_FORMAT_VERSION
        ):
            raise ValueError("Pico requires a current session")
        if not isinstance(self.session.get("messages"), list):
            raise ValueError("v3 session messages must be a list")
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
        refreshed_workspace = WorkspaceContext.build(
            self.root,
            executables=self.trusted_executables,
            repo_root_override=self.root,
            inspect_git=not self.docker_sandbox,
            logical_root=(
                self.sandbox_context.logical_root if self.docker_sandbox else None
            ),
            branch_override=(
                SANDBOX_WORKSPACE_BRANCH if self.docker_sandbox else None
            ),
            default_branch_override=(
                SANDBOX_WORKSPACE_BRANCH if self.docker_sandbox else None
            ),
            status_override=(
                SANDBOX_WORKSPACE_STATUS if self.docker_sandbox else None
            ),
        )
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

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def secret_env_summary(self):
        return securitylib.secret_env_summary(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def redact_text(self, text):
        text = str(text)
        if self.docker_sandbox:
            text = text.replace(str(self.execution_root), self.sandbox_context.logical_root)
        return securitylib.redact_text(
            text,
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(
            value,
            key=key,
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def shell_env(self):
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def emit_trace(self, task_state, event, payload=None):
        envelope = project_trace_event(
            task_state,
            event,
            self.redact_artifact(payload or {}),
            created_at=now(),
        )
        self.run_store.append_trace(task_state, envelope)
        return envelope

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
        `messages`，这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整消息，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        if not isinstance(args, dict):
            return
        result = self.redact_text(result)
        path = args.get("path")
        if not path:
            return

        try:
            canonical_path = self.path(path).relative_to(self.root).as_posix()
        except (OSError, ValueError):
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

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    @staticmethod
    def _public_sandbox_digest(value):
        value = str(value or "")
        if (
            len(value) == 71
            and value.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in value[7:])
        ):
            return "sha256:" + value[7:23]
        return ""

    def _sandbox_report_section(self, tool_report=None, *, diff_counts=None):
        tool_report = dict(tool_report or {})
        if self.sandbox_context is None:
            return {
                "active": False,
                "implementation": "none",
                "session_state": "not_applicable",
                "engine_profile": "not_applicable",
                "image_digest": "",
                "policy_digest": "",
                "network_mode": "not_applicable",
                "source_mounted": False,
                "state_mounted": False,
                "container_calls": 0,
                "target_started_count": 0,
                "outcome_counts": {},
                "cleanup_failure_count": 0,
                "host_fallback_count": 0,
                "diff": {"candidates": 0, "blocked": 0, "generated": 0},
                "apply_status": "not_applicable",
            }
        current = None
        inspect = getattr(self.sandbox_context, "current_session", None)
        if callable(inspect):
            current = inspect()
        if current is None:
            current = getattr(self.sandbox_context, "sandbox_session", None)
        manifest = dict(getattr(current, "manifest", {}) or {})
        engine = dict(manifest.get("engine") or {})
        image = dict(manifest.get("image") or {})
        policy = dict(manifest.get("policy") or {})
        diff = dict(manifest.get("diff") or {})
        apply = dict(manifest.get("apply") or {})
        counts = dict(diff_counts or {})
        return {
            "active": True,
            "implementation": "docker_container",
            "session_state": str(manifest.get("state") or "review_required"),
            "engine_profile": str(
                engine.get("platform_profile") or engine.get("profile") or "unknown"
            ),
            "image_digest": self._public_sandbox_digest(
                image.get("manifest_digest") or image.get("reference")
            ),
            "policy_digest": self._public_sandbox_digest(policy.get("digest")),
            "network_mode": str(policy.get("network") or "none"),
            "source_mounted": False,
            "state_mounted": False,
            "container_calls": int(tool_report.get("sandbox_calls", 0) or 0),
            "target_started_count": int(
                tool_report.get("sandbox_target_started_count", 0) or 0
            ),
            "outcome_counts": dict(
                tool_report.get("sandbox_outcome_counts", {})
            ),
            "cleanup_failure_count": int(
                tool_report.get("sandbox_cleanup_failure_count", 0) or 0
            ),
            "host_fallback_count": int(
                tool_report.get("host_fallback_count", 0) or 0
            ),
            "diff": {
                "candidates": int(
                    counts.get("candidates", diff.get("candidate_count", 0)) or 0
                ),
                "blocked": int(
                    counts.get("blocked", diff.get("blocked_count", 0)) or 0
                ),
                "generated": int(counts.get("generated", 0) or 0),
            },
            "apply_status": str(apply.get("status") or "not_started"),
        }

    def _refresh_sandbox_run_report(self, *, diff_counts):
        task_state = self.current_task_state
        if task_state is None or not self.run_store.report_path(task_state).exists():
            return
        report = self.run_store.load_report(task_state.run_id)
        sandbox = report["sandbox"]
        report["sandbox"] = self._sandbox_report_section(
            {
                "sandbox_calls": sandbox["container_calls"],
                "sandbox_target_started_count": sandbox["target_started_count"],
                "sandbox_outcome_counts": sandbox["outcome_counts"],
                "sandbox_cleanup_failure_count": sandbox[
                    "cleanup_failure_count"
                ],
                "host_fallback_count": sandbox["host_fallback_count"],
            },
            diff_counts=diff_counts,
        )
        self.run_store.write_report(task_state, report)

    def finalize_sandbox_session(self):
        if not self.docker_sandbox or self.depth > 0:
            return None
        store = self.sandbox_context.runner.session_store
        state_root = self.sandbox_context.sandbox_state_root
        result = None
        try:
            result = self.workspace_observer.finalize_diff(self.redact_text)
            if not result["artifact"]["entries"]:
                store.discard(state_root)
                result["status"] = "no_changes_discarded"
                result["session_state"] = "discarded"
            else:
                result["session_state"] = "pending_review"
            result["sandbox_id"] = self.sandbox_session.sandbox_id
            counts = result["artifact"]["counts"]
            self._refresh_sandbox_run_report(
                diff_counts={
                    "candidates": sum(
                        counts.get(name, 0)
                        for name in ("candidate", "high_risk_candidate")
                    ),
                    "blocked": sum(
                        counts.get(name, 0)
                        for name in (
                            "blocked_sensitive",
                            "blocked_size",
                            "blocked_type",
                        )
                    ),
                    "generated": result.get("generated_count", 0),
                }
            )
            return result
        except Exception:
            try:
                store.mark_review_required(
                    state_root,
                    error_code="sandbox_diff_finalization_failed",
                )
            except SandboxSessionError:
                pass
            try:
                self._refresh_sandbox_run_report(diff_counts={})
            except Exception:
                pass
            raise
        finally:
            try:
                current = store.inspect(state_root)
                lease = current.manifest["lease"]
                if lease is not None:
                    store.release(state_root, lease["owner_nonce"])
            except SandboxSessionError:
                pass

    def execute_tool(self, name, args):
        result = self.tool_executor.execute(name, args)
        safe_result = ToolExecutionResult(
            content=self.redact_text(result.content),
            metadata=self.redact_artifact(result.metadata),
        )
        self._last_tool_result_metadata = dict(safe_result.metadata)
        return safe_result

    def record_verification_evidence(
        self,
        argv,
        risk_class,
        runner_executed,
        execution_mode,
        exit_code,
        stdout,
        stderr,
        checkpoint_id="",
        trace_event_id="",
    ):
        """在指定 checkpoint 上附加一条 Verification Evidence。

        - 只接受当前 turn 已创建且显式传入的 recovery checkpoint id；
        - 记录同时写入 checkpoint record，并在 trace 里补一条 verification_recorded 事件。
        """
        current_id = str(
            getattr(self.current_task_state, "recovery_checkpoint_id", "")
            or ""
        )
        target_id = str(checkpoint_id or "")
        if not current_id or not target_id or target_id != current_id:
            return None
        record = new_verification_record(
            argv=argv,
            risk_class=risk_class,
            runner_executed=runner_executed,
            execution_mode=execution_mode,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            affected_checkpoint_id=target_id,
            trace_event_id=trace_event_id,
            redact_text=self.redact_text,
        )
        if record is None:
            return None
        try:
            checkpoint = self.checkpoint_store.load_checkpoint_record(target_id)
        except (OSError, ValueError):
            return None
        if (
            not isinstance(checkpoint, dict)
            or type(checkpoint.get("checkpoint_id")) is not str
            or checkpoint["checkpoint_id"] != target_id
            or not isinstance(checkpoint.get("verification_evidence"), list)
        ):
            return None
        checkpoint["verification_evidence"].append(record)
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
        tool_calls = []
        for message in self.session.get("messages", []):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append(block)
        repeated_count = sum(
            1
            for block in tool_calls[-6:]
            if block.get("name") == name and block.get("input") == args
        )
        return repeated_count >= 2

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(
        self,
        task_state,
        *,
        completion_usage_totals=None,
        model_execution=None,
    ):
        request_metadata = build_report_request_metadata(
            task_state,
            self.last_request_metadata,
        )
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_hit": False,
            **dict(completion_usage_totals or {}),
        }
        execution = dict(model_execution or {})
        tool_report = dict(execution.get("tool_report") or {})
        if execution and not execution.get("transport_evidence_complete", False):
            execution["transport_attempts"] = None
            execution["transport_retries"] = None
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        duration_ms = int(execution.get("run_duration_ms", 0) or 0)
        changed_paths = tool_report.get("changed_paths", [])
        recovery_review_required = bool(
            tool_report.get("recovery_review_required", False)
        )
        workspace_status = str(self.workspace.status or "").strip()
        commit = ""
        if self.workspace.recent_commits:
            candidate = str(self.workspace.recent_commits[0]).split(maxsplit=1)[0]
            if candidate and all(character in "0123456789abcdefABCDEF" for character in candidate):
                commit = candidate
        return {
            "record_type": "run_report",
            "format_version": REPORT_SCHEMA_VERSION,
            "run": {
                "run_id": task_state.run_id,
                "task_id": task_state.task_id,
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "duration_ms": duration_ms,
                "commit": commit,
                "dirty": (
                    bool(changed_paths)
                    if self.docker_sandbox
                    else workspace_status not in {"", "clean"}
                ),
            },
            "model": {
                "attempts": int(
                    execution.get("model_attempts", task_state.attempts) or 0
                ),
                "turns": int(execution.get("model_turns", 0) or 0),
                "failures": int(execution.get("model_failures", 0) or 0),
                "retries": int(execution.get("model_retries", 0) or 0),
                "transport_attempts": execution.get("transport_attempts"),
                "transport_retries": execution.get("transport_retries"),
                "evidence_complete": bool(execution.get("transport_evidence_complete", False)),
                "attempt_origin_counts": dict(execution.get("attempt_origin_counts", {})),
                "failure_reason_counts": dict(execution.get("failure_reason_counts", {})),
                "usage": usage,
            },
            "context": request_metadata,
            "tools": {
                "calls": int(tool_report.get("calls", 0) or 0),
                "allowed": int(tool_report.get("allowed", 0) or 0),
                "denied": int(tool_report.get("denied", 0) or 0),
                "name_counts": dict(tool_report.get("name_counts", {})),
                "status_counts": dict(tool_report.get("status_counts", {})),
            },
            "memory": {
                "recall_candidates": request_metadata.get("memory_candidate_count", 0),
                "recall_selected": request_metadata.get("memory_selected_count", 0),
                "filter_counts": request_metadata.get("memory_filter_counts", {}),
            },
            "sandbox": self._sandbox_report_section(tool_report),
            "effects": {
                "changed_files": len(changed_paths),
                "partial_successes": int(
                    tool_report.get("partial_successes", 0) or 0
                ),
                "recovery_review_required": recovery_review_required,
            },
            "recovery": {
                "checkpoint_id": (
                    task_state.recovery_checkpoint_id or task_state.checkpoint_id
                ),
                "status": task_state.resume_status,
                "review_required": recovery_review_required,
            },
            "integrity": {
                "writer": "current",
                "terminal_event_expected": True,
            },
            "finalization": {"status": "complete", "error_count": 0},
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
            trusted_executables=self.trusted_executables,
            redaction_env=self.redaction_env,
            secret_env_names=self.secret_env_names,
            sandbox_context=self.sandbox_context,
        )

    def spawn_delegate(self, args):
        task = str(args.get("task", "")).strip()
        child_session_store = self.session_store
        if self.docker_sandbox:
            child_session_store = sessionstorelib.SessionStore(
                self.sandbox_context.sandbox_state_root / "delegate-sessions",
                redactor=_artifact_redactor(
                    self.redaction_env,
                    self.secret_env_names,
                ),
            )
        child_factory = (
            Pico._for_docker_sandbox_development
            if self._docker_sandbox_development
            else Pico
        )
        child = child_factory(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=child_session_store,
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            secret_env_names=self.secret_env_names,
            redaction_env=self.redaction_env,
            _trusted_redaction_env=True,
            _trusted_executables=self.trusted_executables,
            shell_env_allowlist=self.shell_env_allowlist,
            sandbox_context=self.sandbox_context,
            project_config=self.project_config,
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.memory.set_task_summary(task)
        child._sync_working_memory()
        return "delegate_result:\n" + child.ask(task)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            safe_args = self.redact_artifact(args)
            answer = input(
                f"approve {name} {json.dumps(safe_args, ensure_ascii=True)}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def reset(self):
        candidate = deepcopy(self.session)
        candidate["messages"] = []
        candidate["recently_recalled"] = []
        candidate.pop("_recall_errors", None)
        candidate["working_memory"] = {
            "task_summary": "",
            "recent_files": [],
        }
        candidate["memory"] = {"file_summaries": {}}
        checkpoints = candidate.setdefault("checkpoints", {"current_id": "", "items": {}})
        checkpoints["current_id"] = ""
        checkpoints.setdefault("items", {})
        candidate["resume_state"] = {}
        recovery = candidate.setdefault("recovery", {})
        recovery["current_checkpoint_id"] = ""
        saved_path = self.session_store.save(candidate)
        self.session = candidate
        self.session_path = saved_path
        self.memory = WorkingMemory(workspace_root=self.root)
        self.resume_state = {}
        self.last_request_metadata = {}

    def path(self, raw_path):
        if self.docker_sandbox:
            return self.sandbox_context.workspace_view.physical_path(raw_path)
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    @staticmethod
    def new_session_id():
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
