"""Small cross-process file lock helper."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat

from .security import ensure_private_dir

try:  # pragma: no cover - fcntl is unavailable on some platforms.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@contextmanager
def locked_file(path, *, require_lock=False):
    path = Path(path)
    ensure_private_dir(path.parent)
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        kind = "symlink" if stat.S_ISLNK(before.st_mode) else "regular file required"
        raise ValueError(kind)
    if require_lock and fcntl is None:
        raise RuntimeError("cross-process lock unavailable")

    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("regular file required")
        current = os.stat(path, follow_symlinks=False)
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
        if descriptor >= 0:
            os.close(descriptor)
