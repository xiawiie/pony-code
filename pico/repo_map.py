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
import stat
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional

from pico import security as securitylib
from pico.workspace import _safe_index_directory, _safe_index_file

SymbolKind = Literal["class", "function", "method"]

# 复用 pico/workspace.py 的忽略列表 + 额外补充
IGNORED_DIRS = frozenset({
    ".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", "dist", "build", "target",
    ".next", ".turbo", "vendor",
})

MAX_FILE_SIZE = 500_000
MAX_FILES = 10_000
MAX_TOTAL_BYTES = 50 * 1024 * 1024


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


def _read_bounded_regular(path, limit):
    path = Path(os.path.abspath(os.fspath(path)))
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
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (path_current.st_dev, path_current.st_ino)
        ):
            raise ValueError("unsafe repo-map source")
        if opened.st_size > limit:
            raise ValueError("repo-map source too large")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
        if len(data) > limit:
            error = ValueError("repo-map source too large")
            error.bytes_read = len(data)
            raise error
        return data, opened
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class Symbol:
    name: str
    file: str
    line: int
    kind: SymbolKind


_FileFingerprint = tuple[int, int, int]


@dataclass(frozen=True)
class _RepoMapSnapshot:
    loaded: bool
    symbols: dict[str, tuple[Symbol, ...]]
    file_fingerprints: dict[str, _FileFingerprint]
    file_sizes: dict[str, int]
    file_count_by_top_dir: dict[str, int]
    language_counts: dict[str, int]


_EMPTY_SNAPSHOT = _RepoMapSnapshot(
    loaded=False,
    symbols={},
    file_fingerprints={},
    file_sizes={},
    file_count_by_top_dir={},
    language_counts={},
)


class RepoMap:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(os.path.abspath(os.fspath(repo_root)))
        self._snapshot = _EMPTY_SNAPSHOT
        self._warned_cap = False

    # ---- scan --------------------------------------------------------------

    def scan(self) -> None:
        symbols: dict[str, list[Symbol]] = defaultdict(list)
        fingerprints: dict[str, _FileFingerprint] = {}
        sizes: dict[str, int] = {}
        remaining = MAX_TOTAL_BYTES
        for real_path, rel_path in self._walk():
            if remaining <= 0:
                break
            used_bytes, exhausted = self._index_file_into(
                real_path,
                rel_path,
                remaining,
                symbols,
                fingerprints,
                sizes,
            )
            remaining -= used_bytes
            if exhausted:
                break
        self._snapshot = self._build_snapshot(symbols, fingerprints, sizes)

    def refresh_if_stale(self) -> None:
        previous = self._snapshot
        if not previous.loaded:
            self.scan()
            return

        stale: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for real_path, rel_path in self._walk():
            real_path = _safe_index_file(self.repo_root, real_path)
            if real_path is None:
                continue
            seen.add(rel_path)
            try:
                current = real_path.stat(follow_symlinks=False)
            except OSError:
                continue
            fingerprint = self._fingerprint(current)
            if previous.file_fingerprints.get(rel_path) != fingerprint:
                stale.append((real_path, rel_path))

        replaced = {rel_path for _, rel_path in stale}
        replaced.update(set(previous.file_fingerprints) - seen)
        symbols: dict[str, list[Symbol]] = defaultdict(list)
        for name, entries in previous.symbols.items():
            retained = [entry for entry in entries if entry.file not in replaced]
            if retained:
                symbols[name].extend(retained)
        fingerprints = {
            rel_path: fingerprint
            for rel_path, fingerprint in previous.file_fingerprints.items()
            if rel_path not in replaced
        }
        sizes = {
            rel_path: size
            for rel_path, size in previous.file_sizes.items()
            if rel_path not in replaced
        }
        remaining = max(0, MAX_TOTAL_BYTES - sum(sizes.values()))
        for real_path, rel_path in stale:
            if remaining <= 0:
                break
            used_bytes, exhausted = self._index_file_into(
                real_path,
                rel_path,
                remaining,
                symbols,
                fingerprints,
                sizes,
            )
            remaining -= used_bytes
            if exhausted:
                break
        if replaced:
            self._snapshot = self._build_snapshot(symbols, fingerprints, sizes)

    @staticmethod
    def _fingerprint(metadata: os.stat_result) -> _FileFingerprint:
        return (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)

    @staticmethod
    def _build_snapshot(
        symbols: dict[str, list[Symbol]],
        fingerprints: dict[str, _FileFingerprint],
        sizes: dict[str, int],
    ) -> _RepoMapSnapshot:
        file_count_by_top_dir: dict[str, int] = defaultdict(int)
        language_counts: dict[str, int] = defaultdict(int)
        for rel_path in fingerprints:
            if "/" in rel_path:
                top = rel_path.split("/", 1)[0]
                file_count_by_top_dir[top] += 1
            ext = os.path.splitext(rel_path)[1].lower()
            lang = LANGUAGE_BY_EXT.get(ext, "other")
            language_counts[lang] += 1
        return _RepoMapSnapshot(
            loaded=True,
            symbols={name: tuple(entries) for name, entries in symbols.items()},
            file_fingerprints=dict(fingerprints),
            file_sizes=dict(sizes),
            file_count_by_top_dir=dict(file_count_by_top_dir),
            language_counts=dict(language_counts),
        )

    def _walk(self) -> Iterable[tuple[Path, str]]:
        indexed = 0
        root = _safe_index_directory(self.repo_root, self.repo_root)
        if root is None:
            return
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if name not in IGNORED_DIRS
                and not name.startswith(".")
                and _safe_index_directory(root, Path(dirpath) / name) is not None
            ]
            for fname in sorted(filenames):
                if indexed >= MAX_FILES:
                    return
                indexed += 1
                if indexed == MAX_FILES and not self._warned_cap:
                    self._warned_cap = True
                    import sys
                    print(
                        f"warning: repo_map scan hit {MAX_FILES}-file cap; "
                        f"symbols beyond this point are not indexed",
                        file=sys.stderr,
                    )
                real_path = Path(dirpath) / fname
                real_path = _safe_index_file(root, real_path)
                if real_path is None:
                    continue
                try:
                    rel = real_path.relative_to(root).as_posix()
                except ValueError:
                    continue
                yield real_path, rel

    def _index_file_into(
        self,
        real_path: Path,
        rel_path: str,
        remaining: int,
        symbols_by_name: dict[str, list[Symbol]],
        fingerprints: dict[str, _FileFingerprint],
        sizes: dict[str, int],
    ):
        real_path = _safe_index_file(self.repo_root, real_path)
        if real_path is None:
            return 0, False
        limit = min(MAX_FILE_SIZE, remaining)
        try:
            data, opened = _read_bounded_regular(real_path, limit)
        except (OSError, RuntimeError, ValueError) as exc:
            used_bytes = getattr(exc, "bytes_read", 0)
            exhausted = used_bytes >= remaining or (
                str(exc) == "repo-map source too large"
                and limit < MAX_FILE_SIZE
            )
            return used_bytes, exhausted
        ext = real_path.suffix.lower()
        lang = LANGUAGE_BY_EXT.get(ext, "other")

        symbols: list[Symbol] = []
        text = data.decode("utf-8", errors="replace")

        if lang == "python":
            symbols = list(self._extract_python(rel_path, text))
        elif lang in ("typescript", "javascript"):
            symbols = list(self._extract_regex(rel_path, text, _JS_TS_PATTERNS))
        elif lang == "go":
            symbols = list(self._extract_regex(rel_path, text, _GO_PATTERNS))
        elif lang == "rust":
            symbols = list(self._extract_regex(rel_path, text, _RUST_PATTERNS))

        for sym in symbols:
            symbols_by_name[sym.name].append(sym)

        fingerprints[rel_path] = self._fingerprint(opened)
        sizes[rel_path] = len(data)
        return len(data), False

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
        snapshot = self._snapshot
        results = list(snapshot.symbols.get(name, ()))
        if kind:
            results = [s for s in results if s.kind == kind]
        return results

    def top_level_tree(self) -> list[dict]:
        snapshot = self._snapshot
        return [
            {"path": path, "kind": "dir", "file_count": count}
            for path, count in sorted(snapshot.file_count_by_top_dir.items())
        ]

    def language_stats(self) -> dict[str, int]:
        return dict(self._snapshot.language_counts)


# ---- tool runner -----------------------------------------------------------

def tool_repo_lookup(context, args: dict) -> str:
    repo_map: Optional[RepoMap] = getattr(context, "repo_map", None)
    if repo_map is None:
        raise RuntimeError("repo_map unavailable")
    symbol = str(args.get("symbol", "")).strip()
    kind = str(args.get("kind", "")).strip() or None
    repo_map.refresh_if_stale()
    hits = repo_map.lookup(symbol, kind=kind)
    if not hits:
        return f"No match for symbol {symbol!r}. Try `search` for grep-style lookup."
    lines = [f"Found {len(hits)} match(es) for {symbol!r}:"]
    for hit in hits[:20]:
        lines.append(f"- {hit.file}:{hit.line} ({hit.kind})")
    return "\n".join(lines)
