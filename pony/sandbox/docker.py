"""Production Docker Sandbox readiness, execution plan, and runner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
from types import MappingProxyType

from pony.security import private_files as securitylib
from pony.state.checkpoint_store import CheckpointStoreError, source_apply_guard_present
from pony.sandbox.session import (
    MAX_ALLOCATED_BYTES,
    MAX_DEPTH,
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_LOGICAL_BYTES,
    SandboxSession,
    SandboxSessionError,
    SandboxSessionStore,
    source_mutation_authority,
    SyntheticGitBootstrapRequest,
    WorkspaceView,
)


FORMAT_VERSION = 1
MAX_DOCKER_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_MANIFEST_BYTES = 1024 * 1024
MAX_CALL_PLAN_BYTES = 1024 * 1024
MAX_PRODUCT_TIMEOUT = 120
_WATCHDOG_MIN_INTERVAL = 0.25
_WATCHDOG_MAX_INTERVAL = 2.0
_WATCHDOG_SCAN_MULTIPLIER = 10.0
_CALL_PLAN_NAME = "active-call-plan.json"

IMAGE_LABELS = {
    "io.pony.sandbox.image-policy": "1",
    "io.pony.sandbox.managed": "true",
    "org.opencontainers.image.title": "Pony Docker Sandbox",
    "org.opencontainers.image.version": "d1",
}
MINIMUM_DOCKER_API_VERSION = "1.44"
GUEST_ENV = (
    "PATH=/opt/pony-venv/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME=/home/pony",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "PONY_SANDBOX=1",
    "PONY_WORKSPACE=/workspace",
    "PYTHONDONTWRITEBYTECODE=1",
    "TMPDIR=/tmp",
)
DOCKER_POLICY = {
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
        "/home/pony": ("rw,nosuid,nodev,noexec,size=64m,mode=700,uid=10001,gid=10001"),
        "/run": "rw,nosuid,nodev,noexec,size=16m,mode=755,uid=10001,gid=10001",
    },
}

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SANDBOX_ID_RE = re.compile(r"^sandbox_[0-9a-f]{32}$")
_CALL_ID_RE = re.compile(r"^call_[0-9a-f]{32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CALL_PLAN_FIELDS = {
    "record_type",
    "format_version",
    "sandbox_id",
    "call_id",
    "reconciliation_token",
    "container_name",
    "image_digest",
    "image_id",
    "workspace",
    "workspace_device",
    "workspace_inode",
    "target_argv",
    "user",
    "labels",
    "env",
    "timeout",
    "policy_digest",
    "client_identity_digest",
    "logical_intent_digest",
    "execution_plan_digest",
}
_IMAGE_SET_MANIFEST_FIELDS = {
    "record_type",
    "format_version",
    "policy_digest",
    "user",
    "working_dir",
    "env",
    "tool_paths",
    "platforms",
}
_PLATFORM_IMAGE_FIELDS = {"image_digest", "image_id"}
_IMAGE_PLATFORMS = {"linux/arm64"}


class DockerSandboxError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


POLICY_DIGEST = _sha256(_canonical_json(DOCKER_POLICY))
MOUNT_POLICY_DIGEST = _sha256(
    _canonical_json(
        {
            "bind_propagation": DOCKER_POLICY["bind_propagation"],
            "bind_recursive": DOCKER_POLICY["bind_recursive"],
            "tmpfs": DOCKER_POLICY["tmpfs"],
        }
    )
)
RESOURCE_POLICY_DIGEST = _sha256(
    _canonical_json(
        {
            name: DOCKER_POLICY[name]
            for name in (
                "read_only_rootfs",
                "cap_drop",
                "no_new_privileges",
                "pids_limit",
                "memory_bytes",
                "memory_swap_bytes",
                "nano_cpus",
                "shm_bytes",
                "nofile",
                "core",
                "log_driver",
            )
        }
    )
)


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DockerSandboxError("docker_response_invalid")
        value[key] = item
    return value


def _decode_json(raw, *, error_code="docker_response_invalid"):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                DockerSandboxError(error_code)
            ),
        )
    except DockerSandboxError as exc:
        raise DockerSandboxError(error_code) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockerSandboxError(error_code) from exc
    return value


def _file_identity(info):
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


def _read_stable_file(path, *, max_bytes, error_code, allowed_modes=None):
    path = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else before.st_uid
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, uid}
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size > max_bytes
            or allowed_modes is not None
            and stat.S_IMODE(before.st_mode) not in allowed_modes
        ):
            raise DockerSandboxError(error_code)
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
            or len(raw) != before.st_size
            or _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(current)
        ):
            raise DockerSandboxError(error_code)
        return raw, after
    except DockerSandboxError:
        raise
    except OSError as exc:
        raise DockerSandboxError(error_code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class DockerCLIIdentity:
    entry_path: str
    resolved_path: str
    entry_device: int
    entry_inode: int
    entry_mode: int
    entry_uid: int
    link_target: str
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int
    sha256: str


def freeze_docker_cli(path):
    entry = Path(os.path.abspath(os.fspath(path)))
    try:
        entry_info = entry.lstat()
        uid = os.geteuid() if hasattr(os, "geteuid") else entry_info.st_uid
        if stat.S_ISLNK(entry_info.st_mode):
            if entry_info.st_uid not in {0, uid}:
                raise DockerSandboxError("docker_cli_unavailable")
            link_target = os.readlink(entry)
        elif stat.S_ISREG(entry_info.st_mode):
            link_target = ""
        else:
            raise DockerSandboxError("docker_cli_unavailable")
        resolved = entry.resolve(strict=True)
        raw, info = _read_stable_file(
            resolved,
            max_bytes=MAX_EXECUTABLE_BYTES,
            error_code="docker_cli_unavailable",
        )
        if not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise DockerSandboxError("docker_cli_unavailable")
        return DockerCLIIdentity(
            entry_path=str(entry),
            resolved_path=str(resolved),
            entry_device=entry_info.st_dev,
            entry_inode=entry_info.st_ino,
            entry_mode=stat.S_IMODE(entry_info.st_mode),
            entry_uid=entry_info.st_uid,
            link_target=link_target,
            device=info.st_dev,
            inode=info.st_ino,
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            sha256=_sha256(raw),
        )
    except DockerSandboxError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DockerSandboxError("docker_cli_unavailable") from exc


def verify_docker_cli(identity):
    if freeze_docker_cli(identity.entry_path) != identity:
        raise DockerSandboxError("docker_cli_unavailable")


@dataclass(frozen=True)
class DockerEndpointIdentity:
    canonical_path: str
    device: int
    inode: int
    mode: int
    uid: int


def freeze_docker_endpoint(path):
    try:
        canonical = Path(path).resolve(strict=True)
        info = canonical.lstat()
    except (OSError, RuntimeError) as exc:
        raise DockerSandboxError("docker_endpoint_untrusted") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise DockerSandboxError("docker_endpoint_untrusted")
    return DockerEndpointIdentity(
        canonical_path=str(canonical),
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
    )


def verify_docker_endpoint(identity):
    try:
        current = freeze_docker_endpoint(identity.canonical_path)
    except DockerSandboxError as exc:
        raise DockerSandboxError("docker_endpoint_untrusted") from exc
    if current != identity:
        raise DockerSandboxError("docker_endpoint_untrusted")


def _docker_config_identity(path):
    path = Path(os.path.abspath(os.fspath(path)))
    try:
        info = path.lstat()
        entries = sorted(item.name for item in path.iterdir())
    except OSError as exc:
        raise DockerSandboxError("docker_config_invalid") from exc
    uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid not in {0, uid}
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or entries != ["config.json"]
    ):
        raise DockerSandboxError("docker_config_invalid")
    raw, config_info = _read_stable_file(
        path / "config.json",
        max_bytes=3,
        error_code="docker_config_invalid",
        allowed_modes={0o400, 0o440, 0o444, 0o600, 0o640, 0o644},
    )
    if raw != b"{}\n":
        raise DockerSandboxError("docker_config_invalid")
    return (
        _file_identity(info),
        _file_identity(config_info),
        _sha256(raw),
        tuple(entries),
    )


def _drain_stream(stream, retained, counts, index, max_bytes, errors, overflow):
    try:
        read = getattr(stream, "read1", stream.read)
        while True:
            chunk = read(64 * 1024)
            if not chunk:
                return
            counts[index] += len(chunk)
            if counts[index] > max_bytes:
                overflow.set()
            available = max(0, max_bytes - len(retained[index]))
            if available:
                retained[index].extend(chunk[:available])
    except BaseException as exc:
        errors.append(exc)
    finally:
        stream.close()


@dataclass(frozen=True)
class DockerCommandResult:
    exit_code: int
    timed_out: bool
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


def _run_bounded_process(argv, *, env, timeout, max_bytes, terminate_on_overflow=False):
    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or any(type(item) is not str or not item or "\x00" in item for item in argv)
    ):
        raise DockerSandboxError("docker_argv_invalid")
    try:
        process = subprocess.Popen(
            list(argv),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise DockerSandboxError("docker_cli_unavailable") from exc
    retained = [bytearray(), bytearray()]
    counts = [0, 0]
    errors = []
    overflow = threading.Event()
    threads = [
        threading.Thread(
            target=_drain_stream,
            args=(stream, retained, counts, index, max_bytes, errors, overflow),
            daemon=True,
        )
        for index, stream in enumerate((process.stdout, process.stderr))
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    interrupted = None
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if terminate_on_overflow and overflow.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            continue
        except BaseException as exc:
            interrupted = exc
            break
    if (
        timed_out
        or interrupted is not None
        or terminate_on_overflow
        and overflow.is_set()
    ):
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, signal_number)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.poll() is None:
            process.wait()
    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads) or errors:
        raise DockerSandboxError("docker_cli_output_incomplete")
    if interrupted is not None:
        raise interrupted
    return DockerCommandResult(
        exit_code=int(process.returncode),
        timed_out=timed_out,
        stdout=bytes(retained[0]),
        stderr=bytes(retained[1]),
        stdout_bytes=counts[0],
        stderr_bytes=counts[1],
        stdout_truncated=counts[0] > max_bytes,
        stderr_truncated=counts[1] > max_bytes,
    )


@dataclass(frozen=True)
class DockerImageManifest:
    image_set_digest: str
    policy_digest: str
    platform: str
    image_digest: str
    image_id: str
    user: str
    working_dir: str
    env: tuple[str, ...]
    tool_paths: tuple[tuple[str, str], ...]

    @property
    def label_map(self):
        return dict(IMAGE_LABELS)

    @property
    def tool_map(self):
        return dict(self.tool_paths)

    @property
    def architecture(self):
        return self.platform.removeprefix("linux/")

    @property
    def operating_system(self):
        return "linux"

    @property
    def minimum_api_version(self):
        return MINIMUM_DOCKER_API_VERSION


_RUNTIME_AUTHORIZATION_SEAL = object()
_RUNTIME_AUTHORIZATION_KINDS = {"local", "development"}
_DISTRIBUTION_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")


@dataclass(frozen=True)
class DockerSandboxRuntimeAuthorization:
    distribution_version: str
    installed_tree_digest: str
    image_set_digest: str
    image_digest: str
    image_id: str
    image_platform: str
    policy_digest: str
    attestation_kind: str
    attestation_digest: str
    release_sequence: int
    _package_root: Path = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self):
        if (
            self._seal is not _RUNTIME_AUTHORIZATION_SEAL
            or _DISTRIBUTION_VERSION_RE.fullmatch(self.distribution_version) is None
            or self.attestation_kind not in _RUNTIME_AUTHORIZATION_KINDS
            or type(self.release_sequence) is not int
            or self.release_sequence < 0
            or self.release_sequence != 0
            or any(
                _SHA256_RE.fullmatch(value) is None
                for value in (
                    self.installed_tree_digest,
                    self.image_set_digest,
                    self.image_digest,
                    self.image_id,
                    self.policy_digest,
                    self.attestation_digest,
                )
            )
            or self.image_platform not in _IMAGE_PLATFORMS
            or not isinstance(self._package_root, Path)
            or not self._package_root.is_absolute()
        ):
            raise DockerSandboxError("sandbox_runtime_authorization_invalid")

    def verify(self, image):
        from . import identity

        if not isinstance(image, DockerImageManifest):
            raise DockerSandboxError("sandbox_runtime_authorization_mismatch")
        try:
            current_tree_digest = (
                identity.installed_tree_digest(self._package_root)
                if self.attestation_kind == "local"
                else identity.installed_tree_digest(
                    self._package_root,
                    self.distribution_version,
                )
            )
            local_identity_matches = self.attestation_kind != "local" or (
                self._package_root == Path(__file__).resolve().parent
                and self.distribution_version == metadata.version("pony-code")
                and image == load_image_manifest(default_image_manifest_path())
                and self.attestation_digest
                == _runtime_authorization_digest(
                    record_type="docker_sandbox_local_authorization",
                    payload={
                        "distribution_version": self.distribution_version,
                        "installed_tree_digest": self.installed_tree_digest,
                        "image_set_digest": self.image_set_digest,
                        "policy_digest": self.policy_digest,
                        "release_sequence": self.release_sequence,
                    },
                    image=image,
                )
            )
        except (
            OSError,
            ValueError,
            metadata.PackageNotFoundError,
            identity.SandboxIdentityError,
        ):
            current_tree_digest = ""
            local_identity_matches = False
        if (
            image.policy_digest != POLICY_DIGEST
            or current_tree_digest != self.installed_tree_digest
            or not local_identity_matches
            or self.image_set_digest != image.image_set_digest
            or self.image_digest != image.image_digest
            or self.image_id != image.image_id
            or self.image_platform != image.platform
            or self.policy_digest != image.policy_digest
        ):
            raise DockerSandboxError("sandbox_runtime_authorization_mismatch")
        return self


def _runtime_authorization_digest(
    *,
    record_type,
    payload,
    image,
):
    from . import identity

    return identity.canonical_digest(
        {
            "record_type": record_type,
            "format_version": 1,
            **payload,
            "image_digest": image.image_digest,
            "image_id": image.image_id,
            "image_platform": image.platform,
        }
    )


def _sealed_runtime_authorization(
    payload,
    image,
    *,
    kind,
    digest,
    package_root,
):
    if not isinstance(payload, dict):
        raise DockerSandboxError("sandbox_runtime_authorization_invalid")
    authorization = DockerSandboxRuntimeAuthorization(
        distribution_version=payload.get("distribution_version", ""),
        installed_tree_digest=payload.get("installed_tree_digest", ""),
        image_set_digest=payload.get("image_set_digest", ""),
        image_digest=image.image_digest,
        image_id=image.image_id,
        image_platform=image.platform,
        policy_digest=payload.get("policy_digest", ""),
        attestation_kind=kind,
        attestation_digest=digest,
        release_sequence=payload.get("release_sequence", -1),
        _package_root=Path(package_root).resolve(),
        _seal=_RUNTIME_AUTHORIZATION_SEAL,
    )
    return authorization.verify(image)


def _authorize_docker_sandbox_development(
    *,
    package_root,
    distribution_version,
    image,
):
    """Private D1-D6 owner seam; never accepted by the public Pony constructor."""
    from . import identity

    payload = {
        "distribution_version": str(distribution_version),
        "installed_tree_digest": identity.installed_tree_digest(
            package_root,
            distribution_version,
        ),
        "image_set_digest": image.image_set_digest,
        "policy_digest": image.policy_digest,
        "release_sequence": 0,
    }
    digest = identity.canonical_digest(
        {
            "record_type": "docker_sandbox_development_authorization",
            "format_version": 1,
            **payload,
            "image_digest": image.image_digest,
            "image_id": image.image_id,
            "image_platform": image.platform,
        }
    )
    return _sealed_runtime_authorization(
        payload,
        image,
        kind="development",
        digest=digest,
        package_root=package_root,
    )


def local_docker_sandbox_runtime():
    """Seal the packaged image to the currently installed Pony tree."""
    from . import identity

    package_root = Path(__file__).resolve().parent
    image = load_image_manifest(default_image_manifest_path())
    try:
        distribution_version = metadata.version("pony-code")
        installed_tree_digest = identity.installed_tree_digest(package_root)
    except (
        OSError,
        ValueError,
        metadata.PackageNotFoundError,
        identity.SandboxIdentityError,
    ) as exc:
        raise DockerSandboxError("sandbox_runtime_authorization_invalid") from exc
    payload = {
        "distribution_version": distribution_version,
        "installed_tree_digest": installed_tree_digest,
        "image_set_digest": image.image_set_digest,
        "policy_digest": image.policy_digest,
        "release_sequence": 0,
    }
    authorization = _sealed_runtime_authorization(
        payload,
        image,
        kind="local",
        digest=_runtime_authorization_digest(
            record_type="docker_sandbox_local_authorization",
            payload=payload,
            image=image,
        ),
        package_root=package_root,
    )
    return image, authorization


def _host_image_platform():
    architecture = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }.get(platform.machine().casefold())
    return "linux/" + architecture if architecture else ""


def load_image_manifest(path, *, target_platform=None):
    raw, _info = _read_stable_file(
        path,
        max_bytes=MAX_IMAGE_MANIFEST_BYTES,
        error_code="sandbox_image_identity_mismatch",
    )
    value = _decode_json(raw, error_code="sandbox_image_identity_mismatch")
    if not isinstance(value, dict) or set(value) != _IMAGE_SET_MANIFEST_FIELDS:
        raise DockerSandboxError("sandbox_image_identity_mismatch")
    tools = value["tool_paths"]
    platforms = value["platforms"]
    if (
        value["record_type"] != "docker_sandbox_image_set_manifest"
        or value["format_version"] != 3
        or not isinstance(value["policy_digest"], str)
        or _SHA256_RE.fullmatch(value["policy_digest"]) is None
        or value["policy_digest"] != POLICY_DIGEST
        or value["working_dir"] != "/workspace"
        or not isinstance(value["user"], str)
        or not value["user"]
        or value["env"] != list(GUEST_ENV)
        or not isinstance(tools, dict)
        or set(tools) != {"git", "pytest", "python", "rg", "ruff", "shell", "uv"}
        or any(
            not isinstance(item, str) or not item.startswith("/")
            for item in tools.values()
        )
        or not isinstance(platforms, dict)
        or not platforms
        or not set(platforms) <= _IMAGE_PLATFORMS
    ):
        raise DockerSandboxError("sandbox_image_identity_mismatch")
    for platform_name, item in platforms.items():
        if (
            not isinstance(item, dict)
            or set(item) != _PLATFORM_IMAGE_FIELDS
            or any(
                not isinstance(item[name], str)
                or _SHA256_RE.fullmatch(item[name]) is None
                for name in ("image_digest", "image_id")
            )
        ):
            raise DockerSandboxError("sandbox_image_identity_mismatch")
    selected_platform = target_platform or _host_image_platform()
    selected = platforms.get(selected_platform)
    if selected is None:
        raise DockerSandboxError("sandbox_image_not_released")
    return DockerImageManifest(
        image_set_digest=_sha256(_canonical_json(value)),
        policy_digest=value["policy_digest"],
        platform=selected_platform,
        image_digest=selected["image_digest"],
        image_id=selected["image_id"],
        user=value["user"],
        working_dir=value["working_dir"],
        env=tuple(value["env"]),
        tool_paths=tuple(sorted(tools.items())),
    )


def default_image_manifest_path():
    return Path(__file__).parent / "resources" / "image-manifest.json"


def default_docker_config_path():
    return Path(__file__).parent / "resources" / "docker-config"


def runtime_docker_config_path(home=None):
    return Path(home or Path.home()) / ".pony" / "docker" / "config"


def ensure_runtime_docker_config(path=None):
    root = securitylib.ensure_private_dir(path or runtime_docker_config_path())
    config = root / "config.json"
    try:
        config.lstat()
    except FileNotFoundError:
        securitylib.write_private_bytes_atomic(
            config,
            b"{}\n",
            trusted_root=root,
            trusted_root_identity=securitylib.private_directory_identity(root),
            max_existing_bytes=3,
        )
    _docker_config_identity(root)
    return root


def discover_local_docker(*, environ=None, home=None, host_system=None):
    env = dict(os.environ if environ is None else environ)
    if env.get("DOCKER_HOST") or env.get("DOCKER_CONTEXT"):
        raise DockerSandboxError("docker_remote_endpoint_unsupported")
    cli = shutil.which("docker", path=env.get("PATH"))
    if not cli:
        raise DockerSandboxError("docker_cli_unavailable")
    system = (host_system or platform.system()).casefold()
    if system == "darwin":
        endpoint = Path(home or Path.home()) / ".docker" / "run" / "docker.sock"
    elif system == "linux":
        runtime = env.get("XDG_RUNTIME_DIR", "")
        if not runtime or not Path(runtime).is_absolute():
            raise DockerSandboxError("docker_endpoint_untrusted")
        endpoint = Path(runtime) / "docker.sock"
    else:
        raise DockerSandboxError("docker_server_unsupported")
    freeze_docker_cli(cli)
    freeze_docker_endpoint(endpoint)
    return Path(cli), endpoint


def _api_version(value):
    try:
        major, minor = str(value).split(".", 1)
        return int(major), int(minor)
    except (TypeError, ValueError) as exc:
        raise DockerSandboxError("docker_server_unsupported") from exc


def verify_image_inspect(payload, image):
    try:
        if not isinstance(payload, list) or len(payload) != 1:
            raise TypeError
        data = payload[0]
        config = data["Config"]
        descriptor_matches = "Descriptor" not in data
        id_matches = data["Id"] == image.image_id
        if not descriptor_matches:
            descriptor = data["Descriptor"]
            descriptor_matches = (
                isinstance(descriptor, dict)
                and descriptor.get("digest") == image.image_digest
                and descriptor.get("annotations", {}).get("config.digest")
                == image.image_id
            )
            id_matches = data["Id"] in {image.image_digest, image.image_id}
        valid = (
            id_matches
            and descriptor_matches
            and {"aarch64": "arm64", "x86_64": "amd64"}.get(
                data["Architecture"],
                data["Architecture"],
            )
            == image.architecture
            and data["Os"] == image.operating_system
            and config.get("Entrypoint") in (None, [])
            and config.get("Cmd") in (None, [])
            and config.get("User") == image.user
            and config.get("WorkingDir") == image.working_dir
            and config.get("Env") == list(image.env)
            and config.get("Labels") == image.label_map
            and config.get("Volumes") in (None, {})
            and config.get("ExposedPorts") in (None, {})
            and config.get("Healthcheck") is None
            and config.get("StopSignal") in (None, "")
        )
    except (KeyError, IndexError, TypeError):
        valid = False
    if not valid:
        raise DockerSandboxError("sandbox_image_identity_mismatch")


class DockerClient:
    def __init__(self, cli, endpoint, config_dir):
        self.cli = freeze_docker_cli(cli)
        self.endpoint = freeze_docker_endpoint(endpoint)
        self.config_dir = Path(os.path.abspath(os.fspath(config_dir)))
        _docker_config_identity(self.config_dir)

    def identity_digest(self):
        return _sha256(
            _canonical_json(
                {
                    "cli": asdict(self.cli),
                    "endpoint": asdict(self.endpoint),
                }
            )
        )

    def endpoint_digest(self):
        return _sha256(_canonical_json(asdict(self.endpoint)))

    def command(self, args, *, timeout=30, max_bytes=MAX_DOCKER_RESPONSE_BYTES):
        if (
            not isinstance(args, (list, tuple))
            or not args
            or any(type(item) is not str or not item for item in args)
        ):
            raise DockerSandboxError("docker_argv_invalid")
        before = _docker_config_identity(self.config_dir)
        verify_docker_cli(self.cli)
        verify_docker_endpoint(self.endpoint)
        result = _run_bounded_process(
            [
                self.cli.resolved_path,
                "--config",
                str(self.config_dir),
                "--host",
                "unix://" + self.endpoint.canonical_path,
                *args,
            ],
            env={
                "DOCKER_CONFIG": str(self.config_dir),
                "HOME": str(self.config_dir),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
            timeout=timeout,
            max_bytes=max_bytes,
        )
        verify_docker_cli(self.cli)
        verify_docker_endpoint(self.endpoint)
        if _docker_config_identity(self.config_dir) != before:
            raise DockerSandboxError("docker_config_invalid")
        return result

    def json_command(self, args, *, timeout=30):
        result = self.command(args, timeout=timeout)
        if result.timed_out or result.exit_code != 0 or result.stdout_truncated:
            raise DockerSandboxError("docker_daemon_unavailable")
        return _decode_json(result.stdout)

    def status(self, image, *, host_system=None):
        version = self.json_command(["version", "--format", "{{json .}}"])
        info = self.json_command(["info", "--format", "{{json .}}"])
        try:
            client = version["Client"]
            server = version["Server"]
            security_options = info["SecurityOptions"]
            if type(security_options) is not list or any(
                type(item) is not str or not item for item in security_options
            ):
                raise TypeError("invalid Docker security options")
            system = (host_system or platform.system()).casefold()
            desktop = system == "darwin"
            profile = "desktop_vm" if desktop else "linux_rootless"
            normalized_security = [item.casefold() for item in security_options]
            rootless = normalized_security.count("name=rootless") == 1
            seccomp_options = [
                item for item in normalized_security if item.startswith("name=seccomp")
            ]
            seccomp_profile = (
                {
                    "name=seccomp,profile=builtin": "builtin",
                    "name=seccomp,profile=default": "default",
                }.get(seccomp_options[0], "")
                if len(seccomp_options) == 1
                else ""
            )
            limits = all(
                info.get(name) is True
                for name in ("MemoryLimit", "CpuCfsPeriod", "PidsLimit")
            )
            server_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(
                str(info["Architecture"]),
                str(info["Architecture"]),
            )
            server_supported = (
                info["OSType"] == "linux"
                and server_arch in {"arm64", "amd64"}
                and _api_version(server["ApiVersion"])
                >= _api_version(image.minimum_api_version)
                and limits
            )
            seccomp_supported = bool(seccomp_profile)
            profile_supported = desktop or rootless
            supported = server_supported and seccomp_supported and profile_supported
        except (KeyError, TypeError) as exc:
            raise DockerSandboxError("docker_server_unsupported") from exc
        image_result = self.command(["image", "inspect", image.image_digest])
        if image_result.timed_out or image_result.stdout_truncated:
            raise DockerSandboxError("docker_daemon_unavailable")
        image_present = image_result.exit_code == 0
        if not image_present:
            self.json_command(["version", "--format", "{{json .}}"])
        image_match = False
        if image_present:
            try:
                verify_image_inspect(_decode_json(image_result.stdout), image)
                image_match = True
            except DockerSandboxError:
                image_match = False
        platform_match = server_arch == image.architecture
        ready = supported and image_match and platform_match
        if not server_supported:
            reason = "docker_server_unsupported"
        elif not seccomp_supported:
            reason = "docker_seccomp_unavailable"
        elif not profile_supported:
            reason = "docker_rootless_required"
        elif not image_present:
            reason = "sandbox_image_missing"
        elif not image_match:
            reason = "sandbox_image_identity_mismatch"
        elif not platform_match:
            reason = "sandbox_image_identity_mismatch"
        else:
            reason = "ready"
        return {
            "record_type": "docker_sandbox_status",
            "format_version": FORMAT_VERSION,
            "status": "ready" if ready else "not_ready",
            "reason_code": reason,
            "platform_profile": profile,
            "client_version": str(client.get("Version", "")),
            "server_version": str(server.get("Version", "")),
            "api_version": str(server.get("ApiVersion", "")),
            "server_os": str(info.get("OSType", "")),
            "server_arch": server_arch,
            "endpoint_kind": "local_unix",
            "security": {
                "rootless": rootless,
                "seccomp": seccomp_profile or "unavailable",
                "cgroup_limits": limits,
                "eci": (
                    "enabled"
                    if any(
                        "enhanced container isolation" in str(item).casefold()
                        for item in security_options
                    )
                    else "unknown"
                ),
            },
            "image": {
                "present": image_present,
                "digest_match": image_match,
                "platform_match": platform_match,
            },
            "network_performed": False,
            "mutation_performed": False,
        }

    def require_ready(self, image):
        status = self.status(image)
        if status["status"] != "ready":
            raise DockerSandboxError(status["reason_code"])
        return status

    def prepare(self, image):
        status = self.status(image)
        if status["status"] != "ready":
            raise DockerSandboxError(status["reason_code"])
        return status


@dataclass(frozen=True)
class DockerExecutionPlan:
    sandbox_id: str
    call_id: str
    reconciliation_token: str
    container_name: str
    image_digest: str
    image_id: str
    workspace: str
    workspace_device: int
    workspace_inode: int
    target_argv: tuple[str, ...]
    user: str
    labels: tuple[tuple[str, str], ...]
    env: tuple[str, ...]
    timeout: int
    policy_digest: str
    client_identity_digest: str
    logical_intent_digest: str
    execution_plan_digest: str

    @property
    def label_map(self):
        return dict(self.labels)

    def digest_payload(self):
        value = asdict(self)
        value.pop("execution_plan_digest")
        value["labels"] = self.label_map
        value["target_argv"] = list(self.target_argv)
        value["env"] = list(self.env)
        return value

    def verify(self):
        if (
            _sha256(_canonical_json(self.digest_payload()))
            != self.execution_plan_digest
        ):
            raise DockerSandboxError("approved_execution_changed")


def _call_plan_record(plan):
    plan.verify()
    return {
        "record_type": "docker_sandbox_call_plan",
        "format_version": FORMAT_VERSION,
        **plan.digest_payload(),
        "execution_plan_digest": plan.execution_plan_digest,
    }


def _execution_plan_from_record(value):
    if not isinstance(value, dict) or set(value) != _CALL_PLAN_FIELDS:
        raise DockerSandboxError("sandbox_call_plan_invalid")
    target_argv = value["target_argv"]
    labels = value["labels"]
    env = value["env"]
    strings = ("container_name", "workspace", "user")
    digests = (
        "image_digest",
        "image_id",
        "policy_digest",
        "client_identity_digest",
        "logical_intent_digest",
        "execution_plan_digest",
    )
    if (
        value["record_type"] != "docker_sandbox_call_plan"
        or type(value["format_version"]) is not int
        or value["format_version"] != FORMAT_VERSION
        or type(value["sandbox_id"]) is not str
        or _SANDBOX_ID_RE.fullmatch(value["sandbox_id"]) is None
        or type(value["call_id"]) is not str
        or _CALL_ID_RE.fullmatch(value["call_id"]) is None
        or type(value["reconciliation_token"]) is not str
        or _HEX64_RE.fullmatch(value["reconciliation_token"]) is None
        or any(
            type(value[name]) is not str
            or not value[name]
            or _CONTROL_RE.search(value[name])
            for name in strings
        )
        or not Path(value["workspace"]).is_absolute()
        or "," in value["workspace"]
        or any(
            type(value[name]) is not int or value[name] <= 0
            for name in (
            "workspace_device",
            "workspace_inode",
            )
        )
        or type(value["timeout"]) is not int
        or not 0 < value["timeout"] <= MAX_PRODUCT_TIMEOUT
        or any(
            type(value[name]) is not str or _SHA256_RE.fullmatch(value[name]) is None
            for name in digests
        )
        or not isinstance(target_argv, list)
        or not target_argv
        or any(
            type(item) is not str or not item or "\x00" in item for item in target_argv
        )
        or not isinstance(env, list)
        or any(type(item) is not str or "\x00" in item for item in env)
        or not isinstance(labels, dict)
        or not labels
        or any(
            type(key) is not str
            or not key
            or type(item) is not str
            or not item
            or _CONTROL_RE.search(key)
            or _CONTROL_RE.search(item)
            for key, item in labels.items()
        )
    ):
        raise DockerSandboxError("sandbox_call_plan_invalid")
    plan = DockerExecutionPlan(
        sandbox_id=value["sandbox_id"],
        call_id=value["call_id"],
        reconciliation_token=value["reconciliation_token"],
        container_name=value["container_name"],
        image_digest=value["image_digest"],
        image_id=value["image_id"],
        workspace=value["workspace"],
        workspace_device=value["workspace_device"],
        workspace_inode=value["workspace_inode"],
        target_argv=tuple(target_argv),
        user=value["user"],
        labels=tuple(sorted(labels.items())),
        env=tuple(env),
        timeout=value["timeout"],
        policy_digest=value["policy_digest"],
        client_identity_digest=value["client_identity_digest"],
        logical_intent_digest=value["logical_intent_digest"],
        execution_plan_digest=value["execution_plan_digest"],
    )
    try:
        plan.verify()
    except DockerSandboxError as exc:
        raise DockerSandboxError("sandbox_call_plan_invalid") from exc
    return plan


def compile_execution_plan(
    session,
    image,
    client_identity_digest,
    target_argv,
    *,
    timeout=MAX_PRODUCT_TIMEOUT,
    logical_intent_digest=None,
    _allowed_states=("ready",),
):
    if not isinstance(session, SandboxSession) or session.state not in _allowed_states:
        raise DockerSandboxError("sandbox_state_invalid")
    if (
        not isinstance(target_argv, (list, tuple))
        or not target_argv
        or any(
            type(item) is not str or not item or "\x00" in item for item in target_argv
        )
        or type(timeout) is not int
        or timeout <= 0
        or timeout > MAX_PRODUCT_TIMEOUT
        or _SHA256_RE.fullmatch(client_identity_digest or "") is None
    ):
        raise DockerSandboxError("execution_plan_invalid")
    workspace = (
        session.workspace_view.physical_root
        if session.state == "ready"
        else WorkspaceView(Path(session.manifest["execution"]["root"])).physical_root
    )
    workspace_info = workspace.lstat()
    if "," in str(workspace):
        raise DockerSandboxError("sandbox_workspace_unsupported")
    call_id = "call_" + secrets.token_hex(16)
    token = secrets.token_hex(32)
    labels = {
        **image.label_map,
        "io.pony.runtime.call": call_id,
        "io.pony.runtime.image": image.image_digest,
        "io.pony.runtime.managed": "true",
        "io.pony.runtime.policy": image.policy_digest,
        "io.pony.runtime.sandbox": session.sandbox_id,
        "io.pony.runtime.token": token,
    }
    logical_payload = {
        "image": image.image_digest,
        "logical_cwd": "/workspace",
        "policy_digest": image.policy_digest,
        "target_argv": list(target_argv),
    }
    computed_logical_digest = _sha256(_canonical_json(logical_payload))
    if (
        logical_intent_digest is not None
        and logical_intent_digest != computed_logical_digest
    ):
        raise DockerSandboxError("approved_execution_changed")
    logical_digest = computed_logical_digest
    values = {
        "sandbox_id": session.sandbox_id,
        "call_id": call_id,
        "reconciliation_token": token,
        "container_name": "pony-sandbox-" + call_id[5:] + "-" + token[:12],
        "image_digest": image.image_digest,
        "image_id": image.image_id,
        "workspace": str(workspace),
        "workspace_device": workspace_info.st_dev,
        "workspace_inode": workspace_info.st_ino,
        "target_argv": tuple(target_argv),
        "user": image.user,
        "labels": tuple(sorted(labels.items())),
        "env": image.env,
        "timeout": timeout,
        "policy_digest": image.policy_digest,
        "client_identity_digest": client_identity_digest,
        "logical_intent_digest": logical_digest,
    }
    temporary = DockerExecutionPlan(**values, execution_plan_digest="")
    return DockerExecutionPlan(
        **values,
        execution_plan_digest=_sha256(_canonical_json(temporary.digest_payload())),
    )


def compile_create_argv(plan):
    plan.verify()
    mount = (
        f"type=bind,src={plan.workspace},dst=/workspace,"
        "bind-propagation=rprivate,bind-recursive=disabled"
    )
    argv = [
        "create",
        "--pull=never",
        f"--name={plan.container_name}",
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
        "--hostname=pony-sandbox",
        f"--user={plan.user}",
        "--workdir=/workspace",
        "--mount",
        mount,
        "--tmpfs=/tmp:" + DOCKER_POLICY["tmpfs"]["/tmp"],
        "--tmpfs=/home/pony:" + DOCKER_POLICY["tmpfs"]["/home/pony"],
        "--tmpfs=/run:" + DOCKER_POLICY["tmpfs"]["/run"],
    ]
    argv.extend("--env=" + item for item in plan.env)
    argv.extend(f"--label={key}={value}" for key, value in plan.labels)
    argv.append(plan.image_digest)
    argv.extend(plan.target_argv)
    return argv


def verify_container_inspect(payload, plan, *, expected_id=None):
    try:
        host = payload["HostConfig"]
        config = payload["Config"]
        mounts = payload["Mounts"]
        host_mounts = host["Mounts"]
        ulimits = {
            item["Name"]: (item["Soft"], item["Hard"]) for item in host["Ulimits"]
        }
        mount = mounts[0]
        host_mount = host_mounts[0]
        descriptor = payload.get("ImageManifestDescriptor", {})
        networks = payload["NetworkSettings"]["Networks"]
        valid = (
            isinstance(payload["Id"], str)
            and _HEX64_RE.fullmatch(payload["Id"]) is not None
            and (expected_id is None or payload["Id"] == expected_id)
            and payload["Name"] == "/" + plan.container_name
            and payload["Image"] == plan.image_id
            and descriptor.get("digest") == plan.image_digest
            and descriptor.get("annotations", {}).get("config.digest") == plan.image_id
            and payload["Path"] == plan.target_argv[0]
            and payload["Args"] == list(plan.target_argv[1:])
            and config.get("Entrypoint") in (None, [])
            and config.get("Volumes") in (None, {})
            and config.get("ExposedPorts") in (None, {})
            and config.get("Healthcheck") is None
            and config["Hostname"] == "pony-sandbox"
            and config["User"] == plan.user
            and len(config["Env"]) == len(plan.env)
            and sorted(config["Env"]) == sorted(plan.env)
            and config["WorkingDir"] == "/workspace"
            and config["Labels"] == plan.label_map
            and host["Binds"] is None
            and host["NetworkMode"] == "none"
            and host["ReadonlyRootfs"] is True
            and host["Privileged"] is False
            and host["CapAdd"] in (None, [])
            and host["CapDrop"] == ["ALL"]
            and host["SecurityOpt"] == ["no-new-privileges:true"]
            and host["PidsLimit"] == DOCKER_POLICY["pids_limit"]
            and host["Memory"] == DOCKER_POLICY["memory_bytes"]
            and host["MemorySwap"] == DOCKER_POLICY["memory_swap_bytes"]
            and host["NanoCpus"] == DOCKER_POLICY["nano_cpus"]
            and host["ShmSize"] == DOCKER_POLICY["shm_bytes"]
            and ulimits == {"nofile": (1024, 1024), "core": (0, 0)}
            and host["LogConfig"] == {"Type": "none", "Config": {}}
            and host["Tmpfs"] == DOCKER_POLICY["tmpfs"]
            and len(host_mounts) == 1
            and host_mount["Type"] == "bind"
            and host_mount["Source"] == plan.workspace
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
            and mount["Source"] == plan.workspace
            and mount["Destination"] == "/workspace"
            and mount["RW"] is True
            and mount["Propagation"] == "rprivate"
            and set(networks) == {"none"}
        )
    except (KeyError, IndexError, TypeError, ValueError):
        valid = False
    if not valid:
        raise DockerSandboxError("container_contract_mismatch")


def verify_cleanup_identity(payload, plan, container_id):
    try:
        valid = (
            payload["Id"] == container_id
            and payload["Image"] == plan.image_id
            and payload["Name"] == "/" + plan.container_name
            and payload["Config"]["Labels"] == plan.label_map
            and payload["ImageManifestDescriptor"]["digest"]
            == plan.image_digest
            and payload["ImageManifestDescriptor"]
            .get("annotations", {})
            .get("config.digest")
            == plan.image_id
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise DockerSandboxError("container_cleanup_identity_mismatch")


@dataclass(frozen=True)
class DockerExecutionOutcome:
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    exit_code: int | None
    timed_out: bool
    runner_executed: bool
    target_started: bool
    container_created: bool
    sandbox_outcome: str
    cleanup_status: str
    residue_detected: bool
    error_code: str


def _empty_outcome(
    error_code,
    *,
    cleanup_status="completed",
    residue=False,
    container_created=False,
):
    return DockerExecutionOutcome(
        stdout=b"",
        stderr=b"",
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        exit_code=None,
        timed_out=False,
        runner_executed=False,
        target_started=False,
        container_created=container_created,
        sandbox_outcome="target_not_started",
        cleanup_status=cleanup_status,
        residue_detected=residue,
        error_code=error_code,
    )


def _command_succeeded(result):
    return (
        not result.timed_out
        and result.exit_code == 0
        and not result.stdout_truncated
        and not result.stderr_truncated
    )


def _parse_container_id(result):
    if not _command_succeeded(result):
        return ""
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    return value if _HEX64_RE.fullmatch(value) is not None else ""


def _target_started(state):
    return bool(
        isinstance(state, dict)
        and state.get("StartedAt")
        and not str(state["StartedAt"]).startswith("0001-")
        and not state.get("Error")
    )


def _target_start_state(state):
    if _target_started(state):
        return True
    if (
        isinstance(state, dict)
        and state.get("StartedAt")
        and str(state["StartedAt"]).startswith("0001-")
        and state.get("FinishedAt")
        and str(state["FinishedAt"]).startswith("0001-")
        and state.get("Status") == "created"
        and state.get("Running") is False
        and state.get("Pid") == 0
        and state.get("Error") == ""
        and state.get("Dead") is False
        and state.get("Paused") is False
        and state.get("Restarting") is False
    ):
        return False
    return None


def _target_finished(state):
    return bool(
        isinstance(state, dict)
        and state.get("Running") is False
        and state.get("Status") in {"dead", "exited"}
        and state.get("FinishedAt")
        and not str(state["FinishedAt"]).startswith("0001-")
    )


def measure_workspace(root):
    root = Path(root)
    root_descriptor = -1
    entries = 0
    logical = 0
    allocated = 0
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        root_info = os.fstat(root_descriptor)

        def visit(directory_descriptor, depth):
            nonlocal entries, logical, allocated
            if depth > MAX_DEPTH:
                raise DockerSandboxError("sandbox_workspace_limit_exceeded")
            for name in os.listdir(directory_descriptor):
                entries += 1
                if entries > MAX_ENTRIES:
                    raise DockerSandboxError("sandbox_workspace_limit_exceeded")
                info = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if info.st_dev != root_info.st_dev:
                    raise DockerSandboxError("sandbox_workspace_limit_exceeded")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_descriptor,
                    )
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (
                            info.st_dev,
                            info.st_ino,
                        ):
                            raise DockerSandboxError("sandbox_workspace_limit_exceeded")
                        visit(child, depth + 1)
                    finally:
                        os.close(child)
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > MAX_FILE_BYTES
                ):
                    raise DockerSandboxError("sandbox_workspace_limit_exceeded")
                logical += info.st_size
                allocated += int(getattr(info, "st_blocks", 0)) * 512
                if logical > MAX_LOGICAL_BYTES or allocated > MAX_ALLOCATED_BYTES:
                    raise DockerSandboxError("sandbox_workspace_limit_exceeded")

        visit(root_descriptor, 0)
        current = root.lstat()
        if (current.st_dev, current.st_ino) != (
            root_info.st_dev,
            root_info.st_ino,
        ):
            raise DockerSandboxError("sandbox_workspace_limit_exceeded")
        return {
            "entries": entries,
            "logical_bytes": logical,
            "allocated_bytes": allocated,
        }
    except DockerSandboxError:
        raise
    except OSError as exc:
        raise DockerSandboxError("sandbox_workspace_limit_exceeded") from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _next_watchdog_interval(scan_duration):
    return min(
        _WATCHDOG_MAX_INTERVAL,
        max(
            _WATCHDOG_MIN_INTERVAL,
            float(scan_duration) * _WATCHDOG_SCAN_MULTIPLIER,
        ),
    )


class DockerSandboxRunner:
    def __init__(
        self,
        client,
        session_store,
        image,
        *,
        workspace_probe=measure_workspace,
        watchdog_interval=_WATCHDOG_MIN_INTERVAL,
    ):
        self.client = client
        self.session_store = session_store
        self.image = image
        self.workspace_probe = workspace_probe
        self.watchdog_interval = float(watchdog_interval)

    def compile(
        self,
        session,
        target_argv,
        *,
        timeout=MAX_PRODUCT_TIMEOUT,
        logical_intent_digest=None,
    ):
        return compile_execution_plan(
            session,
            self.image,
            self.client.identity_digest(),
            target_argv,
            timeout=timeout,
            logical_intent_digest=logical_intent_digest,
        )

    def _persist_call_plan(self, state_root, plan):
        state_root = Path(state_root)
        try:
            record = _call_plan_record(plan)
            _execution_plan_from_record(record)
            raw = _canonical_json(record)
            if len(raw) > MAX_CALL_PLAN_BYTES:
                raise DockerSandboxError("sandbox_call_plan_invalid")
            securitylib.write_private_bytes_atomic(
                state_root / _CALL_PLAN_NAME,
                raw,
                trusted_root=state_root,
                trusted_root_identity=securitylib.private_directory_identity(
                    state_root
                ),
                max_existing_bytes=MAX_CALL_PLAN_BYTES,
            )
        except DockerSandboxError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise DockerSandboxError("sandbox_call_plan_invalid") from exc

    def _load_call_plan(self, state_root):
        state_root = Path(state_root)
        try:
            raw = securitylib.read_private_bytes(
                state_root / _CALL_PLAN_NAME,
                trusted_root=state_root,
                trusted_root_identity=securitylib.private_directory_identity(
                    state_root
                ),
                max_bytes=MAX_CALL_PLAN_BYTES,
            )
            return _execution_plan_from_record(
                _decode_json(raw, error_code="sandbox_call_plan_invalid")
            )
        except DockerSandboxError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise DockerSandboxError("sandbox_call_plan_invalid") from exc

    def _verify_reconciliation_plan(self, session, plan):
        active = session.manifest.get("active_call")
        required_labels = {
            "io.pony.runtime.call": plan.call_id,
            "io.pony.runtime.image": plan.image_digest,
            "io.pony.runtime.managed": "true",
            "io.pony.runtime.policy": plan.policy_digest,
            "io.pony.runtime.sandbox": plan.sandbox_id,
            "io.pony.runtime.token": plan.reconciliation_token,
        }
        session_image = session.manifest["image"]
        session_execution = session.manifest["execution"]
        if (
            session.state not in {"running", "review_required"}
            or not isinstance(active, dict)
            or plan.sandbox_id != session.sandbox_id
            or plan.call_id != active["call_id"]
            or plan.reconciliation_token != active["reconciliation_token"]
            or plan.container_name != active["container_name"]
            or plan.container_name
            != "pony-sandbox-" + plan.call_id[5:] + "-" + plan.reconciliation_token[:12]
            or plan.label_map != active["expected_labels"]
            or any(
                plan.label_map.get(key) != value
                for key, value in required_labels.items()
            )
            or plan.execution_plan_digest != active["plan_digest"]
            or plan.image_digest != session_image["image_digest"]
            or plan.image_id != session_image["image_id"]
            or plan.policy_digest != session.manifest["policy"]["digest"]
            or plan.client_identity_digest
            != session.manifest["engine"]["endpoint_hash"]
            or plan.workspace != session_execution["root"]
            or (plan.workspace_device, plan.workspace_inode)
            != (session_execution["device"], session_execution["inode"])
        ):
            raise DockerSandboxError("sandbox_call_plan_invalid")

    def _verify_plan_identity(
        self,
        session,
        plan,
        *,
        error_code,
        require_current_client=True,
        require_live_workspace=True,
    ):
        workspace = Path(plan.workspace)
        workspace_identity = (plan.workspace_device, plan.workspace_inode)
        if require_live_workspace:
            try:
                workspace_info = workspace.lstat()
                workspace_identity = (workspace_info.st_dev, workspace_info.st_ino)
            except OSError as exc:
                raise DockerSandboxError(error_code) from exc
        expected_labels = {
            **self.image.label_map,
            "io.pony.runtime.call": plan.call_id,
            "io.pony.runtime.image": self.image.image_digest,
            "io.pony.runtime.managed": "true",
            "io.pony.runtime.policy": self.image.policy_digest,
            "io.pony.runtime.sandbox": plan.sandbox_id,
            "io.pony.runtime.token": plan.reconciliation_token,
        }
        expected_image = {
            "image_digest": self.image.image_digest,
            "image_id": self.image.image_id,
            "platform": self.image.platform,
        }
        expected_policy = {
            "version": DOCKER_POLICY["version"],
            "digest": self.image.policy_digest,
            "network": DOCKER_POLICY["network"],
            "mount_digest": MOUNT_POLICY_DIGEST,
            "resource_digest": RESOURCE_POLICY_DIGEST,
        }
        if (
            plan.sandbox_id != session.sandbox_id
            or plan.container_name
            != "pony-sandbox-" + plan.call_id[5:] + "-" + plan.reconciliation_token[:12]
            or plan.label_map != expected_labels
            or plan.image_digest != self.image.image_digest
            or plan.image_id != self.image.image_id
            or plan.user != self.image.user
            or plan.env != self.image.env
            or plan.policy_digest != self.image.policy_digest
            or require_current_client
            and plan.client_identity_digest != self.client.identity_digest()
            or session.manifest["image"] != expected_image
            or session.manifest["policy"] != expected_policy
            or session.manifest["execution"]["root"] != plan.workspace
            or (
                session.manifest["execution"]["device"],
                session.manifest["execution"]["inode"],
            )
            != (plan.workspace_device, plan.workspace_inode)
            or workspace_identity != (plan.workspace_device, plan.workspace_inode)
        ):
            raise DockerSandboxError(error_code)

    def reconcile_session(self, session):
        try:
            current = self.session_store.inspect(session.state_root)
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc
        if current.manifest.get("active_call") is None:
            return current
        if (
            current.manifest["active_call"]["reconciliation"]["status"]
            == "review_required"
            and current.manifest["active_call"]["reconciliation"]["cleanup_status"]
            == "completed"
        ):
            return current
        try:
            plan = self._load_call_plan(current.state_root)
            self._verify_reconciliation_plan(current, plan)
            reconciled, absence_confirmed = self._reconcile_create(
                current.state_root,
                plan,
                preserve_absent=True,
            )
        except (DockerSandboxError, SandboxSessionError):
            return self._record_reconciliation(
                current.state_root,
                target_started=None,
                cleanup_status="not_attempted",
                error_code="target_start_state_unknown",
            )
        active = reconciled.manifest.get("active_call")
        if not active:
            return reconciled
        if reconciled.state != "running":
            previous = active["reconciliation"]
            previous_started = (
                previous["target_started"]
                if previous["status"] == "review_required"
                else None
            )
            return self._record_reconciliation(
                current.state_root,
                target_started=previous_started,
                cleanup_status=("completed" if absence_confirmed else "not_attempted"),
                error_code=(
                    "target_started_before_reconciliation"
                    if previous_started is True
                    else "target_start_state_unknown"
                ),
            )
        container_id = str(active.get("container_id") or "")
        if not container_id:
            return self._record_reconciliation(
                current.state_root,
                target_started=None,
                cleanup_status="not_attempted",
                error_code="target_start_state_unknown",
            )
        try:
            payload = self._inspect(container_id)
            verify_cleanup_identity(payload, plan, container_id)
            target_started = _target_start_state(payload.get("State"))
        except DockerSandboxError:
            return self._record_reconciliation(
                current.state_root,
                target_started=None,
                cleanup_status="not_attempted",
                error_code="target_start_state_unknown",
            )
        if target_started is None:
            return self._record_reconciliation(
                current.state_root,
                target_started=None,
                cleanup_status="not_attempted",
                error_code="target_start_state_unknown",
            )
        if target_started:
            self._record_reconciliation(
                current.state_root,
                target_started=True,
                cleanup_status="pending",
                error_code="target_started_before_reconciliation",
            )
            self._stop(plan, container_id)
            cleaned = self._cleanup(plan, container_id)
            return self._record_reconciliation(
                current.state_root,
                target_started=True,
                cleanup_status="completed" if cleaned else "failed",
                error_code="target_started_before_reconciliation",
            )
        self._stop(plan, container_id)
        cleaned = self._cleanup(plan, container_id)
        return self._finish(current.state_root, review_required=not cleaned)

    def _record_reconciliation(
        self,
        state_root,
        *,
        target_started,
        cleanup_status,
        error_code,
    ):
        try:
            return self.session_store.record_call_reconciliation(
                state_root,
                target_started=target_started,
                cleanup_status=cleanup_status,
                error_code=error_code,
            )
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc

    def bootstrap_git(self, request):
        if not isinstance(request, SyntheticGitBootstrapRequest):
            raise DockerSandboxError("synthetic_git_bootstrap_invalid")
        try:
            session = self.session_store.inspect(request.state_root)
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc
        if (
            session.state != "creating"
            or session.sandbox_id != request.sandbox_id
            or session.manifest["execution"]["root"]
            != str(request.workspace_view.physical_root)
        ):
            raise DockerSandboxError("synthetic_git_bootstrap_invalid")
        paths_name = ".pony-bootstrap-paths-" + secrets.token_hex(12)
        paths_path = request.workspace_view.physical_root / paths_name
        descriptor = -1
        try:
            descriptor = os.open(
                paths_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            raw_paths = b"".join(
                path.encode("utf-8") + b"\x00" for path in request.tracked_paths
            )
            view = memoryview(raw_paths)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise DockerSandboxError("synthetic_git_bootstrap_failed")
                view = view[written:]
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        except DockerSandboxError:
            raise
        except OSError as exc:
            raise DockerSandboxError("synthetic_git_bootstrap_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        python = dict(self.image.tool_paths)["python"]
        script = """
import os
from pathlib import Path
import subprocess
import sys

root = Path('/workspace')
path_file = Path(sys.argv[1])
raw = path_file.read_bytes()
if raw and not raw.endswith(b'\\0'):
    raise SystemExit(91)
paths = [item.decode('utf-8') for item in raw.split(b'\\0') if item]
path_file.unlink()
env = {
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_OPTIONAL_LOCKS': '0',
    'GIT_TERMINAL_PROMPT': '0',
    'HOME': '/home/pony',
    'LANG': 'C.UTF-8',
    'LC_ALL': 'C.UTF-8',
    'PATH': '/usr/bin:/bin',
}
common = [
    '/usr/bin/git',
    '-c', 'core.hooksPath=/dev/null',
    '-c', 'core.fsmonitor=false',
    '-c', 'credential.helper=',
    '-c', 'protocol.ext.allow=never',
]
def run(args):
    return subprocess.run(
        [*common, *args], cwd=root, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
run(['init', '-b', 'pony-sandbox'])
for start in range(0, len(paths), 256):
    run(['add', '--', *paths[start:start + 256]])
run([
    '-c', 'user.name=Pony Sandbox',
    '-c', 'user.email=pony-sandbox@example.invalid',
    'commit', '--allow-empty', '--no-gpg-sign', '-m', 'Pony sandbox baseline',
])
head = run(['rev-parse', 'HEAD']).stdout.decode('ascii').strip()
print(head)
""".strip()
        plan = compile_execution_plan(
            session,
            self.image,
            self.client.identity_digest(),
            [python, "-c", script, "/workspace/" + paths_name],
            timeout=60,
            _allowed_states=("creating",),
        )
        outcome = self.execute(session, plan, expected_state="creating")
        if (
            outcome.sandbox_outcome != "completed"
            or outcome.exit_code != 0
            or outcome.cleanup_status != "completed"
            or paths_path.exists()
        ):
            raise DockerSandboxError("synthetic_git_bootstrap_failed")
        try:
            head = outcome.stdout.decode("ascii").strip().splitlines()[-1]
        except (UnicodeDecodeError, IndexError) as exc:
            raise DockerSandboxError("synthetic_git_bootstrap_failed") from exc
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
            raise DockerSandboxError("synthetic_git_bootstrap_failed")
        return head

    def _inspect(self, container_id):
        result = self.client.command(
            ["container", "inspect", container_id, "--format", "{{json .}}"]
        )
        if (
            result.timed_out
            or result.exit_code != 0
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise DockerSandboxError("container_runtime_failed")
        payload = _decode_json(result.stdout)
        if not isinstance(payload, dict):
            raise DockerSandboxError("container_runtime_failed")
        return payload

    def _find(self, plan):
        args = ["container", "ls", "--all", "--quiet", "--no-trunc"]
        for key in (
            "io.pony.runtime.call",
            "io.pony.runtime.sandbox",
            "io.pony.runtime.token",
        ):
            args.extend(("--filter", f"label={key}={plan.label_map[key]}"))
        result = self.client.command(args)
        if (
            result.timed_out
            or result.exit_code != 0
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise DockerSandboxError("container_reconciliation_failed")
        try:
            ids = [line for line in result.stdout.decode("ascii").splitlines() if line]
        except UnicodeDecodeError as exc:
            raise DockerSandboxError("container_reconciliation_failed") from exc
        if any(_HEX64_RE.fullmatch(item) is None for item in ids):
            raise DockerSandboxError("container_reconciliation_failed")
        matches = []
        for container_id in ids:
            try:
                payload = self._inspect(container_id)
                verify_container_inspect(payload, plan, expected_id=container_id)
                contract_matches = True
            except DockerSandboxError:
                payload = {}
                contract_matches = False
            matches.append(
                {
                    "id": container_id,
                    "name": str(payload.get("Name", "")).removeprefix("/"),
                    "labels": payload.get("Config", {}).get("Labels", {}),
                    "contract_matches": contract_matches,
                }
            )
        return matches

    def _confirm_container_absent(self, container_id):
        result = self.client.command(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "id=" + container_id,
            ]
        )
        if (
            result.timed_out
            or result.exit_code != 0
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise DockerSandboxError("container_reconciliation_failed")
        return result.stdout == b""

    def _cleanup(self, plan, container_id):
        try:
            payload = self._inspect(container_id)
            verify_cleanup_identity(payload, plan, container_id)
            removed = self.client.command(
                ["container", "rm", "--force", container_id],
                timeout=15,
            )
            if not _command_succeeded(removed):
                return False
            remaining = self.client.command(
                [
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    "id=" + container_id,
                ]
            )
            return _command_succeeded(remaining) and remaining.stdout == b""
        except DockerSandboxError:
            return False

    def _stop(self, plan, container_id):
        try:
            verify_container_inspect(
                self._inspect(container_id),
                plan,
                expected_id=container_id,
            )
            stopped = self.client.command(
                ["container", "stop", "--signal=TERM", "--time=2", container_id],
                timeout=10,
            )
            if not _command_succeeded(stopped):
                killed = self.client.command(
                    ["container", "kill", "--signal=KILL", container_id],
                    timeout=10,
                )
                return _command_succeeded(killed)
        except DockerSandboxError:
            return False
        return True

    def _kill(self, plan, container_id):
        try:
            verify_container_inspect(
                self._inspect(container_id),
                plan,
                expected_id=container_id,
            )
            killed = self.client.command(
                ["container", "kill", "--signal=KILL", container_id],
                timeout=10,
            )
            return _command_succeeded(killed)
        except DockerSandboxError:
            return False

    def _reconcile_create(self, state_root, plan, *, preserve_absent=False):
        absence = {"confirmed": False}

        def confirm_container_absent(container_id):
            absence["confirmed"] = self._confirm_container_absent(container_id)
            return absence["confirmed"]

        session = self.session_store.reconcile_active_call(
            state_root,
            lambda _active: self._find(plan),
            confirm_container_absent=confirm_container_absent,
            preserve_absent=preserve_absent,
        )
        return session, absence["confirmed"]

    def _recover_failed_create(self, state_root, plan, error_code):
        try:
            reconciled, _absence_confirmed = self._reconcile_create(
                state_root,
                plan,
            )
        except DockerSandboxError:
            self._finish(state_root, review_required=True)
            return _empty_outcome(
                error_code,
                cleanup_status="failed",
                residue=True,
            )
        active = reconciled.manifest.get("active_call")
        container_id = str((active or {}).get("container_id") or "")
        if not container_id:
            return _empty_outcome(
                error_code,
                cleanup_status=(
                    "failed" if reconciled.state == "review_required" else "completed"
                ),
                residue=reconciled.state == "review_required",
            )
        cleaned = self._cleanup(plan, container_id)
        self._finish(state_root, review_required=not cleaned)
        return _empty_outcome(
            error_code,
            cleanup_status="completed" if cleaned else "failed",
            residue=not cleaned,
            container_created=True,
        )

    def _finish(self, state_root, *, review_required):
        try:
            current = self.session_store.inspect(state_root)
            if current.state == "running":
                return self.session_store.finish_call(
                    state_root,
                    review_required=review_required,
                )
            return current
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc

    @staticmethod
    def _join_watchdog(thread, stop):
        stop.set()
        if thread is None:
            return True
        thread.join(timeout=5)
        return not thread.is_alive()

    def execute(self, session, plan, *, expected_state="ready"):
        plan.verify()
        if plan.client_identity_digest != self.client.identity_digest():
            raise DockerSandboxError("approved_execution_changed")
        self.client.require_ready(self.image)
        try:
            current = self.session_store.inspect(session.state_root)
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc
        workspace = Path(plan.workspace)
        if current.state != expected_state:
            raise DockerSandboxError("approved_execution_changed")
        self._verify_plan_identity(
            current,
            plan,
            error_code="approved_execution_changed",
        )
        self.workspace_probe(workspace)
        self._persist_call_plan(current.state_root, plan)
        try:
            self.session_store.begin_call(
                current.state_root,
                call_id=plan.call_id,
                reconciliation_token=plan.reconciliation_token,
                container_name=plan.container_name,
                expected_labels=plan.label_map,
                plan_digest=plan.execution_plan_digest,
                return_state=expected_state,
            )
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc
        container_id = ""
        start_result = None
        terminal = None
        interrupted = None
        error_code = ""
        watchdog_stop = threading.Event()
        watchdog_violation = threading.Event()
        watchdog = None
        watchdog_joined = True

        def watch_workspace():
            interval = max(0.01, self.watchdog_interval)
            while not watchdog_stop.wait(interval):
                started_at = time.monotonic()
                try:
                    self.workspace_probe(workspace)
                except BaseException:
                    watchdog_violation.set()
                    self._kill(plan, container_id)
                    return
                interval = _next_watchdog_interval(time.monotonic() - started_at)

        try:
            try:
                created = self.client.command(
                    compile_create_argv(plan),
                    timeout=MAX_PRODUCT_TIMEOUT,
                )
            except DockerSandboxError as exc:
                return self._recover_failed_create(
                    current.state_root,
                    plan,
                    exc.code,
                )
            container_id = _parse_container_id(created)
            if not container_id:
                return self._recover_failed_create(
                    current.state_root,
                    plan,
                    "container_create_failed",
                )
            else:
                self.session_store.record_container_id(
                    current.state_root,
                    container_id,
                )
            verify_container_inspect(
                self._inspect(container_id),
                plan,
                expected_id=container_id,
            )
            watchdog = threading.Thread(target=watch_workspace, daemon=True)
            watchdog.start()
            try:
                start_result = self.client.command(
                    ["container", "start", "--attach", container_id],
                    timeout=plan.timeout,
                    max_bytes=MAX_OUTPUT_BYTES,
                )
            except KeyboardInterrupt as exc:
                interrupted = exc
                self._kill(plan, container_id)
            finally:
                watchdog_joined = self._join_watchdog(watchdog, watchdog_stop)
                if not watchdog_joined:
                    watchdog_violation.set()
                    self._kill(plan, container_id)
            if start_result is not None and start_result.timed_out:
                self._kill(plan, container_id)
            terminal = self._inspect(container_id)
        except DockerSandboxError as exc:
            error_code = exc.code
            if start_result is None and watchdog is not None:
                try:
                    terminal = self._inspect(container_id)
                except DockerSandboxError:
                    pass
        except BaseException:
            watchdog_joined = (
                self._join_watchdog(watchdog, watchdog_stop) and watchdog_joined
            )
            cleaned = (
                bool(container_id)
                and watchdog_joined
                and self._cleanup(plan, container_id)
            )
            self._finish(
                current.state_root,
                review_required=not cleaned or not watchdog_joined,
            )
            raise
        if container_id and watchdog_joined:
            try:
                self.workspace_probe(workspace)
            except BaseException:
                watchdog_violation.set()
                error_code = "sandbox_workspace_limit_exceeded"
        cleaned = (
            bool(container_id) and watchdog_joined and self._cleanup(plan, container_id)
        )
        self._finish(current.state_root, review_required=not cleaned)
        if interrupted is None and terminal is None:
            return _empty_outcome(
                error_code or "container_runtime_failed",
                cleanup_status="completed" if cleaned else "failed",
                residue=not cleaned,
                container_created=bool(container_id),
            )
        state = terminal.get("State") if isinstance(terminal, dict) else None
        started = _target_started(state)
        exit_code = state.get("ExitCode") if isinstance(state, dict) else None
        if type(exit_code) is not int:
            exit_code = None
        timed_out = bool(start_result and start_result.timed_out)
        if interrupted is not None:
            outcome = "interrupted"
            error_code = "sandbox_interrupted"
        elif timed_out:
            outcome = "timeout"
            error_code = "sandbox_timeout"
        elif started and state.get("OOMKilled") is True:
            outcome = "oom_killed"
            error_code = "sandbox_oom_killed"
        elif watchdog_violation.is_set():
            outcome = "container_runtime_failed"
            error_code = "sandbox_workspace_limit_exceeded"
        elif error_code:
            outcome = "container_runtime_failed" if started else "target_not_started"
        elif started and _target_finished(state) and exit_code is not None:
            outcome = "completed"
        elif not started:
            outcome = "target_not_started"
            error_code = error_code or "target_not_started"
        else:
            outcome = "container_runtime_failed"
            error_code = error_code or "container_runtime_failed"
        result = DockerExecutionOutcome(
            stdout=start_result.stdout if start_result else b"",
            stderr=start_result.stderr if start_result else b"",
            stdout_bytes=start_result.stdout_bytes if start_result else 0,
            stderr_bytes=start_result.stderr_bytes if start_result else 0,
            stdout_truncated=bool(start_result and start_result.stdout_truncated),
            stderr_truncated=bool(start_result and start_result.stderr_truncated),
            exit_code=exit_code,
            timed_out=timed_out,
            runner_executed=(
                start_result is not None or interrupted is not None or started
            ),
            target_started=started,
            container_created=True,
            sandbox_outcome=outcome,
            cleanup_status="completed" if cleaned else "failed",
            residue_detected=not cleaned,
            error_code=error_code,
        )
        if interrupted is not None:
            interrupted.docker_sandbox_outcome = result
            raise interrupted
        return result


@dataclass(frozen=True)
class DockerSandboxContext:
    source_root: Path
    execution_root: Path
    project_state_root: Path
    sandbox_state_root: Path
    workspace_view: WorkspaceView
    sandbox_session: SandboxSession
    runner: DockerSandboxRunner
    readiness: MappingProxyType
    authorization: DockerSandboxRuntimeAuthorization
    resumed: bool = False
    source_branch: str = "-"
    source_status: str = "(unavailable)"
    source_default_branch: str = "main"

    def __post_init__(self):
        source_root = Path(os.path.abspath(os.fspath(self.source_root)))
        execution_root = Path(os.path.abspath(os.fspath(self.execution_root)))
        project_state_root = Path(os.path.abspath(os.fspath(self.project_state_root)))
        sandbox_state_root = Path(os.path.abspath(os.fspath(self.sandbox_state_root)))
        try:
            authorization = self.authorization.verify(self.runner.image)
        except (AttributeError, DockerSandboxError) as exc:
            raise DockerSandboxError("sandbox_context_invalid") from exc
        if (
            type(self.resumed) is not bool
            or execution_root != self.workspace_view.physical_root
            or sandbox_state_root != self.sandbox_session.state_root
            or self.sandbox_session.state != "ready"
            or self.sandbox_session.manifest["source"]["root"] != str(source_root)
            or self.sandbox_session.manifest["execution"]["root"] != str(execution_root)
            or self.sandbox_session.manifest["sidecar"] is None
            or Path(self.sandbox_session.manifest["sidecar"]["path"]).parent
            != project_state_root / "sandbox_sessions"
            or self.sandbox_session.manifest["image"]["image_digest"]
            != authorization.image_digest
            or self.sandbox_session.manifest["image"]["image_id"]
            != authorization.image_id
            or self.sandbox_session.manifest["image"]["platform"]
            != authorization.image_platform
            or self.sandbox_session.manifest["policy"]["digest"]
            != authorization.policy_digest
        ):
            raise DockerSandboxError("sandbox_context_invalid")
        object.__setattr__(self, "source_root", source_root)
        object.__setattr__(self, "execution_root", execution_root)
        object.__setattr__(self, "project_state_root", project_state_root)
        object.__setattr__(self, "sandbox_state_root", sandbox_state_root)
        object.__setattr__(self, "readiness", MappingProxyType(dict(self.readiness)))

    @property
    def logical_root(self):
        return self.workspace_view.logical_root.as_posix()

    @property
    def source_apply_state_root(self):
        return self.sandbox_state_root

    def current_session(self):
        try:
            return self.runner.session_store.inspect(self.sandbox_state_root)
        except SandboxSessionError as exc:
            raise DockerSandboxError("sandbox_state_invalid") from exc


def _sandbox_manifest_metadata(client, image, readiness):
    endpoint_digest = getattr(client, "endpoint_digest", None)
    endpoint_hash = (
        endpoint_digest() if callable(endpoint_digest) else client.identity_digest()
    )
    return (
        {
            "endpoint_hash": endpoint_hash,
            "client_version": readiness["client_version"],
            "server_version": readiness["server_version"],
            "api_version": readiness["api_version"],
            "profile": readiness["platform_profile"],
            "security_digest": _sha256(_canonical_json(readiness["security"])),
        },
        {
            "image_digest": image.image_digest,
            "image_id": image.image_id,
            "platform": image.platform,
        },
        {
            "version": DOCKER_POLICY["version"],
            "digest": image.policy_digest,
            "network": DOCKER_POLICY["network"],
            "mount_digest": MOUNT_POLICY_DIGEST,
            "resource_digest": RESOURCE_POLICY_DIGEST,
        },
    )


def _resume_sandbox_session(
    store,
    source_root,
    pony_session_id,
    *,
    engine,
    image,
    policy,
):
    matches = [
        manifest
        for manifest in store.list()
        if manifest["pony_session_id"] == pony_session_id
        and manifest["source"]["root"] == str(source_root)
    ]
    if len(matches) != 1:
        raise DockerSandboxError(
            "sandbox_session_not_found" if not matches else "sandbox_state_invalid"
        )
    manifest = matches[0]
    session = store.inspect(Path(manifest["execution"]["root"]).parent)
    source_info = source_root.lstat()
    if (
        session.state != "ready"
        or (source_info.st_dev, source_info.st_ino)
        != (manifest["source"]["device"], manifest["source"]["inode"])
        or manifest["engine"] != engine
        or manifest["image"] != image
        or manifest["policy"] != policy
    ):
        raise DockerSandboxError("sandbox_resume_invalid")
    try:
        return store.acquire(session.state_root)
    except SandboxSessionError as exc:
        raise DockerSandboxError(exc.code) from exc


def build_docker_sandbox_context(
    source_root,
    *,
    authorization,
    pony_session_id,
    docker_cli,
    docker_endpoint,
    project_state_root=None,
    sandbox_parent=None,
    docker_config=None,
    image_manifest_path=None,
    image=None,
    git_executable=None,
    known_secrets=(),
    resume=False,
    source_branch=None,
    source_status="(unavailable)",
    source_default_branch="main",
):
    """Build the production D2+D3 context, with readiness before staging."""
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    project_state_root = Path(
        os.path.abspath(os.fspath(project_state_root or source_root / ".pony"))
    )
    sandbox_parent = Path(
        os.path.abspath(
            os.fspath(sandbox_parent or Path.home() / ".pony" / "sandboxes")
        )
    )
    if image is not None and image_manifest_path is not None:
        raise DockerSandboxError("sandbox_image_identity_mismatch")
    image = image or load_image_manifest(
        image_manifest_path or default_image_manifest_path()
    )
    if not isinstance(image, DockerImageManifest):
        raise DockerSandboxError("sandbox_image_identity_mismatch")
    if not isinstance(authorization, DockerSandboxRuntimeAuthorization):
        raise DockerSandboxError("sandbox_runtime_authorization_invalid")
    authorization.verify(image)
    store = SandboxSessionStore(sandbox_parent)
    try:
        with source_mutation_authority(store.parent, source_root):
            client = DockerClient(
                docker_cli,
                docker_endpoint,
                docker_config or runtime_docker_config_path(),
            )
            readiness = client.require_ready(image)
            runner = DockerSandboxRunner(client, store, image)
            engine, image_metadata, policy = _sandbox_manifest_metadata(
                client,
                image,
                readiness,
            )
            if source_apply_guard_present(source_root):
                raise CheckpointStoreError(
                    "source_apply_review_required", "source apply is unresolved"
                )
            if resume:
                session = _resume_sandbox_session(
                    store,
                    source_root,
                    str(pony_session_id),
                    engine=engine,
                    image=image_metadata,
                    policy=policy,
                )
            else:
                session = store.create(
                    source_root,
                    pony_session_id=str(pony_session_id),
                    bootstrap_git=runner.bootstrap_git,
                    git_executable=git_executable,
                    known_secrets=known_secrets,
                    engine=engine,
                    image=image_metadata,
                    policy=policy,
                    project_state_root=project_state_root,
                )
    except (CheckpointStoreError, SandboxSessionError) as exc:
        raise DockerSandboxError(exc.code) from exc
    view = session.workspace_view
    if source_branch is None:
        source_branch = session.manifest["source"]["branch"] or "-"
    return DockerSandboxContext(
        source_root=source_root,
        execution_root=view.physical_root,
        project_state_root=project_state_root,
        sandbox_state_root=session.state_root,
        workspace_view=view,
        sandbox_session=session,
        runner=runner,
        readiness=MappingProxyType(dict(readiness)),
        authorization=authorization,
        resumed=bool(resume),
        source_branch=str(source_branch),
        source_status=str(source_status or "(unavailable)"),
        source_default_branch=str(source_default_branch or "main"),
    )
