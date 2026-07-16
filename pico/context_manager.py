"""Build the provider request from Pico's canonical messages."""

from __future__ import annotations

import hashlib
import json

from pico import security as securitylib
from pico.messages import build_request_messages, message_content_text, message_metrics


# Pinned system/tools input is never a truncation candidate.
SYSTEM_TOOLS_HARD_CAP = 20000
CONTEXT_SAFETY_MARGIN_TOKENS = 512
OPTIONAL_DROP_ORDER = ("memory_index", "project_structure", "workspace_state", "recalled_memory", "checkpoint")


class ContextBudgetExceeded(RuntimeError):
    code = "context_budget_exceeded"


def _convert_pico_tool_to_anthropic(name, spec):
    """Convert one Pico tool definition to the Anthropic request shape."""
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
    """Build a deterministic provider tool list."""
    if not pico_tools:
        return []
    return [
        _convert_pico_tool_to_anthropic(name, spec)
        for name, spec in sorted(pico_tools.items())
    ]


def _is_top_level_user_message(message):
    return message.get("role") == "user" and isinstance(message.get("content"), str)


def _drop_old_turns(messages, soft_cap_tokens, floor_count, token_of):
    """Drop oldest complete turn units while preserving the message floor."""
    if not messages:
        return list(messages), 0
    message_count = len(messages)
    floor_start = max(0, message_count - floor_count)
    while floor_start > 0 and not _is_top_level_user_message(messages[floor_start]):
        floor_start -= 1

    turn_starts = [
        index
        for index in range(floor_start)
        if _is_top_level_user_message(messages[index])
    ]
    boundaries = turn_starts + [floor_start]
    kept_start = 0
    total = sum(token_of(message) for message in messages)
    for boundary in boundaries:
        if total <= soft_cap_tokens:
            break
        if boundary <= kept_start:
            continue
        total -= sum(token_of(message) for message in messages[kept_start:boundary])
        kept_start = boundary
    return list(messages[kept_start:]), kept_start


class ContextManager:
    def __init__(self, agent):
        self.agent = agent

    def _build_with_counter(self, *, injection_snapshot, injection_telemetry, preflight_metadata, runtime_feedback, token_of, mode):
        system_text = str(getattr(self.agent, "prefix", "") or "")
        system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        tools = _build_tools_list(getattr(self.agent, "tools", {}) or {})
        session = getattr(self.agent, "session", {}) or {}
        config = getattr(self.agent, "context_config", None)
        config = config if isinstance(config, dict) else {}
        total = int(config.get("total_budget_hard_cap", 100000))
        reserved = int(getattr(self.agent, "max_new_tokens", 2048))
        input_limit = total - reserved - CONTEXT_SAFETY_MARGIN_TOKENS
        system, _ = securitylib.sanitize_provider_payload(system, [], env=self.agent.redaction_env, secret_env_names=self.agent.secret_env_names)
        system_text = str(system[0].get("text", ""))
        system_tokens = token_of(system_text)
        tools_tokens = token_of(json.dumps(tools, sort_keys=False))
        pinned_cap = int(config.get("system_tools_hard_cap", SYSTEM_TOOLS_HARD_CAP))
        if system_tokens + tools_tokens > pinned_cap:
            raise RuntimeError(f"SystemTooBig: system+tools tokens {system_tokens + tools_tokens} exceed {pinned_cap}. Inspect workspace.stable_text() or tools schema.")

        sources = list(getattr(injection_snapshot, "sources", ()) or ())
        included = {source.name for source in sources if source.text}
        rendered_user = injection_snapshot.render(included) if hasattr(injection_snapshot, "render") else injection_snapshot
        messages = build_request_messages(list(session.get("messages", []) or []), rendered_user=rendered_user, runtime_feedback=runtime_feedback)
        _, messages = securitylib.sanitize_provider_payload([], messages, env=self.agent.redaction_env, secret_env_names=self.agent.secret_env_names)
        def msg_token(message):
            tokens = token_of(message_content_text(message))
            provider_state = message.get("_pico_provider_state")
            if provider_state:
                tokens += token_of(
                    json.dumps(
                        provider_state,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            return tokens
        before_tokens = sum(msg_token(message) for message in messages)
        soft_cap = int(config.get("history_soft_cap", 40000))
        floor_count = int(config.get("history_floor_messages", 6))
        messages, dropped_messages = _drop_old_turns(messages, soft_cap, floor_count, msg_token)

        def used():
            return system_tokens + tools_tokens + sum(msg_token(message) for message in messages)

        dropped_sources = []
        for name in OPTIONAL_DROP_ORDER:
            if used() <= input_limit:
                break
            source = next((item for item in sources if item.name == name), None)
            if source is None or not source.text or source.required:
                continue
            included.discard(name)
            dropped_sources.append(name)
            rendered = injection_snapshot.render(included)
            messages = build_request_messages(list(session.get("messages", []) or []), rendered_user=rendered, runtime_feedback=runtime_feedback)
            _, messages = securitylib.sanitize_provider_payload([], messages, env=self.agent.redaction_env, secret_env_names=self.agent.secret_env_names)
            messages, dropped_messages = _drop_old_turns(messages, soft_cap, floor_count, msg_token)
        if used() > input_limit:
            messages, extra = _drop_old_turns(messages, input_limit - system_tokens - tools_tokens, 1, msg_token)
            dropped_messages += extra
        final_used = used()
        if final_used > input_limit:
            raise ContextBudgetExceeded(f"context_budget_exceeded: required request uses {final_used} tokens; input limit is {input_limit}")

        source_rows = []
        for source in sources:
            if source.name in dropped_sources:
                status, reason = "dropped_budget", "aggregate_budget"
            else:
                status, reason = source.status, source.reason_code
            source_rows.append({"name": source.name, "required": source.required, "budget_tokens": source.token_count, "actual_tokens": source.token_count if source.name in included else 0, "status": status, "reason": reason})
        digest_count = sum(bool((message.get("_pico_meta") or {}).get("digest_applied")) for message in messages)
        breakdown = {"schema_version": 1, "token_count_mode": mode, "budget": {"total": total, "reserved_output": reserved, "safety_margin": CONTEXT_SAFETY_MARGIN_TOKENS, "input_limit": input_limit, "used": final_used, "within_budget": True}, "sources": source_rows, "history": {"tokens_before": before_tokens, "tokens_after": sum(msg_token(message) for message in messages), "dropped_turns": dropped_messages}, "digest": {"applied_count": digest_count}}
        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []
        recall_errors = session.get("_recall_errors", {}) if isinstance(session, dict) else {}
        recall_errors = recall_errors if isinstance(recall_errors, dict) else {}
        provider_metadata = getattr(self.agent.model_client, "provider_metadata", {})
        provider_metadata = (
            dict(provider_metadata) if isinstance(provider_metadata, dict) else {}
        )
        metadata = {"system_prefix_hash": hashlib.sha256(system_text.encode()).hexdigest(), "system_tokens": system_tokens, "tools_tokens": tools_tokens, "prompt_cache_supported": bool(getattr(self.agent.model_client, "supports_prompt_cache", False)), **message_metrics(messages, token_of=token_of), "dropped_messages": dropped_messages, "cache_control_breakpoints": list(breakpoints), "runtime_feedback_present": bool(str(runtime_feedback or "").strip()), "recall.error_count": int(recall_errors.get("count", 0) or 0), "recall.last_error": str(recall_errors.get("last", "") or ""), "token_count_mode": mode, "context_breakdown": breakdown, "recall_commit_paths": [path for source in sources if source.name == "recalled_memory" and source.name in included for path in source.selected_memory_paths], **dict(injection_telemetry or {}), **dict(preflight_metadata or {}), **provider_metadata}
        return {"system": system, "tools": tools, "messages": messages, "cache_control_breakpoints": breakpoints}, metadata

    def build_request(self, *, injection_snapshot, injection_telemetry, preflight_metadata, runtime_feedback=""):
        counter = getattr(getattr(self.agent, "model_client", None), "count_tokens", None)
        if callable(counter):
            try:
                return self._build_with_counter(injection_snapshot=injection_snapshot, injection_telemetry=injection_telemetry, preflight_metadata=preflight_metadata, runtime_feedback=runtime_feedback, token_of=lambda text: int(counter(text)), mode="provider_text")
            except ContextBudgetExceeded:
                raise
            except Exception:
                pass
        return self._build_with_counter(injection_snapshot=injection_snapshot, injection_telemetry=injection_telemetry, preflight_metadata=preflight_metadata, runtime_feedback=runtime_feedback, token_of=lambda text: max(1, len(text) // 4), mode="estimate")

    def count_tokens(self, text):
        counter = getattr(getattr(self.agent, "model_client", None), "count_tokens", None)
        if callable(counter):
            try:
                return int(counter(text))
            except Exception:
                pass
        return max(1, len(text) // 4)
