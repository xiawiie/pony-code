"""Build one provider request from canonical Session context."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from collections.abc import Mapping

from pony.security import redaction as securitylib
from pony.agent.messages import build_request_messages, message_metrics
from pony.agent.model_capabilities import (
    ModelBudget,
    ModelCapabilities,
    TokenAccounting,
    build_model_budget,
)
from pony.state.session_store import SessionContextView


_PLAN_MODE_REMINDER = """
<system-reminder>
Plan mode is active. Explore the repository and design an implementation approach.
Do not edit workspace files or run non-read-only tools. Use read_plan to inspect the
current plan, write_plan to save the complete plan, and exit_plan_mode only when the
plan is ready for user approval. Do not ask for plan approval in ordinary text.
</system-reminder>
""".strip()


class ContextBudgetExceeded(RuntimeError):
    code = "context_budget_exceeded"


class SystemContextTooLarge(ContextBudgetExceeded):
    code = "system_context_too_large"


def _convert_pony_tool_to_anthropic(name, spec):
    props = {}
    required = []
    for arg_name, sig in (spec.get("schema") or {}).items():
        if isinstance(sig, Mapping):
            props[arg_name] = deepcopy(dict(sig))
            if "default" not in sig:
                required.append(arg_name)
            continue
        sig_str = str(sig)
        props[arg_name] = {"type": "integer" if "int" in sig_str else "string"}
        if "=" not in sig_str:
            required.append(arg_name)
    description = str(spec.get("description", "") or "")
    if spec.get("risky"):
        description = (
            description + " Requires user approval before execution."
        ).strip()
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


def _build_tools_list(pony_tools):
    if not pony_tools:
        return []
    return [
        _convert_pony_tool_to_anthropic(name, spec)
        for name, spec in sorted(pony_tools.items())
    ]


@dataclass(frozen=True)
class _PinnedRequestContext:
    system: list
    tools: list
    system_text: str
    tokens: int


@dataclass(frozen=True)
class _SessionRequestContext:
    session: dict
    view: object
    history_messages: list
    sources: tuple
    rendered_user: str
    current_user: object
    runtime_feedback: str


@dataclass(frozen=True)
class _AssembledRequest:
    pinned: _PinnedRequestContext
    context: _SessionRequestContext
    messages: list
    counted: object


@dataclass(frozen=True)
class _RequestUsage:
    source_rows: list
    selected_source_tokens: int
    current_request_tokens: int
    history_tokens: int
    current_user_tokens: int
    runtime_feedback_tokens: int


class ContextManager:
    def __init__(self, agent):
        self.agent = agent

    @property
    def accounting(self):
        value = getattr(self.agent, "token_accounting", None)
        if isinstance(value, TokenAccounting):
            return value
        value = TokenAccounting(
            getattr(getattr(self.agent, "model_client", None), "count_tokens", None)
        )
        self.agent.token_accounting = value
        return value

    @property
    def budget(self):
        value = getattr(self.agent, "model_budget", None)
        if isinstance(value, ModelBudget):
            return value
        capabilities = getattr(self.agent, "model_capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            capabilities = ModelCapabilities(
                context_window=128_000,
                max_output_tokens=16_384,
                token_counter_mode="provider_usage_or_estimate",
                source="fallback",
            )
        value = build_model_budget(capabilities)
        self.agent.model_budget = value
        return value

    def build_request(
        self,
        *,
        injection_snapshot,
        injection_telemetry,
        preflight_metadata,
        runtime_feedback="",
    ):
        pinned = self._build_pinned_context()
        context = self._load_session_context(injection_snapshot, runtime_feedback)
        request = self._assemble_request(pinned, context)
        usage = self._measure_request_usage(request)
        breakdown = self._build_context_breakdown(request, usage)
        breakpoints = [len(request.messages) - 2] if len(request.messages) >= 2 else []
        metadata = self._build_request_metadata(
            request,
            usage,
            breakdown,
            breakpoints,
        )
        metadata.update(dict(injection_telemetry or {}))
        metadata.update(dict(preflight_metadata or {}))
        return {
            "system": pinned.system,
            "tools": pinned.tools,
            "messages": request.messages,
            "cache_control_breakpoints": breakpoints,
        }, metadata

    def _build_pinned_context(self):
        budget = self.budget
        accounting = self.accounting
        system_text = str(getattr(self.agent, "prefix", "") or "")
        current_mode = getattr(self.agent, "current_permission_mode", None)
        permission_mode = (
            current_mode()
            if callable(current_mode)
            else (getattr(self.agent, "session", {}) or {}).get(
                "permission_mode", "auto"
            )
        )
        if permission_mode == "plan":
            system_text = f"{system_text}\n\n{_PLAN_MODE_REMINDER}"
        system = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        tools = _build_tools_list(self.agent.visible_tools())
        system, _ = securitylib.sanitize_provider_payload(
            system,
            [],
            env=self.agent.redaction_env,
            secret_env_names=self.agent.secret_env_names,
        )
        system_text = str(system[0].get("text", ""))
        pinned_tokens = accounting.count_json(system) + accounting.count_json(tools)
        if pinned_tokens > budget.system_tools_hard_cap:
            raise SystemContextTooLarge(
                "SystemContextTooLarge: system+tools use "
                f"{pinned_tokens} tokens; cap is {budget.system_tools_hard_cap}"
            )
        return _PinnedRequestContext(system, tools, system_text, pinned_tokens)

    def _load_session_context(self, injection_snapshot, runtime_feedback):
        session = getattr(self.agent, "session", {}) or {}
        context_view = None
        session_store = getattr(self.agent, "session_store", None)
        session_id = session.get("id") if isinstance(session, dict) else None
        if session_store is not None and session_id:
            candidate_view = session_store.context_view(session_id)
            canonical_messages = list(session.get("messages", []) or [])
            if isinstance(candidate_view, SessionContextView) and (
                not canonical_messages
                or (
                    candidate_view.canonical_message_count == len(canonical_messages)
                    and candidate_view.canonical_last_message == canonical_messages[-1]
                )
            ):
                context_view = candidate_view
        history_messages = (
            list(context_view.messages)
            if context_view is not None
            else list(session.get("messages", []) or [])
        )
        sources = tuple(getattr(injection_snapshot, "sources", ()) or ())
        rendered_user = (
            injection_snapshot.render()
            if hasattr(injection_snapshot, "render")
            else str(injection_snapshot)
        )
        return _SessionRequestContext(
            session=session,
            view=context_view,
            history_messages=history_messages,
            sources=sources,
            rendered_user=rendered_user,
            current_user=getattr(
                injection_snapshot,
                "current_user",
                rendered_user,
            ),
            runtime_feedback=runtime_feedback,
        )

    def _assemble_request(self, pinned, context):
        messages = build_request_messages(
            context.history_messages,
            rendered_user=context.rendered_user,
            runtime_feedback=context.runtime_feedback,
        )
        _, messages = securitylib.sanitize_provider_payload(
            [],
            messages,
            env=self.agent.redaction_env,
            secret_env_names=self.agent.secret_env_names,
        )
        counted = self.accounting.count_request(
            system=pinned.system,
            tools=pinned.tools,
            messages=messages,
        )
        if counted.total > self.budget.input_limit:
            raise ContextBudgetExceeded(
                "context_budget_exceeded: assembled request uses "
                f"{counted.total} tokens; input limit is {self.budget.input_limit}. "
                "Run /compact or enable automatic compaction."
            )
        self.agent._pending_token_anchor = counted.anchor_candidate
        return _AssembledRequest(pinned, context, messages, counted)

    def _measure_request_usage(self, request):
        accounting = self.accounting
        source_rows = []
        selected_source_tokens = 0
        for source in request.context.sources:
            actual = accounting.count_text(source.text) if source.text else 0
            selected_source_tokens += actual
            source_rows.append(
                {
                    "name": source.name,
                    "required": bool(source.required),
                    "hard_cap": int(getattr(source, "hard_cap", 0) or 0),
                    "actual_tokens": actual,
                    "priority": int(getattr(source, "priority", 0) or 0),
                    "status": source.status,
                    "reason": source.reason_code,
                }
            )
        current_message_index = next(
            (
                index
                for index in range(len(request.messages) - 1, -1, -1)
                if request.messages[index].get("role") == "user"
                and isinstance(request.messages[index].get("content"), str)
            ),
            None,
        )
        current_request_tokens = (
            accounting.count_message(request.messages[current_message_index])
            if current_message_index is not None
            else 0
        )
        history_tokens = max(0, request.counted.messages - current_request_tokens)
        safe_current_user, _ = securitylib.sanitize_provider_payload(
            str(request.context.current_user),
            [],
            env=self.agent.redaction_env,
            secret_env_names=self.agent.secret_env_names,
        )
        current_user_tokens = accounting.count_text(safe_current_user)
        if str(request.context.runtime_feedback or "").strip():
            safe_runtime_feedback, _ = securitylib.sanitize_provider_payload(
                str(request.context.runtime_feedback),
                [],
                env=self.agent.redaction_env,
                secret_env_names=self.agent.secret_env_names,
            )
            runtime_feedback_tokens = accounting.count_text(safe_runtime_feedback)
        else:
            runtime_feedback_tokens = 0
        return _RequestUsage(
            source_rows=source_rows,
            selected_source_tokens=selected_source_tokens,
            current_request_tokens=current_request_tokens,
            history_tokens=history_tokens,
            current_user_tokens=current_user_tokens,
            runtime_feedback_tokens=runtime_feedback_tokens,
        )

    def _build_context_breakdown(self, request, usage):
        budget = self.budget
        counted = request.counted
        context_view = request.context.view
        capabilities = budget.capabilities
        return {
            "schema_version": 2,
            "token_count_mode": counted.mode,
            "model": {
                "context_window": capabilities.context_window,
                "max_output_tokens": capabilities.max_output_tokens,
                "capabilities_source": capabilities.source,
            },
            "budget": {
                "output": budget.output_tokens,
                "reserve": budget.reserve_tokens,
                "input_limit": budget.input_limit,
                "used": counted.total,
                "remaining": budget.input_limit - counted.total,
                "within_budget": True,
                "system_tools_hard_cap": budget.system_tools_hard_cap,
                "source_pool": budget.source_pool_tokens,
            },
            "pinned": {
                "system": counted.system,
                "tools": counted.tools,
                "actual": request.pinned.tokens,
            },
            "sources": usage.source_rows,
            "current_request": {
                "actual_tokens": usage.current_request_tokens,
                "user_tokens": usage.current_user_tokens,
                "runtime_feedback_tokens": usage.runtime_feedback_tokens,
                "source_tokens": usage.selected_source_tokens,
            },
            "history": {
                "budget": max(
                    0,
                    budget.input_limit
                    - request.pinned.tokens
                    - usage.current_request_tokens,
                ),
                "actual_tokens": usage.history_tokens,
                "dropped_turns": 0,
            },
            "compaction": {
                "enabled": bool(
                    self.agent.context_config.get("compaction", {}).get("enabled", True)
                ),
                "summary_tokens": (
                    context_view.summary_tokens if context_view is not None else 0
                ),
                "tail_tokens": (
                    context_view.tail_tokens
                    if context_view is not None and context_view.compaction_entry_id
                    else usage.history_tokens + usage.current_user_tokens
                ),
                "reason": (
                    context_view.compaction_reason
                    if context_view is not None
                    else "not_compacted"
                ),
                "compression_ratio": (
                    context_view.compression_ratio if context_view is not None else 1.0
                ),
                "entry_id": (
                    context_view.compaction_entry_id if context_view is not None else ""
                ),
            },
        }

    def _build_request_metadata(self, request, usage, breakdown, breakpoints):
        session = request.context.session
        recall_errors = (
            session.get("_recall_errors", {}) if isinstance(session, dict) else {}
        )
        recall_errors = recall_errors if isinstance(recall_errors, dict) else {}
        provider_metadata = getattr(
            self.agent.model_client,
            "provider_metadata",
            {},
        )
        provider_metadata = (
            dict(provider_metadata) if isinstance(provider_metadata, dict) else {}
        )
        return {
            "system_prefix_hash": hashlib.sha256(
                request.pinned.system_text.encode()
            ).hexdigest(),
            "system_tokens": request.counted.system,
            "tools_tokens": request.counted.tools,
            "prompt_cache_supported": bool(
                getattr(self.agent.model_client, "supports_prompt_cache", False)
            ),
            **message_metrics(request.messages, token_of=self.accounting.count_text),
            "dropped_messages": 0,
            "cache_control_breakpoints": list(breakpoints),
            "runtime_feedback_present": bool(
                str(request.context.runtime_feedback or "").strip()
            ),
            "recall.error_count": int(recall_errors.get("count", 0) or 0),
            "recall.last_error": str(recall_errors.get("last", "") or ""),
            "token_count_mode": request.counted.mode,
            "context_breakdown": breakdown,
            "recall_commit_paths": [
                path
                for source in request.context.sources
                if source.name == "recalled_memory"
                for path in source.selected_memory_paths
            ],
            **provider_metadata,
        }

    def count_tokens(self, text):
        return self.accounting.count_text(text)
