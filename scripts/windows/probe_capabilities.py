#!/usr/bin/env python3
"""Probe the native Windows API surface required by Pony's Windows backend."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


_REQUIRED_SYMBOLS = {
    "ntdll": (
        "NtCreateFile",
        "RtlNtStatusToDosError",
    ),
    "kernel32": (
        "AssignProcessToJobObject",
        "CloseHandle",
        "CreateFileW",
        "CreateJobObjectW",
        "CreateProcessW",
        "FlushFileBuffers",
        "GetFileInformationByHandleEx",
        "GetFinalPathNameByHandleW",
        "LockFileEx",
        "ReplaceFileW",
        "ResumeThread",
        "SetInformationJobObject",
        "TerminateJobObject",
        "UnlockFileEx",
    ),
    "advapi32": (
        "GetNamedSecurityInfoW",
        "GetSecurityInfo",
        "GetTokenInformation",
        "OpenProcessToken",
        "SetEntriesInAclW",
        "SetNamedSecurityInfoW",
        "SetSecurityInfo",
    ),
}
_POWERSHELL_RELATIVE = Path("System32/WindowsPowerShell/v1.0/powershell.exe")
_POWERSHELL_ARGS = (
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "[Console]::Out.Write($PSVersionTable.PSVersion.ToString())",
)


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's required native Windows API surface."
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the JSON report",
    )
    return parser


def _load_required_symbols(loader):
    loaded = {}
    for library_name, symbols in _REQUIRED_SYMBOLS.items():
        library = loader(library_name, use_last_error=True)
        missing = [symbol for symbol in symbols if not hasattr(library, symbol)]
        if missing:
            raise RuntimeError(
                f"missing Windows API symbols in {library_name}: {', '.join(missing)}"
            )
        loaded[library_name] = list(symbols)
    return loaded


def _probe_powershell(system_root, runner):
    root = Path(system_root)
    if not root.is_absolute():
        raise RuntimeError("SystemRoot is missing or not absolute")
    executable = root / _POWERSHELL_RELATIVE
    if not executable.is_file():
        raise RuntimeError("system Windows PowerShell is unavailable")
    completed = runner(
        [str(executable), *_POWERSHELL_ARGS],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version or completed.stderr.strip():
        raise RuntimeError("system Windows PowerShell probe failed")
    return version


def probe(*, system_root, loader, runner):
    if sys.maxsize <= 2**32 or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("Pony Windows support requires native x64 Python")
    return {
        "schema_version": 1,
        "architecture": "x64",
        "python": platform.python_version(),
        "powershell": _probe_powershell(system_root, runner),
        "api_symbols": _load_required_symbols(loader),
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_capability_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe(
            system_root=os.environ.get("SystemRoot", ""),
            loader=ctypes.WinDLL,
            runner=subprocess.run,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"windows_capability_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
