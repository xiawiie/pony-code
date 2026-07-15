from __future__ import annotations

import json
import os
import platform
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from pico.sandbox import (
    consume_target_start_frame,
    SandboxContext,
    SandboxIdentity,
    TARGET_START_ENV,
    target_start_frame,
)
from pico.sandbox_macos import build_settings, run_macos_sandbox, validate_settings
from pico.sandbox_toolchain import SandboxToolchain


@dataclass(frozen=True)
class Execution:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout: float = 5


class FakeProcess:
    pid = 12345
    returncode = 0

    def __init__(self, stderr="permission denied is ordinary stderr"):
        self.stderr = stderr

    def communicate(self, timeout=None):
        return "out", self.stderr


@pytest.fixture
def sandbox_parts(tmp_path):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    home = tmp_path / "real-home"
    workspace.mkdir()
    toolchain.mkdir()
    home.mkdir()
    node = toolchain / "node"
    entry = toolchain / "dist" / "cli.js"
    entry.parent.mkdir()
    node.write_text("node")
    node.chmod(0o700)
    entry.write_text("cli")
    context = SandboxContext(SandboxIdentity(toolchain, node, entry), workspace, home)
    return context, Execution(("printf", "%s", "hello world"), workspace, {"PATH": "/usr/bin"})


def test_exact_settings_are_strict_and_protect_sensitive_paths(sandbox_parts, tmp_path):
    context, _ = sandbox_parts
    settings = build_settings(context, tmp_path / "call")
    assert settings["network"] == {
        "allowedDomains": [], "deniedDomains": ["*"], "allowLocalBinding": False,
        "allowUnixSockets": [], "allowAllUnixSockets": False,
    }
    fs = settings["filesystem"]
    assert fs["allowRead"] == []
    for path in (context.workspace_root / ".env", context.workspace_root / ".pico",
                 context.original_home / ".ssh", context.original_home / ".aws"):
        assert str(path) in fs["denyRead"]
    for path in (context.workspace_root / ".git", context.workspace_root / ".pico"):
        assert str(path) in fs["denyWrite"]
    invalid = dict(settings, surprise=True)
    with pytest.raises(ValueError):
        validate_settings(invalid)


def test_launcher_contract_uses_managed_node_direct_entry_and_literal_argv(sandbox_parts):
    context, execution = sandbox_parts
    seen = {}

    def launcher(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        settings_path = Path(argv[3])
        seen["settings"] = json.loads(settings_path.read_text())
        seen["settings_mode"] = stat.S_IMODE(settings_path.stat().st_mode)
        seen["temp_mode"] = stat.S_IMODE(settings_path.parent.stat().st_mode)
        token = kwargs["env"][TARGET_START_ENV]
        return FakeProcess(
            target_start_frame(token) + "permission denied is ordinary stderr"
        )

    result = run_macos_sandbox(context, execution, launcher=launcher)
    assert seen["argv"][:2] == [str(context.identity.node_path), str(context.identity.srt_entry_path)]
    assert seen["argv"][2:5] == ["--settings", seen["argv"][3], "--"]
    assert seen["argv"][5:7] == [str(context.identity.node_path), "-e"]
    assert seen["argv"][8:] == list(execution.argv)
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["settings_mode"] == 0o600 and seen["temp_mode"] == 0o700
    env = seen["kwargs"]["env"]
    assert env["HOME"] != str(context.original_home)
    assert len({env["HOME"], env["TMPDIR"], env["XDG_CACHE_HOME"]}) == 3
    assert result.sandbox_outcome == "completed"
    assert result.target_started is True
    assert result.stderr == "permission denied is ordinary stderr"


def test_wrapper_exit_before_target_spawn_is_not_completed(sandbox_parts):
    context, execution = sandbox_parts

    class BootstrapFailure(FakeProcess):
        returncode = 126

    result = run_macos_sandbox(
        context,
        execution,
        launcher=lambda *_args, **_kwargs: BootstrapFailure(),
    )

    assert result.target_started is False
    assert result.wrapper_status == "failed"
    assert result.sandbox_outcome == "target_not_started"


def test_target_start_frame_requires_exact_random_token_and_is_removed():
    stderr = target_start_frame("a" * 64) + "ordinary stderr"
    unchanged, wrong = consume_target_start_frame(stderr, "b" * 64)
    cleaned, started = consume_target_start_frame(stderr, "a" * 64)

    assert unchanged == stderr and wrong is False
    assert cleaned == "ordinary stderr" and started is True


def test_invalid_identity_fails_before_launcher(sandbox_parts, tmp_path):
    context, execution = sandbox_parts
    outside = tmp_path / "outside-node"
    outside.write_text("x")
    outside.chmod(0o700)
    bad = SandboxContext(SandboxIdentity(context.identity.trusted_root, outside,
                                         context.identity.srt_entry_path),
                         context.workspace_root, context.original_home)
    result = run_macos_sandbox(bad, execution, launcher=lambda *a, **k: pytest.fail("launched"))
    assert not result.target_started
    assert result.sandbox_outcome == "wrapper_failed"


def test_wrapper_exit_with_process_residue_is_not_clean_success(
    sandbox_parts, monkeypatch
):
    context, execution = sandbox_parts
    cleaned = []
    monkeypatch.setattr("pico.sandbox_macos._process_group_exists", lambda pid: True)
    monkeypatch.setattr(
        "pico.sandbox_macos._cleanup_residue",
        lambda pid, grace: cleaned.append((pid, grace)) or False,
    )

    result = run_macos_sandbox(context, execution, launcher=lambda *a, **k: FakeProcess())

    assert result.sandbox_outcome == "cleanup_failed"
    assert result.cleanup_status == "failed"
    assert result.residue_detected is True
    assert cleaned == [(FakeProcess.pid, 2.0)]


def test_real_process_group_residue_is_detected_and_reaped(
    sandbox_parts, tmp_path
):
    context, execution = sandbox_parts
    pid_file = tmp_path / "child.pid"

    def launcher(_argv, **kwargs):
        return subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                f"/bin/sleep 30 >/dev/null 2>&1 & echo $! > {pid_file}",
            ],
            **kwargs,
        )

    result = run_macos_sandbox(
        context, execution, launcher=launcher, term_grace=0.2
    )

    assert result.sandbox_outcome == "cleanup_failed"
    assert result.residue_detected is True
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_fatal_communicate_cleans_process_and_temp_before_reraising(
    sandbox_parts, monkeypatch
):
    context, execution = sandbox_parts
    primary = KeyboardInterrupt("stop")
    call_roots = []
    cleaned = []

    class InterruptedProcess(FakeProcess):
        def communicate(self, timeout=None):
            raise primary

    def launcher(argv, **kwargs):
        call_roots.append(Path(argv[3]).parent)
        return InterruptedProcess()

    monkeypatch.setattr("pico.sandbox_macos._process_group_exists", lambda _pid: True)

    def cleanup(pid, grace):
        cleaned.append((pid, grace))
        raise OSError("cleanup probe failed")

    monkeypatch.setattr("pico.sandbox_macos._cleanup_residue", cleanup)

    with pytest.raises(KeyboardInterrupt) as caught:
        run_macos_sandbox(context, execution, launcher=launcher)

    assert caught.value is primary
    assert cleaned == [(InterruptedProcess.pid, 2.0)]
    assert call_roots and not call_roots[0].exists()


@pytest.mark.skipif(
    not os.environ.get("PICO_RUN_REAL_SRT") or platform.system() != "Darwin",
    reason="explicit real macOS SRT smoke only",
)
def test_real_srt_smoke_is_explicit(tmp_path, real_home):
    toolchain_root = real_home.resolve(strict=True) / ".pico" / "toolchains" / "sandbox"
    identity = SandboxToolchain(toolchain_root, create_root=False).identity()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = SandboxContext(identity, workspace, real_home)
    result = run_macos_sandbox(context, Execution(("/usr/bin/true",), workspace, {}))
    assert result.sandbox_outcome == "completed"
    assert result.target_started is True
    assert result.exit_code == 0
    assert result.cleanup_status == "completed"
    assert result.residue_detected is False
