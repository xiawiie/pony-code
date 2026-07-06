"""Project-local configuration helpers."""

import os
import re
import sys
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _strip_inline_comment(value):
    quote = ""
    for index, char in enumerate(value):
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
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(_strip_inline_comment(value))


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def _warn_invalid_env_line(env_path, line_number, error):
    print(f"warning: skipped invalid .env line {line_number} in {env_path}: {error}", file=sys.stderr)


def read_project_env(start, warn=True):
    env_path = find_project_env(start)
    if env_path is None:
        return {}
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
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


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
