"""Project-local configuration helpers."""

import json
import os
import re
import stat
import sys
import tempfile
import urllib.parse
from pathlib import Path

from .file_lock import locked_file
from .providers.defaults import API_KEY_ENV_NAMES, BASE_URL_ENV_NAMES, MODEL_ENV_NAMES
from .security import (
    contains_secret_material,
    ensure_private_dir,
    ensure_private_file,
    require_regular_no_symlink,
)


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXECUTION_ENV_EXACT_DENY = {"PATH", "HOME", "SHELL", "PYTHONPATH", "BASH_ENV", "ENV"}
_PROJECT_ENV_ALLOWED = {
    "PICO_PROVIDER",
    "PICO_SECRET_ENV_NAMES",
    *(name for names in MODEL_ENV_NAMES.values() for name in names),
    *(name for names in BASE_URL_ENV_NAMES.values() for name in names),
    *(name for names in API_KEY_ENV_NAMES.values() for name in names),
}
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


def find_project_env(start):
    """Compatibility wrapper with exact-root semantics."""
    env_path = project_env_path(start)
    return env_path if env_path.exists() else None


def _warn_invalid_env_line(env_path, line_number, error):
    print(f"warning: skipped invalid .env line {line_number}: {error}", file=sys.stderr)


def read_project_env(start, warn=True):
    env_path = require_regular_no_symlink(project_env_path(start), allow_missing=True)
    if not env_path.exists():
        return {}
    env_path = ensure_private_file(env_path)
    loaded = {}
    for line_number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            parsed = _parse_env_line(line)
        except ValueError as exc:
            if warn:
                _warn_invalid_env_line(env_path, line_number, exc)
            continue
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
    return loaded


def load_project_env(start, override=True, warn=True):
    loaded = read_project_env(start, warn=warn)
    for name, value in loaded.items():
        if _may_import_project_env(name) and (override or name not in os.environ):
            os.environ[name] = value
    return loaded


def _may_import_project_env(name):
    upper = str(name).upper()
    if upper in _EXECUTION_ENV_EXACT_DENY or upper.startswith(("LD_", "DYLD_")):
        return False
    return upper.startswith("PICO_") or upper in _PROJECT_ENV_ALLOWED


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


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_created_temp(path, identity):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        path.unlink()


def write_project_env_assignments(workspace_root, assignments):
    assignments = _validated_project_env_assignments(assignments)
    root = Path(workspace_root).resolve()
    private_root = ensure_private_dir(root / ".pico")
    env_path = project_env_path(root)
    lock_path = private_root / "project-env.lock"

    with locked_file(lock_path, require_lock=True):
        checked_path = require_regular_no_symlink(env_path, allow_missing=True)
        existing_text = ""
        if checked_path.exists():
            existing_text = ensure_private_file(checked_path).read_text(encoding="utf-8")
        content, result = _render_project_env_update(existing_text, assignments)

        descriptor, temp_name = tempfile.mkstemp(prefix=".pico-env-", dir=root)
        temp_path = Path(temp_name)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                current = temp_path.lstat()
            except OSError:
                raise ValueError("project env temp changed") from None
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                raise ValueError("project env temp changed")
            temp_path.replace(env_path)
            env_path.chmod(0o600, follow_symlinks=False)
            _fsync_directory(root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            _remove_created_temp(temp_path, identity)
    return result


def validate_provider_base_url(value):
    raw = str(value or "")
    parsed = urllib.parse.urlsplit(raw)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider_base_url_credentials")
    if any(key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS for key, _ in query):
        raise ValueError("provider_base_url_credentials")
    if any(contains_secret_material(item, env={}) for _, item in query):
        raise ValueError("provider_base_url_credentials")
    return raw


def provider_env(name, legacy_names=(), default=""):
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default


def _parse_scalar(raw):
    text = raw.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def project_max_blob_size(workspace_root):
    """ADR-0034 承诺的 pico.toml 轻量 override：读取 `[policy] max_blob_size`。

    没写 pico.toml、section 缺失、值非法（负数或非整数）都回退到 recovery_policy
    里的默认值，让调用方可以无条件把返回值传给 snapshot_eligibility。
    """
    from .recovery_policy import DEFAULT_MAX_BLOB_SIZE

    data = load_pico_toml(workspace_root)
    raw = data.get("policy", {}).get("max_blob_size")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_MAX_BLOB_SIZE
    if raw <= 0:
        return DEFAULT_MAX_BLOB_SIZE
    return raw


def load_pico_toml(workspace_root):
    """极简的 pico.toml 解析器：只支持 `[section]` 头 + `key = scalar` 行。

    Phase 1 只需要 `policy.max_blob_size` 之类的标量覆写，等真正的复杂
    配置进来再切到 tomllib。
    """
    path = Path(workspace_root) / "pico.toml"
    if not path.exists():
        return {}
    data = {}
    current = data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                continue
            node = data
            for part in section.split("."):
                node = node.setdefault(part, {})
            current = node
            continue
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        current[name.strip()] = _parse_scalar(value)
    return data


def load_pico_toml_full(workspace_root):
    """Full-fidelity pico.toml parser.

    Prefers :mod:`tomllib` (stdlib since Python 3.11) so nested tables and
    typed values (arrays, floats, booleans) round-trip correctly. Falls
    back to :func:`load_pico_toml` for environments where tomllib is
    unavailable, or when the file is malformed enough that tomllib
    raises. Returns ``{}`` if the file doesn't exist.

    The function never raises: config errors surface as an empty dict
    plus a stderr warning, keeping the config surface strictly opt-in.
    """
    path = Path(workspace_root) / "pico.toml"
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        # Python 3.10 or earlier — should not reach here after B1 bump,
        # but be defensive so we never crash on config load.
        return load_pico_toml(workspace_root)
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"warning: pico.toml is malformed, using simple parser fallback ({exc})", file=sys.stderr)
        try:
            return load_pico_toml(workspace_root)
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Task B2-B6: pico.toml surface for the context/memory subsystems.
# Each helper is independent: missing file / missing section / bad type all
# fall back to the hard-coded default. The pattern mirrors
# ``project_max_blob_size`` above so future keys can be added without
# building a shared config object.
# ---------------------------------------------------------------------------

def _context_int(root, key, default):
    data = load_pico_toml_full(root)
    raw = data.get("context", {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    if raw <= 0:
        return default
    return raw


def _context_float(root, key, default):
    data = load_pico_toml_full(root)
    raw = data.get("context", {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    if raw < 0:
        return default
    return float(raw)


def context_history_soft_cap(root) -> int:
    """Max tokens allowed in messages array before older turns are dropped."""
    return _context_int(root, "history_soft_cap", 40000)


def context_history_floor_messages(root) -> int:
    """Minimum tail messages preserved regardless of budget."""
    return _context_int(root, "history_floor_messages", 6)


def context_injection_budget_ratio(root) -> float:
    """Fraction of total budget available for <system-reminder> injection."""
    return _context_float(root, "injection_budget_ratio", 0.15)


def context_system_tools_hard_cap(root) -> int:
    """Fail-loud threshold for system + tools token count."""
    return _context_int(root, "system_tools_hard_cap", 20000)


def context_total_budget_hard_cap(root) -> int:
    """Ceiling for the whole prompt used to derive the injection budget.

    The renderer computes ``injection_budget = ratio × total_budget_hard_cap``
    to cap ``<system-reminder>`` blocks. Exposing this via pico.toml keeps
    the config surface complete against ``renderer._compose_injection``,
    which already reads ``cfg.get("total_budget_hard_cap", 100000)``.
    """
    return _context_int(root, "total_budget_hard_cap", 100000)


def _context_digest_int(root, key, default):
    data = load_pico_toml_full(root)
    raw = data.get("context", {}).get("digest", {}).get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    if raw <= 0:
        return default
    return raw


def context_digest_size_threshold(root) -> int:
    """Threshold in characters above which a tool_result gets digested."""
    return _context_digest_int(root, "size_threshold_chars", 1200)


def memory_recall_config(root) -> dict:
    """Recall subsystem config: min_score, top_k, max_tokens_per_note, skip_recent_turns."""
    data = load_pico_toml_full(root)
    raw = data.get("memory", {}).get("recall", {}) or {}

    def _pick_float(key, default):
        v = raw.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else default

    def _pick_int(key, default):
        v = raw.get(key)
        return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else default

    return {
        "min_score": _pick_float("min_score", 0.3),
        "top_k": _pick_int("top_k", 2),
        "max_tokens_per_note": _pick_int("max_tokens_per_note", 400),
        "skip_recent_turns": _pick_int("skip_recent_turns", 2),
    }


def memory_field_boosts(root) -> dict:
    """BM25 field boost weights: {name, description, tags, aliases, body}.

    Reads ``[memory.retrieval.field_boost]`` from pico.toml. Each key is
    validated independently against a non-negative numeric type; missing
    or malformed entries fall back to the module-level defaults, so a
    partial override in pico.toml only affects the keys it names.
    """
    data = load_pico_toml_full(root)
    raw = data.get("memory", {}).get("retrieval", {}).get("field_boost", {}) or {}
    defaults = {"name": 5.0, "description": 3.0, "tags": 4.0, "aliases": 4.0, "body": 1.0}
    out = dict(defaults)
    for key in defaults:
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[key] = float(v)
    return out


def memory_link_config(root) -> tuple:
    """(max_added, decay) for [[name]] link expansion.

    Reads ``[memory.retrieval.link]`` from pico.toml. ``max_added`` must
    be a positive int; ``decay`` must be a float in ``[0, 1]``. Either
    invalid or missing falls back to the module-level defaults ``(3, 0.4)``.
    """
    data = load_pico_toml_full(root)
    raw = data.get("memory", {}).get("retrieval", {}).get("link", {}) or {}
    max_added_raw = raw.get("max_added")
    decay_raw = raw.get("decay")
    max_added = (
        max_added_raw
        if isinstance(max_added_raw, int) and not isinstance(max_added_raw, bool) and max_added_raw > 0
        else 3
    )
    decay = (
        float(decay_raw)
        if isinstance(decay_raw, (int, float)) and not isinstance(decay_raw, bool) and 0 <= decay_raw <= 1
        else 0.4
    )
    return (max_added, decay)
