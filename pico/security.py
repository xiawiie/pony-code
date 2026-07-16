"""Security and redaction helpers for runtime artifacts."""

from copy import deepcopy
import errno
import hashlib
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


class PrivateAtomicWriteError(RuntimeError):
    """An atomic write failed after its committed state became ambiguous."""

    committed = True


class WorkspaceIOError(ValueError):
    """Stable fail-closed error raised by anchored workspace I/O."""

    def __init__(self, code, detail=""):
        self.code = str(code)
        message = self.code if not detail else f"{self.code}: {detail}"
        super().__init__(message)


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
    max_bytes=None,
):
    return read_private_bytes(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
        max_bytes=max_bytes,
    ).decode(encoding, errors=errors)


def read_private_bytes(
    path,
    *,
    trusted_root=None,
    trusted_root_identity=None,
    max_bytes=None,
    harden=True,
):
    path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        opened = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else opened.st_uid
        if harden and stat.S_IMODE(opened.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        elif opened.st_uid != uid or stat.S_IMODE(opened.st_mode) != 0o600:
            raise ValueError("private file permissions are unsafe")
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
    parts = _workspace_relative_parts(raw_path)
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("invalid workspace file limit")
    parent = -1
    leaf = -1
    try:
        try:
            parent = _open_workspace_directory_anchored(
                workspace_root,
                parts[:-1],
                expected_root_identity=expected_root_identity,
            )
        except FileNotFoundError:
            return _missing_workspace_file()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            before = os.stat(
                parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
            _require_safe_workspace_file(before)
            leaf = os.open(
                parts[-1],
                flags,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return _missing_workspace_file()
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
                raise WorkspaceIOError(
                    "workspace_entry_unsafe",
                    "path is not a stable regular file",
                ) from None
            raise
        opened = os.fstat(leaf)
        _require_safe_workspace_file(opened)
        if opened.st_size > limit:
            raise _workspace_file_limit_error(opened)
        before_signature = _workspace_entry_signature(before)
        opened_signature = _workspace_entry_signature(opened)
        if opened_signature != before_signature:
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a stable regular file",
            )
        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_entry_unsafe",
        )
        chunks = []
        digest = hashlib.sha256()
        remaining = limit + 1
        while remaining:
            chunk = os.read(leaf, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise _workspace_file_limit_error(opened)
        try:
            current = os.stat(
                parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace file changed while it was read",
            ) from None
        after = os.fstat(leaf)
        if (
            _workspace_entry_signature(after) != opened_signature
            or _workspace_entry_signature(current) != opened_signature
        ):
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace file changed while it was read",
            )
        return {
            "exists": True,
            "data": data,
            "mode": stat.S_IMODE(opened.st_mode),
            "sha256": digest.hexdigest(),
            "identity": (opened.st_dev, opened.st_ino),
        }
    finally:
        if leaf >= 0:
            os.close(leaf)
        if parent >= 0:
            os.close(parent)


def list_directory_names_anchored(
    workspace_root,
    raw_path=".",
    *,
    max_entries,
    expected_root_identity=None,
):
    """List one directory without following or returning unsafe entries."""
    parts = _workspace_relative_parts(raw_path, allow_root=True)
    limit = int(max_entries)
    if limit < 1:
        raise ValueError("invalid workspace directory limit")
    descriptor = _open_workspace_directory_anchored(
        workspace_root,
        parts,
        expected_root_identity=expected_root_identity,
    )
    try:
        opened_identity = _workspace_inode_identity(os.fstat(descriptor))
        entries = []
        unsafe_count = 0
        scanned = 0
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > limit:
                    raise WorkspaceIOError(
                        "workspace_directory_limit_exceeded",
                        "workspace directory scan limit exceeded",
                    )
                try:
                    before = entry.stat(follow_symlinks=False)
                    current = os.stat(
                        entry.name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except (FileNotFoundError, OSError):
                    unsafe_count += 1
                    continue
                safe = (
                    _workspace_entry_signature(before)
                    == _workspace_entry_signature(current)
                    and (
                        stat.S_ISDIR(current.st_mode)
                        or (
                            stat.S_ISREG(current.st_mode)
                            and current.st_nlink == 1
                        )
                    )
                )
                if not safe:
                    unsafe_count += 1
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "mode": current.st_mode,
                        "size": current.st_size,
                    }
                )
        _require_current_workspace_directory(
            workspace_root,
            parts,
            opened_identity,
            expected_root_identity=expected_root_identity,
            error_code="workspace_entry_unsafe",
        )
        entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        return {
            "entries": tuple(entries),
            "unsafe_count": unsafe_count,
            "scanned": scanned,
        }
    finally:
        os.close(descriptor)


def write_regular_bytes_anchored_atomic(
    workspace_root,
    raw_path,
    data,
    *,
    max_bytes,
    expected_sha256=None,
    expected_root_identity=None,
    fsync_file=None,
    fsync_parent=None,
):
    """CAS-check and atomically replace one workspace regular file."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("workspace atomic write requires bytes")
    rendered = bytes(data)
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("invalid workspace file limit")
    if len(rendered) > limit:
        raise WorkspaceIOError(
            "workspace_file_limit_exceeded",
            "workspace file exceeds the configured limit",
        )
    if expected_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}",
        str(expected_sha256),
    ):
        raise ValueError("invalid expected workspace digest")

    parts = _workspace_relative_parts(raw_path)
    parent = _open_workspace_directory_anchored(
        workspace_root,
        parts[:-1],
        expected_root_identity=expected_root_identity,
        create=True,
    )
    sync_file = fsync_file or os.fsync
    sync_parent = fsync_parent or os.fsync
    existing_descriptor = -1
    temp_descriptor = -1
    temp_name = f".{parts[-1]}.{secrets.token_hex(12)}.tmp"
    temp_identity = None
    replaced = False
    try:
        existing_signature = None
        existing_digest = None
        try:
            existing = os.stat(
                parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            _require_safe_workspace_file(existing)
            if expected_sha256 is not None and existing.st_size > limit:
                raise _workspace_file_limit_error(existing)
            existing_descriptor = _open_workspace_regular_at(
                parent,
                parts[-1],
            )
            opened_existing = os.fstat(existing_descriptor)
            existing_signature = _workspace_entry_signature(opened_existing)
            if existing_signature != _workspace_entry_signature(existing):
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file identity changed before write",
                )
            _require_current_workspace_directory(
                workspace_root,
                parts[:-1],
                _workspace_inode_identity(os.fstat(parent)),
                expected_root_identity=expected_root_identity,
                error_code="workspace_changed_during_write",
            )
            existing_digest = None
            if expected_sha256 is not None:
                existing_digest = _workspace_descriptor_sha256(
                    existing_descriptor,
                    opened_existing.st_size,
                    "workspace_changed_during_write",
                )
            if (
                expected_sha256 is not None
                and existing_digest != expected_sha256
            ):
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file content changed before write",
                )
            target_mode = stat.S_IMODE(opened_existing.st_mode)
        else:
            if expected_sha256 is not None:
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file disappeared before write",
                )
            target_mode = 0o644

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        temp_descriptor = os.open(
            temp_name,
            flags,
            0o600,
            dir_fd=parent,
        )
        opened_temp = os.fstat(temp_descriptor)
        temp_identity = _workspace_inode_identity(opened_temp)
        os.fchmod(temp_descriptor, target_mode)
        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_changed_during_write",
        )
        _write_all(temp_descriptor, rendered)
        sync_file(temp_descriptor)
        opened_temp = os.fstat(temp_descriptor)
        current_temp = os.stat(
            temp_name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened_temp.st_mode)
            or opened_temp.st_nlink != 1
            or _workspace_inode_identity(opened_temp) != temp_identity
            or _workspace_inode_identity(current_temp) != temp_identity
            or stat.S_IMODE(opened_temp.st_mode) != target_mode
            or opened_temp.st_size != len(rendered)
        ):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace temporary file changed",
            )

        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_changed_during_write",
        )
        try:
            current = os.stat(
                parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        if existing_signature is None:
            if current is not None:
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file appeared during write",
                )
        else:
            if (
                current is None
                or _workspace_entry_signature(current) != existing_signature
            ):
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file identity changed during write",
                )
            if existing_digest is not None:
                current_digest = _workspace_descriptor_sha256(
                    existing_descriptor,
                    existing_signature[4],
                    "workspace_changed_during_write",
                )
                if current_digest != existing_digest:
                    raise WorkspaceIOError(
                        "workspace_changed_during_write",
                        "workspace file content changed during write",
                    )
            if (
                _workspace_entry_signature(os.fstat(existing_descriptor))
                != existing_signature
            ):
                raise WorkspaceIOError(
                    "workspace_changed_during_write",
                    "workspace file changed during write",
                )

        os.replace(
            temp_name,
            parts[-1],
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        replaced = True
        current = os.stat(
            parts[-1],
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or _workspace_inode_identity(current) != temp_identity
            or stat.S_IMODE(current.st_mode) != target_mode
        ):
            raise WorkspaceIOError(
                "workspace_changed_during_write",
                "workspace replace result changed",
            )
        sync_parent(parent)
        _require_current_workspace_directory(
            workspace_root,
            parts[:-1],
            _workspace_inode_identity(os.fstat(parent)),
            expected_root_identity=expected_root_identity,
            error_code="workspace_changed_during_write",
        )
        return {
            "mode": target_mode,
            "sha256": hashlib.sha256(rendered).hexdigest(),
            "created": existing_signature is None,
        }
    finally:
        if temp_descriptor >= 0:
            os.close(temp_descriptor)
        if existing_descriptor >= 0:
            os.close(existing_descriptor)
        if temp_identity is not None and not replaced:
            try:
                _remove_owned_entry(parent, temp_name, temp_identity)
            except OSError:
                pass
        os.close(parent)


def _missing_workspace_file():
    return {
        "exists": False,
        "data": None,
        "mode": None,
        "sha256": "",
        "identity": None,
    }


def _workspace_file_limit_error(value):
    error = WorkspaceIOError(
        "workspace_file_limit_exceeded",
        "workspace file exceeds the configured limit",
    )
    error.state = {
        "exists": True,
        "data": None,
        "mode": stat.S_IMODE(value.st_mode),
        "sha256": "",
        "identity": _workspace_inode_identity(value),
    }
    return error


def _workspace_relative_parts(raw_path, *, allow_root=False):
    raw = os.fsdecode(os.fspath(raw_path))
    if "\x00" in raw:
        raise ValueError("invalid relative path")
    relative = Path(raw)
    if relative.is_absolute():
        raise ValueError("invalid relative path")
    if allow_root and raw in {"", "."}:
        return ()
    if not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("invalid relative path")
    return tuple(relative.parts)


def _workspace_inode_identity(value):
    return value.st_dev, value.st_ino


def _workspace_entry_signature(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _require_safe_workspace_file(value):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "path is not a stable regular file",
        )


def _open_workspace_directory_anchored(
    workspace_root,
    parts,
    *,
    expected_root_identity=None,
    create=False,
):
    try:
        descriptor = _open_private_directory(workspace_root)
    except (OSError, ValueError) as exc:
        raise WorkspaceIOError(
            "workspace_entry_unsafe",
            "workspace root is unsafe",
        ) from exc
    try:
        opened_root = os.fstat(descriptor)
        if expected_root_identity is not None and (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != tuple(expected_root_identity):
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace root changed",
            )
        directory_flags = _private_directory_flags()
        for component in parts:
            try:
                child = os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise WorkspaceIOError(
                        "workspace_entry_unsafe",
                        "workspace parent could not be created safely",
                    ) from exc
                os.fsync(descriptor)
                try:
                    child = os.open(
                        component,
                        directory_flags,
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise WorkspaceIOError(
                        "workspace_entry_unsafe",
                        "workspace parent is unsafe",
                    ) from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise WorkspaceIOError(
                        "workspace_entry_unsafe",
                        "workspace parent is unsafe",
                    ) from None
                raise
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise WorkspaceIOError(
                    "workspace_entry_unsafe",
                    "workspace parent is not a directory",
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_current_workspace_directory(
    workspace_root,
    parts,
    expected_identity,
    *,
    expected_root_identity,
    error_code,
):
    current = -1
    try:
        try:
            current = _open_workspace_directory_anchored(
                workspace_root,
                parts,
                expected_root_identity=expected_root_identity,
            )
        except (FileNotFoundError, OSError, WorkspaceIOError) as exc:
            raise WorkspaceIOError(
                error_code,
                "workspace directory changed",
            ) from exc
        if _workspace_inode_identity(os.fstat(current)) != tuple(
            expected_identity
        ):
            raise WorkspaceIOError(
                error_code,
                "workspace directory changed",
            )
    finally:
        if current >= 0:
            os.close(current)


def _open_workspace_regular_at(parent_descriptor, name):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
            raise WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a stable regular file",
            ) from None
        raise
    try:
        _require_safe_workspace_file(os.fstat(descriptor))
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _workspace_descriptor_sha256(descriptor, size, error_code):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = int(size)
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise WorkspaceIOError(
                error_code,
                "workspace file changed while hashing",
            )
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise WorkspaceIOError(
            error_code,
            "workspace file changed while hashing",
        )
    return digest.hexdigest()


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


def private_file_signature(path, *, trusted_root=None, trusted_root_identity=None):
    """Return a no-follow identity/version signature for a private file."""
    _path, descriptor = _open_private_file(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    try:
        opened = os.fstat(descriptor)
        return (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
            stat.S_IMODE(opened.st_mode),
            opened.st_uid,
        )
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
        return True
    if (current.st_dev, current.st_ino) == identity:
        os.unlink(name, dir_fd=parent_descriptor)
        return True
    return False


def _private_entry_signature(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _descriptor_digest(descriptor, size, error):
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    remaining = int(size)
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            raise ValueError(error)
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError(error)
    return digest.digest()


def _validate_private_backup(
    parent_descriptor,
    name,
    descriptor,
    signature,
    digest,
    error,
):
    opened = os.fstat(descriptor)
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        raise ValueError(error) from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or _private_entry_signature(opened) != signature
        or _private_entry_signature(current) != signature
        or current.st_nlink != 1
    ):
        raise ValueError(error)
    if _descriptor_digest(descriptor, signature[2], error) != digest:
        raise ValueError(error)
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        raise ValueError(error) from None
    if (
        _private_entry_signature(os.fstat(descriptor)) != signature
        or _private_entry_signature(current) != signature
    ):
        raise ValueError(error)


def _validate_open_backup(descriptor, signature, digest, error):
    opened = os.fstat(descriptor)
    opened_signature = _private_entry_signature(opened)
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink not in {0, 1}
        or opened_signature[:4] != signature[:4]
        or _descriptor_digest(descriptor, signature[2], error) != digest
    ):
        raise ValueError(error)
    after = os.fstat(descriptor)
    if (
        _private_entry_signature(after) != opened_signature
        or after.st_nlink != opened.st_nlink
    ):
        raise ValueError(error)


def _restore_backup_from_descriptor(
    parent_descriptor,
    canonical_name,
    backup_descriptor,
    backup_signature,
    backup_digest,
    writer_identity,
    existing_signature,
    error,
    sync_file,
    sync_parent,
):
    _validate_open_backup(
        backup_descriptor,
        backup_signature,
        backup_digest,
        error,
    )
    restore_name = f".{canonical_name}.{secrets.token_hex(12)}.restore"
    restore_descriptor = -1
    restore_identity = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        restore_descriptor = os.open(
            restore_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(restore_descriptor)
        restore_identity = (opened.st_dev, opened.st_ino)
        os.fchmod(restore_descriptor, 0o600)
        os.lseek(backup_descriptor, 0, os.SEEK_SET)
        remaining = backup_signature[2]
        while remaining:
            chunk = os.read(backup_descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError(error)
            _write_all(restore_descriptor, chunk)
            remaining -= len(chunk)
        if os.read(backup_descriptor, 1):
            raise ValueError(error)
        sync_file(restore_descriptor)
        restored = os.fstat(restore_descriptor)
        current = _private_entry_stat(parent_descriptor, restore_name)
        if (
            not stat.S_ISREG(restored.st_mode)
            or restored.st_nlink != 1
            or stat.S_IMODE(restored.st_mode) != 0o600
            or (restored.st_dev, restored.st_ino) != restore_identity
            or (current.st_dev, current.st_ino) != restore_identity
            or _descriptor_digest(
                restore_descriptor,
                backup_signature[2],
                error,
            )
            != backup_digest
        ):
            raise ValueError(error)
        _validate_open_backup(
            backup_descriptor,
            backup_signature,
            backup_digest,
            error,
        )
        if _canonical_state(
            parent_descriptor,
            canonical_name,
            writer_identity,
            existing_signature,
        ) not in {"writer", "missing"}:
            raise ValueError(error)
        os.replace(
            restore_name,
            canonical_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        restored = _private_entry_stat(parent_descriptor, canonical_name)
        if (
            (restored.st_dev, restored.st_ino) != restore_identity
            or restored.st_nlink != 1
        ):
            raise ValueError(error)
        sync_parent(parent_descriptor)
    finally:
        if restore_descriptor >= 0:
            os.close(restore_descriptor)
        if restore_identity is not None:
            try:
                _remove_owned_entry(
                    parent_descriptor,
                    restore_name,
                    restore_identity,
                )
            except OSError:
                pass


def _canonical_state(parent_descriptor, name, identity, existing_signature):
    try:
        current = _private_entry_stat(parent_descriptor, name)
    except FileNotFoundError:
        return "missing"
    if (current.st_dev, current.st_ino) == identity:
        return "writer"
    if (
        existing_signature is not None
        and stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and _private_entry_signature(current) == existing_signature
    ):
        return "original"
    return "unknown"


def _require_current_private_parent(
    path,
    parent_descriptor,
    *,
    trusted_root,
    trusted_root_identity,
):
    current_descriptor = -1
    try:
        try:
            _, current_descriptor = _open_private_parent(
                path,
                trusted_root=trusted_root,
                trusted_root_identity=trusted_root_identity,
            )
        except FileNotFoundError as exc:
            raise ValueError("private root changed") from exc
        opened = os.fstat(parent_descriptor)
        current = os.fstat(current_descriptor)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("private root changed")
    finally:
        if current_descriptor >= 0:
            os.close(current_descriptor)


def write_private_bytes_atomic(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
    error="private temp changed",
    fsync_file=None,
    fsync_parent=None,
    max_existing_bytes=None,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("private atomic write requires bytes")
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    temp_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    backup_name = None
    descriptor = -1
    backup_descriptor = -1
    identity = None
    backup_identity = None
    backup_signature = None
    backup_digest = None
    backup_preserved = False
    preserve_new = False
    replace_started = False
    committed = False
    sync_file = fsync_file or os.fsync
    sync_parent = fsync_parent or os.fsync
    try:
        try:
            existing = _private_entry_stat(parent_descriptor, path.name)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise ValueError(error)
        if (
            existing is not None
            and max_existing_bytes is not None
            and existing.st_size > int(max_existing_bytes)
        ):
            raise ValueError("private file too large")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, bytes(data))
        sync_file(descriptor)
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

        _require_current_private_parent(
            path,
            parent_descriptor,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        existing_signature = (
            _private_entry_signature(existing) if existing is not None else None
        )
        if existing is not None:
            # Keep canonical at nlink=1 while retaining rollback bytes.
            source_descriptor = -1
            backup_name = f".{path.name}.{secrets.token_hex(12)}.bak"
            try:
                source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                source_flags |= getattr(os, "O_NOFOLLOW", 0)
                source_flags |= getattr(os, "O_NONBLOCK", 0)
                source_descriptor = os.open(
                    path.name,
                    source_flags,
                    dir_fd=parent_descriptor,
                )
                source_opened = os.fstat(source_descriptor)
                source_current = _private_entry_stat(
                    parent_descriptor,
                    path.name,
                )
                if (
                    not stat.S_ISREG(source_opened.st_mode)
                    or source_opened.st_nlink != 1
                    or _private_entry_signature(source_opened)
                    != existing_signature
                    or _private_entry_signature(source_current)
                    != existing_signature
                ):
                    raise ValueError(error)

                backup_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                backup_flags |= getattr(os, "O_CLOEXEC", 0)
                backup_flags |= getattr(os, "O_NOFOLLOW", 0)
                backup_descriptor = os.open(
                    backup_name,
                    backup_flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                backup_opened = os.fstat(backup_descriptor)
                backup_identity = (backup_opened.st_dev, backup_opened.st_ino)
                os.fchmod(backup_descriptor, 0o600)
                source_digest = hashlib.sha256()
                remaining = source_opened.st_size
                while remaining:
                    chunk = os.read(source_descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        raise ValueError(error)
                    source_digest.update(chunk)
                    _write_all(backup_descriptor, chunk)
                    remaining -= len(chunk)
                if os.read(source_descriptor, 1):
                    raise ValueError(error)
                sync_file(backup_descriptor)

                source_opened = os.fstat(source_descriptor)
                source_current = _private_entry_stat(
                    parent_descriptor,
                    path.name,
                )
                backup_opened = os.fstat(backup_descriptor)
                backup_current = _private_entry_stat(
                    parent_descriptor,
                    backup_name,
                )
                backup_signature = _private_entry_signature(backup_opened)
                backup_digest = source_digest.digest()
                if (
                    _private_entry_signature(source_opened)
                    != existing_signature
                    or _private_entry_signature(source_current)
                    != existing_signature
                    or not stat.S_ISREG(backup_opened.st_mode)
                    or backup_opened.st_nlink != 1
                    or stat.S_IMODE(backup_opened.st_mode) != 0o600
                    or backup_opened.st_size != source_opened.st_size
                    or (backup_opened.st_dev, backup_opened.st_ino)
                    != backup_identity
                    or (backup_current.st_dev, backup_current.st_ino)
                    != backup_identity
                    or backup_current.st_nlink != 1
                    or _private_entry_signature(backup_current)
                    != backup_signature
                ):
                    raise ValueError(error)
                _validate_private_backup(
                    parent_descriptor,
                    backup_name,
                    backup_descriptor,
                    backup_signature,
                    backup_digest,
                    error,
                )
            finally:
                if source_descriptor >= 0:
                    os.close(source_descriptor)
            sync_parent(parent_descriptor)

        _require_current_private_parent(
            path,
            parent_descriptor,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
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
        try:
            current = _private_entry_stat(parent_descriptor, path.name)
        except FileNotFoundError:
            current = None
        if (
            existing_signature is None
            and current is not None
            or existing_signature is not None
            and (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or _private_entry_signature(current) != existing_signature
            )
        ):
            raise ValueError(error)

        replace_started = True
        os.replace(
            temp_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        current = _private_entry_stat(parent_descriptor, path.name)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o600
            or (current.st_dev, current.st_ino) != identity
            or opened.st_nlink != 1
        ):
            raise ValueError(error)
        _require_current_private_parent(
            path,
            parent_descriptor,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        sync_parent(parent_descriptor)
        _require_current_private_parent(
            path,
            parent_descriptor,
            trusted_root=trusted_root,
            trusted_root_identity=trusted_root_identity,
        )
        # This is the commit point; backup cleanup must now fail closed.
        committed = True
        return path
    except BaseException as primary:
        if replace_started:
            try:
                state = _canonical_state(
                    parent_descriptor,
                    path.name,
                    identity,
                    existing_signature,
                )
                if state == "unknown":
                    backup_preserved = backup_name is not None
                    raise ValueError(error)

                if existing_signature is None:
                    if state == "writer":
                        if not _remove_owned_entry(
                            parent_descriptor,
                            path.name,
                            identity,
                        ):
                            raise ValueError(error)
                        sync_parent(parent_descriptor)
                elif state != "original":
                    try:
                        _validate_private_backup(
                            parent_descriptor,
                            backup_name,
                            backup_descriptor,
                            backup_signature,
                            backup_digest,
                            error,
                        )
                        state = _canonical_state(
                            parent_descriptor,
                            path.name,
                            identity,
                            existing_signature,
                        )
                        if state == "unknown":
                            raise ValueError(error)
                        if state != "original":
                            os.replace(
                                backup_name,
                                path.name,
                                src_dir_fd=parent_descriptor,
                                dst_dir_fd=parent_descriptor,
                            )
                            backup_name = None
                            restored = _private_entry_stat(
                                parent_descriptor,
                                path.name,
                            )
                            if (
                                not stat.S_ISREG(restored.st_mode)
                                or restored.st_nlink != 1
                                or stat.S_IMODE(restored.st_mode) != 0o600
                                or (restored.st_dev, restored.st_ino)
                                != backup_identity
                                or _descriptor_digest(
                                    backup_descriptor,
                                    backup_signature[2],
                                    error,
                                )
                                != backup_digest
                            ):
                                raise ValueError(error)
                            sync_parent(parent_descriptor)
                    except BaseException:
                        backup_preserved = backup_name is not None
                        preserve_new = backup_name is not None
                        raise

                if descriptor >= 0 and identity is not None:
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
            except BaseException as rollback_error:
                backup_preserved = backup_preserved or backup_name is not None
                raise rollback_error from primary
        raise
    finally:
        cleanup_error = None
        committed_error = None
        committed_cause = None
        if (
            descriptor >= 0
            and identity is not None
            and not committed
            and not preserve_new
        ):
            try:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
            except OSError:
                pass
        if identity is not None and not committed and not preserve_new:
            try:
                _remove_owned_entry(parent_descriptor, temp_name, identity)
            except OSError:
                pass
        if (
            backup_descriptor >= 0
            and backup_identity is not None
            and backup_name is not None
            and not backup_preserved
        ):
            if committed:
                backup_removed = False
                try:
                    current = _private_entry_stat(
                        parent_descriptor,
                        backup_name,
                    )
                    if (current.st_dev, current.st_ino) != backup_identity:
                        raise ValueError(error)
                    os.unlink(backup_name, dir_fd=parent_descriptor)
                    sync_parent(parent_descriptor)
                    backup_removed = True
                except BaseException as exc:
                    cleanup_error = exc
                if backup_removed:
                    try:
                        os.ftruncate(backup_descriptor, 0)
                        os.fsync(backup_descriptor)
                    except OSError:
                        pass
                else:
                    try:
                        _restore_backup_from_descriptor(
                            parent_descriptor,
                            path.name,
                            backup_descriptor,
                            backup_signature,
                            backup_digest,
                            identity,
                            existing_signature,
                            error,
                            sync_file,
                            sync_parent,
                        )
                        committed = False
                        try:
                            os.ftruncate(descriptor, 0)
                            os.fsync(descriptor)
                        except OSError:
                            pass
                    except BaseException as rollback_error:
                        committed_error = PrivateAtomicWriteError(error)
                        committed_cause = rollback_error
            else:
                try:
                    os.ftruncate(backup_descriptor, 0)
                    os.fsync(backup_descriptor)
                    if _remove_owned_entry(
                        parent_descriptor,
                        backup_name,
                        backup_identity,
                    ):
                        sync_parent(parent_descriptor)
                except (OSError, ValueError):
                    pass
        if descriptor >= 0:
            os.close(descriptor)
        if backup_descriptor >= 0:
            os.close(backup_descriptor)
        os.close(parent_descriptor)
        if committed_error is not None:
            raise committed_error from committed_cause
        if cleanup_error is not None:
            raise cleanup_error


def append_private_bytes(
    path,
    data,
    *,
    trusted_root,
    trusted_root_identity,
    max_total_bytes=None,
    expected_identity=None,
):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("private append requires bytes")
    if max_total_bytes is not None and len(data) > int(max_total_bytes):
        raise ValueError("private file too large")
    path, parent_descriptor = _open_private_parent(
        path,
        trusted_root=trusted_root,
        trusted_root_identity=trusted_root_identity,
    )
    descriptor = -1
    original_size = None
    write_started = False
    completed = False
    try:
        try:
            before = _private_entry_stat(parent_descriptor, path.name)
        except FileNotFoundError:
            before = None
        if expected_identity is not None and (
            before is None
            or (before.st_dev, before.st_ino) != tuple(expected_identity)
        ):
            raise ValueError("private file changed")
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
        if (
            max_total_bytes is not None
            and opened.st_size + len(data) > int(max_total_bytes)
        ):
            raise ValueError("private file too large")
        os.fchmod(descriptor, 0o600)
        write_started = True
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
        if (
            descriptor >= 0
            and original_size is not None
            and write_started
            and not completed
        ):
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

    def has_sensitive_leaf(value):
        if isinstance(value, dict):
            return any(
                contains_secret_material(
                    key,
                    env=env,
                    secret_env_names=secret_env_names,
                )
                or has_sensitive_leaf(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(has_sensitive_leaf(item) for item in value)
        return isinstance(value, str) and contains_secret_material(
            value,
            env=env,
            secret_env_names=secret_env_names,
        )

    # Check individual strings before JSON escaping can erase a token boundary
    # (for example ``"line\ngithub_pat_..."`` becomes ``"line\\ngithub..."``).
    if has_sensitive_leaf(safe_system) or has_sensitive_leaf(safe_messages):
        raise SensitiveDataBlockedError("sensitive_data_blocked")
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
