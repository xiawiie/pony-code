"""Pico memory v2 subsystem.

结构:
    block_store.py — 记忆文件读写、atomic append、mtime 快照
    retrieval.py   — BM25 + CJK 分词检索
    tools.py       — 4 个 memory tool runner
    refresher.py   — 每 turn lazy mtime 检查
"""

from .block_store import (
    AGENT_NOTES_SOFT_LIMIT_CHARS,
    BlockStore,
    MAX_NOTE_CHARS,
    MemoryFile,
)
from .refresher import MemoryRefresher, RefreshSnapshot
from .retrieval import Retrieval, SearchHit

VERSION = 2

__all__ = [
    "AGENT_NOTES_SOFT_LIMIT_CHARS",
    "BlockStore",
    "MAX_NOTE_CHARS",
    "MemoryFile",
    "MemoryRefresher",
    "RefreshSnapshot",
    "Retrieval",
    "SearchHit",
    "VERSION",
]
