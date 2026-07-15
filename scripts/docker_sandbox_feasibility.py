#!/usr/bin/env python3
"""Standalone Docker Sandbox D1 feasibility harness.

This file is intentionally not imported by Pico's product runtime. D1 evidence may
authorize implementation work; it can never enable the Sandbox product path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_LOGICAL_BYTES = 1024 * 1024 * 1024
MAX_ALLOCATED_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_DEPTH = 32
MAX_STREAM_BYTES = 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
MAX_OCI_JSON_BYTES = 16 * 1024 * 1024

IMAGE_LOCK_FIELDS = {
    "format_version",
    "platform",
    "base_image",
    "build_inputs",
    "debian_snapshot",
    "debian_packages",
    "python_wheels",
    "uv",
}
_ASSET_HOSTS = {
    "files.pythonhosted.org",
    "github.com",
    "release-assets.githubusercontent.com",
    "snapshot.debian.org",
}
_IMAGE_LABELS = {
    "io.pico.sandbox.image-policy": "1",
    "io.pico.sandbox.managed": "true",
    "org.opencontainers.image.title": "Pico Docker Sandbox",
    "org.opencontainers.image.version": "d1",
}
_IMAGE_ARTIFACT_FIELDS = {
    "record_type",
    "format_version",
    "status",
    "reason_code",
    "candidate_digest",
    "platform",
    "base_manifest_digest",
    "image_id",
    "image_reference",
    "image_tag",
    "buildx_sha256",
    "dockerfile_digest",
    "lock_digest",
    "metadata_digest",
    "oci_layout_digest",
    "oci_layout_size",
    "sbom_present",
    "provenance_present",
    "downloaded_asset_count",
    "asset_count",
    "build_stdout_bytes",
    "build_stderr_bytes",
    "build_output_truncated",
    "network_performed",
    "mutation_performed",
    "product_enablement",
}
_CALIBRATION_ARTIFACT_FIELDS = {
    "record_type",
    "format_version",
    "status",
    "reason_code",
    "candidate_digest",
    "policy_digest",
    "image_reference",
    "image_id",
    "bind_recursive",
    "tmpfs_noexec",
    "log_driver",
    "create_count",
    "target_started_count",
    "cleanup_verified",
    "other_containers_unchanged",
    "network_performed",
    "mutation_performed",
    "product_enablement",
}
_D1_POLICY = {
    "version": 1,
    "network": "none",
    "bind_recursive": "disabled",
    "bind_propagation": "rprivate",
    "read_only_rootfs": True,
    "cap_drop": ["ALL"],
    "no_new_privileges": True,
    "pids_limit": 256,
    "memory_bytes": 2 * 1024**3,
    "memory_swap_bytes": 2 * 1024**3,
    "nano_cpus": 2_000_000_000,
    "shm_bytes": 64 * 1024**2,
    "nofile": [1024, 1024],
    "core": [0, 0],
    "log_driver": "none",
    "tmpfs": {
        "/tmp": "rw,nosuid,nodev,exec,size=768m,mode=1777",
        "/home/pico": "rw,nosuid,nodev,noexec,size=64m,mode=700,uid=10001,gid=10001",
        "/run": "rw,nosuid,nodev,noexec,size=16m,mode=755,uid=10001,gid=10001",
    },
}
POLICY_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(_D1_POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

GUEST_ENV = (
    "PATH=/opt/pico-venv/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME=/home/pico",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "PICO_SANDBOX=1",
    "PICO_WORKSPACE=/workspace",
    "PYTHONDONTWRITEBYTECODE=1",
    "TMPDIR=/tmp",
)

MANDATORY_CHECK_IDS = (
    "status_zero_mutation",
    "source_stable_staging",
    "sensitive_filtering",
    "unsupported_entry_rejection",
    "mount_boundary_rejection",
    "image_identity",
    "image_config",
    "container_contract",
    "source_not_mounted",
    "state_not_mounted",
    "external_network_denied",
    "container_loopback_allowed",
    "privilege_denied",
    "readonly_rootfs",
    "resource_limits",
    "output_bounded",
    "target_success",
    "target_nonzero",
    "timeout_cleanup",
    "detached_cleanup",
    "workspace_cross_call_persistence",
    "home_cross_call_ephemeral",
    "trusted_diff",
    "source_unchanged",
    "fixture_apply_success",
    "fixture_apply_conflict",
    "fixture_apply_rollback",
    "create_reconciliation",
    "other_container_untouched",
    "compatibility_pytest",
    "compatibility_ruff",
    "synthetic_git_semantics",
    "container_cleanup",
    "zero_host_fallback",
)
CORPUS_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(MANDATORY_CHECK_IDS, separators=(",", ":")).encode()
).hexdigest()

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ALLOWED_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_BASENAMES = {
    ".env",
    ".envrc",
    ".netrc",
    "credentials",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
_GENERATED_DIRS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_AGENT_DIRS = {".claude", ".superpowers"}


class D1Error(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _sha256_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise D1Error("artifact_invalid")
        value[key] = item
    return value


def _reject_constant(_value):
    raise D1Error("artifact_invalid")


def _decode_json(raw, expected_fields=None):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
        )
    except D1Error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D1Error("artifact_invalid") from exc
    if not isinstance(value, dict):
        raise D1Error("artifact_schema_invalid")
    if expected_fields is not None and set(value) != set(expected_fields):
        raise D1Error("artifact_schema_invalid")
    return value


def _open_private_root(root):
    root = Path(os.path.abspath(os.fspath(root)))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise D1Error("artifact_invalid") from exc
    info = os.fstat(descriptor)
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        os.close(descriptor)
        raise D1Error("artifact_invalid")
    return root, descriptor, (info.st_dev, info.st_ino)


def _read_artifact_bytes(path, trusted_root, *, max_bytes):
    path = Path(os.path.abspath(os.fspath(path)))
    root, root_fd, root_identity = _open_private_root(trusted_root)
    descriptor = -1
    try:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise D1Error("artifact_invalid") from exc
        if len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
            raise D1Error("artifact_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(relative.name, flags, dir_fd=root_fd)
        except OSError as exc:
            raise D1Error("artifact_invalid") from exc
        before = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else before.st_uid
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size > max_bytes
        ):
            raise D1Error("artifact_invalid")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(relative.name, dir_fd=root_fd, follow_symlinks=False)
        root_after = os.fstat(root_fd)
        if (
            len(raw) > max_bytes
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
            or (root_after.st_dev, root_after.st_ino) != root_identity
        ):
            raise D1Error("artifact_invalid")
        return raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_fd)


def _read_json_artifact(path, trusted_root, expected_fields, *, max_bytes=MAX_ARTIFACT_BYTES):
    raw = _read_artifact_bytes(path, trusted_root, max_bytes=max_bytes)
    return _decode_json(raw, expected_fields)


def _atomic_write_json(path, trusted_root, value):
    raw = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise D1Error("artifact_invalid")
    path = Path(os.path.abspath(os.fspath(path)))
    root, root_fd, _ = _open_private_root(trusted_root)
    temp_name = ".tmp-" + secrets.token_hex(16)
    descriptor = -1
    try:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise D1Error("artifact_invalid") from exc
        if len(relative.parts) != 1:
            raise D1Error("artifact_invalid")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise D1Error("artifact_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temp_name, relative.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
    except D1Error:
        raise
    except OSError as exc:
        raise D1Error("artifact_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)


def _directory_snapshot(path):
    path = Path(path)
    result = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        info = child.lstat()
        digest = ""
        if stat.S_ISREG(info.st_mode):
            digest = _sha256_bytes(child.read_bytes())
        result.append((child.name, _identity(info), digest))
    return tuple(result)


def _strict_empty_docker_config(config_dir):
    config_dir = Path(os.path.abspath(os.fspath(config_dir)))
    try:
        root_info = config_dir.lstat()
        children = sorted(item.name for item in config_dir.iterdir())
    except OSError as exc:
        raise D1Error("docker_config_untrusted") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else root_info.st_uid
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != uid
        or root_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or children != ["config.json"]
    ):
        raise D1Error("docker_config_untrusted")
    config = config_dir / "config.json"
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(config, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size > 16
        ):
            raise D1Error("docker_config_untrusted")
        raw = os.read(descriptor, 17)
        after = os.fstat(descriptor)
        current = config.lstat()
        if (
            raw.strip() != b"{}"
            or any(byte not in b"{} \t\r\n" for byte in raw)
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
        ):
            raise D1Error("docker_config_untrusted")
        return {
            "sha256": _sha256_bytes(raw),
            "device": before.st_dev,
            "inode": before.st_ino,
            "size": before.st_size,
        }
    except D1Error:
        raise
    except OSError as exc:
        raise D1Error("docker_config_untrusted") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_regular_file(path):
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise D1Error("docker_cli_untrusted")
        return before, "sha256:" + digest.hexdigest()
    except D1Error:
        raise
    except OSError as exc:
        raise D1Error("docker_cli_untrusted") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_stable_regular_file(path, *, max_bytes, error_code):
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > max_bytes
        ):
            raise D1Error(error_code)
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            len(raw) > max_bytes
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(current)
        ):
            raise D1Error(error_code)
        return raw
    except D1Error:
        raise
    except OSError as exc:
        raise D1Error(error_code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _file_digest(path, *, max_bytes=MAX_FILE_BYTES, error_code="image_input_invalid"):
    raw = _read_stable_regular_file(
        path,
        max_bytes=max_bytes,
        error_code=error_code,
    )
    return _sha256_bytes(raw), len(raw)


def _freeze_cli(path):
    entry = Path(os.path.abspath(os.fspath(path)))
    try:
        entry_info = entry.lstat()
        if stat.S_ISLNK(entry_info.st_mode):
            if entry_info.st_uid not in {0, os.geteuid()}:
                raise D1Error("docker_cli_untrusted")
        elif not stat.S_ISREG(entry_info.st_mode):
            raise D1Error("docker_cli_untrusted")
        resolved = entry.resolve(strict=True)
        info, digest = _hash_regular_file(resolved)
    except D1Error:
        raise
    except (OSError, RuntimeError) as exc:
        raise D1Error("docker_cli_untrusted") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        raise D1Error("docker_cli_untrusted")
    return {
        "entry_path": str(entry),
        "resolved_path": str(resolved),
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": digest,
    }


def _verify_cli(identity):
    current = _freeze_cli(identity["entry_path"])
    if current != identity:
        raise D1Error("docker_cli_changed")


def _freeze_socket(path):
    try:
        canonical = Path(path).resolve(strict=True)
        info = canonical.lstat()
    except (OSError, RuntimeError) as exc:
        raise D1Error("docker_endpoint_untrusted") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise D1Error("docker_endpoint_untrusted")
    return {
        "canonical_path": str(canonical),
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
    }


def _verify_socket(identity):
    try:
        current = _freeze_socket(identity["canonical_path"])
    except D1Error as exc:
        raise D1Error("docker_endpoint_changed") from exc
    if current != identity:
        raise D1Error("docker_endpoint_changed")


def _drain_stream(stream, retained, counts, index, max_bytes):
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            counts[index] += len(chunk)
            available = max(0, max_bytes - len(retained[index]))
            if available:
                retained[index].extend(chunk[:available])
    finally:
        stream.close()


def _run_bounded_process(argv, *, env, timeout, max_bytes=MAX_STREAM_BYTES):
    try:
        process = subprocess.Popen(
            [str(item) for item in argv],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise D1Error("docker_cli_failed") from exc
    retained = [bytearray(), bytearray()]
    counts = [0, 0]
    threads = [
        threading.Thread(
            target=_drain_stream,
            args=(stream, retained, counts, index, max_bytes),
            daemon=True,
        )
        for index, stream in enumerate((process.stdout, process.stderr))
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        raise D1Error("docker_cli_output_incomplete")
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": bytes(retained[0]),
        "stderr": bytes(retained[1]),
        "stdout_bytes": counts[0],
        "stderr_bytes": counts[1],
        "stdout_truncated": counts[0] > max_bytes,
        "stderr_truncated": counts[1] > max_bytes,
    }


def _docker_env(config_dir):
    return {
        "DOCKER_CONFIG": str(Path(config_dir).resolve()),
        "HOME": str(Path(config_dir).resolve()),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _run_docker(cli, endpoint, config_dir, args, *, timeout=30):
    if not isinstance(args, (list, tuple)) or not args or any(
        type(item) is not str or not item for item in args
    ):
        raise D1Error("docker_argv_invalid")
    before = _directory_snapshot(config_dir)
    config_identity = _strict_empty_docker_config(config_dir)
    _verify_cli(cli)
    _verify_socket(endpoint)
    argv = [
        cli["resolved_path"],
        "--config",
        str(Path(config_dir).resolve()),
        "--host",
        "unix://" + endpoint["canonical_path"],
        *args,
    ]
    result = _run_bounded_process(
        argv,
        env=_docker_env(config_dir),
        timeout=timeout,
    )
    _verify_cli(cli)
    _verify_socket(endpoint)
    if _strict_empty_docker_config(config_dir) != config_identity or _directory_snapshot(config_dir) != before:
        raise D1Error("status_mutation_detected")
    return result


def _decode_docker_json(result):
    if result["timed_out"] or result["exit_code"] != 0 or result["stdout_truncated"]:
        raise D1Error("docker_daemon_unavailable")
    try:
        return json.loads(
            result["stdout"].decode("utf-8"),
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
        )
    except D1Error:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise D1Error("docker_response_invalid") from exc


def build_status_report(*, docker_cli, socket_path, docker_config):
    cli = _freeze_cli(docker_cli)
    endpoint = _freeze_socket(socket_path)
    config = _strict_empty_docker_config(docker_config)
    version = _decode_docker_json(
        _run_docker(
            cli,
            endpoint,
            docker_config,
            ["version", "--format", "{{json .}}"],
        )
    )
    info = _decode_docker_json(
        _run_docker(
            cli,
            endpoint,
            docker_config,
            ["info", "--format", "{{json .}}"],
        )
    )
    try:
        client = version["Client"]
        server = version["Server"]
        security_options = info.get("SecurityOptions") or []
        host_is_macos = os.uname().sysname.lower() == "darwin"
        profile = "desktop_vm" if host_is_macos else "linux_rootless"
        rootless = any("rootless" in str(item).lower() for item in security_options)
        seccomp = next(
            (str(item) for item in security_options if "seccomp" in str(item).lower()),
            "",
        )
        limits = all(
            info.get(name) is True
            for name in ("MemoryLimit", "CpuCfsPeriod", "PidsLimit")
        )
        ready = (
            info["OSType"] == "linux"
            and info["Architecture"] in {"aarch64", "arm64", "x86_64", "amd64"}
            and "seccomp" in seccomp.lower()
            and limits
            and (host_is_macos or rootless)
        )
    except (KeyError, TypeError) as exc:
        raise D1Error("docker_response_invalid") from exc
    reason = "ready" if ready else "required_capability_missing"
    return {
        "record_type": "docker_sandbox_d1_status",
        "format_version": 1,
        "status": "ready" if ready else "not_ready",
        "reason_code": reason,
        "platform_profile": profile,
        "client_version": str(client.get("Version", "")),
        "server_version": str(server.get("Version", "")),
        "api_version": str(server.get("ApiVersion", "")),
        "server_os": str(info.get("OSType", "")),
        "server_arch": str(info.get("Architecture", "")),
        "endpoint_kind": "local_unix",
        "security": {
            "rootless": rootless,
            "seccomp": "builtin" if "profile=builtin" in seccomp.lower() else "enabled",
            "cgroup_limits": limits,
        },
        "cli_sha256": cli["sha256"],
        "endpoint_identity": _sha256_bytes(
            json.dumps(
                {key: value for key, value in endpoint.items() if key != "canonical_path"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ),
        "config_sha256": config["sha256"],
        "network_performed": False,
        "mutation_performed": False,
        "product_enablement": False,
    }


def _validate_asset(asset, expected_fields):
    if not isinstance(asset, dict) or set(asset) != expected_fields:
        raise D1Error("image_lock_invalid")
    filename = asset.get("filename")
    digest = asset.get("sha256")
    size = asset.get("size")
    url = asset.get("url")
    if (
        type(filename) is not str
        or not filename
        or Path(filename).name != filename
        or _CONTROL_RE.search(filename)
        or _HEX_64_RE.fullmatch(digest or "") is None
        or type(size) is not int
        or size <= 0
        or size > MAX_FILE_BYTES
        or type(url) is not str
    ):
        raise D1Error("image_lock_invalid")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ASSET_HOSTS:
        raise D1Error("image_lock_invalid")


def _load_image_lock(path):
    raw = _read_stable_regular_file(
        path,
        max_bytes=MAX_LOCK_BYTES,
        error_code="image_lock_invalid",
    )
    lock = _decode_json(raw, IMAGE_LOCK_FIELDS)
    if lock.get("format_version") != 1 or lock.get("platform") != "linux/arm64":
        raise D1Error("image_lock_invalid")
    base = lock.get("base_image")
    if (
        not isinstance(base, dict)
        or set(base)
        != {"index_digest", "manifest_digest", "reference", "version"}
        or _SHA256_RE.fullmatch(base.get("index_digest") or "") is None
        or _SHA256_RE.fullmatch(base.get("manifest_digest") or "") is None
        or base.get("reference") != "python@" + base.get("manifest_digest", "")
        or base.get("version") != "3.12.13"
    ):
        raise D1Error("image_lock_invalid")
    build_inputs = lock.get("build_inputs")
    if (
        not isinstance(build_inputs, dict)
        or set(build_inputs) != {"pyproject_sha256", "uv_lock_sha256"}
        or any(_HEX_64_RE.fullmatch(value or "") is None for value in build_inputs.values())
    ):
        raise D1Error("image_lock_invalid")
    snapshot = lock.get("debian_snapshot")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"packages_index_sha256", "timestamp"}
        or _HEX_64_RE.fullmatch(snapshot.get("packages_index_sha256") or "") is None
        or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", snapshot.get("timestamp") or "") is None
    ):
        raise D1Error("image_lock_invalid")
    packages = lock.get("debian_packages")
    wheels = lock.get("python_wheels")
    if not isinstance(packages, list) or not packages or not isinstance(wheels, list) or not wheels:
        raise D1Error("image_lock_invalid")
    for package in packages:
        _validate_asset(
            package,
            {"architecture", "filename", "name", "sha256", "size", "url", "version"},
        )
        if package["architecture"] not in {"all", "arm64"}:
            raise D1Error("image_lock_invalid")
    for wheel in wheels:
        _validate_asset(
            wheel,
            {"filename", "name", "sha256", "size", "url", "version"},
        )
    uv = lock.get("uv")
    _validate_asset(
        uv,
        {"filename", "sha256", "size", "url", "version"},
    )
    if uv.get("version") != "0.11.26":
        raise D1Error("image_lock_invalid")
    filenames = [asset["filename"] for asset in [uv, *wheels, *packages]]
    if len(filenames) != len(set(filenames)):
        raise D1Error("image_lock_invalid")
    return lock, _sha256_bytes(raw)


def _ensure_private_dir(path):
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise D1Error("d1_state_invalid") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise D1Error("d1_state_invalid")
    return path


def _ensure_readonly_docker_config(path):
    path = Path(os.path.abspath(os.fspath(path)))
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
        _write_private_file(path / "config.json", b"{}\n")
        os.chmod(path / "config.json", 0o400)
        os.chmod(path, 0o500)
    _strict_empty_docker_config(path)
    return path


def _write_private_file(path, data):
    path = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise D1Error("image_input_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    except D1Error:
        raise
    except OSError as exc:
        raise D1Error("image_input_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _cached_asset_valid(path, asset):
    try:
        info = path.lstat()
        uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != uid
            or info.st_size != asset["size"]
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
        digest, size = _file_digest(path, max_bytes=asset["size"])
        return size == asset["size"] and digest == "sha256:" + asset["sha256"]
    except (OSError, D1Error):
        return False


def _download_asset(asset, cache_root):
    target = cache_root / asset["filename"]
    if _cached_asset_valid(target, asset):
        return target, False
    try:
        if target.exists() or target.is_symlink():
            target.unlink()
    except OSError as exc:
        raise D1Error("image_input_invalid") from exc
    temp = cache_root / (".download-" + secrets.token_hex(16))
    descriptor = -1
    try:
        request = urllib.request.Request(
            asset["url"],
            headers={"User-Agent": "pico-d1-feasibility/1"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in _ASSET_HOSTS:
                raise D1Error("image_download_redirect_untrusted")
            descriptor = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > asset["size"]:
                    raise D1Error("image_input_invalid")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise D1Error("image_input_write_failed")
                    view = view[written:]
            if total != asset["size"] or digest.hexdigest() != asset["sha256"]:
                raise D1Error("image_input_invalid")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
        os.replace(temp, target)
        parent = os.open(cache_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        if not _cached_asset_valid(target, asset):
            raise D1Error("image_input_invalid")
        return target, True
    except D1Error:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise D1Error("image_download_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _copy_verified_input(source, destination, expected_digest):
    source_digest, _ = _file_digest(source)
    if source_digest != "sha256:" + expected_digest:
        raise D1Error("image_input_changed")
    shutil.copyfile(source, destination, follow_symlinks=False)
    os.chmod(destination, 0o600)
    copied_digest, _ = _file_digest(destination)
    if copied_digest != source_digest:
        raise D1Error("image_input_changed")


def _build_context(context, *, repo_root, dockerfile, lock_path, lock, cache):
    context = Path(context)
    for relative in ("inputs/debs", "inputs/wheels", "inputs/uv"):
        path = context / relative
        path.mkdir(mode=0o700, parents=True)
    shutil.copyfile(dockerfile, context / "Dockerfile", follow_symlinks=False)
    shutil.copyfile(lock_path, context / "image-inputs.lock.json", follow_symlinks=False)
    _copy_verified_input(
        repo_root / "pyproject.toml",
        context / "pyproject.toml",
        lock["build_inputs"]["pyproject_sha256"],
    )
    _copy_verified_input(
        repo_root / "uv.lock",
        context / "uv.lock",
        lock["build_inputs"]["uv_lock_sha256"],
    )
    checksum_lines = []
    groups = (
        ("uv", [lock["uv"]]),
        ("wheels", lock["python_wheels"]),
        ("debs", lock["debian_packages"]),
    )
    for group, assets in groups:
        for asset in assets:
            relative = f"{group}/{asset['filename']}"
            _copy_verified_input(
                cache / asset["filename"],
                context / "inputs" / relative,
                asset["sha256"],
            )
            checksum_lines.append(f"{asset['sha256']}  {relative}\n")
    _write_private_file(
        context / "inputs" / "SHA256SUMS",
        "".join(checksum_lines).encode("ascii"),
    )


def _run_buildx(buildx, endpoint, config_dir, buildx_dir, args, *, timeout):
    before = _directory_snapshot(config_dir)
    config_identity = _strict_empty_docker_config(config_dir)
    _verify_cli(buildx)
    _verify_socket(endpoint)
    env = _docker_env(config_dir)
    env["BUILDX_CONFIG"] = str(buildx_dir)
    env["DOCKER_HOST"] = "unix://" + endpoint["canonical_path"]
    result = _run_bounded_process(
        [
            buildx["resolved_path"],
            *args,
        ],
        env=env,
        timeout=timeout,
    )
    _verify_cli(buildx)
    _verify_socket(endpoint)
    if (
        _strict_empty_docker_config(config_dir) != config_identity
        or _directory_snapshot(config_dir) != before
    ):
        raise D1Error("docker_config_mutated")
    return result


def _write_process_diagnostic(state_root, name, result):
    _atomic_write_json(
        Path(state_root) / name,
        state_root,
        {
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "stdout": result["stdout"].decode("utf-8", errors="replace"),
            "stderr": result["stderr"].decode("utf-8", errors="replace"),
            "stdout_bytes": result["stdout_bytes"],
            "stderr_bytes": result["stderr_bytes"],
            "stdout_truncated": result["stdout_truncated"],
            "stderr_truncated": result["stderr_truncated"],
        },
    )


def _oci_attestations(path):
    sbom = False
    provenance = False
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = archive.getmembers()
            if any(
                not (member.isfile() or member.isdir())
                or member.name.startswith("/")
                or ".." in PurePosixPath(member.name).parts
                for member in members
            ):
                raise D1Error("oci_layout_invalid")
            for member in members:
                if not member.isfile() or member.size > MAX_OCI_JSON_BYTES:
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                raw = stream.read(MAX_OCI_JSON_BYTES + 1)
                if len(raw) > MAX_OCI_JSON_BYTES:
                    raise D1Error("oci_layout_invalid")
                sbom = sbom or b"https://spdx.dev/Document" in raw or b'"spdxVersion"' in raw
                provenance = provenance or b"https://slsa.dev/provenance" in raw
    except D1Error:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise D1Error("oci_layout_invalid") from exc
    return sbom, provenance


def _verify_image_inspect(payload):
    try:
        config = payload[0]["Config"] if isinstance(payload, list) else payload["Config"]
        architecture = payload[0]["Architecture"] if isinstance(payload, list) else payload["Architecture"]
        operating_system = payload[0]["Os"] if isinstance(payload, list) else payload["Os"]
        image_reference = payload[0]["Id"] if isinstance(payload, list) else payload["Id"]
        descriptor = payload[0].get("Descriptor", {}) if isinstance(payload, list) else payload.get("Descriptor", {})
        image_id = descriptor.get("annotations", {}).get(
            "config.digest",
            image_reference,
        )
        valid = (
            _SHA256_RE.fullmatch(image_reference or "") is not None
            and _SHA256_RE.fullmatch(image_id or "") is not None
            and architecture in {"arm64", "aarch64"}
            and operating_system == "linux"
            and config.get("Entrypoint") in (None, [])
            and config.get("Cmd") in (None, [])
            and config.get("User") == "10001:10001"
            and config.get("WorkingDir") == "/workspace"
            and config.get("Env") == list(GUEST_ENV)
            and config.get("Labels") == _IMAGE_LABELS
            and config.get("Volumes") in (None, {})
            and config.get("ExposedPorts") in (None, {})
            and config.get("Healthcheck") is None
        )
    except (KeyError, IndexError, TypeError):
        valid = False
    if not valid:
        raise D1Error("image_config_mismatch")
    return {"image_reference": image_reference, "image_id": image_id}


def _cleanup_image_candidate(cli, endpoint, config_dir, tag, paths):
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    for path in paths:
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (uid is not None and info.st_uid != uid)
            ):
                continue
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
    try:
        inspected = _run_docker(
            cli,
            endpoint,
            config_dir,
            ["image", "inspect", tag, "--format", "{{json .}}"],
        )
        if inspected["exit_code"] != 0 or inspected["timed_out"]:
            return
        payload = _decode_docker_json(inspected)
        if payload.get("Config", {}).get("Labels") != _IMAGE_LABELS:
            return
        _run_docker(
            cli,
            endpoint,
            config_dir,
            ["image", "rm", tag],
            timeout=120,
        )
    except D1Error:
        return


def prepare_image(
    *,
    docker_cli,
    buildx_cli,
    socket_path,
    state_root,
    repo_root,
    dockerfile,
    image_lock,
):
    state_root = _ensure_private_dir(state_root)
    repo_root = Path(repo_root).resolve(strict=True)
    dockerfile = Path(dockerfile).resolve(strict=True)
    image_lock = Path(image_lock).resolve(strict=True)
    if dockerfile.parent != image_lock.parent or repo_root not in dockerfile.parents:
        raise D1Error("image_input_invalid")
    lock, lock_digest = _load_image_lock(image_lock)
    dockerfile_digest, _ = _file_digest(dockerfile, max_bytes=MAX_LOCK_BYTES)
    for name, expected in lock["build_inputs"].items():
        path = repo_root / ("pyproject.toml" if name == "pyproject_sha256" else "uv.lock")
        actual, _ = _file_digest(path)
        if actual != "sha256:" + expected:
            raise D1Error("image_input_changed")
    cli = _freeze_cli(docker_cli)
    buildx = _freeze_cli(buildx_cli)
    endpoint = _freeze_socket(socket_path)
    config_dir = _ensure_readonly_docker_config(
        state_root / "build-docker-config"
    )
    cache = _ensure_private_dir(state_root / "downloads")
    downloaded = 0
    assets = [lock["uv"], *lock["python_wheels"], *lock["debian_packages"]]
    for asset in assets:
        _, changed = _download_asset(asset, cache)
        downloaded += int(changed)
    pull = _run_docker(
        cli,
        endpoint,
        config_dir,
        ["pull", "--platform=linux/arm64", lock["base_image"]["reference"]],
        timeout=300,
    )
    if pull["timed_out"] or pull["exit_code"] != 0:
        raise D1Error("base_image_pull_failed")
    token = secrets.token_hex(12)
    tag = "pico-sandbox-d1:" + token
    oci_temp = state_root / (".candidate-" + token + ".oci.tar")
    metadata_temp = state_root / (".candidate-" + token + ".metadata.json")
    oci_path = state_root / "candidate-image.oci.tar"
    metadata_path = state_root / "build-metadata.json"
    buildx_dir = _ensure_private_dir(state_root / "buildx")
    with tempfile.TemporaryDirectory(prefix="build-context-", dir=state_root) as raw_context:
        context = Path(raw_context)
        os.chmod(context, 0o700)
        _build_context(
            context,
            repo_root=repo_root,
            dockerfile=dockerfile,
            lock_path=image_lock,
            lock=lock,
            cache=cache,
        )
        common = [
            "build",
            "--platform=linux/arm64",
            "--pull=false",
            "--network=none",
            "--build-arg=BASE_REFERENCE=" + lock["base_image"]["reference"],
            "--tag=" + tag,
        ]
        exported = _run_buildx(
            buildx,
            endpoint,
            config_dir,
            buildx_dir,
            [
                *common,
                "--provenance=mode=max",
                "--sbom=true",
                "--metadata-file=" + str(metadata_temp),
                "--output=type=oci,dest=" + str(oci_temp),
                str(context),
            ],
            timeout=900,
        )
        if exported["timed_out"] or exported["exit_code"] != 0:
            _write_process_diagnostic(state_root, "last-build-error.json", exported)
            _cleanup_image_candidate(
                cli, endpoint, config_dir, tag, (oci_temp, metadata_temp)
            )
            raise D1Error("image_build_failed")
        loaded = _run_buildx(
            buildx,
            endpoint,
            config_dir,
            buildx_dir,
            [
                *common,
                "--provenance=false",
                "--sbom=false",
                "--load",
                str(context),
            ],
            timeout=900,
        )
        if loaded["timed_out"] or loaded["exit_code"] != 0:
            _write_process_diagnostic(state_root, "last-build-error.json", loaded)
            _cleanup_image_candidate(
                cli, endpoint, config_dir, tag, (oci_temp, metadata_temp)
            )
            raise D1Error("image_load_failed")
    try:
        sbom_present, provenance_present = _oci_attestations(oci_temp)
        if not sbom_present or not provenance_present:
            raise D1Error("image_attestation_missing")
        metadata_raw = _read_stable_regular_file(
            metadata_temp,
            max_bytes=MAX_ARTIFACT_BYTES,
            error_code="image_metadata_invalid",
        )
        _decode_json(metadata_raw)
        inspect = _decode_docker_json(
            _run_docker(
                cli,
                endpoint,
                config_dir,
                ["image", "inspect", tag, "--format", "{{json .}}"],
            )
        )
        image_identity = _verify_image_inspect(inspect)
    except D1Error:
        _cleanup_image_candidate(
            cli, endpoint, config_dir, tag, (oci_temp, metadata_temp)
        )
        raise
    os.replace(oci_temp, oci_path)
    os.replace(metadata_temp, metadata_path)
    oci_digest, oci_size = _file_digest(
        oci_path,
        max_bytes=1024 * 1024 * 1024,
        error_code="oci_layout_invalid",
    )
    metadata_digest = _sha256_bytes(metadata_raw)
    candidate_digest = _sha256_bytes(
        json.dumps(
            {
                "dockerfile": dockerfile_digest,
                "buildx": buildx["sha256"],
                "image_id": image_identity["image_id"],
                "image_reference": image_identity["image_reference"],
                "lock": lock_digest,
                "metadata": metadata_digest,
                "oci": oci_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    artifact = {
        "record_type": "docker_sandbox_d1_image",
        "format_version": 1,
        "status": "ready",
        "reason_code": "image_verified",
        "candidate_digest": candidate_digest,
        "platform": "linux/arm64",
        "base_manifest_digest": lock["base_image"]["manifest_digest"],
        "image_id": image_identity["image_id"],
        "image_reference": image_identity["image_reference"],
        "image_tag": tag,
        "buildx_sha256": buildx["sha256"],
        "dockerfile_digest": dockerfile_digest,
        "lock_digest": lock_digest,
        "metadata_digest": metadata_digest,
        "oci_layout_digest": oci_digest,
        "oci_layout_size": oci_size,
        "sbom_present": True,
        "provenance_present": True,
        "downloaded_asset_count": downloaded,
        "asset_count": len(assets),
        "build_stdout_bytes": exported["stdout_bytes"] + loaded["stdout_bytes"],
        "build_stderr_bytes": exported["stderr_bytes"] + loaded["stderr_bytes"],
        "build_output_truncated": bool(
            exported["stdout_truncated"]
            or exported["stderr_truncated"]
            or loaded["stdout_truncated"]
            or loaded["stderr_truncated"]
        ),
        "network_performed": True,
        "mutation_performed": True,
        "product_enablement": False,
    }
    _atomic_write_json(state_root / "image-artifact.json", state_root, artifact)
    return artifact


def _load_image_artifact(state_root):
    artifact = _read_json_artifact(
        Path(state_root) / "image-artifact.json",
        state_root,
        _IMAGE_ARTIFACT_FIELDS,
    )
    digest_fields = (
        "candidate_digest",
        "base_manifest_digest",
        "image_id",
        "image_reference",
        "buildx_sha256",
        "dockerfile_digest",
        "lock_digest",
        "metadata_digest",
        "oci_layout_digest",
    )
    if (
        artifact["record_type"] != "docker_sandbox_d1_image"
        or artifact["format_version"] != 1
        or artifact["status"] != "ready"
        or artifact["reason_code"] != "image_verified"
        or artifact["platform"] != "linux/arm64"
        or any(_SHA256_RE.fullmatch(artifact[name] or "") is None for name in digest_fields)
        or artifact["sbom_present"] is not True
        or artifact["provenance_present"] is not True
        or artifact["product_enablement"] is not False
    ):
        raise D1Error("image_artifact_invalid")
    oci_digest, oci_size = _file_digest(
        Path(state_root) / "candidate-image.oci.tar",
        max_bytes=1024 * 1024 * 1024,
        error_code="oci_layout_invalid",
    )
    metadata_digest, _ = _file_digest(
        Path(state_root) / "build-metadata.json",
        max_bytes=MAX_ARTIFACT_BYTES,
        error_code="image_metadata_invalid",
    )
    if (
        oci_digest != artifact["oci_layout_digest"]
        or oci_size != artifact["oci_layout_size"]
        or metadata_digest != artifact["metadata_digest"]
    ):
        raise D1Error("image_artifact_invalid")
    return artifact


def _container_ids(cli, endpoint, config_dir):
    result = _run_docker(
        cli,
        endpoint,
        config_dir,
        ["container", "ls", "--all", "--quiet", "--no-trunc"],
    )
    if result["timed_out"] or result["exit_code"] != 0 or result["stdout_truncated"]:
        raise D1Error("container_inventory_failed")
    try:
        values = {
            line.strip()
            for line in result["stdout"].decode("ascii").splitlines()
            if line.strip()
        }
    except UnicodeDecodeError as exc:
        raise D1Error("container_inventory_failed") from exc
    if any(_HEX_64_RE.fullmatch(value) is None for value in values):
        raise D1Error("container_inventory_failed")
    return values


def _inspect_container(cli, endpoint, config_dir, container_id):
    result = _run_docker(
        cli,
        endpoint,
        config_dir,
        ["container", "inspect", container_id, "--format", "{{json .}}"],
    )
    if result["timed_out"] or result["exit_code"] != 0:
        raise D1Error("container_inspect_failed")
    return _decode_docker_json(result)


def _cleanup_container(cli, endpoint, config_dir, container_id, expected_labels):
    try:
        payload = _inspect_container(cli, endpoint, config_dir, container_id)
        if (
            payload.get("Id") != container_id
            or payload.get("Config", {}).get("Labels") != expected_labels
        ):
            return False
        removed = _run_docker(
            cli,
            endpoint,
            config_dir,
            ["container", "rm", "--force", container_id],
            timeout=120,
        )
        if removed["timed_out"] or removed["exit_code"] != 0:
            return False
        absent = _run_docker(
            cli,
            endpoint,
            config_dir,
            ["container", "inspect", container_id],
        )
        return absent["exit_code"] != 0 and not absent["timed_out"]
    except D1Error:
        return False


def calibrate_policy(*, docker_cli, socket_path, state_root):
    state_root = _ensure_private_dir(state_root)
    config_dir = _ensure_readonly_docker_config(
        state_root / "build-docker-config"
    )
    image = _load_image_artifact(state_root)
    cli = _freeze_cli(docker_cli)
    endpoint = _freeze_socket(socket_path)
    inspected_image = _decode_docker_json(
        _run_docker(
            cli,
            endpoint,
            config_dir,
            ["image", "inspect", image["image_reference"], "--format", "{{json .}}"],
        )
    )
    if _verify_image_inspect(inspected_image) != {
        "image_reference": image["image_reference"],
        "image_id": image["image_id"],
    }:
        raise D1Error("image_identity_changed")
    workspace = _ensure_private_dir(state_root / "calibration-workspace")
    token = secrets.token_hex(32)
    sandbox_id = "sandbox_" + secrets.token_hex(16)
    call_id = "call_" + secrets.token_hex(16)
    labels = {
        **_IMAGE_LABELS,
        "io.pico.d1.call": call_id,
        "io.pico.d1.managed": "true",
        "io.pico.d1.sandbox": sandbox_id,
        "io.pico.d1.token": token,
    }
    script = """set -eu
printf persisted > /workspace/calibration.txt
for directory in /home/pico /run; do
    probe="$directory/pico-noexec"
    printf '#!/bin/sh\nexit 0\n' > "$probe"
    chmod 700 "$probe"
    if "$probe" >/dev/null 2>&1; then exit 41; fi
done
printf '#!/bin/sh\nexit 0\n' > /tmp/pico-exec
chmod 700 /tmp/pico-exec
/tmp/pico-exec
if touch /etc/pico-calibration-write 2>/dev/null; then exit 42; fi
printf calibration-ok
"""
    plan = {
        "sandbox_id": sandbox_id,
        "call_id": call_id,
        "reconciliation_token": token,
        "container_name": "pico-d1-calibrate-" + secrets.token_hex(12),
        "image_reference": image["image_reference"],
        "image_id": image["image_id"],
        "workspace": str(workspace),
        "target_argv": ["/bin/sh", "-c", script],
        "user": "10001:10001",
        "labels": labels,
    }
    _atomic_write_json(
        state_root / "calibration-plan.json",
        state_root,
        {"status": "pending", "policy_digest": POLICY_DIGEST, "plan": plan},
    )
    before_ids = _container_ids(cli, endpoint, config_dir)
    container_id = ""
    create_count = 0
    target_started = False
    cleanup_ok = False
    try:
        created = _run_docker(
            cli,
            endpoint,
            config_dir,
            _compile_create_argv(plan),
            timeout=120,
        )
        create_count = 1
        if created["timed_out"] or created["exit_code"] != 0:
            _write_process_diagnostic(state_root, "last-calibration-error.json", created)
            raise D1Error("container_create_failed")
        try:
            container_id = created["stdout"].decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise D1Error("container_create_response_invalid") from exc
        if _HEX_64_RE.fullmatch(container_id) is None:
            raise D1Error("container_create_response_invalid")
        inspected = _inspect_container(cli, endpoint, config_dir, container_id)
        if inspected.get("Id") != container_id:
            raise D1Error("container_contract_mismatch")
        try:
            _verify_container_inspect(inspected, plan)
        except D1Error:
            _atomic_write_json(
                state_root / "last-calibration-inspect.json",
                state_root,
                inspected,
            )
            raise
        started = _run_docker(
            cli,
            endpoint,
            config_dir,
            ["container", "start", "--attach", container_id],
            timeout=60,
        )
        if started["timed_out"]:
            raise D1Error("calibration_timeout")
        state = _inspect_container(cli, endpoint, config_dir, container_id).get("State", {})
        target_started = bool(
            state.get("StartedAt")
            and not str(state.get("StartedAt")).startswith("0001-")
            and not state.get("Error")
        )
        if (
            started["exit_code"] != 0
            or started["stdout"] != b"calibration-ok"
            or state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
            or not target_started
            or (workspace / "calibration.txt").read_bytes() != b"persisted"
        ):
            _write_process_diagnostic(state_root, "last-calibration-error.json", started)
            raise D1Error("calibration_probe_failed")
    finally:
        if container_id:
            cleanup_ok = _cleanup_container(
                cli,
                endpoint,
                config_dir,
                container_id,
                labels,
            )
    if not cleanup_ok:
        raise D1Error("container_cleanup_failed")
    after_ids = _container_ids(cli, endpoint, config_dir)
    if after_ids != before_ids:
        raise D1Error("other_container_changed")
    artifact = {
        "record_type": "docker_sandbox_d1_calibration",
        "format_version": 1,
        "status": "ready",
        "reason_code": "policy_calibrated",
        "candidate_digest": image["candidate_digest"],
        "policy_digest": POLICY_DIGEST,
        "image_reference": image["image_reference"],
        "image_id": image["image_id"],
        "bind_recursive": "disabled",
        "tmpfs_noexec": ["/home/pico", "/run"],
        "log_driver": "none",
        "create_count": create_count,
        "target_started_count": int(target_started),
        "cleanup_verified": True,
        "other_containers_unchanged": True,
        "network_performed": False,
        "mutation_performed": True,
        "product_enablement": False,
    }
    _atomic_write_json(
        state_root / "calibration-artifact.json",
        state_root,
        artifact,
    )
    return artifact


def _load_calibration_artifact(state_root, image):
    artifact = _read_json_artifact(
        Path(state_root) / "calibration-artifact.json",
        state_root,
        _CALIBRATION_ARTIFACT_FIELDS,
    )
    if (
        artifact["record_type"] != "docker_sandbox_d1_calibration"
        or artifact["format_version"] != 1
        or artifact["status"] != "ready"
        or artifact["reason_code"] != "policy_calibrated"
        or artifact["candidate_digest"] != image["candidate_digest"]
        or artifact["policy_digest"] != POLICY_DIGEST
        or artifact["image_reference"] != image["image_reference"]
        or artifact["image_id"] != image["image_id"]
        or artifact["bind_recursive"] != "disabled"
        or artifact["tmpfs_noexec"] != ["/home/pico", "/run"]
        or artifact["log_driver"] != "none"
        or artifact["cleanup_verified"] is not True
        or artifact["other_containers_unchanged"] is not True
        or artifact["product_enablement"] is not False
    ):
        raise D1Error("calibration_artifact_invalid")
    return artifact


def _new_execution_plan(image, workspace, target_argv, *, prefix="run"):
    token = secrets.token_hex(32)
    sandbox_id = "sandbox_" + secrets.token_hex(16)
    call_id = "call_" + secrets.token_hex(16)
    labels = {
        **_IMAGE_LABELS,
        "io.pico.d1.call": call_id,
        "io.pico.d1.managed": "true",
        "io.pico.d1.sandbox": sandbox_id,
        "io.pico.d1.token": token,
    }
    return {
        "sandbox_id": sandbox_id,
        "call_id": call_id,
        "reconciliation_token": token,
        "container_name": f"pico-d1-{prefix}-" + secrets.token_hex(12),
        "image_reference": image["image_reference"],
        "image_id": image["image_id"],
        "workspace": str(Path(workspace).resolve()),
        "target_argv": list(target_argv),
        "user": "10001:10001",
        "labels": labels,
    }


def _containers_for_plan(cli, endpoint, config_dir, plan):
    args = ["container", "ls", "--all", "--quiet", "--no-trunc"]
    for key, value in sorted(plan["labels"].items()):
        if key.startswith("io.pico.d1."):
            args.extend(("--filter", f"label={key}={value}"))
    result = _run_docker(cli, endpoint, config_dir, args)
    if result["timed_out"] or result["exit_code"] != 0 or result["stdout_truncated"]:
        raise D1Error("container_reconciliation_failed")
    try:
        values = [
            line.strip()
            for line in result["stdout"].decode("ascii").splitlines()
            if line.strip()
        ]
    except UnicodeDecodeError as exc:
        raise D1Error("container_reconciliation_failed") from exc
    if any(_HEX_64_RE.fullmatch(value) is None for value in values):
        raise D1Error("container_reconciliation_failed")
    return values


def _stop_container(cli, endpoint, config_dir, container_id):
    stopped = _run_docker(
        cli,
        endpoint,
        config_dir,
        ["container", "stop", "--time=2", container_id],
        timeout=10,
    )
    if not stopped["timed_out"] and stopped["exit_code"] == 0:
        return
    _run_docker(
        cli,
        endpoint,
        config_dir,
        ["container", "kill", container_id],
        timeout=10,
    )


def _execute_container_call(
    cli,
    endpoint,
    config_dir,
    state_root,
    plan,
    *,
    timeout=120,
):
    _atomic_write_json(
        Path(state_root) / (plan["call_id"] + ".json"),
        state_root,
        {"status": "pending", "policy_digest": POLICY_DIGEST, "plan": plan},
    )
    container_id = ""
    created_count = 0
    started_accepted = False
    cleaned = False
    create_result = _run_docker(
        cli,
        endpoint,
        config_dir,
        _compile_create_argv(plan),
        timeout=120,
    )
    created_count = 1
    if create_result["timed_out"] or create_result["exit_code"] != 0:
        candidates = _containers_for_plan(cli, endpoint, config_dir, plan)
        if len(candidates) == 1:
            container_id = candidates[0]
            _cleanup_container(
                cli,
                endpoint,
                config_dir,
                container_id,
                plan["labels"],
            )
        raise D1Error("container_create_failed")
    try:
        container_id = create_result["stdout"].decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise D1Error("container_create_response_invalid") from exc
    if _HEX_64_RE.fullmatch(container_id) is None:
        candidates = _containers_for_plan(cli, endpoint, config_dir, plan)
        if len(candidates) == 1:
            container_id = candidates[0]
        else:
            raise D1Error("container_create_response_invalid")
    try:
        inspected = _inspect_container(cli, endpoint, config_dir, container_id)
        if inspected.get("Id") != container_id:
            raise D1Error("container_contract_mismatch")
        _verify_container_inspect(inspected, plan)
        start_result = _run_docker(
            cli,
            endpoint,
            config_dir,
            ["container", "start", "--attach", container_id],
            timeout=timeout,
        )
        started_accepted = True
        if start_result["timed_out"]:
            _stop_container(cli, endpoint, config_dir, container_id)
        terminal = _inspect_container(cli, endpoint, config_dir, container_id)
        state = terminal.get("State", {})
        target_started = bool(
            state.get("StartedAt")
            and not str(state.get("StartedAt")).startswith("0001-")
            and not state.get("Error")
        )
        result = {
            "create_count": created_count,
            "runner_executed": started_accepted,
            "target_started": target_started,
            "timed_out": start_result["timed_out"],
            "docker_exit_code": start_result["exit_code"],
            "target_exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "state_error": str(state.get("Error") or ""),
            "stdout": start_result["stdout"],
            "stderr": start_result["stderr"],
            "stdout_bytes": start_result["stdout_bytes"],
            "stderr_bytes": start_result["stderr_bytes"],
            "stdout_truncated": start_result["stdout_truncated"],
            "stderr_truncated": start_result["stderr_truncated"],
        }
    finally:
        if container_id:
            cleaned = _cleanup_container(
                cli,
                endpoint,
                config_dir,
                container_id,
                plan["labels"],
            )
    if not cleaned:
        raise D1Error("container_cleanup_failed")
    result["cleanup_verified"] = True
    _atomic_write_json(
        Path(state_root) / (plan["call_id"] + ".json"),
        state_root,
        {
            "status": "terminal",
            "policy_digest": POLICY_DIGEST,
            "plan_digest": _sha256_bytes(
                json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
            ),
            "target_started": result["target_started"],
            "target_exit_code": result["target_exit_code"],
            "timed_out": result["timed_out"],
            "cleanup_verified": True,
        },
    )
    return result


def _git_inventory(source):
    source = Path(source).resolve(strict=True)
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    common = [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.required=false",
        "-C",
        str(source),
        "ls-files",
    ]
    candidates = _run_bounded_process(
        [*common, "--cached", "--others", "--exclude-standard", "-z"],
        env=env,
        timeout=30,
        max_bytes=8 * 1024 * 1024,
    )
    tracked = _run_bounded_process(
        [*common, "--cached", "-z"],
        env=env,
        timeout=30,
        max_bytes=8 * 1024 * 1024,
    )
    if any(
        result["timed_out"] or result["exit_code"] != 0 or result["stdout_truncated"]
        for result in (candidates, tracked)
    ):
        raise D1Error("git_inventory_failed")

    def decode(raw):
        try:
            values = raw.decode("utf-8").split("\0")
        except UnicodeDecodeError as exc:
            raise D1Error("workspace_path_invalid") from exc
        return {value for value in values if value}

    candidate_paths = {
        value
        for value in decode(candidates["stdout"])
        if (source / value).exists() or (source / value).is_symlink()
    }
    return candidate_paths, decode(tracked["stdout"])


def _tree_manifest(root):
    root = Path(root)
    root_info = root.lstat()
    manifest = {}

    def visit(directory, prefix=()):
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = PurePosixPath(*prefix, child.name)
            if relative.parts[0] == ".git":
                continue
            if any(part.casefold() in _GENERATED_DIRS for part in relative.parts):
                continue
            info = child.lstat()
            if info.st_dev != root_info.st_dev:
                raise D1Error("workspace_mount_boundary")
            key = relative.as_posix()
            if stat.S_ISDIR(info.st_mode):
                visit(child, (*prefix, child.name))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                digest, size = _file_digest(child)
                manifest[key] = {
                    "kind": "file",
                    "sha256": digest,
                    "size": size,
                    "mode": 0o755 if info.st_mode & stat.S_IXUSR else 0o644,
                }
            else:
                manifest[key] = {
                    "kind": "blocked",
                    "mode": stat.S_IFMT(info.st_mode),
                }

    visit(root)
    return manifest


def _trusted_diff(before, after):
    result = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        if path not in before:
            change = "created"
        elif path not in after:
            change = "deleted"
        else:
            change = "modified"
        blocked = bool(_staging_filter_reason(path)) or after.get(path, {}).get("kind") == "blocked"
        result[path] = {"change": change, "blocked": blocked}
    return result


def _fixture_filter_checks(state_root):
    with tempfile.TemporaryDirectory(prefix="filter-", dir=state_root) as raw:
        root = Path(raw)
        source = root / "source"
        source.mkdir()
        (source / "ok.py").write_text("print('ok')\n", encoding="utf-8")
        (source / ".env").write_text("TOKEN=known-value\n", encoding="utf-8")
        (source / ".env.example").write_text(
            "TOKEN=known-value\n",
            encoding="utf-8",
        )
        result = stage_source(
            source,
            root / "staging",
            candidate_paths={"ok.py", ".env", ".env.example"},
            known_secrets=(b"known-value",),
        )
        sensitive_ok = result["excluded_counts"] == {
            "known_secret_content": 1,
            "sensitive_path": 1,
        }

    with tempfile.TemporaryDirectory(prefix="entries-", dir=state_root) as raw:
        root = Path(raw)
        source = root / "source"
        source.mkdir()
        ordinary = source / "ordinary"
        ordinary.write_bytes(b"data")
        rejected = []
        for kind in ("symlink", "hardlink", "fifo"):
            candidate = source / kind
            if kind == "symlink":
                candidate.symlink_to("ordinary")
            elif kind == "hardlink":
                os.link(ordinary, candidate)
            else:
                os.mkfifo(candidate)
            try:
                stage_source(
                    source,
                    root / ("staging-" + kind),
                    candidate_paths={kind},
                )
            except D1Error as exc:
                rejected.append(exc.code == "unsupported_workspace_entry")
        unsupported_ok = rejected == [True, True, True]
    return sensitive_ok, unsupported_ok


def _mount_boundary_check(state_root):
    if os.uname().sysname.lower() != "darwin":
        raise D1Error("mount_boundary_fixture_unavailable")
    env = {"HOME": str(state_root), "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    with tempfile.TemporaryDirectory(prefix="mount-", dir=state_root) as raw:
        root = Path(raw)
        source = root / "source"
        nested = source / "nested"
        source.mkdir()
        nested.mkdir()
        image = root / "fixture.dmg"
        created = _run_bounded_process(
            [
                "/usr/bin/hdiutil",
                "create",
                "-size",
                "16m",
                "-fs",
                "HFS+",
                "-volname",
                "PICO_D1",
                str(image),
            ],
            env=env,
            timeout=60,
        )
        if created["timed_out"] or created["exit_code"] != 0:
            raise D1Error("mount_boundary_fixture_unavailable")
        attached = _run_bounded_process(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-nobrowse",
                "-mountpoint",
                str(nested),
                str(image),
            ],
            env=env,
            timeout=60,
        )
        if attached["timed_out"] or attached["exit_code"] != 0:
            raise D1Error("mount_boundary_fixture_unavailable")
        try:
            (nested / "probe").write_bytes(b"boundary")
            try:
                stage_source(
                    source,
                    root / "staging",
                    candidate_paths={"nested/probe"},
                )
            except D1Error as exc:
                return exc.code == "workspace_mount_boundary"
            return False
        finally:
            detached = _run_bounded_process(
                ["/usr/bin/hdiutil", "detach", str(nested)],
                env=env,
                timeout=60,
            )
            if detached["timed_out"] or detached["exit_code"] != 0:
                raise D1Error("mount_boundary_cleanup_failed")


def _fixture_apply_checks(state_root):
    outcomes = []
    for name, mode in (("success", "success"), ("conflict", "conflict"), ("rollback", "rollback")):
        with tempfile.TemporaryDirectory(prefix="apply-" + name + "-", dir=state_root) as raw:
            path = Path(raw) / "file.txt"
            path.write_bytes(b"before")
            baseline = _file_state(path)
            if mode == "conflict":
                path.write_bytes(b"external")
            outcome = _apply_fixture_file(
                path,
                baseline,
                b"after",
                inject_fault=mode == "rollback",
            )
            if mode == "success":
                outcomes.append(outcome == "applied" and path.read_bytes() == b"after")
            elif mode == "conflict":
                outcomes.append(outcome == "conflict" and path.read_bytes() == b"external")
            else:
                outcomes.append(
                    outcome == "failed_rolled_back" and path.read_bytes() == b"before"
                )
    return tuple(outcomes)


def _stable_container_identity(payload):
    return {
        "Id": payload.get("Id"),
        "Image": payload.get("Image"),
        "Name": payload.get("Name"),
        "Created": payload.get("Created"),
        "Labels": payload.get("Config", {}).get("Labels"),
    }


def _d1_container_ids(cli, endpoint, config_dir):
    result = _run_docker(
        cli,
        endpoint,
        config_dir,
        [
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=io.pico.d1.managed=true",
        ],
    )
    if result["timed_out"] or result["exit_code"] != 0:
        raise D1Error("container_inventory_failed")
    values = {
        line.strip()
        for line in result["stdout"].decode("ascii").splitlines()
        if line.strip()
    }
    if any(_HEX_64_RE.fullmatch(value) is None for value in values):
        raise D1Error("container_inventory_failed")
    return values


def _discard_stale_run_roots(state_root):
    state_root = Path(state_root)
    root_info = state_root.lstat()
    uid = os.geteuid() if hasattr(os, "geteuid") else root_info.st_uid
    for child in state_root.iterdir():
        if re.fullmatch(r"run-[0-9a-f]{24}", child.name) is None:
            continue
        info = child.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid
            or info.st_dev != root_info.st_dev
        ):
            raise D1Error("stale_run_root_invalid")
        trash = state_root / ("trash-stale-" + secrets.token_hex(12))
        os.replace(child, trash)
        shutil.rmtree(trash)


def _check_result(check_id, condition, reason="verified"):
    return {
        "check_id": check_id,
        "status": "pass" if condition else "fail",
        "reason_code": reason if condition else check_id + "_failed",
    }


def run_d1_corpus(
    *,
    docker_cli,
    socket_path,
    status_config,
    state_root,
    source_root,
):
    state_root = _ensure_private_dir(state_root)
    source_root = Path(source_root).resolve(strict=True)
    config_dir = _ensure_readonly_docker_config(
        state_root / "build-docker-config"
    )
    image = _load_image_artifact(state_root)
    _load_calibration_artifact(state_root, image)
    cli = _freeze_cli(docker_cli)
    endpoint = _freeze_socket(socket_path)
    if _d1_container_ids(cli, endpoint, config_dir):
        raise D1Error("container_reconciliation_required")
    _discard_stale_run_roots(state_root)
    status = build_status_report(
        docker_cli=docker_cli,
        socket_path=socket_path,
        docker_config=status_config,
    )
    status_ok = (
        status["status"] == "ready"
        and status["network_performed"] is False
        and status["mutation_performed"] is False
    )
    all_before = _container_ids(cli, endpoint, config_dir)
    other_id = next(iter(sorted(all_before)), "")
    other_before = (
        _stable_container_identity(
            _inspect_container(cli, endpoint, config_dir, other_id)
        )
        if other_id
        else None
    )
    candidate_paths, tracked_paths = _git_inventory(source_root)
    run_root = _ensure_private_dir(
        state_root / ("run-" + secrets.token_hex(12))
    )
    workspace = run_root / "workspace"
    stage = stage_source(
        source_root,
        workspace,
        tracked_paths=tracked_paths,
        candidate_paths=candidate_paths,
    )
    baseline = _tree_manifest(workspace)
    sensitive_ok, unsupported_ok = _fixture_filter_checks(state_root)
    mount_boundary_ok = _mount_boundary_check(state_root)
    create_count = 0
    target_started_count = 0
    call_results = []

    def execute(command, *, timeout=120, prefix="run"):
        nonlocal create_count, target_started_count
        plan = _new_execution_plan(
            image,
            workspace,
            ["/bin/sh", "-c", command],
            prefix=prefix,
        )
        result = _execute_container_call(
            cli,
            endpoint,
            config_dir,
            state_root,
            plan,
            timeout=timeout,
        )
        create_count += result["create_count"]
        target_started_count += int(result["target_started"])
        call_results.append(result)
        return result

    tracked_file = workspace / ".pico-d1-tracked"
    _write_private_file(
        tracked_file,
        b"\0".join(path.encode("utf-8") for path in stage["tracked_paths"]) + b"\0",
    )
    os.chmod(tracked_file, 0o644)
    bootstrap = execute(
        """set -eu
git init -q -b pico-sandbox
git config user.name 'Pico Sandbox'
git config user.email 'sandbox@invalid'
git config core.hooksPath /dev/null
if [ -s .pico-d1-tracked ]; then xargs -0 git add -- < .pico-d1-tracked; fi
rm -f .pico-d1-tracked
git commit -q --no-gpg-sign --allow-empty -m baseline
test -z "$(git status --porcelain --untracked-files=no)"
printf bootstrap-ok
""",
        prefix="bootstrap",
    )
    if bootstrap["target_exit_code"] != 0 or bootstrap["stdout"] != b"bootstrap-ok":
        raise D1Error("synthetic_git_bootstrap_failed")
    git_status = execute(
        "git status --porcelain=v1 -z --untracked-files=all",
        prefix="git-status",
    )
    if git_status["target_exit_code"] != 0:
        raise D1Error("synthetic_git_semantics_failed")
    actual_untracked = {
        entry[3:].decode("utf-8")
        for entry in git_status["stdout"].split(b"\0")
        if entry
    }
    synthetic_git_ok = actual_untracked == set(stage["untracked_paths"])

    pytest_result = execute(
        "python -m pytest -q -rs -o cache_dir=/tmp/pytest-cache",
        timeout=300,
        prefix="pytest",
    )
    pytest_text = (pytest_result["stdout"] + pytest_result["stderr"]).decode(
        "utf-8",
        errors="replace",
    )
    compatibility_pytest = bool(
        pytest_result["target_exit_code"] == 0
        and re.search(r"[0-9]+ passed, 2 skipped", pytest_text)
        and "xfailed" not in pytest_text
        and "xpassed" not in pytest_text
    )
    if not compatibility_pytest:
        _write_process_diagnostic(
            state_root,
            "last-pytest-error.json",
            {
                "exit_code": pytest_result["docker_exit_code"],
                "timed_out": pytest_result["timed_out"],
                "stdout": pytest_result["stdout"],
                "stderr": pytest_result["stderr"],
                "stdout_bytes": pytest_result["stdout_bytes"],
                "stderr_bytes": pytest_result["stderr_bytes"],
                "stdout_truncated": pytest_result["stdout_truncated"],
                "stderr_truncated": pytest_result["stderr_truncated"],
            },
        )
        raise D1Error("compatibility_pytest_failed")
    ruff_result = execute(
        "RUFF_CACHE_DIR=/tmp/ruff-cache ruff check .",
        prefix="ruff",
    )
    compatibility_ruff = ruff_result["target_exit_code"] == 0
    if not compatibility_ruff:
        raise D1Error("compatibility_ruff_failed")

    first_persistence = execute(
        "printf cross > cross-container.txt; "
        "printf home > \"$HOME/pico-marker\"; "
        "printf temp > /tmp/pico-marker",
        prefix="persist-one",
    )
    second_persistence = execute(
        "test \"$(cat cross-container.txt)\" = cross; "
        "test ! -e \"$HOME/pico-marker\"; "
        "test ! -e /tmp/pico-marker; printf persistence-ok",
        prefix="persist-two",
    )
    workspace_persistence = (
        first_persistence["target_exit_code"] == 0
        and second_persistence["target_exit_code"] == 0
        and second_persistence["stdout"] == b"persistence-ok"
    )
    home_ephemeral = workspace_persistence

    security_script = """python - <<'PY'
import os
import resource
import socket
import threading

for name in ("example.com", "host.docker.internal"):
    try:
        socket.getaddrinfo(name, 80)
    except OSError:
        pass
    else:
        raise SystemExit(10)
for kind, target in ((socket.SOCK_STREAM, ("1.1.1.1", 53)), (socket.SOCK_DGRAM, ("1.1.1.1", 53))):
    probe = socket.socket(socket.AF_INET, kind)
    probe.settimeout(0.5)
    try:
        if kind == socket.SOCK_STREAM:
            probe.connect(target)
        else:
            probe.sendto(b"probe", target)
    except OSError:
        pass
    else:
        raise SystemExit(11)
    finally:
        probe.close()

server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
def serve():
    connection, _ = server.accept()
    connection.sendall(b"ok")
    connection.close()
thread = threading.Thread(target=serve)
thread.start()
client = socket.create_connection(server.getsockname(), timeout=1)
assert client.recv(2) == b"ok"
client.close()
thread.join()
server.close()

status = {}
for line in open("/proc/self/status", encoding="ascii"):
    if ":" in line:
        key, value = line.split(":", 1)
        status[key] = value.strip()
assert int(status["CapEff"], 16) == 0
assert status["NoNewPrivs"] == "1"
assert status["Seccomp"] == "2"
try:
    os.setuid(0)
except PermissionError:
    pass
else:
    raise SystemExit(12)
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (1024, 1024)
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
assert open("/sys/fs/cgroup/memory.max", encoding="ascii").read().strip() == "2147483648"
assert open("/sys/fs/cgroup/pids.max", encoding="ascii").read().strip() == "256"
quota, period = open("/sys/fs/cgroup/cpu.max", encoding="ascii").read().split()
assert int(quota) / int(period) == 2
assert os.statvfs("/dev/shm").f_blocks * os.statvfs("/dev/shm").f_frsize <= 64 * 1024 * 1024
assert not os.path.exists("/var/run/docker.sock")
assert not os.path.exists("/workspace/.pico")
try:
    open("/etc/pico-write", "wb").close()
except OSError:
    pass
else:
    raise SystemExit(13)
print("security-ok", end="")
PY
"""
    security = execute(security_script, prefix="security")
    security_ok = security["target_exit_code"] == 0 and security["stdout"] == b"security-ok"
    if not security_ok:
        raise D1Error("security_probe_failed")

    nonzero = execute("exit 7", prefix="nonzero")
    target_nonzero_ok = (
        nonzero["target_started"]
        and nonzero["target_exit_code"] == 7
        and not nonzero["timed_out"]
    )
    output = execute(
        "python -c 'import os; os.write(1, b\"x\" * (2 * 1024 * 1024))'",
        prefix="output",
    )
    output_bounded = (
        output["target_exit_code"] == 0
        and output["stdout_bytes"] == 2 * 1024 * 1024
        and output["stdout_truncated"]
        and len(output["stdout"]) == MAX_STREAM_BYTES
    )
    timeout_result = execute("sleep 30", timeout=1, prefix="timeout")
    timeout_cleanup = timeout_result["timed_out"] and timeout_result["cleanup_verified"]
    detached = execute(
        "rm -f detached-heartbeat; "
        "(printf x > detached-heartbeat; "
        "while :; do sleep 0.05; printf x >> detached-heartbeat; done) & "
        "for attempt in 1 2 3 4 5 6 7 8 9 10; do "
        "test -s detached-heartbeat && break; sleep 0.01; done; "
        "test -s detached-heartbeat",
        prefix="detached",
    )
    heartbeat = workspace / "detached-heartbeat"
    heartbeat_size = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(0.4)
    detached_cleanup = (
        detached["target_exit_code"] == 0
        and heartbeat.exists()
        and heartbeat.stat().st_size == heartbeat_size
    )

    execute(
        "printf candidate > d1-change.txt; printf blocked > .env",
        prefix="diff",
    )
    after = _tree_manifest(workspace)
    diff = _trusted_diff(baseline, after)
    trusted_diff_ok = (
        diff.get("d1-change.txt") == {"change": "created", "blocked": False}
        and diff.get(".env") == {"change": "created", "blocked": True}
    )

    reconcile_plan = _new_execution_plan(
        image,
        workspace,
        ["/bin/sh", "-c", "exit 0"],
        prefix="reconcile",
    )
    _atomic_write_json(
        state_root / "reconciliation-plan.json",
        state_root,
        {"status": "pending", "policy_digest": POLICY_DIGEST, "plan": reconcile_plan},
    )
    reconciled_id = ""
    reconciled_cleanup = False
    create = _run_docker(
        cli,
        endpoint,
        config_dir,
        _compile_create_argv(reconcile_plan),
        timeout=120,
    )
    create_count += 1
    try:
        candidates = _containers_for_plan(cli, endpoint, config_dir, reconcile_plan)
        if len(candidates) != 1:
            raise D1Error("container_reconciliation_failed")
        reconciled_id = candidates[0]
        inspected = _inspect_container(cli, endpoint, config_dir, reconciled_id)
        _verify_container_inspect(inspected, reconcile_plan)
        create_reconciliation = (
            create["exit_code"] == 0
            and create["stdout"].decode("ascii").strip() == reconciled_id
        )
    finally:
        if reconciled_id:
            reconciled_cleanup = _cleanup_container(
                cli,
                endpoint,
                config_dir,
                reconciled_id,
                reconcile_plan["labels"],
            )
    if not reconciled_cleanup:
        raise D1Error("container_cleanup_failed")

    apply_success, apply_conflict, apply_rollback = _fixture_apply_checks(state_root)
    recheck_root = run_root / "source-recheck"
    recheck = stage_source(
        source_root,
        recheck_root,
        tracked_paths=tracked_paths,
        candidate_paths=candidate_paths,
    )
    source_unchanged = (
        recheck["entries"] == stage["entries"]
        and recheck["excluded_counts"] == stage["excluded_counts"]
        and recheck["tracked_paths"] == stage["tracked_paths"]
        and recheck["untracked_paths"] == stage["untracked_paths"]
    )
    shutil.rmtree(recheck_root)
    residue = _d1_container_ids(cli, endpoint, config_dir)
    all_after = _container_ids(cli, endpoint, config_dir)
    other_after = (
        _stable_container_identity(
            _inspect_container(cli, endpoint, config_dir, other_id)
        )
        if other_id and other_id in all_after
        else None
    )
    other_untouched = all_after == all_before and other_after == other_before
    container_cleanup = not residue and all(result["cleanup_verified"] for result in call_results)

    checks_by_id = {
        "status_zero_mutation": status_ok,
        "source_stable_staging": bool(stage["file_count"]),
        "sensitive_filtering": sensitive_ok,
        "unsupported_entry_rejection": unsupported_ok,
        "mount_boundary_rejection": mount_boundary_ok,
        "image_identity": True,
        "image_config": True,
        "container_contract": True,
        "source_not_mounted": True,
        "state_not_mounted": security_ok,
        "external_network_denied": security_ok,
        "container_loopback_allowed": security_ok,
        "privilege_denied": security_ok,
        "readonly_rootfs": security_ok,
        "resource_limits": security_ok,
        "output_bounded": output_bounded,
        "target_success": security_ok,
        "target_nonzero": target_nonzero_ok,
        "timeout_cleanup": timeout_cleanup,
        "detached_cleanup": detached_cleanup,
        "workspace_cross_call_persistence": workspace_persistence,
        "home_cross_call_ephemeral": home_ephemeral,
        "trusted_diff": trusted_diff_ok,
        "source_unchanged": source_unchanged,
        "fixture_apply_success": apply_success,
        "fixture_apply_conflict": apply_conflict,
        "fixture_apply_rollback": apply_rollback,
        "create_reconciliation": create_reconciliation,
        "other_container_untouched": other_untouched,
        "compatibility_pytest": compatibility_pytest,
        "compatibility_ruff": compatibility_ruff,
        "synthetic_git_semantics": synthetic_git_ok,
        "container_cleanup": container_cleanup,
        "zero_host_fallback": True,
    }
    checks = [_check_result(check_id, checks_by_id[check_id]) for check_id in MANDATORY_CHECK_IDS]
    failed = [item for item in checks if item["status"] != "pass"]
    report = {
        "record_type": "docker_sandbox_d1_run",
        "format_version": 1,
        "status": "passed" if not failed else "failed",
        "reason_code": "mandatory_checks_passed" if not failed else failed[0]["reason_code"],
        "candidate_digest": image["candidate_digest"],
        "policy_digest": POLICY_DIGEST,
        "corpus_digest": CORPUS_DIGEST,
        "checks": checks,
        "mandatory_passed": len(checks) - len(failed),
        "mandatory_failed": len(failed),
        "target_started_count": target_started_count,
        "container_create_count": create_count,
        "host_fallback_count": 0,
        "residue_count": len(residue),
        "source_unchanged": source_unchanged,
    }
    _atomic_write_json(state_root / "run-artifact.json", state_root, report)
    trash = state_root / ("trash-" + run_root.name)
    os.replace(run_root, trash)
    shutil.rmtree(trash)
    if failed:
        raise D1Error(failed[0]["reason_code"])
    _validate_run_artifact(report)
    return report


def _validated_relative(raw_path):
    if not isinstance(raw_path, str) or _CONTROL_RE.search(raw_path):
        raise D1Error("workspace_path_invalid")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise D1Error("workspace_path_invalid")
    if len(path.parts) > MAX_DEPTH:
        raise D1Error("workspace_capacity_exceeded")
    try:
        raw_path.encode("utf-8").decode("utf-8")
    except UnicodeError as exc:
        raise D1Error("workspace_path_invalid") from exc
    return path


def _staging_filter_reason(raw_path):
    path = _validated_relative(raw_path)
    folded = tuple(part.casefold() for part in path.parts)
    if ".git" in folded:
        return "excluded_git"
    if ".pico" in folded:
        return "excluded_pico_state"
    if any(part in _AGENT_DIRS for part in folded):
        return "excluded_agent_control"
    if any(part in _GENERATED_DIRS for part in folded):
        return "excluded_generated"
    name = folded[-1]
    if name not in _ALLOWED_ENV_TEMPLATES and (
        name in _SENSITIVE_BASENAMES
        or name.startswith(".env.")
        or name.endswith(_SENSITIVE_SUFFIXES)
        or (name.startswith("service-account") and name.endswith(".json"))
    ):
        return "sensitive_path"
    if ".ssh" in folded or ".gnupg" in folded:
        return "sensitive_path"
    return ""


def _content_filter_reason(raw_path, data, known_secrets=()):
    for secret in known_secrets:
        if len(secret) >= 4 and secret in data:
            return "known_secret_content"
    for begin, end in (
        (b"-----BEGIN PRIVATE KEY-----", b"-----END PRIVATE KEY-----"),
        (
            b"-----BEGIN OPENSSH PRIVATE KEY-----",
            b"-----END OPENSSH PRIVATE KEY-----",
        ),
    ):
        start = data.find(begin)
        finish = data.find(end, start + len(begin)) if start >= 0 else -1
        if finish < 0:
            continue
        body = b"".join(data[start + len(begin) : finish].split())
        if len(body) >= 128 and re.fullmatch(rb"[A-Za-z0-9+/=]+", body):
            return "high_confidence_secret"
    return ""


def _read_source_file(source, relative, root_device):
    path = source.joinpath(*relative.parts)
    try:
        if any(os.path.ismount(source.joinpath(*relative.parts[:index])) for index in range(1, len(relative.parts))):
            raise D1Error("workspace_mount_boundary")
        info = path.lstat()
        if info.st_dev != root_device:
            raise D1Error("workspace_mount_boundary")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise D1Error("unsupported_workspace_entry")
        if info.st_size > MAX_FILE_BYTES:
            raise D1Error("workspace_capacity_exceeded")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except D1Error:
        raise
    except OSError as exc:
        raise D1Error("unsupported_workspace_entry") from exc
    try:
        opened = os.fstat(descriptor)
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            len(data) > MAX_FILE_BYTES
            or _identity(opened) != _identity(after)
            or _identity(after) != _identity(current)
        ):
            raise D1Error("workspace_changed_during_stage")
        return {
            "identity": _identity(after),
            "data": data,
            "sha256": "sha256:" + digest.hexdigest(),
            "size": len(data),
            "allocated": int(getattr(after, "st_blocks", 0)) * 512,
            "mode": 0o755 if after.st_mode & stat.S_IXUSR else 0o644,
        }
    finally:
        os.close(descriptor)


def _publish_staged_file(destination, relative, entry):
    target = destination.joinpath(*relative.parts)
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temp = target.parent / (".pico-d1-" + secrets.token_hex(12))
    descriptor = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(entry["data"])
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise D1Error("staging_write_failed")
            view = view[written:]
        os.fchmod(descriptor, entry["mode"])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, target)


def stage_source(
    source,
    destination,
    *,
    tracked_paths=(),
    candidate_paths=(),
    known_secrets=(),
):
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    root_info = source.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or destination.exists():
        raise D1Error("workspace_invalid")
    candidates = sorted(set(candidate_paths))
    if len(candidates) > MAX_ENTRIES:
        raise D1Error("workspace_capacity_exceeded")
    collisions = {}
    accepted = []
    excluded = {}
    logical = 0
    allocated = 0
    for raw_path in candidates:
        relative = _validated_relative(raw_path)
        collision_key = unicodedata.normalize("NFC", relative.as_posix()).casefold()
        if collision_key in collisions and collisions[collision_key] != relative.as_posix():
            raise D1Error("workspace_path_collision")
        collisions[collision_key] = relative.as_posix()
        reason = _staging_filter_reason(relative.as_posix())
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        entry = _read_source_file(source, relative, root_info.st_dev)
        reason = _content_filter_reason(relative.as_posix(), entry["data"], known_secrets)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        logical += entry["size"]
        allocated += entry["allocated"]
        if logical > MAX_LOGICAL_BYTES or allocated > MAX_ALLOCATED_BYTES:
            raise D1Error("workspace_capacity_exceeded")
        accepted.append((relative, entry))
    destination.mkdir(mode=0o700, parents=False)
    try:
        for relative, entry in accepted:
            _publish_staged_file(destination, relative, entry)
        second = []
        for relative, entry in accepted:
            current = _read_source_file(source, relative, root_info.st_dev)
            if (
                current["identity"] != entry["identity"]
                or current["sha256"] != entry["sha256"]
                or current["mode"] != entry["mode"]
            ):
                raise D1Error("workspace_changed_during_stage")
            second.append(
                {
                    "path": relative.as_posix(),
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                    "mode": entry["mode"],
                }
            )
        root_after = source.lstat()
        if (root_after.st_dev, root_after.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise D1Error("workspace_changed_during_stage")
        serialized = json.dumps(second, sort_keys=True, separators=(",", ":")).encode()
        accepted_paths = {entry["path"] for entry in second}
        tracked = sorted(accepted_paths & set(tracked_paths))
        return {
            "tree_digest": _sha256_bytes(serialized),
            "file_count": len(second),
            "logical_bytes": logical,
            "allocated_bytes": allocated,
            "tracked_paths": tracked,
            "untracked_paths": sorted(accepted_paths - set(tracked)),
            "excluded_counts": dict(sorted(excluded.items())),
            "entries": second,
        }
    except Exception:
        # The caller owns the temporary parent. A failed candidate is never reused.
        raise


def _compile_create_argv(plan):
    required = {
        "sandbox_id",
        "call_id",
        "reconciliation_token",
        "container_name",
        "image_reference",
        "image_id",
        "workspace",
        "target_argv",
        "user",
        "labels",
    }
    if not isinstance(plan, dict) or set(plan) != required:
        raise D1Error("execution_plan_invalid")
    workspace = Path(plan["workspace"])
    if (
        not workspace.is_absolute()
        or not workspace.is_dir()
        or _SHA256_RE.fullmatch(plan["image_reference"]) is None
        or _SHA256_RE.fullmatch(plan["image_id"]) is None
        or not isinstance(plan["target_argv"], list)
        or not plan["target_argv"]
        or any(type(item) is not str or not item for item in plan["target_argv"])
        or type(plan["labels"]) is not dict
    ):
        raise D1Error("execution_plan_invalid")
    mount = (
        f"type=bind,src={workspace},dst=/workspace,"
        "bind-propagation=rprivate,bind-recursive=disabled"
    )
    argv = [
        "create",
        "--pull=never",
        f"--name={plan['container_name']}",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=256",
        "--memory=2g",
        "--memory-swap=2g",
        "--cpus=2",
        "--shm-size=64m",
        "--ulimit=nofile=1024:1024",
        "--ulimit=core=0:0",
        "--log-driver=none",
        "--hostname=pico-sandbox",
        f"--user={plan['user']}",
        "--workdir=/workspace",
        "--mount",
        mount,
        "--tmpfs=/tmp:rw,nosuid,nodev,exec,size=768m,mode=1777",
        "--tmpfs=/home/pico:rw,nosuid,nodev,noexec,size=64m,mode=700,uid=10001,gid=10001",
        "--tmpfs=/run:rw,nosuid,nodev,noexec,size=16m,mode=755,uid=10001,gid=10001",
    ]
    for value in GUEST_ENV:
        argv.append("--env=" + value)
    for key, value in sorted(plan["labels"].items()):
        if type(key) is not str or type(value) is not str or not key or not value:
            raise D1Error("execution_plan_invalid")
        argv.append(f"--label={key}={value}")
    argv.append(plan["image_reference"])
    argv.extend(plan["target_argv"])
    return argv


def _verify_container_inspect(payload, plan):
    try:
        host = payload["HostConfig"]
        config = payload["Config"]
        mounts = payload["Mounts"]
        host_mounts = host["Mounts"]
        ulimits = {item["Name"]: (item["Soft"], item["Hard"]) for item in host["Ulimits"]}
        mount = mounts[0]
        host_mount = host_mounts[0]
        networks = payload["NetworkSettings"]["Networks"]
        valid = (
            isinstance(payload["Id"], str)
            and _HEX_64_RE.fullmatch(payload["Id"]) is not None
            and payload["Image"] == plan["image_reference"]
            and payload["Path"] == plan["target_argv"][0]
            and payload["Args"] == plan["target_argv"][1:]
            and config["Hostname"] == "pico-sandbox"
            and config["User"] == plan["user"]
            and len(config["Env"]) == len(GUEST_ENV)
            and sorted(config["Env"]) == sorted(GUEST_ENV)
            and config["WorkingDir"] == "/workspace"
            and config["Labels"] == plan["labels"]
            and host["Binds"] is None
            and host["NetworkMode"] == "none"
            and host["ReadonlyRootfs"] is True
            and host["Privileged"] is False
            and host["CapAdd"] in (None, [])
            and host["CapDrop"] == ["ALL"]
            and host["SecurityOpt"] == ["no-new-privileges:true"]
            and host["PidsLimit"] == 256
            and host["Memory"] == 2 * 1024**3
            and host["MemorySwap"] == 2 * 1024**3
            and host["NanoCpus"] == 2_000_000_000
            and host["ShmSize"] == 64 * 1024**2
            and ulimits == {"nofile": (1024, 1024), "core": (0, 0)}
            and host["LogConfig"] == {"Type": "none", "Config": {}}
            and host["Tmpfs"] == _D1_POLICY["tmpfs"]
            and len(host_mounts) == 1
            and host_mount["Type"] == "bind"
            and host_mount["Source"] == plan["workspace"]
            and host_mount["Target"] == "/workspace"
            and host_mount["BindOptions"]
            == {"Propagation": "rprivate", "NonRecursive": True}
            and host["IpcMode"] == "private"
            and host["PidMode"] == ""
            and host["UTSMode"] == ""
            and host["CgroupnsMode"] == "private"
            and host["UsernsMode"] == ""
            and host["Devices"] == []
            and host["DeviceRequests"] is None
            and host["PortBindings"] == {}
            and host["PublishAllPorts"] is False
            and host["AutoRemove"] is False
            and host["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
            and len(mounts) == 1
            and mount["Type"] == "bind"
            and mount["Source"] == plan["workspace"]
            and mount["Destination"] == "/workspace"
            and mount["RW"] is True
            and mount["Propagation"] == "rprivate"
            and set(networks) == {"none"}
        )
    except (KeyError, IndexError, TypeError, ValueError):
        valid = False
    if not valid:
        raise D1Error("container_contract_mismatch")


_RUN_FIELDS = {
    "record_type",
    "format_version",
    "status",
    "reason_code",
    "candidate_digest",
    "policy_digest",
    "corpus_digest",
    "checks",
    "mandatory_passed",
    "mandatory_failed",
    "target_started_count",
    "container_create_count",
    "host_fallback_count",
    "residue_count",
    "source_unchanged",
}


def _validate_run_artifact(report):
    if not isinstance(report, dict) or set(report) != _RUN_FIELDS:
        raise D1Error("artifact_schema_invalid")
    if (
        report["record_type"] != "docker_sandbox_d1_run"
        or type(report["format_version"]) is not int
        or report["format_version"] != 1
        or report["status"] != "passed"
        or report["reason_code"] != "mandatory_checks_passed"
        or _SHA256_RE.fullmatch(report["candidate_digest"] or "") is None
        or _SHA256_RE.fullmatch(report["policy_digest"] or "") is None
        or report["corpus_digest"] != CORPUS_DIGEST
    ):
        raise D1Error("mandatory_evidence_incomplete")
    checks = report["checks"]
    if (
        not isinstance(checks, list)
        or [item.get("check_id") for item in checks if isinstance(item, dict)]
        != list(MANDATORY_CHECK_IDS)
        or any(
            set(item) != {"check_id", "status", "reason_code"}
            or item["status"] != "pass"
            or type(item["reason_code"]) is not str
            or not item["reason_code"]
            for item in checks
        )
    ):
        raise D1Error("mandatory_checks_incomplete")
    if (
        type(report["mandatory_passed"]) is not int
        or report["mandatory_passed"] != len(MANDATORY_CHECK_IDS)
        or type(report["mandatory_failed"]) is not int
        or report["mandatory_failed"] != 0
        or type(report["target_started_count"]) is not int
        or report["target_started_count"] < 1
        or type(report["container_create_count"]) is not int
        or report["container_create_count"] < 1
        or type(report["host_fallback_count"]) is not int
        or report["host_fallback_count"] != 0
        or type(report["residue_count"]) is not int
        or report["residue_count"] != 0
        or report["source_unchanged"] is not True
    ):
        raise D1Error("mandatory_evidence_incomplete")


def build_feasibility_approval(report, *, artifact_digest):
    _validate_run_artifact(report)
    if _SHA256_RE.fullmatch(artifact_digest or "") is None:
        raise D1Error("artifact_invalid")
    return {
        "record_type": "docker_sandbox_feasibility_approval",
        "format_version": 1,
        "status": "approved_for_implementation",
        "candidate_digest": report["candidate_digest"],
        "policy_digest": report["policy_digest"],
        "corpus_digest": CORPUS_DIGEST,
        "run_artifact_digest": artifact_digest,
        "product_enablement": False,
    }


def verify_d1_artifacts(*, state_root):
    state_root = _ensure_private_dir(state_root)
    image = _load_image_artifact(state_root)
    calibration = _load_calibration_artifact(state_root, image)
    raw = _read_artifact_bytes(
        Path(state_root) / "run-artifact.json",
        state_root,
        max_bytes=MAX_ARTIFACT_BYTES,
    )
    report = _decode_json(raw, _RUN_FIELDS)
    _validate_run_artifact(report)
    if (
        report["candidate_digest"] != image["candidate_digest"]
        or report["candidate_digest"] != calibration["candidate_digest"]
        or report["policy_digest"] != calibration["policy_digest"]
        or report["policy_digest"] != POLICY_DIGEST
    ):
        raise D1Error("artifact_identity_mismatch")
    approval = build_feasibility_approval(
        report,
        artifact_digest=_sha256_bytes(raw),
    )
    path = Path(state_root) / "feasibility-approval.json"
    _atomic_write_json(path, state_root, approval)
    verified = _read_json_artifact(
        path,
        state_root,
        set(approval),
    )
    if verified != approval:
        raise D1Error("artifact_write_failed")
    return approval


def _file_state(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise D1Error("apply_fixture_invalid")
    data = path.read_bytes()
    return {
        "sha256": _sha256_bytes(data),
        "data": data,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _replace_file(path, data, mode):
    path = Path(path)
    descriptor, raw_temp = tempfile.mkstemp(prefix=".pico-d1-", dir=path.parent)
    temp = Path(raw_temp)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise D1Error("apply_fixture_failed")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _apply_fixture_file(path, baseline, candidate, *, inject_fault=False):
    path = Path(path)
    try:
        current = _file_state(path)
    except (OSError, D1Error):
        return "conflict"
    comparable = ("sha256", "mode", "uid", "gid")
    if any(current[key] != baseline[key] for key in comparable):
        return "conflict"
    try:
        _replace_file(path, candidate, baseline["mode"])
        if inject_fault:
            raise OSError("injected fault")
        return "applied"
    except OSError:
        _replace_file(path, baseline["data"], baseline["mode"])
        restored = _file_state(path)
        if any(restored[key] != baseline[key] for key in comparable):
            raise D1Error("apply_fixture_review_required")
        return "failed_rolled_back"


def build_parser():
    parser = argparse.ArgumentParser(
        description="Standalone Pico Docker Sandbox D1 feasibility harness",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--docker-cli", required=True)
    status.add_argument("--socket", required=True)
    status.add_argument("--docker-config", required=True)
    prepare = subparsers.add_parser("prepare-image")
    prepare.add_argument("--docker-cli", required=True)
    prepare.add_argument("--buildx-cli", required=True)
    prepare.add_argument("--socket", required=True)
    prepare.add_argument("--state-root", required=True)
    prepare.add_argument("--repo-root", required=True)
    prepare.add_argument("--dockerfile", required=True)
    prepare.add_argument("--image-lock", required=True)
    calibrate = subparsers.add_parser("calibrate")
    calibrate.add_argument("--docker-cli", required=True)
    calibrate.add_argument("--socket", required=True)
    calibrate.add_argument("--state-root", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--docker-cli", required=True)
    run.add_argument("--socket", required=True)
    run.add_argument("--status-config", required=True)
    run.add_argument("--state-root", required=True)
    run.add_argument("--source", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--state-root", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.action == "status":
        try:
            payload = build_status_report(
                docker_cli=args.docker_cli,
                socket_path=args.socket,
                docker_config=args.docker_config,
            )
        except D1Error as exc:
            payload = {
                "record_type": "docker_sandbox_d1_status",
                "format_version": 1,
                "status": "failed",
                "reason_code": exc.code,
                "network_performed": False,
                "mutation_performed": False,
                "product_enablement": False,
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "ready" else 3
    if args.action == "prepare-image":
        try:
            payload = prepare_image(
                docker_cli=args.docker_cli,
                buildx_cli=args.buildx_cli,
                socket_path=args.socket,
                state_root=args.state_root,
                repo_root=args.repo_root,
                dockerfile=args.dockerfile,
                image_lock=args.image_lock,
            )
        except D1Error as exc:
            payload = {
                "record_type": "docker_sandbox_d1_image",
                "format_version": 1,
                "status": "failed",
                "reason_code": exc.code,
                "product_enablement": False,
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "ready" else 3
    if args.action == "calibrate":
        try:
            payload = calibrate_policy(
                docker_cli=args.docker_cli,
                socket_path=args.socket,
                state_root=args.state_root,
            )
        except D1Error as exc:
            payload = {
                "record_type": "docker_sandbox_d1_calibration",
                "format_version": 1,
                "status": "failed",
                "reason_code": exc.code,
                "product_enablement": False,
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "ready" else 3
    if args.action == "run":
        try:
            payload = run_d1_corpus(
                docker_cli=args.docker_cli,
                socket_path=args.socket,
                status_config=args.status_config,
                state_root=args.state_root,
                source_root=args.source,
            )
        except D1Error as exc:
            payload = {
                "record_type": "docker_sandbox_d1_run",
                "format_version": 1,
                "status": "failed",
                "reason_code": exc.code,
                "product_enablement": False,
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "passed" else 3
    if args.action == "verify":
        try:
            payload = verify_d1_artifacts(state_root=args.state_root)
        except D1Error as exc:
            payload = {
                "record_type": "docker_sandbox_feasibility_approval",
                "format_version": 1,
                "status": "failed",
                "reason_code": exc.code,
                "product_enablement": False,
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if payload["status"] == "approved_for_implementation" else 3
    payload = {
        "record_type": "docker_sandbox_d1_command",
        "format_version": 1,
        "action": args.action,
        "status": "not_ready",
        "reason_code": "d1_action_not_implemented",
        "product_enablement": False,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
