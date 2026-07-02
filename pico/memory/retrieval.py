"""Pico memory v2 · retrieval.

BM25 + CJK bigram tokenizer, stdlib only.

Limitation (记入 spec §7.3): 关键词级匹配, 不做 semantic similarity.
```

    "身份认证" 不会命中含 "auth" 的 note.
```
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from pico.memory.block_store import BlockStore

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿]")

BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(text: str) -> list[str]:
    """英文分词 + CJK 二元组切分."""
    text = str(text)
    tokens = [t.lower() for t in _WORD_RE.findall(text)]
    cjk_chars = _CJK_RE.findall(text)
    for i in range(len(cjk_chars) - 1):
        tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    return tokens


@dataclass(frozen=True)
class SearchHit:
    path: str
    score: float
    snippets: tuple[str, ...] = field(default_factory=tuple)


class Retrieval:
    def __init__(self, store: BlockStore):
        self.store = store

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        docs = self._load_docs()
        if not docs:
            return []

        avg_doc_len = sum(len(tokens) for _, tokens, _ in docs) / len(docs)
        df: Counter = Counter()
        for _, tokens, _ in docs:
            for term in set(tokens):
                df[term] += 1
        N = len(docs)

        results: list[SearchHit] = []
        for path, tokens, raw in docs:
            score = self._bm25_score(query_tokens, tokens, avg_doc_len, N, df)
            if score <= 0:
                continue
            snippets = self._extract_snippets(raw, query_tokens)
            results.append(SearchHit(path=path, score=score, snippets=snippets))

        results.sort(key=lambda h: h.score, reverse=True)
        return results[:limit]

    def _load_docs(self) -> list[tuple[str, list[str], str]]:
        docs: list[tuple[str, list[str], str]] = []
        for entry in self.store.list():
            try:
                raw = self.store.read(entry.path)
            except (OSError, ValueError):
                continue
            tokens = tokenize(raw)
            if tokens:
                docs.append((entry.path, tokens, raw))
        return docs

    @staticmethod
    def _bm25_score(
        query_tokens: Iterable[str],
        doc_tokens: list[str],
        avg_doc_len: float,
        N: int,
        df: Counter,
    ) -> float:
        doc_len = len(doc_tokens)
        if doc_len == 0 or avg_doc_len == 0:
            return 0.0
        doc_counter = Counter(doc_tokens)
        score = 0.0
        for term in set(query_tokens):
            if term not in doc_counter:
                continue
            tf = doc_counter[term]
            idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1)
            norm = tf * (BM25_K1 + 1) / (
                tf + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / avg_doc_len)
            )
            score += idf * norm
        return score

    @staticmethod
    def _extract_snippets(raw: str, query_tokens: Iterable[str]) -> tuple[str, ...]:
        q_lower = [t.lower() for t in query_tokens]
        snippets: list[str] = []
        for i, line in enumerate(raw.splitlines(), start=1):
            line_lower = line.lower()
            if any(term in line_lower for term in q_lower):
                snippets.append(f"L{i}: {line.strip()[:200]}")
                if len(snippets) >= 3:
                    break
        return tuple(snippets)
