"""Capture workspace state before and after host tool execution."""

import os
import stat
import subprocess
from pathlib import Path
from types import MappingProxyType

from pony.security import paths as security_paths
from pony.security import private_files, workspace_files
from pony.tools.subprocess import run_hardened_git
from pony.workspace.context import _safe_index_path

IGNORED_OBSERVER_NAMES = {".git", ".pony", "__pycache__", ".venv", "node_modules"}
MAX_OBSERVER_ENTRIES = 10_000
MAX_OBSERVER_DEPTH = 32


def _entry_marker(entry):
    return ":".join(
        str(item)
        for item in (
            *entry["identity"],
            entry["size"],
            entry["modified_ns"],
            entry["changed_ns"],
        )
    )


class WorkspaceObserver:
    def __init__(self, root, *, executables=None):
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.trusted_executables = MappingProxyType(dict(executables or {}))
        try:
            self.root_identity = private_files.private_directory_identity(self.root)
        except (OSError, ValueError):
            self.root_identity = None

    def _require_current_root(self):
        if self.root_identity is None:
            return False
        try:
            current = private_files.private_directory_identity(self.root)
        except (OSError, ValueError) as exc:
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace root changed",
            ) from exc
        if tuple(current) != tuple(self.root_identity):
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace root changed",
            )
        return True

    def _is_git_repo(self):
        git_executable = self.trusted_executables.get("git")
        if not git_executable:
            return False
        try:
            result = run_hardened_git(
                git_executable,
                ["rev-parse", "--is-inside-work-tree"],
                cwd=self.root,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _list_directory(self, relative, *, max_entries=MAX_OBSERVER_ENTRIES):
        if self.root_identity is None:
            return None
        return workspace_files.list_directory_names_anchored(
            self.root,
            relative,
            max_entries=max_entries,
            expected_root_identity=self.root_identity,
        )

    def _file_marker(self, path):
        candidate = _safe_index_path(self.root, self.root / path)
        if candidate is None:
            return None
        relative = candidate.relative_to(self.root)
        parent = relative.parent.as_posix()
        listing = self._list_directory(parent)
        if listing is None:
            return None
        for entry in listing["entries"]:
            if entry["name"] == relative.name and stat.S_ISREG(entry["mode"]):
                return _entry_marker(entry)
        return None

    def _capture_git(self):
        git_executable = self.trusted_executables.get("git")
        if not git_executable:
            return self._capture_filesystem()
        try:
            proc = run_hardened_git(
                git_executable,
                ["status", "--porcelain=v1", "-z", "-uall"],
                cwd=self.root,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return self._capture_filesystem()
        if proc.returncode != 0:
            return self._capture_filesystem()
        raw = proc.stdout or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        paths = {}
        entries = raw.split(b"\x00")
        i = 0
        while i < len(entries):
            entry = entries[i]
            if not entry:
                i += 1
                continue
            status = entry[:2].decode("ascii", errors="replace")
            path = entry[3:].decode("utf-8", errors="replace")
            if status.startswith("R"):
                original = (
                    entries[i + 1].decode("utf-8", errors="replace")
                    if i + 1 < len(entries)
                    else ""
                )
                original_path = _safe_index_path(self.root, self.root / original)
                renamed_path = _safe_index_path(self.root, self.root / path)
                if original_path is not None:
                    paths[original_path.relative_to(self.root).as_posix()] = "R:removed"
                if renamed_path is not None:
                    paths[renamed_path.relative_to(self.root).as_posix()] = "R:added"
                i += 2
                continue
            candidate = _safe_index_path(self.root, self.root / path)
            if candidate is not None:
                paths[candidate.relative_to(self.root).as_posix()] = status.strip() or "?"
            i += 1
        detail = {}
        for path in paths:
            marker = self._file_marker(path)
            if marker is not None:
                detail[path] = marker
        return {"mode": "git", "paths": paths, "detail": detail, "summaries": []}

    def _capture_filesystem(self):
        paths = {}
        if self.root_identity is None:
            return {"mode": "filesystem", "paths": paths, "detail": paths, "summaries": []}
        stack = [(".", 0)]
        scanned = 0
        while stack:
            directory, depth = stack.pop()
            listing = self._list_directory(
                directory,
                max_entries=MAX_OBSERVER_ENTRIES - scanned,
            )
            scanned += listing["scanned"]
            children = []
            for entry in listing["entries"]:
                relative = (
                    entry["name"]
                    if directory == "."
                    else f"{directory}/{entry['name']}"
                )
                if entry["name"] in IGNORED_OBSERVER_NAMES or security_paths.is_sensitive_path(relative):
                    continue
                if stat.S_ISDIR(entry["mode"]):
                    if depth >= MAX_OBSERVER_DEPTH:
                        raise workspace_files.WorkspaceIOError(
                            "workspace_observer_limit_exceeded",
                            "workspace observer depth limit exceeded",
                        )
                    children.append(relative)
                elif stat.S_ISREG(entry["mode"]):
                    paths[relative] = _entry_marker(entry)
            for child in reversed(children):
                stack.append((child, depth + 1))
            if scanned >= MAX_OBSERVER_ENTRIES and stack:
                raise workspace_files.WorkspaceIOError(
                    "workspace_observer_limit_exceeded",
                    "workspace observer entry limit exceeded",
                )
        return {"mode": "filesystem", "paths": paths, "detail": paths, "summaries": []}

    def capture(self):
        if not self._require_current_root():
            return self._capture_filesystem()
        if self._is_git_repo():
            return self._capture_git()
        return self._capture_filesystem()

    def capture_call_start(self):
        return self.capture()

    def capture_call_end(self):
        return self.capture()

    def invalidate_call_cache(self):
        return None

    def diff(self, before, after):
        """Compare two captures and report paths that actually changed."""
        before_paths = dict(before.get("paths", {}))
        after_paths = dict(after.get("paths", {}))
        before_detail = dict(before.get("detail", {}))
        after_detail = dict(after.get("detail", {}))
        mode = after.get("mode") or before.get("mode") or "filesystem"
        candidates = (
            set(before_paths)
            | set(after_paths)
            | set(before_detail)
            | set(after_detail)
        )
        changed = []
        summaries = []
        for path in sorted(candidates):
            before_marker = before_detail.get(path) or before_paths.get(path)
            after_marker = after_detail.get(path) or after_paths.get(path)
            if before_marker == after_marker:
                continue
            after_exists = self._file_marker(path) is not None
            before_had_marker = before_marker is not None
            after_had_marker = after_marker is not None
            if not before_had_marker and after_had_marker and after_exists:
                summaries.append(f"created:{path}")
            elif before_had_marker and not after_exists:
                summaries.append(f"deleted:{path}")
            elif before_had_marker and after_had_marker:
                summaries.append(f"modified:{path}")
            else:
                summaries.append(f"deleted:{path}")
            changed.append(path)
        return {"mode": mode, "changed_paths": changed, "summaries": summaries}
