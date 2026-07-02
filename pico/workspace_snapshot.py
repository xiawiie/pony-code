"""Bounded workspace snapshot helpers for side-effect detection."""

import hashlib
import os
from pathlib import Path

from .workspace import IGNORED_PATH_NAMES

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
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        scanned_files += 1
        scanned_bytes += size
    return snapshot


def _iter_snapshot_files(root):
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_PATH_NAMES)
        for filename in sorted(filenames):
            path = Path(current_root) / filename
            try:
                relative_parts = path.relative_to(root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if path.is_file():
                yield path


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
