"""Bounded workspace snapshot helpers for side-effect detection."""

import os
from pathlib import Path

from pico import security as securitylib
from pico.recovery.paths import hash_file_bytes
from pico.workspace import IGNORED_PATH_NAMES

DEFAULT_MAX_SNAPSHOT_FILES = 5000
DEFAULT_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def capture_workspace_snapshot(
    root,
    max_files=DEFAULT_MAX_SNAPSHOT_FILES,
    max_total_bytes=DEFAULT_MAX_SNAPSHOT_BYTES,
):
    root = Path(root).resolve()
    snapshot = {}
    scanned_files = 0
    scanned_bytes = 0
    for path in _iter_snapshot_files(root):
        if scanned_files >= max_files:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if scanned_bytes + size > max_total_bytes:
            break
        try:
            info = hash_file_bytes(path)
        except OSError:
            continue
        snapshot[path.relative_to(root).as_posix()] = info["content_hash"]
        scanned_files += 1
        scanned_bytes += size
    return snapshot


def _iter_snapshot_files(root):
    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        safe_dirnames = []
        for name in sorted(dirnames):
            candidate = current / name
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if (
                name in IGNORED_PATH_NAMES
                or securitylib.is_sensitive_path(relative)
            ):
                continue
            safe_dirnames.append(name)
        dirnames[:] = safe_dirnames
        for filename in sorted(filenames):
            path = Path(current_root) / filename
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            relative_parts = relative.parts
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if securitylib.is_sensitive_path(relative.as_posix()):
                continue
            try:
                yield securitylib.require_regular_no_symlink(path)
            except (FileNotFoundError, OSError, ValueError):
                continue


def diff_workspace_snapshots(before, after):
    changed_paths = []
    summaries = []
    all_paths = sorted(set(before) | set(after))
    for path in all_paths:
        if before.get(path) == after.get(path):
            continue
        changed_paths.append(path)
        if path not in before:
            summaries.append(f"created:{path}")
        elif path not in after:
            summaries.append(f"deleted:{path}")
        else:
            summaries.append(f"modified:{path}")
    return changed_paths, summaries
