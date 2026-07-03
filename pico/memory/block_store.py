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

import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

MAX_NOTE_CHARS = 500
AGENT_NOTES_SOFT_LIMIT_CHARS = 8000


@dataclass(frozen=True)
class MemoryFile:
    path: str          # e.g. "workspace/notes/auth.md"
    size_chars: int
    mtime: float
    first_line: str


class BlockStore:
    def __init__(self, workspace_root: Path, user_root: Path):
        self.workspace_root = Path(workspace_root)
        self.user_root = Path(user_root)
        self._size_warned: set[str] = set()

    # ---- listing / reading -------------------------------------------------

    def list(self) -> list[MemoryFile]:
        entries: list[MemoryFile] = []
        entries.extend(self._scan_scope("workspace", self.workspace_root))
        entries.extend(self._scan_scope("user", self.user_root))
        entries.sort(key=lambda e: e.path)
        return entries

    def _scan_scope(self, scope: str, root: Path) -> list[MemoryFile]:
        if not root.exists():
            return []
        results: list[MemoryFile] = []
        # notes/*.md (可嵌套)
        notes_dir = root / "notes"
        if notes_dir.exists():
            for md in sorted(notes_dir.rglob("*.md")):
                if not md.is_file():
                    continue
                rel = md.relative_to(root).as_posix()
                results.append(self._to_memory_file(f"{scope}/{rel}", md))
        # agent_notes.md
        agent_notes = root / "agent_notes.md"
        if agent_notes.is_file():
            results.append(self._to_memory_file(f"{scope}/agent_notes.md", agent_notes))
        return results

    @staticmethod
    def _to_memory_file(rel_path: str, real_path: Path) -> MemoryFile:
        stat = real_path.stat()
        content = ""
        try:
            content = real_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        first_line = (content.splitlines()[0] if content else "").rstrip("\n")
        return MemoryFile(
            path=rel_path,
            size_chars=len(content),
            mtime=stat.st_mtime,
            first_line=first_line[:200],
        )

    def read(self, rel_path: str) -> str:
        target = self._resolve(rel_path)
        return target.read_text(encoding="utf-8", errors="replace")

    def exists(self, rel_path: str) -> bool:
        try:
            return self._resolve(rel_path).is_file()
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
        target = (root / sub).resolve()
        # symlink safety: target must live under root
        try:
            target.relative_to(root.resolve())
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
