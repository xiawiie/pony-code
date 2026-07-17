"""Provider-neutral model limits and request token accounting."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import unicodedata
import warnings


DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
DEFAULT_RESERVE_TOKENS = 16_384
DEFAULT_KEEP_RECENT_TOKENS = 20_000
DEFAULT_SYSTEM_TOOLS_HARD_CAP = 24_576
DEFAULT_SOURCE_POOL_TOKENS = 16_384
MIN_EFFECTIVE_INPUT_TOKENS = 16_384
ESTIMATE_MARGIN = 1.05


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    token_counter_mode: str
    source: str


@dataclass(frozen=True)
class ModelBudget:
    capabilities: ModelCapabilities
    output_tokens: int
    reserve_tokens: int
    input_limit: int
    system_tools_hard_cap: int
    source_pool_tokens: int
    keep_recent_tokens: int

    @property
    def compaction_summary_tokens(self):
        return math.floor(self.reserve_tokens * 0.8)

    @property
    def split_turn_summary_tokens(self):
        return math.floor(self.reserve_tokens * 0.5)

    @property
    def branch_summary_tokens(self):
        return 2_048


@dataclass(frozen=True)
class TokenUsageAnchor:
    pinned_digest: str
    message_digests: tuple[str, ...]
    input_tokens: int


@dataclass(frozen=True)
class RequestTokenCount:
    total: int
    mode: str
    system: int
    tools: int
    messages: int
    anchor_candidate: tuple[str, tuple[str, ...]]


# Pony only claims builtin limits for its fixed public model. Internal benchmark
# clients and custom endpoints use explicit project limits or the conservative
# fallback.
BUILTIN_MODEL_CAPABILITIES = {
    "deepseek-v4-flash": (DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS),
}


def _positive_int(value, *, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def resolve_model_capabilities(
    model,
    *,
    model_config=None,
    context_window=None,
    max_output_tokens=None,
    warning_sink=None,
):
    """Resolve limits in CLI -> project config -> builtin -> fallback order."""
    model_name = str(model or "").strip()
    config = model_config if isinstance(model_config, dict) else {}
    builtin = BUILTIN_MODEL_CAPABILITIES.get(model_name.casefold())
    fallback = builtin or (DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS)
    config_context = config.get("context_window")
    config_output = config.get("output_limit")

    resolved_context = (
        context_window
        if context_window is not None
        else config_context if config_context is not None else fallback[0]
    )
    resolved_output = (
        max_output_tokens
        if max_output_tokens is not None
        else config_output if config_output is not None else fallback[1]
    )
    resolved_context = _positive_int(resolved_context, name="context_window")
    resolved_output = _positive_int(resolved_output, name="max_output_tokens")

    if context_window is not None or max_output_tokens is not None:
        source = "cli"
    elif config_context is not None or config_output is not None:
        source = "config"
    elif builtin is not None:
        source = "builtin"
    else:
        source = "fallback"
        if model_name:
            message = (
                f"warning: unknown model {model_name!r}; using conservative "
                f"fallback {DEFAULT_CONTEXT_WINDOW} context / "
                f"{DEFAULT_MAX_OUTPUT_TOKENS} output tokens. Set [model] limits or CLI overrides."
            )
            if warning_sink is not None:
                warning_sink(message)
            else:
                warnings.warn(message, RuntimeWarning, stacklevel=2)

    return ModelCapabilities(
        context_window=resolved_context,
        max_output_tokens=resolved_output,
        token_counter_mode="provider_usage_or_estimate",
        source=source,
    )


def build_model_budget(
    capabilities,
    *,
    output_limit=None,
    reserve_tokens=DEFAULT_RESERVE_TOKENS,
    keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS,
    system_tools_hard_cap=DEFAULT_SYSTEM_TOOLS_HARD_CAP,
    source_pool_tokens=DEFAULT_SOURCE_POOL_TOKENS,
):
    configured_output = (
        capabilities.max_output_tokens if output_limit is None else output_limit
    )
    configured_output = _positive_int(
        configured_output,
        name="output_limit",
    )
    reserve_tokens = _positive_int(reserve_tokens, name="reserve_tokens")
    keep_recent_tokens = _positive_int(
        keep_recent_tokens,
        name="keep_recent_tokens",
    )
    output_tokens = min(configured_output, capabilities.max_output_tokens)
    reserve_tokens = max(reserve_tokens, output_tokens)
    input_limit = capabilities.context_window - reserve_tokens
    if input_limit < MIN_EFFECTIVE_INPUT_TOKENS:
        raise ValueError(
            "model context window must leave at least 16384 input tokens after reserve"
        )
    scaled_system_cap = min(
        _positive_int(system_tools_hard_cap, name="system_tools_hard_cap"),
        math.floor(capabilities.context_window * 0.20),
    )
    configured_source_pool = _positive_int(
        source_pool_tokens,
        name="source_pool_tokens",
    )
    scaled_source_pool = (
        min(configured_source_pool, math.floor(capabilities.context_window * 0.125))
        if capabilities.context_window < DEFAULT_CONTEXT_WINDOW
        else configured_source_pool
    )
    return ModelBudget(
        capabilities=capabilities,
        output_tokens=output_tokens,
        reserve_tokens=reserve_tokens,
        input_limit=input_limit,
        system_tools_hard_cap=scaled_system_cap,
        source_pool_tokens=scaled_source_pool,
        keep_recent_tokens=keep_recent_tokens,
    )


def _is_cjk(char):
    if not char:
        return False
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def estimate_text_tokens(text):
    """Conservative stdlib estimate: CJK ~= 1 token, other text ~= 4 chars."""
    value = str(text or "")
    cjk = sum(1 for char in value if _is_cjk(char))
    other = len(value) - cjk
    # Combining marks and control-heavy protocol text tokenize less efficiently.
    structural = sum(
        1
        for char in value
        if unicodedata.category(char) in {"Cc", "Cf", "Mn"}
    )
    return max(1, cjk + math.ceil(other / 4) + math.ceil(structural / 4))


def _stable_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value):
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


class TokenAccounting:
    """One request counter shared by budgets, sources, summaries, and telemetry."""

    def __init__(self, provider_counter=None):
        self._provider_counter = provider_counter if callable(provider_counter) else None
        self.usage_anchor = None
        self._session_entry_tokens = {}

    def count_text(self, text):
        if self._provider_counter is not None:
            try:
                value = int(self._provider_counter(str(text or "")))
                if value >= 0:
                    return value
            except Exception:
                pass
        return estimate_text_tokens(text)

    def count_json(self, value):
        # JSON punctuation/field boundaries carry real model cost. Counting the
        # compact representation plus one token per object/list is conservative.
        structural = 0
        stack = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                structural += 1 + len(current)
                stack.extend(current.keys())
                stack.extend(current.values())
            elif isinstance(current, (list, tuple)):
                structural += 1
                stack.extend(current)
        return self.count_text(_stable_json(value)) + structural

    def count_message(self, message):
        return self.count_json(message) + 4

    def count_session_entry(self, entry_id, messages, *, fallback_estimate=None):
        """Cache immutable Session-entry costs for compaction planning."""
        key = str(entry_id)
        cached = self._session_entry_tokens.get(key)
        if cached is not None:
            return cached
        value = (
            int(fallback_estimate)
            if self._provider_counter is None
            and type(fallback_estimate) is int
            and fallback_estimate >= 0
            else sum(self.count_message(message) for message in messages)
        )
        self._session_entry_tokens[key] = value
        return value

    def count_request(self, *, system, tools, messages):
        system_tokens = self.count_json(system)
        tools_tokens = self.count_json(tools)
        message_tokens = [self.count_message(message) for message in messages]
        pinned_digest = _digest({"system": system, "tools": tools})
        message_digests = tuple(_digest(message) for message in messages)
        mode = "provider_text" if self._provider_counter is not None else "estimate"
        total = system_tokens + tools_tokens + sum(message_tokens)

        anchor = self.usage_anchor
        if (
            isinstance(anchor, TokenUsageAnchor)
            and anchor.pinned_digest == pinned_digest
            and len(message_digests) >= len(anchor.message_digests)
            and message_digests[: len(anchor.message_digests)] == anchor.message_digests
        ):
            tail = sum(message_tokens[len(anchor.message_digests) :])
            total = anchor.input_tokens + tail
            mode = "provider_usage_plus_estimate" if tail else "provider_usage"
        elif self._provider_counter is None:
            total = math.ceil(total * ESTIMATE_MARGIN)

        return RequestTokenCount(
            total=max(1, total),
            mode=mode,
            system=system_tokens,
            tools=tools_tokens,
            messages=sum(message_tokens),
            anchor_candidate=(pinned_digest, message_digests),
        )

    def commit_provider_usage(self, usage, anchor_candidate):
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = usage.get("input_tokens")
        if type(input_tokens) is not int or input_tokens < 0:
            return False
        pinned_digest, message_digests = anchor_candidate
        self.usage_anchor = TokenUsageAnchor(
            pinned_digest=pinned_digest,
            message_digests=tuple(message_digests),
            input_tokens=input_tokens,
        )
        return True
