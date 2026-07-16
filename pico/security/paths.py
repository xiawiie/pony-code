"""Sensitive-path classification and symlink-safe lexical checks."""

import os
from pathlib import Path
import posixpath
import stat


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

    for index, component in enumerate(parts):
        if index == len(parts) - 1 and component in _ALLOWED_ENV_TEMPLATE_BASENAMES:
            continue
        if (
            component in _SENSITIVE_PATH_BASENAMES
            or component.startswith(".env.")
            or (component.startswith("service-account") and component.endswith(".json"))
            or component.endswith(_SENSITIVE_KEYSTORE_SUFFIXES)
        ):
            return "sensitive_path"
    return ""


def is_sensitive_path(raw_path):
    return bool(sensitive_path_reason(raw_path))


def has_sensitive_path_suffix(raw_path):
    text = os.fsdecode(os.fspath(raw_path)).replace("\\", "/").casefold()
    leaf = text.rsplit("/", 1)[-1]
    return (
        any(text.endswith(name) for name in _SENSITIVE_PATH_BASENAMES)
        or text.endswith(_SENSITIVE_KEYSTORE_SUFFIXES)
        or ("service-account" in leaf and leaf.endswith(".json"))
    )


def is_allowed_env_template_leaf(raw_path):
    parts = _normalized_posix_parts(raw_path)
    return bool(parts and parts[-1] in _ALLOWED_ENV_TEMPLATE_BASENAMES)


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


def require_directory_no_symlink(path):
    path = _lstat_chain(path)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("path is not a directory")
    return path
