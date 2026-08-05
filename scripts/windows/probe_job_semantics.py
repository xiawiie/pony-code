#!/usr/bin/env python3
"""Probe Job Object process-tree containment required by Pony subprocesses."""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF


class _StartupInfo(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    )


class _ProcessInformation(ctypes.Structure):
    _fields_ = (
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    )


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's required native Windows Job Object semantics."
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    parser.add_argument("--child-marker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--grandchild-pid", type=Path, help=argparse.SUPPRESS)
    return parser


def _configure_api(loader):
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo),
        ctypes.POINTER(_ProcessInformation),
    )
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _create_kill_on_close_job(kernel32):
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        _winerror("CreateJobObjectW failed")
    limits = _JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(job)
        _winerror("SetInformationJobObject failed")
    return job


def _create_suspended_child(kernel32, marker, grandchild_pid):
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-marker",
        str(marker),
        "--grandchild-pid",
        str(grandchild_pid),
    ]
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    process = _ProcessInformation()
    if not kernel32.CreateProcessW(
        sys.executable,
        command_line,
        None,
        None,
        False,
        _CREATE_SUSPENDED | _CREATE_NO_WINDOW,
        None,
        None,
        ctypes.byref(startup),
        ctypes.byref(process),
    ):
        _winerror("CreateProcessW failed")
    return process


def _is_active(kernel32, handle):
    exit_code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        _winerror("GetExitCodeProcess failed")
    return exit_code.value == _STILL_ACTIVE


def _wait_for_tree(marker, grandchild_pid, kernel32, child_handle, deadline):
    while time.monotonic() < deadline:
        if marker.exists() and grandchild_pid.exists():
            return int(grandchild_pid.read_text(encoding="ascii"))
        if not _is_active(kernel32, child_handle):
            raise RuntimeError("Job Object child exited before creating its descendant")
        time.sleep(0.02)
    raise RuntimeError("Job Object child readiness timed out")


def _require_terminated(kernel32, handle, name):
    result = kernel32.WaitForSingleObject(handle, 5000)
    if result == _WAIT_TIMEOUT:
        raise RuntimeError(f"Job Object did not terminate the {name}")
    if result == _WAIT_FAILED:
        _winerror(f"WaitForSingleObject failed for {name}")
    if result != _WAIT_OBJECT_0:
        raise RuntimeError(f"unexpected wait result for {name}: {result}")


def _close(kernel32, handle):
    if handle:
        kernel32.CloseHandle(handle)


def _run_child(marker, grandchild_pid):
    if marker is None or grandchild_pid is None:
        raise ValueError("child mode requires marker and grandchild pid paths")
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    grandchild_pid.write_text(str(grandchild.pid), encoding="ascii")
    marker.write_bytes(b"started")
    time.sleep(60)


def probe(*, loader):
    kernel32 = _configure_api(loader)
    with tempfile.TemporaryDirectory(prefix="pony-windows-job-probe-") as temporary:
        root = Path(temporary)
        marker = root / "child-started"
        grandchild_pid_path = root / "grandchild-pid"
        job = child_handle = thread_handle = grandchild_handle = None
        process = None
        try:
            job = _create_kill_on_close_job(kernel32)
            process = _create_suspended_child(kernel32, marker, grandchild_pid_path)
            child_handle = process.hProcess
            thread_handle = process.hThread
            if marker.exists() or grandchild_pid_path.exists():
                raise RuntimeError("CREATE_SUSPENDED allowed child code to run")
            if not kernel32.AssignProcessToJobObject(job, child_handle):
                _winerror("AssignProcessToJobObject failed")
            if kernel32.ResumeThread(thread_handle) == _INFINITE:
                _winerror("ResumeThread failed")
            grandchild_pid = _wait_for_tree(
                marker,
                grandchild_pid_path,
                kernel32,
                child_handle,
                time.monotonic() + 10,
            )
            grandchild_handle = kernel32.OpenProcess(
                _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                grandchild_pid,
            )
            if not grandchild_handle:
                _winerror("OpenProcess failed for Job Object descendant")
            if not _is_active(kernel32, child_handle) or not _is_active(
                kernel32, grandchild_handle
            ):
                raise RuntimeError("Job Object process tree exited before containment check")
            if not kernel32.CloseHandle(job):
                _winerror("CloseHandle failed for Job Object")
            job = None
            _require_terminated(kernel32, child_handle, "child process")
            _require_terminated(kernel32, grandchild_handle, "grandchild process")
        finally:
            if job:
                kernel32.TerminateJobObject(job, 1)
                _close(kernel32, job)
            if (
                child_handle
                and kernel32.WaitForSingleObject(child_handle, 0) == _WAIT_TIMEOUT
            ):
                kernel32.TerminateProcess(child_handle, 1)
            _close(kernel32, grandchild_handle)
            _close(kernel32, thread_handle)
            _close(kernel32, child_handle)

    return {
        "schema_version": 1,
        "create_suspended_before_assignment": True,
        "kill_on_job_close": True,
        "descendant_process_terminated": True,
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_job_semantics_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        if args.child_marker is not None or args.grandchild_pid is not None:
            _run_child(args.child_marker, args.grandchild_pid)
            return 0
        report = probe(loader=ctypes.WinDLL)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"windows_job_semantics_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
