"""Native Windows implementation of Pony's cross-process file lock."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import threading
import time

from pony.security.private_files import ensure_private_dir
from pony.security import windows_native as native


_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_LOCK_VIOLATION = 33
_LOCK_STATE = threading.local()


class _Overlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


def _active_lock_keys():
    pid = os.getpid()
    active = getattr(_LOCK_STATE, "active", None)
    if active is None or getattr(_LOCK_STATE, "active_pid", None) != pid:
        active = set()
        _LOCK_STATE.active = active
        _LOCK_STATE.active_pid = pid
    return active


def _configure_lock_api():
    kernel32 = native.api().kernel32
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
    return kernel32


def _acquire(handle, deadline):
    kernel32 = _configure_lock_api()
    overlapped = _Overlapped()
    if deadline is None:
        if not kernel32.LockFileEx(
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        ):
            raise OSError(ctypes.get_last_error(), "LockFileEx failed")
        return overlapped
    while True:
        if kernel32.LockFileEx(
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        ):
            return overlapped
        error = ctypes.get_last_error()
        if error != _ERROR_LOCK_VIOLATION:
            raise OSError(error, "LockFileEx failed")
        if time.monotonic() >= deadline:
            raise TimeoutError("lock acquisition timed out")
        time.sleep(0.01)


def _release(handle, overlapped):
    kernel32 = _configure_lock_api()
    if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        raise OSError(ctypes.get_last_error(), "UnlockFileEx failed")


def _open_lock(path, parent, *, require_existing):
    descriptor = None
    if not require_existing:
        descriptor = native.private_security_descriptor()
        security_descriptor = descriptor.__enter__()
    else:
        security_descriptor = None
    try:
        handle, created = native.open_relative(
            parent,
            path.name,
            directory=False,
            desired_access=native.FILE_WRITE_ACCESS,
            disposition=native.FILE_OPEN if require_existing else native.FILE_OPEN_IF,
            security_descriptor=security_descriptor,
            share_access=native.FILE_SHARE_READ_WRITE,
        )
    finally:
        if descriptor is not None:
            descriptor.__exit__(None, None, None)
    try:
        if require_existing:
            native.require_private(handle)
        elif not created:
            native.require_current_owner(handle)
            native.make_private(handle)
        native.require_private(handle)
        return handle
    except Exception:
        handle.close()
        raise


def _require_current(path, parent, handle):
    _, current_parent = native.open_parent(
        path,
        share_access=native.FILE_SHARE_READ_WRITE,
    )
    try:
        if native.identity(current_parent) != native.identity(parent):
            raise ValueError("lock path changed")
        current, _created = native.open_relative(
            current_parent,
            path.name,
            directory=False,
            desired_access=native.FILE_READ_ACCESS,
            share_access=native.FILE_SHARE_READ_WRITE,
        )
        try:
            if native.identity(current) != native.identity(handle):
                raise ValueError("lock path changed")
            native.require_private(current)
        finally:
            current.close()
    finally:
        current_parent.close()


@contextmanager
def locked_file(path, *, require_lock=False, require_existing=False, lock_timeout=None):
    del require_lock  # LockFileEx is mandatory on Windows.
    path = native.lexical_absolute(path)
    key = os.path.normcase(str(path))
    active = _active_lock_keys()
    if key in active:
        raise RuntimeError("lock reentry")
    deadline = (
        None
        if lock_timeout is None
        else time.monotonic() + max(0.0, float(lock_timeout))
    )
    if not require_existing:
        ensure_private_dir(path.parent)
    path, parent = native.open_parent(
        path,
        share_access=native.FILE_SHARE_READ_WRITE,
    )
    handle = None
    stream = None
    raw_handle = None
    registered = False
    locked = False
    overlapped = None
    primary = None
    try:
        native.require_private(parent)
        handle = _open_lock(path, parent, require_existing=require_existing)
        raw_handle = handle.value
        active.add(key)
        registered = True
        overlapped = _acquire(raw_handle, deadline)
        locked = True
        _require_current(path, parent, handle)
        import msvcrt

        descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDWR | os.O_APPEND)
        handle.value = None
        try:
            stream = os.fdopen(descriptor, "a+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        yield stream
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_error = None
        if locked:
            try:
                _release(raw_handle, overlapped)
            except BaseException as exc:
                cleanup_error = exc
        if stream is not None:
            try:
                stream.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if handle is not None:
            handle.close()
        parent.close()
        if registered:
            active.discard(key)
        if primary is None and cleanup_error is not None:
            raise cleanup_error
