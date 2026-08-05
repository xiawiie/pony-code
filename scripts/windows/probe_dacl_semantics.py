#!/usr/bin/env python3
"""Probe the Windows DACL semantics required by Pony's private-state backend."""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import sys
import tempfile


_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_SET_ACCESS = 2
_NO_INHERITANCE = 0
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_WIN_WORLD_SID = 1
_SECURITY_MAX_SID_SIZE = 68


class _SidAndAttributes(ctypes.Structure):
    _fields_ = (
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    )


class _TokenUser(ctypes.Structure):
    _fields_ = (("User", _SidAndAttributes),)


class _TrusteeW(ctypes.Structure):
    _fields_ = (
        ("pMultipleTrustee", wintypes.LPVOID),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", wintypes.LPWSTR),
    )


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = (
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TrusteeW),
    )


class _AclSizeInformation(ctypes.Structure):
    _fields_ = (
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    )


class _AceHeader(ctypes.Structure):
    _fields_ = (
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    )


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = (
        ("Header", _AceHeader),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's required native Windows DACL semantics."
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    return parser


def _configure_api(loader):
    kernel32 = loader("kernel32", use_last_error=True)
    advapi32 = loader("advapi32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
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
    kernel32.LocalFree.argtypes = (wintypes.LPVOID,)
    kernel32.LocalFree.restype = wintypes.LPVOID

    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.SetEntriesInAclW.argtypes = (
        wintypes.ULONG,
        ctypes.POINTER(_ExplicitAccessW),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD
    advapi32.SetSecurityInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    advapi32.SetSecurityInfo.restype = wintypes.DWORD
    security_outputs = (
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi32.GetSecurityInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        *security_outputs,
    )
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        *security_outputs,
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = (
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    )
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = (wintypes.LPVOID,)
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.CreateWellKnownSid.argtypes = (
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.CreateWellKnownSid.restype = wintypes.BOOL
    return kernel32, advapi32


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _status_error(status, message):
    if status:
        raise OSError(status, message)


def _current_user_sid(kernel32, advapi32):
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        _winerror("OpenProcessToken failed")
    try:
        required = wintypes.DWORD()
        ctypes.set_last_error(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not required.value:
            _winerror("GetTokenInformation size query failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required,
            ctypes.byref(required),
        ):
            _winerror("GetTokenInformation failed")
        sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid
        if not sid or not advapi32.IsValidSid(sid):
            raise RuntimeError("current process token returned an invalid user SID")
        return buffer, sid
    finally:
        kernel32.CloseHandle(token)


def _world_sid(advapi32):
    buffer = ctypes.create_string_buffer(_SECURITY_MAX_SID_SIZE)
    size = wintypes.DWORD(len(buffer))
    if not advapi32.CreateWellKnownSid(
        _WIN_WORLD_SID,
        None,
        buffer,
        ctypes.byref(size),
    ):
        _winerror("CreateWellKnownSid failed")
    return buffer, ctypes.cast(buffer, wintypes.LPVOID)


def _open_object(kernel32, path, *, directory):
    flags = _FILE_FLAG_BACKUP_SEMANTICS if directory else 0
    handle = kernel32.CreateFileW(
        str(path),
        _READ_CONTROL | _WRITE_DAC,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _winerror("CreateFileW failed")
    return handle


def _set_private_dacl(kernel32, advapi32, handle, sid):
    trustee = _TrusteeW(
        None,
        0,
        _TRUSTEE_IS_SID,
        _TRUSTEE_IS_USER,
        ctypes.cast(sid, wintypes.LPWSTR),
    )
    entry = _ExplicitAccessW(_FILE_ALL_ACCESS, _SET_ACCESS, _NO_INHERITANCE, trustee)
    acl = wintypes.LPVOID()
    _status_error(
        advapi32.SetEntriesInAclW(1, ctypes.byref(entry), None, ctypes.byref(acl)),
        "SetEntriesInAclW failed",
    )
    try:
        _status_error(
            advapi32.SetSecurityInfo(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            ),
            "SetSecurityInfo failed",
        )
    finally:
        kernel32.LocalFree(acl)


def _verify_descriptor(advapi32, descriptor, owner, dacl, expected_sid, expected_mask):
    if not owner or not advapi32.IsValidSid(owner) or not advapi32.EqualSid(owner, expected_sid):
        raise RuntimeError("security owner drift detected")
    if not dacl:
        raise RuntimeError("private DACL is missing")

    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        _winerror("GetSecurityDescriptorControl failed")
    if not control.value & _SE_DACL_PROTECTED:
        raise RuntimeError("private DACL inheritance protection is missing")

    acl_info = _AclSizeInformation()
    if not advapi32.GetAclInformation(
        dacl,
        ctypes.byref(acl_info),
        ctypes.sizeof(acl_info),
        _ACL_SIZE_INFORMATION_CLASS,
    ):
        _winerror("GetAclInformation failed")
    if acl_info.AceCount != 1:
        raise RuntimeError("private DACL ACE set drift detected")

    ace_pointer = wintypes.LPVOID()
    if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
        _winerror("GetAce failed")
    ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
    minimum_size = _AccessAllowedAce.SidStart.offset + 8
    if (
        ace.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE
        or ace.Header.AceFlags != _NO_INHERITANCE
        or ace.Header.AceSize < minimum_size
        or ace.Mask != expected_mask
    ):
        raise RuntimeError("private DACL allow entry drift detected")
    ace_sid = wintypes.LPVOID(ctypes.addressof(ace) + _AccessAllowedAce.SidStart.offset)
    if not advapi32.IsValidSid(ace_sid) or not advapi32.EqualSid(ace_sid, expected_sid):
        raise RuntimeError("private DACL trustee drift detected")


def _read_security(kernel32, advapi32, *, handle=None, path=None, sid, mask):
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    outputs = (
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if handle is not None:
        status = advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            *outputs,
        )
        message = "GetSecurityInfo failed"
    else:
        status = advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            *outputs,
        )
        message = "GetNamedSecurityInfoW failed"
    _status_error(status, message)
    try:
        _verify_descriptor(advapi32, descriptor, owner, dacl, sid, mask)
    finally:
        kernel32.LocalFree(descriptor)


def _require_rejected(check, message):
    try:
        check()
    except RuntimeError:
        return True
    raise RuntimeError(message)


def _probe_object(kernel32, advapi32, path, *, directory, sid, foreign_sid):
    handle = _open_object(kernel32, path, directory=directory)
    try:
        _set_private_dacl(kernel32, advapi32, handle, sid)
        _read_security(kernel32, advapi32, handle=handle, sid=sid, mask=_FILE_ALL_ACCESS)
        _read_security(kernel32, advapi32, path=path, sid=sid, mask=_FILE_ALL_ACCESS)
        owner_drift_rejected = _require_rejected(
            lambda: _read_security(
                kernel32,
                advapi32,
                handle=handle,
                sid=foreign_sid,
                mask=_FILE_ALL_ACCESS,
            ),
            "owner drift was not rejected",
        )
        dacl_drift_rejected = _require_rejected(
            lambda: _read_security(
                kernel32,
                advapi32,
                handle=handle,
                sid=sid,
                mask=_FILE_ALL_ACCESS & ~0x00000002,
            ),
            "DACL drift was not rejected",
        )
    finally:
        kernel32.CloseHandle(handle)
    return {
        "owner_is_current_user": True,
        "protected_dacl": True,
        "explicit_allow_ace_count": 1,
        "handle_and_named_verification": True,
        "owner_drift_rejected": owner_drift_rejected,
        "dacl_drift_rejected": dacl_drift_rejected,
    }


def probe(loader):
    kernel32, advapi32 = _configure_api(loader)
    sid_buffer, sid = _current_user_sid(kernel32, advapi32)
    world_buffer, world_sid = _world_sid(advapi32)
    if advapi32.EqualSid(sid, world_sid):
        raise RuntimeError("current user SID unexpectedly equals the world SID")
    with tempfile.TemporaryDirectory(prefix="pony-windows-dacl-") as temp_root:
        root = Path(temp_root)
        directory = root / "private-directory"
        directory.mkdir()
        file_path = root / "private-file"
        file_path.write_bytes(b"pony-private-state")
        objects = {
            "directory": _probe_object(
                kernel32,
                advapi32,
                directory,
                directory=True,
                sid=sid,
                foreign_sid=world_sid,
            ),
            "file": _probe_object(
                kernel32,
                advapi32,
                file_path,
                directory=False,
                sid=sid,
                foreign_sid=world_sid,
            ),
        }
    _ = sid_buffer, world_buffer
    return {
        "schema_version": 1,
        "python": platform.python_version(),
        "objects": objects,
    }


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_dacl_semantics_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe(ctypes.WinDLL)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"windows_dacl_semantics_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
