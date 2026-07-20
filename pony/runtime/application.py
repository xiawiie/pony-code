"""Agent 运行时核心逻辑。

Pony 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

from copy import deepcopy
from dataclasses import replace
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from pony.state import checkpoint as checkpointlib
from pony.state import session_store as sessionstorelib
import pony.memory.service as memorylib
from pony.security import private_files as private_files
from pony.security import redaction as redaction
from pony.agent.compaction import (
    compact_session as compact_session_tree,
    rewind_with_branch_summary,
)
from pony.agent.context_manager import ContextManager
from pony.memory.block_store import BlockStore
from pony.memory.retrieval import Retrieval
from pony.agent.model_capabilities import (
    build_model_budget,
    DEFAULT_MAX_OUTPUT_TOKENS as MODEL_DEFAULT_MAX_OUTPUT_TOKENS,
    resolve_model_capabilities,
    TokenAccounting,
)
from pony.agent.prompt_prefix import build_prompt_prefix, tool_signature
from pony.memory.repo_map import RepoMap
from pony.state.run_store import RunStore
from pony.agent.observability import REPORT_SCHEMA_VERSION, project_trace_event
from pony.state.session_store import SESSION_FORMAT_VERSION, SESSION_RECORD_TYPE
from pony.context.skills import discover_project_skills
from pony.tools.context import ToolContext
from pony.tools.executor import ToolExecutionResult, ToolExecutor
from pony.tools import registry as toolkit
from pony.tools.permissions import PermissionMode, validate_permission_mode
from pony.tools.validation import SensitiveToolError
from pony.tools.validation import validate_tool as validate_tool_arguments
from pony.config.environment import read_project_env
from pony.config.model import validate_model_name
from pony.config.project import load_pony_toml
from pony.runtime.options import RuntimeOptions
from pony.runtime.legacy import preflight_legacy_sandbox_resume
from pony.runtime.reporting import build_report_request_metadata
from pony.runtime.working_memory import WorkingMemory
from pony.workspace.context import WorkspaceContext, now
from pony.workspace.observer import WorkspaceObserver

DEFAULT_SHELL_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
)
DEFAULT_SECRET_ENV_NAMES = (
    "PONY_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PONY_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "PONY_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "PONY_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_OUTPUT_TOKENS = MODEL_DEFAULT_MAX_OUTPUT_TOKENS
MAX_DELEGATE_RESULT_CHARS = 4_000
MAX_DELEGATE_STEPS = 3
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
}
_OPAQUE_PROVIDER_STATE_PROTOCOLS = frozenset(
    {"anthropic_messages", "openai_responses"}
)
_SECRET_ENV_NAMES_VAR = "PONY_SECRET_ENV_NAMES"


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
    return lambda value: redaction.redact_artifact(
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
            and redaction.is_secret_env_name(name, configured_names)
        ):
            collision_name = f"PONY_REDACTION_COLLISION_{index}_SECRET"
            suffix = 0
            while collision_name in merged or collision_name in project_values:
                suffix += 1
                collision_name = f"PONY_REDACTION_COLLISION_{index}_{suffix}_SECRET"
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


def _session_has_provider_state(session):
    messages = session.get("messages", []) if isinstance(session, dict) else []
    return any(
        isinstance(message, dict) and bool(message.get("_pony_provider_state"))
        for message in messages
    )


def _session_requires_bypass_permission_capability(session):
    if not isinstance(session, dict):
        return False
    return session.get("permission_mode") == PermissionMode.BYPASS_PERMISSIONS.value or (
        session.get("permission_mode") == PermissionMode.PLAN.value
        and session.get("pre_plan_mode") == PermissionMode.BYPASS_PERMISSIONS.value
    )


class Pony:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        *,
        session=None,
        options=None,
    ):
        self._initialize(
            model_client,
            workspace,
            session_store,
            session=session,
            options=options,
        )

    @property
    def bypass_permissions_available(self):
        return self._bypass_permissions_available

    def _initialize(
        self,
        model_client,
        workspace,
        session_store,
        *,
        session=None,
        options=None,
    ):
        if options is None:
            options = RuntimeOptions()
        if not isinstance(options, RuntimeOptions):
            raise TypeError("options must be a RuntimeOptions instance")
        if (
            isinstance(session, dict)
            and _session_requires_bypass_permission_capability(session)
            and not options.allow_dangerously_skip_permissions
        ):
            raise ValueError(
                "resuming bypassPermissions requires dangerous capability"
            )
        if isinstance(session, dict):
            session_store.path_for(session.get("id"))
            preflight_legacy_sandbox_resume(workspace.repo_root, session["id"])
        self.model_client = model_client
        model_binding = getattr(model_client, "provider_binding", None)
        model_binding = (
            deepcopy(model_binding) if isinstance(model_binding, dict) else None
        )
        self._configure_workspace(workspace, options)
        redactor = self._configure_runtime_options(session_store, options)
        self._refresh_project_skills()
        self._configure_recovery_services(redactor)
        self._configure_project_model(options)
        self._configure_session(session, model_binding, options.session_id)
        self._configure_memory_and_tools()
        self._persist_initialized_session()
        self._reset_turn_state()

    def _configure_workspace(self, workspace, options):
        self.source_root = Path(workspace.repo_root)
        self.execution_root = self.source_root
        self.project_state_root = self.source_root / ".pony"
        self.workspace = workspace
        # Existing tool code treats root as the model-visible execution root.
        self.root = self.execution_root
        self.source_root_identity = private_files.private_directory_identity(
            self.source_root
        )
        self.workspace_root_identity = private_files.private_directory_identity(
            self.root
        )
        executable_source = (
            workspace.trusted_executables
            if options.trusted_executables is None
            else options.trusted_executables
        )
        self.trusted_executables = MappingProxyType(dict(executable_source or {}))

    def _configure_runtime_options(self, session_store, options):
        self.session_store = session_store
        self.project_trusted = options.project_trusted is True
        self._trace_listener = None
        self._approval_prompt = None
        self.max_steps = options.max_steps
        self.depth = options.depth
        self.max_depth = options.max_depth
        self.read_only = options.read_only
        self._bypass_permissions_available = (
            options.allow_dangerously_skip_permissions is True
        )
        self.shell_env_allowlist = tuple(
            options.shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST
        )
        if options.redaction_env is None:
            redaction_env, configured_names, _ = _build_redaction_snapshot(
                self.source_root,
                secret_env_names=options.secret_env_names,
            )
        else:
            redaction_env, configured_names, _ = _freeze_redaction_snapshot(
                options.redaction_env,
                options.secret_env_names,
                trusted=options.trusted_redaction_env,
            )
        self.redaction_env = redaction_env
        self.secret_env_names = configured_names
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if options.feature_flags:
            if not options.feature_flags.keys() <= DEFAULT_FEATURE_FLAGS.keys():
                raise ValueError("unsupported feature flag")
            self.feature_flags.update(
                {str(key): bool(value) for key, value in options.feature_flags.items()}
            )
        self.allowed_tools = self._normalize_allowed_tools(options.allowed_tools)
        self.model_client_factory = options.model_client_factory
        self.delegate_model_client_factory = options.delegate_model_client_factory
        self.run_store = options.run_store or RunStore(self.project_state_root / "runs")
        redactor = _artifact_redactor(
            self.redaction_env,
            self.secret_env_names,
        )
        if hasattr(self.run_store, "set_redactor") and (
            self.depth == 0
            or not getattr(self.run_store, "_redactor_configured", False)
        ):
            self.run_store.set_redactor(redactor)
        if hasattr(self.session_store, "set_redactor") and (
            self.depth == 0
            or not getattr(self.session_store, "_redactor_configured", False)
        ):
            self.session_store.set_redactor(redactor)
        return redactor

    def _refresh_project_skills(self):
        from pony.cli.help import SLASH_COMMANDS

        self.project_skills = discover_project_skills(
            self.source_root,
            expected_root_identity=self.source_root_identity,
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
            reserved_names=(command.name for command in SLASH_COMMANDS),
        )

    def project_skill(self, name):
        return self.project_skills.get(str(name))

    def _configure_recovery_services(self, redactor):
        self.workspace_observer = WorkspaceObserver(
            self.root,
            executables=self.trusted_executables,
        )
        self.mutation_lock_path = self.project_state_root / ".workspace-mutation.lock"

    def _configure_project_model(self, options):
        project_config = (
            load_pony_toml(
                self.source_root,
                expected_root_identity=self.source_root_identity,
            )
            if options.project_config is None
            else deepcopy(options.project_config)
        )
        self.project_config = deepcopy(project_config)
        self._model_runtime_options = options
        self._apply_model_runtime(
            self.model_client,
            self._resolve_model_runtime(self.model_client),
        )

    def _resolve_model_runtime(self, model_client):
        project_config = self.project_config
        options = self._model_runtime_options
        model_config = project_config["model"]
        config_meta = project_config.get("_meta", {})
        config_meta = config_meta if isinstance(config_meta, dict) else {}
        explicit_model_config = {}
        if config_meta.get("model_context_explicit") is True:
            explicit_model_config["context_window"] = model_config["context_window"]
        if config_meta.get("model_output_explicit") is True:
            explicit_model_config["output_limit"] = model_config["output_limit"]
        model_name = str(getattr(model_client, "model", "") or "")
        model_capabilities = resolve_model_capabilities(
            model_name,
            model_config=explicit_model_config,
            context_window=options.context_window,
            max_output_tokens=options.max_output_tokens,
            warning_sink=lambda message: print(message, file=sys.stderr),
        )
        context_config = project_config["context"]
        memory_config = project_config["memory"]
        retrieval_config = memory_config["retrieval"]
        compaction_config = context_config["compaction"]
        model_budget = build_model_budget(
            model_capabilities,
            output_limit=(
                options.max_output_tokens
                if options.max_output_tokens is not None
                else (
                    model_config["output_limit"]
                    if config_meta.get("model_output_explicit") is True
                    else model_capabilities.max_output_tokens
                )
            ),
            reserve_tokens=compaction_config["reserve_tokens"],
            keep_recent_tokens=compaction_config["keep_recent_tokens"],
            system_tools_hard_cap=context_config["system_tools_hard_cap"],
            source_pool_tokens=context_config["source_pool_tokens"],
        )
        return {
            "capabilities": model_capabilities,
            "budget": model_budget,
            "token_accounting": TokenAccounting(
                getattr(model_client, "count_tokens", None)
            ),
            "context_config": {
                "system_tools_hard_cap": context_config["system_tools_hard_cap"],
                "source_pool_tokens": context_config["source_pool_tokens"],
                "compaction": deepcopy(compaction_config),
                "tool_results": deepcopy(context_config["tool_results"]),
                "recall": memory_config["recall"],
                "field_boosts": retrieval_config["field_boost"],
                "link_config": (
                    retrieval_config["link"]["max_added"],
                    retrieval_config["link"]["decay"],
                ),
            },
        }

    def _apply_model_runtime(self, model_client, runtime):
        self.model_client = model_client
        self.model_capabilities = runtime["capabilities"]
        self.model_budget = runtime["budget"]
        self.max_output_tokens = self.model_budget.output_tokens
        self.token_accounting = runtime["token_accounting"]
        self.context_config = runtime["context_config"]

    def _new_runtime_session(self, session_id, model_binding):
        session = {
            "record_type": SESSION_RECORD_TYPE,
            "format_version": SESSION_FORMAT_VERSION,
            "id": session_id,
            "created_at": now(),
            "workspace_root": str(self.source_root),
            "messages": [],
            "recently_recalled": [],
            "working_memory": {"task_summary": "", "recent_files": []},
            "memory": {"file_summaries": {}},
            "checkpoints": {"current_id": "", "items": {}},
            "runtime_identity": {},
            "resume_state": {},
            "permission_mode": PermissionMode.AUTO.value,
            "permission_rules": {"allow": [], "ask": [], "deny": []},
            "plan_text": "",
            "plan_revision": 0,
            "pre_plan_mode": "",
        }
        if model_binding:
            session["provider_binding"] = model_binding
        return session

    def _validate_restored_session_binding(self, session, model_binding):
        session_root = os.path.abspath(
            os.path.expanduser(str(session.get("workspace_root", "")))
        )
        source_root = os.path.abspath(os.path.expanduser(str(self.source_root)))
        if session_root != source_root:
            raise ValueError("session worktree root mismatch")
        saved_binding = session.get("provider_binding")
        if saved_binding != model_binding:
            raise ValueError("model_session_mismatch")
        if _session_has_provider_state(session) and (
            not isinstance(saved_binding, dict)
            or saved_binding.get("protocol_family")
            not in _OPAQUE_PROVIDER_STATE_PROTOCOLS
        ):
            raise ValueError("model_session_mismatch")

    @staticmethod
    def _session_runtime_identities(session):
        identities = [session.get("runtime_identity", {})]
        items = session.get("checkpoints", {}).get("items", {})
        if isinstance(items, dict):
            identities.extend(
                checkpoint.get("runtime_identity", {})
                for checkpoint in items.values()
                if isinstance(checkpoint, dict)
            )
        return identities

    def _validate_session_feature_flags(self, session):
        for identity in self._session_runtime_identities(session):
            if not isinstance(identity, dict):
                continue
            identity_flags = identity.get("feature_flags", {})
            if (
                not isinstance(identity_flags, dict)
                or not identity_flags.keys() <= DEFAULT_FEATURE_FLAGS.keys()
            ):
                raise ValueError("unsupported runtime identity feature flag")

    def _configure_session(self, session, model_binding, requested_session_id):
        if session is None:
            session_id = requested_session_id or self.new_session_id()
            self.session = self._new_runtime_session(session_id, model_binding)
        else:
            self.session = self.redact_artifact(deepcopy(session))
            self._validate_restored_session_binding(self.session, model_binding)
            self._validate_session_feature_flags(self.session)
        self._ensure_session_shape()
        if (
            session is not None
            and _session_requires_bypass_permission_capability(self.session)
            and not self.bypass_permissions_available
        ):
            raise ValueError(
                "resuming bypassPermissions requires dangerous capability"
            )
        self.memory = WorkingMemory.from_dict(
            self.session.get("working_memory"), workspace_root=self.root
        )
        self._sync_working_memory()

    def _configure_memory_and_tools(self):
        workspace_memory_root = self.project_state_root / "memory"
        user_memory_root = Path.home() / ".pony" / "memory"
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
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)

    def _persist_initialized_session(self):
        session_exists = self.session_store.path_for(self.session["id"]).exists()
        self.resume_state = self.evaluate_resume_state()
        if session_exists:
            self.session_path = self.session_store.append_messages(
                self.session["id"],
                (),
                state_updates={
                    "resume_state": self.session.get("resume_state", {}),
                    "runtime_identity": self.session.get("runtime_identity", {}),
                },
            )
        else:
            self.session_path = self.session_store.save(self.session)

    def _reset_turn_state(self):
        self.current_task_state = None
        self.current_run_dir = None
        self.last_request_metadata = {}
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }
        self._permission_turn = None

    @classmethod
    def from_session(
        cls,
        model_client,
        workspace,
        session_store,
        session_id,
        *,
        options=None,
        resume_permission_mode=None,
        resume_permission_rule_updates=(),
    ):
        options = RuntimeOptions() if options is None else options
        if not isinstance(options, RuntimeOptions):
            raise TypeError("options must be a RuntimeOptions instance")
        session_store.path_for(session_id)
        preflight_legacy_sandbox_resume(workspace.repo_root, session_id)
        redaction_env = options.redaction_env
        trusted_redaction_env = options.trusted_redaction_env
        secret_env_names = options.secret_env_names or ()
        source_root = workspace.repo_root
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
        options = replace(
            options,
            redaction_env=redaction_env,
            trusted_redaction_env=trusted_redaction_env,
            secret_env_names=tuple(configured_names),
        )
        session = session_store.load_for_resume(session_id)
        permission_mode_update = None
        permission_pre_mode = None
        if resume_permission_mode is not None:
            resume_permission_mode = validate_permission_mode(resume_permission_mode)
            if (
                resume_permission_mode == PermissionMode.BYPASS_PERMISSIONS.value
                and not options.allow_dangerously_skip_permissions
            ):
                raise ValueError(
                    "bypassPermissions requires dangerous capability"
                )
            if session.get("permission_mode") != resume_permission_mode:
                permission_mode_update = resume_permission_mode
                previous_mode = session.get("permission_mode")
                session = deepcopy(session)
                session["permission_mode"] = resume_permission_mode
                session["pre_plan_mode"] = ""
                if resume_permission_mode == PermissionMode.PLAN.value:
                    permission_pre_mode = previous_mode
                    if (
                        previous_mode == PermissionMode.BYPASS_PERMISSIONS.value
                        and not options.allow_dangerously_skip_permissions
                    ):
                        permission_pre_mode = PermissionMode.DEFAULT.value
                    session["pre_plan_mode"] = permission_pre_mode
        agent = cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session,
            options=options,
        )
        resume_permission_rule_updates = tuple(resume_permission_rule_updates)
        if permission_mode_update is not None or resume_permission_rule_updates:
            session_store.update_permissions(
                session_id,
                mode=permission_mode_update,
                pre_mode=permission_pre_mode,
                rule_updates=resume_permission_rule_updates,
            )
            agent._reload_session_projection()
        return agent

    def _ensure_session_shape(self):
        if (
            self.session.get("record_type") != SESSION_RECORD_TYPE
            or type(self.session.get("format_version")) is not int
            or self.session["format_version"] != SESSION_FORMAT_VERSION
        ):
            raise ValueError("Pony requires a current session")
        if not isinstance(self.session.get("messages"), list):
            raise ValueError("session messages must be a list")
        try:
            validate_permission_mode(self.session.get("permission_mode"))
        except ValueError as exc:
            raise ValueError("invalid permission mode") from exc
        if not isinstance(self.session.get("recently_recalled"), list):
            self.session["recently_recalled"] = []
        existing_memory = self.session.get("memory")
        if not isinstance(existing_memory, dict):
            existing_memory = {}
        working_source = self.session.get("working_memory") or existing_memory or {}
        self.session["working_memory"] = WorkingMemory.from_dict(
            working_source, workspace_root=self.root
        ).to_dict()
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
        invalidated = memorylib.invalidate_stale_file_summaries_dict(
            summaries, self.root
        )
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
        tools = toolkit.build_tool_registry(self.tool_context())
        tools["read_plan"]["run"] = self._read_plan
        tools["write_plan"]["run"] = self._write_plan
        tools["exit_plan_mode"]["run"] = self._exit_plan_mode
        return tools

    def current_model_binding(self):
        binding = self.session.get("provider_binding")
        if not isinstance(binding, dict):
            raise ValueError("model_session_mismatch")
        return deepcopy(binding)

    def set_model(self, model):
        if self._permission_turn is not None:
            raise RuntimeError("permission_turn_active")
        model = validate_model_name(model)
        if self.redact_artifact(model) != model:
            raise ValueError("model_invalid")
        current = self.current_model_binding()
        tree = self.session_store.load_tree(self.session["id"])
        durable_session = tree.projection
        if durable_session.get("provider_binding") != current:
            raise ValueError("model_session_mismatch")
        if current.get("model") == model:
            return None
        if _session_has_provider_state(durable_session):
            raise ValueError("model_session_mismatch")
        if not callable(self.model_client_factory):
            raise ValueError("model switching is unavailable")
        try:
            candidate_client = self.model_client_factory(model)
        except (TypeError, ValueError) as exc:
            raise ValueError("model_session_mismatch") from exc
        candidate = getattr(candidate_client, "provider_binding", None)
        if (
            not isinstance(candidate, dict)
            or candidate.keys() != {"protocol_family", "model", "endpoint_hash"}
            or candidate.get("model") != model
            or candidate.get("protocol_family") != current.get("protocol_family")
            or candidate.get("endpoint_hash") != current.get("endpoint_hash")
        ):
            raise ValueError("model_session_mismatch")
        runtime = self._resolve_model_runtime(candidate_client)
        entry = self.session_store.set_provider_model(
            self.session["id"],
            candidate,
            expected_binding=current,
            expected_leaf_id=tree.leaf_id,
        )
        if entry is None:
            return None
        self._apply_model_runtime(candidate_client, runtime)
        self.delegate_model_client_factory = lambda: self.model_client_factory(model)
        self.session["provider_binding"] = deepcopy(candidate)
        self._reload_session_projection()
        return deepcopy(candidate)

    def current_permission_mode(self):
        turn = getattr(self, "_permission_turn", None)
        if turn is not None:
            return turn["mode"]
        return self.session["permission_mode"]

    def visible_tools(self):
        if self._permission_turn is not None:
            return self._permission_turn["tools"]
        return self._visible_tools_for(self.session["permission_mode"])

    def _visible_tools_for(self, mode):
        plan_tools = {"read_plan", "write_plan", "exit_plan_mode"}
        if self.read_only:
            return {
                name: tool
                for name, tool in self.tools.items()
                if tool.get("effect_class") == "read_only"
                and name != "run_shell"
                and name not in plan_tools
            }
        if mode != PermissionMode.PLAN.value:
            return {
                name: tool for name, tool in self.tools.items() if name not in plan_tools
            }
        return {
            name: tool
            for name, tool in self.tools.items()
            if (
                tool.get("effect_class") == "read_only" and name != "run_shell"
            )
            or name in plan_tools
        }

    def begin_permission_turn(self):
        if self._permission_turn is not None:
            raise RuntimeError("permission_turn_active")
        mode = self.session["permission_mode"]
        self._permission_turn = {
            "mode": mode,
            "tools": self._visible_tools_for(mode),
        }

    def end_permission_turn(self):
        self._permission_turn = None

    def refresh_permission_turn_after_plan_exit(self):
        if self._permission_turn is None or self._permission_turn["mode"] != "plan":
            raise RuntimeError("permission_turn_not_in_plan")
        mode = self.session["permission_mode"]
        if mode == PermissionMode.PLAN.value:
            raise RuntimeError("plan_mode_still_active")
        self._permission_turn = {"mode": mode, "tools": self._visible_tools_for(mode)}

    def set_permission_mode(self, mode):
        result = self.update_permissions(mode=mode)
        return result["mode_entry"]

    def update_permissions(self, *, mode=None, rule_updates=()):
        if self._permission_turn is not None:
            raise RuntimeError("permission_turn_active")
        if mode is not None:
            mode = validate_permission_mode(mode)
        if (
            mode == PermissionMode.BYPASS_PERMISSIONS.value
            and not self.bypass_permissions_available
        ):
            raise ValueError(
                "bypassPermissions requires dangerous capability"
            )
        rule_updates = tuple(
            (str(name), str(behavior)) for name, behavior in rule_updates
        )
        if any(name not in toolkit.legal_tool_names() for name, _ in rule_updates):
            raise ValueError("unknown permission rule tool")
        result = self.session_store.update_permissions(
            self.session["id"],
            mode=mode,
            rule_updates=rule_updates,
        )
        if result["mode_entry"] is not None or result["rules"] is not None:
            self._reload_session_projection()
        return result

    def permission_rules(self):
        rules = self.session.get("permission_rules", {})
        return {
            behavior: list(rules.get(behavior, []))
            for behavior in ("allow", "ask", "deny")
        }

    def permission_for_tool(self, name):
        name = str(name)
        rules = self.permission_rules()
        for behavior in ("deny", "ask", "allow"):
            if name in rules[behavior]:
                return behavior
        return None

    def set_permission_rule(self, name, behavior):
        return self.set_permission_rules(((name, behavior),))

    def set_permission_rules(self, updates):
        return self.update_permissions(rule_updates=updates)["rules"]

    def current_plan(self):
        return str(self.session.get("plan_text", "") or "")

    def current_plan_revision(self):
        return int(self.session.get("plan_revision", 0) or 0)

    def _read_plan(self, _args):
        if self.current_permission_mode() != PermissionMode.PLAN.value:
            raise ValueError("read_plan requires plan mode")
        return self.current_plan() or "(no plan saved)"

    def _write_plan(self, args):
        if self.current_permission_mode() != PermissionMode.PLAN.value:
            raise ValueError("write_plan requires plan mode")
        return self.save_plan_text(args.get("plan", ""))

    def save_plan_text(
        self,
        value,
        *,
        expected_leaf_id=None,
        expected_plan_text=None,
        expected_revision=None,
        expected_permission_mode=None,
    ):
        if self.current_permission_mode() != PermissionMode.PLAN.value:
            raise ValueError("write_plan requires plan mode")
        plan = str(value).strip()
        if not plan:
            raise ValueError("plan must not be empty")
        self.validate_tool("write_plan", {"plan": plan})
        try:
            entry = self.session_store.set_plan_text(
                self.session["id"],
                plan,
                expected_leaf_id=expected_leaf_id,
                expected_plan_text=expected_plan_text,
                expected_revision=expected_revision,
                expected_permission_mode=expected_permission_mode,
            )
        except sessionstorelib.PlanApprovalChanged:
            self._reload_session_projection()
            raise
        if entry is not None:
            self._reload_session_projection()
        return "plan saved"

    def _exit_plan_mode(self, args):
        if self.current_permission_mode() != PermissionMode.PLAN.value:
            raise ValueError("exit_plan_mode requires plan mode")
        if (
            self.session.get("pre_plan_mode")
            == PermissionMode.BYPASS_PERMISSIONS.value
            and not self.bypass_permissions_available
        ):
            raise ValueError("bypassPermissions requires dangerous capability")
        try:
            self.session_store.exit_plan_mode(
                self.session["id"],
                plan_text=args["plan"],
                plan_revision=args["revision"],
                expected_leaf_id=args["expected_leaf_id"],
            )
        except sessionstorelib.PlanApprovalChanged:
            self._reload_session_projection()
            raise
        self._reload_session_projection()
        return "plan approved; permission mode restored"

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
        return {name: tool for name, tool in tools.items() if name in allowed}

    def tool_signature(self):
        return tool_signature(self.tools)

    def build_prefix(self):
        return build_prompt_prefix(workspace=self.workspace, tools=self.tools)

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(
            getattr(self, "prefix_state", None), "workspace_fingerprint", None
        )

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(
            self.root,
            executables=self.trusted_executables,
            repo_root_override=self.root,
        )
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = (
            force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        )
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = (
            self.build_prefix()
            if workspace_changed or force or previous_hash is None
            else self.prefix_state
        )
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
        return redaction.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return redaction.is_secret_env_name(
            name, secret_env_names=self.secret_env_names
        )

    def configured_secret_env_items(self):
        return redaction.configured_secret_env_items(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def detected_secret_env_items(self):
        return redaction.detected_secret_env_items(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def secret_env_summary(self):
        return redaction.secret_env_summary(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def detected_secret_env_summary(self):
        return redaction.detected_secret_env_summary(
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def redact_text(self, text):
        return redaction.redact_text(
            str(text),
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def redact_artifact(self, value, key=None):
        return redaction.redact_artifact(
            value,
            key=key,
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        )

    def shell_env(self):
        return redaction.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def emit_trace(self, task_state, event, payload=None):
        redacted_payload = self.redact_artifact(payload or {})
        envelope = project_trace_event(
            task_state,
            event,
            redacted_payload,
            created_at=now(),
        )
        self.run_store.append_trace(task_state, envelope)
        if callable(self._trace_listener):
            try:
                listener_event = deepcopy(envelope)
                if event == "tool_started" and "args" in redacted_payload:
                    listener_event["args"] = deepcopy(redacted_payload["args"])
                elif event == "tool_executed" and "result" in redacted_payload:
                    listener_event["result"] = deepcopy(redacted_payload["result"])
                self._trace_listener(listener_event)
            except Exception:  # noqa: BLE001 - optional UI cannot break durable trace
                pass
        return envelope

    def create_checkpoint(
        self,
        task_state,
        user_message,
        trigger,
        *,
        modified_files=(),
        label="",
    ):
        return checkpointlib.create_checkpoint(
            self,
            task_state,
            user_message,
            trigger,
            modified_files=modified_files,
            label=label,
        )

    def create_manual_checkpoint(self, label=""):
        checkpoint = checkpointlib.create_manual_checkpoint(self, label=label)
        self._reload_session_projection()
        return checkpoint

    def infer_next_step(self, task_state):
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到派生的 task working set。

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
            memorylib.set_file_summary_dict(
                summaries, canonical_path, summary, workspace_root=self.root
            )
        elif name in {"write_file", "patch_file"}:
            memorylib.invalidate_file_summary_dict(
                summaries, canonical_path, workspace_root=self.root
            )
        self.session["memory"] = {"file_summaries": summaries}

    def ask(self, user_message, *, skill=None):
        from pony.agent.loop import AgentLoop

        return AgentLoop(self).run(user_message, skill=skill)

    def compact_session(
        self,
        *,
        focus="",
        reason="manual",
        keep_recent_tokens=None,
        expected_leaf_id=None,
        model_observer=None,
    ):
        return compact_session_tree(
            self,
            focus=focus,
            reason=reason,
            keep_recent_tokens=keep_recent_tokens,
            expected_leaf_id=expected_leaf_id,
            model_observer=model_observer,
        )

    def _reload_session_projection(self):
        self.session = self.session_store.load(self.session["id"])
        self.memory = WorkingMemory.from_dict(
            self.session.get("working_memory"),
            workspace_root=self.root,
        )
        self.session_path = self.session_store.path_for(self.session["id"])
        return self.session

    def rewind_session(
        self,
        entry_id,
        *,
        summary=False,
        focus="",
        expected_leaf_id=None,
    ):
        if summary:
            result = rewind_with_branch_summary(
                self,
                entry_id,
                focus=focus,
                expected_leaf_id=expected_leaf_id,
            )
        else:
            current_leaf = expected_leaf_id
            if current_leaf is None:
                current_leaf = self.session_store.load_tree(self.session["id"]).leaf_id
            result = self.session_store.rewind(
                self.session["id"],
                entry_id,
                expected_leaf_id=current_leaf,
            )
        self._reload_session_projection()
        return result

    def fork_session(self, entry_id, *, expected_leaf_id=None):
        current_leaf = expected_leaf_id
        if current_leaf is None:
            current_leaf = self.session_store.load_tree(self.session["id"]).leaf_id
        result = self.session_store.fork(
            self.session["id"],
            entry_id,
            expected_leaf_id=current_leaf,
        )
        self._reload_session_projection()
        return result

    def execute_tool(self, name, args):
        result = self.tool_executor.execute(name, args)
        safe_result = ToolExecutionResult(
            content=self.redact_text(result.content),
            metadata=self.redact_artifact(result.metadata),
        )
        self._last_tool_result_metadata = dict(safe_result.metadata)
        return safe_result

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
        return (
            "task_"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

    @staticmethod
    def new_run_id():
        return (
            "run_"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

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
        workspace_status = str(self.workspace.status or "").strip()
        commit = ""
        if self.workspace.recent_commits:
            candidate = str(self.workspace.recent_commits[0]).split(maxsplit=1)[0]
            if candidate and all(
                character in "0123456789abcdefABCDEF" for character in candidate
            ):
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
                "dirty": workspace_status not in {"", "clean"},
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
                "evidence_complete": bool(
                    execution.get("transport_evidence_complete", False)
                ),
                "attempt_origin_counts": dict(
                    execution.get("attempt_origin_counts", {})
                ),
                "failure_reason_counts": dict(
                    execution.get("failure_reason_counts", {})
                ),
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
            "effects": {
                "changed_files": len(changed_paths),
                "partial_successes": int(tool_report.get("partial_successes", 0) or 0),
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
        validate_tool_arguments(self.tool_context(), name, args)
        if name == "write_plan":
            plan = args.get("plan", "")
            if len(plan.encode("utf-8")) > sessionstorelib.MAX_PLAN_TEXT_BYTES:
                raise ValueError("plan text exceeds 12 KiB")
            if self.redact_artifact(plan) != plan:
                raise SensitiveToolError("sensitive_content_block")
        if name == "memory_save":
            note_tokens = self.token_accounting.count_text(args.get("note", ""))
            if note_tokens > 1_024:
                raise ValueError("memory_save note exceeds 1024 model tokens")
        if name == "delegate_worktrees" and self.redact_artifact(args) != args:
            raise SensitiveToolError("sensitive_content_block")

    def tool_context(self):
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_delegate=self.spawn_delegate,
            spawn_worktree_agents=self.spawn_worktree_agents,
            memory_store=self.memory_store,
            memory_retrieval=self.memory_retrieval,
            repo_map=self.repo_map,
            trusted_executables=self.trusted_executables,
            redaction_env=self.redaction_env,
            secret_env_names=self.secret_env_names,
            workspace_root_identity=self.workspace_root_identity,
        )

    def spawn_delegate(self, args):
        self.validate_tool("delegate", args)
        task = str(args.get("task", "")).strip()
        name = str(args.get("name", "delegate")).strip()
        if not callable(self.delegate_model_client_factory):
            raise ValueError("delegate model client factory is not configured")
        child_model_client = self.delegate_model_client_factory()
        if child_model_client is self.model_client:
            raise ValueError("delegate model client factory reused the parent client")
        child_session_id = f"delegate-{name}-{self.new_session_id()}"
        child_root = self.project_state_root / "delegates" / f"{name}-{child_session_id}"
        child = Pony(
            model_client=child_model_client,
            workspace=self.workspace,
            session_store=sessionstorelib.SessionStore(child_root / "sessions"),
            options=RuntimeOptions(
                run_store=RunStore(child_root / "runs"),
                project_trusted=self.project_trusted,
                max_steps=int(args.get("max_steps", MAX_DELEGATE_STEPS)),
                max_output_tokens=self.max_output_tokens,
                context_window=self.model_capabilities.context_window,
                depth=self.depth + 1,
                max_depth=self.depth + 1,
                read_only=True,
                secret_env_names=self.secret_env_names,
                redaction_env=self.redaction_env,
                trusted_redaction_env=True,
                trusted_executables=self.trusted_executables,
                shell_env_allowlist=self.shell_env_allowlist,
                project_config=self.project_config,
                allowed_tools=self.allowed_tools,
                session_id=child_session_id,
            ),
        )
        child.set_permission_mode(PermissionMode.DONT_ASK.value)
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.memory.set_task_summary(task)
        child._sync_working_memory()
        result = child.ask(task)
        if len(result) > MAX_DELEGATE_RESULT_CHARS:
            result = result[:MAX_DELEGATE_RESULT_CHARS] + "\n[delegate result truncated]"
        prefix = "delegate_result" if name == "delegate" else f"delegate_result[{name}]"
        return prefix + ":\n" + result

    def spawn_worktree_agents(self, args):
        from pony.runtime.worktree_agents import run_worktree_agents

        return run_worktree_agents(self, args)

    def approve(self, name, args):
        if self.read_only:
            return False
        safe_args = self.redact_artifact(args)
        if callable(self._approval_prompt):
            try:
                return self._approval_prompt(name, safe_args) is True
            except Exception:  # noqa: BLE001 - approval UI fails closed
                return False
        try:
            answer = input(
                f"approve {name} {json.dumps(safe_args, ensure_ascii=True)}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def reset(self):
        tree = self.session_store.load_tree(self.session["id"])
        candidate = deepcopy(tree.projection)
        candidate["messages"] = []
        candidate["recently_recalled"] = []
        candidate.pop("_recall_errors", None)
        candidate["working_memory"] = {
            "task_summary": "",
            "recent_files": [],
        }
        candidate["memory"] = {"file_summaries": {}}
        checkpoints = candidate.setdefault(
            "checkpoints", {"current_id": "", "items": {}}
        )
        checkpoints["current_id"] = ""
        checkpoints.setdefault("items", {})
        candidate["resume_state"] = {}
        saved_path = self.session_store.save(
            candidate,
            force_branch=True,
            expected_leaf_id=tree.leaf_id,
        )
        self.session = candidate
        self.session_path = saved_path
        self.memory = WorkingMemory(workspace_root=self.root)
        self.resume_state = {}
        self.last_request_metadata = {}

    def path(self, raw_path):
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
