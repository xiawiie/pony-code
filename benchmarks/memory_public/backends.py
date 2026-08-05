"""Benchmark-only adapters for Pony User Notes, mem0 OSS, and full context."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest

from pony.agent.model_capabilities import TokenAccounting
from pony.memory.block_store import (
    MAX_MEMORY_FILE_BYTES,
    MAX_MEMORY_INDEX_BYTES,
    MAX_MEMORY_INDEX_FILES,
    BlockStore,
)
from pony.memory.recall import recall_candidates
from pony.memory.retrieval import Retrieval

from .datasets import Session, Turn, opaque_id, render_context_item


@dataclass(frozen=True)
class RetrievedContext:
    text: str
    tokens: int
    source_ids: tuple[str, ...] = ()


def _clip_text(text: str, accounting: TokenAccounting, limit: int) -> str:
    value = str(text).strip()
    if accounting.count_text(value) <= limit:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if accounting.count_text(value[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip()


def clip_contexts(
    texts: Iterable[str],
    accounting: TokenAccounting,
    budget_tokens: int,
) -> tuple[str, int, int]:
    selected = []
    used = 0
    considered = 0
    for raw in texts:
        considered += 1
        text = str(raw).strip()
        if not text:
            continue
        remaining = budget_tokens - used
        if remaining <= 0:
            break
        clipped = _clip_text(text, accounting, remaining)
        if not clipped:
            break
        selected.append(clipped)
        used += accounting.count_text(clipped)
        if clipped != text:
            break
    return "\n\n".join(selected), used, considered


def _turn_text(turn: Turn) -> str:
    return f"{turn.role}: {turn.content}"


def _note_bytes(name: str, body_lines: list[str]) -> bytes:
    body = "\n\n".join(body_lines)
    return (
        "---\n"
        f"name: {name}\n"
        "type: transcript\n"
        "description: conversation transcript\n"
        "---\n"
        f"{body}\n"
    ).encode("utf-8")


def split_session_note(session: Session) -> list[bytes]:
    source_name = opaque_id(session.source_id, prefix="session")
    chunks = []
    lines = []
    for turn in session.turns:
        candidate = [*lines, _turn_text(turn)]
        rendered = _note_bytes(f"{source_name}-{len(chunks):03d}", candidate)
        if len(rendered) <= MAX_MEMORY_FILE_BYTES:
            lines = candidate
            continue
        if not lines:
            raise ValueError("one transcript turn exceeds Pony memory file limit")
        chunks.append(_note_bytes(f"{source_name}-{len(chunks):03d}", lines))
        lines = [_turn_text(turn)]
        if len(_note_bytes(f"{source_name}-{len(chunks):03d}", lines)) > MAX_MEMORY_FILE_BYTES:
            raise ValueError("one transcript turn exceeds Pony memory file limit")
    if lines:
        chunks.append(_note_bytes(f"{source_name}-{len(chunks):03d}", lines))
    return chunks


class PonyMemoryBackend:
    """Raw transcript -> benchmark User Notes -> production BM25 recall."""

    name = "pony"
    ingest_adapter = "transcript-to-notes-v1"

    def __init__(self, root: Path, *, provider_counter: Callable | None = None):
        self.root = Path(root)
        self.workspace_root = self.root / "workspace"
        self.user_root = self.root / "user"
        self.notes_root = self.workspace_root / "notes"
        self.notes_root.mkdir(parents=True, exist_ok=False)
        self._accounting = TokenAccounting(provider_counter)
        self._source_by_path = {}
        self._file_count = 0
        self._total_bytes = 0
        self._store = None
        self._retrieval = None

    def ingest_sessions(self, sessions: Iterable[Session]) -> None:
        for session_index, session in enumerate(sessions):
            for chunk_index, data in enumerate(split_session_note(session)):
                self._add_note(data, session.source_id, session_index, chunk_index)

    def ingest_items(self, items: Iterable[object], *, source_prefix: str) -> None:
        turns = tuple(
            Turn(role="context", content=render_context_item(item)) for item in items
        )
        if turns:
            self.ingest_sessions((Session(source_id=source_prefix, date="", turns=turns),))

    def _add_note(self, data: bytes, source_id: str, session_index: int, chunk_index: int):
        if self._file_count + 1 > MAX_MEMORY_INDEX_FILES:
            raise ValueError("Pony memory benchmark exceeds 512 files")
        if self._total_bytes + len(data) > MAX_MEMORY_INDEX_BYTES:
            raise ValueError("Pony memory benchmark exceeds 2 MiB index")
        path = self.notes_root / f"note-{self._file_count:04d}-{session_index:04d}-{chunk_index:03d}.md"
        path.write_bytes(data)
        self._source_by_path[f"workspace/notes/{path.name}"] = source_id
        self._file_count += 1
        self._total_bytes += len(data)
        self._store = None
        self._retrieval = None

    def _ensure_retrieval(self):
        if self._retrieval is None:
            self._store = BlockStore(self.workspace_root, self.user_root, redaction_env={})
            self._retrieval = Retrieval(self._store)

    def retrieve(self, question: str, *, budget_tokens: int) -> RetrievedContext:
        self._ensure_retrieval()
        agent = SimpleNamespace(
            memory_store=self._store,
            memory_retrieval=self._retrieval,
            session={"recently_recalled": []},
            model_client=None,
            token_accounting=self._accounting,
            memory=SimpleNamespace(task_summary="", recent_files=[]),
            context_config={},
            redaction_env={},
            secret_env_names=(),
        )
        selected = []
        used = 0
        source_ids = []
        for candidate in recall_candidates(agent, question):
            if used + candidate.tokens > budget_tokens:
                continue
            selected.append(candidate.text)
            used += candidate.tokens
            source_id = self._source_by_path.get(candidate.path)
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
        return RetrievedContext("\n".join(selected), used, tuple(source_ids))


class FullContextBackend:
    name = "full-context"
    ingest_adapter = "chronological-context-v1"

    def __init__(self, *, provider_counter: Callable | None = None):
        self._items = []
        self._accounting = TokenAccounting(provider_counter)

    def ingest_sessions(self, sessions: Iterable[Session]) -> None:
        for session in sessions:
            text = "\n".join(_turn_text(turn) for turn in session.turns)
            self._items.append((text, session.source_id))

    def ingest_items(self, items: Iterable[object], *, source_prefix: str) -> None:
        text = "\n".join(render_context_item(item) for item in items)
        if text.strip():
            self._items.append((text, source_prefix))

    def retrieve(self, question: str, *, budget_tokens: int) -> RetrievedContext:
        del question
        text, tokens, considered = clip_contexts(
            (item[0] for item in reversed(self._items)),
            self._accounting,
            budget_tokens,
        )
        source_ids = tuple(item[1] for item in reversed(self._items[-considered:]))
        return RetrievedContext(text, tokens, source_ids)


class Mem0Client:
    """Small stdlib client for the mem0 OSS HTTP API used by memory-benchmarks."""

    def __init__(self, base_url: str, *, timeout: float = 300, opener=None):
        value = str(base_url).rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("mem0 URL must use http or https")
        self.base_url = value
        self.timeout = float(timeout)
        self._opener = opener or urlrequest.urlopen

    def _post(self, path: str, payload: dict):
        request = urlrequest.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                data = response.read()
        except (OSError, urlerror.URLError) as exc:
            raise RuntimeError(f"mem0 request failed: {path}") from exc
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"mem0 returned HTTP {status}: {path}")
        try:
            return json.loads(data.decode("utf-8")) if data else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("mem0 returned invalid JSON") from exc

    def add(self, messages: list[dict], *, user_id: str, run_id: str, metadata: dict):
        return self._post(
            "/memories",
            {
                "messages": messages,
                "user_id": user_id,
                "run_id": run_id,
                "metadata": metadata,
            },
        )

    def search(
        self,
        query: str,
        *,
        user_id: str,
        run_id: str,
        top_k: int = 200,
    ) -> list[dict]:
        payload = self._post(
            "/search",
            {
                "query": query,
                "filters": {"user_id": user_id, "run_id": run_id},
                "limit": top_k,
            },
        )
        rows = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("mem0 search response must contain a result list")
        return rows


class Mem0Backend:
    name = "mem0"
    ingest_adapter = "mem0-native-v1"

    def __init__(
        self,
        client: Mem0Client,
        *,
        user_id: str,
        run_id: str,
        provider_counter: Callable | None = None,
    ):
        self.client = client
        self.user_id = user_id
        self.run_id = run_id
        self._accounting = TokenAccounting(provider_counter)

    def ingest_sessions(self, sessions: Iterable[Session]) -> None:
        for session in sessions:
            self._ingest_turns(session.turns, source_id=session.source_id)

    def ingest_items(self, items: Iterable[object], *, source_prefix: str) -> None:
        turns = []
        for item in items:
            role = item.get("role") if isinstance(item, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            turns.append(
                Turn(
                    role=role if isinstance(role, str) else "user",
                    content=content if isinstance(content, str) else render_context_item(item),
                )
            )
        self._ingest_turns(tuple(turns), source_id=source_prefix)

    def _ingest_turns(self, turns: tuple[Turn, ...], *, source_id: str) -> None:
        pair = []
        source = opaque_id(source_id, prefix="source")
        for turn in turns:
            role = turn.role if turn.role in {"user", "assistant"} else "user"
            pair.append({"role": role, "content": turn.content})
            if role == "assistant" or len(pair) == 2:
                self.client.add(
                    pair,
                    user_id=self.user_id,
                    run_id=self.run_id,
                    metadata={"source_id": source},
                )
                pair = []
        if pair:
            self.client.add(
                pair,
                user_id=self.user_id,
                run_id=self.run_id,
                metadata={"source_id": source},
            )

    def retrieve(self, question: str, *, budget_tokens: int) -> RetrievedContext:
        rows = self.client.search(
            question,
            user_id=self.user_id,
            run_id=self.run_id,
            top_k=200,
        )
        texts = []
        sources = []
        for row in rows:
            text = row.get("memory", row.get("text"))
            if not isinstance(text, str):
                raise ValueError("mem0 result memory must be text")
            texts.append(text)
            metadata = row.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("source_id"), str):
                sources.append(metadata["source_id"])
        text, tokens, considered = clip_contexts(texts, self._accounting, budget_tokens)
        return RetrievedContext(text, tokens, tuple(sources[:considered]))
