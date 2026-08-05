"""Small, read-only health check for durable Memory files.

Memory semantics belong to ``BlockStore`` and ``Retrieval``. Doctor only
answers a narrower question: can bounded, no-follow reads inspect configured
note files without exposing their contents?
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from itertools import islice

from pony.security import private_files, workspace_files
from pony.security.paths import _lexical_absolute

from . import block_store


def _issue(path, reason_code, count=1, limit=0):
    return {
        "path": str(path),
        "count": int(count),
        "reason_code": str(reason_code),
        "limit": int(limit),
    }


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _directory_matches(parent_descriptor, name, expected):
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and _identity(current) == _identity(expected)


def _open_child_directory(parent_descriptor, name, expected):
    descriptor = os.open(
        name,
        private_files._private_directory_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _identity(expected) != _identity(opened)
            or _identity(opened) != _identity(current)
        ):
            raise ValueError("memory directory changed")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_bounded_at(parent_descriptor, name, expected, limit):
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("bounded no-follow reads unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | nofollow
    )
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(expected) != _identity(opened)
            or _identity(opened) != _identity(current)
        ):
            raise ValueError("unsafe memory file")
        if opened.st_size > limit:
            error = ValueError("memory file too large")
            error.bytes_read = limit + 1
            raise error
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
        if len(data) > limit:
            error = ValueError("memory file too large")
            error.bytes_read = len(data)
            raise error
        final = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or _identity(opened) != _identity(final)
        ):
            raise ValueError("memory file changed")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _consume_entry(scope, relative, issues, state):
    state["entries"] += 1
    if state["entries"] <= block_store.MAX_MEMORY_INDEX_FILES:
        return True
    issues.append(
        _issue(
            f"{scope}/{relative}",
            "memory_index_limit_reached",
            state["entries"],
            block_store.MAX_MEMORY_INDEX_FILES,
        )
    )
    return False


def _read_candidate(scope, relative, descriptor, name, metadata, issues, state):
    remaining = block_store.MAX_MEMORY_INDEX_BYTES - state["bytes"]
    if remaining <= 0:
        issues.append(
            _issue(
                f"{scope}/{relative}",
                "memory_total_bytes_limit_reached",
                state["bytes"],
                block_store.MAX_MEMORY_INDEX_BYTES,
            )
        )
        return False
    try:
        data = _read_bounded_at(
            descriptor,
            name,
            metadata,
            min(block_store.MAX_MEMORY_FILE_BYTES, remaining),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        state["bytes"] += int(getattr(exc, "bytes_read", 0))
        issues.append(_issue(f"{scope}/{relative}", "memory_file_unavailable"))
    else:
        state["bytes"] += len(data)
    return True


def _scan_directory(scope, relative_dir, descriptor, issues, state):
    try:
        with os.scandir(descriptor) as entries:
            remaining = block_store.MAX_MEMORY_INDEX_FILES - state["entries"]
            bounded = sorted(
                islice(entries, max(0, remaining) + 1),
                key=lambda item: item.name,
            )
    except OSError:
        issues.append(
            _issue(f"{scope}/{relative_dir}", "memory_directory_unavailable")
        )
        return True

    for entry in bounded:
        relative = relative_dir / entry.name
        if not _consume_entry(scope, relative, issues, state):
            return False
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            issues.append(_issue(f"{scope}/{relative}", "memory_file_unavailable"))
            continue
        if stat.S_ISLNK(metadata.st_mode):
            issues.append(_issue(f"{scope}/{relative}", "memory_file_unavailable"))
            continue
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child = _open_child_directory(descriptor, entry.name, metadata)
            except (OSError, RuntimeError, ValueError):
                issues.append(
                    _issue(f"{scope}/{relative}", "memory_directory_unavailable")
                )
                continue
            try:
                continue_scan = _scan_directory(
                    scope, relative, child, issues, state
                )
            finally:
                os.close(child)
            if not _directory_matches(descriptor, entry.name, metadata):
                issues.append(
                    _issue(f"{scope}/{relative}", "memory_directory_unavailable")
                )
            if not continue_scan:
                return False
        elif entry.name.endswith(".md") and not _read_candidate(
            scope,
            relative,
            descriptor,
            entry.name,
            metadata,
            issues,
            state,
        ):
            return False
    return True


def _scan_notes(scope, root_descriptor, notes_metadata, issues, state):
    try:
        notes_descriptor = _open_child_directory(
            root_descriptor, "notes", notes_metadata
        )
    except (OSError, RuntimeError, ValueError):
        issues.append(_issue(f"{scope}/notes", "memory_directory_unavailable"))
        return
    try:
        _scan_directory(
            scope,
            Path("notes"),
            notes_descriptor,
            issues,
            state,
        )
    finally:
        os.close(notes_descriptor)
        if not _directory_matches(root_descriptor, "notes", notes_metadata):
            issues.append(_issue(f"{scope}/notes", "memory_directory_unavailable"))


def _root_matches(root, expected_identity):
    try:
        return private_files.private_directory_identity(root) == expected_identity
    except (OSError, RuntimeError, ValueError):
        return False


def _scan_scope(scope, root, issues, state):
    if os.name == "nt":
        _scan_scope_windows(scope, root, issues, state)
        return
    root = _lexical_absolute(root)
    try:
        root_descriptor = private_files._open_private_directory(root)
    except FileNotFoundError:
        return
    except (OSError, RuntimeError, ValueError):
        issues.append(_issue(scope, "memory_root_unavailable"))
        return
    root_identity = _identity(os.fstat(root_descriptor))
    try:
        try:
            notes_metadata = os.stat(
                "notes", dir_fd=root_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        except OSError:
            issues.append(_issue(f"{scope}/notes", "memory_directory_unavailable"))
        else:
            if stat.S_ISDIR(notes_metadata.st_mode) and not stat.S_ISLNK(
                notes_metadata.st_mode
            ):
                _scan_notes(
                    scope,
                    root_descriptor,
                    notes_metadata,
                    issues,
                    state,
                )
            else:
                issues.append(
                    _issue(f"{scope}/notes", "memory_directory_unavailable")
                )

        try:
            agent_metadata = os.stat(
                "agent_notes.md", dir_fd=root_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        except OSError:
            issues.append(
                _issue(f"{scope}/agent_notes.md", "memory_file_unavailable")
            )
        else:
            relative = Path("agent_notes.md")
            if _consume_entry(scope, relative, issues, state):
                _read_candidate(
                    scope,
                    relative,
                    root_descriptor,
                    "agent_notes.md",
                    agent_metadata,
                    issues,
                    state,
                )
    finally:
        if not _root_matches(root, root_identity):
            issues.append(_issue(scope, "memory_root_changed"))
        os.close(root_descriptor)


def _windows_listing(root, relative, root_identity, expected_identity):
    listing = workspace_files.list_directory_names_anchored(
        root,
        relative,
        max_entries=block_store.MAX_MEMORY_INDEX_FILES + 1,
        expected_root_identity=root_identity,
    )
    if tuple(listing["identity"]) != tuple(expected_identity):
        raise ValueError("memory directory changed")
    return listing


def _windows_entry_matches(
    root, parent, parent_identity, name, expected, root_identity
):
    try:
        listing = _windows_listing(
            root,
            parent,
            root_identity,
            parent_identity,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return any(
        entry["name"] == name and tuple(entry["identity"]) == tuple(expected)
        for entry in listing["entries"]
    )


def _read_windows_candidate(
    scope, relative, root, root_identity, expected_identity, issues, state
):
    remaining = block_store.MAX_MEMORY_INDEX_BYTES - state["bytes"]
    if remaining <= 0:
        issues.append(
            _issue(
                f"{scope}/{relative}",
                "memory_total_bytes_limit_reached",
                state["bytes"],
                block_store.MAX_MEMORY_INDEX_BYTES,
            )
        )
        return False
    limit = min(block_store.MAX_MEMORY_FILE_BYTES, remaining)
    try:
        result = workspace_files.read_regular_bytes_anchored(
            root,
            relative,
            max_bytes=limit,
            expected_root_identity=root_identity,
        )
        if not result["exists"] or tuple(result["identity"]) != tuple(
            expected_identity
        ):
            raise ValueError("memory file changed")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        error_state = getattr(exc, "state", {})
        size = error_state.get("size") or getattr(exc, "bytes_read", 0)
        state["bytes"] += min(int(size), limit + 1)
        issues.append(_issue(f"{scope}/{relative}", "memory_file_unavailable"))
    else:
        state["bytes"] += len(result["data"])
    return True


def _scan_windows_directory(
    scope, root, relative_dir, expected_identity, root_identity, issues, state
):
    try:
        listing = _windows_listing(
            root, relative_dir, root_identity, expected_identity
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        issues.append(
            _issue(f"{scope}/{relative_dir}", "memory_directory_unavailable")
        )
        return True
    if listing["unsafe_count"]:
        state["entries"] += listing["unsafe_count"]
        issues.append(
            _issue(
                f"{scope}/{relative_dir}",
                "memory_file_unavailable",
                listing["unsafe_count"],
            )
        )
    for entry in listing["entries"]:
        relative = f"{relative_dir}/{entry['name']}"
        if not _consume_entry(scope, relative, issues, state):
            return False
        if stat.S_ISDIR(entry["mode"]):
            if not _scan_windows_directory(
                scope,
                root,
                relative,
                entry["identity"],
                root_identity,
                issues,
                state,
            ):
                return False
            if not _windows_entry_matches(
                root,
                relative_dir,
                expected_identity,
                entry["name"],
                entry["identity"],
                root_identity,
            ):
                issues.append(
                    _issue(f"{scope}/{relative}", "memory_directory_unavailable")
                )
        elif entry["name"].endswith(".md") and not _read_windows_candidate(
            scope,
            relative,
            root,
            root_identity,
            entry["identity"],
            issues,
            state,
        ):
            return False
    return True


def _scan_scope_windows(scope, root, issues, state):
    root = _lexical_absolute(root)
    try:
        root_identity = private_files.private_directory_identity(root)
        listing = _windows_listing(root, ".", root_identity, root_identity)
    except FileNotFoundError:
        return
    except (OSError, RuntimeError, TypeError, ValueError):
        issues.append(_issue(scope, "memory_root_unavailable"))
        return
    entries = {entry["name"]: entry for entry in listing["entries"]}
    notes = entries.get("notes")
    if notes is not None:
        if stat.S_ISDIR(notes["mode"]):
            _scan_windows_directory(
                scope,
                root,
                "notes",
                notes["identity"],
                root_identity,
                issues,
                state,
            )
            if not _windows_entry_matches(
                root,
                ".",
                root_identity,
                "notes",
                notes["identity"],
                root_identity,
            ):
                issues.append(
                    _issue(f"{scope}/notes", "memory_directory_unavailable")
                )
        else:
            issues.append(_issue(f"{scope}/notes", "memory_directory_unavailable"))
    agent = entries.get("agent_notes.md")
    if agent is not None and _consume_entry(
        scope, "agent_notes.md", issues, state
    ):
        _read_windows_candidate(
            scope,
            "agent_notes.md",
            root,
            root_identity,
            agent["identity"],
            issues,
            state,
        )
    if not _root_matches(root, root_identity):
        issues.append(_issue(scope, "memory_root_changed"))


def collect_memory_diagnostics(workspace_root, *, user_memory_root=None):
    """Return bounded diagnostic metadata without creating or modifying state.

    This deliberately does not validate note frontmatter or repository policy.
    Those are user-authored content concerns, not a prerequisite for safely
    reading Memory. Secret/path boundaries remain enforced by descriptor-bound
    no-follow reads.
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
