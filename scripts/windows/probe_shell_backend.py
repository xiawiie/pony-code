"""Probe Pony's production Windows executable and PowerShell boundaries."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback


def _stage(name):
    print(f"windows_shell_stage={name}", file=sys.stderr, flush=True)



def _run_direct_powershell(argv, *, root, env, creationflags, timeout=8):
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        executable=argv[0],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process_running = process.poll() is None
        if process_running:
            process.kill()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        process.wait(timeout=5)
        return {
            "timed_out": True,
            "process_running": process_running,
            "stdout": exc.output,
            "stderr": exc.stderr,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    return {
        "timed_out": False,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's production Windows executable and PowerShell boundaries."
    )
    parser.add_argument(
        "--expect-elevated-rejection",
        action="store_true",
        help="require the elevated account to be rejected as an executable trust root",
    )
    return parser


def probe(*, expect_elevated_rejection=False):
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))

    from pony.security.command_policy import assess_command
    from pony.tools.subprocess import (
        _minimal_env,
        _shell_argv,
        _verified_executable_identity,
        build_trusted_executables,
        run_hardened_command,
    )
    from pony.security import windows_native

    with tempfile.TemporaryDirectory(prefix="pony-windows-shell-") as temporary:
        root = Path(temporary)
        shim_directory = root / "untrusted-bin"
        shim_directory.mkdir()
        shim = shim_directory / "git.exe"
        shim.write_bytes(b"not an executable")
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join((str(shim_directory), env.get("PATH", "")))

        _stage("discover")
        powershell_path = (
            windows_native.windows_directory()
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if expect_elevated_rejection:
            try:
                _verified_executable_identity(powershell_path)
            except ValueError as exc:
                if str(exc) != "mutable trusted executable directory":
                    raise
            else:
                raise RuntimeError("elevated executable trust root was not rejected")
            return {
                "schema_version": 1,
                "elevated_executable_trust_rejected": True,
            }

        _verified_executable_identity(powershell_path)
        trusted = build_trusted_executables(root, env=env)
        powershell = trusted.get("powershell")
        git = trusted.get("git")
        rg = trusted.get("rg")
        if powershell is None:
            raise RuntimeError("fixed system PowerShell was not trusted")
        if git is None or Path(git) == shim:
            raise RuntimeError("immutable native git.exe was not trusted")
        if rg is None:
            raise RuntimeError("immutable native rg.exe was not trusted")

        command = "Write-Output 'pony-shell-ok'"
        assessment = assess_command(command, root, trusted)
        if assessment["execution_mode"] != "shell" or assessment["decision"] != "ask":
            raise RuntimeError("PowerShell command was not classified as shell grammar")
        shell_env = _minimal_env(root, powershell)
        shell_argv = _shell_argv(str(powershell), command, windows=True)
        _stage("powershell_direct")
        direct = _run_direct_powershell(
            shell_argv,
            root=root,
            env=shell_env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if direct["timed_out"]:
            raise RuntimeError(
                "direct fixed PowerShell execution timed out: "
                f"elapsed_seconds={direct['elapsed_seconds']}, "
                f"stdout={direct['stdout']!r}, stderr={direct['stderr']!r}"
            )
        if direct["returncode"] != 0 or direct["stdout"].strip() != "pony-shell-ok":
            raise RuntimeError(
                "direct fixed PowerShell execution failed: "
                f"returncode={direct['returncode']}, stderr={direct['stderr']!r}"
            )

        _stage("powershell_job")
        try:
            result = run_hardened_command(
                powershell,
                command=command,
                shell=True,
                cwd=root,
                timeout=20,
                env=shell_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "fixed PowerShell execution timed out: "
                f"stdout={exc.output!r}, stderr={exc.stderr!r}"
            ) from exc
        if result.returncode != 0 or result.stdout.strip() != "pony-shell-ok":
            raise RuntimeError(
                "fixed PowerShell execution failed: "
                f"returncode={result.returncode}, stderr={result.stderr!r}"
            )

        _stage("argv")
        result = run_hardened_command(
            git,
            args=("--version",),
            cwd=root,
            timeout=5,
            env=_minimal_env(root, git),
        )
        if result.returncode != 0 or not result.stdout.startswith("git version "):
            raise RuntimeError("native git argv execution failed")

        _stage("policy")
        rejected = {
            assess_command("Write-Output $env:PATH", root, trusted)["reason"],
            assess_command("Get-Content file.txt:stream", root, trusted)["reason"],
            assess_command("cmd /c dir", root, trusted)["reason"],
        }
        if rejected != {
            "dynamic_expansion_rejected",
            "alternate_data_stream_rejected",
            "shell_wrapper_rejected",
        }:
            raise RuntimeError(f"Windows command policy mismatch: {sorted(rejected)!r}")

    return {
        "schema_version": 1,
        "direct_system_powershell": True,
        "fixed_system_powershell": True,
        "native_git_argv": True,
        "native_rg_trusted": True,
        "user_writable_path_shim_rejected": True,
        "dynamic_syntax_rejected": True,
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_shell_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe(expect_elevated_rejection=args.expect_elevated_rejection)
    except BaseException as exc:
        traceback.print_exc()
        print(f"windows_shell_probe_failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
