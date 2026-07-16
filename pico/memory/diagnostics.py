"""Read-only, bounded diagnostics for durable memory."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
import os
from pathlib import Path
import stat
import subprocess

from pico import security as securitylib
from pico.tools.subprocess import run_hardened_git

from . import block_store
from .block_store import _read_bounded_regular
from .frontmatter import FRONTMATTER_KEYS, parse_frontmatter


_GIT_IGNORE_SENTINEL = "workspace/notes/__pico_git_ignore_probe__.md"
_LIST_FRONTMATTER_KEYS = {"aliases", "supersedes", "tags"}


def _issue(path, reason_code, count, limit):
    return {
        "path": str(path),
        "count": int(count),
        "reason_code": str(reason_code),
        "limit": int(limit),
    }


def _scan_failure(path, issues, state):
    issues.append(_issue(path, "memory_scan_failed", 1, 0))
    state["incomplete"] = True


def _open_child_directory(parent_descriptor, name):
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
        raise ValueError("unsafe memory directory")
    descriptor = os.open(
        name,
        securitylib._private_directory_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("unsafe memory directory")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _consume_scan_entry(scope, issues, state):
    state["scan_entries"] += 1
    limit = block_store.MAX_MEMORY_INDEX_FILES
    if state["scan_entries"] <= limit:
        return True
    issues.append(
        _issue(
            f"{scope}/notes",
            "memory_scan_entry_limit_reached",
            state["scan_entries"],
            limit,
        )
    )
    state["stopped"] = True
    return False


def _append_memory_file(scope, relative, real_path, files, issues, state):
    limit = block_store.MAX_MEMORY_INDEX_FILES
    if state["file_count"] >= limit:
        issues.append(
            _issue(
                f"{scope}/{relative}",
                "memory_file_count_limit_reached",
                state["file_count"] + 1,
                limit,
            )
        )
        state["stopped"] = True
        return False
    state["file_count"] += 1
    files.append((f"{scope}/{relative}", real_path))
    return True


def _scan_notes(scope, root, notes_descriptor, issues, state):
    files = []
    stack = deque((("", notes_descriptor),))
    try:
        while stack and not state["stopped"]:
            relative_dir, descriptor = stack.popleft()
            if descriptor < 0:
                try:
                    descriptor = securitylib._open_private_directory(
                        root / "notes" / relative_dir
                    )
                except (OSError, RuntimeError, ValueError):
                    _scan_failure(f"{scope}/notes", issues, state)
                    continue
            try:
                with os.scandir(descriptor) as entries:
                    remaining_entries = (
                        block_store.MAX_MEMORY_INDEX_FILES - state["file_count"]
                        + block_store.MAX_MEMORY_INDEX_FILES - state["scan_entries"]
                        + 1
                    )
                    bounded_entries = []
                    for entry in entries:
                        bounded_entries.append(entry)
                        if len(bounded_entries) >= max(1, remaining_entries):
                            break
                    for entry in sorted(bounded_entries, key=lambda item: item.name):
                        relative = "/".join(
                            part for part in (relative_dir, entry.name) if part
                        )
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            _scan_failure(f"{scope}/notes", issues, state)
                            continue
                        if is_directory:
                            if not _consume_scan_entry(scope, issues, state):
                                break
                            stack.append((relative, -1))
                            continue
                        if entry.name.endswith(".md"):
                            if not _append_memory_file(
                                scope,
                                f"notes/{relative}",
                                root / "notes" / relative,
                                files,
                                issues,
                                state,
                            ):
                                break
                        elif not _consume_scan_entry(scope, issues, state):
                            break
            except (OSError, RuntimeError, ValueError):
                _scan_failure(f"{scope}/notes", issues, state)
            finally:
                os.close(descriptor)
    finally:
        for _, descriptor in stack:
            if descriptor >= 0:
                os.close(descriptor)
    return files


def _scope_files(scope, root, issues, state):
    try:
        root_descriptor = securitylib._open_private_directory(root)
    except FileNotFoundError:
        return []
    except (OSError, RuntimeError, ValueError):
        _scan_failure(scope, issues, state)
        return []

    files = []
    try:
        try:
            notes_descriptor = _open_child_directory(root_descriptor, "notes")
        except FileNotFoundError:
            notes_descriptor = -1
        except (OSError, RuntimeError, ValueError):
            notes_descriptor = -1
            _scan_failure(f"{scope}/notes", issues, state)
        if notes_descriptor >= 0:
            files.extend(
                _scan_notes(scope, root, notes_descriptor, issues, state)
            )
        if state["stopped"]:
            return files

        try:
            os.stat(
                "agent_notes.md",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError:
            _scan_failure(f"{scope}/agent_notes.md", issues, state)
        else:
            _append_memory_file(
                scope,
                "agent_notes.md",
                root / "agent_notes.md",
                files,
                issues,
                state,
            )
        return files
    finally:
        os.close(root_descriptor)


def _invalid_frontmatter(text):
    if not text.startswith("---\n"):
        return False
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        end = rest.rfind("\n---")
        if end == -1 or end + len("\n---") != len(rest):
            return True
    block = rest[:end]
    recognized = False
    for line in block.splitlines():
        if not line.strip():
            continue
        if ":" not in line or not line.partition(":")[0].strip():
            return True
        key, _, raw = line.partition(":")
        key = key.strip()
        if key not in FRONTMATTER_KEYS:
            continue
        recognized = True
        raw = raw.strip()
        if not raw:
            return True
        if key in _LIST_FRONTMATTER_KEYS and not (
            raw.startswith("[") and raw.endswith("]")
        ):
            return True
    return not recognized


def _ignored_workspace_notes(repo_root, git_executable, candidates):
    marker = Path(repo_root) / ".git"
    try:
        marker_mode = marker.lstat().st_mode
    except FileNotFoundError:
        return [], ""
    except OSError:
        return [], "memory_git_ignore_check_failed"
    if stat.S_ISLNK(marker_mode) or not (
        stat.S_ISDIR(marker_mode) or stat.S_ISREG(marker_mode)
    ):
        return [], "memory_git_ignore_check_failed"
    if not git_executable:
        return [], "memory_git_ignore_check_failed"

    relative_to_canonical = {
        f".pico/memory/{path.removeprefix('workspace/')}": path
        for path in (*candidates, _GIT_IGNORE_SENTINEL)
    }
    try:
        result = run_hardened_git(
            git_executable,
            [
                "check-ignore",
                "--no-index",
                "--",
                *relative_to_canonical,
            ],
            cwd=repo_root,
            text=False,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return [], "memory_git_ignore_check_failed"
    if result.returncode not in {0, 1} or not isinstance(result.stdout, (bytes, bytearray)):
        return [], "memory_git_ignore_check_failed"

    def decode_path(raw):
        try:
            if raw.startswith(b'"') and raw.endswith(b'"'):
                quoted = ast.literal_eval(raw.decode("latin-1"))
                return os.fsdecode(quoted.encode("latin-1"))
            return os.fsdecode(raw)
        except (SyntaxError, UnicodeError, ValueError):
            return ""

    ignored = set()
    for raw in bytes(result.stdout).splitlines():
        path = decode_path(raw)
        if not raw or path not in relative_to_canonical:
            return [], "memory_git_ignore_check_failed"
        ignored.add(relative_to_canonical[path])
    if (result.returncode == 0) != bool(ignored):
        return [], "memory_git_ignore_check_failed"
    return sorted(ignored), ""


def collect_memory_diagnostics(
    workspace_root,
    *,
    user_memory_root=None,
    git_executable=None,
):
    """Return bounded diagnostic metadata without creating or modifying memory state."""
    workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
    workspace_memory = workspace_root / ".pico" / "memory"
    if user_memory_root is None:
        try:
            user_memory_root = Path.home() / ".pico" / "memory"
        except (OSError, RuntimeError):
            user_memory_root = None

    issues = []
    documents = []
    git_candidates = []
    total_bytes = 0
    state = {
        "file_count": 0,
        "scan_entries": 0,
        "stopped": False,
        "incomplete": False,
    }
    roots = [("workspace", workspace_memory)]
    if user_memory_root is not None:
        roots.append(
            ("user", Path(os.path.abspath(os.fspath(user_memory_root))))
        )

    for scope, root in roots:
        try:
            scoped_files = _scope_files(scope, root, issues, state)
        except (OSError, RuntimeError, ValueError):
            _scan_failure(scope, issues, state)
            scoped_files = []
        for canonical, real_path in scoped_files:
            if canonical.startswith("workspace/notes/") and canonical.endswith(".md"):
                git_candidates.append(canonical)
            remaining = block_store.MAX_MEMORY_INDEX_BYTES - total_bytes
            if remaining <= 0:
                issues.append(
                    _issue(
                        canonical,
                        "memory_total_bytes_limit_reached",
                        total_bytes,
                        block_store.MAX_MEMORY_INDEX_BYTES,
                    )
                )
                state["stopped"] = True
                break
            read_limit = min(block_store.MAX_MEMORY_FILE_BYTES, remaining)
            try:
                data, _ = _read_bounded_regular(real_path, read_limit, private=False)
            except (OSError, RuntimeError, ValueError) as exc:
                used_bytes = int(getattr(exc, "bytes_read", 0))
                total_bytes += used_bytes
                if total_bytes >= block_store.MAX_MEMORY_INDEX_BYTES or (
                    str(exc) == "memory file too large"
                    and read_limit < block_store.MAX_MEMORY_FILE_BYTES
                ):
                    issues.append(
                        _issue(
                            canonical,
                            "memory_total_bytes_limit_reached",
                            total_bytes,
                            block_store.MAX_MEMORY_INDEX_BYTES,
                        )
                    )
                    state["stopped"] = True
                    break
                if str(exc) == "memory file too large":
                    issues.append(
                        _issue(
                            canonical,
                            "memory_file_size_limit_reached",
                            used_bytes,
                            block_store.MAX_MEMORY_FILE_BYTES,
                        )
                    )
                    if (
                        canonical.endswith("/agent_notes.md")
                        and used_bytes > block_store.AGENT_NOTES_SOFT_LIMIT_BYTES
                    ):
                        issues.append(
                            _issue(
                                canonical,
                                "agent_notes_soft_limit_exceeded",
                                used_bytes,
                                block_store.AGENT_NOTES_SOFT_LIMIT_BYTES,
                            )
                        )
                else:
                    issues.append(
                        _issue(canonical, "memory_file_read_failed", 1, 0)
                    )
                    state["incomplete"] = True
                continue

            total_bytes += len(data)
            content = data.decode("utf-8", errors="replace")
            if canonical.endswith("/agent_notes.md"):
                if len(data) > block_store.AGENT_NOTES_SOFT_LIMIT_BYTES:
                    issues.append(
                        _issue(
                            canonical,
                            "agent_notes_soft_limit_exceeded",
                            len(data),
                            block_store.AGENT_NOTES_SOFT_LIMIT_BYTES,
                        )
                    )
                continue
            metadata, _ = parse_frontmatter(content)
            documents.append((canonical, metadata))
            if _invalid_frontmatter(content):
                issues.append(_issue(canonical, "invalid_frontmatter", 1, 0))
        if state["stopped"]:
            break

    paths_by_name = defaultdict(list)
    for path, metadata in documents:
        name = metadata.get("name")
        if name:
            paths_by_name[str(name)].append(path)
    for paths in paths_by_name.values():
        if len(paths) > 1:
            issues.extend(
                _issue(path, "duplicate_frontmatter_name", len(paths), 1)
                for path in paths
            )
    known_names = set(paths_by_name)
    for path, metadata in documents:
        missing_count = sum(
            name not in known_names
            for name in metadata.get("supersedes", ())
        )
        if missing_count:
            issues.append(
                _issue(path, "missing_supersedes_target", missing_count, 0)
            )
    ignored_notes, git_error = _ignored_workspace_notes(
        workspace_root,
        git_executable,
        git_candidates,
    )
    issues.extend(
        _issue(path, "workspace_user_note_git_ignored", 1, 0)
        for path in ignored_notes
    )
    if git_error:
        issues.append(_issue(_GIT_IGNORE_SENTINEL, git_error, 1, 0))
        state["incomplete"] = True
    issues.sort(key=lambda item: (item["path"], item["reason_code"]))
    if state["incomplete"]:
        status = "unknown"
        reason_code = "memory_diagnostics_incomplete"
        remediation = "resolve Memory filesystem or Git access and rerun pico doctor"
    elif issues:
        status = "warn"
        reason_code = "memory_review_required"
        remediation = "review Memory note metadata and Git ignore rules"
    else:
        status = "pass"
        reason_code = "memory_diagnostics_passed"
        remediation = ""
    return {
        "check_id": "memory",
        "status": status,
        "reason_code": reason_code,
        "remediation": remediation,
        "issues": issues,
    }
