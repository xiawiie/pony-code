"""Validation for repository-local ``pony.toml`` settings."""

import math
import sys
import tomllib

from pony.security.private_files import private_directory_identity
from pony.security.workspace_files import read_regular_bytes_anchored


_PONY_TOML_WARNING = "warning: invalid pony.toml; using defaults"
MAX_PONY_TOML_BYTES = 1024 * 1024
_MISSING = object()
_REMOVED_CONTEXT_KEYS = (
    "history_soft_cap",
    "history_floor_messages",
    "injection_budget_ratio",
)


def _warn_invalid_pony_toml_field(path):
    print(
        f"warning: invalid pony.toml field {path}; using default",
        file=sys.stderr,
    )


def _table(parent, key, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return {}
    if not isinstance(value, dict):
        _warn_invalid_pony_toml_field(path)
        return {}
    return value


def _bounded_int(parent, key, default, minimum, maximum, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return default
    if type(value) is int and minimum <= value <= maximum:
        return value
    _warn_invalid_pony_toml_field(path)
    return default


def _bounded_bool(parent, key, default, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return default
    if type(value) is bool:
        return value
    _warn_invalid_pony_toml_field(path)
    return default


def _bounded_float(parent, key, default, minimum, maximum, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return default
    if (
        type(value) in {int, float}
        and math.isfinite(value)
        and minimum <= value <= maximum
    ):
        return float(value)
    _warn_invalid_pony_toml_field(path)
    return default


def _validated_model(model, context):
    context_explicit = "context_window" in model
    output_explicit = "output_limit" in model
    if context_explicit:
        context_window = _bounded_int(
            model,
            "context_window",
            128000,
            4096,
            2_000_000,
            "model.context_window",
        )
    elif "total_budget_hard_cap" in context:
        context_window = _bounded_int(
            context,
            "total_budget_hard_cap",
            128000,
            4096,
            2_000_000,
            "context.total_budget_hard_cap",
        )
        context_explicit = True
    else:
        context_window = 128000
    return {
        "_meta": {
            "model_context_explicit": context_explicit,
            "model_output_explicit": output_explicit,
        },
        "model": {
            "context_window": context_window,
            "output_limit": _bounded_int(
                model,
                "output_limit",
                16384,
                1,
                384000,
                "model.output_limit",
            ),
        },
    }


def _validated_context(context):
    compaction = _table(context, "compaction", "context.compaction")
    tool_results = _table(context, "tool_results", "context.tool_results")
    return {
        "system_tools_hard_cap": _bounded_int(
            context,
            "system_tools_hard_cap",
            24576,
            1,
            100000,
            "context.system_tools_hard_cap",
        ),
        "source_pool_tokens": _bounded_int(
            context,
            "source_pool_tokens",
            16384,
            1,
            200000,
            "context.source_pool_tokens",
        ),
        "compaction": {
            "enabled": _bounded_bool(
                compaction,
                "enabled",
                True,
                "context.compaction.enabled",
            ),
            "reserve_tokens": _bounded_int(
                compaction,
                "reserve_tokens",
                16384,
                1,
                1_000_000,
                "context.compaction.reserve_tokens",
            ),
            "keep_recent_tokens": _bounded_int(
                compaction,
                "keep_recent_tokens",
                20000,
                1,
                1_000_000,
                "context.compaction.keep_recent_tokens",
            ),
        },
        "tool_results": {
            "inline_tokens": _bounded_int(
                tool_results,
                "inline_tokens",
                4096,
                1,
                100000,
                "context.tool_results.inline_tokens",
            ),
            "digest_tokens": _bounded_int(
                tool_results,
                "digest_tokens",
                512,
                1,
                16384,
                "context.tool_results.digest_tokens",
            ),
        },
    }


def _validated_recall(recall):
    return {
        "min_score": _bounded_float(
            recall,
            "min_score",
            0.3,
            0,
            1,
            "memory.recall.min_score",
        ),
        "top_k": _bounded_int(
            recall,
            "top_k",
            6,
            1,
            20,
            "memory.recall.top_k",
        ),
        "max_tokens_per_note": _bounded_int(
            recall,
            "max_tokens_per_note",
            1024,
            1,
            4000,
            "memory.recall.max_tokens_per_note",
        ),
        "skip_recent_turns": _bounded_int(
            recall,
            "skip_recent_turns",
            2,
            0,
            100,
            "memory.recall.skip_recent_turns",
        ),
    }


def _validated_retrieval(retrieval):
    field_boost = _table(
        retrieval,
        "field_boost",
        "memory.retrieval.field_boost",
    )
    link = _table(retrieval, "link", "memory.retrieval.link")
    defaults = {
        "name": 5.0,
        "description": 3.0,
        "tags": 4.0,
        "aliases": 4.0,
        "body": 1.0,
    }
    return {
        "field_boost": {
            key: _bounded_float(
                field_boost,
                key,
                default,
                0,
                10,
                f"memory.retrieval.field_boost.{key}",
            )
            for key, default in defaults.items()
        },
        "link": {
            "max_added": _bounded_int(
                link,
                "max_added",
                3,
                0,
                20,
                "memory.retrieval.link.max_added",
            ),
            "decay": _bounded_float(
                link,
                "decay",
                0.4,
                0,
                1,
                "memory.retrieval.link.decay",
            ),
        },
    }


def _validated_pony_toml(raw):
    model = _table(raw, "model", "model")
    context = _table(raw, "context", "context")
    memory = _table(raw, "memory", "memory")
    validated = _validated_model(model, context)
    validated.update(
        context=_validated_context(context),
        memory={
            "recall": _validated_recall(_table(memory, "recall", "memory.recall")),
            "retrieval": _validated_retrieval(
                _table(memory, "retrieval", "memory.retrieval")
            ),
        },
    )
    return validated


def _warn_deprecated_pony_toml(raw):
    context = raw.get("context") if isinstance(raw, dict) else None
    if not isinstance(context, dict):
        return
    for key in _REMOVED_CONTEXT_KEYS:
        if key in context:
            replacement = (
                "source_pool_tokens"
                if key == "injection_budget_ratio"
                else "automatic compaction"
            )
            print(
                f"warning: [context].{key} was removed; use {replacement}",
                file=sys.stderr,
            )
    if "total_budget_hard_cap" in context:
        print(
            "warning: [context].total_budget_hard_cap is deprecated; "
            "migrating it to [model].context_window",
            file=sys.stderr,
        )
    if "digest" in context:
        print(
            "warning: [context.digest] was removed; use "
            "[context.tool_results] token limits",
            file=sys.stderr,
        )


def load_pony_toml(workspace_root, *, expected_root_identity=None):
    """Return one complete, validated snapshot of the project TOML config."""
    try:
        root_identity = (
            private_directory_identity(workspace_root)
            if expected_root_identity is None
            else expected_root_identity
        )
        state = read_regular_bytes_anchored(
            workspace_root,
            "pony.toml",
            max_bytes=MAX_PONY_TOML_BYTES,
            expected_root_identity=root_identity,
        )
        if not state["exists"]:
            return _validated_pony_toml({})
        raw = tomllib.loads(state["data"].decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError, ValueError):
        print(_PONY_TOML_WARNING, file=sys.stderr)
        return _validated_pony_toml({})
    if not isinstance(raw, dict):
        print(_PONY_TOML_WARNING, file=sys.stderr)
        return _validated_pony_toml({})
    _warn_deprecated_pony_toml(raw)
    return _validated_pony_toml(raw)
