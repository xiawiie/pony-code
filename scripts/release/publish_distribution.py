#!/usr/bin/env python3
"""Atomically publish the verified release directory without replacement."""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import stat
import sys


AT_FDCWD = -100
RENAME_EXCL = 0x00000004
RENAME_NOREPLACE = 0x00000001
DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class PublishError(RuntimeError):
    """The verified archives could not be published safely."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        arguments = (os.fsencode(source), os.fsencode(destination), RENAME_EXCL)
        argument_types = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        arguments = (
            AT_FDCWD,
            os.fsencode(source),
            AT_FDCWD,
            os.fsencode(destination),
            RENAME_NOREPLACE,
        )
        argument_types = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
    else:
        raise PublishError(f"atomic no-replace rename unsupported on {sys.platform}")
    if rename is None:
        raise PublishError("atomic no-replace rename unavailable")
    rename.argtypes = argument_types
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _validate_archives(source_fd: int) -> None:
    entries = sorted(os.listdir(source_fd))
    if (
        len(entries) != 2
        or len([name for name in entries if name.endswith(".whl")]) != 1
        or len([name for name in entries if name.endswith(".tar.gz")]) != 1
    ):
        raise PublishError(f"unexpected release artifacts: {entries}")
    for name in entries:
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"release artifact is not a regular file: {name}")


def publish_distribution(source: Path, *, root: Path | None = None) -> None:
    root = Path(os.path.abspath(Path.cwd() if root is None else root))
    source = Path(os.path.abspath(source))
    staging = source.parent
    if (
        source.name != "dist"
        or staging.parent != root
        or not staging.name.startswith(".pony-check.")
    ):
        raise PublishError("release source must be .pony-check.*/dist under the repository")
    root_info = os.stat(root, follow_symlinks=False)
    staging_info = os.stat(staging, follow_symlinks=False)
    if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISDIR(staging_info.st_mode):
        raise PublishError("release root or staging is not a directory")

    source_fd = os.open(source, DIRECTORY_FLAGS)
    try:
        source_identity = _identity(os.fstat(source_fd))
        _validate_archives(source_fd)
        current = os.stat(source, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != source_identity:
            raise PublishError("release source changed before publish")
        destination = root / "dist"
        _rename_noreplace(source, destination)
        published = os.stat(destination, follow_symlinks=False)
        if not stat.S_ISDIR(published.st_mode) or _identity(published) != source_identity:
            raise PublishError("release destination identity mismatch")
    finally:
        os.close(source_fd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dist", type=Path)
    args = parser.parse_args()
    try:
        publish_distribution(args.source_dist)
    except (OSError, PublishError) as exc:
        parser.exit(1, f"release dist publish failed: {exc}\n")


if __name__ == "__main__":
    main()
