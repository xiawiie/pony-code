"""Project-local configuration helpers."""

import json
import ipaddress
import math
import os
import re
import stat
import sys
import tomllib
import urllib.parse
from pathlib import Path

from .file_lock import locked_file
from .security import (
    ensure_private_dir,
    private_directory_identity,
    read_private_text,
    write_private_bytes_atomic,
)


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_PROJECT_ENV_BYTES = 1024 * 1024
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_API_URL = "https://api.deepseek.com"
API_URL_ENV_NAME = "PICO_API_URL"
API_KEY_ENV_NAME = "PICO_DEEPSEEK_API_KEY"
PROTOCOL_FAMILY = "openai_chat_completions"
AUTH_MODE = "bearer"
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


def validate_api_url(value):
    raw = str(value or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("api_url_invalid")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("api_url_invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_url_credentials")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS for key, _ in query):
        raise ValueError("api_url_credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("api_url_query_or_fragment")
    if parsed.scheme.casefold() != "https" and not _loopback_api_url(raw):
        raise ValueError("insecure_api_url")
    return raw.rstrip("/")


def _resolve_env_value(name, project_env, process_env, default="", default_name=""):
    for source_name, source in (
        ("project_env", project_env),
        ("environment", process_env),
    ):
        value = source.get(name)
        if value:
            return {"value": value, "source": source_name, "name": name}
    if default:
        return {"value": default, "source": "default", "name": default_name}
    return {"value": "", "source": "unset", "name": ""}


def _loopback_api_url(value):
    parsed = urllib.parse.urlsplit(str(value))
    host = (parsed.hostname or "").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_model_config(*, project_env=None, process_env=None, required=True):
    """Resolve Pico's one fixed model endpoint without legacy fallbacks."""
    project_env = dict(project_env or {})
    process_env = dict(os.environ if process_env is None else process_env)
    api_url = _resolve_env_value(
        API_URL_ENV_NAME,
        project_env,
        process_env,
        DEFAULT_API_URL,
        "DEFAULT_API_URL",
    )
    api_url["value"] = validate_api_url(api_url["value"])
    api_key = _resolve_env_value(
        API_KEY_ENV_NAME,
        project_env,
        process_env,
    )
    if required and not api_key["value"]:
        raise ValueError("api_key_not_configured")
    return {
        "protocol": {
            "value": PROTOCOL_FAMILY,
            "source": "fixed",
            "name": "OpenAI Chat Completions",
        },
        "model": {
            "value": DEFAULT_MODEL,
            "source": "fixed",
            "name": "DEFAULT_MODEL",
        },
        "base_url": api_url,
        "auth_mode": {
            "value": AUTH_MODE,
            "source": "fixed",
            "name": "AUTH_MODE",
        },
        "api_key": api_key,
    }


_PICO_TOML_WARNING = "warning: invalid pico.toml; using defaults"


def _positive_int(value, default):
    return value if type(value) is int and value > 0 else default


def _nonnegative_float(value, default):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return default
    return float(value)


def _validated_pico_toml(raw):
    policy = raw.get("policy")
    policy = policy if isinstance(policy, dict) else {}
    context = raw.get("context")
    context = context if isinstance(context, dict) else {}
    digest = context.get("digest")
    digest = digest if isinstance(digest, dict) else {}
    memory = raw.get("memory")
    memory = memory if isinstance(memory, dict) else {}
    recall = memory.get("recall")
    recall = recall if isinstance(recall, dict) else {}
    retrieval = memory.get("retrieval")
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    field_boost = retrieval.get("field_boost")
    field_boost = field_boost if isinstance(field_boost, dict) else {}
    link = retrieval.get("link")
    link = link if isinstance(link, dict) else {}
    field_boost_defaults = {
        "name": 5.0,
        "description": 3.0,
        "tags": 4.0,
        "aliases": 4.0,
        "body": 1.0,
    }
    decay = _nonnegative_float(link.get("decay"), 0.4)
    if decay > 1:
        decay = 0.4
    return {
        "policy": {
            "max_blob_size": _positive_int(
                policy.get("max_blob_size"), 8 * 1024 * 1024
            ),
        },
        "context": {
            "history_soft_cap": _positive_int(
                context.get("history_soft_cap"), 40000
            ),
            "history_floor_messages": _positive_int(
                context.get("history_floor_messages"), 6
            ),
            "injection_budget_ratio": _nonnegative_float(
                context.get("injection_budget_ratio"), 0.15
            ),
            "system_tools_hard_cap": _positive_int(
                context.get("system_tools_hard_cap"), 20000
            ),
            "total_budget_hard_cap": _positive_int(
                context.get("total_budget_hard_cap"), 100000
            ),
            "digest": {
                "size_threshold_chars": _positive_int(
                    digest.get("size_threshold_chars"), 1200
                ),
            },
        },
        "memory": {
            "recall": {
                "min_score": _nonnegative_float(recall.get("min_score"), 0.3),
                "top_k": _positive_int(recall.get("top_k"), 2),
                "max_tokens_per_note": _positive_int(
                    recall.get("max_tokens_per_note"), 400
                ),
                "skip_recent_turns": _positive_int(
                    recall.get("skip_recent_turns"), 2
                ),
            },
            "retrieval": {
                "field_boost": {
                    key: _nonnegative_float(field_boost.get(key), default)
                    for key, default in field_boost_defaults.items()
                },
                "link": {
                    "max_added": _positive_int(link.get("max_added"), 3),
                    "decay": decay,
                },
            },
        },
    }


def load_pico_toml(workspace_root):
    """Return one complete, validated snapshot of the project TOML config."""
    path = Path(workspace_root) / "pico.toml"
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        return _validated_pico_toml({})
    except (tomllib.TOMLDecodeError, OSError):
        print(_PICO_TOML_WARNING, file=sys.stderr)
        return _validated_pico_toml({})
    if not isinstance(raw, dict):
        print(_PICO_TOML_WARNING, file=sys.stderr)
        return _validated_pico_toml({})
    return _validated_pico_toml(raw)
