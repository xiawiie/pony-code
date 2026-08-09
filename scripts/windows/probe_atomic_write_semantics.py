#!/usr/bin/env python3
"""Probe the Windows atomic replacement semantics required by Pony state writes."""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import secrets
import sys
import tempfile

import probe_dacl_semantics as dacl


_FILE_READ_ATTRIBUTES = 0x0080
_READ_CONTROL = 0x00020000
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_ID_INFO_CLASS = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _FileIdInfo(ctypes.Structure):
    _fields_ = (
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", wintypes.BYTE * 16),
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's required native Windows atomic-write semantics."
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
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
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.ReplaceFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    kernel32.ReplaceFileW.restype = wintypes.BOOL
    return kernel32


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _open_file(kernel32, path):
    handle = kernel32.CreateFileW(
        str(path),
        _FILE_READ_ATTRIBUTES | _READ_CONTROL,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _winerror("CreateFileW failed")
    return handle


def _file_id(kernel32, path):
    handle = _open_file(kernel32, path)
    try:
        value = _FileIdInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(value),
            ctypes.sizeof(value),
        ):
            _winerror("GetFileInformationByHandleEx(FileIdInfo) failed")
        return value.VolumeSerialNumber, bytes(value.FileId).hex()
    finally:
        kernel32.CloseHandle(handle)


def _write_durable(kernel32, path, data):
    import msvcrt

    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        native_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
        if not kernel32.FlushFileBuffers(native_handle):
            _winerror("FlushFileBuffers failed")


def _set_private_dacl(kernel32, advapi32, path, sid):
    handle = dacl._open_object(kernel32, path, directory=False)
    try:
        dacl._set_private_dacl(kernel32, advapi32, handle, sid)
        dacl._read_security(
            kernel32,
            advapi32,
            handle=handle,
            sid=sid,
            mask=dacl._FILE_ALL_ACCESS,
        )
    finally:
        kernel32.CloseHandle(handle)


def probe(loader):
    kernel32 = _configure_api(loader)
    dacl_kernel32, advapi32 = dacl._configure_api(loader)
    sid_buffer, sid = dacl._current_user_sid(dacl_kernel32, advapi32)
    old_data = b"old-private-state"
    new_data = b"new-private-state"
    with tempfile.TemporaryDirectory(prefix="pony-windows-atomic-") as temp_root:
        root = Path(temp_root)
        target = root / "state.json"
        replacement = root / f".state.{secrets.token_hex(8)}.tmp"
        missing = root / "missing.tmp"
        backup = root / "state.backup"

        _write_durable(kernel32, target, old_data)
        _set_private_dacl(dacl_kernel32, advapi32, target, sid)
        old_id = _file_id(kernel32, target)

        ctypes.set_last_error(0)
        if kernel32.ReplaceFileW(str(target), str(missing), None, 0, None, None):
            raise RuntimeError("ReplaceFileW unexpectedly accepted a missing replacement")
        if not ctypes.get_last_error() or target.read_bytes() != old_data:
            raise RuntimeError("failed replacement did not preserve the old state")

        _write_durable(kernel32, replacement, new_data)
        _set_private_dacl(dacl_kernel32, advapi32, replacement, sid)
        replacement_id = _file_id(kernel32, replacement)
        if not kernel32.ReplaceFileW(
            str(target),
            str(replacement),
            str(backup),
            0,
            None,
            None,
        ):
            _winerror("ReplaceFileW failed")

        if target.read_bytes() != new_data or backup.read_bytes() != old_data:
            raise RuntimeError("ReplaceFileW content result is invalid")
        if replacement.exists():
            raise RuntimeError("replacement name still exists after ReplaceFileW")
        if _file_id(kernel32, target) != replacement_id:
            raise RuntimeError("replacement File ID did not follow the installed file")
        if _file_id(kernel32, backup) != old_id:
            raise RuntimeError("backup File ID did not preserve the replaced file")
        dacl._read_security(
            dacl_kernel32,
            advapi32,
            path=target,
            sid=sid,
            mask=dacl._FILE_ALL_ACCESS,
        )
    _ = sid_buffer
    return {
        "schema_version": 1,
        "python": platform.python_version(),
        "same_directory_temp": True,
        "temp_flushed": True,
        "failed_replace_preserved_old": True,
        "successful_replace_installed_new": True,
        "file_identity_transition_verified": True,
        "protected_dacl_preserved": True,
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_atomic_write_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe(ctypes.WinDLL)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"windows_atomic_write_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
