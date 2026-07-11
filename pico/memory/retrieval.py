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

Limitation: keyword-level matching, no semantic
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

# Task 19: `[[name]]` link expansion caps.
LINK_MAX_ADDED = 3       # at most this many neighbors per query
LINK_DECAY = 0.4         # neighbor score = primary_score × decay
LINK_DEPTH = 1           # depth cap — no recursion beyond one hop

# `[[name]]` — kebab-case-friendly User Note links.
_LINK_RE = re.compile(r"\[\[([a-zA-Z0-9][a-zA-Z0-9_-]*)\]\]")


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


@dataclass(frozen=True)
class _IndexedDocument:
    path: str
    raw: str
    frontmatter: dict
    fields: dict[str, list[str]]
    flat_tokens: list[str]


class Retrieval:
    def __init__(self, store: BlockStore, *, config=None):
        self.store = store
        # Task B5: allow pico.toml overrides for field boosts + link config.
        # Passing None keeps the module-level constants active for callers
        # that don't wire config yet.
        cfg = config or {}
        self._field_boosts = cfg.get("field_boosts", FIELD_BOOSTS)
        link_cfg = cfg.get("link_config", (LINK_MAX_ADDED, LINK_DECAY))
        self._link_max_added, self._link_decay = link_cfg

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        hits, _documents = self._search_with_documents(query, limit)
        return hits

    def _search_with_documents(self, query: str, limit: int):
        query_tokens = tokenize(query)
        if not query_tokens:
            return [], {}

        docs = self._index_documents(self.store._load_documents())
        if not docs:
            return [], {}
        documents_by_path = {document.path: document for document in docs}

        # Flat token counts drive avg_doc_len and df — the length normalization
        # remains BM25 standard; only tf accumulation is field-weighted.
        avg_doc_len = sum(len(document.flat_tokens) for document in docs) / len(docs)
        df: Counter = Counter()
        for document in docs:
            for term in set(document.flat_tokens):
                df[term] += 1
        N = len(docs)

        results: list[SearchHit] = []
        for document in docs:
            score = self._bm25_field_score(
                query_tokens,
                document.fields,
                document.flat_tokens,
                avg_doc_len,
                N,
                df,
            )
            if score <= 0:
                continue
            snippets = self._extract_snippets(document.raw, query_tokens)
            results.append(
                SearchHit(
                    path=document.path,
                    score=score,
                    snippets=snippets,
                )
            )

        results.sort(key=lambda h: h.score, reverse=True)
        primary = results[:limit]

        # Task 19: one-hop link expansion. Walk `[[name]]` markers in each
        # primary hit's body, look up the target note by frontmatter `name`,
        # and pull it in with a decayed score. Deduplicated against the
        # primary set and against neighbors already added.
        primary_paths = {h.path for h in primary}
        name_to_path = self._name_to_path_index(docs)
        expanded: list[SearchHit] = []
        for hit in primary:
            if len(expanded) >= self._link_max_added:
                break
            source = documents_by_path.get(hit.path)
            if source is None:
                continue
            seen_here = {e.path for e in expanded}
            for match in _LINK_RE.finditer(source.raw):
                if len(expanded) >= self._link_max_added:
                    break
                neighbor_name = match.group(1)
                neighbor_path = name_to_path.get(neighbor_name)
                if not neighbor_path:
                    continue
                if neighbor_path in primary_paths or neighbor_path in seen_here:
                    continue
                expanded.append(
                    SearchHit(
                        path=neighbor_path,
                        score=hit.score * self._link_decay,
                        snippets=(f"(via [[{neighbor_name}]] from {hit.path})",),
                    )
                )
                seen_here.add(neighbor_path)

        return primary + expanded, documents_by_path

    @staticmethod
    def _name_to_path_index(docs):
        """Build ``frontmatter.name → store path`` from this query snapshot."""
        return {
            document.frontmatter["name"]: document.path
            for document in docs
            if document.frontmatter.get("name")
        }

    @staticmethod
    def _index_documents(snapshot):
        """Tokenize one BlockStore snapshot and apply tombstones.

        Every consumer in one query reuses these raw documents: tombstones,
        fields, snippets, names, and link expansion never reopen a file.
        """
        superseded = {
            name
            for document in snapshot
            for name in (document.frontmatter.get("supersedes") or [])
            if name
        }
        docs = []
        for document in snapshot:
            entry_name = document.frontmatter.get("name")
            if entry_name and entry_name in superseded:
                continue
            fm, body = parse_frontmatter(document.raw)
            fields = tokenize_by_field(fm, body if fm else document.raw)
            flat = []
            for ftokens in fields.values():
                flat.extend(ftokens)
            if flat:
                docs.append(
                    _IndexedDocument(
                        path=document.path,
                        raw=document.raw,
                        frontmatter=fm,
                        fields=fields,
                        flat_tokens=flat,
                    )
                )
        return docs

    def _bm25_field_score(
        self,
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
                self._field_boosts.get(name, 1.0) * counters[name].get(term, 0)
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
