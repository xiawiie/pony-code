"""Build one provider request from canonical Session context."""

from __future__ import annotations

import hashlib

from pico import security as securitylib
from pico.messages import build_request_messages, message_metrics
from pico.model_capabilities import (
    ModelBudget,
    ModelCapabilities,
    TokenAccounting,
    build_model_budget,
)
from pico.session_store import SessionContextView


class ContextBudgetExceeded(RuntimeError):
    code = "context_budget_exceeded"


class SystemContextTooLarge(ContextBudgetExceeded):
    code = "system_context_too_large"


def _convert_pico_tool_to_anthropic(name, spec):
    props = {}
    required = []
    for arg_name, sig in (spec.get("schema") or {}).items():
        sig_str = str(sig)
        props[arg_name] = {"type": "integer" if "int" in sig_str else "string"}
        if "=" not in sig_str:
            required.append(arg_name)
    description = str(spec.get("description", "") or "")
    if spec.get("risky"):
        description = (description + " Requires user approval before execution.").strip()
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


def _build_tools_list(pico_tools):
    if not pico_tools:
        return []
    return [
        _convert_pico_tool_to_anthropic(name, spec)
        for name, spec in sorted(pico_tools.items())
    ]


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
        budget = self.budget
        accounting = self.accounting
        system_text = str(getattr(self.agent, "prefix", "") or "")
        system = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        tools = _build_tools_list(getattr(self.agent, "tools", {}) or {})
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
                    candidate_view.canonical_message_count
                    == len(canonical_messages)
                    and candidate_view.canonical_last_message
                    == canonical_messages[-1]
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
        messages = build_request_messages(
            history_messages,
            rendered_user=rendered_user,
            runtime_feedback=runtime_feedback,
        )
        _, messages = securitylib.sanitize_provider_payload(
            [],
            messages,
            env=self.agent.redaction_env,
            secret_env_names=self.agent.secret_env_names,
        )
        counted = accounting.count_request(
            system=system,
            tools=tools,
            messages=messages,
        )
        if counted.total > budget.input_limit:
            raise ContextBudgetExceeded(
                "context_budget_exceeded: assembled request uses "
                f"{counted.total} tokens; input limit is {budget.input_limit}. "
                "Run /compact or enable automatic compaction."
            )
        self.agent._pending_token_anchor = counted.anchor_candidate

        source_rows = []
        selected_source_tokens = 0
        for source in sources:
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
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "user"
                and isinstance(messages[index].get("content"), str)
            ),
            None,
        )
        current_request_tokens = (
            accounting.count_message(messages[current_message_index])
            if current_message_index is not None
            else 0
        )
        history_tokens = max(0, counted.messages - current_request_tokens)
        raw_current_user = getattr(
            injection_snapshot,
            "current_user",
            rendered_user,
        )
        safe_current_user, _ = securitylib.sanitize_provider_payload(
            str(raw_current_user),
            [],
            env=self.agent.redaction_env,
            secret_env_names=self.agent.secret_env_names,
        )
        current_user_tokens = accounting.count_text(safe_current_user)
        if str(runtime_feedback or "").strip():
            safe_runtime_feedback, _ = securitylib.sanitize_provider_payload(
                str(runtime_feedback),
                [],
                env=self.agent.redaction_env,
                secret_env_names=self.agent.secret_env_names,
            )
            runtime_feedback_tokens = accounting.count_text(safe_runtime_feedback)
        else:
            runtime_feedback_tokens = 0
        capabilities = budget.capabilities
        breakdown = {
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
                "actual": pinned_tokens,
            },
            "sources": source_rows,
            "current_request": {
                "actual_tokens": current_request_tokens,
                "user_tokens": current_user_tokens,
                "runtime_feedback_tokens": runtime_feedback_tokens,
                "source_tokens": selected_source_tokens,
            },
            "history": {
                "budget": max(
                    0,
                    budget.input_limit - pinned_tokens - current_request_tokens,
                ),
                "actual_tokens": history_tokens,
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
                    else history_tokens + current_user_tokens
                ),
                "reason": (
                    context_view.compaction_reason
                    if context_view is not None
                    else "not_compacted"
                ),
                "compression_ratio": (
                    context_view.compression_ratio
                    if context_view is not None
                    else 1.0
                ),
                "entry_id": (
                    context_view.compaction_entry_id
                    if context_view is not None
                    else ""
                ),
            },
        }
        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []
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
        metadata = {
            "system_prefix_hash": hashlib.sha256(system_text.encode()).hexdigest(),
            "system_tokens": counted.system,
            "tools_tokens": counted.tools,
            "prompt_cache_supported": bool(
                getattr(self.agent.model_client, "supports_prompt_cache", False)
            ),
            **message_metrics(messages, token_of=accounting.count_text),
            "dropped_messages": 0,
            "cache_control_breakpoints": list(breakpoints),
            "runtime_feedback_present": bool(str(runtime_feedback or "").strip()),
            "recall.error_count": int(recall_errors.get("count", 0) or 0),
            "recall.last_error": str(recall_errors.get("last", "") or ""),
            "token_count_mode": counted.mode,
            "context_breakdown": breakdown,
            "recall_commit_paths": [
                path
                for source in sources
                if source.name == "recalled_memory"
                for path in source.selected_memory_paths
            ],
            **provider_metadata,
            **dict(injection_telemetry or {}),
            **dict(preflight_metadata or {}),
        }
        return {
            "system": system,
            "tools": tools,
            "messages": messages,
            "cache_control_breakpoints": breakpoints,
        }, metadata

    def count_tokens(self, text):
        return self.accounting.count_text(text)
