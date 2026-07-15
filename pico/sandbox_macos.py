"""macOS adapter for Anthropic Sandbox Runtime (SRT)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .sandbox import (
    ApprovedExecution,
    consume_target_start_frame,
    new_target_start_token,
    SandboxContext,
    SandboxOutcome,
    TARGET_START_ENV,
    TARGET_START_WRAPPER,
)

_NETWORK_KEYS = {
    "allowedDomains", "deniedDomains", "allowLocalBinding",
    "allowUnixSockets", "allowAllUnixSockets",
}
_FILESYSTEM_KEYS = {"denyRead", "allowRead", "allowWrite", "denyWrite"}


def build_settings(context: SandboxContext, call_root: Path) -> dict[str, object]:
    workspace = context.workspace_root.resolve(strict=True)
    home = context.original_home.resolve(strict=True)
    toolchain = context.identity.trusted_root.resolve(strict=True)
    deny_read = [
        workspace / ".env", workspace / ".env.*", workspace / ".pico",
        home / ".pico", home / ".ssh", home / ".aws",
        home / ".config" / "gcloud", home / ".kube", home / ".docker",
        home / ".netrc", home / ".npmrc", home / ".pypirc",
        home / ".bash_history", home / ".zsh_history",
    ]
    deny_write = [
        workspace / ".env", workspace / ".env.*", workspace / ".git",
        workspace / ".pico", toolchain, call_root / "settings.json",
    ]
    settings: dict[str, object] = {
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "allowLocalBinding": False,
            "allowUnixSockets": [],
            "allowAllUnixSockets": False,
        },
        "filesystem": {
            "denyRead": [str(path) for path in deny_read],
            "allowRead": [],
            "allowWrite": [str(workspace), str(call_root)],
            "denyWrite": [str(path) for path in deny_write],
        },
    }
    validate_settings(settings)
    return settings


def validate_settings(settings: object) -> None:
    if not isinstance(settings, dict) or set(settings) != {"network", "filesystem"}:
        raise ValueError("invalid sandbox settings schema")
    network, filesystem = settings["network"], settings["filesystem"]
    if not isinstance(network, dict) or set(network) != _NETWORK_KEYS:
        raise ValueError("invalid sandbox network settings")
    if not isinstance(filesystem, dict) or set(filesystem) != _FILESYSTEM_KEYS:
        raise ValueError("invalid sandbox filesystem settings")
    for key in ("allowedDomains", "deniedDomains", "allowUnixSockets"):
        if not isinstance(network[key], list) or not all(isinstance(v, str) for v in network[key]):
            raise ValueError("invalid sandbox network setting type")
    for key in ("allowLocalBinding", "allowAllUnixSockets"):
        if type(network[key]) is not bool:
            raise ValueError("invalid sandbox network setting type")
    for key in _FILESYSTEM_KEYS:
        if not isinstance(filesystem[key], list) or not all(isinstance(v, str) for v in filesystem[key]):
            raise ValueError("invalid sandbox filesystem setting type")


def _verify_identity(context: SandboxContext) -> None:
    identity = context.identity
    identity.verify()
    root = identity.trusted_root.resolve(strict=True)
    for path in (identity.node_path, identity.srt_entry_path):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("sandbox executable identity is outside trusted root")
    if not os.access(identity.node_path, os.X_OK):
        raise ValueError("managed Node is not executable")


def _environment(
    execution: ApprovedExecution,
    call_root: Path,
    target_start_token: str,
) -> dict[str, str]:
    env = dict(execution.env)
    home, tmp, cache = call_root / "home", call_root / "tmp", call_root / "cache"
    for path in (home, tmp, cache, cache / "pip", cache / "npm"):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "XDG_CACHE_HOME": str(cache), "PIP_CACHE_DIR": str(cache / "pip"),
        "npm_config_cache": str(cache / "npm"), TARGET_START_ENV: target_start_token,
    })
    return env


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_residue(process_group: int, *, grace: float) -> bool:
    """Best-effort cleanup; return whether any process still remains."""
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


def run_macos_sandbox(
    context: SandboxContext,
    execution: ApprovedExecution,
    *,
    launcher: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    term_grace: float = 2.0,
) -> SandboxOutcome:
    """Run already-approved argv through managed Node and SRT; never use a shell."""
    process = None
    call_root = Path(tempfile.mkdtemp(prefix="pico-sandbox-"))
    target_start_token = new_target_start_token()
    os.chmod(call_root, 0o700)
    result = SandboxOutcome(
        "", "", None, False, False, "failed", "wrapper_failed", "completed"
    )
    interrupted = None
    try:
        _verify_identity(context)
        if not execution.argv or not all(isinstance(arg, str) and "\0" not in arg for arg in execution.argv):
            raise ValueError("approved argv must be non-empty strings")
        cwd = Path(execution.cwd).resolve(strict=True)
        if not cwd.is_relative_to(context.workspace_root.resolve(strict=True)):
            raise ValueError("execution cwd escapes workspace")
        settings = build_settings(context, call_root)
        settings_path = call_root / "settings.json"
        fd = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, sort_keys=True, separators=(",", ":"))
        argv = [
            str(context.identity.node_path),
            str(context.identity.srt_entry_path),
            "--settings",
            str(settings_path),
            "--",
            str(context.identity.node_path),
            "-e",
            TARGET_START_WRAPPER,
            *execution.argv,
        ]
        process = launcher(
            argv,
            cwd=str(cwd),
            env=_environment(execution, call_root, target_start_token),
                           shell=False, start_new_session=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=execution.timeout)
            stderr, started = consume_target_start_frame(stderr, target_start_token)
            outcome = "completed" if started else "target_not_started"
            wrapper_status = "completed" if started else "failed"
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=term_grace)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            stderr, started = consume_target_start_frame(stderr, target_start_token)
            outcome = "timeout"
            wrapper_status = "completed"
        result = SandboxOutcome(
            stdout,
            stderr,
            process.returncode,
            timed_out,
            started,
            wrapper_status,
            outcome,
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

    residue = False
    cleanup_error = ""
    if process is not None:
        try:
            residue = _process_group_exists(process.pid)
            if residue:
                _cleanup_residue(process.pid, grace=term_grace)
        except BaseException as exc:
            residue = True
            if interrupted is None and not isinstance(exc, Exception):
                interrupted = exc
            elif isinstance(exc, Exception):
                cleanup_error = str(exc)
    if residue:
        result = SandboxOutcome(
            result.stdout,
            result.stderr + cleanup_error,
            result.exit_code,
            result.timed_out,
            result.target_started,
            result.wrapper_status,
            "cleanup_failed",
            "failed",
            True,
        )
    try:
        shutil.rmtree(call_root)
    except BaseException as exc:
        result = SandboxOutcome(result.stdout, result.stderr + str(exc), result.exit_code,
                                result.timed_out, result.target_started, result.wrapper_status,
                                "cleanup_failed", "failed", result.residue_detected)
        if interrupted is None and not isinstance(exc, Exception):
            interrupted = exc
    if interrupted is not None:
        raise interrupted
    return result
