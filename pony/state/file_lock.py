"""Small cross-process file lock helper."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import threading
import time

from pony.security.private_files import (
    _open_private_directory,
    _open_private_parent,
    ensure_private_dir,
)

try:  # pragma: no cover - fcntl is unavailable on some platforms.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


_LOCK_STATE = threading.local()


def _authority_root():
    return Path("/tmp").resolve(strict=True) / f".pony-lock-authority-{os.geteuid()}"


def _active_lock_keys():
    pid = os.getpid()
    active = getattr(_LOCK_STATE, "active", None)
    if active is None or getattr(_LOCK_STATE, "active_pid", None) != pid:
        active = set()
        _LOCK_STATE.active = active
        _LOCK_STATE.active_pid = pid
    return active


def _authority_locks():
    pid = os.getpid()
    state = getattr(_LOCK_STATE, "authorities", None)
    if state is None or getattr(_LOCK_STATE, "authority_pid", None) != pid:
        if state is not None:
            for descriptor, _depth in state.values():
                os.close(descriptor)
        state = {}
        _LOCK_STATE.authorities = state
        _LOCK_STATE.authority_pid = pid
    return state


def _acquire_flock(descriptor, deadline):
    if deadline is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("lock acquisition timed out") from exc
            time.sleep(0.01)


def _require_current_authority(root, descriptor):
    opened = os.fstat(descriptor)
    current = root.lstat()
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        raise ValueError("unsafe lock authority")


def _open_authority():
    root = ensure_private_dir(_authority_root())
    descriptor = _open_private_directory(root)
    try:
        _require_current_authority(root, descriptor)
    except Exception:
        os.close(descriptor)
        raise
    opened = os.fstat(descriptor)
    return root, descriptor, (opened.st_dev, opened.st_ino)


def _acquire_authority(deadline):
    authorities = _authority_locks()
    root, descriptor, key = _open_authority()
    current = authorities.get(key)
    if current is not None:
        _require_current_authority(root, descriptor)
        os.close(descriptor)
        current[1] += 1
        return key
    try:
        _acquire_flock(descriptor, deadline)
        _require_current_authority(root, descriptor)
    except Exception:
        os.close(descriptor)
        raise
    authorities[key] = [descriptor, 1]
    return key


def _release_authority(key):
    authorities = _authority_locks()
    descriptor, depth = authorities[key]
    if depth > 1:
        authorities[key][1] = depth - 1
        return
    del authorities[key]
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def lock_is_active(path):
    key = os.path.abspath(os.fspath(path))
    return key in _active_lock_keys()


def _require_existing_lock_entry(value, *, directory=False):
    uid = os.geteuid() if hasattr(os, "geteuid") else value.st_uid
    expected_kind = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if (
        not expected_kind
        or value.st_uid != uid
        or (
            not directory
            and (value.st_nlink != 1 or stat.S_IMODE(value.st_mode) != 0o600)
        )
    ):
        raise ValueError("existing lock path is unsafe")


@contextmanager
def locked_file(path, *, require_lock=False, require_existing=False, lock_timeout=None):
    owner_pid = os.getpid()
    path = Path(os.path.abspath(os.fspath(path)))
    key = str(path)
    active = _active_lock_keys()
    if key in active:
        raise RuntimeError("lock reentry")
    if require_lock and fcntl is None:
        raise RuntimeError("cross-process lock unavailable")
    deadline = (
        None
        if lock_timeout is None
        else time.monotonic() + max(0.0, float(lock_timeout))
    )
    if not require_existing:
        ensure_private_dir(path.parent)
    path, parent_descriptor = _open_private_parent(path)
    authority = None
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
        if require_existing and before is None:
            raise FileNotFoundError("lock file missing")
        if require_existing:
            _require_existing_lock_entry(os.fstat(parent_descriptor), directory=True)
            _require_existing_lock_entry(before)
        if before is not None and not stat.S_ISREG(before.st_mode):
            kind = (
                "symlink" if stat.S_ISLNK(before.st_mode) else "regular file required"
            )
            raise ValueError(kind)
        if before is not None and before.st_nlink != 1:
            raise ValueError("private file has multiple links")
        authority = _acquire_authority(deadline) if fcntl is not None else None
        flags = os.O_RDWR | os.O_APPEND
        if not require_existing:
            flags |= os.O_CREAT
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
        if before is not None and (before.st_dev, before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ValueError("inode_changed")
        if require_existing:
            _require_existing_lock_entry(opened)
            _require_existing_lock_entry(current)
        else:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            descriptor = -1
            if fcntl is not None:
                _acquire_flock(handle.fileno(), deadline)
                current_parent_descriptor = -1
                try:
                    _, current_parent_descriptor = _open_private_parent(path)
                    locked = os.fstat(handle.fileno())
                    original_parent = os.fstat(parent_descriptor)
                    current_parent = os.fstat(current_parent_descriptor)
                    current = os.stat(
                        path.name,
                        dir_fd=current_parent_descriptor,
                        follow_symlinks=False,
                    )
                    if require_existing:
                        _require_existing_lock_entry(current_parent, directory=True)
                        _require_existing_lock_entry(locked)
                        _require_existing_lock_entry(current)
                    if (
                        (current_parent.st_dev, current_parent.st_ino)
                        != (original_parent.st_dev, original_parent.st_ino)
                        or not stat.S_ISREG(locked.st_mode)
                        or locked.st_nlink != 1
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_nlink != 1
                        or (current.st_dev, current.st_ino)
                        != (locked.st_dev, locked.st_ino)
                    ):
                        raise ValueError("inode_changed")
                except FileNotFoundError as exc:
                    raise ValueError("inode_changed") from exc
                finally:
                    if current_parent_descriptor >= 0:
                        os.close(current_parent_descriptor)
            try:
                yield handle
            finally:
                if fcntl is not None and os.getpid() == owner_pid:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if registered:
            active.discard(key)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
        if authority is not None:
            if os.getpid() == owner_pid:
                _release_authority(authority)
            else:
                _authority_locks()
