"""Pico memory · retrieval.

BM25 + CJK bigram tokenizer, stdlib only. Task 18 introduces **field
boost**: frontmatter fields (`name`, `description`, `tags`, `aliases`)
get a multiplicative weight on their term-frequency contribution, so a
hit inside a note's title or tag counts for more than a stray mention
in the body.

Field weights (spec §5.3):

    name: 5.0
    description: 3.0
    tags: 4.0
    aliases: 4.0
    body: 1.0

Weights apply during the tf accumulation only. IDF and length
normalization keep the standard BM25 form. The intent is not to make
scores comparable to any external corpus but to give the on-disk memory
layout a consistent, explainable ranking behavior.

Limitation (unchanged from v2): keyword-level matching, no semantic
similarity. "身份认证" will not surface a note containing only "auth".
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from pico.memory.block_store import BlockStore
from pico.memory.frontmatter import parse_frontmatter

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿]")

BM25_K1 = 1.5
BM25_B = 0.75

# Task 18: per-field boost applied at tf accumulation.
FIELD_BOOSTS = {
    "name": 5.0,
    "description": 3.0,
    "tags": 4.0,
    "aliases": 4.0,
    "body": 1.0,
}


def tokenize(text: str) -> list[str]:
    """English word split + CJK bigram (per-chunk).

    CJK bigrams are generated only inside whitespace-delimited chunks so
    "使用 加密" does not produce a cross-word "用加" bigram.
    """
    text = str(text)
    tokens = [t.lower() for t in _WORD_RE.findall(text)]
    for chunk in re.split(r"\s+", text):
        cjk_chars = _CJK_RE.findall(chunk)
        for i in range(len(cjk_chars) - 1):
            tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    return tokens


def tokenize_by_field(frontmatter: dict, body: str) -> dict[str, list[str]]:
    """Return a token list per BM25 field (name/description/tags/aliases/body)."""
    fm = frontmatter or {}
    tags_text = " ".join(fm.get("tags") or [])
    aliases_text = " ".join(fm.get("aliases") or [])
    return {
        "name": tokenize(str(fm.get("name", ""))),
        "description": tokenize(str(fm.get("description", ""))),
        "tags": tokenize(tags_text),
        "aliases": tokenize(aliases_text),
        "body": tokenize(body or ""),
    }


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

        # Flat token counts drive avg_doc_len and df — the length normalization
        # remains BM25 standard; only tf accumulation is field-weighted.
        avg_doc_len = sum(len(flat) for _path, flat, _raw, _fields in docs) / len(docs)
        df: Counter = Counter()
        for _path, flat, _raw, _fields in docs:
            for term in set(flat):
                df[term] += 1
        N = len(docs)

        results: list[SearchHit] = []
        for path, flat, raw, fields in docs:
            score = self._bm25_field_score(query_tokens, fields, flat, avg_doc_len, N, df)
            if score <= 0:
                continue
            snippets = self._extract_snippets(raw, query_tokens)
            results.append(SearchHit(path=path, score=score, snippets=snippets))

        results.sort(key=lambda h: h.score, reverse=True)
        return results[:limit]

    def _load_docs(self):
        """Return ``[(path, flat_tokens, raw_text, per_field_tokens), ...]``."""
        docs = []
        for entry in self.store.list():
            try:
                raw = self.store.read(entry.path)
            except (OSError, ValueError):
                continue
            fm, body = parse_frontmatter(raw)
            fields = tokenize_by_field(fm, body if fm else raw)
            flat = []
            for ftokens in fields.values():
                flat.extend(ftokens)
            if flat:
                docs.append((entry.path, flat, raw, fields))
        return docs

    @staticmethod
    def _bm25_field_score(
        query_tokens: Iterable[str],
        fields: dict[str, list[str]],
        flat_tokens: list[str],
        avg_doc_len: float,
        N: int,
        df: Counter,
    ) -> float:
        doc_len = len(flat_tokens)
        if doc_len == 0 or avg_doc_len == 0:
            return 0.0
        counters = {name: Counter(tokens) for name, tokens in fields.items()}
        score = 0.0
        for term in set(query_tokens):
            if term not in df:
                continue
            tf_weighted = sum(
                FIELD_BOOSTS.get(name, 1.0) * counters[name].get(term, 0)
                for name in counters
            )
            if tf_weighted == 0:
                continue
            idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1)
            norm = tf_weighted * (BM25_K1 + 1) / (
                tf_weighted + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / avg_doc_len)
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
