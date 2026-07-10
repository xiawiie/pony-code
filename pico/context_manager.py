"""Build the provider request from Pico's canonical messages."""

from __future__ import annotations

import hashlib
import json

from pico.messages import build_request_messages, message_content_text, message_metrics


# Pinned system/tools input is never a truncation candidate.
SYSTEM_TOOLS_HARD_CAP = 20000


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
        "input_schema": {"type": "object", "properties": props, "required": required},
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

    def build_v2(
        self,
        *,
        injection_snapshot,
        injection_telemetry,
        preflight_metadata,
        runtime_feedback="",
    ):
        """Return the single request shape that will be sent to the provider."""
        system_text = str(getattr(self.agent, "prefix", "") or "")
        system = [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }]
        tools = _build_tools_list(getattr(self.agent, "tools", {}) or {})

        system_tokens = self._count_tokens_for_v2(system_text)
        tools_tokens = self._count_tokens_for_v2(json.dumps(tools, sort_keys=False))
        config = getattr(self.agent, "context_config", None)
        if not isinstance(config, dict):
            config = {}
        pinned_cap = int(config.get("system_tools_hard_cap", SYSTEM_TOOLS_HARD_CAP))
        if system_tokens + tools_tokens > pinned_cap:
            raise RuntimeError(
                f"SystemTooBig: system+tools tokens {system_tokens + tools_tokens} "
                f"exceed {pinned_cap}. Inspect workspace.stable_text() or tools schema."
            )

        session = getattr(self.agent, "session", {}) or {}
        messages = build_request_messages(
            list(session.get("messages", []) or []),
            rendered_user=injection_snapshot,
            runtime_feedback=runtime_feedback,
        )
        runtime_feedback_present = bool(str(runtime_feedback or "").strip())
        soft_cap = int(config.get("history_soft_cap", 40000))
        floor_count = int(config.get("history_floor_messages", 6))
        messages, dropped_messages = _drop_old_turns(
            messages,
            soft_cap_tokens=soft_cap,
            floor_count=floor_count,
            token_of=lambda message: self._count_tokens_for_v2(
                message_content_text(message)
            ),
        )
        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []

        recall_errors = session.get("_recall_errors", {}) if isinstance(session, dict) else {}
        if not isinstance(recall_errors, dict):
            recall_errors = {}
        metrics = message_metrics(messages, token_of=self._count_tokens_for_v2)
        metadata = {
            "system_cache_key": hashlib.sha256(system_text.encode("utf-8")).hexdigest(),
            "system_tokens": system_tokens,
            "tools_tokens": tools_tokens,
            "prompt_cache_supported": bool(
                getattr(self.agent.model_client, "supports_prompt_cache", False)
            ),
            **metrics,
            "dropped_messages": dropped_messages,
            "cache_control_breakpoints": list(breakpoints),
            "runtime_feedback_present": runtime_feedback_present,
            "recall.error_count": int(recall_errors.get("count", 0) or 0),
            "recall.last_error": str(recall_errors.get("last", "") or ""),
            **dict(injection_telemetry or {}),
            **dict(preflight_metadata or {}),
        }
        return {
            "system": system,
            "tools": tools,
            "messages": messages,
            "cache_control_breakpoints": breakpoints,
        }, metadata

    def _count_tokens_for_v2(self, text):
        counter = getattr(getattr(self.agent, "model_client", None), "count_tokens", None)
        if callable(counter):
            try:
                return int(counter(text))
            except Exception:
                pass
        return max(1, len(text) // 4)
