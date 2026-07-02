"""Pico repo map · 符号索引 tool 后端.

- Python: stdlib `ast` 提取 class/function/method.
- TypeScript / JavaScript / Go / Rust: 正则 best-effort.
- 其他语言: 不提取符号.

只做 `repo_lookup` tool 后端, 不塞进 prompt.
顶层目录树 (无符号) 会通过 top_level_tree() 塞进 stable prefix 的 <project_structure> 段.
"""

from __future__ import annotations

import ast
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional

SymbolKind = Literal["class", "function", "method"]

# 复用 pico/workspace.py 的忽略列表 + 额外补充
IGNORED_DIRS = frozenset({
    ".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", "dist", "build", "target",
    ".next", ".turbo", "vendor",
})

MAX_FILE_SIZE = 500_000
MAX_FILES = 10_000
MAX_INDEXED_FILES_LARGE_REPO = 500


LANGUAGE_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".md": "markdown",
    ".toml": "toml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}

_JS_TS_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:\(|function)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][A-Za-z0-9_$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="), "class"),
]

_GO_PATTERNS = [
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("), "function"),
    (re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b"), "class"),
]

_RUST_PATTERNS = [
    (re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)"), "function"),
    (re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"), "class"),
]


@dataclass(frozen=True)
class Symbol:
    name: str
    file: str
    line: int
    kind: SymbolKind


class RepoMap:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self._symbols: dict[str, list[Symbol]] = defaultdict(list)
        self._file_mtimes: dict[str, float] = {}
        self._file_count_by_top_dir: dict[str, int] = defaultdict(int)
        self._language_counts: dict[str, int] = defaultdict(int)

    # ---- scan --------------------------------------------------------------

    def scan(self) -> None:
        self._symbols.clear()
        self._file_mtimes.clear()
        self._file_count_by_top_dir.clear()
        self._language_counts.clear()
        for real_path, rel_path in self._walk():
            self._index_file(real_path, rel_path)

    def refresh_if_stale(self) -> None:
        existing = dict(self._file_mtimes)
        stale: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for real_path, rel_path in self._walk():
            seen.add(rel_path)
            try:
                mtime = real_path.stat().st_mtime
            except OSError:
                continue
            if existing.get(rel_path) != mtime:
                stale.append((real_path, rel_path))

        dead = set(existing) - seen
        for rel_path in dead:
            self._remove_file(rel_path)
        for real_path, rel_path in stale:
            self._remove_file(rel_path)
            self._index_file(real_path, rel_path)

    def _remove_file(self, rel_path: str) -> None:
        self._file_mtimes.pop(rel_path, None)
        for symbols in self._symbols.values():
            symbols[:] = [s for s in symbols if s.file != rel_path]
        # rebuild top-level counts + language stats lazily on next tree call
        self._recount_top_level()

    def _recount_top_level(self) -> None:
        self._file_count_by_top_dir.clear()
        self._language_counts.clear()
        for rel_path in self._file_mtimes:
            if "/" in rel_path:
                top = rel_path.split("/", 1)[0]
                self._file_count_by_top_dir[top] += 1
            ext = os.path.splitext(rel_path)[1].lower()
            lang = LANGUAGE_BY_EXT.get(ext, "other")
            self._language_counts[lang] += 1

    def _walk(self) -> Iterable[tuple[Path, str]]:
        indexed = 0
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for fname in filenames:
                real_path = Path(dirpath) / fname
                try:
                    rel = real_path.relative_to(self.repo_root).as_posix()
                except ValueError:
                    continue
                try:
                    size = real_path.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_SIZE:
                    continue
                yield real_path, rel
                indexed += 1
                if indexed >= MAX_FILES:
                    return

    def _index_file(self, real_path: Path, rel_path: str) -> None:
        try:
            mtime = real_path.stat().st_mtime
        except OSError:
            return
        ext = real_path.suffix.lower()
        lang = LANGUAGE_BY_EXT.get(ext, "other")

        symbols: list[Symbol] = []
        try:
            text = real_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        if lang == "python":
            symbols = list(self._extract_python(rel_path, text))
        elif lang in ("typescript", "javascript"):
            symbols = list(self._extract_regex(rel_path, text, _JS_TS_PATTERNS))
        elif lang == "go":
            symbols = list(self._extract_regex(rel_path, text, _GO_PATTERNS))
        elif lang == "rust":
            symbols = list(self._extract_regex(rel_path, text, _RUST_PATTERNS))

        for sym in symbols:
            self._symbols[sym.name].append(sym)

        self._file_mtimes[rel_path] = mtime
        if "/" in rel_path:
            top = rel_path.split("/", 1)[0]
            self._file_count_by_top_dir[top] += 1
        self._language_counts[lang] += 1

    def _extract_python(self, rel_path: str, source: str) -> Iterable[Symbol]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield Symbol(name=node.name, file=rel_path, line=node.lineno, kind="class")
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield Symbol(name=child.name, file=rel_path, line=child.lineno, kind="method")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield Symbol(name=node.name, file=rel_path, line=node.lineno, kind="function")

    @staticmethod
    def _extract_regex(rel_path: str, source: str, patterns: list[tuple[re.Pattern, str]]) -> Iterable[Symbol]:
        for line_no, line in enumerate(source.splitlines(), start=1):
            for pattern, kind in patterns:
                match = pattern.match(line)
                if match:
                    name = match.group(1)
                    yield Symbol(name=name, file=rel_path, line=line_no, kind=kind)  # type: ignore[arg-type]
                    break

    # ---- query -------------------------------------------------------------

    def lookup(self, name: str, kind: Optional[str] = None) -> list[Symbol]:
        results = list(self._symbols.get(name, []))
        if kind:
            results = [s for s in results if s.kind == kind]
        return results

    def top_level_tree(self) -> list[dict]:
        return [
            {"path": path, "kind": "dir", "file_count": count}
            for path, count in sorted(self._file_count_by_top_dir.items())
        ]

    def language_stats(self) -> dict[str, int]:
        return dict(self._language_counts)


# ---- tool runner -----------------------------------------------------------

def tool_repo_lookup(context, args: dict) -> str:
    repo_map: Optional[RepoMap] = getattr(context, "repo_map", None)
    if repo_map is None:
        return "repo_map unavailable"
    symbol = str(args.get("symbol", "")).strip()
    kind = str(args.get("kind", "")).strip() or None
    if not symbol:
        return "error: symbol must not be empty"
    repo_map.refresh_if_stale()
    hits = repo_map.lookup(symbol, kind=kind)
    if not hits:
        return f"No match for symbol {symbol!r}. Try `search` for grep-style lookup."
    lines = [f"Found {len(hits)} match(es) for {symbol!r}:"]
    for hit in hits[:20]:
        lines.append(f"- {hit.file}:{hit.line} ({hit.kind})")
    return "\n".join(lines)
