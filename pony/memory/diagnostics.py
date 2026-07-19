"""Small, read-only health check for durable Memory files.

Memory semantics belong to ``BlockStore`` and ``Retrieval``.  Doctor only
needs to answer a narrower question: can bounded, no-follow reads inspect the
configured note files without exposing their contents?
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

from pony.memory import block_store
from pony.memory.block_store import _read_bounded_regular
from pony.security import private_files
from pony.security.paths import _lexical_absolute


def _issue(path, reason_code, count=1, limit=0):
    return {
        "path": str(path),
        "count": int(count),
        "reason_code": str(reason_code),
        "limit": int(limit),
    }


def _candidate_files(scope, root, issues, state):
    """Yield canonical Markdown paths without following unsafe entries."""
    root = _lexical_absolute(root)
    try:
        root_descriptor = private_files._open_private_directory(root)
    except FileNotFoundError:
        return
    except (OSError, RuntimeError, ValueError):
        issues.append(_issue(scope, "memory_root_unavailable"))
        return
    try:
        try:
            notes = os.stat("notes", dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            stack = []
        except OSError:
            issues.append(_issue(f"{scope}/notes", "memory_directory_unavailable"))
            stack = []
        else:
            if stat.S_ISDIR(notes.st_mode) and not stat.S_ISLNK(notes.st_mode):
                stack = [Path("notes")]
            else:
                issues.append(_issue(f"{scope}/notes", "memory_directory_unavailable"))
                stack = []

        while stack:
            relative_dir = stack.pop()
            try:
                descriptor = private_files._open_private_directory(root / relative_dir)
            except (OSError, RuntimeError, ValueError):
                issues.append(
                    _issue(f"{scope}/{relative_dir.as_posix()}", "memory_directory_unavailable")
                )
                continue
            try:
                with os.scandir(descriptor) as entries:
                    remaining = block_store.MAX_MEMORY_INDEX_FILES - state["entries"]
                    bounded = []
                    for entry in entries:
                        bounded.append(entry)
                        if len(bounded) > remaining:
                            break
                for entry in sorted(bounded, key=lambda item: item.name):
                    relative = relative_dir / entry.name
                    state["entries"] += 1
                    if state["entries"] > block_store.MAX_MEMORY_INDEX_FILES:
                        issues.append(
                            _issue(
                                f"{scope}/{relative}",
                                "memory_index_limit_reached",
                                state["entries"],
                                block_store.MAX_MEMORY_INDEX_FILES,
                            )
                        )
                        return
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        issues.append(
                            _issue(f"{scope}/{relative}", "memory_directory_unavailable")
                        )
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        reason = (
                            "memory_directory_unavailable"
                            if stat.S_ISDIR(metadata.st_mode)
                            else "memory_file_unavailable"
                        )
                        issues.append(_issue(f"{scope}/{relative}", reason))
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append(relative)
                    elif entry.name.endswith(".md"):
                        yield f"{scope}/{relative}", root / relative
            except OSError:
                issues.append(
                    _issue(f"{scope}/{relative_dir.as_posix()}", "memory_directory_unavailable")
                )
            finally:
                os.close(descriptor)

        try:
            os.stat("agent_notes.md", dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            issues.append(_issue(f"{scope}/agent_notes.md", "memory_file_unavailable"))
        else:
            state["entries"] += 1
            if state["entries"] > block_store.MAX_MEMORY_INDEX_FILES:
                issues.append(
                    _issue(
                        f"{scope}/agent_notes.md",
                        "memory_index_limit_reached",
                        state["entries"],
                        block_store.MAX_MEMORY_INDEX_FILES,
                    )
                )
                return
            yield f"{scope}/agent_notes.md", root / "agent_notes.md"
    finally:
        os.close(root_descriptor)


def _scan_scope(scope, root, issues, state):
    for canonical, path in _candidate_files(scope, root, issues, state) or ():
        remaining = block_store.MAX_MEMORY_INDEX_BYTES - state["bytes"]
        if remaining <= 0:
            issues.append(
                _issue(
                    canonical,
                    "memory_total_bytes_limit_reached",
                    state["bytes"],
                    block_store.MAX_MEMORY_INDEX_BYTES,
                )
            )
            return
        limit = min(block_store.MAX_MEMORY_FILE_BYTES, remaining)
        try:
            data, _ = _read_bounded_regular(path, limit, private=False)
        except (OSError, RuntimeError, ValueError) as exc:
            state["bytes"] += int(getattr(exc, "bytes_read", 0))
            issues.append(_issue(canonical, "memory_file_unavailable"))
            continue
        state["bytes"] += len(data)


def collect_memory_diagnostics(workspace_root, *, user_memory_root=None):
    """Return bounded diagnostic metadata without creating or modifying state.

    This deliberately does not validate note frontmatter or repository policy.
    Those are user-authored content concerns, not a prerequisite for safely
    reading Memory.  Secret/path boundaries remain enforced by the bounded
    no-follow reader and the workspace index helpers.
    """
    workspace_root = _lexical_absolute(workspace_root) / ".pony" / "memory"
    if user_memory_root is None:
        try:
            user_memory_root = Path.home() / ".pony" / "memory"
        except (OSError, RuntimeError):
            user_memory_root = None

    issues = []
    state = {"entries": 0, "bytes": 0}
    roots = [("workspace", workspace_root)]
    if user_memory_root is not None:
        roots.append(("user", _lexical_absolute(user_memory_root)))
    for scope, root in roots:
        _scan_scope(scope, root, issues, state)

    issues.sort(key=lambda item: (item["path"], item["reason_code"]))
    if issues:
        status = "unknown"
        reason_code = "memory_diagnostics_incomplete"
        remediation = "resolve Memory filesystem access and rerun pony doctor"
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
