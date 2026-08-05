#!/usr/bin/env python3
"""Probe LockFileEx cross-process exclusion required by Pony state locks."""

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


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x00000007
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_LOCK_VIOLATION = 33
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _Overlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's required native Windows lock semantics."
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    parser.add_argument("--hold", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ready", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--release", type=Path, help=argparse.SUPPRESS)
    return parser


def _configure_api(loader):
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _open(kernel32, path):
    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_ALL,
        None,
        _OPEN_ALWAYS,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _winerror("CreateFileW failed")
    return handle


def _try_lock(kernel32, handle):
    overlapped = _Overlapped()
    ctypes.set_last_error(0)
    acquired = kernel32.LockFileEx(
        handle,
        _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    )
    return bool(acquired), ctypes.get_last_error(), overlapped


def _unlock(kernel32, handle, overlapped):
    if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        _winerror("UnlockFileEx failed")


def _close(kernel32, handle):
    if handle and not kernel32.CloseHandle(handle):
        _winerror("CloseHandle failed")


def _wait_for(path, process, deadline):
    while time.monotonic() < deadline:
        if path.exists():
            return
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"lock holder exited early with status {returncode}")
        time.sleep(0.02)
    raise RuntimeError("lock holder readiness timed out")


def _hold_lock(path, ready, release, *, loader):
    if ready is None or release is None:
        raise ValueError("lock holder requires ready and release paths")
    kernel32 = _configure_api(loader)
    handle = _open(kernel32, path)
    acquired, error, overlapped = _try_lock(kernel32, handle)
    if not acquired:
        _close(kernel32, handle)
        raise OSError(error, "lock holder could not acquire LockFileEx")
    try:
        ready.write_bytes(b"ready")
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("lock holder release timed out")
            time.sleep(0.02)
        _unlock(kernel32, handle, overlapped)
    finally:
        _close(kernel32, handle)


def probe(*, loader, popen):
    kernel32 = _configure_api(loader)
    with tempfile.TemporaryDirectory(prefix="pony-windows-lock-probe-") as temporary:
        root = Path(temporary)
        lock_path = root / "state.lock"
        ready = root / "ready"
        release = root / "release"
        process = popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--hold",
                str(lock_path),
                "--ready",
                str(ready),
                "--release",
                str(release),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        handle = None
        try:
            _wait_for(ready, process, time.monotonic() + 10)
            handle = _open(kernel32, lock_path)
            acquired, error, _overlapped = _try_lock(kernel32, handle)
            if acquired:
                raise RuntimeError("LockFileEx allowed a second process to acquire the lock")
            if error != _ERROR_LOCK_VIOLATION:
                raise OSError(error, "LockFileEx returned an unexpected conflict error")
            release.write_bytes(b"release")
            stdout, stderr = process.communicate(timeout=10)
            if process.returncode != 0:
                raise RuntimeError(
                    f"lock holder failed with status {process.returncode}: {stderr.strip()}"
                )
            if stdout.strip() or stderr.strip():
                raise RuntimeError("lock holder emitted unexpected output")
            acquired, error, overlapped = _try_lock(kernel32, handle)
            if not acquired:
                raise OSError(error, "LockFileEx was not released when the holder exited")
            _unlock(kernel32, handle, overlapped)
        finally:
            release.touch(exist_ok=True)
            if process.poll() is None:
                process.kill()
                process.communicate()
            if handle is not None:
                _close(kernel32, handle)

    return {
        "schema_version": 1,
        "cross_process_exclusion": True,
        "conflict_error": _ERROR_LOCK_VIOLATION,
        "reacquire_after_release": True,
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_lock_semantics_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        if args.hold is not None:
            _hold_lock(args.hold, args.ready, args.release, loader=ctypes.WinDLL)
            return 0
        if args.ready is not None or args.release is not None:
            raise ValueError("ready/release are only valid with hold")
        report = probe(loader=ctypes.WinDLL, popen=subprocess.Popen)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"windows_lock_semantics_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
