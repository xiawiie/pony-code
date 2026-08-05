"""Native Windows process-tree capture using a kill-on-close Job Object."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import threading
import time


_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_HANDLE_FLAG_INHERIT = 0x00000001
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_TERMINATED_EXIT_CODE = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    )


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


class _StartupInfoEx(ctypes.Structure):
    _fields_ = (("StartupInfo", _StartupInfo), ("lpAttributeList", wintypes.LPVOID))


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


@dataclass(frozen=True)
class CapturedWindowsProcess:
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool
    output_limit_exceeded: bool


class _Native:
    def __init__(self, loader):
        kernel32 = loader("kernel32", use_last_error=True)
        kernel32.CreatePipe.argtypes = (
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(_SecurityAttributes),
            wintypes.DWORD,
        )
        kernel32.CreatePipe.restype = wintypes.BOOL
        kernel32.SetHandleInformation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = (
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        )
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = (
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            wintypes.LPVOID,
        )
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = (wintypes.LPVOID,)
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
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32 = kernel32


@lru_cache(maxsize=1)
def _native():
    if os.name != "nt":
        raise RuntimeError("Windows process backend requires Windows")
    return _Native(ctypes.WinDLL)


def _winerror(message):
    raise OSError(ctypes.get_last_error(), message)


def _close(native, handle):
    if handle:
        native.kernel32.CloseHandle(handle)


def _environment_block(env):
    entries = []
    for key, value in env.items():
        key = str(key)
        value = str(value)
        if not key or "\0" in key or "\0" in value or "=" in key.lstrip("="):
            raise ValueError("invalid Windows process environment")
        entries.append(f"{key}={value}")
    entries.sort(key=str.casefold)
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


def _create_pipe(native, security):
    read_handle = wintypes.HANDLE()
    write_handle = wintypes.HANDLE()
    if not native.kernel32.CreatePipe(
        ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(security), 0
    ):
        _winerror("CreatePipe failed")
    if not native.kernel32.SetHandleInformation(
        read_handle, _HANDLE_FLAG_INHERIT, 0
    ):
        _close(native, read_handle)
        _close(native, write_handle)
        _winerror("SetHandleInformation failed")
    return read_handle.value, write_handle.value


def _create_job(native):
    job = native.kernel32.CreateJobObjectW(None, None)
    if not job:
        _winerror("CreateJobObjectW failed")
    limits = _JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not native.kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        _close(native, job)
        _winerror("SetInformationJobObject failed")
    return job


def _create_attribute_list(native, inherited_handles):
    size = ctypes.c_size_t()
    native.kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        _winerror("InitializeProcThreadAttributeList sizing failed")
    storage = ctypes.create_string_buffer(size.value)
    attributes = ctypes.cast(storage, wintypes.LPVOID)
    if not native.kernel32.InitializeProcThreadAttributeList(
        attributes, 1, 0, ctypes.byref(size)
    ):
        _winerror("InitializeProcThreadAttributeList failed")
    handles = (wintypes.HANDLE * len(inherited_handles))(*inherited_handles)
    if not native.kernel32.UpdateProcThreadAttribute(
        attributes,
        0,
        _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.cast(handles, wintypes.LPVOID),
        ctypes.sizeof(handles),
        None,
        None,
    ):
        native.kernel32.DeleteProcThreadAttributeList(attributes)
        _winerror("UpdateProcThreadAttribute failed")
    return storage, attributes, handles


def _terminate(native, job, process, *, assigned):
    if assigned:
        if not native.kernel32.TerminateJobObject(job, _TERMINATED_EXIT_CODE):
            _winerror("TerminateJobObject failed")
    elif process and not native.kernel32.TerminateProcess(
        process, _TERMINATED_EXIT_CODE
    ):
        _winerror("TerminateProcess failed")


def _wait(native, process, milliseconds):
    result = native.kernel32.WaitForSingleObject(process, milliseconds)
    if result == _WAIT_FAILED:
        _winerror("WaitForSingleObject failed")
    if result not in {_WAIT_OBJECT_0, _WAIT_TIMEOUT}:
        raise RuntimeError(f"unexpected process wait result: {result}")
    return result == _WAIT_OBJECT_0


def _exit_code(native, process):
    code = wintypes.DWORD()
    if not native.kernel32.GetExitCodeProcess(process, ctypes.byref(code)):
        _winerror("GetExitCodeProcess failed")
    return int(code.value)


def capture_process(
    argv,
    *,
    cwd,
    env,
    timeout,
    executable=None,
    max_output_bytes,
):
    """Run one command in a Job Object and capture bounded stdout/stderr."""
    import msvcrt

    native = _native()
    command = [str(arg) for arg in argv]
    if not command:
        raise ValueError("empty Windows process command")
    limit = int(max_output_bytes)
    if limit < 1:
        raise ValueError("invalid process output limit")
    application = str(executable or command[0])
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    environment = _environment_block(env)
    directory = str(Path(cwd).resolve())
    security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
    stdout_read = stdout_write = stderr_read = stderr_write = stdin_handle = None
    process = thread_handle = job = attributes = None
    assigned = False
    reader_threads = []
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()
    reader_errors = []

    def read_stream(name, descriptor):
        nonlocal total
        try:
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    return
                with lock:
                    if total + len(chunk) > limit:
                        overflow.set()
                        return
                    buffers[name].extend(chunk)
                    total += len(chunk)
        except OSError as exc:
            reader_errors.append(exc)
        finally:
            os.close(descriptor)

    primary = None
    try:
        stdout_read, stdout_write = _create_pipe(native, security)
        stderr_read, stderr_write = _create_pipe(native, security)
        stdin_handle = native.kernel32.CreateFileW(
            "NUL",
            _GENERIC_READ,
            _FILE_SHARE_READ_WRITE,
            ctypes.byref(security),
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if stdin_handle == _INVALID_HANDLE_VALUE:
            stdin_handle = None
            _winerror("CreateFileW failed for NUL")
        job = _create_job(native)
        _storage, attributes, _handles = _create_attribute_list(
            native, (stdin_handle, stdout_write, stderr_write)
        )
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_handle
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stderr_write
        startup.lpAttributeList = attributes
        process_info = _ProcessInformation()
        flags = (
            _CREATE_SUSPENDED
            | _CREATE_NO_WINDOW
            | _CREATE_UNICODE_ENVIRONMENT
            | _EXTENDED_STARTUPINFO_PRESENT
        )
        if not native.kernel32.CreateProcessW(
            application,
            command_line,
            None,
            None,
            True,
            flags,
            ctypes.cast(environment, wintypes.LPVOID),
            directory,
            ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfo)),
            ctypes.byref(process_info),
        ):
            _winerror("CreateProcessW failed")
        process = process_info.hProcess
        thread_handle = process_info.hThread
        native.kernel32.DeleteProcThreadAttributeList(attributes)
        attributes = None
        _close(native, stdin_handle)
        stdin_handle = None
        _close(native, stdout_write)
        stdout_write = None
        _close(native, stderr_write)
        stderr_write = None
        if not native.kernel32.AssignProcessToJobObject(job, process):
            _winerror("AssignProcessToJobObject failed")
        assigned = True
        if native.kernel32.ResumeThread(thread_handle) == _INFINITE:
            _winerror("ResumeThread failed")
        _close(native, thread_handle)
        thread_handle = None

        for name, handle in (("stdout", stdout_read), ("stderr", stderr_read)):
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            if name == "stdout":
                stdout_read = None
            else:
                stderr_read = None
            reader = threading.Thread(
                target=read_stream,
                args=(name, descriptor),
                name=f"pony-{name}-reader",
                daemon=True,
            )
            reader.start()
            reader_threads.append(reader)

        deadline = time.monotonic() + max(0.0, float(timeout))
        timed_out = False
        terminated = False
        while True:
            process_done = _wait(native, process, 20)
            if overflow.is_set() or reader_errors:
                _terminate(native, job, process, assigned=assigned)
                terminated = True
                break
            if process_done and all(not reader.is_alive() for reader in reader_threads):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate(native, job, process, assigned=assigned)
                terminated = True
                break
        if terminated and not _wait(native, process, 5000):
            raise RuntimeError("Windows process tree did not terminate")
        for reader in reader_threads:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in reader_threads):
            raise RuntimeError("Windows process capture did not close")
        if reader_errors:
            raise reader_errors[0]
        return CapturedWindowsProcess(
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            returncode=_exit_code(native, process),
            timed_out=timed_out,
            output_limit_exceeded=overflow.is_set(),
        )
    except BaseException as exc:
        primary = exc
        if process:
            try:
                _terminate(native, job, process, assigned=assigned)
                _wait(native, process, 5000)
            except Exception:
                pass
        raise
    finally:
        if attributes:
            native.kernel32.DeleteProcThreadAttributeList(attributes)
        _close(native, thread_handle)
        _close(native, stdin_handle)
        _close(native, stdout_write)
        _close(native, stderr_write)
        _close(native, stdout_read)
        _close(native, stderr_read)
        _close(native, process)
        _close(native, job)
        if primary is not None:
            for reader in reader_threads:
                reader.join(timeout=1)
