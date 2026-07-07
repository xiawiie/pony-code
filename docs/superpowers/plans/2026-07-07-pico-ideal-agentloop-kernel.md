# Pico Ideal AgentLoop Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Pico's runtime so `AgentLoop` is the kernel, `SessionRecord` is the only persisted transcript, `ModelRequest` is the only model request object, and the main path no longer writes `history/messages` or calls `build_v2/complete_v2`.

**Architecture:** This is a hard internal schema upgrade, not a compatibility wrapper. `schema_version = 3` sessions keep non-transcript runtime state such as `memory.file_summaries`, `checkpoints`, `runtime_identity`, `resume_state`, `recovery`, `working_memory`, and `recently_recalled`, while replacing only the persisted transcript with `records`. The runtime moves vertically: session records first, then context request construction, provider request completion, tool-call execution, and finally `AgentLoop` orchestration.

**Tech Stack:** Python 3.10+, dataclasses, pytest, ruff, existing Pico modules under `pico/`, canonical verification through `./scripts/check.sh`.

---

## Scope Check

This is one dependent vertical migration rather than separate independent subsystems. Splitting session, context, provider, and loop into isolated feature branches would leave Pico in a mixed state where the old transcript remains the effective source of truth. The tasks below are still bite-sized and each task ends with focused tests.

## Code Alignment Review

The current code path verified before writing this plan:

- `pico/agent_loop.py` writes both `session["messages"]` and `session["history"]`, calls `ContextManager.build_v2()`, and calls `model_client.complete_v2(...)`.
- `pico/runtime.py` wraps providers without `complete_v2` in `FallbackAdapter`, initializes `schema_version = 2`, and persists transcript state through `record()` and `record_message()`.
- `pico/context_manager.py` has `build()` for rich flat prompt metadata and `build_v2()` for a thinner message-array request.
- `pico/session_store.py` only migrates v1 `history` to v2 `messages`.
- `pico/tool_executor.py` already has `ToolExecutionResult` and the real safety/recovery path; it needs a `ToolCall` input wrapper, not a rewrite.
- Provider support is uneven: Anthropic-compatible has native `complete_v2`; OpenAI-compatible, Ollama, and Fake providers still use flat `complete(...)` plus `FallbackAdapter`.
- Tests still assert `history`, `messages`, `build_v2`, `complete_v2`, and `FallbackAdapter`; those assertions must be replaced, not carried as permanent compatibility.

## File Structure

- Create `pico/model_request.py`: owns the `ModelRequest` dataclass only.
- Create `pico/session_records.py`: owns `SessionRecord`, record constructors, v1/v2-to-v3 transcript conversion, and projections from records to model messages/history-like views.
- Create `pico/providers/text_protocol.py`: provider-internal helper for prompt-string providers and tests; flattens `ModelRequest` and parses Pico text protocol output into `Response`.
- Modify `pico/session_store.py`: replace v1-to-v2 migration with one-way v1/v2-to-v3 migration, backup old files, and strip removed transcript fields from v3 saves.
- Modify `pico/runtime.py`: initialize v3 sessions, expose `append_record()`, remove `record_message()`, route transcript-derived helpers through projections, and keep non-transcript session state.
- Modify `pico/context_manager.py`: add `build_request(user_message, records) -> ModelRequest`, keep `build()` for prompt preview/report helpers, and remove `build_v2()`.
- Modify `pico/providers/anthropic_compatible.py`: add native `complete_request(...)`.
- Modify `pico/providers/openai_compatible.py`, `pico/providers/ollama.py`, and `pico/providers/clients.py`: add `complete_request(...)` using `text_protocol` where the provider remains prompt-string based.
- Delete `pico/providers/fallback_adapter.py`: no central fallback wrapper remains.
- Modify `pico/tool_executor.py`: add `ToolCall` and `execute_call(call)`.
- Modify `pico/agent_loop.py`: use `SessionRecord`, `ModelRequest`, `complete_request`, `ToolCall`, and records-only persistence.
- Modify focused tests first; broad old assertions against `history/messages/build_v2/complete_v2/FallbackAdapter` are replaced with v3 assertions.

---

### Task 1: Add Kernel Data Types And Projection Helpers

**Files:**
- Create: `pico/model_request.py`
- Create: `pico/session_records.py`
- Create: `tests/test_session_records.py`

- [ ] **Step 1: Write failing projection tests**

Create `tests/test_session_records.py`:

```python
from pico.session_records import (
    SessionRecord,
    new_session_record,
    records_from_history,
    records_from_messages,
    records_to_history,
    records_to_model_messages,
    recent_tool_call_matches,
)


def test_session_record_round_trips_to_plain_dict():
    record = new_session_record(
        kind="user",
        content={"text": "hello"},
        run_id="run_1",
        task_id="task_1",
        created_at="2026-07-07T00:00:00+00:00",
        meta={"source": "test"},
    )

    assert record["kind"] == "user"
    assert record["content"] == {"text": "hello"}
    assert record["run_id"] == "run_1"
    assert record["task_id"] == "task_1"
    assert record["meta"] == {"source": "test"}

    typed = SessionRecord.from_dict(record)
    assert typed.to_dict() == record


def test_history_entries_convert_to_records_and_back():
    history = [
        {"role": "user", "content": "inspect", "created_at": "1"},
        {"role": "assistant", "content": "checking", "created_at": "2"},
        {
            "role": "tool",
            "name": "read_file",
            "args": {"path": "README.md"},
            "content": "demo",
            "created_at": "3",
        },
    ]

    records = records_from_history(history)

    assert [record["kind"] for record in records] == ["user", "assistant", "tool_call", "tool_result"]
    assert records[2]["content"]["name"] == "read_file"
    assert records[2]["content"]["input"] == {"path": "README.md"}
    assert records[3]["content"]["content"] == "demo"
    assert records_to_history(records)[-1]["role"] == "tool"
    assert records_to_history(records)[-1]["name"] == "read_file"


def test_messages_convert_to_records_and_model_messages():
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "demo",
                }
            ],
        },
        {"role": "assistant", "content": "done"},
    ]

    records = records_from_messages(messages)
    projected = records_to_model_messages(records)

    assert [record["kind"] for record in records] == ["user", "tool_call", "tool_result", "assistant"]
    assert projected == messages


def test_recent_tool_call_matches_short_loop_window():
    records = [
        new_session_record("tool_call", {"name": "list_files", "input": {}}, created_at="1"),
        new_session_record("tool_result", {"name": "list_files", "content": "(empty)"}, created_at="2"),
        new_session_record("tool_call", {"name": "read_file", "input": {"path": "README.md"}}, created_at="3"),
        new_session_record("tool_result", {"name": "read_file", "content": "demo"}, created_at="4"),
        new_session_record("tool_call", {"name": "list_files", "input": {}}, created_at="5"),
        new_session_record("tool_result", {"name": "list_files", "content": "(empty)"}, created_at="6"),
    ]

    assert recent_tool_call_matches(records, "list_files", {}) is True
    assert recent_tool_call_matches(records, "read_file", {"path": "README.md"}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_session_records.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pico.session_records'`.

- [ ] **Step 3: Add `ModelRequest`**

Create `pico/model_request.py`:

```python
"""Provider-neutral model request shape used by AgentLoop."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelRequest:
    system: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    cache_control_breakpoints: list[int] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "system": list(self.system),
            "tools": list(self.tools),
            "messages": list(self.messages),
            "cache_control_breakpoints": list(self.cache_control_breakpoints),
            "metadata": dict(self.metadata),
        }
```

- [ ] **Step 4: Add session record helpers**

Create `pico/session_records.py`:

```python
"""SessionRecord helpers and transcript projections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .workspace import now


SESSION_SCHEMA_VERSION = 3
TRANSCRIPT_RECORD_KINDS = {
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    "model_error",
    "runtime_notice",
    "context_reduction",
}


@dataclass(frozen=True)
class SessionRecord:
    id: str
    kind: str
    content: dict
    created_at: str
    run_id: str | None = None
    task_id: str | None = None
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict) -> "SessionRecord":
        return cls(
            id=str(value.get("id") or _new_record_id()),
            kind=str(value.get("kind") or "runtime_notice"),
            content=dict(value.get("content") or {}),
            created_at=str(value.get("created_at") or now()),
            run_id=value.get("run_id"),
            task_id=value.get("task_id"),
            meta=dict(value.get("meta") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": dict(self.content),
            "created_at": self.created_at,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "meta": dict(self.meta),
        }


def _new_record_id() -> str:
    return "rec_" + uuid.uuid4().hex[:12]


def new_session_record(
    kind: str,
    content: dict,
    *,
    created_at: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    meta: dict | None = None,
) -> dict:
    return SessionRecord(
        id=_new_record_id(),
        kind=str(kind),
        content=dict(content or {}),
        created_at=str(created_at or now()),
        run_id=run_id,
        task_id=task_id,
        meta=dict(meta or {}),
    ).to_dict()


def normalize_records(records) -> list[dict]:
    if not isinstance(records, list):
        return []
    return [SessionRecord.from_dict(record).to_dict() for record in records if isinstance(record, dict)]


def records_from_history(history) -> list[dict]:
    records: list[dict] = []
    for entry in list(history or []):
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        created_at = entry.get("created_at")
        if role == "user":
            records.append(new_session_record("user", {"text": entry.get("content", "")}, created_at=created_at))
        elif role == "assistant":
            records.append(new_session_record("assistant", {"text": entry.get("content", "")}, created_at=created_at))
        elif role == "tool":
            tool_call_id = f"toolu_migrated_{uuid.uuid4().hex[:12]}"
            name = str(entry.get("name") or "")
            records.append(
                new_session_record(
                    "tool_call",
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "input": dict(entry.get("args") or {}),
                    },
                    created_at=created_at,
                )
            )
            records.append(
                new_session_record(
                    "tool_result",
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "content": entry.get("content", ""),
                    },
                    created_at=created_at,
                )
            )
    return records


def records_from_messages(messages) -> list[dict]:
    records: list[dict] = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        created_at = dict(message.get("_pico_meta") or {}).get("created_at")
        content = message.get("content")
        if isinstance(content, str):
            if role in {"user", "assistant"}:
                records.append(new_session_record(role, {"text": content}, created_at=created_at))
            continue
        for block in list(content or []):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if role == "assistant" and block_type == "text":
                records.append(new_session_record("assistant", {"text": block.get("text", "")}, created_at=created_at))
            elif role == "assistant" and block_type == "tool_use":
                records.append(
                    new_session_record(
                        "tool_call",
                        {
                            "tool_call_id": block.get("id") or f"toolu_migrated_{uuid.uuid4().hex[:12]}",
                            "name": block.get("name", ""),
                            "input": dict(block.get("input") or {}),
                        },
                        created_at=created_at,
                    )
                )
            elif role == "user" and block_type == "tool_result":
                records.append(
                    new_session_record(
                        "tool_result",
                        {
                            "tool_call_id": block.get("tool_use_id", ""),
                            "name": block.get("name", ""),
                            "content": block.get("content", ""),
                        },
                        created_at=created_at,
                    )
                )
            elif role == "user" and block_type == "text":
                records.append(new_session_record("user", {"text": block.get("text", "")}, created_at=created_at))
    return records


def records_to_model_messages(records) -> list[dict]:
    messages: list[dict] = []
    for record in normalize_records(records):
        kind = record["kind"]
        content = record["content"]
        if kind == "user":
            _append_text_message(messages, "user", str(content.get("text", "")))
        elif kind == "assistant":
            _append_text_message(messages, "assistant", str(content.get("text", "")))
        elif kind == "tool_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": str(content.get("tool_call_id") or record["id"]),
                            "name": str(content.get("name") or ""),
                            "input": dict(content.get("input") or {}),
                        }
                    ],
                }
            )
        elif kind == "tool_result":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(content.get("tool_call_id") or ""),
                            "content": str(content.get("content") or ""),
                        }
                    ],
                }
            )
        elif kind in {"runtime_notice", "model_error", "context_reduction"}:
            _append_text_message(messages, "user", f"Pico runtime notice: {content.get('text', '')}")
    return messages


def records_to_history(records) -> list[dict]:
    history: list[dict] = []
    pending_tool_call_by_id: dict[str, dict] = {}
    for record in normalize_records(records):
        kind = record["kind"]
        content = record["content"]
        created_at = record["created_at"]
        if kind in {"user", "assistant"}:
            history.append({"role": kind, "content": content.get("text", ""), "created_at": created_at})
        elif kind == "runtime_notice":
            history.append({"role": "assistant", "content": content.get("text", ""), "created_at": created_at})
        elif kind == "model_error":
            history.append({"role": "assistant", "content": content.get("text", ""), "created_at": created_at})
        elif kind == "tool_call":
            tool_call_id = str(content.get("tool_call_id") or record["id"])
            pending_tool_call_by_id[tool_call_id] = record
        elif kind == "tool_result":
            tool_call_id = str(content.get("tool_call_id") or "")
            call = pending_tool_call_by_id.get(tool_call_id, {})
            call_content = dict(call.get("content") or {})
            history.append(
                {
                    "role": "tool",
                    "name": content.get("name") or call_content.get("name", ""),
                    "args": dict(call_content.get("input") or {}),
                    "content": content.get("content", ""),
                    "created_at": created_at,
                }
            )
    return history


def recent_tool_call_matches(records, name: str, args: dict, *, window: int = 6) -> bool:
    calls = [
        record
        for record in normalize_records(records)
        if record["kind"] == "tool_call"
    ]
    recent = calls[-int(window):]
    return sum(
        1
        for record in recent
        if record["content"].get("name") == name and dict(record["content"].get("input") or {}) == dict(args or {})
    ) >= 2


def _append_text_message(messages: list[dict], role: str, text: str) -> None:
    text = str(text)
    if not text:
        return
    if messages and messages[-1]["role"] == role and isinstance(messages[-1]["content"], str):
        messages[-1]["content"] += "\n\n" + text
        return
    messages.append({"role": role, "content": text})
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_session_records.py -q
```

Expected: PASS, 4 tests.

- [ ] **Step 6: Commit**

Run:

```bash
git add pico/model_request.py pico/session_records.py tests/test_session_records.py
git commit -m "feat: add session records and model request"
```

Expected: commit succeeds with only those three files staged.

---

### Task 2: Upgrade SessionStore To Schema v3

**Files:**
- Modify: `pico/session_store.py`
- Modify: `tests/test_session_store_migrator.py`
- Modify: `tests/test_session_store.py`

- [ ] **Step 1: Replace migrator tests with v3 expectations**

Edit `tests/test_session_store_migrator.py` so its assertions use records:

```python
import json

import pytest

from pico.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / ".pico" / "sessions")


def _v1_session(tmp_path):
    return {
        "id": "s1",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_root": str(tmp_path),
        "history": [
            {"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "content": "hello", "created_at": "2026-01-01T00:00:02Z"},
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "a.py"},
                "content": "file content",
                "created_at": "2026-01-01T00:00:03Z",
            },
        ],
        "working_memory": {"task_summary": "", "recent_files": []},
        "memory": {"file_summaries": {"a.py": {"summary": "old"}}},
    }


def test_migrator_converts_v1_history_to_v3_records(store, tmp_path):
    v1 = _v1_session(tmp_path)
    session_path = store.path_for(v1["id"])
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v1), encoding="utf-8")

    loaded = store.load("s1")

    assert loaded["schema_version"] == 3
    assert "history" not in loaded
    assert "messages" not in loaded
    assert [record["kind"] for record in loaded["records"]] == ["user", "assistant", "tool_call", "tool_result"]
    assert loaded["memory"]["file_summaries"]["a.py"]["summary"] == "old"
    assert loaded["working_memory"] == {"task_summary": "", "recent_files": []}


def test_migrator_converts_v2_messages_to_v3_records(store, tmp_path):
    v2 = {
        "id": "s2",
        "workspace_root": str(tmp_path),
        "schema_version": 2,
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "file body",
                    }
                ],
            },
        ],
        "checkpoints": {"current_id": "ckpt_1", "items": {}},
        "runtime_identity": {"cwd": str(tmp_path)},
        "resume_state": {"status": "full-valid"},
    }
    session_path = store.path_for("s2")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v2), encoding="utf-8")

    loaded = store.load("s2")

    assert loaded["schema_version"] == 3
    assert "messages" not in loaded
    assert [record["kind"] for record in loaded["records"]] == ["user", "tool_call", "tool_result"]
    assert loaded["checkpoints"]["current_id"] == "ckpt_1"
    assert loaded["runtime_identity"]["cwd"] == str(tmp_path)
    assert loaded["resume_state"]["status"] == "full-valid"


def test_migrator_writes_versioned_backup(store, tmp_path):
    v1 = _v1_session(tmp_path)
    session_path = store.path_for(v1["id"])
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v1), encoding="utf-8")

    store.load("s1")

    backups = list((session_path.parent / "backup").glob("s1.v1.*.json"))
    assert len(backups) == 1
    assert "history" in json.loads(backups[0].read_text(encoding="utf-8"))


def test_migrator_idempotent_on_v3(store, tmp_path):
    v3 = {
        "id": "s3",
        "workspace_root": str(tmp_path),
        "schema_version": 3,
        "records": [{"id": "rec_1", "kind": "user", "content": {"text": "hi"}, "created_at": "1", "run_id": None, "task_id": None, "meta": {}}],
    }
    session_path = store.path_for("s3")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v3), encoding="utf-8")

    loaded = store.load("s3")

    assert loaded["schema_version"] == 3
    assert loaded["records"][0]["kind"] == "user"
    backup_dir = session_path.parent / "backup"
    assert not backup_dir.exists() or not list(backup_dir.glob("s3.v3.*.json"))
```

Edit `tests/test_session_store.py` so save/load examples use `schema_version: 3` and `records`:

```python
first = {
    "id": "session_001",
    "schema_version": 3,
    "records": [{"id": "rec_1", "kind": "user", "content": {"text": "first"}, "created_at": "1", "run_id": None, "task_id": None, "meta": {}}],
}
second = {
    "id": "session_002",
    "schema_version": 3,
    "records": [{"id": "rec_2", "kind": "user", "content": {"text": "second"}, "created_at": "2", "run_id": None, "task_id": None, "meta": {}}],
}
```

Replace atomic/lock save payloads with:

```python
store.save({"id": "session_atomic", "schema_version": 3, "records": []})
store.save({"id": "session_locked", "schema_version": 3, "records": []})
```

- [ ] **Step 2: Run migrator tests to verify they fail**

Run:

```bash
uv run pytest tests/test_session_store_migrator.py tests/test_session_store.py -q
```

Expected: FAIL because `SessionStore.load()` still upgrades only to v2 and keeps `messages`.

- [ ] **Step 3: Replace session migration implementation**

Edit `pico/session_store.py` with these definitions:

```python
from .session_records import (
    SESSION_SCHEMA_VERSION,
    normalize_records,
    records_from_history,
    records_from_messages,
)
```

Replace `_migrate_v1_to_v2` and `_write_backup` with:

```python
def _session_version(session: dict) -> int:
    try:
        return int(session.get("schema_version", 1))
    except (TypeError, ValueError):
        return 1


def _normalize_non_transcript_state(session: dict) -> dict:
    session.setdefault("created_at", "")
    session.setdefault("workspace_root", "")
    session.setdefault("working_memory", {"task_summary": "", "recent_files": []})
    session.setdefault("recently_recalled", [])
    memory = session.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    file_summaries = memory.get("file_summaries")
    if not isinstance(file_summaries, dict):
        file_summaries = {}
    session["memory"] = {"file_summaries": file_summaries}
    checkpoints = session.get("checkpoints")
    if not isinstance(checkpoints, dict):
        checkpoints = {}
    checkpoints.setdefault("current_id", "")
    checkpoints.setdefault("items", {})
    session["checkpoints"] = checkpoints
    session["runtime_identity"] = dict(session.get("runtime_identity") or {})
    session["resume_state"] = dict(session.get("resume_state") or {})
    recovery = session.get("recovery")
    if not isinstance(recovery, dict):
        recovery = {}
    recovery.setdefault("current_checkpoint_id", "")
    session["recovery"] = recovery
    return session


def migrate_session_to_v3(session: dict) -> dict:
    version = _session_version(session)
    if version >= SESSION_SCHEMA_VERSION:
        session["records"] = normalize_records(session.get("records", []))
    elif "messages" in session:
        session["records"] = records_from_messages(session.get("messages", []))
    else:
        session["records"] = records_from_history(session.get("history", []))
    session.pop("history", None)
    session.pop("messages", None)
    session["schema_version"] = SESSION_SCHEMA_VERSION
    return _normalize_non_transcript_state(session)


def _write_backup(session_path, raw_bytes, session_id, version):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    (backup_dir / f"{session_id}.v{version}.{ts}.json").write_bytes(raw_bytes)
```

Update `SessionStore.save()` before redaction:

```python
    def save(self, session):
        path = self.path(session["id"])
        if _session_version(session) >= SESSION_SCHEMA_VERSION:
            session = migrate_session_to_v3(dict(session))
        payload = self._redactor(session)
```

Update `SessionStore.load()`:

```python
    def load(self, session_id):
        p = self.path(session_id)
        raw = p.read_bytes()
        session = json.loads(raw.decode("utf-8"))
        version = _session_version(session)
        if version < SESSION_SCHEMA_VERSION:
            _write_backup(p, raw, session_id, version)
            session = migrate_session_to_v3(session)
            p.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
        else:
            session = migrate_session_to_v3(session)
        return session
```

- [ ] **Step 4: Run session tests to verify they pass**

Run:

```bash
uv run pytest tests/test_session_store_migrator.py tests/test_session_store.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add pico/session_store.py tests/test_session_store_migrator.py tests/test_session_store.py
git commit -m "feat: migrate sessions to records schema"
```

Expected: commit succeeds.

---

### Task 3: Move Runtime Session API To Records

**Files:**
- Modify: `pico/runtime.py`
- Modify: `tests/memory/test_runtime_wiring.py`
- Create: `tests/test_runtime_records.py`

- [ ] **Step 1: Add runtime record tests**

Create `tests/test_runtime_records.py`:

```python
from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def build_agent(tmp_path, outputs=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient(outputs or ["<final>done</final>"]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_new_runtime_session_uses_records_only_for_transcript(tmp_path):
    agent = build_agent(tmp_path)

    assert agent.session["schema_version"] == 3
    assert agent.session["records"] == []
    assert "history" not in agent.session
    assert "messages" not in agent.session
    assert set(agent.session["memory"]) == {"file_summaries"}
    assert "checkpoints" in agent.session
    assert "runtime_identity" in agent.session
    assert "resume_state" in agent.session


def test_append_record_persists_redacted_record(tmp_path):
    agent = build_agent(tmp_path)

    record = agent.append_record("user", {"text": "hello"})

    assert record["kind"] == "user"
    assert agent.session["records"][-1]["content"]["text"] == "hello"
    assert "records" in agent.session_path.read_text(encoding="utf-8")


def test_history_text_projects_from_records(tmp_path):
    agent = build_agent(tmp_path)
    agent.append_record("user", {"text": "inspect"}, created_at="1")
    agent.append_record("assistant", {"text": "checking"}, created_at="2")

    text = agent.history_text()

    assert "[user] inspect" in text
    assert "[assistant] checking" in text


def test_repeated_tool_call_uses_records(tmp_path):
    agent = build_agent(tmp_path)
    agent.append_record("tool_call", {"name": "list_files", "input": {}}, created_at="1")
    agent.append_record("tool_result", {"name": "list_files", "content": "(empty)"}, created_at="2")
    agent.append_record("tool_call", {"name": "list_files", "input": {}}, created_at="3")
    agent.append_record("tool_result", {"name": "list_files", "content": "(empty)"}, created_at="4")

    assert agent.repeated_tool_call("list_files", {}) is True


def test_reset_clears_records_and_keeps_non_transcript_state(tmp_path):
    agent = build_agent(tmp_path)
    agent.append_record("user", {"text": "hello"})
    agent.memory.set_task_summary("Task")
    agent._sync_working_memory()
    agent.update_memory_after_tool("read_file", {"path": "README.md"}, "demo\n")

    agent.reset()

    assert agent.session["records"] == []
    assert "history" not in agent.session
    assert "messages" not in agent.session
    assert agent.session["working_memory"] == {"task_summary": "", "recent_files": []}
    assert agent.session["memory"] == {"file_summaries": {}}
```

Edit `tests/memory/test_runtime_wiring.py`:

```python
def test_reset_clears_records_working_memory_and_keeps_narrow_memory_shape(tmp_path, monkeypatch):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = _build_agent(tmp_path, monkeypatch)
    agent.append_record("user", {"text": "hello"})
    agent.memory.set_task_summary("Task")
    agent._sync_working_memory()
    agent.update_memory_after_tool("read_file", {"path": "sample.txt"}, "alpha\nbeta\n")

    agent.reset()

    assert agent.session["records"] == []
    assert "history" not in agent.session
    assert "messages" not in agent.session
    assert type(agent.memory).__name__ == "WorkingMemory"
    assert agent.session["working_memory"] == {"task_summary": "", "recent_files": []}
    assert agent.session["memory"] == {"file_summaries": {}}
```

- [ ] **Step 2: Run runtime record tests to verify they fail**

Run:

```bash
uv run pytest tests/test_runtime_records.py tests/memory/test_runtime_wiring.py::test_reset_clears_records_working_memory_and_keeps_narrow_memory_shape -q
```

Expected: FAIL because `Pico` still initializes `history/messages` and has no `append_record()`.

- [ ] **Step 3: Update runtime session shape and record API**

Edit imports in `pico/runtime.py`:

```python
from .session_records import (
    SESSION_SCHEMA_VERSION,
    new_session_record,
    records_to_history,
    recent_tool_call_matches,
)
from .session_store import SessionStore, migrate_session_to_v3
```

Replace the default session dict in `Pico.__init__` with:

```python
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "schema_version": SESSION_SCHEMA_VERSION,
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "records": [],
            "recovery": {"current_checkpoint_id": ""},
            "checkpoints": {"current_id": "", "items": {}},
            "runtime_identity": {},
            "resume_state": {},
            "working_memory": {"task_summary": "", "recent_files": []},
            "memory": {"file_summaries": {}},
            "recently_recalled": [],
        }
```

Replace `_ensure_session_shape()` with:

```python
    def _ensure_session_shape(self):
        self.session = migrate_session_to_v3(dict(self.session))
        if not isinstance(self.session.get("recently_recalled"), list):
            self.session["recently_recalled"] = []
        existing_memory = self.session.get("memory")
        if not isinstance(existing_memory, dict):
            existing_memory = {}
        working_source = self.session.get("working_memory") or existing_memory or {}
        self.session["working_memory"] = WorkingMemory.from_dict(working_source, workspace_root=self.root).to_dict()
        self.session["memory"] = {
            "file_summaries": memorylib.normalize_file_summaries_dict(
                existing_memory.get("file_summaries", {}),
                workspace_root=self.root,
            )
        }
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}
        recovery = self.session.setdefault("recovery", {})
        if not isinstance(recovery, dict):
            recovery = {}
            self.session["recovery"] = recovery
        recovery.setdefault("current_checkpoint_id", "")
```

Replace `record()` and `record_message()` with:

```python
    def append_record(self, kind, content, *, created_at=None, run_id=None, task_id=None, meta=None):
        record = new_session_record(
            kind,
            self.redact_artifact(content),
            created_at=created_at,
            run_id=run_id,
            task_id=task_id,
            meta=self.redact_artifact(meta or {}),
        )
        self.session["records"].append(record)
        self.session_path = self.session_store.save(self.session)
        return record
```

Replace `history_text()` with:

```python
    def history_text(self):
        history = records_to_history(self.session.get("records", []))
        if not history:
            return "- empty"
        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)
            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")
        return clip("\n".join(lines), MAX_HISTORY)
```

Replace `repeated_tool_call()` with:

```python
    def repeated_tool_call(self, name, args):
        return recent_tool_call_matches(self.session.get("records", []), name, args)
```

Replace `reset()` with:

```python
    def reset(self):
        self.session["records"] = []
        self.memory = WorkingMemory(workspace_root=self.root)
        self._sync_working_memory()
        self.session["memory"] = {"file_summaries": {}}
        self.session_store.save(self.session)
```

- [ ] **Step 4: Run runtime record tests to verify they pass**

Run:

```bash
uv run pytest tests/test_runtime_records.py tests/memory/test_runtime_wiring.py -q
```

Expected: PASS for updated runtime wiring tests.

- [ ] **Step 5: Commit**

Run:

```bash
git add pico/runtime.py tests/test_runtime_records.py tests/memory/test_runtime_wiring.py
git commit -m "feat: store runtime transcript as records"
```

Expected: commit succeeds.

---

### Task 4: Add ContextManager.build_request And Remove build_v2

**Files:**
- Modify: `pico/context_manager.py`
- Delete: `tests/test_context_manager_v2.py`
- Create: `tests/test_context_manager_request.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_runtime_report.py`

- [ ] **Step 1: Add build_request tests**

Create `tests/test_context_manager_request.py`:

```python
from unittest.mock import MagicMock

from pico.context_manager import ContextManager
from pico.model_request import ModelRequest
from pico.session_records import new_session_record


def _make_agent():
    agent = MagicMock()
    agent.prefix = "SYSTEM_CORE_TEXT"
    agent.tools = {
        "read_file": {
            "schema": {"path": "str"},
            "risky": False,
            "description": "Read a file.",
        },
        "write_file": {
            "schema": {"path": "str", "content": "str"},
            "risky": True,
            "description": "Write a file.",
        },
    }
    agent.session = {"records": []}
    agent.workspace = MagicMock()
    agent.workspace.volatile_text = MagicMock(return_value="<workspace_state>clean</workspace_state>")
    agent.render_checkpoint_text = MagicMock(return_value="Task checkpoint:\n- Resume status: full-valid")
    agent.feature_enabled = MagicMock(return_value=True)
    agent.memory_store = None
    agent.repo_map = None
    agent.model_client = MagicMock(count_tokens=lambda text: len(text) // 4)
    return agent


def test_build_request_returns_model_request_with_context_and_tools():
    agent = _make_agent()
    records = [
        new_session_record("user", {"text": "hello"}, created_at="1"),
        new_session_record("assistant", {"text": "hi there"}, created_at="2"),
    ]

    request = ContextManager(agent).build_request("current input", records)

    assert isinstance(request, ModelRequest)
    assert request.system[0]["type"] == "text"
    assert "SYSTEM_CORE_TEXT" in request.system[0]["text"]
    assert request.system[0]["cache_control"] == {"type": "ephemeral"}
    assert any("workspace_state" in block["text"] for block in request.system)
    assert any("Task checkpoint" in block["text"] for block in request.system)
    assert request.messages[-1] == {"role": "user", "content": "current input"}
    assert request.metadata["current_request"]["text"] == "current input"
    tools_by_name = {tool["name"]: tool for tool in request.tools}
    assert "read_file" in tools_by_name
    assert "approval" in tools_by_name["write_file"]["description"].lower()


def test_build_request_projects_tool_records_to_model_messages():
    agent = _make_agent()
    records = [
        new_session_record("user", {"text": "read"}, created_at="1"),
        new_session_record(
            "tool_call",
            {"tool_call_id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
            created_at="2",
        ),
        new_session_record(
            "tool_result",
            {"tool_call_id": "toolu_1", "name": "read_file", "content": "demo"},
            created_at="3",
        ),
    ]

    request = ContextManager(agent).build_request("continue", records)

    assert request.messages[1]["role"] == "assistant"
    assert request.messages[1]["content"][0]["type"] == "tool_use"
    assert request.messages[2]["role"] == "user"
    assert request.messages[2]["content"][0]["type"] == "tool_result"


def test_build_request_cache_breakpoint_targets_previous_message():
    agent = _make_agent()
    records = [
        new_session_record("user", {"text": "one"}, created_at="1"),
        new_session_record("assistant", {"text": "two"}, created_at="2"),
    ]

    request = ContextManager(agent).build_request("three", records)

    assert request.cache_control_breakpoints == [len(request.messages) - 2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_context_manager_request.py -q
```

Expected: FAIL because `ContextManager.build_request` does not exist.

- [ ] **Step 3: Refactor ContextManager around records**

Edit imports in `pico/context_manager.py`:

```python
from .model_request import ModelRequest
from .session_records import records_to_history, records_to_model_messages
```

Change `build()` signature and history source:

```python
    def build(self, user_message, records=None):
        rendered, metadata = self._build_rendered_context(user_message, records=records)
        return self._assemble_prompt(rendered), metadata
```

Move the existing body of `build()` after `user_message = str(user_message)` into a new helper that returns the rendered sections and metadata instead of assembling the final prompt itself:

```python
    def _build_rendered_context(self, user_message, records=None):
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        refresher = self._get_refresher()
        memory_index_text = ""
        project_structure_text = ""
        if refresher is not None:
            snap = refresher.refresh_if_stale()
            memory_index_text = snap.memory_index_text
            project_structure_text = snap.project_structure_text
        base_prefix = str(getattr(self.agent, "prefix", ""))
        composed_prefix_parts = [base_prefix, MEMORY_USAGE_GUIDANCE, MEMORY_READING_GUIDANCE]
        if project_structure_text:
            composed_prefix_parts.append(project_structure_text)
        if memory_index_text:
            composed_prefix_parts.append(memory_index_text)
        workspace_state_text = ""
        if hasattr(self.agent, "workspace") and hasattr(self.agent.workspace, "volatile_text"):
            workspace_state_text = str(self.agent.workspace.volatile_text() or "").strip()
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        section_texts = {
            "prefix": "\n\n".join(part for part in composed_prefix_parts if part),
            "history": {
                "workspace_state": workspace_state_text,
                "checkpoint_text": checkpoint_text,
            },
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, records=records)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                user_message=user_message,
                section_texts=section_texts,
                base_prefix=base_prefix,
            )
            return rendered, metadata

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(section_texts, budgets, records=records)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, records=records)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break
        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            user_message=user_message,
            section_texts=section_texts,
            base_prefix=base_prefix,
        )
        return rendered, metadata
```

Update calls inside `build()` that render history so they pass `records`:

```python
            rendered = self._render_sections_without_reduction(section_texts, records=records)
```

```python
        rendered = self._render_sections(section_texts, budgets, records=records)
```

Update helper signatures:

```python
    def _render_sections_without_reduction(self, section_texts, records=None):
        history = records_to_history(records if records is not None else getattr(self.agent, "session", {}).get("records", []))
```

```python
    def _render_sections(self, section_texts, budgets, records=None):
```

Inside `_render_sections()`, update the history branch:

```python
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0), section_texts["history"], records=records)
```

```python
    def _render_history_section(self, budget, history_head="", records=None):
        history = records_to_history(records if records is not None else getattr(self.agent, "session", {}).get("records", []))
```

Add `build_request()`:

```python
    def build_request(self, user_message, records=None):
        records = list(records if records is not None else getattr(self.agent, "session", {}).get("records", []))
        older_records, recent_records = _split_records_for_request(records)
        rendered, metadata = self._build_rendered_context(user_message, records=older_records)
        system_cache_key = metadata.get("prompt_cache_key")
        system = [
            {
                "type": "text",
                "text": rendered["prefix"].rendered,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        volatile_context = rendered["history"].rendered
        if volatile_context:
            system.append({"type": "text", "text": volatile_context})

        messages = records_to_model_messages(recent_records)
        if not _last_message_is_user_text(messages, user_message):
            messages.append({"role": "user", "content": str(user_message)})
        breakpoints = [len(messages) - 2] if len(messages) >= 2 else []
        metadata = dict(metadata)
        metadata.update(
            {
                "messages_count": len(messages),
                "cache_control_breakpoints": list(breakpoints),
                "system_cache_key": system_cache_key,
            }
        )
        return ModelRequest(
            system=system,
            tools=_build_tools_list(getattr(self.agent, "tools", {}) or {}),
            messages=messages,
            cache_control_breakpoints=breakpoints,
            metadata=metadata,
        )
```

Add the helper near the other module-level helpers:

```python
def _split_records_for_request(records, recent_window=8):
    records = list(records or [])
    recent_window = max(0, int(recent_window))
    if recent_window == 0 or len(records) <= recent_window:
        return [], records
    return records[:-recent_window], records[-recent_window:]


def _last_message_is_user_text(messages, user_message):
    if not messages:
        return False
    last = messages[-1]
    return last.get("role") == "user" and last.get("content") == str(user_message)
```

Delete `build_v2()` from `ContextManager`.

- [ ] **Step 4: Update prompt metadata callers**

In `pico/runtime.py`, change `_build_prompt_and_metadata()`:

```python
        prompt, metadata = self.context_manager.build(user_message, records=self.session.get("records", []))
```

In `tests/test_context_manager.py` and `tests/test_runtime_report.py`, replace `agent.record({...})` setup calls with `agent.append_record(...)`. Example:

```python
agent.append_record("user", {"text": "old request"}, created_at="2026-04-07T09:59:00+00:00")
agent.append_record("assistant", {"text": "old answer"}, created_at="2026-04-07T10:00:30+00:00")
```

For old tool history setup, use:

```python
agent.append_record(
    "tool_call",
    {"tool_call_id": "toolu_test", "name": "read_file", "input": {"path": "sample.txt"}},
    created_at="2026-04-07T10:00:00+00:00",
)
agent.append_record(
    "tool_result",
    {"tool_call_id": "toolu_test", "name": "read_file", "content": "alpha\nbeta"},
    created_at="2026-04-07T10:00:01+00:00",
)
```

- [ ] **Step 5: Run context tests**

Run:

```bash
uv run pytest tests/test_context_manager_request.py tests/test_context_manager.py tests/test_runtime_report.py -q
```

Expected: PASS after replacing old setup calls.

- [ ] **Step 6: Delete v2 context test**

Run:

```bash
git rm tests/test_context_manager_v2.py
uv run pytest tests/test_context_manager_request.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add pico/context_manager.py pico/runtime.py tests/test_context_manager_request.py tests/test_context_manager.py tests/test_runtime_report.py
git add -u tests/test_context_manager_v2.py
git commit -m "feat: build model requests from session records"
```

Expected: commit succeeds.

---

### Task 5: Replace complete_v2 And FallbackAdapter With complete_request

**Files:**
- Create: `pico/providers/text_protocol.py`
- Modify: `pico/providers/anthropic_compatible.py`
- Modify: `pico/providers/openai_compatible.py`
- Modify: `pico/providers/ollama.py`
- Modify: `pico/providers/clients.py`
- Modify: `pico/runtime.py`
- Delete: `pico/providers/fallback_adapter.py`
- Delete: `tests/test_provider_fallback.py`
- Delete: `tests/test_provider_anthropic_v2.py`
- Create: `tests/test_provider_text_protocol.py`
- Create: `tests/test_provider_anthropic_request.py`
- Modify: `tests/test_agent_loop_e2e_v2.py`

- [ ] **Step 1: Add provider request tests**

Create `tests/test_provider_text_protocol.py`:

```python
from pico.model_request import ModelRequest
from pico.providers.response import StopReason
from pico.providers.text_protocol import flatten_model_request, response_from_text_protocol


def test_flatten_model_request_includes_system_tools_and_messages():
    request = ModelRequest(
        system=[{"type": "text", "text": "SYSTEM_CORE"}],
        tools=[{"name": "read_file", "description": "Read", "input_schema": {"properties": {"path": {"type": "string"}}}}],
        messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "body"}],
            },
        ],
    )

    prompt = flatten_model_request(request)

    assert "SYSTEM_CORE" in prompt
    assert "read_file" in prompt
    assert "hi" in prompt
    assert "toolu_1" in prompt
    assert "body" in prompt


def test_response_from_text_protocol_final_and_tool():
    final = response_from_text_protocol("<final>done</final>", usage={"input_tokens": 1})
    tool = response_from_text_protocol('<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>')

    assert final.stop_reason == StopReason.END_TURN
    assert final.content == [{"type": "text", "text": "done"}]
    assert final.usage == {"input_tokens": 1}
    assert tool.stop_reason == StopReason.TOOL_USE
    assert tool.content[0]["name"] == "read_file"
    assert tool.content[0]["input"] == {"path": "a.py"}
```

Create `tests/test_provider_anthropic_request.py` by renaming the old v2 test concepts:

```python
import json
from unittest.mock import MagicMock, patch

from pico.model_request import ModelRequest
from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
from pico.providers.response import Response, StopReason


def _mock_urlopen(response_body):
    mocked = MagicMock()
    mocked.__enter__.return_value = MagicMock(read=lambda: json.dumps(response_body).encode("utf-8"))
    mocked.__exit__.return_value = False
    return mocked


def _make_client():
    return AnthropicCompatibleModelClient(
        model="claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com",
        api_key="test-key",
        temperature=0.0,
        timeout=30,
    )


def test_complete_request_payload_shape_and_cache_control():
    client = _make_client()
    request = ModelRequest(
        system=[{"type": "text", "text": "SYSTEM_CORE", "cache_control": {"type": "ephemeral"}}],
        tools=[{"name": "read_file", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
        messages=[{"role": "user", "content": "hi"}],
        cache_control_breakpoints=[],
    )
    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5},
            }
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        response = client.complete_request(request, max_tokens=100)

    assert captured_payload["data"]["system"] == request.system
    assert captured_payload["data"]["tools"] == request.tools
    assert captured_payload["data"]["messages"] == request.messages
    assert isinstance(response, Response)
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == [{"type": "text", "text": "ok"}]
    assert response.usage["cache_creation_input_tokens"] == 5


def test_complete_request_cache_breakpoint_on_message():
    client = _make_client()
    request = ModelRequest(
        system=[{"type": "text", "text": "sys"}],
        tools=[],
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
        cache_control_breakpoints=[1],
    )
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return _mock_urlopen({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_request(request, max_tokens=10)

    assert captured["data"]["messages"][1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
```

- [ ] **Step 2: Run provider tests to verify they fail**

Run:

```bash
uv run pytest tests/test_provider_text_protocol.py tests/test_provider_anthropic_request.py -q
```

Expected: FAIL because `pico.providers.text_protocol` and `complete_request()` do not exist.

- [ ] **Step 3: Add text protocol helper**

Create `pico/providers/text_protocol.py`:

```python
"""Prompt-string provider helpers for ModelRequest."""

from __future__ import annotations

import json
import uuid

from pico.model_request import ModelRequest
from pico.model_output_parser import parse_model_output
from pico.providers.response import Response, StopReason


def flatten_model_request(request: ModelRequest) -> str:
    return "\n\n".join(
        part
        for part in (
            _flatten_system(request.system),
            _flatten_tools(request.tools),
            _flatten_messages(request.messages),
        )
        if part
    )


def response_from_text_protocol(raw: str, usage: dict | None = None) -> Response:
    usage = dict(usage or {})
    kind, payload = parse_model_output(raw)
    if kind == "tool":
        return Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                {
                    "type": "tool_use",
                    "id": f"toolu_local_{uuid.uuid4().hex[:12]}",
                    "name": payload["name"],
                    "input": dict(payload.get("args", {})),
                }
            ],
            usage=usage,
        )
    if kind == "final":
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": payload}],
            usage=usage,
        )
    return Response(
        stop_reason=StopReason.STOP_SEQUENCE,
        content=[{"type": "text", "text": str(payload)}],
        usage=usage,
    )


def _flatten_system(system: list[dict]) -> str:
    parts = []
    for block in system:
        text = block.get("text", "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _flatten_tools(tools: list[dict]) -> str:
    if not tools:
        return ""
    lines = ["Available tools:"]
    for tool in tools:
        schema = tool.get("input_schema", {}).get("properties", {})
        fields = ", ".join(f"{key}: {value.get('type', 'any')}" for key, value in schema.items())
        lines.append(f"- {tool['name']}({fields}): {tool.get('description', '')}")
    return "\n".join(lines)


def _flatten_messages(messages: list[dict]) -> str:
    lines = ["Transcript:"]
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                lines.append(f"[{role}] {block.get('text', '')}")
            elif block_type == "tool_use":
                tool_id = block.get("id", "")
                id_part = f" id={tool_id}" if tool_id else ""
                lines.append(f"[{role}:tool_use{id_part}] {block['name']}({json.dumps(block.get('input', {}), sort_keys=True)})")
            elif block_type == "tool_result":
                tool_id = block.get("tool_use_id", "")
                id_part = f" id={tool_id}" if tool_id else ""
                lines.append(f"[{role}:tool_result{id_part}] {block.get('content', '')}")
    return "\n".join(lines)
```

- [ ] **Step 4: Add complete_request to providers**

In `pico/providers/anthropic_compatible.py`, replace `complete_v2(...)` with:

```python
    def complete_request(self, request, *, max_tokens):
        from .response import Response, StopReason

        prepared_messages = []
        breakpoints = set(request.cache_control_breakpoints or [])
        for idx, msg in enumerate(request.messages):
            if idx in breakpoints:
                content = msg["content"]
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                else:
                    blocks = list(content)
                    if blocks:
                        last = dict(blocks[-1])
                        last["cache_control"] = {"type": "ephemeral"}
                        blocks[-1] = last
                prepared_messages.append({"role": msg["role"], "content": blocks})
            else:
                prepared_messages.append({"role": msg["role"], "content": msg["content"]})

        payload = {
            "model": self.model,
            "system": request.system,
            "tools": request.tools,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if not request.tools:
            payload.pop("tools")

        req = urllib.request.Request(
            f"{self.base_url}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as raw:
            data = json.loads(raw.read().decode("utf-8"))

        stop_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.STOP_SEQUENCE,
        }
        stop_reason = stop_map.get(data.get("stop_reason", "end_turn"), StopReason.END_TURN)
        usage_details = _extract_anthropic_usage_cache_details(data)
        self.last_completion_metadata = usage_details
        return Response(stop_reason=stop_reason, content=list(data.get("content") or []), usage=usage_details)
```

In `pico/providers/clients.py`, add imports:

```python
from .text_protocol import flatten_model_request, response_from_text_protocol
```

Add to `FakeModelClient`:

```python
    def complete_request(self, request, *, max_tokens):
        prompt = flatten_model_request(request)
        raw = self.complete(prompt, max_tokens)
        return response_from_text_protocol(raw, usage=self.last_completion_metadata)
```

In `pico/providers/ollama.py`, add imports:

```python
from .text_protocol import flatten_model_request, response_from_text_protocol
```

Add to `OllamaModelClient`:

```python
    def complete_request(self, request, *, max_tokens):
        raw = self.complete(flatten_model_request(request), max_tokens)
        return response_from_text_protocol(raw, usage=self.last_completion_metadata)
```

In `pico/providers/openai_compatible.py`, add imports:

```python
from .text_protocol import flatten_model_request, response_from_text_protocol
```

Add to `OpenAICompatibleModelClient`:

```python
    def complete_request(self, request, *, max_tokens):
        raw = self.complete(
            flatten_model_request(request),
            max_tokens,
            prompt_cache_key=request.metadata.get("prompt_cache_key"),
            prompt_cache_retention=request.metadata.get("prompt_cache_retention"),
        )
        return response_from_text_protocol(raw, usage=self.last_completion_metadata)
```

- [ ] **Step 5: Stop runtime auto-wrapping providers**

In `pico/runtime.py`, remove the `FallbackAdapter` wrapping block and replace it with:

```python
        if not hasattr(model_client, "complete_request"):
            raise TypeError("model_client must expose complete_request(request, *, max_tokens)")
```

Update `pico/checkpoint.py` comment and provider identity logic:

```python
def current_runtime_identity(agent):
    model_client = agent.model_client
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.root),
        "model": str(getattr(model_client, "model", "")),
        "model_client": model_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_new_tokens": int(agent.max_new_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(
            getattr(agent, "prefix_state", None),
            "workspace_fingerprint",
            agent.workspace.fingerprint(),
        ),
        "tool_signature": agent.tool_signature(),
    }
```

- [ ] **Step 6: Run provider tests**

Run:

```bash
uv run pytest tests/test_provider_text_protocol.py tests/test_provider_anthropic_request.py tests/test_provider_clients.py tests/test_provider_response.py -q
```

Expected: PASS.

- [ ] **Step 7: Remove fallback files and v2 provider tests**

Run:

```bash
git rm pico/providers/fallback_adapter.py tests/test_provider_fallback.py tests/test_provider_anthropic_v2.py
uv run pytest tests/test_provider_text_protocol.py tests/test_provider_anthropic_request.py -q
```

Expected: PASS and `rg -n "FallbackAdapter|complete_v2" pico tests -g '*.py'` shows only removed-file references in git diff, then no live matches after deletion.

- [ ] **Step 8: Commit**

Run:

```bash
git add pico/providers/text_protocol.py pico/providers/anthropic_compatible.py pico/providers/openai_compatible.py pico/providers/ollama.py pico/providers/clients.py pico/runtime.py pico/checkpoint.py tests/test_provider_text_protocol.py tests/test_provider_anthropic_request.py tests/test_agent_loop_e2e_v2.py
git add -u pico/providers/fallback_adapter.py tests/test_provider_fallback.py tests/test_provider_anthropic_v2.py
git commit -m "feat: complete model requests without fallback adapter"
```

Expected: commit succeeds.

---

### Task 6: Add ToolCall And execute_call

**Files:**
- Modify: `pico/tool_executor.py`
- Modify: `tests/test_tool_executor.py`

- [ ] **Step 1: Add ToolCall tests**

Append to `tests/test_tool_executor.py`:

```python
from pico.tool_executor import ToolCall


def test_execute_call_delegates_to_existing_tool_path(tmp_path):
    agent = build_agent(tmp_path)
    call = ToolCall(
        id="toolu_1",
        name="list_files",
        input={},
        source_record_id="rec_1",
        run_id="run_1",
        task_id="task_1",
    )

    result = agent.tool_executor.execute_call(call)

    assert result.metadata["tool_status"] == "ok"
    assert "[F] README.md" in result.content
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_tool_executor.py::test_execute_call_delegates_to_existing_tool_path -q
```

Expected: FAIL because `ToolCall` or `execute_call` does not exist.

- [ ] **Step 3: Add ToolCall dataclass and execute_call**

At the top of `pico/tool_executor.py`, update imports:

```python
from dataclasses import dataclass
```

Add above `ToolExecutionResult`:

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict
    source_record_id: str
    run_id: str
    task_id: str
```

Add to `ToolExecutor`:

```python
    def execute_call(self, call: ToolCall):
        return self.execute(call.name, dict(call.input or {}))
```

- [ ] **Step 4: Run tool executor tests**

Run:

```bash
uv run pytest tests/test_tool_executor.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add pico/tool_executor.py tests/test_tool_executor.py
git commit -m "feat: execute structured tool calls"
```

Expected: commit succeeds.

---

### Task 7: Rewrite AgentLoop As Records Kernel

**Files:**
- Modify: `pico/agent_loop.py`
- Modify: `tests/test_agent_loop.py`
- Rename: `tests/test_agent_loop_e2e_v2.py` to `tests/test_agent_loop_records.py`
- Delete: `tests/test_agent_loop_v2_shape.py`
- Modify: `tests/test_pico.py`
- Modify: `tests/test_safety_invariants.py`
- Modify: `tests/test_allowed_tools.py`
- Modify: `tests/test_recovery_e2e.py`

- [ ] **Step 1: Replace agent loop tests with records assertions**

Rename `tests/test_agent_loop_e2e_v2.py` to `tests/test_agent_loop_records.py` and use this core test:

```python
from pico.providers.response import Response, StopReason


class _StubProvider:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_request(self, request, *, max_tokens):
        self.calls.append(request)
        return self.script.pop(0)


def test_end_to_end_tool_call_then_final_records_only(tmp_path):
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    provider = _StubProvider(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_a",
                        "name": "read_file",
                        "input": {"path": "README.md", "start": 1, "end": 1},
                    }
                ],
            ),
            Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}]),
        ]
    )
    pico = Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=3,
    )

    result = pico.ask("what's in readme?")

    assert result == "done"
    assert "history" not in pico.session
    assert "messages" not in pico.session
    assert [record["kind"] for record in pico.session["records"]] == ["user", "tool_call", "tool_result", "assistant"]
    assert provider.calls[0].messages[-1] == {"role": "user", "content": "what's in readme?"}
    assert provider.calls[1].messages[-1]["content"][0]["type"] == "tool_result"
```

Add a multiple-tool policy test:

```python
def test_multiple_tool_calls_record_runtime_notice_for_ignored_tools(tmp_path):
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    provider = _StubProvider(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {"type": "tool_use", "id": "toolu_a", "name": "read_file", "input": {"path": "README.md"}},
                    {"type": "tool_use", "id": "toolu_b", "name": "list_files", "input": {}},
                ],
            ),
            Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}]),
        ]
    )
    pico = Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=3,
    )

    assert pico.ask("inspect") == "done"

    notices = [record for record in pico.session["records"] if record["kind"] == "runtime_notice"]
    assert notices
    assert notices[0]["content"]["ignored_tool_call_count"] == 1
```

Delete `tests/test_agent_loop_v2_shape.py`.

- [ ] **Step 2: Run agent loop records tests to verify they fail**

Run:

```bash
uv run pytest tests/test_agent_loop_records.py -q
```

Expected: FAIL because `AgentLoop` still calls `build_v2/complete_v2` and writes `messages/history`.

- [ ] **Step 3: Replace message append helpers**

In `pico/agent_loop.py`, delete `_append_user_turn`, `_append_tool_use`, `_append_tool_result`, and `_append_assistant_text`.

Add:

```python
from .tool_executor import ToolCall
```

Add helpers:

```python
def _append_record(agent, task_state, kind, content, *, created_at=None, meta=None):
    return agent.append_record(
        kind,
        content,
        created_at=created_at,
        run_id=getattr(task_state, "run_id", None),
        task_id=getattr(task_state, "task_id", None),
        meta=meta or {},
    )


def _text_from_blocks(text_blocks):
    for block in text_blocks:
        candidate = str(block.get("text", "") or "").strip()
        if candidate:
            return candidate
    return ""
```

- [ ] **Step 4: Change run startup to append a user record**

In `AgentLoop.run()`, remove `_append_user_turn(...)` and `agent.record(...)`. After `task_state` is created, append the user record:

```python
        user_record = _append_record(agent, task_state, "user", {"text": user_message})
```

Because `task_state` is currently created after the old append calls, move `TaskState.create(...)` above the first record append.

- [ ] **Step 5: Change context and provider calls**

Replace:

```python
            request, v2_metadata = agent.context_manager.build_v2(user_message)
```

and the `complete_v2(...)` call with:

```python
            request = agent.context_manager.build_request(user_message, agent.session.get("records", []))
            prompt_metadata = dict(prompt_metadata)
            prompt_metadata.update(request.metadata)
            raw_response = agent.model_client.complete_request(
                request,
                max_tokens=agent.max_new_tokens,
            )
```

Keep the existing `_build_prompt_and_metadata(user_message)` call until report/checkpoint metadata parity is covered by tests. It now reads records through `ContextManager.build(...)` and no longer writes transcript state.

- [ ] **Step 6: Record tool calls and results**

Replace the `kind == "tool"` block with:

```python
            if kind == "tool":
                tool_block = tool_use_blocks[0]
                ignored_count = max(0, len(tool_use_blocks) - 1)
                name = str(tool_block.get("name", ""))
                args = dict(tool_block.get("input", {}) or {})
                tool_call_id = str(tool_block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}")
                tool_record = _append_record(
                    agent,
                    task_state,
                    "tool_call",
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "input": args,
                    },
                )
                if ignored_count:
                    _append_record(
                        agent,
                        task_state,
                        "runtime_notice",
                        {
                            "text": f"Provider returned {len(tool_use_blocks)} tool calls; Pico executed the first one.",
                            "ignored_tool_call_count": ignored_count,
                        },
                    )
                tool_started_at = time.monotonic()
                agent.emit_trace(
                    task_state,
                    "tool_started",
                    {
                        "name": name,
                        "args": args,
                        "tool_use_id": tool_call_id,
                    },
                )
                tool_result = agent.tool_executor.execute_call(
                    ToolCall(
                        id=tool_call_id,
                        name=name,
                        input=args,
                        source_record_id=tool_record["id"],
                        run_id=task_state.run_id,
                        task_id=task_state.task_id,
                    )
                )
                result = tool_result.content
                if tool_result.metadata.get("tool_status") != "rejected":
                    tool_steps += 1
                    task_state.record_tool(name)
                tool_change_id = tool_result.metadata.get("tool_change_id") or ""
                if tool_change_id:
                    run_tool_change_ids.append(tool_change_id)
                _append_record(
                    agent,
                    task_state,
                    "tool_result",
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "content": result,
                    },
                    meta=dict(tool_result.metadata or {}),
                )
```

Keep the existing trace, checkpoint, and verification logic after this block.

- [ ] **Step 7: Record retry, final, and terminal stop records**

In the retry path, replace `agent.record(...)` with:

```python
                _append_record(agent, task_state, "runtime_notice", {"text": retry_text})
```

In the final path, replace `_append_assistant_text(...)` and `agent.record(...)` with:

```python
            _append_record(agent, task_state, "assistant", {"text": final})
```

In the step-limit/retry-limit terminal path, replace `_append_assistant_text(...)` and `agent.record(...)` with the same assistant record append.

In the model error except block before `_finish_run(...)`, append:

```python
                _append_record(agent, task_state, "model_error", {"text": final})
```

- [ ] **Step 8: Add checkpoint and verification records**

In `_create_resume_checkpoint(...)`, after `checkpoint = agent.create_checkpoint(...)`, append:

```python
    agent.append_record(
        "checkpoint",
        {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "checkpoint_kind": "resume_summary",
            "trigger": trigger,
        },
        run_id=task_state.run_id,
        task_id=task_state.task_id,
    )
```

In `_emit_recovery_checkpoint_created(...)`, append:

```python
    agent.append_record(
        "recovery_checkpoint",
        {
            "checkpoint_id": recovery_checkpoint["checkpoint_id"],
            "checkpoint_type": "turn",
            "trigger": trigger,
        },
        run_id=task_state.run_id,
        task_id=task_state.task_id,
    )
```

In `_record_pending_verification_evidence(...)`, after `agent.record_verification_evidence(...)`, append:

```python
        agent.append_record(
            "verification",
            {
                "checkpoint_id": recovery_checkpoint["checkpoint_id"],
                "command": evidence["command"],
                "exit_code": evidence["exit_code"],
            },
            run_id=agent.current_task_state.run_id if agent.current_task_state else None,
            task_id=agent.current_task_state.task_id if agent.current_task_state else None,
        )
```

- [ ] **Step 9: Update tests that read history/messages**

Replace assertions such as:

```python
tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
```

with:

```python
tool_events = [record for record in agent.session["records"] if record["kind"] == "tool_result"]
```

Replace old assistant notice assertions with:

```python
notices = [record["content"]["text"] for record in agent.session["records"] if record["kind"] == "runtime_notice"]
```

Replace old resume assertions against `resumed.session["history"]` with:

```python
assert resumed.session["records"][0]["kind"] == "user"
assert resumed.session["records"][0]["content"]["text"] == "Start a session"
```

- [ ] **Step 10: Run agent/runtime/recovery tests**

Run:

```bash
uv run pytest tests/test_agent_loop.py tests/test_agent_loop_records.py tests/test_pico.py tests/test_safety_invariants.py tests/test_allowed_tools.py tests/test_recovery_e2e.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit**

Run:

```bash
git add pico/agent_loop.py tests/test_agent_loop.py tests/test_agent_loop_records.py tests/test_pico.py tests/test_safety_invariants.py tests/test_allowed_tools.py tests/test_recovery_e2e.py
git add -u tests/test_agent_loop_e2e_v2.py tests/test_agent_loop_v2_shape.py
git commit -m "feat: run agent loop on session records"
```

Expected: commit succeeds.

---

### Task 8: Update Reports, Benchmarks, And Evaluation Helpers

**Files:**
- Modify: `pico/evaluation/fixed_benchmark.py`
- Modify: `pico/evaluation/experiments_real.py`
- Modify: `pico/evaluation/experiments_synthetic.py`
- Modify: `pico/evaluation/experiments_recovery.py`
- Modify: `tests/test_runtime_report.py`
- Modify: `tests/test_metrics.py`
- Modify: `tests/test_memory_quality_benchmark.py`

- [ ] **Step 1: Search remaining transcript-field usage**

Run:

```bash
rg -n 'session\\["history"\\]|session\\["messages"\\]|agent\\.record\\(|record_message|build_v2|complete_v2|FallbackAdapter' pico tests -g '*.py'
```

Expected before edits: matches remain only in evaluation helpers and tests that seed transcript state.

- [ ] **Step 2: Replace benchmark transcript seeding**

In benchmark/evaluation files, replace:

```python
agent.record({"role": "user", "content": text, "created_at": created_at})
```

with:

```python
agent.append_record("user", {"text": text}, created_at=created_at)
```

Replace assistant records similarly:

```python
agent.append_record("assistant", {"text": text}, created_at=created_at)
```

Replace tool records:

```python
tool_call_id = f"toolu_bench_{index}"
agent.append_record(
    "tool_call",
    {"tool_call_id": tool_call_id, "name": name, "input": args},
    created_at=created_at,
)
agent.append_record(
    "tool_result",
    {"tool_call_id": tool_call_id, "name": name, "content": result},
    created_at=created_at,
)
```

Replace checks such as:

```python
initial_history_empty = len(agent.session["history"]) == 0
```

with:

```python
initial_history_empty = len(agent.session["records"]) == 0
```

- [ ] **Step 3: Update tests that inspect prompt-string provider prompts**

Where tests previously looked into `session["messages"]` or `FallbackAdapter` prompts, assert on `ModelRequest` or flattened prompt text through `FakeModelClient.prompts`. Example:

```python
assert agent.model_client.prompts
assert "Current user request:" not in agent.model_client.prompts[0]
assert "recall" in agent.model_client.prompts[0]
```

For native request-shape tests, use a stub provider with `complete_request()` and inspect captured `ModelRequest` objects.

- [ ] **Step 4: Run report and evaluation tests**

Run:

```bash
uv run pytest tests/test_runtime_report.py tests/test_metrics.py tests/test_memory_quality_benchmark.py -q
```

Expected: PASS.

- [ ] **Step 5: Verify no old runtime API remains**

Run:

```bash
rg -n 'session\\["history"\\]|session\\["messages"\\]|agent\\.record\\(|record_message|build_v2|complete_v2|FallbackAdapter' pico tests -g '*.py'
```

Expected: no output.

- [ ] **Step 6: Commit**

Run:

```bash
git add pico/evaluation/fixed_benchmark.py pico/evaluation/experiments_real.py pico/evaluation/experiments_synthetic.py pico/evaluation/experiments_recovery.py tests/test_runtime_report.py tests/test_metrics.py tests/test_memory_quality_benchmark.py
git commit -m "test: update reports and benchmarks for records"
```

Expected: commit succeeds.

---

### Task 9: Tighten Public Contract And Documentation Tests

**Files:**
- Modify: `tests/test_public_api_contract.py`
- Modify: `tests/test_provider_clients.py`
- Modify: `docs/superpowers/specs/2026-07-07-pico-ideal-agentloop-kernel-design.md`

- [ ] **Step 1: Add public contract assertions**

Append to `tests/test_public_api_contract.py`:

```python
def test_runtime_contract_has_records_request_and_no_v2_fallback_names():
    import pico.context_manager as context_manager
    import pico.providers.clients as provider_clients
    from pico.model_request import ModelRequest
    from pico.session_records import SessionRecord

    assert ModelRequest.__name__ == "ModelRequest"
    assert SessionRecord.__name__ == "SessionRecord"
    assert hasattr(context_manager.ContextManager, "build_request")
    assert not hasattr(context_manager.ContextManager, "build_v2")
    assert hasattr(provider_clients.FakeModelClient, "complete_request")
    assert not (Path("pico/providers") / "fallback_adapter.py").exists()
```

- [ ] **Step 2: Run public contract test to verify it fails if cleanup is incomplete**

Run:

```bash
uv run pytest tests/test_public_api_contract.py -q
```

Expected: PASS after Tasks 5-8. If it fails, remove the named old API from live code or update the assertion to target the real remaining name.

- [ ] **Step 3: Verify design spec is implementation-planned**

Confirm the status line in `docs/superpowers/specs/2026-07-07-pico-ideal-agentloop-kernel-design.md` is:

```markdown
Status: Implementation plan written
```

Keep the approval gate section unchanged; execution still requires choosing an implementation mode.

- [ ] **Step 4: Run public and provider contract tests**

Run:

```bash
uv run pytest tests/test_public_api_contract.py tests/test_provider_clients.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add tests/test_public_api_contract.py tests/test_provider_clients.py docs/superpowers/specs/2026-07-07-pico-ideal-agentloop-kernel-design.md
git commit -m "test: enforce records kernel contract"
```

Expected: commit succeeds.

---

### Task 10: Final Verification

**Files:**
- No source edits unless verification exposes a concrete failure.

- [ ] **Step 1: Run focused old-name scan**

Run:

```bash
rg -n 'history|messages|build_v2|complete_v2|FallbackAdapter|record_message' pico tests -g '*.py'
```

Expected: no matches for `build_v2`, `complete_v2`, `FallbackAdapter`, or `record_message`. Matches for the words `history` or `messages` are allowed only in generic prose, provider HTTP payload fields, or tests that intentionally assert provider API payload shape.

- [ ] **Step 2: Run focused kernel tests**

Run:

```bash
uv run pytest \
  tests/test_session_records.py \
  tests/test_session_store_migrator.py \
  tests/test_runtime_records.py \
  tests/test_context_manager_request.py \
  tests/test_provider_text_protocol.py \
  tests/test_provider_anthropic_request.py \
  tests/test_tool_executor.py \
  tests/test_agent_loop_records.py \
  tests/test_recovery_e2e.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run canonical repository check**

Run:

```bash
./scripts/check.sh
```

Expected: `uv run ruff check .` passes and `uv run pytest -q` passes.

- [ ] **Step 4: Inspect working tree**

Run:

```bash
git status --short
```

Expected: only intended tracked changes are present. Existing unrelated untracked files such as local planning scratch files remain untracked and unstaged.

- [ ] **Step 5: Final commit if verification forced fixups**

If Step 3 required a small fixup, commit that exact fix:

```bash
git add <changed-files-from-fixup>
git commit -m "fix: complete records kernel migration"
```

Expected: no fixup commit is needed when Tasks 1-9 already committed cleanly.

---

## Self-Review

**Spec coverage:** The plan maps the spec requirements to tasks: schema v3 migration in Task 2, records-only runtime in Task 3 and Task 7, `ModelRequest` and `build_request` in Task 1 and Task 4, `complete_request` providers in Task 5, `ToolCall` and `execute_call` in Task 6, trace/report/recovery preservation in Task 7 and Task 8, old API deletion in Task 5 and Task 8, and final gates in Task 10.

**Placeholder scan:** The plan avoids unresolved markers and gives concrete file paths, code snippets, commands, expected failures, expected passes, and commit commands for each task.

**Type consistency:** `ModelRequest`, `SessionRecord`, `ToolCall`, `ToolExecutionResult`, `Response`, and `StopReason` keep the names from the design spec. Provider entrypoints consistently use `complete_request(request, *, max_tokens)`. Context entrypoint consistently uses `build_request(user_message, records)`.
