"""Pico memory v2 · per-turn lazy refresher.

每次 turn 开始时调用 refresh_if_stale():
- 扫 memory 目录 mtime, 只在实际变化时重生成 <memory_index> 文本
- 触发 repo_map.refresh_if_stale() 更新符号索引和 top_level_tree
- 只在 top_level_tree 内容变化时重生成 <project_structure> 文本

返回的 RefreshSnapshot 里两个 text 保证 byte-identical 当底层没变.
"""

from __future__ import annotations

from dataclasses import dataclass

from pico import security as securitylib
from pico.memory.block_store import BlockStore
from pico.repo_map import RepoMap


@dataclass(frozen=True)
class RefreshSnapshot:
    memory_index_text: str
    project_structure_text: str


class MemoryRefresher:
    def __init__(self, store: BlockStore, repo_map: RepoMap):
        self.store = store
        self.repo_map = repo_map
        self._last_memory_stat: dict[str, float] = {}
        self._last_project_key: tuple = ()
        self._cached_memory_text = ""
        self._cached_project_text = ""

    def refresh_if_stale(self) -> RefreshSnapshot:
        # Memory index
        entries = self.store.list()
        current_stat = {entry.path: entry.mtime for entry in entries}
        if current_stat != self._last_memory_stat or not self._cached_memory_text:
            self._cached_memory_text = self._render_memory_index(entries)
            self._last_memory_stat = current_stat

        # Repo map (incremental) + project structure. Cache key must include
        # language_stats so first-of-a-kind extensions (e.g. new .rs file
        # under existing src/) still invalidate the rendered header.
        self.repo_map.refresh_if_stale()
        current_tree = tuple((e["path"], e["file_count"]) for e in self.repo_map.top_level_tree())
        current_langs = tuple(sorted(self.repo_map.language_stats().items()))
        current_key = (current_tree, current_langs)
        if current_key != self._last_project_key or not self._cached_project_text:
            self._cached_project_text = self._render_project_structure()
            self._last_project_key = current_key

        return RefreshSnapshot(
            memory_index_text=self._cached_memory_text,
            project_structure_text=self._cached_project_text,
        )

    def _render_memory_index(self, entries=None) -> str:
        entries = self.store.list() if entries is None else entries
        entries = [
            entry
            for entry in entries
            if not securitylib.is_sensitive_path(str(entry.path))
        ]
        if not entries:
            return "<memory_index>\n(no memory files yet)\n</memory_index>"
        lines = ["<memory_index>"]
        notes = [e for e in entries if "/notes/" in e.path]
        agents = [e for e in entries if e.path.endswith("/agent_notes.md")]
        if notes:
            lines.append("Notes (user-written, read-only for agent):")
            for e in notes:
                lines.append(f"- {e.path} ({e.size_chars} chars)")
        if agents:
            lines.append("Agent records:")
            for e in agents:
                lines.append(f"- {e.path} ({e.size_chars} chars)")
        lines.append("Use memory_search / memory_read to access.")
        lines.append("</memory_index>")
        return "\n".join(lines)

    def _render_project_structure(self) -> str:
        tree = [
            entry
            for entry in self.repo_map.top_level_tree()
            if not securitylib.is_sensitive_path(str(entry.get("path", "")))
        ]
        stats = self.repo_map.language_stats()
        if not tree:
            return "<project_structure>\n(empty repo)\n</project_structure>"
        lang_str = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        lines = [f"<project_structure languages=\"{lang_str}\">"]
        for entry in tree:
            lines.append(f"{entry['path']}/  ({entry['file_count']} files)")
        lines.append("</project_structure>")
        return "\n".join(lines)
