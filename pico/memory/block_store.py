"""Pico memory v2 · block store.

职责:
- 读写 `.pico/memory/notes/*.md`（用户手写）和 `.pico/memory/agent_notes.md`（agent 追加）
- 原子写入（tempfile + rename）
- 提供扁平化列表和 mtime 快照给 refresher

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
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pico.workspace import _safe_index_directory, _safe_index_file

from .frontmatter import parse_frontmatter

MAX_NOTE_CHARS = 500
AGENT_NOTES_SOFT_LIMIT_CHARS = 8000

# Task 17: agent-owned topic slug — kebab-case, alphanumeric-first.
# Rejects `..`, `/`, dots, spaces, and any other filesystem-fragile chars.
_TOPIC_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


@dataclass(frozen=True)
class MemoryFile:
    path: str          # e.g. "workspace/notes/auth.md"
    size_chars: int
    mtime: float
    first_line: str
    # Task 17: parsed frontmatter metadata, empty dict when the file has none.
    frontmatter: dict = field(default_factory=dict)


class BlockStore:
    def __init__(self, workspace_root: Path, user_root: Path):
        self.workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
        self.user_root = Path(os.path.abspath(os.fspath(user_root)))
        self._size_warned: set[str] = set()

    # ---- listing / reading -------------------------------------------------

    def list(self) -> list[MemoryFile]:
        entries: list[MemoryFile] = []
        entries.extend(self._scan_scope("workspace", self.workspace_root))
        entries.extend(self._scan_scope("user", self.user_root))
        entries.sort(key=lambda e: e.path)
        return entries

    def _scan_scope(self, scope: str, root: Path) -> list[MemoryFile]:
        root = _safe_index_directory(root, root)
        if root is None:
            return []
        results: list[MemoryFile] = []
        # notes/*.md (nested allowed) — user-written, agent read-only
        notes_dir = _safe_index_directory(root, root / "notes")
        if notes_dir is not None:
            for md in sorted(notes_dir.rglob("*.md")):
                md = _safe_index_file(root, md)
                if md is None:
                    continue
                rel = md.relative_to(root).as_posix()
                entry = self._to_memory_file(root, f"{scope}/{rel}", md)
                if entry is not None:
                    results.append(entry)
        # agent/*.md (Task 17) — agent-owned, per-topic
        agent_dir = _safe_index_directory(root, root / "agent")
        if agent_dir is not None:
            for md in sorted(agent_dir.rglob("*.md")):
                md = _safe_index_file(root, md)
                if md is None:
                    continue
                rel = md.relative_to(root).as_posix()
                entry = self._to_memory_file(root, f"{scope}/{rel}", md)
                if entry is not None:
                    results.append(entry)
        # agent_notes.md (legacy single-file). We exclude anything with the
        # .legacy suffix (post-migration renames).
        agent_notes = _safe_index_file(root, root / "agent_notes.md")
        if agent_notes is not None:
            entry = self._to_memory_file(root, f"{scope}/agent_notes.md", agent_notes)
            if entry is not None:
                results.append(entry)
        return results

    @staticmethod
    def _to_memory_file(root: Path, rel_path: str, real_path: Path) -> MemoryFile | None:
        real_path = _safe_index_file(root, real_path)
        if real_path is None:
            return None
        stat = real_path.stat()
        content = ""
        try:
            content = real_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
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
        return MemoryFile(
            path=rel_path,
            size_chars=len(content),
            mtime=stat.st_mtime,
            first_line=first_line,
            frontmatter=meta or {},
        )

    def read(self, rel_path: str) -> str:
        target = self._resolve(rel_path)
        root = self.workspace_root if rel_path.startswith("workspace/") else self.user_root
        target = _safe_index_file(root, target)
        if target is None:
            raise FileNotFoundError(rel_path)
        return target.read_text(encoding="utf-8", errors="replace")

    def exists(self, rel_path: str) -> bool:
        try:
            target = self._resolve(rel_path)
            root = self.workspace_root if rel_path.startswith("workspace/") else self.user_root
            return _safe_index_file(root, target) is not None
        except ValueError:
            return False

    def stat_all(self) -> dict[str, float]:
        return {entry.path: entry.mtime for entry in self.list()}

    # ---- agent append ------------------------------------------------------

    def append_agent_note(self, scope: Literal["workspace", "user"], note: str) -> int:
        note = str(note).strip()
        if not note:
            raise ValueError("note must not be empty")
        if len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"note exceeds {MAX_NOTE_CHARS} chars")
        target = self._agent_notes_path(scope)
        target.parent.mkdir(parents=True, exist_ok=True)

        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_line = f"- {timestamp}  {note}\n"
        new_content = existing + new_line if existing.endswith("\n") or not existing else existing + "\n" + new_line

        self._atomic_write(target, new_content)
        size = len(new_content)
        if size > AGENT_NOTES_SOFT_LIMIT_CHARS and scope not in self._size_warned:
            self._size_warned.add(scope)
            print(
                f"warning: {scope}/agent_notes.md is at {size} chars "
                f"(soft target {AGENT_NOTES_SOFT_LIMIT_CHARS}). "
                f"Consider: pico-cli memory review",
                file=sys.stderr,
            )
        return size

    # ---- agent topic write (Task 17) ---------------------------------------

    def write_agent_topic(self, scope, topic, note, note_type="feedback"):
        """Create or append `agent/<topic>.md` with frontmatter on first write.

        On first-time create the file gets a full frontmatter block with
        ``name = topic``, ``type = note_type``, and ``description`` seeded
        from the note's first line. On subsequent calls the body is
        appended and the frontmatter is left untouched.

        Raises ``ValueError`` on empty note, bad scope, or a topic slug that
        would let the filename escape ``agent/`` (contains ``..``, ``/``, or
        non-``[A-Za-z0-9_-]`` chars).
        """
        note = str(note).strip()
        if not note:
            raise ValueError("note must not be empty")
        topic = str(topic).strip()
        if not _TOPIC_RE.match(topic):
            raise ValueError(f"invalid topic: {topic!r}")
        if scope == "workspace":
            root = self.workspace_root
        elif scope == "user":
            root = self.user_root
        else:
            raise ValueError(f"unknown scope: {scope!r}")
        agent_dir = root / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        target = agent_dir / f"{topic}.md"
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            new_body = existing.rstrip("\n") + "\n\n" + note + "\n"
            self._atomic_write(target, new_body)
        else:
            description = note.splitlines()[0][:80] if note else ""
            fm = (
                "---\n"
                f"name: {topic}\n"
                f"type: {note_type}\n"
                f"description: {description}\n"
                "tags: []\n"
                "aliases: []\n"
                "supersedes: []\n"
                "---\n"
                f"\n{note}\n"
            )
            self._atomic_write(target, fm)
        return target

    # ---- internals ---------------------------------------------------------

    def _agent_notes_path(self, scope: str) -> Path:
        if scope == "workspace":
            return self.workspace_root / "agent_notes.md"
        if scope == "user":
            return self.user_root / "agent_notes.md"
        raise ValueError(f"unknown scope: {scope}")

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
        root = Path(os.path.abspath(os.fspath(root)))
        target = Path(os.path.abspath(os.fspath(root / sub)))
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"invalid path (escapes scope root): {rel_path!r}") from exc
        return target

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(target.parent),
            prefix=target.name + ".",
            suffix=".tmp",
        ) as fh:
            fh.write(content)
            tmp_name = fh.name
        Path(tmp_name).replace(target)
