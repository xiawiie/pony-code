"""Project-local configuration helpers."""

import ipaddress
import json
import math
import os
import re
import stat
import sys
import tomllib
import urllib.parse
from pathlib import Path

from .file_lock import locked_file
from .providers.defaults import (
    API_KEY_ENV_NAMES,
    BASE_URL_ENV_NAMES,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    DEFAULT_PROVIDER,
    MODEL_ENV_NAMES,
    OFFICIAL_PROVIDER_HOSTS,
    PROVIDER_CHOICES,
)
from .security import (
    ensure_private_dir,
    private_directory_identity,
    read_regular_bytes_anchored,
    read_private_text,
    write_private_bytes_atomic,
)


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_PROJECT_ENV_BYTES = 1024 * 1024
_SECRET_QUERY_KEYS = {
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "token",
    "secret",
    "password",
    "credential",
}
def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON-quoted .env value") from exc
        if not isinstance(decoded, str):
            raise ValueError("quoted .env value must be a string")
        return decoded
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _strip_inline_comment(value):
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        raise ValueError("missing '=' separator")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError("invalid .env variable name")
    value = _strip_quotes(_strip_inline_comment(value))
    if any(char in value for char in ("\0", "\r", "\n")):
        raise ValueError("invalid control character in .env value")
    return name, value


def project_env_path(workspace_root):
    return Path(workspace_root).resolve() / ".env"


def project_env_metadata(workspace_root, status):
    return {
        "path": str(project_env_path(workspace_root)),
        "scope": "repo_root_exact",
        "status": str(status),
    }


def _warn_invalid_env_line(env_path, line_number, error):
    print(f"warning: skipped invalid .env line {line_number}: {error}", file=sys.stderr)


def read_project_env_with_status(start, warn=True):
    env_path = project_env_path(start)
    try:
        initial_mode = env_path.lstat().st_mode
        text = read_private_text(env_path, max_bytes=MAX_PROJECT_ENV_BYTES)
    except FileNotFoundError:
        return {}, project_env_metadata(start, "missing")
    loaded = {}
    status = (
        "review_required"
        if os.name == "posix" and stat.S_IMODE(initial_mode) != 0o600
        else "loaded"
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            parsed = _parse_env_line(line)
        except ValueError as exc:
            status = "review_required"
            if warn:
                _warn_invalid_env_line(env_path, line_number, exc)
            continue
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
    return loaded, project_env_metadata(start, status)


def read_project_env(start, warn=True):
    loaded, _ = read_project_env_with_status(start, warn=warn)
    return loaded


def _validated_project_env_assignments(assignments):
    result = {}
    for raw_name, raw_value in dict(assignments or {}).items():
        name = str(raw_name)
        if not ENV_KEY_PATTERN.fullmatch(name):
            raise ValueError("invalid project environment variable name")
        value = "" if raw_value is None else str(raw_value)
        if any(char in value for char in ("\0", "\r", "\n")):
            raise ValueError(f"{name} cannot contain NUL or newlines")
        result[name] = value
    return result


def _format_project_env_assignment(name, value):
    return f"{name}={json.dumps(value, ensure_ascii=False)}"


def _render_project_env_update(existing_text, assignments):
    existing_lines = existing_text.splitlines()
    rendered = []
    seen = set()
    old_values = {}
    for line in existing_lines:
        try:
            parsed = _parse_env_line(line)
        except ValueError:
            rendered.append(line)
            continue
        if parsed is None:
            rendered.append(line)
            continue
        name, old_value = parsed
        if name not in assignments:
            rendered.append(line)
            continue
        old_values.setdefault(name, []).append(old_value)
        if name not in seen:
            rendered.append(_format_project_env_assignment(name, assignments[name]))
            seen.add(name)

    added = [name for name in assignments if name not in seen]
    if added and rendered and rendered[-1].strip():
        rendered.append("")
    rendered.extend(_format_project_env_assignment(name, assignments[name]) for name in added)
    updated = [
        name
        for name in assignments
        if name in seen and (len(old_values[name]) != 1 or old_values[name][0] != assignments[name])
    ]
    unchanged = [name for name in assignments if name in seen and name not in updated]
    text = "\n".join(rendered)
    return (text + "\n" if rendered else ""), {
        "updated": updated,
        "added": added,
        "unchanged": unchanged,
    }


def write_project_env_assignments(workspace_root, assignments):
    assignments = _validated_project_env_assignments(assignments)
    root = Path(workspace_root).resolve()
    root_identity = private_directory_identity(root)
    private_root = ensure_private_dir(root / ".pico")
    env_path = project_env_path(root)
    lock_path = private_root / "project-env.lock"

    with locked_file(lock_path, require_lock=True):
        try:
            existing_text = read_private_text(
                env_path,
                trusted_root=root,
                trusted_root_identity=root_identity,
                max_bytes=MAX_PROJECT_ENV_BYTES,
            )
        except FileNotFoundError:
            existing_text = ""
        content, result = _render_project_env_update(existing_text, assignments)
        rendered = content.encode("utf-8")
        if len(rendered) > MAX_PROJECT_ENV_BYTES:
            raise ValueError("private file too large")
        write_private_bytes_atomic(
            env_path,
            rendered,
            trusted_root=root,
            trusted_root_identity=root_identity,
            error="project env temp changed",
            max_existing_bytes=MAX_PROJECT_ENV_BYTES,
        )
    return result


def validate_provider_base_url(value):
    raw = str(value or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("provider_base_url_invalid")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("provider_base_url_invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider_base_url_credentials")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS for key, _ in query):
        raise ValueError("provider_base_url_credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("provider_base_url_query_or_fragment")
    return raw


def classify_provider_destination(provider, base_url, *, source):
    """Classify a validated endpoint without consulting a relay allowlist."""
    raw = validate_provider_base_url(base_url)
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").casefold().rstrip(".")
    loopback = host == "localhost" or host.endswith(".localhost")
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if loopback:
        classification = "local"
    elif host in OFFICIAL_PROVIDER_HOSTS.get(str(provider), ()):
        classification = "official"
    elif source in {"cli", "project_env", "environment"}:
        classification = "explicit_third_party"
    else:
        raise ValueError("provider_destination_implicit_third_party")
    return {
        "classification": classification,
        "host": host,
        "source": str(source),
    }


def _resolve_provider_value(
    explicit,
    explicit_name,
    env_names,
    project_env,
    process_env,
    default,
    default_name,
):
    if explicit:
        return {"value": explicit, "source": "cli", "name": explicit_name}
    for source_name, source in (
        ("project_env", project_env),
        ("environment", process_env),
    ):
        for name in env_names:
            value = source.get(name)
            if value:
                return {"value": value, "source": source_name, "name": name}
    if default:
        return {"value": default, "source": "default", "name": default_name}
    return {"value": "", "source": "unset", "name": ""}


def resolve_provider_config(*, explicit=None, project_env=None, process_env=None):
    """Resolve one provider configuration with shared value provenance."""
    explicit = dict(explicit or {})
    project_env = dict(project_env or {})
    process_env = dict(os.environ if process_env is None else process_env)
    provider = _resolve_provider_value(
        explicit.get("provider"),
        "--provider",
        ("PICO_PROVIDER",),
        project_env,
        process_env,
        DEFAULT_PROVIDER,
        "DEFAULT_PROVIDER",
    )
    provider_name = provider["value"]
    if provider_name not in PROVIDER_CHOICES:
        raise ValueError("unknown provider")

    model = _resolve_provider_value(
        explicit.get("model"),
        "--model",
        MODEL_ENV_NAMES.get(provider_name, ()),
        project_env,
        process_env,
        DEFAULT_MODELS[provider_name],
        f"DEFAULT_{provider_name.upper()}_MODEL",
    )
    explicit_base_url = explicit.get("base_url")
    explicit_base_name = "--base-url"
    if provider_name == "ollama" and not explicit_base_url:
        explicit_host = explicit.get("host")
        if explicit_host != DEFAULT_BASE_URLS["ollama"]:
            explicit_base_url = explicit_host
            explicit_base_name = "--host"
    base_url = _resolve_provider_value(
        explicit_base_url,
        explicit_base_name,
        BASE_URL_ENV_NAMES.get(provider_name, ()),
        project_env,
        process_env,
        DEFAULT_BASE_URLS[provider_name],
        (
            "DEFAULT_OLLAMA_HOST"
            if provider_name == "ollama"
            else f"DEFAULT_{provider_name.upper()}_BASE_URL"
        ),
    )
    base_url["value"] = validate_provider_base_url(base_url["value"])
    destination = classify_provider_destination(
        provider_name,
        base_url["value"],
        source=base_url["source"],
    )
    destination["name"] = base_url["name"]
    api_key = _resolve_provider_value(
        explicit.get("api_key"),
        "api_key",
        API_KEY_ENV_NAMES.get(provider_name, ()),
        project_env,
        process_env,
        "",
        "",
    )
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "destination": destination,
        "api_key": api_key,
    }


_PICO_TOML_WARNING = "warning: invalid pico.toml; using defaults"
MAX_PICO_TOML_BYTES = 1024 * 1024
_MISSING = object()
_REMOVED_CONTEXT_KEYS = (
    "history_soft_cap",
    "history_floor_messages",
    "injection_budget_ratio",
)


def _warn_invalid_pico_toml_field(path):
    print(
        f"warning: invalid pico.toml field {path}; using default",
        file=sys.stderr,
    )


def _table(parent, key, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return {}
    if not isinstance(value, dict):
        _warn_invalid_pico_toml_field(path)
        return {}
    return value


def _bounded_int(parent, key, default, minimum, maximum, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return default
    if type(value) is int and minimum <= value <= maximum:
        return value
    _warn_invalid_pico_toml_field(path)
    return default


def _bounded_bool(parent, key, default, path):
    value = parent.get(key, _MISSING)
    if value is _MISSING:
        return default
    if type(value) is bool:
        return value
    _warn_invalid_pico_toml_field(path)
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
    _warn_invalid_pico_toml_field(path)
    return default


def _validated_pico_toml(raw):
    model = _table(raw, "model", "model")
    policy = _table(raw, "policy", "policy")
    context = _table(raw, "context", "context")
    compaction = _table(context, "compaction", "context.compaction")
    tool_results = _table(context, "tool_results", "context.tool_results")
    memory = _table(raw, "memory", "memory")
    recall = _table(memory, "recall", "memory.recall")
    retrieval = _table(memory, "retrieval", "memory.retrieval")
    field_boost = _table(
        retrieval,
        "field_boost",
        "memory.retrieval.field_boost",
    )
    link = _table(retrieval, "link", "memory.retrieval.link")

    field_boost_defaults = {
        "name": 5.0,
        "description": 3.0,
        "tags": 4.0,
        "aliases": 4.0,
        "body": 1.0,
    }
    model_context_explicit = "context_window" in model
    model_output_explicit = "output_limit" in model
    if model_context_explicit:
        configured_context_window = _bounded_int(
            model, "context_window", 128000, 4096, 2_000_000, "model.context_window"
        )
    elif "total_budget_hard_cap" in context:
        configured_context_window = _bounded_int(
            context,
            "total_budget_hard_cap",
            128000,
            4096,
            2_000_000,
            "context.total_budget_hard_cap",
        )
        model_context_explicit = True
    else:
        configured_context_window = 128000
    return {
        "_meta": {
            "model_context_explicit": model_context_explicit,
            "model_output_explicit": model_output_explicit,
        },
        "model": {
            "context_window": configured_context_window,
            "output_limit": _bounded_int(
                model, "output_limit", 16384, 1, 384000, "model.output_limit"
            ),
        },
        "policy": {
            "max_blob_size": _bounded_int(
                policy,
                "max_blob_size",
                8 * 1024 * 1024,
                1,
                8 * 1024 * 1024,
                "policy.max_blob_size",
            ),
        },
        "context": {
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
                    compaction, "enabled", True, "context.compaction.enabled"
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
        },
        "memory": {
            "recall": {
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
            },
            "retrieval": {
                "field_boost": {
                    key: _bounded_float(
                        field_boost,
                        key,
                        default,
                        0,
                        10,
                        f"memory.retrieval.field_boost.{key}",
                    )
                    for key, default in field_boost_defaults.items()
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
            },
        },
    }


def _warn_deprecated_pico_toml(raw):
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
            "warning: [context.digest] was removed; use [context.tool_results] token limits",
            file=sys.stderr,
        )


def load_pico_toml(workspace_root, *, expected_root_identity=None):
    """Return one complete, validated snapshot of the project TOML config."""
    try:
        root_identity = (
            private_directory_identity(workspace_root)
            if expected_root_identity is None
            else expected_root_identity
        )
        state = read_regular_bytes_anchored(
            workspace_root,
            "pico.toml",
            max_bytes=MAX_PICO_TOML_BYTES,
            expected_root_identity=root_identity,
        )
        if not state["exists"]:
            return _validated_pico_toml({})
        raw = tomllib.loads(state["data"].decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError, ValueError):
        print(_PICO_TOML_WARNING, file=sys.stderr)
        return _validated_pico_toml({})
    if not isinstance(raw, dict):
        print(_PICO_TOML_WARNING, file=sys.stderr)
        return _validated_pico_toml({})
    _warn_deprecated_pico_toml(raw)
    return _validated_pico_toml(raw)
