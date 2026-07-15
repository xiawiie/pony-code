"""Linux sandbox capability gate and bubblewrap adapter."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .safe_subprocess import (
    _execution_path,
    _prepared_executable,
    _verified_executable_identity,
    build_trusted_executables,
    run_hardened_command,
)
from .sandbox import (
    ApprovedExecution,
    consume_target_start_frame,
    new_target_start_token,
    SandboxContext,
    SandboxIdentity,
    SandboxOutcome,
    TARGET_START_ENV,
    TARGET_START_WRAPPER,
)

STATUSES = frozenset({"available", "missing", "blocked", "incompatible", "unknown"})
_SUPPORTED_MACHINES = {
    "x86_64": ("x86_64", "x64", 62),
    "amd64": ("x86_64", "x64", 62),
    "aarch64": ("arm64", "arm64", 183),
    "arm64": ("arm64", "arm64", 183),
}
_EXECUTABLE_NAMES = ("bwrap", "socat", "rg", "true", "test")
_SECCOMP_PROBE = """
const net = require('node:net');
const server = net.createServer();
server.on('error', error => process.exit(
  error.code === 'EPERM' || error.code === 'EACCES' ? 0 : 2));
server.listen('/tmp/pico-seccomp-probe.sock', () => {
  server.close(() => process.exit(3));
});
""".strip()
@dataclass(frozen=True)
class Capability:
    status: str
    reason: str

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"invalid capability status: {self.status}")


@dataclass(frozen=True)
class LinuxCapabilityReport:
    platform: str
    architecture: str
    applicability: str
    applicability_reason: str
    bwrap: Capability
    socat: Capability
    rg: Capability
    user_namespace: Capability
    mount_namespace: Capability
    network_namespace: Capability
    seccomp_architecture: Capability
    proc: Capability
    temp: Capability

    @property
    def status(self):
        if self.applicability != "applicable":
            return "not_applicable"
        capabilities = (
            self.bwrap,
            self.socat,
            self.rg,
            self.user_namespace,
            self.mount_namespace,
            self.network_namespace,
            self.seccomp_architecture,
            self.proc,
            self.temp,
        )
        return "ready" if all(value.status == "available" for value in capabilities) else "not_ready"

    def to_dict(self):
        return {**asdict(self), "status": self.status}

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True)


Runner = Callable[[Sequence[object], float], subprocess.CompletedProcess]


class _SeccompArchitectureMismatch(ValueError):
    pass


def _run(command: Sequence[object], timeout: float) -> subprocess.CompletedProcess:
    if not command or not Path(str(command[0])).is_absolute():
        raise ValueError("probe executable must be an absolute frozen identity")
    return run_hardened_command(
        command[0],
        args=command[1:],
        cwd=Path("/"),
        timeout=timeout,
        env={"PATH": ""},
    )


def resolve_linux_executables(*, workspace_root=None, env=None):
    """Resolve immutable absolute identities once; never execute through PATH."""
    return build_trusted_executables(
        Path.cwd() if workspace_root is None else workspace_root,
        env=os.environ if env is None else env,
        names=_EXECUTABLE_NAMES,
    )


def _probe_command(
    runner: Runner,
    command: Sequence[object],
    timeout: float,
    *,
    available: str,
    failed: str,
    failure_status: str = "blocked",
):
    try:
        result = runner(command, timeout)
    except (subprocess.TimeoutExpired, TimeoutError):
        return Capability("unknown", "probe_timeout")
    except FileNotFoundError:
        return Capability("missing", "missing_binary")
    except PermissionError:
        return Capability("blocked", "binary_not_executable")
    except (OSError, RuntimeError, ValueError):
        return Capability("unknown", "executable_identity_unverified")
    if result.returncode == 0:
        return Capability("available", available)
    if result.returncode in (126, 13):
        return Capability("blocked", "binary_not_executable")
    return Capability(failure_status, failed)


def _executable(runner: Runner, executable: object | None, args, identity: str, timeout: float):
    if executable is None:
        return Capability("missing", "missing_binary")
    if not Path(str(executable)).is_absolute():
        return Capability("incompatible", "executable_not_absolute")
    try:
        result = runner((executable, *args), timeout)
    except (subprocess.TimeoutExpired, TimeoutError):
        return Capability("unknown", "executable_identity_timeout")
    except FileNotFoundError:
        return Capability("missing", "missing_binary")
    except PermissionError:
        return Capability("blocked", "binary_not_executable")
    except (OSError, RuntimeError, ValueError):
        return Capability("unknown", "executable_identity_unverified")
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if result.returncode == 0 and identity in output:
        return Capability("available", "executable_identity_verified")
    if result.returncode in (126, 13):
        return Capability("blocked", "binary_not_executable")
    return Capability("incompatible", "executable_identity_mismatch")


def _unsupported_report(system: str, machine: str, reason: str):
    unavailable = Capability("unknown", reason)
    return LinuxCapabilityReport(
        platform=system,
        architecture=machine,
        applicability="not_applicable",
        applicability_reason=reason,
        bwrap=unavailable,
        socat=unavailable,
        rg=unavailable,
        user_namespace=unavailable,
        mount_namespace=unavailable,
        network_namespace=unavailable,
        seccomp_architecture=unavailable,
        proc=unavailable,
        temp=unavailable,
    )


def _bwrap_probe_argv(bwrap, target, *target_args):
    return (
        bwrap,
        "--new-session",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-ipc",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--chdir",
        "/tmp",
        "--",
        target,
        *target_args,
    )


def _userns_disabled(paths):
    for raw_path in paths:
        try:
            if Path(raw_path).read_text(encoding="ascii").strip() == "0":
                return True
        except OSError:
            continue
    return False


def _host_proc_capability(proc_path):
    try:
        path = Path(proc_path)
        if path.is_file() and os.access(path, os.R_OK):
            return None
    except OSError:
        pass
    return Capability("missing", "proc_unavailable")


def _host_temp_capability(temp_root):
    path = None
    try:
        path = Path(tempfile.mkdtemp(prefix="pico-linux-probe-", dir=temp_root))
        probe_file = path / "writable"
        descriptor = os.open(probe_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        probe_file.unlink()
        return None
    except OSError:
        return Capability("blocked", "temp_unusable")
    finally:
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)


def _seccomp_path(identity: SandboxIdentity, vendor_arch: str, elf_machine: int):
    if not identity.bundle_manifest_hash:
        raise ValueError("seccomp binary is not bound to a bundle manifest")
    identity.verify()
    root = identity.trusted_root.resolve(strict=True)
    path = (
        root
        / "node_modules"
        / "@anthropic-ai"
        / "sandbox-runtime"
        / "vendor"
        / "seccomp"
        / vendor_arch
        / "apply-seccomp"
    )
    resolved = path.resolve(strict=True)
    info = path.lstat()
    if (
        resolved != path
        or not resolved.is_relative_to(root)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        or not os.access(path, os.X_OK)
    ):
        raise ValueError("seccomp binary identity is unsafe")
    header = path.read_bytes()[:20]
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01":
        raise ValueError("seccomp binary is not a supported ELF64 image")
    if struct.unpack_from("<H", header, 18)[0] != elf_machine:
        raise _SeccompArchitectureMismatch("seccomp binary architecture mismatch")
    return path


def _seccomp_capability(
    runner,
    timeout,
    namespace,
    bwrap,
    identity,
    vendor_arch,
    elf_machine,
):
    if identity is None:
        return Capability("unknown", "seccomp_binary_unverified")
    try:
        path = _seccomp_path(identity, vendor_arch, elf_machine)
        node = identity.node_path.resolve(strict=True)
    except _SeccompArchitectureMismatch:
        return Capability("incompatible", "seccomp_arch_mismatch")
    except (OSError, ValueError):
        return Capability("unknown", "seccomp_binary_unverified")
    if namespace.status != "available" or bwrap is None:
        return Capability("unknown", "seccomp_not_probed")
    return _probe_command(
        runner,
        _bwrap_probe_argv(bwrap, path, node, "-e", _SECCOMP_PROBE),
        timeout,
        available="seccomp_enforcement_verified",
        failed="seccomp_enforcement_unavailable",
    )


def probe(
    *,
    runner: Runner = _run,
    timeout: float = 2.0,
    system: str | None = None,
    machine: str | None = None,
    executables: Mapping[str, object] | None = None,
    sandbox_identity: SandboxIdentity | None = None,
    proc_path: str | os.PathLike[str] = "/proc/self/status",
    temp_root: str | os.PathLike[str] | None = None,
    userns_paths: Sequence[str | os.PathLike[str]] = (
        "/proc/sys/kernel/unprivileged_userns_clone",
        "/proc/sys/user/max_user_namespaces",
    ),
) -> LinuxCapabilityReport:
    """Probe the exact Linux isolation composition without privilege changes."""
    if timeout <= 0:
        raise ValueError("probe timeout must be positive")
    system = platform.system() if system is None else system
    raw_machine = (platform.machine() if machine is None else machine).lower()
    if system != "Linux":
        return _unsupported_report(system, raw_machine, "unsupported_platform")
    architecture = _SUPPORTED_MACHINES.get(raw_machine)
    if architecture is None:
        return _unsupported_report(system, raw_machine, "unsupported_architecture")
    canonical_arch, vendor_arch, elf_machine = architecture
    resolved = (
        resolve_linux_executables()
        if executables is None
        else dict(executables)
    )
    bwrap_path = resolved.get("bwrap")
    bwrap = _executable(runner, bwrap_path, ("--version",), "bubblewrap", timeout)
    socat = _executable(runner, resolved.get("socat"), ("-V",), "socat", timeout)
    rg = _executable(runner, resolved.get("rg"), ("--version",), "ripgrep", timeout)

    if _userns_disabled(userns_paths):
        user_namespace = Capability("blocked", "userns_disabled")
        mount_namespace = Capability("unknown", "namespace_not_probed")
        network_namespace = Capability("unknown", "namespace_not_probed")
    elif bwrap.status != "available" or resolved.get("true") is None:
        user_namespace = Capability("unknown", "namespace_not_probed")
        mount_namespace = Capability("unknown", "namespace_not_probed")
        network_namespace = Capability("unknown", "namespace_not_probed")
    else:
        namespace = _probe_command(
            runner,
            _bwrap_probe_argv(bwrap_path, resolved["true"]),
            timeout,
            available="namespace_combination_verified",
            failed="namespace_denied",
        )
        user_namespace = namespace
        mount_namespace = namespace
        network_namespace = namespace

    host_proc = _host_proc_capability(proc_path)
    if host_proc is not None:
        proc = host_proc
    elif mount_namespace.status != "available" or resolved.get("test") is None:
        proc = Capability("unknown", "proc_not_probed")
    else:
        proc = _probe_command(
            runner,
            _bwrap_probe_argv(bwrap_path, resolved["test"], "-r", "/proc/self/status"),
            timeout,
            available="proc_mount_verified",
            failed="proc_unavailable",
            failure_status="missing",
        )

    host_temp = _host_temp_capability(temp_root)
    if host_temp is not None:
        temp = host_temp
    elif mount_namespace.status != "available" or resolved.get("test") is None:
        temp = Capability("unknown", "temp_not_probed")
    else:
        temp = _probe_command(
            runner,
            _bwrap_probe_argv(bwrap_path, resolved["test"], "-w", "/tmp"),
            timeout,
            available="temporary_mount_verified",
            failed="temp_unusable",
        )

    seccomp = _seccomp_capability(
        runner,
        timeout,
        user_namespace,
        bwrap_path,
        sandbox_identity,
        vendor_arch,
        elf_machine,
    )
    return LinuxCapabilityReport(
        platform=system,
        architecture=canonical_arch,
        applicability="applicable",
        applicability_reason="linux_supported_architecture",
        bwrap=bwrap,
        socat=socat,
        rg=rg,
        user_namespace=user_namespace,
        mount_namespace=mount_namespace,
        network_namespace=network_namespace,
        seccomp_architecture=seccomp,
        proc=proc,
        temp=temp,
    )


def _environment(execution: ApprovedExecution, call_root: Path, target_start_token: str):
    env = {str(key): str(value) for key, value in execution.env.items()}
    home, tmp, cache = call_root / "home", call_root / "tmp", call_root / "cache"
    for path in (home, tmp, cache):
        path.mkdir(mode=0o700)
    env.update(
        HOME=str(home),
        TMPDIR=str(tmp),
        TMP=str(tmp),
        TEMP=str(tmp),
        XDG_CACHE_HOME=str(cache),
        PWD=str(Path(execution.cwd).resolve(strict=True)),
        **{TARGET_START_ENV: target_start_token},
    )
    for key, value in env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
            raise ValueError("invalid approved environment")
    return env


def _create_placeholder(path: Path, *, directory: bool):
    try:
        info = path.lstat()
        created = False
    except FileNotFoundError:
        if directory:
            path.mkdir(mode=0o700)
        else:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
        info = path.lstat()
        created = True
    if stat.S_ISLNK(info.st_mode) or not (
        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
    ):
        raise ValueError("unsafe protected workspace path")
    return created, (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _validate_protected_tree(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (
        stat.S_ISREG(info.st_mode) and info.st_nlink != 1
    ):
        raise ValueError("protected workspace path has an alias")
    for candidate in path.rglob("*") if stat.S_ISDIR(info.st_mode) else ():
        item = candidate.lstat()
        if stat.S_ISLNK(item.st_mode) or (
            stat.S_ISREG(item.st_mode) and item.st_nlink != 1
        ):
            raise ValueError("protected workspace path has an alias")


def _protected_paths(workspace):
    protected = [(workspace / ".git", True, False), (workspace / ".pico", True, True)]
    protected.extend(
        (path, False, True)
        for path in workspace.iterdir()
        if path.name == ".env" or path.name.startswith(".env.")
    )
    if not any(path.name == ".env" for path, _, _ in protected):
        protected.append((workspace / ".env", False, True))
    return protected


def _protected_mounts(workspace: Path, call_root: Path):
    empty = call_root / "empty"
    empty.mkdir(mode=0o500)
    mounts = ["--ro-bind", str(empty), str(empty)]
    placeholders = []
    try:
        for path, missing_directory, deny_read in _protected_paths(workspace):
            created, identity = _create_placeholder(path, directory=missing_directory)
            if created:
                placeholders.append((path, identity))
            _validate_protected_tree(path)
            info = path.lstat()
            if deny_read:
                source = empty if stat.S_ISDIR(info.st_mode) else Path("/dev/null")
            else:
                source = path
            mounts.extend(("--ro-bind", str(source), str(path)))
    except BaseException:
        _cleanup_placeholders(placeholders)
        raise
    return mounts, placeholders


def _planned_protected_mounts(workspace: Path, call_root: Path):
    empty_directory = call_root / "plan-empty-directory"
    empty_file = call_root / "plan-empty-file"
    empty_directory.mkdir(mode=0o500)
    empty_file.touch(mode=0o400)
    mounts = [
        "--ro-bind",
        str(empty_directory),
        str(empty_directory),
        "--ro-bind",
        str(empty_file),
        str(empty_file),
    ]
    for path, missing_directory, deny_read in _protected_paths(workspace):
        try:
            info = path.lstat()
        except FileNotFoundError:
            source = empty_directory if missing_directory else empty_file
        else:
            _validate_protected_tree(path)
            if deny_read:
                source = empty_directory if stat.S_ISDIR(info.st_mode) else empty_file
            else:
                source = path
        mounts.extend(("--ro-bind", str(source), str(path)))
    return mounts


def _cleanup_placeholders(placeholders):
    clean = True
    for path, expected in reversed(placeholders):
        try:
            info = path.lstat()
            actual = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
            if actual != expected:
                clean = False
            elif stat.S_ISDIR(info.st_mode):
                path.rmdir()
            elif info.st_size == 0:
                path.unlink()
            else:
                clean = False
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
    return clean


def _process_group_exists(process_group: int):
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_residue(process_group: int, *, grace: float):
    if not _process_group_exists(process_group):
        return False
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process_group, signal_number)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            if not _process_group_exists(process_group):
                return False
            time.sleep(0.01)
    return _process_group_exists(process_group)


def _not_ready(report):
    reasons = sorted(
        {
            value.reason
            for value in (
                report.bwrap,
                report.socat,
                report.rg,
                report.user_namespace,
                report.mount_namespace,
                report.network_namespace,
                report.seccomp_architecture,
                report.proc,
                report.temp,
            )
            if value.status != "available"
        }
    )
    return SandboxOutcome(
        "",
        "linux sandbox not ready: " + ",".join(reasons),
        None,
        False,
        False,
        "failed",
        "sandbox_not_ready",
        "completed",
    )


def _build_linux_bwrap_plan(
    context,
    execution,
    resolved,
    call_root,
    *,
    machine,
    prepare_workspace,
):
    raw_machine = (platform.machine() if machine is None else machine).lower()
    if raw_machine not in _SUPPORTED_MACHINES:
        raise ValueError("unsupported Linux architecture")
    _, vendor_arch, elf_machine = _SUPPORTED_MACHINES[raw_machine]
    seccomp = _seccomp_path(context.identity, vendor_arch, elf_machine)
    if not execution.argv or not all(
        isinstance(argument, str) and "\0" not in argument for argument in execution.argv
    ):
        raise ValueError("approved argv must be non-empty strings")
    target = Path(str(execution.argv[0]))
    if not target.is_absolute():
        raise ValueError("approved executable must be absolute")
    expected = getattr(getattr(execution, "executable", None), "_identity", None)
    _verified_executable_identity(target, expected=expected)
    workspace = context.workspace_root.resolve(strict=True)
    cwd = Path(execution.cwd).resolve(strict=True)
    home = context.original_home.resolve(strict=True)
    toolchain = context.identity.trusted_root.resolve(strict=True)
    if not cwd.is_relative_to(workspace) or home == Path("/"):
        raise ValueError("sandbox execution path is unsafe")
    target_start_token = new_target_start_token()
    env_args = ["--clearenv"]
    for key, value in sorted(
        _environment(execution, call_root, target_start_token).items()
    ):
        env_args.extend(("--setenv", key, value))
    if prepare_workspace:
        protected, placeholders = _protected_mounts(workspace, call_root)
    else:
        protected, placeholders = _planned_protected_mounts(workspace, call_root), []
    bwrap_args = [
        "--new-session",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-ipc",
        "--ro-bind",
        "/",
        "/",
        "--tmpfs",
        str(home),
        "--bind",
        str(workspace),
        str(workspace),
        "--ro-bind",
        str(toolchain),
        str(toolchain),
        "--tmpfs",
        "/tmp",
        "--dir",
        str(call_root),
        "--bind",
        str(call_root),
        str(call_root),
        *protected,
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        *env_args,
        "--chdir",
        str(cwd),
        "--",
        str(seccomp),
        str(context.identity.node_path),
        "-e",
        TARGET_START_WRAPPER,
        *(str(argument) for argument in execution.argv),
    ]
    return bwrap_args, target_start_token, placeholders


def build_linux_sandbox_plan(
    context: SandboxContext,
    execution: ApprovedExecution,
    *,
    capability_report: LinuxCapabilityReport,
    executables: Mapping[str, object] | None = None,
    machine: str | None = None,
):
    """Validate and build one bwrap argv without spawning a target."""
    if capability_report.status != "ready":
        raise ValueError("linux sandbox capability report is not ready")
    resolved = (
        resolve_linux_executables(workspace_root=context.workspace_root)
        if executables is None
        else dict(executables)
    )
    call_root = Path(tempfile.mkdtemp(prefix="pico-linux-plan-"))
    os.chmod(call_root, 0o700)
    placeholders = []
    try:
        bwrap_args, _, placeholders = _build_linux_bwrap_plan(
            context,
            execution,
            resolved,
            call_root,
            machine=machine or capability_report.architecture,
            prepare_workspace=False,
        )
        with _prepared_executable(resolved["bwrap"]):
            pass
        return (str(resolved["bwrap"]), *bwrap_args)
    finally:
        placeholders_clean = _cleanup_placeholders(placeholders)
        try:
            shutil.rmtree(call_root)
        except OSError:
            placeholders_clean = False
        if not placeholders_clean:
            raise RuntimeError("linux sandbox plan cleanup failed")


def run_linux_sandbox(
    context: SandboxContext,
    execution: ApprovedExecution,
    *,
    runner: Runner = _run,
    launcher: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    executables: Mapping[str, object] | None = None,
    system: str | None = None,
    machine: str | None = None,
    capability_report: LinuxCapabilityReport | None = None,
    term_grace: float = 2.0,
    proc_path: str | os.PathLike[str] = "/proc/self/status",
    temp_root: str | os.PathLike[str] | None = None,
) -> SandboxOutcome:
    """Run one approved argv through bwrap and the bundled seccomp loader."""
    resolved = (
        resolve_linux_executables(workspace_root=context.workspace_root)
        if executables is None
        else dict(executables)
    )
    report = capability_report or probe(
        runner=runner,
        system=system,
        machine=machine,
        executables=resolved,
        sandbox_identity=context.identity,
        proc_path=proc_path,
        temp_root=temp_root,
    )
    if report.status != "ready":
        return _not_ready(report)

    process = None
    placeholders = []
    call_root = Path(tempfile.mkdtemp(prefix="pico-linux-sandbox-"))
    target_start_token = ""
    os.chmod(call_root, 0o700)
    result = SandboxOutcome("", "", None, False, False, "failed", "wrapper_failed", "completed")
    interrupted = None
    try:
        bwrap_args, target_start_token, placeholders = _build_linux_bwrap_plan(
            context,
            execution,
            resolved,
            call_root,
            machine=machine or report.architecture,
            prepare_workspace=True,
        )
        cwd = Path(execution.cwd).resolve(strict=True)
        with _prepared_executable(resolved["bwrap"]) as bwrap:
            process = launcher(
                [bwrap, *bwrap_args],
                executable=_execution_path(bwrap),
                cwd=str(cwd),
                env={"PATH": ""},
                shell=False,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        try:
            stdout, stderr = process.communicate(timeout=execution.timeout)
            stderr, started = consume_target_start_frame(stderr, target_start_token)
            result = SandboxOutcome(
                stdout,
                stderr,
                process.returncode,
                False,
                started,
                "completed" if started else "failed",
                "completed" if started else "target_not_started",
                "completed",
            )
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=term_grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
            stderr, started = consume_target_start_frame(stderr, target_start_token)
            result = SandboxOutcome(
                stdout,
                stderr,
                process.returncode,
                True,
                started,
                "completed",
                "timeout",
                "completed",
            )
    except BaseException as exc:  # cleanup must also run for interrupts
        if not isinstance(exc, Exception):
            interrupted = exc
        else:
            result = SandboxOutcome(
                "",
                str(exc),
                None,
                False,
                False,
                "failed",
                "wrapper_failed",
                "completed",
            )
    residue = process is not None and _process_group_exists(process.pid)
    if residue:
        _cleanup_residue(process.pid, grace=term_grace)
    placeholders_clean = _cleanup_placeholders(placeholders)
    try:
        shutil.rmtree(call_root)
        temp_clean = True
    except OSError:
        temp_clean = False
    if residue or not placeholders_clean or not temp_clean:
        result = SandboxOutcome(
            result.stdout,
            result.stderr,
            result.exit_code,
            result.timed_out,
            result.target_started,
            result.wrapper_status,
            "cleanup_failed",
            "failed",
            True,
        )
    if interrupted is not None:
        raise interrupted
    return result
