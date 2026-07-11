"""Security and redaction helpers for runtime artifacts."""

from copy import deepcopy
import errno
import json
import os
import posixpath
import re
import secrets
import stat
from pathlib import Path

_OPEN_SUPPORTS_DIR_FD = os.open in getattr(os, "supports_dir_fd", ())

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


class SensitiveDataBlockedError(RuntimeError):
    """Provider-bound content still contains high-confidence secret material."""


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
        if (
            index == len(parts) - 1
            and component in _ALLOWED_ENV_TEMPLATE_BASENAMES
        ):
            continue
        if (
            component in _SENSITIVE_PATH_BASENAMES
            or component.startswith(".env.")
            or (
                component.startswith("service-account")
                and component.endswith(".json")
            )
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


def ensure_private_file(path, *, trusted_root=None, trusted_root_identity=None):
    path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return path


def read_private_text(
    path,
    *,
    encoding="utf-8",
    errors="strict",
    trusted_root=None,
    trusted_root_identity=None,
):
    path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r", encoding=encoding, errors=errors) as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_private_bytes(
    path,
    *,
    trusted_root=None,
    trusted_root_identity=None,
    max_bytes=None,
):
    path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        os.fchmod(descriptor, 0o600)
        chunks = []
        remaining = None if max_bytes is None else int(max_bytes) + 1
        while remaining is None or remaining > 0:
            size = 64 * 1024 if remaining is None else min(64 * 1024, remaining)
            chunk = os.read(descriptor, size)
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        data = b"".join(chunks)
        if max_bytes is not None and len(data) > int(max_bytes):
            raise ValueError("private file too large")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_regular_bytes_anchored(
    workspace_root, raw_path, *, max_bytes, expected_root_identity=None
):
    """Read one relative regular file once through an anchored bounded fd."""
    relative = Path(os.fspath(raw_path))
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("invalid relative path")
    descriptors = []
    leaf = -1
    try:
        descriptors.append(_open_private_directory(workspace_root))
        opened_root = os.fstat(descriptors[0])
        if expected_root_identity is not None and (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != tuple(expected_root_identity):
            raise ValueError("workspace root changed")
        directory_flags = _private_directory_flags()
        for component in relative.parts[:-1]:
            try:
                descriptors.append(
                    os.open(
                        component,
                        directory_flags,
                        dir_fd=descriptors[-1],
                    )
                )
            except FileNotFoundError:
                return {"exists": False, "data": None, "mode": None}
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            before = os.stat(
                relative.parts[-1],
                dir_fd=descriptors[-1],
                follow_symlinks=False,
            )
            leaf = os.open(
                relative.parts[-1],
                flags,
                dir_fd=descriptors[-1],
            )
        except FileNotFoundError:
            return {"exists": False, "data": None, "mode": None}
        opened = os.fstat(leaf)
        current = os.stat(
            relative.parts[-1],
            dir_fd=descriptors[-1],
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("path is not a stable regular file")
        chunks = []
        remaining = int(max_bytes) + 1
        while remaining:
            chunk = os.read(leaf, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return {
            "exists": True,
            "data": b"".join(chunks),
            "mode": stat.S_IMODE(opened.st_mode),
        }
    finally:
        if leaf >= 0:
            os.close(leaf)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _private_directory_flags():
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if (
        not _OPEN_SUPPORTS_DIR_FD
        or not directory_flag
        or not nofollow_flag
    ):
        raise RuntimeError("private file descriptor traversal unavailable")
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | directory_flag
        | nofollow_flag
    )


def _open_private_directory(path):
    path = _lexical_absolute(path)
    directory_flags = _private_directory_flags()
    descriptor = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:]:
            current = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("refusing symlink component")
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError("private file has unsafe parent")
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("refusing symlink component") from None
                raise
            try:
                if not stat.S_ISDIR(os.fstat(child_descriptor).st_mode):
                    raise ValueError("private file has unsafe parent")
            except Exception:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def private_directory_identity(path):
    descriptor = _open_private_directory(path)
    try:
        opened = os.fstat(descriptor)
        return opened.st_dev, opened.st_ino
    finally:
        os.close(descriptor)


def _open_private_parent(path, *, trusted_root=None, trusted_root_identity=None):
    path = _lexical_absolute(path)
    if trusted_root is None:
        return path, _open_private_directory(path.parent)
    root = _lexical_absolute(trusted_root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("private path escapes trusted root") from exc
    if not relative.parts:
        raise ValueError("private path must name a file")
    descriptor = _open_private_directory(root)
    try:
        opened = os.fstat(descriptor)
        if trusted_root_identity is None or (
            opened.st_dev,
            opened.st_ino,
        ) != tuple(trusted_root_identity):
            raise ValueError("private root changed")
        directory_flags = _private_directory_flags()
        for component in relative.parts[:-1]:
            current = os.stat(
                component,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(current.st_mode):
                raise ValueError("refusing symlink component")
            if not stat.S_ISDIR(current.st_mode):
                raise ValueError("private file has unsafe parent")
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("refusing symlink component") from None
                raise
            try:
                if not stat.S_ISDIR(os.fstat(child_descriptor).st_mode):
                    raise ValueError("private file has unsafe parent")
            except Exception:
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
        return path, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_file(path, *, trusted_root=None, trusted_root_identity=None):
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(current.st_mode):
            raise ValueError("refusing symlink component")
        if not stat.S_ISREG(current.st_mode):
            raise ValueError("path is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)

    try:
        opened = os.fstat(descriptor)
        path_current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (path_current.st_dev, path_current.st_ino)
        ):
            raise ValueError("private file changed or has multiple links")
    except Exception:
        os.close(descriptor)
        raise
    return path, descriptor


def _write_all(descriptor, data):
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("private write failed")
        view = view[written:]


def _private_entry_stat(parent_descriptor, name):
    return os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )


def _remove_owned_entry(parent_descriptor, name, identity):
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        os.unlink(name, dir_fd=parent_descriptor)


def write_private_bytes_atomic(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
    error="private temp changed",
    fsync_file=None,
    fsync_parent=None,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("private atomic write requires bytes")
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    temp_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    identity = None
    installed = False
    try:
        try:
            existing = _private_entry_stat(parent_descriptor, path.name)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise ValueError(error)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, bytes(data))
        (fsync_file or os.fsync)(descriptor)
        opened = os.fstat(descriptor)
        current = _private_entry_stat(parent_descriptor, temp_name)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity
            or current.st_nlink != 1
        ):
            raise ValueError(error)
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        installed = True
        current = _private_entry_stat(parent_descriptor, path.name)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
            or (current.st_dev, current.st_ino) != identity
            or opened.st_nlink != 1
        ):
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
            if not stat.S_ISREG(current.st_mode):
                os.unlink(path.name, dir_fd=parent_descriptor)
            else:
                _remove_owned_entry(parent_descriptor, path.name, identity)
            os.fsync(parent_descriptor)
            raise ValueError(error)
        (fsync_parent or os.fsync)(parent_descriptor)
        return path
    finally:
        if descriptor >= 0 and identity is not None and not installed:
            try:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        if identity is not None and not installed:
            _remove_owned_entry(parent_descriptor, temp_name, identity)
        os.close(parent_descriptor)


def append_private_bytes(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("private append requires bytes")
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    descriptor = -1
    original_size = None
    completed = False
    try:
        try:
            before = _private_entry_stat(parent_descriptor, path.name)
        except FileNotFoundError:
            before = None
        if before is not None:
            if stat.S_ISLNK(before.st_mode):
                raise ValueError("refusing symlink component")
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("private file has multiple links")
        flags = os.O_APPEND | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_CREAT | (os.O_EXCL if before is None else 0)
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        original_size = opened.st_size
        current = _private_entry_stat(parent_descriptor, path.name)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
            or before is not None
            and (before.st_dev, before.st_ino) != identity
        ):
            raise ValueError("private file changed")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, bytes(data))
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = _private_entry_stat(parent_descriptor, path.name)
        if (
            after.st_nlink != 1
            or (current.st_dev, current.st_ino) != identity
            or current.st_nlink != 1
        ):
            raise ValueError("private file changed")
        os.fsync(parent_descriptor)
        completed = True
        return path
    finally:
        if descriptor >= 0 and original_size is not None and not completed:
            try:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def harden_private_tree(path):
    """Repair modes below one application-owned tree without following links."""
    root = ensure_private_dir(path)
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                child = Path(entry.path)
                if stat.S_ISDIR(mode):
                    pending.append(ensure_private_dir(child))
                elif stat.S_ISREG(mode):
                    ensure_private_file(child)
                else:
                    raise ValueError("private tree has unsafe entry")
    return root


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
        looks_sensitive_env_name(key)
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
        return tuple(
            redact_artifact(
                item,
                key=key,
                env=env,
                secret_env_names=secret_env_names,
            )
            for item in value
        )
    if isinstance(value, str):
        return redact_text(value, env=env, secret_env_names=secret_env_names)
    return value


def sanitize_provider_payload(system, messages, env=None, secret_env_names=None):
    safe_system = redact_artifact(
        deepcopy(system),
        env=env,
        secret_env_names=secret_env_names,
    )
    safe_messages = redact_artifact(
        deepcopy(messages),
        env=env,
        secret_env_names=secret_env_names,
    )
    serialized = json.dumps(
        {"system": safe_system, "messages": safe_messages},
        sort_keys=True,
        ensure_ascii=False,
    )
    if contains_secret_material(
        serialized,
        env=env,
        secret_env_names=secret_env_names,
    ):
        raise SensitiveDataBlockedError("sensitive_data_blocked")
    return safe_system, safe_messages


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
