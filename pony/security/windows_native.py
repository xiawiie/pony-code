"""Native Windows file-handle and private-DACL primitives."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path


_FILE_READ_DATA = 0x0001
_FILE_WRITE_DATA = 0x0002
_FILE_APPEND_DATA = 0x0004
_FILE_TRAVERSE = 0x0020
_FILE_DELETE_CHILD = 0x0040
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_WRITE_ATTRIBUTES = 0x0100
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_SYNCHRONIZE = 0x00100000
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_SHARE_READ_WRITE = 0x00000003
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_OVERWRITE_IF = 5
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_CREATED = 2
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_BASIC_INFO_CLASS = 0
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
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
_SECURITY_DESCRIPTOR_REVISION = 1
_MOVEFILE_WRITE_THROUGH = 0x00000008
_RESERVED_DOS_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{value}" for value in range(1, 10)),
    *(f"lpt{value}" for value in range(1, 10)),
}


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
    _fields_ = (("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID))


class _IoStatusBlock(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = (("value", _IoStatusValue), ("Information", ctypes.c_size_t))


class _FileIdInfo(ctypes.Structure):
    _fields_ = (
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", wintypes.BYTE * 16),
    )


class _FileBasicInfo(ctypes.Structure):
    _fields_ = (
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
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
    _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = (("DeleteFile", wintypes.BOOLEAN),)


class _FileRenameInfo(ctypes.Structure):
    _fields_ = (
        ("ReplaceIfExists", wintypes.BOOLEAN),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    )


class _SidAndAttributes(ctypes.Structure):
    _fields_ = (("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD))


class _SecurityDescriptor(ctypes.Structure):
    _fields_ = (
        ("Revision", wintypes.BYTE),
        ("Sbz1", wintypes.BYTE),
        ("Control", wintypes.WORD),
        ("Owner", wintypes.LPVOID),
        ("Group", wintypes.LPVOID),
        ("Sacl", wintypes.LPVOID),
        ("Dacl", wintypes.LPVOID),
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


@dataclass(frozen=True)
class FileFacts:
    filesystem_id: int
    file_id: bytes
    size: int
    modified_ns: int
    changed_ns: int
    link_count: int
    directory: bool
    reparse_tag: int


class Handle:
    def __init__(self, value):
        self.value = value

    def close(self):
        if self.value not in {None, _INVALID_HANDLE_VALUE}:
            api().kernel32.CloseHandle(self.value)
            self.value = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _Api:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("native Windows file security is unavailable")
        loader = ctypes.WinDLL
        self.kernel32 = loader("kernel32", use_last_error=True)
        self.advapi32 = loader("advapi32", use_last_error=True)
        self.ntdll = loader("ntdll", use_last_error=True)
        self._configure()
        self.sid_buffer, self.current_sid = self._current_user_sid()

    def _configure(self):
        kernel32 = self.kernel32
        advapi32 = self.advapi32
        ntdll = self.ntdll
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetWindowsDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
        kernel32.GetWindowsDirectoryW.restype = wintypes.UINT
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
        kernel32.SetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (wintypes.LPVOID,)
        kernel32.LocalFree.restype = wintypes.LPVOID
        kernel32.DeleteFileW.argtypes = (wintypes.LPCWSTR,)
        kernel32.DeleteFileW.restype = wintypes.BOOL
        kernel32.MoveFileExW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        )
        kernel32.MoveFileExW.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.ReplaceFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        kernel32.ReplaceFileW.restype = wintypes.BOOL
        kernel32.SetFilePointerEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong),
            wintypes.DWORD,
        )
        kernel32.SetFilePointerEx.restype = wintypes.BOOL
        kernel32.SetEndOfFile.argtypes = (wintypes.HANDLE,)
        kernel32.SetEndOfFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.WriteFile.restype = wintypes.BOOL

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
        advapi32.InitializeSecurityDescriptor.argtypes = (wintypes.LPVOID, wintypes.DWORD)
        advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
        advapi32.SetSecurityDescriptorDacl.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPVOID,
            wintypes.BOOL,
        )
        advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.SetSecurityDescriptorOwner.argtypes = (
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
        )
        advapi32.SetSecurityDescriptorOwner.restype = wintypes.BOOL
        advapi32.SetSecurityDescriptorControl.argtypes = (
            wintypes.LPVOID,
            wintypes.WORD,
            wintypes.WORD,
        )
        advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL
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
        outputs = (
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
            *outputs,
        )
        advapi32.GetSecurityInfo.restype = wintypes.DWORD
        advapi32.GetSecurityDescriptorControl.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorDacl.argtypes = (
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.BOOL),
        )
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
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
        advapi32.GetLengthSid.argtypes = (wintypes.LPVOID,)
        advapi32.GetLengthSid.restype = wintypes.DWORD
        advapi32.IsValidSid.argtypes = (wintypes.LPVOID,)
        advapi32.IsValidSid.restype = wintypes.BOOL
        advapi32.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
        advapi32.EqualSid.restype = wintypes.BOOL

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

    def _current_user_sid(self):
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            _winerror("OpenProcessToken failed")
        try:
            required = wintypes.DWORD()
            ctypes.set_last_error(0)
            self.advapi32.GetTokenInformation(
                token, _TOKEN_USER, None, 0, ctypes.byref(required)
            )
            if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or not required.value:
                _winerror("GetTokenInformation size query failed")
            buffer = ctypes.create_string_buffer(required.value)
            if not self.advapi32.GetTokenInformation(
                token, _TOKEN_USER, buffer, required, ctypes.byref(required)
            ):
                _winerror("GetTokenInformation failed")
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid
            if not sid or not self.advapi32.IsValidSid(sid):
                raise RuntimeError("current process token returned an invalid user SID")
            return buffer, sid
        finally:
            self.kernel32.CloseHandle(token)


@lru_cache(maxsize=1)
def api():
    return _Api()


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _status_error(status, message):
    if status:
        raise OSError(int(status), message)


def error_code(exc):
    return getattr(exc, "winerror", None) or exc.errno


def lexical_component(value):
    value = os.fsdecode(os.fspath(value))
    if (
        not value
        or value in {".", ".."}
        or value[-1] in {" ", "."}
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in value)
        or value.split(".", 1)[0].casefold() in _RESERVED_DOS_NAMES
    ):
        raise ValueError("unsafe Windows path component")
    return value


def lexical_absolute(path):
    path = Path(os.path.abspath(os.fspath(path)))
    if not path.anchor:
        raise ValueError("Windows path must be absolute")
    for component in path.parts[1:]:
        lexical_component(component)
    return path


def _win32_path(path):
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def win32_path(path):
    return _win32_path(lexical_absolute(path))


@contextmanager
def private_security_descriptor():
    native = api()
    trustee = _TrusteeW(
        None,
        0,
        _TRUSTEE_IS_SID,
        _TRUSTEE_IS_USER,
        ctypes.cast(native.current_sid, wintypes.LPWSTR),
    )
    entry = _ExplicitAccessW(_FILE_ALL_ACCESS, _SET_ACCESS, _NO_INHERITANCE, trustee)
    acl = wintypes.LPVOID()
    _status_error(
        native.advapi32.SetEntriesInAclW(1, ctypes.byref(entry), None, ctypes.byref(acl)),
        "SetEntriesInAclW failed",
    )
    descriptor = _SecurityDescriptor()
    descriptor_pointer = ctypes.byref(descriptor)
    try:
        if not native.advapi32.InitializeSecurityDescriptor(
            descriptor_pointer, _SECURITY_DESCRIPTOR_REVISION
        ):
            _winerror("InitializeSecurityDescriptor failed")
        if not native.advapi32.SetSecurityDescriptorDacl(descriptor_pointer, True, acl, False):
            _winerror("SetSecurityDescriptorDacl failed")
        if not native.advapi32.SetSecurityDescriptorOwner(
            descriptor_pointer, native.current_sid, False
        ):
            _winerror("SetSecurityDescriptorOwner failed")
        if not native.advapi32.SetSecurityDescriptorControl(
            descriptor_pointer, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED
        ):
            _winerror("SetSecurityDescriptorControl failed")
        yield descriptor_pointer
    finally:
        native.kernel32.LocalFree(acl)


def _open_root(path, *, desired_access=None, share_access=_FILE_SHARE_ALL):
    native = api()
    access = desired_access
    if access is None:
        access = _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL
    handle = native.kernel32.CreateFileW(
        _win32_path(Path(path.anchor)),
        access | _SYNCHRONIZE,
        share_access,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _winerror("CreateFileW root open failed")
    opened = Handle(handle)
    try:
        require_kind(opened, directory=True)
        return opened
    except Exception:
        opened.close()
        raise


def open_relative(
    root,
    name,
    *,
    directory,
    desired_access,
    disposition=_FILE_OPEN,
    security_descriptor=None,
    share_access=_FILE_SHARE_ALL,
    single_link=True,
):
    native = api()
    name = lexical_component(name)
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        root.value,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        ctypes.cast(security_descriptor, wintypes.LPVOID) if security_descriptor else None,
        None,
    )
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
    options |= _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
    status = native.ntdll.NtCreateFile(
        ctypes.byref(handle),
        desired_access | _SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        share_access,
        disposition,
        options,
        None,
        0,
    )
    if status < 0:
        error = native.ntdll.RtlNtStatusToDosError(status)
        raise OSError(error, "NtCreateFile failed")
    opened = Handle(handle.value)
    try:
        require_kind(opened, directory=directory, single_link=single_link)
        return opened, io_status.Information == _FILE_CREATED
    except Exception:
        opened.close()
        raise


def open_path(
    path,
    *,
    directory,
    desired_access=None,
    share_access=_FILE_SHARE_ALL,
    single_link=True,
):
    path = lexical_absolute(path)
    if desired_access is None:
        desired_access = (
            _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL
            if directory
            else FILE_READ_ACCESS
        )
    current = _open_root(
        path,
        desired_access=desired_access if len(path.parts) == 1 else None,
        share_access=share_access,
    )
    try:
        if len(path.parts) == 1:
            if not directory:
                raise ValueError("regular file required")
            return current
        for index, component in enumerate(path.parts[1:]):
            final = index == len(path.parts[1:]) - 1
            child, _created = open_relative(
                current,
                component,
                directory=directory if final else True,
                desired_access=(
                    desired_access
                    if final
                    else _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL
                ),
                share_access=share_access,
                single_link=single_link if final else True,
            )
            current.close()
            current = child
        return current
    except Exception:
        current.close()
        raise


def ensure_directory(path):
    path = lexical_absolute(path)
    current = _open_root(path)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            final = index == len(components) - 1
            access = (
                _FILE_ALL_ACCESS
                if final
                else _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL
            )
            try:
                child, _created = open_relative(
                    current,
                    component,
                    directory=True,
                    desired_access=access,
                )
            except OSError as exc:
                if error_code(exc) not in {2, 3}:
                    raise
                with private_security_descriptor() as descriptor:
                    child, _created = open_relative(
                        current,
                        component,
                        directory=True,
                        desired_access=_FILE_ALL_ACCESS,
                        disposition=_FILE_CREATE,
                        security_descriptor=descriptor,
                    )
            current.close()
            current = child
        require_current_owner(current)
        make_private(current)
        require_private(current)
        return path
    finally:
        current.close()


def open_parent(
    path,
    *,
    trusted_root=None,
    trusted_root_identity=None,
    share_access=_FILE_SHARE_ALL,
):
    path = lexical_absolute(path)
    if trusted_root is None:
        return path, open_path(
            path.parent, directory=True, share_access=share_access
        )
    root = lexical_absolute(trusted_root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("private path escapes trusted root") from exc
    if not relative.parts:
        raise ValueError("private path must name a file")
    current = open_path(root, directory=True, share_access=share_access)
    try:
        if trusted_root_identity is None or identity(current) != tuple(trusted_root_identity):
            raise ValueError("private root changed")
        for component in relative.parts[:-1]:
            child, _created = open_relative(
                current,
                component,
                directory=True,
                desired_access=_FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL,
                share_access=share_access,
            )
            current.close()
            current = child
        return path, current
    except Exception:
        current.close()
        raise


def facts(handle):
    native = api()

    def query(info_class, value):
        if not native.kernel32.GetFileInformationByHandleEx(
            handle.value, info_class, ctypes.byref(value), ctypes.sizeof(value)
        ):
            _winerror("GetFileInformationByHandleEx failed")
        return value

    file_id = query(_FILE_ID_INFO_CLASS, _FileIdInfo())
    basic = query(_FILE_BASIC_INFO_CLASS, _FileBasicInfo())
    standard = query(_FILE_STANDARD_INFO_CLASS, _FileStandardInfo())
    tag = query(_FILE_ATTRIBUTE_TAG_INFO_CLASS, _FileAttributeTagInfo())
    return FileFacts(
        filesystem_id=file_id.VolumeSerialNumber,
        file_id=bytes(file_id.FileId),
        size=standard.EndOfFile,
        modified_ns=basic.LastWriteTime * 100,
        changed_ns=basic.ChangeTime * 100,
        link_count=standard.NumberOfLinks,
        directory=bool(standard.Directory),
        reparse_tag=tag.ReparseTag if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT else 0,
    )


def identity(handle):
    value = facts(handle)
    return value.filesystem_id, value.file_id


def require_kind(handle, *, directory, single_link=True):
    value = facts(handle)
    if value.reparse_tag:
        raise ValueError("refusing reparse point component")
    if value.directory != directory:
        raise ValueError("private path has unsafe component")
    if not directory and single_link and value.link_count != 1:
        raise ValueError("private file has multiple links")
    return value


def windows_directory():
    native = api()
    size = 260
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = native.kernel32.GetWindowsDirectoryW(buffer, size)
        if not length:
            _winerror("GetWindowsDirectoryW failed")
        if length < size:
            return lexical_absolute(buffer.value)
        size = length + 1


def path_is_mutable_by_current_user(path, *, directory, contents=True):
    path = Path(path)
    accesses = (_WRITE_DAC, _WRITE_OWNER)
    if not directory or path.parent != path:
        accesses = (_DELETE, *accesses)
    if not directory:
        accesses = (_FILE_WRITE_ATTRIBUTES, *accesses)
    if directory:
        accesses += (_FILE_DELETE_CHILD,)
        if contents:
            accesses += (_FILE_WRITE_DATA, _FILE_APPEND_DATA)
    else:
        accesses += (_FILE_WRITE_DATA, _FILE_APPEND_DATA)
    for access in accesses:
        try:
            handle = open_path(
                path,
                directory=directory,
                desired_access=access,
                single_link=False,
            )
        except OSError as exc:
            if error_code(exc) in {5, 1314}:
                continue
            raise
        else:
            handle.close()
            return True
    return False


def _security_snapshot(handle):
    native = api()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    status = native.advapi32.GetSecurityInfo(
        handle.value,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    _status_error(status, "GetSecurityInfo failed")
    return native, descriptor, owner, dacl


def protection_identity(handle):
    native, descriptor, owner, dacl = _security_snapshot(handle)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not native.advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            _winerror("GetSecurityDescriptorControl failed")
        owner_size = native.advapi32.GetLengthSid(owner) if owner else 0
        acl_info = _AclSizeInformation()
        if dacl and not native.advapi32.GetAclInformation(
            dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            _winerror("GetAclInformation failed")
        return (
            ctypes.string_at(owner, owner_size) if owner_size else b"",
            ctypes.string_at(dacl, acl_info.AclBytesInUse) if dacl else b"",
            bool(control.value & _SE_DACL_PROTECTED),
        )
    finally:
        native.kernel32.LocalFree(descriptor)


def require_current_owner(handle):
    native, descriptor, owner, _dacl = _security_snapshot(handle)
    try:
        if not owner or not native.advapi32.EqualSid(owner, native.current_sid):
            raise ValueError("private file owner is unsafe")
    finally:
        native.kernel32.LocalFree(descriptor)


def require_private(handle):
    native, descriptor, owner, dacl = _security_snapshot(handle)
    try:
        if not owner or not native.advapi32.EqualSid(owner, native.current_sid):
            raise ValueError("private file owner is unsafe")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not native.advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            _winerror("GetSecurityDescriptorControl failed")
        if not dacl or not control.value & _SE_DACL_PROTECTED:
            raise ValueError("private file permissions are unsafe")
        info = _AclSizeInformation()
        if not native.advapi32.GetAclInformation(
            dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION_CLASS
        ):
            _winerror("GetAclInformation failed")
        if info.AceCount != 1:
            raise ValueError("private file permissions are unsafe")
        pointer = wintypes.LPVOID()
        if not native.advapi32.GetAce(dacl, 0, ctypes.byref(pointer)):
            _winerror("GetAce failed")
        ace = ctypes.cast(pointer, ctypes.POINTER(_AccessAllowedAce)).contents
        sid = wintypes.LPVOID(ctypes.addressof(ace) + _AccessAllowedAce.SidStart.offset)
        minimum_size = _AccessAllowedAce.SidStart.offset + 8
        if (
            ace.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE
            or ace.Header.AceFlags != _NO_INHERITANCE
            or ace.Header.AceSize < minimum_size
            or ace.Mask != _FILE_ALL_ACCESS
            or not native.advapi32.IsValidSid(sid)
            or not native.advapi32.EqualSid(sid, native.current_sid)
        ):
            raise ValueError("private file permissions are unsafe")
    finally:
        native.kernel32.LocalFree(descriptor)


def make_private(handle):
    native = api()
    with private_security_descriptor() as descriptor:
        absolute = ctypes.cast(descriptor, wintypes.LPVOID)
        # Extracting the DACL avoids a second independently-built ACL policy.
        dacl_present = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        defaulted = wintypes.BOOL()
        if not native.advapi32.GetSecurityDescriptorDacl(
            absolute, ctypes.byref(dacl_present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            _winerror("GetSecurityDescriptorDacl failed")
        if not dacl_present or not dacl:
            raise RuntimeError("private DACL construction failed")
        _status_error(
            native.advapi32.SetSecurityInfo(
                handle.value,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION
                | _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                native.current_sid,
                None,
                dacl,
                None,
            ),
            "SetSecurityInfo failed",
        )


def read_chunks(handle):
    native = api()
    if not native.kernel32.SetFilePointerEx(handle.value, 0, None, 0):
        _winerror("SetFilePointerEx failed")
    while True:
        buffer = ctypes.create_string_buffer(64 * 1024)
        read = wintypes.DWORD()
        if not native.kernel32.ReadFile(
            handle.value, buffer, len(buffer), ctypes.byref(read), None
        ):
            _winerror("ReadFile failed")
        if not read.value:
            return
        yield buffer.raw[: read.value]


def read_bytes(handle, *, max_bytes=None):
    chunks = []
    total = 0
    for chunk in read_chunks(handle):
        total += len(chunk)
        if max_bytes is not None and total > int(max_bytes):
            raise ValueError("private file too large")
        chunks.append(chunk)
    return b"".join(chunks)


def write_bytes(handle, data, *, append=False):
    native = api()
    position = 2 if append else 0
    if not native.kernel32.SetFilePointerEx(handle.value, 0, None, position):
        _winerror("SetFilePointerEx failed")
    if not append and not native.kernel32.SetEndOfFile(handle.value):
        _winerror("SetEndOfFile failed")
    view = memoryview(data)
    while view:
        chunk = bytes(view[: 64 * 1024])
        buffer = ctypes.create_string_buffer(chunk)
        written = wintypes.DWORD()
        if not native.kernel32.WriteFile(
            handle.value, buffer, len(chunk), ctypes.byref(written), None
        ):
            _winerror("WriteFile failed")
        if not written.value:
            raise OSError("WriteFile made no progress")
        view = view[written.value :]
    if not native.kernel32.FlushFileBuffers(handle.value):
        _winerror("FlushFileBuffers failed")


def truncate(handle, size):
    native = api()
    if not native.kernel32.SetFilePointerEx(handle.value, int(size), None, 0):
        _winerror("SetFilePointerEx failed")
    if not native.kernel32.SetEndOfFile(handle.value):
        _winerror("SetEndOfFile failed")
    if not native.kernel32.FlushFileBuffers(handle.value):
        _winerror("FlushFileBuffers failed")


def rename_handle(handle, destination_parent, destination_name):
    name = lexical_component(destination_name).encode("utf-16-le")
    size = ctypes.sizeof(_FileRenameInfo) + len(name)
    buffer = ctypes.create_string_buffer(size)
    info = _FileRenameInfo.from_buffer(buffer)
    info.ReplaceIfExists = False
    info.RootDirectory = destination_parent.value
    info.FileNameLength = len(name)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FileRenameInfo.FileName.offset, name, len(name)
    )
    if not api().kernel32.SetFileInformationByHandle(handle.value, 3, buffer, size):
        _winerror("SetFileInformationByHandle rename failed")


def delete_handle(handle):
    info = _FileDispositionInfo(True)
    if not api().kernel32.SetFileInformationByHandle(
        handle.value,
        4,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        _winerror("SetFileInformationByHandle delete failed")


def delete_file(path, *, missing_ok=False):
    native = api()
    if native.kernel32.DeleteFileW(_win32_path(path)):
        return
    error = ctypes.get_last_error()
    if missing_ok and error in {2, 3}:
        return
    raise OSError(error, "DeleteFileW failed")


def move_file(source, destination):
    native = api()
    if not native.kernel32.MoveFileExW(
        _win32_path(source), _win32_path(destination), _MOVEFILE_WRITE_THROUGH
    ):
        _winerror("MoveFileExW failed")


def replace_file(target, replacement, backup=None):
    native = api()
    if not native.kernel32.ReplaceFileW(
        _win32_path(target),
        _win32_path(replacement),
        _win32_path(backup) if backup is not None else None,
        0,
        None,
        None,
    ):
        _winerror("ReplaceFileW failed")


FILE_READ_ACCESS = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _READ_CONTROL
FILE_DIRECTORY_ACCESS = _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _READ_CONTROL
FILE_DELETE_ACCESS = FILE_READ_ACCESS | _DELETE
DIRECTORY_DELETE_ACCESS = FILE_DIRECTORY_ACCESS | _DELETE
FILE_REPLACE_ACCESS = (
    _FILE_READ_DATA
    | _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_READ_ATTRIBUTES
    | _DELETE
)
FILE_SHARE_ALL = _FILE_SHARE_ALL
FILE_SHARE_READ_WRITE = _FILE_SHARE_READ_WRITE
FILE_WRITE_ACCESS = (
    _FILE_READ_DATA
    | _FILE_WRITE_DATA
    | _FILE_APPEND_DATA
    | _FILE_READ_ATTRIBUTES
    | _FILE_WRITE_ATTRIBUTES
    | _READ_CONTROL
    | _WRITE_DAC
    | _WRITE_OWNER
    | _DELETE
)
FILE_LOCK_ACCESS = FILE_WRITE_ACCESS & ~_DELETE
FILE_OPEN = _FILE_OPEN
FILE_CREATE = _FILE_CREATE
FILE_OPEN_IF = _FILE_OPEN_IF
FILE_OVERWRITE_IF = _FILE_OVERWRITE_IF
FILE_ALL_ACCESS = _FILE_ALL_ACCESS
