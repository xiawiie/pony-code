"""Probe Pony's production Windows executable and PowerShell boundaries."""

import json
import os
from pathlib import Path
import sys
import tempfile
import traceback


def _stage(name):
    print(f"windows_shell_stage={name}", file=sys.stderr, flush=True)


def _report_mutable_directory_access(native, path):
    accesses = (
        ("write_attributes", native._FILE_WRITE_ATTRIBUTES),
        ("delete", native._DELETE),
        ("write_dacl", native._WRITE_DAC),
        ("write_owner", native._WRITE_OWNER),
        ("delete_child", native._FILE_DELETE_CHILD),
        ("add_file", native._FILE_WRITE_DATA),
        ("add_directory", native._FILE_APPEND_DATA),
    )
    path = native.lexical_absolute(path)
    current = Path(path.anchor)
    components = path.parts[1:]
    for index, component in enumerate((None, *components)):
        if component is not None:
            current /= component
        allowed = []
        for name, access in accesses:
            if index != len(components) and name in {"add_file", "add_directory"}:
                continue
            try:
                handle = native.open_path(
                    current,
                    directory=True,
                    desired_access=access,
                    single_link=False,
                )
            except OSError as exc:
                if native.error_code(exc) in {5, 1314}:
                    continue
                raise
            else:
                handle.close()
                allowed.append(name)
                break
        if allowed:
            print(
                "windows_shell_mutable_directory="
                f"{index}:{current.name or current.anchor}:{allowed[0]}",
                file=sys.stderr,
                flush=True,
            )


def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))

    from pony.security.command_policy import assess_command
    from pony.tools.subprocess import (
        _minimal_env,
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
        _report_mutable_directory_access(windows_native, powershell_path.parent)
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

        _stage("powershell")
        command = "Write-Output 'pony-shell-ok'"
        assessment = assess_command(command, root, trusted)
        if assessment["execution_mode"] != "shell" or assessment["decision"] != "ask":
            raise RuntimeError("PowerShell command was not classified as shell grammar")
        result = run_hardened_command(
            powershell,
            command=command,
            shell=True,
            cwd=root,
            timeout=5,
            env=_minimal_env(root, powershell),
        )
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
        "fixed_system_powershell": True,
        "native_git_argv": True,
        "native_rg_trusted": True,
        "user_writable_path_shim_rejected": True,
        "dynamic_syntax_rejected": True,
    }


def main():
    if os.name != "nt":
        print("windows_shell_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe()
    except BaseException as exc:
        traceback.print_exc()
        print(f"windows_shell_probe_failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
