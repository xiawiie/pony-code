"""Transactional installer for the bundled sandbox toolchain."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
import hashlib
import io
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import platform as platformlib
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlparse, urlunsplit
from urllib.request import urlopen

from .file_lock import locked_file
from .sandbox import SandboxIdentity, capture_file_identity
from .sandbox_lifecycle import acquire_bundle_lease, bundle_tree_hash, bundle_usage_state
from .security import ensure_private_dir

_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MIN_FREE_BYTES = 350 * 1024 * 1024
_DOWNLOAD_HOSTS = {"nodejs.org"}
_DOWNLOAD_TIMEOUT = 30
_MARKER = ".pico-toolchain.json"
_MIRROR_CONFIG_ENV = "PICO_SANDBOX_MIRROR_CONFIG"
_MIRROR_CONFIG_NAME = "sandbox-mirror.json"
_MAX_MIRROR_CONFIG_BYTES = 16 * 1024
_MAX_MARKER_BYTES = 16 * 1024 * 1024
_MAX_PACKAGE_METADATA_BYTES = 4 * 1024 * 1024
_MARKER_FIELDS = frozenset(
    {"format_version", "bundle_id", "tree", "package_lock_sha256", "srt_capability"}
)
_PACKAGE_FIELDS = frozenset({"name", "version", "private", "dependencies"})
_LOCK_FIELDS = frozenset(
    {"name", "version", "lockfileVersion", "requires", "packages"}
)
_LOCK_ROOT_FIELDS = frozenset({"name", "version", "dependencies"})
_LOCK_PACKAGE_REQUIRED_FIELDS = frozenset(
    {"version", "resolved", "integrity", "license"}
)
_LOCK_PACKAGE_FIELDS = _LOCK_PACKAGE_REQUIRED_FIELDS | {
    "dependencies",
    "bin",
    "engines",
    "funding",
}
_SRT_PACKAGE = "@anthropic-ai/sandbox-runtime"
_F0_REASON_CODES = {"no_approved_srt_candidate", "candidate_rejected"}
_PRODUCT_REASON_CODES = {"sandbox_not_released"}
_EXACT_VERSION_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class UnsupportedPlatform(RuntimeError):
    def __init__(self, message, *, code="unsupported_platform"):
        super().__init__(message)
        self.code = code


class ToolchainCorrupt(RuntimeError):
    def __init__(self, message, *, code="toolchain_corrupt"):
        super().__init__(message)
        self.code = code


class ToolchainNotReady(RuntimeError):
    def __init__(self, message, *, code="no_approved_srt_candidate"):
        super().__init__(message)
        self.code = code


def _object_from_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("JSON contains duplicate keys")
        result[key] = value
    return result


def _default_platform():
    machine = platformlib.machine().lower()
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return f"{platformlib.system().lower()}-{machine}"


def _load_manifest():
    payload = json.loads(
        resources.files("pico._sandbox_toolchain")
        .joinpath("manifest.json")
        .read_text(encoding="utf-8"),
        object_pairs_hook=_object_from_pairs,
    )
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema_version", "node", "srt", "retirements", "f0", "product"}
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("node"), dict)
        or set(payload["node"]) != {"version", "artifacts"}
        or not isinstance(payload.get("srt"), dict)
        or set(payload["srt"])
        != {"package", "version", "entrypoint", "integrity"}
        or not isinstance(payload.get("retirements"), dict)
        or not isinstance(payload.get("f0"), dict)
        or set(payload["f0"]) != {"status", "reason_code"}
        or not isinstance(payload.get("product"), dict)
        or set(payload["product"]) != {"status", "reason_code"}
    ):
        raise ValueError("sandbox manifest schema mismatch")
    node = payload["node"]
    srt = payload["srt"]
    f0 = payload["f0"]
    product = payload["product"]
    if (
        not isinstance(node.get("version"), str)
        or not node["version"]
        or not isinstance(node.get("artifacts"), dict)
        or set(node["artifacts"])
        != {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"}
        or any(not isinstance(srt.get(key), str) or not srt[key] for key in srt)
        or srt["package"] != _SRT_PACKAGE
        or f0.get("status") not in {"approved", "rejected"}
        or not isinstance(f0.get("reason_code"), str)
        or f0["status"] == "approved"
        and f0["reason_code"]
        or f0["status"] == "rejected"
        and f0["reason_code"] not in _F0_REASON_CODES
        or product.get("status") not in {"blocked", "enabled"}
        or not isinstance(product.get("reason_code"), str)
        or product["status"] == "enabled"
        and product["reason_code"]
        or product["status"] == "blocked"
        and product["reason_code"] not in _PRODUCT_REASON_CODES
    ):
        raise ValueError("sandbox manifest values are invalid")
    platforms = {}
    for platform_name, artifact in node["artifacts"].items():
        allowed = {
            "filename",
            "url",
            "sha256",
            "size",
            "offline_tree_sha256",
            "srt_capability",
        }
        parsed = urlparse(artifact.get("url", "")) if isinstance(artifact, dict) else None
        filename = artifact.get("filename") if isinstance(artifact, dict) else None
        digest = artifact.get("sha256") if isinstance(artifact, dict) else None
        if (
            not isinstance(artifact, dict)
            or not {"filename", "url", "sha256"} <= set(artifact) <= allowed
            or not isinstance(filename, str)
            or not filename.endswith(".tar.gz")
            or parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "nodejs.org"
            or PurePosixPath(parsed.path).name != filename
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or artifact.get("size") is not None
            and (type(artifact["size"]) is not int or artifact["size"] <= 0)
        ):
            raise ValueError("sandbox artifact manifest is invalid")
        entry = {
            "url": artifact["url"],
            "sha256": artifact["sha256"],
            "size": artifact.get("size"),
            "identity": (
                f"{platform_name}-node-{node['version']}-srt-{srt['version']}"
            ),
            "node_version": node["version"],
            "srt_version": srt["version"],
            "srt_integrity": srt["integrity"],
            "srt_entrypoint": (
                f"node_modules/{srt['package']}/{srt['entrypoint']}"
            ),
            "archive_root": artifact["filename"].removesuffix(".tar.gz"),
        }
        for key in ("offline_tree_sha256", "srt_capability"):
            if key in artifact:
                entry[key] = artifact[key]
        platforms[platform_name] = entry
    retirements = payload["retirements"]
    retirement_fields = {
        "platform",
        "arch",
        "tree_sha256",
        "security_reason",
        "replacement",
        "compatibility_evidence",
        "rollback_window",
        "release_note",
    }
    for identity, retirement in retirements.items():
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(retirement, dict)
            or set(retirement) != retirement_fields
            or any(
                not isinstance(retirement.get(key), str) or not retirement[key]
                for key in retirement_fields
            )
            or len(retirement["tree_sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in retirement["tree_sha256"])
        ):
            raise ValueError("sandbox retirement manifest is invalid")
        platform_name = f"{retirement['platform']}-{retirement['arch']}"
        replacement = platforms.get(platform_name, {}).get("identity")
        if retirement["replacement"] != replacement:
            raise ValueError("sandbox retirement replacement is invalid")
        try:
            rollback = retirement["rollback_window"]
            if not _RFC3339_RE.fullmatch(rollback):
                raise ValueError
            parsed = datetime.fromisoformat(
                rollback[:-1] + "+00:00" if rollback.endswith("Z") else rollback
            )
        except ValueError as exc:
            raise ValueError("sandbox retirement rollback window is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("sandbox retirement rollback window is invalid")
    return {
        "schema_version": payload["schema_version"],
        "platforms": platforms,
        "retirements": retirements,
        "f0": f0,
        "product": product,
    }


def _candidate_not_ready_reason(manifest):
    """Return the feasibility or product stop-gate reason."""
    f0 = manifest.get("f0")
    if f0 is None:
        # Test fixtures pass compact in-memory manifests; only packaged manifests
        # are production authority for a Sandbox candidate.
        return ""
    if not isinstance(f0, dict) or f0.get("status") != "approved":
        reason = f0.get("reason_code") if isinstance(f0, dict) else ""
        return reason if reason in _F0_REASON_CODES else "no_approved_srt_candidate"
    product = manifest.get("product")
    if not isinstance(product, dict) or product.get("status") != "enabled":
        reason = product.get("reason_code") if isinstance(product, dict) else ""
        return reason if reason in _PRODUCT_REASON_CODES else "sandbox_not_released"
    return ""


def _verify_binary_architecture(path, platform_id):
    system, _, architecture = str(platform_id).partition("-")
    if system not in {"darwin", "linux"}:
        return
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise ToolchainCorrupt(
            "managed Node architecture is unreadable", code="unsupported_architecture"
        ) from exc
    if system == "linux":
        expected = {"x64": 62, "arm64": 183}.get(architecture)
        valid = (
            expected is not None
            and len(header) >= 20
            and header[:6] == b"\x7fELF\x02\x01"
            and struct.unpack_from("<H", header, 18)[0] == expected
        )
    else:
        expected = {"x64": 0x01000007, "arm64": 0x0100000C}.get(architecture)
        valid = (
            expected is not None
            and len(header) >= 8
            and header[:4] == b"\xcf\xfa\xed\xfe"
            and struct.unpack_from("<I", header, 4)[0] == expected
        )
    if not valid:
        raise ToolchainCorrupt(
            "managed Node architecture mismatch", code="unsupported_architecture"
        )


def _download(url, *, allowed_hosts=None):
    allowed_hosts = _DOWNLOAD_HOSTS if allowed_hosts is None else set(allowed_hosts)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError("download URL is not allowlisted")
    with urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response:  # nosec: origin and redirects are checked.
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in allowed_hosts:
            raise ValueError("download redirect is not allowlisted")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > _MAX_ARCHIVE_BYTES:
            raise ValueError("archive exceeds size limit")
        chunks = []
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > _MAX_ARCHIVE_BYTES:
                raise ValueError("archive exceeds size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _read_json_object(path, expected):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) & 0o077
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise PermissionError("mirror config must be owner-only")
        chunks = []
        remaining = _MAX_MIRROR_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_MIRROR_CONFIG_BYTES:
        raise ValueError("mirror config exceeds size limit")

    value = json.loads(data.decode("utf-8"), object_pairs_hook=_object_from_pairs)
    if not isinstance(value, dict):
        raise ValueError("mirror config schema must be an object")
    return value


def _owner_only_config(path):
    """Read an explicitly selected operator file without following symlinks."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("mirror config path must be absolute")
    try:
        current = Path(path.anchor)
        for part in path.parts[1:-1]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("mirror config has unsafe parent")
        info = path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError("mirror config not found") from None
    parent = path.parent.lstat()
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("mirror config has unsafe parent")
    if stat.S_IMODE(parent.st_mode) & 0o022:
        raise PermissionError("mirror config parent must not be writable by others")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("mirror config must be a regular file")
    if info.st_uid != os.getuid() or info.st_nlink != 1:
        raise PermissionError("mirror config must be owner-only")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("mirror config must be owner-only")
    return _read_json_object(path, info)


def _mirror_url(value, *, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"mirror config {field} must be a URL")
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"mirror config {field} must be an HTTPS origin")
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/") + "/", "", ""))


def _validate_mirror_config(value, *, source):
    if not isinstance(value, dict) or set(value) != {"node_base_url", "npm_registry_url"}:
        raise ValueError("mirror config schema must contain node_base_url and npm_registry_url")
    return {
        "node_base_url": _mirror_url(value["node_base_url"], field="node_base_url"),
        "npm_registry_url": _mirror_url(value["npm_registry_url"], field="npm_registry_url"),
        "source": source,
    }


def load_operator_mirror_config(*, home=None, env=None):
    """Load only an explicitly operator-owned mirror config.

    Workspace files are never searched. Environment selection is explicit and
    still requires an absolute, owner-only, symlink-free file.
    """
    home = Path.home() if home is None else Path(home)
    env = os.environ if env is None else env
    selected = env.get(_MIRROR_CONFIG_ENV)
    if selected is not None:
        value = _owner_only_config(selected)
        return _validate_mirror_config(value, source="environment")
    default = home / ".pico" / _MIRROR_CONFIG_NAME
    try:
        default.lstat()
    except FileNotFoundError:
        return None
    value = _owner_only_config(default)
    return _validate_mirror_config(value, source="user_config")


class SandboxToolchain:
    def __init__(
        self,
        root,
        *,
        manifest=None,
        platform=None,
        downloader=None,
        runner=None,
        create_root=True,
        lock_timeout=120,
        mirror=None,
    ):
        self.root = Path(root)
        self.manifest = _load_manifest() if manifest is None else manifest
        self.platform = platform or _default_platform()
        self._entry = self.manifest.get("platforms", {}).get(self.platform)
        self._candidate_reason = _candidate_not_ready_reason(self.manifest)
        if not self._candidate_reason and self.root.exists():
            mode = self.root.stat().st_mode & 0o777
            if mode & 0o077:
                raise PermissionError("toolchain root must be owner-only")
        elif not self._candidate_reason and create_root:
            ensure_private_dir(self.root)
        if self._candidate_reason or mirror is None:
            self.mirror = None
        else:
            source = mirror.get("source", "explicit") if isinstance(mirror, dict) else "explicit"
            value = (
                {key: item for key, item in mirror.items() if key != "source"}
                if isinstance(mirror, dict)
                else mirror
            )
            self.mirror = _validate_mirror_config(value, source=source)
        if downloader is not None:
            self.downloader = downloader
        else:
            allowed_hosts = (
                {urlparse(self.mirror["node_base_url"]).hostname}
                if self.mirror is not None
                else set(_DOWNLOAD_HOSTS)
            )
            self.downloader = lambda url: _download(url, allowed_hosts=allowed_hosts)
        self.runner = runner or subprocess.run
        self._external_runner = runner is not None
        self.lock_timeout = float(lock_timeout)
        bundle_id = self._entry.get("identity", "unsupported") if self._entry else "unsupported"
        self.install_dir = self.root / "bundles" / bundle_id
        self.lock_path = self.root / "install.lock"

    def _artifact_url(self, entry):
        if self.mirror is None:
            return entry["url"]
        source = urlparse(entry["url"])
        base = urlparse(self.mirror["node_base_url"])
        return urlunsplit(
            (
                base.scheme,
                base.netloc,
                base.path.rstrip("/") + "/" + source.path.lstrip("/"),
                "",
                "",
            )
        )

    def inspect_bundle(self, bundle_path):
        """Return a redacted inventory item without executing bundle files."""
        path = Path(bundle_path)
        item = {"verified": False, "identity": "", "path": str(path)}
        try:
            marker = self._read_marker(path)
            identity = marker["bundle_id"]
            entry = self._entry if identity == (self._entry or {}).get("identity") else None
            retirement = self.manifest.get("retirements", {}).get(identity)
            if entry is None and isinstance(retirement, dict):
                entry = {
                    "identity": identity,
                    "tree_sha256": retirement.get("tree_sha256", ""),
                }
            if entry is None or marker["tree"] != self._tree(path):
                raise ToolchainCorrupt("inventory identity is not trusted")
            trusted_tree = entry.get("tree_sha256") or self._trusted_tree_hash(entry)
            if bundle_tree_hash(marker["tree"]) != trusted_tree:
                raise ToolchainCorrupt("inventory tree provenance mismatch")
            item["identity"] = identity
            info = path.lstat()
            item.update(
                verified=True,
                device=info.st_dev,
                inode=info.st_ino,
                platform=(retirement or {}).get("platform", self.platform.split("-", 1)[0]),
                arch=(retirement or {}).get("arch", self.platform.split("-", 1)[1]),
            )
            if retirement is not None:
                item["retirement"] = {
                    key: retirement[key]
                    for key in (
                        "security_reason",
                        "replacement",
                        "compatibility_evidence",
                        "rollback_window",
                        "release_note",
                    )
                }
            item.update(bundle_usage_state(path))
            return item
        except (OSError, ValueError, KeyError, TypeError, ToolchainCorrupt) as exc:
            item["reason"] = type(exc).__name__
            return item

    def inventory(self, *, include_quarantine=False):
        bundles = self.root / "bundles"
        result = []
        if bundles.is_dir() and not bundles.is_symlink():
            result.extend(self.inspect_bundle(path) for path in sorted(bundles.iterdir()))
        if include_quarantine:
            quarantine = self.root / "quarantine"
            if quarantine.is_dir() and not quarantine.is_symlink():
                for path in sorted(quarantine.iterdir()):
                    item = self.inspect_bundle(path)
                    item["location"] = "quarantine"
                    result.append(item)
        return result

    def _supported(self):
        if self._entry is None:
            system, separator, _ = self.platform.partition("-")
            code = (
                "unsupported_architecture"
                if separator
                and any(name.startswith(f"{system}-") for name in self.manifest.get("platforms", {}))
                else "unsupported_platform"
            )
            raise UnsupportedPlatform(
                f"unsupported platform: {self.platform}", code=code
            )
        return self._entry

    def _require_approved_candidate(self):
        if self._candidate_reason:
            raise ToolchainNotReady(
                "sandbox candidate is not approved",
                code=self._candidate_reason,
            )

    def _payload(self, status, reason_code):
        system, _, architecture = self.platform.partition("-")
        entry = self._entry or {}
        return {
            "record_type": "sandbox_toolchain_status",
            "format_version": 1,
            "status": status,
            "platform": system,
            "architecture": architecture,
            "bundle_id": entry.get("identity", ""),
            "node_version": entry.get("node_version", ""),
            "srt_version": entry.get("srt_version", ""),
            "reason_code": reason_code,
        }

    def status(self):
        if self._candidate_reason:
            return self._payload("not_ready", self._candidate_reason)
        if self._entry is None:
            try:
                self._supported()
            except UnsupportedPlatform as exc:
                return self._payload("unsupported", exc.code)
        if not self.install_dir.exists():
            return self._payload("absent", "toolchain_absent")
        try:
            return self.validate()
        except ToolchainCorrupt as exc:
            return self._payload("corrupt", exc.code)

    def validate(self):
        self._require_approved_candidate()
        self._supported()
        kwargs = {}
        if self._entry and self._entry.get("node_version"):
            _, trusted_tree_sha256, capability = self._offline_requirements()
            kwargs = {
                "trusted_tree_sha256": trusted_tree_sha256,
                "expected_capability": capability,
            }
        self._validate_directory(self.install_dir, **kwargs)
        return self._payload("ready", "ready")

    @staticmethod
    def _regular_file(path, label):
        try:
            info = path.lstat()
        except OSError as exc:
            raise ToolchainCorrupt(f"{label} missing") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ToolchainCorrupt(f"{label} is unsafe")
        return info

    @classmethod
    def _open_directory_descriptor(cls, root, parts=()):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(root, flags)
        except OSError as exc:
            raise ToolchainCorrupt("toolchain directory is unsafe") from exc
        try:
            for part in parts:
                replacement = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = replacement
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ToolchainCorrupt("toolchain directory is unsafe")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def _read_regular_bytes(
        cls,
        directory,
        relative,
        *,
        limit,
        label,
        private=False,
        code="toolchain_integrity_failed",
    ):
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ToolchainCorrupt(f"{label} is unsafe", code=code)
        try:
            parent = cls._open_directory_descriptor(directory, path.parts[:-1])
        except ToolchainCorrupt as exc:
            raise ToolchainCorrupt(f"{label} missing or unsafe", code=code) from exc
        descriptor = None
        try:
            name = path.parts[-1]
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(name, flags, dir_fd=parent)
            opened = os.fstat(descriptor)
            after_open = os.stat(name, dir_fd=parent, follow_symlinks=False)
            def identity(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_size > limit
                or private and stat.S_IMODE(opened.st_mode) & 0o077
                or identity(before) != identity(opened)
                or identity(after_open) != identity(opened)
            ):
                raise ToolchainCorrupt(f"{label} changed or is unsafe", code=code)
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after_read = os.fstat(descriptor)
            if (
                len(data) > limit
                or len(data) != opened.st_size
                or identity(after_read) != identity(opened)
            ):
                raise ToolchainCorrupt(f"{label} changed or is unsafe", code=code)
            return data
        except ToolchainCorrupt:
            raise
        except OSError as exc:
            raise ToolchainCorrupt(f"{label} missing or unsafe", code=code) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    @classmethod
    def _read_json(cls, directory, relative, *, limit, label, private=False, code):
        try:
            data = cls._read_regular_bytes(
                directory,
                relative,
                limit=limit,
                label=label,
                private=private,
                code=code,
            )
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_object_from_pairs)
        except ToolchainCorrupt:
            raise
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ToolchainCorrupt(f"{label} is invalid", code=code) from exc
        if not isinstance(value, dict):
            raise ToolchainCorrupt(f"{label} schema mismatch", code=code)
        return value

    @classmethod
    def _read_marker(cls, directory):
        marker = cls._read_json(
            directory,
            _MARKER,
            limit=_MAX_MARKER_BYTES,
            label="identity marker",
            private=True,
            code="toolchain_corrupt",
        )
        if (
            set(marker) != _MARKER_FIELDS
            or type(marker.get("format_version")) is not int
            or marker["format_version"] != 1
            or not isinstance(marker.get("tree"), dict)
        ):
            raise ToolchainCorrupt("identity schema mismatch")
        return marker

    @staticmethod
    def _bundled_package_bytes(name):
        return resources.files("pico._sandbox_toolchain").joinpath(name).read_bytes()

    @classmethod
    def _bundled_lock_hash(cls):
        return hashlib.sha256(cls._bundled_package_bytes("package-lock.json")).hexdigest()

    @staticmethod
    def _package_name(package_path):
        suffix = package_path.rsplit("node_modules/", 1)[-1]
        parts = suffix.split("/")
        if parts[0].startswith("@"):
            return "/".join(parts[:2]) if len(parts) == 2 else ""
        return parts[0] if len(parts) == 1 else ""

    @staticmethod
    def _dependency_path(packages, package_path, dependency):
        base = package_path
        while base:
            candidate = f"{base}/node_modules/{dependency}"
            if candidate in packages:
                return candidate
            if "/node_modules/" not in base:
                break
            base = base.rsplit("/node_modules/", 1)[0]
        candidate = f"node_modules/{dependency}"
        return candidate if candidate in packages else ""

    @staticmethod
    def _valid_integrity(value):
        if not isinstance(value, str) or not value.startswith("sha512-"):
            return False
        try:
            return len(base64.b64decode(value[7:], validate=True)) == 64
        except (ValueError, binascii.Error):
            return False

    @classmethod
    def _validate_bundled_package_metadata(cls, entry):
        code = "toolchain_integrity_failed"
        try:
            package_bytes = cls._bundled_package_bytes("package.json")
            lock_bytes = cls._bundled_package_bytes("package-lock.json")
            if (
                len(package_bytes) > _MAX_PACKAGE_METADATA_BYTES
                or len(lock_bytes) > _MAX_PACKAGE_METADATA_BYTES
            ):
                raise ValueError("package metadata exceeds size limit")
            package = json.loads(
                package_bytes.decode("utf-8"),
                object_pairs_hook=_object_from_pairs,
            )
            lock = json.loads(
                lock_bytes.decode("utf-8"),
                object_pairs_hook=_object_from_pairs,
            )
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ToolchainCorrupt("bundled package metadata is invalid", code=code) from exc
        dependencies = {_SRT_PACKAGE: entry.get("srt_version")}
        if (
            not isinstance(package, dict)
            or set(package) != _PACKAGE_FIELDS
            or package.get("name") != "pico-sandbox-toolchain"
            or package.get("version") != "0.0.0"
            or package.get("private") is not True
            or package.get("dependencies") != dependencies
            or not isinstance(lock, dict)
            or set(lock) != _LOCK_FIELDS
            or lock.get("name") != package["name"]
            or lock.get("version") != package["version"]
            or type(lock.get("lockfileVersion")) is not int
            or lock["lockfileVersion"] != 3
            or lock.get("requires") is not True
            or not isinstance(lock.get("packages"), dict)
        ):
            raise ToolchainCorrupt("bundled package metadata schema mismatch", code=code)
        packages = lock["packages"]
        root = packages.get("")
        if (
            not isinstance(root, dict)
            or set(root) != _LOCK_ROOT_FIELDS
            or root.get("name") != package["name"]
            or root.get("version") != package["version"]
            or root.get("dependencies") != dependencies
        ):
            raise ToolchainCorrupt("bundled package lock root mismatch", code=code)
        for package_path, metadata in packages.items():
            if package_path == "":
                continue
            parsed_path = PurePosixPath(package_path)
            parsed_url = (
                urlparse(metadata.get("resolved", ""))
                if isinstance(metadata, dict)
                else None
            )
            package_dependencies = metadata.get("dependencies", {}) if isinstance(metadata, dict) else None
            if (
                not isinstance(package_path, str)
                or parsed_path.is_absolute()
                or ".." in parsed_path.parts
                or not package_path.startswith("node_modules/")
                or not cls._package_name(package_path)
                or not isinstance(metadata, dict)
                or not _LOCK_PACKAGE_REQUIRED_FIELDS <= set(metadata) <= _LOCK_PACKAGE_FIELDS
                or not isinstance(metadata.get("version"), str)
                or not _EXACT_VERSION_RE.fullmatch(metadata["version"])
                or parsed_url is None
                or parsed_url.scheme != "https"
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
                or not cls._valid_integrity(metadata.get("integrity"))
                or not isinstance(metadata.get("license"), str)
                or not metadata["license"].strip()
                or not isinstance(package_dependencies, dict)
                or any(
                    not isinstance(name, str)
                    or not name
                    or not isinstance(version, str)
                    or not version
                    for name, version in package_dependencies.items()
                )
                or any(
                    key in metadata
                    and (
                        not isinstance(metadata[key], dict)
                        or any(
                            not isinstance(name, str)
                            or not name
                            or not isinstance(value, str)
                            or not value
                            for name, value in metadata[key].items()
                        )
                    )
                    for key in ("bin", "engines")
                )
                or "funding" in metadata
                and (
                    not isinstance(metadata["funding"], dict)
                    or set(metadata["funding"]) != {"url"}
                    or not isinstance(metadata["funding"]["url"], str)
                    or urlparse(metadata["funding"]["url"]).scheme != "https"
                )
            ):
                raise ToolchainCorrupt("bundled package lock entry is invalid", code=code)
        srt_path = f"node_modules/{_SRT_PACKAGE}"
        srt = packages.get(srt_path, {})
        if (
            srt.get("version") != entry.get("srt_version")
            or not isinstance(entry.get("srt_integrity"), str)
            or srt.get("integrity") != entry["srt_integrity"]
        ):
            raise ToolchainCorrupt("bundled SRT pin mismatch", code=code)
        reachable = {""}
        pending = [""]
        while pending:
            package_path = pending.pop()
            metadata = packages[package_path]
            for dependency in metadata.get("dependencies", {}):
                resolved = cls._dependency_path(packages, package_path, dependency)
                if not resolved:
                    raise ToolchainCorrupt("bundled package lock is incomplete", code=code)
                if resolved not in reachable:
                    reachable.add(resolved)
                    pending.append(resolved)
        if reachable != set(packages):
            raise ToolchainCorrupt("bundled package lock contains unreachable packages", code=code)
        return package, lock

    @staticmethod
    def _trusted_tree_hash(entry):
        value = entry.get("offline_tree_sha256")
        if value is not None:
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ToolchainCorrupt("offline tree provenance is invalid")
            return value
        tree = entry.get("tree")
        if isinstance(tree, dict) and tree:
            return bundle_tree_hash(tree)
        raise ToolchainCorrupt("offline tree provenance is not pinned")

    def _offline_requirements(self):
        entry = self._supported()
        for key in ("identity", "node_version", "srt_version", "srt_entrypoint"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ToolchainCorrupt("offline version evidence is not pinned")
        self._validate_bundled_package_metadata(entry)
        capability = entry.get("srt_capability")
        if capability != "settings_schema_rejected":
            raise ToolchainCorrupt("offline capability evidence is not pinned")
        return entry, self._trusted_tree_hash(entry), capability

    @classmethod
    def _validate_safe_modes(cls, directory):
        for path in [Path(directory), *Path(directory).rglob("*")]:
            info = path.lstat()
            if info.st_uid != os.getuid():
                raise ToolchainCorrupt("offline bundle ownership mismatch")
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o700:
                    raise ToolchainCorrupt("offline bundle directory mode mismatch")
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) not in {0o400, 0o500}:
                    raise ToolchainCorrupt("offline bundle file mode mismatch")
            elif not stat.S_ISLNK(info.st_mode):
                raise ToolchainCorrupt("offline bundle contains unsafe file type")

    @classmethod
    def _validate_regular_license(cls, directory, package_path, label):
        try:
            descriptor = cls._open_directory_descriptor(
                directory, PurePosixPath(package_path).parts
            )
        except ToolchainCorrupt as exc:
            raise ToolchainCorrupt(
                f"{label} license directory is unsafe",
                code="toolchain_integrity_failed",
            ) from exc
        try:
            names = sorted(os.listdir(descriptor))
        finally:
            os.close(descriptor)
        candidates = [
            name
            for name in names
            if Path(name).name.casefold().startswith(("license", "copying", "notice"))
        ]
        for name in candidates:
            try:
                data = cls._read_regular_bytes(
                    directory,
                    f"{package_path}/{name}",
                    limit=_MAX_PACKAGE_METADATA_BYTES,
                    label=f"{label} license",
                    code="toolchain_integrity_failed",
                )
            except ToolchainCorrupt:
                continue
            if data:
                return
        raise ToolchainCorrupt(
            f"{label} license is missing", code="toolchain_integrity_failed"
        )

    @classmethod
    def _validate_installed_package_closure(cls, directory, entry):
        _, lock = cls._validate_bundled_package_metadata(entry)
        for package_path, expected in lock["packages"].items():
            if not package_path:
                continue
            package_name = cls._package_name(package_path)
            code = (
                "srt_version_mismatch"
                if package_name == _SRT_PACKAGE
                else "toolchain_integrity_failed"
            )
            installed = cls._read_json(
                directory,
                f"{package_path}/package.json",
                limit=_MAX_PACKAGE_METADATA_BYTES,
                label=f"installed package metadata: {package_name}",
                code=code,
            )
            if installed.get("name") != package_name:
                raise ToolchainCorrupt(
                    f"installed package name mismatch: {package_name}",
                    code="toolchain_integrity_failed",
                )
            if installed.get("version") != expected["version"]:
                raise ToolchainCorrupt(
                    f"installed package version mismatch: {package_name}", code=code
                )
            if installed.get("license") != expected["license"]:
                raise ToolchainCorrupt(
                    f"installed package license mismatch: {package_name}",
                    code="toolchain_integrity_failed",
                )
            cls._validate_regular_license(directory, package_path, package_name)
        cls._validate_regular_license(directory, "node", "managed Node")
        return lock

    @classmethod
    def _validate_license_coverage(cls, directory, manifest, entry):
        licenses = set(manifest.get("licenses", ()))
        if not licenses:
            raise ToolchainCorrupt(
                "offline bundle license evidence is missing",
                code="toolchain_integrity_failed",
            )
        if not entry.get("node_version"):
            return
        if "node/LICENSE" not in licenses:
            raise ToolchainCorrupt(
                "managed Node license is missing", code="toolchain_integrity_failed"
            )
        _, lock = cls._validate_bundled_package_metadata(entry)
        for package_path, package in lock.get("packages", {}).items():
            if not package_path or not package.get("license"):
                continue
            if not any(Path(path).parent.as_posix() == package_path for path in licenses):
                raise ToolchainCorrupt(
                    f"package license is missing: {package_path}",
                    code="toolchain_integrity_failed",
                )
        for path in licenses:
            cls._read_regular_bytes(
                directory,
                path,
                limit=_MAX_PACKAGE_METADATA_BYTES,
                label="indexed license",
                code="toolchain_integrity_failed",
            )

    def _validate_directory(
        self,
        directory,
        *,
        trusted_tree_sha256="",
        expected_capability=None,
        safe_modes=False,
    ):
        entry = self._supported()
        directory = Path(directory)
        if not directory.is_dir() or directory.is_symlink():
            raise ToolchainCorrupt("toolchain missing or unsafe")
        root_stat = directory.stat()
        if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
            raise ToolchainCorrupt("toolchain ownership or mode mismatch")
        marker = self._read_marker(directory)
        if marker["bundle_id"] != entry["identity"]:
            raise ToolchainCorrupt("identity mismatch")
        capability = (
            "settings_schema_rejected"
            if expected_capability is None and entry.get("node_version")
            else expected_capability
        )
        if capability is not None and marker["srt_capability"] != capability:
            raise ToolchainCorrupt("SRT capability gate failed")
        tree = self._tree(directory)
        if marker["tree"] != tree:
            raise ToolchainCorrupt("tree mismatch")
        expected_tree = entry.get("tree")
        if expected_tree is not None and tree != expected_tree:
            raise ToolchainCorrupt("tree mismatch")
        if trusted_tree_sha256 and bundle_tree_hash(tree) != trusted_tree_sha256:
            raise ToolchainCorrupt("offline tree provenance mismatch")
        lock_path = directory / "package-lock.json"
        if entry.get("node_version"):
            package_bytes = self._read_regular_bytes(
                directory,
                "package.json",
                limit=_MAX_PACKAGE_METADATA_BYTES,
                label="package manifest",
                code="toolchain_integrity_failed",
            )
            lock_bytes = self._read_regular_bytes(
                directory,
                "package-lock.json",
                limit=_MAX_PACKAGE_METADATA_BYTES,
                label="package lock",
                code="toolchain_integrity_failed",
            )
            if package_bytes != self._bundled_package_bytes("package.json"):
                raise ToolchainCorrupt(
                    "package manifest mismatch", code="toolchain_integrity_failed"
                )
            if lock_bytes != self._bundled_package_bytes("package-lock.json"):
                raise ToolchainCorrupt(
                    "package lock mismatch", code="toolchain_integrity_failed"
                )
            if marker["package_lock_sha256"] != self._bundled_lock_hash():
                raise ToolchainCorrupt(
                    "package lock mismatch", code="toolchain_integrity_failed"
                )
        elif lock_path.exists():
            self._regular_file(lock_path, "package lock")
            if marker["package_lock_sha256"] != hashlib.sha256(lock_path.read_bytes()).hexdigest():
                raise ToolchainCorrupt("package lock mismatch")
        elif marker["package_lock_sha256"]:
            raise ToolchainCorrupt("package lock mismatch")
        node_path = self._node_path(directory, entry)
        node_info = self._regular_file(node_path, "managed Node")
        srt_path = self._srt_path(directory, entry)
        if not stat.S_IMODE(node_info.st_mode) & 0o100:
            raise ToolchainCorrupt("managed Node missing or not executable")
        if entry.get("srt_entrypoint"):
            self._regular_file(srt_path, "SRT entry")
        if entry.get("node_version"):
            self._validate_installed_package_closure(directory, entry)
        if safe_modes:
            self._validate_safe_modes(directory)
        return marker, tree

    def validate_offline_candidate(self, directory, manifest):
        entry, trusted_tree_sha256, capability = self._offline_requirements()
        system, _, architecture = self.platform.partition("-")
        expected = {
            "identity": entry.get("identity", ""),
            "platform": system,
            "arch": architecture,
            "node_version": entry.get("node_version", ""),
            "srt_version": entry.get("srt_version", ""),
            "package_lock_sha256": (
                self._bundled_lock_hash() if entry.get("node_version") else ""
            ),
            "srt_capability": capability or "",
            "tree_sha256": trusted_tree_sha256,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ToolchainCorrupt("offline bundle metadata mismatch")
        self._validate_directory(
            directory,
            trusted_tree_sha256=trusted_tree_sha256,
            expected_capability=capability,
            safe_modes=True,
        )
        self._validate_license_coverage(directory, manifest, entry)
        return self._payload("ready", "ready")

    def offline_bundle_metadata(self):
        entry, trusted_tree_sha256, capability = self._offline_requirements()
        marker, tree = self._validate_directory(
            self.install_dir,
            trusted_tree_sha256=trusted_tree_sha256,
            expected_capability=capability,
        )
        licenses = [
            path
            for path in tree
            if Path(path).name.casefold().startswith(("license", "copying", "notice"))
        ]
        self._validate_license_coverage(
            self.install_dir,
            {"licenses": licenses},
            entry,
        )
        system, _, architecture = self.platform.partition("-")
        return {
            "identity": entry["identity"],
            "platform": system,
            "arch": architecture,
            "node_version": entry.get("node_version", ""),
            "srt_version": entry.get("srt_version", ""),
            "package_lock_sha256": marker["package_lock_sha256"],
            "srt_capability": marker["srt_capability"],
        }

    def identity(self):
        self.validate()
        entry = self._supported()
        marker = self.install_dir / _MARKER
        package_json = self.install_dir / "package.json"
        paths = [self._node_path(self.install_dir, entry), marker]
        if package_json.exists():
            paths.append(package_json)
        lock = self.install_dir / "package-lock.json"
        if lock.exists():
            paths.append(lock)
        srt = self._srt_path(self.install_dir, entry)
        if srt.exists():
            paths.append(srt)
        return SandboxIdentity(
            trusted_root=self.install_dir,
            node_path=paths[0],
            srt_entry_path=srt,
            package_json_path=package_json,
            bundle_manifest_hash=hashlib.sha256(marker.read_bytes()).hexdigest(),
            file_identities=tuple(
                capture_file_identity(path, self.install_dir) for path in paths
            ),
        )

    @staticmethod
    def _node_path(directory, entry):
        if entry.get("node_version"):
            return directory / "node" / "bin" / "node"
        return directory / "bin" / "node"

    @staticmethod
    def _srt_path(directory, entry):
        value = entry.get("srt_entrypoint", "")
        return directory / value if value else directory / "package.json"

    @staticmethod
    def _tree(directory):
        result = {}
        root = directory.resolve(strict=True)
        for path in sorted(directory.rglob("*")):
            if path.name == _MARKER or path.is_dir():
                continue
            relative = path.relative_to(directory).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                resolved = (path.parent / target).resolve(strict=False)
                if not resolved.is_relative_to(root):
                    raise ToolchainCorrupt("unsafe installed link")
                payload = f"symlink:{target}".encode()
            elif path.is_file():
                payload = path.read_bytes()
            else:
                raise ToolchainCorrupt("unsafe installed tree")
            result[relative] = hashlib.sha256(payload).hexdigest()
        return result

    def install(self):
        if self._candidate_reason:
            return self._payload("not_ready", self._candidate_reason)
        self._supported()
        with locked_file(self.lock_path, require_lock=True, lock_timeout=self.lock_timeout):
            status = self.status()["status"]
            if status == "ready":
                result = self.validate()
                acquire_bundle_lease(self.root, self.install_dir.name)
                return result
            if status == "corrupt":
                raise ToolchainCorrupt("existing toolchain is corrupt; use repair")
            result = self._install_locked()
            acquire_bundle_lease(self.root, self.install_dir.name)
            return result

    def repair(self):
        if self._candidate_reason:
            return self._payload("not_ready", self._candidate_reason)
        self._supported()
        with locked_file(self.lock_path, require_lock=True, lock_timeout=self.lock_timeout):
            if self.status()["status"] == "ready":
                result = self.validate()
                acquire_bundle_lease(self.root, self.install_dir.name)
                return result
            quarantine = None
            if os.path.lexists(self.install_dir):
                quarantine = self.root / "quarantine" / str(time.time_ns())
                ensure_private_dir(quarantine.parent)
                os.replace(self.install_dir, quarantine)
                self._fsync_dir(self.install_dir.parent)
                self._fsync_dir(quarantine.parent)
            try:
                result = self._install_locked()
            except BaseException:
                if quarantine is not None:
                    if os.path.lexists(self.install_dir):
                        self._remove_path(self.install_dir)
                        self._fsync_dir(self.install_dir.parent)
                    os.replace(quarantine, self.install_dir)
                    self._fsync_dir(quarantine.parent)
                    self._fsync_dir(self.install_dir.parent)
                raise
            if quarantine is not None:
                self._remove_path(quarantine)
                self._fsync_dir(quarantine.parent)
            acquire_bundle_lease(self.root, self.install_dir.name)
            return result

    def _install_locked(self):
        entry = self._supported()
        self._require_approved_candidate()
        validation_kwargs = {}
        if entry.get("node_version"):
            _, trusted_tree_sha256, capability = self._offline_requirements()
            validation_kwargs = {
                "trusted_tree_sha256": trusted_tree_sha256,
                "expected_capability": capability,
            }
        ensure_private_dir(self.root / "bundles")
        if shutil.disk_usage(self.root).free < _MIN_FREE_BYTES:
            raise OSError("insufficient disk space for sandbox toolchain")
        payload = self.downloader(self._artifact_url(entry))
        if not isinstance(payload, bytes) or len(payload) > _MAX_ARCHIVE_BYTES:
            raise ValueError("archive exceeds size limit")
        if entry.get("size") is not None and len(payload) != entry["size"]:
            raise ValueError("archive size mismatch")
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError("archive hash mismatch")
        staging_root = ensure_private_dir(self.root / "staging")
        staging = Path(tempfile.mkdtemp(prefix="candidate-", dir=staging_root))
        os.chmod(staging, 0o700)
        try:
            if entry.get("node_version"):
                extracted = staging / ".node-extract"
                extracted.mkdir(mode=0o700)
                self._unpack(payload, extracted)
                roots = [path for path in extracted.iterdir()]
                if len(roots) != 1 or not roots[0].is_dir():
                    raise ValueError("Node archive must contain one root directory")
                archive_root = entry.get("archive_root")
                if archive_root and roots[0].name != archive_root:
                    raise ValueError("Node archive root mismatch")
                os.replace(roots[0], staging / "node")
                extracted.rmdir()
                self._verify_node(staging, entry)
                self._install_srt(staging, entry)
            else:
                self._unpack(payload, staging)
            tree = self._tree(staging)
            expected_tree = entry.get("tree")
            if expected_tree and tree != expected_tree:
                raise ToolchainCorrupt("downloaded tree mismatch")
            lock = staging / "package-lock.json"
            marker = {
                "format_version": 1,
                "bundle_id": entry["identity"],
                "tree": tree,
                "package_lock_sha256": (
                    hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else ""
                ),
                "srt_capability": (
                    "settings_schema_rejected" if entry.get("node_version") else "not_applicable"
                ),
            }
            marker_path = staging / _MARKER
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(marker_path, 0o600)
            self._validate_directory(staging, **validation_kwargs)
            self._fsync_tree(staging)
            os.replace(staging, self.install_dir)
            self._fsync_dir(self.install_dir.parent)
            return self.validate()
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _verify_node(self, staging, entry):
        node = self._node_path(staging, entry)
        if not node.is_file():
            raise ToolchainCorrupt("managed Node archive is incomplete")
        _verify_binary_architecture(node, self.platform)
        try:
            result = self.runner(
                [str(node), "--version"],
                cwd=staging,
                env={"HOME": str(staging), "PATH": str(node.parent), "TMPDIR": str(staging)},
                check=False,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolchainCorrupt("managed Node version probe failed") from exc
        if (
            getattr(result, "returncode", None) != 0
            or str(getattr(result, "stdout", "")).strip()
            != f"v{entry['node_version']}"
        ):
            raise ToolchainCorrupt(
                "managed Node version mismatch", code="node_version_mismatch"
            )

    def _install_srt(self, staging, entry):
        package_root = resources.files("pico._sandbox_toolchain")
        for name in ("package.json", "package-lock.json"):
            target = staging / name
            target.write_bytes(package_root.joinpath(name).read_bytes())
            os.chmod(target, 0o600)
        node = self._node_path(staging, entry)
        npm = staging / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if not node.is_file() or not npm.is_file():
            raise ToolchainCorrupt("managed Node archive is incomplete")
        home = staging / ".install-home"
        cache = home / "npm-cache"
        cache.mkdir(mode=0o700, parents=True)
        user_npmrc = home / "user.npmrc"
        global_npmrc = home / "global.npmrc"
        for npmrc in (user_npmrc, global_npmrc):
            npmrc.touch(mode=0o600)
        env = {
            "HOME": str(home),
            "PATH": str(node.parent),
            "TMPDIR": str(home),
            "npm_config_cache": str(cache),
            "npm_config_userconfig": str(user_npmrc),
            "npm_config_globalconfig": str(global_npmrc),
        }
        if self.mirror is not None:
            env.update(
                {
                    "npm_config_registry": self.mirror["npm_registry_url"],
                    "npm_config_replace_registry_host": "always",
                }
            )
        self.runner(
            [
                str(node),
                str(npm),
                "ci",
                "--ignore-scripts",
                "--omit=dev",
                "--no-audit",
                "--no-fund",
            ],
            cwd=staging,
            env=env,
            check=True,
        )
        shutil.rmtree(home)
        if not self._srt_path(staging, entry).is_file():
            raise ToolchainCorrupt("SRT entry missing after npm ci")
        self._validate_installed_package_closure(staging, entry)
        if not self._external_runner:
            settings = staging / ".srt-capability-settings.json"
            valid_settings = {
                "network": {
                    "allowedDomains": [], "deniedDomains": ["*"],
                    "allowLocalBinding": False, "allowUnixSockets": [],
                    "allowAllUnixSockets": False,
                },
                "filesystem": {
                    "denyRead": [], "allowRead": [],
                    "allowWrite": [str(staging)], "denyWrite": [],
                },
            }
            settings.write_text(json.dumps(valid_settings), encoding="utf-8")
            settings.chmod(0o600)
            valid_probe = subprocess.run(
                [str(node), str(self._srt_path(staging, entry)), "--settings", str(settings), "--", "/usr/bin/true"],
                cwd=staging, env=env, capture_output=True, text=True, check=False, timeout=10,
            )
            if valid_probe.returncode != 0:
                raise ToolchainCorrupt("SRT capability gate failed: valid settings rejected")
            invalid_settings = dict(valid_settings, unknownPicoProbeKey=True)
            settings.write_text(json.dumps(invalid_settings), encoding="utf-8")
            probe = subprocess.run(
                [str(node), str(self._srt_path(staging, entry)), "--settings", str(settings), "--", "/usr/bin/true"],
                cwd=staging,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            settings.unlink(missing_ok=True)
            if probe.returncode == 0:
                raise ToolchainCorrupt("SRT capability gate failed: unknown settings accepted")

    @staticmethod
    def _unpack(payload, destination):
        destination = Path(destination).resolve(strict=True)
        links = []
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe tar member: {member.name}")
                target = destination.joinpath(*path.parts)
                if not target.resolve(strict=False).is_relative_to(destination):
                    raise ValueError(f"unsafe tar member: {member.name}")
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"unsafe tar member: {member.name}")
                    with source, open(target, "xb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, 0o500 if member.mode & 0o111 else 0o400)
                elif member.issym() or member.islnk():
                    links.append((member, target))
                else:
                    raise ValueError(f"unsafe tar member: {member.name}")
            for member, target in links:
                link = PurePosixPath(member.linkname)
                base = destination if member.islnk() else target.parent
                resolved = base.joinpath(*link.parts).resolve(strict=False)
                if link.is_absolute() or not resolved.is_relative_to(destination):
                    raise ValueError(f"unsafe tar member: {member.name}")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if member.islnk():
                    if not resolved.is_file():
                        raise ValueError(f"unsafe tar member: {member.name}")
                    os.link(resolved, target)
                else:
                    if not resolved.exists():
                        raise ValueError(f"unsafe tar member: {member.name}")
                    os.symlink(member.linkname, target)

    @staticmethod
    def _fsync_dir(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_path(path):
        info = Path(path).lstat()
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            shutil.rmtree(path)
        else:
            Path(path).unlink()

    @classmethod
    def _fsync_tree(cls, root):
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            cls._fsync_dir(path)
        cls._fsync_dir(root)
