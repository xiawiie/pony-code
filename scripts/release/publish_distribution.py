#!/usr/bin/env python3
"""Publish the two verified release archives without replacing an existing dist."""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
import stat


DIST_NAME = "dist"
ARCHIVE_PATTERNS = ("*.whl", "*.tar.gz")
DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class PublishError(RuntimeError):
    """The verified archives could not be published safely."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _archive_identities(source_fd: int) -> dict[str, tuple[int, int]]:
    entries = sorted(os.listdir(source_fd))
    names = []
    for pattern in ARCHIVE_PATTERNS:
        matches = [name for name in entries if fnmatch.fnmatchcase(name, pattern)]
        if len(matches) != 1:
            raise PublishError(f"expected one {pattern}, found {matches}")
        names.extend(matches)
    if sorted(names) != entries:
        raise PublishError(f"unexpected release artifacts: {entries}")

    identities = {}
    for name in names:
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"release artifact is not a regular file: {name}")
        identities[name] = _identity(info)
    return identities


def _path_has_identity(root_fd: int, expected: tuple[int, int]) -> bool:
    try:
        info = os.stat(DIST_NAME, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and _identity(info) == expected


def _cleanup_owned_publish(
    root_fd: int,
    destination_fd: int,
    destination_identity: tuple[int, int],
    published: dict[str, tuple[int, int]],
) -> bool:
    if not _path_has_identity(root_fd, destination_identity):
        return False
    for name, expected in published.items():
        try:
            current = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
            if _identity(current) == expected:
                os.unlink(name, dir_fd=destination_fd)
        except OSError:
            pass
    try:
        os.fsync(destination_fd)
    except OSError:
        pass
    # There is no portable rmdir-by-fd. Retain the directory instead of racing
    # a path replacement between an identity check and path-based rmdir.
    return _path_has_identity(root_fd, destination_identity)


def publish_distribution(source: Path, *, root: Path | None = None) -> None:
    root = Path.cwd() if root is None else root
    source_fd = os.open(source, DIRECTORY_FLAGS)
    root_fd = None
    destination_fd = None
    destination_identity = None
    published: dict[str, tuple[int, int]] = {}
    try:
        root_fd = os.open(root, DIRECTORY_FLAGS)
        archives = _archive_identities(source_fd)
        try:
            os.mkdir(DIST_NAME, dir_fd=root_fd)
        except FileExistsError as exc:
            raise PublishError("release dist already exists") from exc

        destination_fd = os.open(DIST_NAME, DIRECTORY_FLAGS, dir_fd=root_fd)
        destination_info = os.fstat(destination_fd)
        destination_identity = _identity(destination_info)
        if os.listdir(destination_fd):
            raise PublishError("release dist changed before publish")
        if os.fstat(source_fd).st_dev != destination_info.st_dev:
            raise PublishError("release staging and dist are on different filesystems")

        for name, expected in archives.items():
            os.link(
                name,
                name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            published[name] = expected
            artifact_fd = os.open(name, FILE_FLAGS, dir_fd=destination_fd)
            try:
                if _identity(os.fstat(artifact_fd)) != expected:
                    raise PublishError(f"release artifact changed during publish: {name}")
                os.fsync(artifact_fd)
            finally:
                os.close(artifact_fd)

        os.fsync(destination_fd)
        os.fsync(root_fd)
        if sorted(os.listdir(destination_fd)) != sorted(archives):
            raise PublishError("release dist changed during publish")
        if not _path_has_identity(root_fd, destination_identity):
            raise PublishError("release dist path changed during publish")
    except BaseException as exc:
        retained = False
        if destination_fd is not None and destination_identity is not None:
            retained = _cleanup_owned_publish(
                root_fd,
                destination_fd,
                destination_identity,
                published,
            )
        if retained and isinstance(exc, Exception):
            raise PublishError(
                f"{exc}; release dist retained for safe manual cleanup"
            ) from exc
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if root_fd is not None:
            os.close(root_fd)
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
