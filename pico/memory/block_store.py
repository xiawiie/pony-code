"""Pico memory block store.

职责:
- 读写 `.pico/memory/notes/*.md`（用户手写）和 `.pico/memory/agent_notes.md`（agent 追加）
- 原子写入（tempfile + rename）
- 提供单次扫描的私有 document snapshot 给列表与检索

路径命名:
    "workspace/notes/auth.md"     -> <workspace_root>/notes/auth.md
    "workspace/agent_notes.md"    -> <workspace_root>/agent_notes.md
    "user/notes/prefs.md"         -> <user_root>/notes/prefs.md
    "user/agent_notes.md"         -> <user_root>/agent_notes.md

路径安全:
    拒绝 `..`, 绝对路径, 结果符号链接出 workspace/user root。
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pico import security as securitylib
from pico.file_lock import locked_file
from pico.security import (
    ensure_private_dir,
    ensure_private_file,
    read_private_text,
    require_regular_no_symlink,
)
from pico.workspace import _safe_index_directory, _safe_index_file

from .frontmatter import parse_frontmatter

MAX_NOTE_CHARS = 500
AGENT_NOTES_SOFT_LIMIT_CHARS = 8000
MAX_MEMORY_INDEX_FILES = 512
MAX_MEMORY_FILE_BYTES = 128 * 1024
MAX_MEMORY_INDEX_BYTES = 2 * 1024 * 1024


def _is_agent_owned_path(rel_path):
    parts = str(rel_path).split("/", 1)
    sub_path = parts[1] if len(parts) == 2 else parts[0]
    return sub_path == "agent_notes.md"


def _read_bounded_regular(path, limit, *, private=False):
    path = Path(os.path.abspath(os.fspath(path)))
    descriptor = -1
    if private:
        _, descriptor = securitylib._open_private_file(path)
        os.fchmod(descriptor, 0o600)
    else:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise RuntimeError("bounded no-follow reads unavailable")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | nofollow
        )
        parent_descriptor = securitylib._open_private_directory(path.parent)
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        finally:
            os.close(parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        path_current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not private
            and (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (path_current.st_dev, path_current.st_ino)
        ):
            error = ValueError("unsafe automatic memory file")
            error.bytes_read = min(opened.st_size, limit + 1)
            raise error
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
        return data, opened
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class MemoryFile:
    path: str          # e.g. "workspace/notes/auth.md"
    size_chars: int
    mtime: float
    first_line: str
    # Task 17: parsed frontmatter metadata, empty dict when the file has none.
    frontmatter: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _MemoryDocument:
    path: str
    size_chars: int
    mtime: float
    first_line: str
    frontmatter: dict
    raw: str

    def metadata(self) -> MemoryFile:
        return MemoryFile(
            path=self.path,
            size_chars=self.size_chars,
            mtime=self.mtime,
            first_line=self.first_line,
            frontmatter=self.frontmatter,
        )


class BlockStore:
    def __init__(
        self,
        workspace_root: Path,
        user_root: Path,
        redaction_env=None,
        secret_env_names=(),
    ):
        self.workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
        self.user_root = Path(os.path.abspath(os.fspath(user_root)))
        for root in (self.workspace_root, self.user_root):
            try:
                root.lstat()
            except FileNotFoundError:
                continue
            ensure_private_dir(root)
            self._harden_agent_notes(root)
        self.redaction_env = MappingProxyType(
            dict(os.environ if redaction_env is None else redaction_env)
        )
        self.secret_env_names = tuple(secret_env_names or ())
        self._size_warned: set[str] = set()

    @staticmethod
    def _harden_agent_notes(root: Path) -> None:
        agent_notes = root / "agent_notes.md"
        try:
            notes_mode = agent_notes.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISREG(notes_mode):
                ensure_private_file(agent_notes)

    # ---- listing / reading -------------------------------------------------

    def list(self) -> list[MemoryFile]:
        return [document.metadata() for document in self._load_documents()]

    def _load_documents(self) -> list[_MemoryDocument]:
        documents: list[_MemoryDocument] = []
        file_count = 0
        total_bytes = 0
        for scope, root in (
            ("workspace", self.workspace_root),
            ("user", self.user_root),
        ):
            for rel_path, real_path in self._scope_files(scope, root):
                if file_count >= MAX_MEMORY_INDEX_FILES:
                    documents.sort(key=lambda document: document.path)
                    return documents
                file_count += 1
                remaining = MAX_MEMORY_INDEX_BYTES - total_bytes
                if remaining <= 0:
                    documents.sort(key=lambda document: document.path)
                    return documents
                limit = min(MAX_MEMORY_FILE_BYTES, remaining)
                try:
                    document, used_bytes = self._load_document(
                        root,
                        rel_path,
                        real_path,
                        limit,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    total_bytes += getattr(exc, "bytes_read", 0)
                    if total_bytes >= MAX_MEMORY_INDEX_BYTES:
                        documents.sort(key=lambda item: item.path)
                        return documents
                    if str(exc) == "memory file too large" and limit < MAX_MEMORY_FILE_BYTES:
                        documents.sort(key=lambda item: item.path)
                        return documents
                    continue
                documents.append(document)
                total_bytes += used_bytes
        documents.sort(key=lambda document: document.path)
        return documents

    @staticmethod
    def _markdown_files(root: Path, directory: Path):
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            safe_dirnames = []
            for name in sorted(dirnames):
                candidate = Path(dirpath) / name
                if _safe_index_directory(root, candidate) is None:
                    yield candidate
                else:
                    safe_dirnames.append(name)
            dirnames[:] = safe_dirnames
            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue
                yield Path(dirpath) / name

    def _scope_files(self, scope: str, root: Path):
        root = _safe_index_directory(root, root)
        if root is None:
            return
        # notes/*.md (nested allowed) — user-written, agent read-only
        notes_dir = _safe_index_directory(root, root / "notes")
        if notes_dir is not None:
            for md in self._markdown_files(root, notes_dir):
                rel = md.relative_to(root).as_posix()
                yield f"{scope}/{rel}", md
        # agent_notes.md — agent-owned, append-only
        agent_notes = root / "agent_notes.md"
        try:
            agent_notes.lstat()
        except OSError:
            pass
        else:
            yield f"{scope}/agent_notes.md", agent_notes

    @staticmethod
    def _load_document(root: Path, rel_path: str, real_path: Path, limit: int):
        candidate = real_path
        real_path = _safe_index_file(root, candidate)
        if real_path is None:
            error = ValueError("unsafe automatic memory file")
            try:
                error.bytes_read = min(candidate.lstat().st_size, limit + 1)
            except OSError:
                error.bytes_read = 0
            raise error
        agent_owned = _is_agent_owned_path(rel_path)
        data, stat_result = _read_bounded_regular(
            real_path,
            limit,
            private=agent_owned,
        )
        content = data.decode("utf-8", errors="replace")
        # Task 17: parse frontmatter so retrieval / recall can boost by field.
        # When a file has a `description` header, prefer that as the display
        # first-line (memory_index shows it); otherwise fall back to the body's
        # first line.
        meta, body = parse_frontmatter(content)
        if meta.get("description"):
            first_line = str(meta["description"])[:200]
        else:
            first_line = (body.splitlines()[0] if body else "").rstrip("\n")[:200] if body else ""
            if not first_line:
                # Body was empty (or file was body-only, no frontmatter case):
                # fall back to the raw first line.
                first_line = (content.splitlines()[0] if content else "").rstrip("\n")[:200]
        return (
            _MemoryDocument(
                path=rel_path,
                size_chars=len(content),
                mtime=stat_result.st_mtime,
                first_line=first_line,
                frontmatter=meta or {},
                raw=content,
            ),
            len(data),
        )

    def read(self, rel_path: str) -> str:
        target = self._resolve(rel_path)
        agent_owned = _is_agent_owned_path(rel_path)
        if not agent_owned:
            root = (
                self.workspace_root
                if rel_path.startswith("workspace/")
                else self.user_root
            )
            target = _safe_index_file(root, target)
            if target is None:
                raise FileNotFoundError(rel_path)
        data, _ = _read_bounded_regular(
            target,
            MAX_MEMORY_FILE_BYTES,
            private=agent_owned,
        )
        return data.decode("utf-8", errors="replace")

    def exists(self, rel_path: str) -> bool:
        try:
            target = self._resolve(rel_path)
            root = self.workspace_root if rel_path.startswith("workspace/") else self.user_root
            target = _safe_index_file(root, target)
            if target is None:
                return False
            if _is_agent_owned_path(rel_path) and target.lstat().st_nlink != 1:
                return False
            return True
        except ValueError:
            return False

    # ---- agent append ------------------------------------------------------

    def append_agent_note(self, scope: Literal["workspace", "user"], note: str) -> int:
        scope = str(scope)
        note = str(note).strip()
        self._reject_sensitive_content(note + "\n" + scope)
        if not note:
            raise ValueError("note must not be empty")
        if len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"note exceeds {MAX_NOTE_CHARS} chars")
        if scope not in {"workspace", "user"}:
            raise ValueError("invalid scope")
        target = self._agent_notes_path(scope)
        ensure_private_dir(target.parent)
        lock_path = target.parent / ".agent_notes.lock"

        with locked_file(lock_path, require_lock=True):
            target = require_regular_no_symlink(target, allow_missing=True)
            try:
                target.lstat()
            except FileNotFoundError:
                existing = ""
            else:
                existing = read_private_text(target)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            new_line = f"- {timestamp}  {note}\n"
            new_content = (
                existing + new_line
                if existing.endswith("\n") or not existing
                else existing + "\n" + new_line
            )
            self._reject_sensitive_content(new_content)
            self._atomic_write(target, new_content)
            size = len(new_content)
        if size > AGENT_NOTES_SOFT_LIMIT_CHARS and scope not in self._size_warned:
            self._size_warned.add(scope)
            print(
                f"warning: {scope}/agent_notes.md is at {size} chars "
                f"(soft target {AGENT_NOTES_SOFT_LIMIT_CHARS}). "
                "Consider: pico memory review",
                file=sys.stderr,
            )
        return size

    # ---- internals ---------------------------------------------------------

    def _agent_notes_path(self, scope: str) -> Path:
        if scope == "workspace":
            return self.workspace_root / "agent_notes.md"
        if scope == "user":
            return self.user_root / "agent_notes.md"
        raise ValueError("invalid scope")

    def _resolve(self, rel_path: str) -> Path:
        if not rel_path or ".." in rel_path.split("/") or rel_path.startswith("/"):
            raise ValueError(f"invalid path: {rel_path!r}")
        parts = rel_path.split("/", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid path (must start with scope/): {rel_path!r}")
        scope, sub = parts
        if scope == "workspace":
            root = self.workspace_root
        elif scope == "user":
            root = self.user_root
        else:
            raise ValueError(f"invalid scope: {scope!r}")
        if sub != "agent_notes.md" and not (
            sub.startswith("notes/") and sub.endswith(".md")
        ):
            raise ValueError("invalid memory path")
        root = Path(os.path.abspath(os.fspath(root)))
        target = Path(os.path.abspath(os.fspath(root / sub)))
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"invalid path (escapes scope root): {rel_path!r}") from exc
        return target

    def _reject_sensitive_content(self, content):
        if securitylib.contains_secret_material(
            content,
            env=self.redaction_env,
            secret_env_names=self.secret_env_names,
        ):
            raise ValueError("sensitive_content")

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        ensure_private_dir(target.parent)
        target = require_regular_no_symlink(target, allow_missing=True)
        temp_path = None
        temp_identity = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=str(target.parent),
                prefix=target.name + ".",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                opened = os.fstat(handle.fileno())
                temp_identity = (opened.st_dev, opened.st_ino)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            current = temp_path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != temp_identity
            ):
                raise ValueError("memory temp changed")
            require_regular_no_symlink(target, allow_missing=True)
            temp_path.replace(target)
            installed = target.lstat()
            if (
                not stat.S_ISREG(installed.st_mode)
                or installed.st_nlink != 1
                or (installed.st_dev, installed.st_ino) != temp_identity
            ):
                if (
                    not stat.S_ISREG(installed.st_mode)
                    or (installed.st_dev, installed.st_ino) == temp_identity
                ):
                    target.unlink()
                raise ValueError("memory temp changed")
            ensure_private_file(target)
        finally:
            if temp_path is not None and temp_identity is not None:
                try:
                    current = temp_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (current.st_dev, current.st_ino) == temp_identity:
                        temp_path.unlink()
