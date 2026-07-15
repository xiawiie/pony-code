from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from pico.sandbox import (
    SandboxContext,
    SandboxIdentity,
    TARGET_START_ENV,
    target_start_frame,
)
from pico.sandbox_linux import (
    STATUSES,
    build_linux_sandbox_plan,
    probe,
    resolve_linux_executables,
    run_linux_sandbox,
)
from pico.sandbox_toolchain import SandboxToolchain


BWRAP = "/usr/bin/true"
SOCAT = "/usr/bin/false"
RG = "/usr/bin/env"
TRUE = "/usr/bin/true"
TEST = "/usr/bin/test"
EXECUTABLES = {"bwrap": BWRAP, "socat": SOCAT, "rg": RG, "true": TRUE, "test": TEST}


class FakeRunner:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def __call__(self, command, timeout):
        command = tuple(str(value) for value in command)
        self.calls.append((command, timeout))
        outcome = self.outcomes.get(command)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return subprocess.CompletedProcess(command, outcome, stdout="", stderr="")
        if command == (BWRAP, "--version"):
            output = "bubblewrap 0.11"
        elif command == (SOCAT, "-V"):
            output = "socat version 1.8"
        elif command == (RG, "--version"):
            output = "ripgrep 14.1"
        else:
            output = "ignored output"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="ignored error")


@dataclass(frozen=True)
class Execution:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout: float = 5
    executable: object | None = None


class FakeProcess:
    pid = 987654
    returncode = 0

    def __init__(self, stderr="ordinary stderr"):
        self.stderr = stderr

    def communicate(self, timeout=None):
        return "out", self.stderr


def _tree(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".pico-toolchain.json"
    }


def sandbox_parts(tmp_path, *, machine="x86_64", seccomp_elf_machine=None):
    workspace = tmp_path / "workspace"
    toolchain = tmp_path / "toolchain"
    home = tmp_path / "home"
    workspace.mkdir()
    toolchain.mkdir(mode=0o700)
    home.mkdir()
    node = toolchain / "node"
    entry = toolchain / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    vendor_arch = "x64" if machine == "x86_64" else "arm64"
    elf_machine = 62 if machine == "x86_64" else 183
    seccomp = toolchain / f"node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp/{vendor_arch}/apply-seccomp"
    entry.parent.mkdir(parents=True)
    seccomp.parent.mkdir(parents=True)
    node.write_text("node")
    entry.write_text("cli")
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, seccomp_elf_machine or elf_machine)
    seccomp.write_bytes(header)
    node.chmod(0o500)
    seccomp.chmod(0o500)
    marker = toolchain / ".pico-toolchain.json"
    marker.write_text(json.dumps({"tree": _tree(toolchain)}, sort_keys=True))
    marker.chmod(0o600)
    identity = SandboxIdentity(
        toolchain,
        node,
        entry,
        bundle_manifest_hash=hashlib.sha256(marker.read_bytes()).hexdigest(),
    )
    context = SandboxContext(identity, workspace, home)
    proc = tmp_path / "proc-status"
    proc.write_text("Name:\ttest\n")
    execution = Execution((TRUE,), workspace, {"PATH": "/usr/bin:/bin"}, executable=TRUE)
    return context, execution, proc


def test_probe_requires_absolute_identities_combined_namespaces_and_seccomp(tmp_path):
    context, _, proc = sandbox_parts(tmp_path)
    runner = FakeRunner()

    report = probe(
        runner=runner,
        timeout=0.25,
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )

    assert report.status == "ready"
    assert report.architecture == "x86_64"
    assert report.user_namespace.reason == "namespace_combination_verified"
    assert report.seccomp_architecture.reason == "seccomp_enforcement_verified"
    assert all(timeout == 0.25 for _, timeout in runner.calls)
    namespace_commands = [command for command, _ in runner.calls if "--unshare-user" in command]
    assert namespace_commands
    assert all("--unshare-net" in command and "--unshare-pid" in command for command in namespace_commands)
    assert all(
        Path(command[0]).is_absolute()
        and (Path(command[-1]).is_absolute() or "-e" in command)
        for command in namespace_commands
    )


def test_probe_rejects_path_shadow_and_unverified_seccomp(tmp_path):
    workspace = tmp_path / "workspace"
    shadow = tmp_path / "shadow"
    workspace.mkdir()
    shadow.mkdir()
    binary = shadow / "bwrap"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    resolved = resolve_linux_executables(workspace_root=workspace, env={"PATH": str(shadow)})
    report = probe(
        runner=FakeRunner(),
        system="Linux",
        machine="x86_64",
        executables=resolved,
        proc_path=Path(__file__),
        temp_root=tmp_path,
        userns_paths=(),
    )

    assert resolved == {}
    assert (report.bwrap.status, report.bwrap.reason) == ("missing", "missing_binary")
    assert report.seccomp_architecture.reason == "seccomp_binary_unverified"
    assert report.status == "not_ready"


def test_unsupported_architecture_runs_no_probe_commands():
    runner = FakeRunner()

    report = probe(
        runner=runner,
        system="Linux",
        machine="s390x",
        executables=EXECUTABLES,
    )

    assert report.status == "not_applicable"
    assert report.applicability_reason == "unsupported_architecture"
    assert runner.calls == []


def test_userns_disabled_and_namespace_denied_have_stable_reasons(tmp_path):
    context, _, proc = sandbox_parts(tmp_path)
    disabled = tmp_path / "userns"
    disabled.write_text("0\n")
    runner = FakeRunner()

    report = probe(
        runner=runner,
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(disabled,),
    )

    assert (report.user_namespace.status, report.user_namespace.reason) == (
        "blocked",
        "userns_disabled",
    )
    assert not any("--unshare-user" in command for command, _ in runner.calls)

    class NamespaceDenied(FakeRunner):
        def __call__(self, command, timeout):
            result = super().__call__(command, timeout)
            if "--unshare-user" in tuple(str(value) for value in command):
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            return result

    denied = probe(
        runner=NamespaceDenied(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )
    assert (denied.user_namespace.status, denied.user_namespace.reason) == (
        "blocked",
        "namespace_denied",
    )

    class NamespaceTimeout(FakeRunner):
        def __call__(self, command, timeout):
            if "--unshare-user" in tuple(str(value) for value in command):
                raise subprocess.TimeoutExpired(command, timeout)
            return super().__call__(command, timeout)

    timed_out = probe(
        runner=NamespaceTimeout(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )
    assert (timed_out.user_namespace.status, timed_out.user_namespace.reason) == (
        "unknown",
        "probe_timeout",
    )


def test_seccomp_arch_proc_and_temp_failures_are_not_ready(tmp_path):
    context, _, _ = sandbox_parts(tmp_path, seccomp_elf_machine=183)
    unusable_temp = tmp_path / "not-a-directory"
    unusable_temp.write_text("x")

    report = probe(
        runner=FakeRunner(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=tmp_path / "missing-proc",
        temp_root=unusable_temp,
        userns_paths=(),
    )

    assert report.seccomp_architecture.reason == "seccomp_arch_mismatch"
    assert (report.proc.status, report.proc.reason) == ("missing", "proc_unavailable")
    assert (report.temp.status, report.temp.reason) == ("blocked", "temp_unusable")
    assert report.status == "not_ready"


def test_report_never_contains_command_output(tmp_path):
    context, _, proc = sandbox_parts(tmp_path)
    report = probe(
        runner=FakeRunner(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )
    serialized = report.to_json()

    assert "ignored output" not in serialized
    assert "ignored error" not in serialized
    for capability in (
        report.bwrap,
        report.socat,
        report.rg,
        report.user_namespace,
        report.mount_namespace,
        report.network_namespace,
        report.seccomp_architecture,
        report.proc,
        report.temp,
    ):
        assert capability.status in STATUSES


def test_missing_capability_never_starts_linux_target(tmp_path):
    context, execution, proc = sandbox_parts(tmp_path)
    launched = []
    executables = dict(EXECUTABLES)
    executables.pop("bwrap")

    result = run_linux_sandbox(
        context,
        execution,
        runner=FakeRunner(),
        launcher=lambda *args, **kwargs: launched.append((args, kwargs)),
        executables=executables,
        system="Linux",
        machine="x86_64",
        proc_path=proc,
        temp_root=tmp_path,
    )

    assert result.sandbox_outcome == "sandbox_not_ready"
    assert result.target_started is False
    assert launched == []


def test_linux_wrapper_exit_before_target_spawn_is_not_completed(tmp_path, monkeypatch):
    context, execution, proc = sandbox_parts(tmp_path)
    report = probe(
        runner=FakeRunner(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )

    class BootstrapFailure(FakeProcess):
        returncode = 126

    monkeypatch.setattr("pico.sandbox_linux._process_group_exists", lambda _pid: False)
    result = run_linux_sandbox(
        context,
        execution,
        launcher=lambda *_args, **_kwargs: BootstrapFailure(),
        executables=EXECUTABLES,
        system="Linux",
        machine="x86_64",
        capability_report=report,
        proc_path=proc,
        temp_root=tmp_path,
    )

    assert result.target_started is False
    assert result.wrapper_status == "failed"
    assert result.sandbox_outcome == "target_not_started"


def test_linux_adapter_uses_structured_bwrap_argv_and_cleans_placeholders(tmp_path, monkeypatch):
    context, execution, proc = sandbox_parts(tmp_path)
    seen = {}
    report = probe(
        runner=FakeRunner(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )

    def launcher(argv, **kwargs):
        seen.update(argv=[str(value) for value in argv], kwargs=kwargs)
        token = seen["argv"][seen["argv"].index(TARGET_START_ENV) + 1]
        return FakeProcess(target_start_frame(token) + "ordinary stderr")

    monkeypatch.setattr("pico.sandbox_linux._process_group_exists", lambda _pid: False)
    result = run_linux_sandbox(
        context,
        execution,
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("precomputed capability report must skip probe")
        ),
        launcher=launcher,
        executables=EXECUTABLES,
        system="Linux",
        machine="x86_64",
        capability_report=report,
        proc_path=proc,
        temp_root=tmp_path,
    )

    argv = seen["argv"]
    assert argv[0] == BWRAP
    assert argv[-1] == TRUE
    assert "--unshare-user" in argv and "--unshare-net" in argv and "--unshare-pid" in argv
    assert argv[argv.index("--ro-bind") + 1 : argv.index("--ro-bind") + 3] == ["/", "/"]
    workspace = str(context.workspace_root)
    bind = argv.index("--bind")
    assert argv[bind + 1 : bind + 3] == [workspace, workspace]
    call_root = argv[argv.index("--dir") + 1]
    tmpfs = next(index for index in range(len(argv) - 1) if argv[index : index + 2] == ["--tmpfs", "/tmp"])
    directory = argv.index("--dir")
    call_bind = next(
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3] == ["--bind", call_root, call_root]
    )
    assert tmpfs < directory < call_bind
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["start_new_session"] is True
    assert result.sandbox_outcome == "completed"
    assert result.target_started is True
    assert result.stderr == "ordinary stderr"
    assert not (context.workspace_root / ".git").exists()
    assert not (context.workspace_root / ".pico").exists()
    assert not (context.workspace_root / ".env").exists()


def test_linux_plan_builder_validates_real_structured_argv_without_launch(tmp_path, monkeypatch):
    context, execution, proc = sandbox_parts(tmp_path)
    report = probe(
        runner=FakeRunner(),
        system="Linux",
        machine="x86_64",
        executables=EXECUTABLES,
        sandbox_identity=context.identity,
        proc_path=proc,
        temp_root=tmp_path,
        userns_paths=(),
    )

    monkeypatch.setattr(
        "pico.sandbox_linux._create_placeholder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pure plan validation must not mutate the workspace")
        ),
    )
    plan = build_linux_sandbox_plan(
        context,
        execution,
        capability_report=report,
        executables=EXECUTABLES,
        machine="x86_64",
    )

    assert plan[0] == BWRAP
    assert plan[-1] == TRUE
    assert {"--unshare-user", "--unshare-pid", "--unshare-net", "--proc"} <= set(plan)
    assert not (context.workspace_root / ".git").exists()
    assert not (context.workspace_root / ".pico").exists()
    assert not (context.workspace_root / ".env").exists()


def test_linux_adapter_timeout_terminates_process_group(tmp_path, monkeypatch):
    context, execution, proc = sandbox_parts(tmp_path)
    signals = []

    class TimeoutProcess(FakeProcess):
        returncode = -signal.SIGTERM

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("bwrap", timeout)
            return "", ""

    monkeypatch.setattr("pico.sandbox_linux.os.killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr("pico.sandbox_linux._process_group_exists", lambda _pid: False)
    result = run_linux_sandbox(
        context,
        execution,
        runner=FakeRunner(),
        launcher=lambda *args, **kwargs: TimeoutProcess(),
        executables=EXECUTABLES,
        system="Linux",
        machine="x86_64",
        proc_path=proc,
        temp_root=tmp_path,
        term_grace=0,
    )

    assert result.timed_out is True
    assert result.sandbox_outcome == "timeout"
    assert signals == [(FakeProcess.pid, signal.SIGTERM)]


def test_linux_adapter_rejects_hardlinked_protected_file_before_launch(tmp_path):
    context, execution, proc = sandbox_parts(tmp_path)
    protected = context.workspace_root / ".env"
    alias = context.workspace_root / "alias"
    protected.write_text("secret")
    os.link(protected, alias)
    launched = []

    result = run_linux_sandbox(
        context,
        execution,
        runner=FakeRunner(),
        launcher=lambda *args, **kwargs: launched.append((args, kwargs)),
        executables=EXECUTABLES,
        system="Linux",
        machine="x86_64",
        proc_path=proc,
        temp_root=tmp_path,
    )

    assert result.sandbox_outcome == "wrapper_failed"
    assert result.target_started is False
    assert launched == []
    assert not (context.workspace_root / ".git").exists()
    assert not (context.workspace_root / ".pico").exists()


def test_detected_process_residue_is_never_reported_clean(tmp_path, monkeypatch):
    context, execution, proc = sandbox_parts(tmp_path)
    monkeypatch.setattr("pico.sandbox_linux._process_group_exists", lambda _pid: True)
    monkeypatch.setattr("pico.sandbox_linux._cleanup_residue", lambda _pid, grace: False)

    result = run_linux_sandbox(
        context,
        execution,
        runner=FakeRunner(),
        launcher=lambda *args, **kwargs: FakeProcess(),
        executables=EXECUTABLES,
        system="Linux",
        machine="x86_64",
        proc_path=proc,
        temp_root=tmp_path,
    )

    assert result.sandbox_outcome == "cleanup_failed"
    assert result.cleanup_status == "failed"
    assert result.residue_detected is True


@pytest.mark.skipif(
    not os.environ.get("PICO_RUN_REAL_SRT") or platform.system() != "Linux",
    reason="explicit real Linux sandbox smoke only",
)
def test_real_linux_adapter_smoke_is_explicit(tmp_path, real_home):
    toolchain_root = real_home.resolve(strict=True) / ".pico" / "toolchains" / "sandbox"
    identity = SandboxToolchain(toolchain_root, create_root=False).identity()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = SandboxContext(identity, workspace, real_home)
    execution = Execution(
        ("/usr/bin/true",),
        workspace,
        {"PATH": "/usr/bin:/bin"},
        executable="/usr/bin/true",
    )

    result = run_linux_sandbox(context, execution)

    assert result.sandbox_outcome == "completed"
    assert result.target_started is True
    assert result.exit_code == 0
    assert result.cleanup_status == "completed"
    assert result.residue_detected is False


def test_non_linux_is_not_applicable_and_does_not_run_commands():
    runner = FakeRunner()
    report = probe(runner=runner, system="Darwin", machine="arm64", executables=EXECUTABLES)

    assert report.status == "not_applicable"
    assert report.applicability_reason == "unsupported_platform"
    assert runner.calls == []
