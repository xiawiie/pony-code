#!/usr/bin/env python3
"""Probe the Windows file semantics required by Pony's private-state backend."""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_OPEN = 1
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _UnicodeString(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    )


class _ObjectAttributes(ctypes.Structure):
    _fields_ = (
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    )


class _IoStatusValue(ctypes.Union):
    _fields_ = (
        ("Status", wintypes.LONG),
        ("Pointer", wintypes.LPVOID),
    )


class _IoStatusBlock(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = (
        ("value", _IoStatusValue),
        ("Information", ctypes.c_size_t),
    )


class _FileIdInfo(ctypes.Structure):
    _fields_ = (
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", wintypes.BYTE * 16),
    )


class _FileStandardInfo(ctypes.Structure):
    _fields_ = (
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    )


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = (
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's required native Windows file semantics."
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    return parser


def _configure_api(loader):
    kernel32 = loader("kernel32", use_last_error=True)
    ntdll = loader("ntdll", use_last_error=True)
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
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetSystemDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    ntdll.NtCreateFile.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    ntdll.NtCreateFile.restype = wintypes.LONG
    ntdll.RtlNtStatusToDosError.argtypes = (wintypes.LONG,)
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
    return kernel32, ntdll


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _system_cmd(kernel32):
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        _winerror("GetSystemDirectoryW failed")
    return str(Path(buffer.value) / "cmd.exe")


def _create_directory_reparse(kernel32, target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
        return "directory_symlink"
    except OSError as exc:
        if getattr(exc, "winerror", None) not in {5, 1314}:
            raise
    command = subprocess.list2cmdline(
        ["mklink", "/J", str(link), str(Path(target).resolve(strict=True))]
    )
    completed = subprocess.run(
        [_system_cmd(kernel32), "/d", "/s", "/c", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 or not link.exists():
        raise RuntimeError("failed to create directory reparse fixture")
    return "junction"


def _open_absolute(kernel32, path, *, directory):
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    handle = kernel32.CreateFileW(
        str(path),
        _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _winerror("CreateFileW failed")
    return handle


def _component_name(value):
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("relative traversal requires one lexical path component")
    return value


def _open_relative(ntdll, root_handle, name, *, directory):
    name = _component_name(name)
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        length,
        length + 2,
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        root_handle,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
    options |= _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
    status = ntdll.NtCreateFile(
        ctypes.byref(handle),
        _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _FILE_SHARE_ALL,
        _FILE_OPEN,
        options,
        None,
        0,
    )
    if status < 0:
        error = ntdll.RtlNtStatusToDosError(status)
        raise OSError(error, f"NtCreateFile failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")
    return handle


def _query(kernel32, handle, info_class, result):
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        info_class,
        ctypes.byref(result),
        ctypes.sizeof(result),
    ):
        _winerror("GetFileInformationByHandleEx failed")
    return result


def _identity(kernel32, handle):
    info = _query(kernel32, handle, _FILE_ID_INFO_CLASS, _FileIdInfo())
    return info.VolumeSerialNumber, bytes(info.FileId)


def _link_count(kernel32, handle):
    return _query(
        kernel32, handle, _FILE_STANDARD_INFO_CLASS, _FileStandardInfo()
    ).NumberOfLinks


def _reparse_tag(kernel32, handle):
    info = _query(
        kernel32, handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, _FileAttributeTagInfo()
    )
    if not info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return None
    if not info.ReparseTag:
        raise RuntimeError("reparse point is missing a reparse tag")
    return info.ReparseTag


def _close(kernel32, *handles):
    for handle in reversed(handles):
        if handle and not kernel32.CloseHandle(handle):
            _winerror("CloseHandle failed")


def probe(*, loader):
    kernel32, ntdll = _configure_api(loader)
    with tempfile.TemporaryDirectory(prefix="pony-windows-file-probe-") as temporary:
        root = Path(temporary)
        nested = root / "nested"
        nested.mkdir()
        target = nested / "target.txt"
        target.write_bytes(b"pony")
        hardlink = nested / "hardlink.txt"
        os.link(target, hardlink)
        link = root / "nested-link"
        reparse_fixture = _create_directory_reparse(kernel32, nested, link)

        root_handle = nested_handle = relative_handle = absolute_handle = link_handle = None
        try:
            root_handle = _open_absolute(kernel32, root, directory=True)
            nested_handle = _open_relative(ntdll, root_handle, "nested", directory=True)
            relative_handle = _open_relative(
                ntdll, nested_handle, "target.txt", directory=False
            )
            absolute_handle = _open_absolute(kernel32, target, directory=False)
            link_handle = _open_relative(
                ntdll, root_handle, "nested-link", directory=True
            )

            relative_identity = _identity(kernel32, relative_handle)
            if relative_identity != _identity(kernel32, absolute_handle):
                raise RuntimeError("root-relative open changed file identity")
            if _reparse_tag(kernel32, relative_handle) is not None:
                raise RuntimeError("regular file reported as a reparse point")
            reparse_tag = _reparse_tag(kernel32, link_handle)
            if reparse_tag is None:
                raise RuntimeError("directory symlink was followed instead of opened")
            links = _link_count(kernel32, relative_handle)
            if links != 2:
                raise RuntimeError(f"expected hard-link count 2, got {links}")
        finally:
            _close(
                kernel32,
                root_handle,
                nested_handle,
                relative_handle,
                absolute_handle,
                link_handle,
            )

    volume, file_id = relative_identity
    return {
        "schema_version": 1,
        "root_handle_relative_open": True,
        "stable_file_identity": {
            "volume_serial": f"{volume:016x}",
            "file_id": file_id.hex(),
        },
        "hard_link_count": links,
        "reparse_point_rejected_before_traversal": True,
        "reparse_fixture": reparse_fixture,
        "reparse_tag": f"0x{reparse_tag:08x}",
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_file_semantics_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe(loader=ctypes.WinDLL)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"windows_file_semantics_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
