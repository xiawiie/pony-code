"""Security and redaction helpers for runtime artifacts."""

import os
import posixpath
import re
import stat
from pathlib import Path

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
MIN_SECRET_SUBSTRING_REDACTION_LENGTH = 8
SECRET_SHAPED_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?key|auth[_ -]?token|bearer[_ -]?token|credential|secret|password|token)\b"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)
_PLACEHOLDER_VALUE_RE = re.compile(
    r"(?i)^(?:example|dummy|changeme|replace[-_ ]?me|your[-_ ]?(?:api[-_ ]?)?key|x{3,}|\$\{[^}]+\}|<[^>]+>)$"
)
_PLACEHOLDER_SPAN_RE = re.compile(r"(\$\{[^}]+\}|<[^<>=\s\"']+>)")
_QUOTED_OR_PLACEHOLDER_VALUE_PATTERN = (
    r'"(?:\\.|[^"\\])+"'
    r"|'(?:\\.|[^'\\])+'"
    r"|\$\{[^}]+\}"
)
_CONCRETE_TOKEN_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)(\b(?:api[_ -]?key|access[_ -]?(?:key|token)|auth[_ -]?token|client[_ -]?secret|credential|secret|password|token)\b[\"']?\s*[:=]\s*)({_QUOTED_OR_PLACEHOLDER_VALUE_PATTERN}|[^\"'\s,;}}]+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([^\s]+)")
_SECRET_FLAG_RE = re.compile(
    rf"(?i)(--(?:api[-_]?key|access[-_]?key|auth[-_]?token|credential|secret|password|token)(?:=|\s+))({_QUOTED_OR_PLACEHOLDER_VALUE_PATTERN}|[^\s]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://[^/@\s:]+:)([^/@\s]+)(?=@)")
_URL_SECRET_RE = re.compile(r"(?i)([?&](?:api[_-]?key|token|secret|password)=)([^&#\s]+)")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_MAPPING_KEYS = {
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "bearer_token",
    "credential",
    "credentials",
    "secret",
    "client_secret",
    "password",
    "token",
    "authorization",
    "private_key",
}
_SENSITIVE_PATH_BASENAMES = {
    ".env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials.json",
    "auth.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.toml",
}
_ALLOWED_ENV_TEMPLATE_BASENAMES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_KEYSTORE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


def _normalized_posix_parts(raw_path):
    raw = os.fsdecode(os.fspath(raw_path)).replace("\\", "/")
    return tuple(
        part.casefold()
        for part in posixpath.normpath(raw).split("/")
        if part not in {"", "."}
    )


def sensitive_path_reason(raw_path):
    parts = _normalized_posix_parts(raw_path)
    if not parts:
        return ""

    if ".ssh" in parts or ".gnupg" in parts:
        return "sensitive_path"

    for parent, child in zip(parts, parts[1:]):
        if (parent, child) in {
            (".aws", "credentials"),
            (".docker", "config.json"),
            (".kube", "config"),
        }:
            return "sensitive_path"
        if parent == ".pico" and child in {"sessions", "runs", "checkpoints"}:
            return "sensitive_path"

    basename = parts[-1]
    if basename in _ALLOWED_ENV_TEMPLATE_BASENAMES:
        return ""
    if basename in _SENSITIVE_PATH_BASENAMES or basename.startswith(".env."):
        return "sensitive_path"
    if basename.startswith("service-account") and basename.endswith(".json"):
        return "sensitive_path"
    if basename.endswith(_SENSITIVE_KEYSTORE_SUFFIXES):
        return "sensitive_path"
    return ""


def is_sensitive_path(raw_path):
    return bool(sensitive_path_reason(raw_path))


def _lexical_absolute(path):
    return Path(os.path.abspath(os.fspath(path)))


def _lstat_chain(path, *, allow_missing_leaf=False):
    target = _lexical_absolute(path)
    current = Path(target.anchor)
    parts = target.parts[1:]
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return target
            raise
        if stat.S_ISLNK(mode):
            raise ValueError("refusing symlink component")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError("parent component is not a directory")
    return target


def require_regular_no_symlink(path, *, allow_missing=False):
    path = _lstat_chain(path, allow_missing_leaf=allow_missing)
    if allow_missing and not path.exists():
        return path
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("path is not a regular file")
    return path


def ensure_private_dir(path):
    path = _lexical_absolute(path)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        created = False
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            mode = current.lstat().st_mode
            created = True
        if stat.S_ISLNK(mode):
            raise ValueError("private directory has symlink component")
        if not stat.S_ISDIR(mode):
            raise ValueError("private directory has unsafe component")
        if created:
            current.chmod(0o700, follow_symlinks=False)
    path.chmod(0o700, follow_symlinks=False)
    return path


def ensure_private_file(path):
    path = require_regular_no_symlink(path)
    path.chmod(0o600, follow_symlinks=False)
    return path


def _normalized_secret_names(secret_env_names):
    return {str(name).upper() for name in (secret_env_names or ())}


def looks_sensitive_env_name(name):
    upper = str(name).upper()
    return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)


def is_secret_env_name(name, secret_env_names=None):
    upper = str(name).upper()
    return upper in _normalized_secret_names(secret_env_names) or looks_sensitive_env_name(upper)


def looks_secret_shaped_text(text):
    text = str(text or "")
    return any(pattern.search(text) for pattern in SECRET_SHAPED_TEXT_PATTERNS)


def configured_secret_env_items(env=None, secret_env_names=None):
    env = os.environ if env is None else env
    configured_names = _normalized_secret_names(secret_env_names)
    items = [
        (name, value)
        for name, value in env.items()
        if str(name).upper() in configured_names and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def detected_secret_env_items(env=None, secret_env_names=None):
    env = os.environ if env is None else env
    items = [
        (name, value)
        for name, value in env.items()
        if is_secret_env_name(name, secret_env_names=secret_env_names) and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def secret_env_summary(env=None, secret_env_names=None):
    names = [name for name, _ in configured_secret_env_items(env=env, secret_env_names=secret_env_names)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def detected_secret_env_summary(env=None, secret_env_names=None):
    names = [name for name, _ in detected_secret_env_items(env=env, secret_env_names=secret_env_names)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def _is_secret_mapping_key(key):
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    normalized = re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")
    return normalized in _SECRET_MAPPING_KEYS or any(
        normalized.endswith("_" + item)
        for item in _SECRET_MAPPING_KEYS
    )


def _replace_secret_value(match):
    matched_value = match.group(2)
    quote = matched_value[0] if matched_value[-1:] == matched_value[:1] and matched_value[:1] in "\"'" else ""
    value = matched_value[1:-1] if quote else matched_value
    if _PLACEHOLDER_VALUE_RE.fullmatch(value):
        return match.group(0)
    return match.group(1) + quote + REDACTED_VALUE + quote


def _sub_concrete_token_outside_placeholders(pattern, text):
    return "".join(
        part if index % 2 else pattern.sub(REDACTED_VALUE, part)
        for index, part in enumerate(_PLACEHOLDER_SPAN_RE.split(text))
    )


def _replace_known_secret(text, secret):
    if REDACTED_VALUE in secret:
        return text.replace(secret, REDACTED_VALUE)
    return REDACTED_VALUE.join(
        part.replace(secret, REDACTED_VALUE)
        for part in text.split(REDACTED_VALUE)
    )


def redact_text(text, env=None, secret_env_names=None):
    text = str(text)
    for _, value in sorted(
        detected_secret_env_items(env=env, secret_env_names=secret_env_names),
        key=lambda item: len(item[1]),
        reverse=True,
    ):
        if len(value) >= MIN_SECRET_SUBSTRING_REDACTION_LENGTH:
            text = _replace_known_secret(text, value)
        elif text == value:
            text = REDACTED_VALUE
    text = _PRIVATE_KEY_RE.sub(REDACTED_VALUE, text)
    text = _AUTH_HEADER_RE.sub(_replace_secret_value, text)
    text = _SECRET_FLAG_RE.sub(_replace_secret_value, text)
    text = _URL_USERINFO_RE.sub(_replace_secret_value, text)
    text = _SECRET_ASSIGNMENT_RE.sub(_replace_secret_value, text)
    text = _URL_SECRET_RE.sub(_replace_secret_value, text)
    for pattern in _CONCRETE_TOKEN_RES:
        text = _sub_concrete_token_outside_placeholders(pattern, text)
    return text


def contains_secret_material(text, env=None, secret_env_names=None):
    original = str(text or "")
    return redact_text(original, env=env, secret_env_names=secret_env_names) != original


def redact_artifact(value, key=None, env=None, secret_env_names=None):
    if key and (
        is_secret_env_name(key, secret_env_names=secret_env_names)
        or _is_secret_mapping_key(key)
    ):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return {
            str(item_key): redact_artifact(item_value, key=item_key, env=env, secret_env_names=secret_env_names)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_artifact(item, key=key, env=env, secret_env_names=secret_env_names) for item in value]
    if isinstance(value, tuple):
        return [redact_artifact(item, key=key, env=env, secret_env_names=secret_env_names) for item in value]
    if isinstance(value, str):
        return redact_text(value, env=env, secret_env_names=secret_env_names)
    return value


def shell_env(env=None, allowlist=(), root="."):
    env = os.environ if env is None else env
    filtered = {
        name: env[name]
        for name in allowlist
        if name in env
    }
    filtered["PWD"] = str(root)
    if "PATH" not in filtered and env.get("PATH"):
        filtered["PATH"] = env["PATH"]
    return filtered
