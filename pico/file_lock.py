"""Small cross-process file lock helper."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

from .security import _open_private_parent, ensure_private_dir

try:  # pragma: no cover - fcntl is unavailable on some platforms.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


_LOCK_STATE = threading.local()


def _active_lock_keys():
    active = getattr(_LOCK_STATE, "active", None)
    if active is None:
        active = set()
        _LOCK_STATE.active = active
    return active


def lock_is_active(path):
    key = os.path.abspath(os.fspath(path))
    return key in _active_lock_keys()


@contextmanager
def locked_file(path, *, require_lock=False):
    path = Path(os.path.abspath(os.fspath(path)))
    key = str(path)
    active = _active_lock_keys()
    if key in active:
        raise RuntimeError("lock reentry")
    ensure_private_dir(path.parent)
    path, parent_descriptor = _open_private_parent(path)
    descriptor = -1
    registered = False
    try:
        try:
            before = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            kind = (
                "symlink"
                if stat.S_ISLNK(before.st_mode)
                else "regular file required"
            )
            raise ValueError(kind)
        if before is not None and before.st_nlink != 1:
            raise ValueError("private file has multiple links")
        if require_lock and fcntl is None:
            raise RuntimeError("cross-process lock unavailable")

        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        active.add(key)
        registered = True
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("regular file required")
        if opened.st_nlink != 1:
            raise ValueError("private file has multiple links")
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("inode_changed")
        if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("inode_changed")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            descriptor = -1
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if registered:
            active.discard(key)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
