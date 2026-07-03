# Retire v1 Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire v1 memory from Pico's runtime public type surface and prompt output while preserving v2 memory files, raw `file_summaries` compatibility, checkpoint resume behavior, and evaluator/metrics smoke coverage.
**Architecture:** Introduce `WorkingMemory` as the only `Pico.memory` type, sync it explicitly to `session["working_memory"]`, keep `session["memory"]` narrowed to `{"file_summaries": summaries}`, collapse prompt assembly to `prefix`, `history`, and `current_request`, and compute prompt cache keys from the final stable prefix.
**Tech Stack:** Python stdlib (`dataclasses`, `json`, `hashlib`, `pathlib`), existing Pico runtime/session stores, `pytest`, `uv`, and repository grep gates.

---

## Source Contract

Implement against `docs/superpowers/specs/2026-07-03-retire-v1-memory-design.md` rev 3. The following constraints are hard requirements:

- [ ] Do not delete `pico/features/memory.py` or `LayeredMemory`.
- [ ] Do not pop `session["memory"]`; narrow it to an internal `file_summaries` channel.
- [ ] Do not add `memory_state` to recoverable-editing checkpoint records.
- [ ] Do not change `pico/checkpoint_store.py`, `pico/recovery_checkpoint_writer.py`, or `pico/recovery_models.py`.
- [ ] Do not preserve v1 episodic notes by hiding them in another prompt path.
- [ ] Do not change `DEFAULT_FEATURE_FLAGS["relevant_memory"]` in this plan.
- [ ] Keep the two known pre-existing full-suite failures separate from this migration:
  `tests/test_allowed_tools.py::test_benchmark_evaluator_applies_allowed_tools_to_runtime_prompt` and
  `tests/test_evaluator.py::test_benchmark_verifier_runs_with_reproducibility_locale`.

## Task 1: Add `WorkingMemory`

Files:

- `pico/working_memory.py`
- `tests/test_working_memory.py`
- `tests/test_public_api_contract.py`

Steps:

- [ ] Add `tests/test_working_memory.py` with focused behavior coverage:

```python
from pico.working_memory import WorkingMemory


def test_defaults_are_empty():
    memory = WorkingMemory()

    assert memory.task_summary == ""
    assert memory.recent_files == []
    assert memory.to_dict() == {"task_summary": "", "recent_files": []}


def test_task_summary_is_clipped():
    memory = WorkingMemory()

    memory.set_task_summary("x" * 400)

    assert len(memory.task_summary) == 300


def test_recent_files_are_canonical_deduped_and_limited(tmp_path):
    for index in range(10):
        (tmp_path / f"file{index}.py").write_text("print('ok')\n", encoding="utf-8")
    memory = WorkingMemory(workspace_root=tmp_path)

    for index in range(10):
        memory.remember_file(tmp_path / f"file{index}.py")
    memory.remember_file("./file9.py")

    assert memory.recent_files[0] == "file9.py"
    assert len(memory.recent_files) == 8
    assert "file0.py" not in memory.recent_files


def test_from_dict_accepts_new_shape(tmp_path):
    memory = WorkingMemory.from_dict(
        {"task_summary": "ship it", "recent_files": ["src/app.py"]},
        workspace_root=tmp_path,
    )

    assert memory.task_summary == "ship it"
    assert memory.recent_files == ["src/app.py"]
    assert memory.to_dict() == {"task_summary": "ship it", "recent_files": ["src/app.py"]}


def test_from_dict_accepts_v1_nested_shape():
    memory = WorkingMemory.from_dict(
        {"working": {"task_summary": "legacy task", "recent_files": ["a.py"]}}
    )

    assert memory.task_summary == "legacy task"
    assert memory.recent_files == ["a.py"]


def test_from_dict_accepts_v1_flat_shape():
    memory = WorkingMemory.from_dict({"task": "legacy flat", "files": ["b.py"]})

    assert memory.task_summary == "legacy flat"
    assert memory.recent_files == ["b.py"]


def test_from_dict_ignores_invalid_input():
    assert WorkingMemory.from_dict(None).to_dict() == {"task_summary": "", "recent_files": []}
    assert WorkingMemory.from_dict(["bad"]).to_dict() == {"task_summary": "", "recent_files": []}


def test_v1_methods_are_not_exposed():
    memory = WorkingMemory()

    for name in (
        "append_note",
        "set_file_summary",
        "invalidate_file_summary",
        "invalidate_stale_file_summaries",
        "retrieval_candidates",
        "retrieval_view",
        "render_memory_text",
        "promote_durable",
    ):
        assert not hasattr(memory, name)
```

- [ ] Run the new tests and confirm they fail because `pico.working_memory` does not exist:

```bash
uv run pytest tests/test_working_memory.py -q
```

- [ ] Create `pico/working_memory.py` with the complete `WorkingMemory` implementation:

```python
"""Small runtime working-memory model for Pico sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .features.memory import canonicalize_path
from .workspace import clip


@dataclass
class WorkingMemory:
    task_summary: str = ""
    recent_files: list[str] = field(default_factory=list)
    workspace_root: str | Path | None = None

    TASK_SUMMARY_LIMIT = 300
    RECENT_FILES_LIMIT = 8

    def set_task_summary(self, summary: str) -> None:
        self.task_summary = clip(str(summary or "").strip(), self.TASK_SUMMARY_LIMIT)

    def canonical_path(self, path: str | Path) -> str:
        return canonicalize_path(path, self.workspace_root)

    def remember_file(self, path: str | Path) -> None:
        canonical = self.canonical_path(path)
        if not canonical:
            return
        self.recent_files = [item for item in self.recent_files if item != canonical]
        self.recent_files.insert(0, canonical)
        del self.recent_files[self.RECENT_FILES_LIMIT :]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_summary": self.task_summary,
            "recent_files": list(self.recent_files),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        workspace_root: str | Path | None = None,
    ) -> "WorkingMemory":
        memory = cls(workspace_root=workspace_root)
        if not isinstance(data, dict):
            return memory

        if isinstance(data.get("working"), dict):
            source = data["working"]
            task_summary = source.get("task_summary", "")
            recent_files = source.get("recent_files", [])
        elif "task_summary" in data or "recent_files" in data:
            task_summary = data.get("task_summary", "")
            recent_files = data.get("recent_files", [])
        else:
            task_summary = data.get("task", "")
            recent_files = data.get("files", [])

        memory.set_task_summary(task_summary)
        if not isinstance(recent_files, (list, tuple, set)):
            recent_files = [recent_files]
        for path in reversed(list(recent_files)):
            memory.remember_file(path)
        return memory
```

- [ ] Add a public API contract assertion that the runtime imports the new type:

```python
from pico.working_memory import WorkingMemory


def test_working_memory_is_public_import():
    assert WorkingMemory.__name__ == "WorkingMemory"
```

- [ ] Verify Task 1:

```bash
uv run pytest tests/test_working_memory.py tests/test_public_api_contract.py -q
```

## Task 2: Add Raw File-Summary Helpers

Files:

- `pico/features/memory.py`
- `tests/test_memory.py`

Steps:

- [ ] Add focused helper tests to `tests/test_memory.py`:

```python
from pico.features import memory as memorylib


def test_file_summary_dict_helpers_mutate_raw_summary_channel(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    summaries = {}

    returned = memorylib.set_file_summary_dict(
        summaries,
        "./sample.txt",
        "sample.txt: alpha",
        workspace_root=tmp_path,
    )

    assert returned is summaries
    assert summaries["sample.txt"]["summary"] == "sample.txt: alpha"
    assert summaries["sample.txt"]["freshness"]

    invalidated = memorylib.invalidate_file_summary_dict(
        summaries,
        tmp_path / "sample.txt",
        workspace_root=tmp_path,
    )

    assert invalidated is summaries
    assert summaries == {}


def test_normalize_file_summaries_dict_accepts_legacy_values(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")

    normalized = memorylib.normalize_file_summaries_dict(
        {
            str(tmp_path / "a.py"): {"summary": "absolute"},
            "b.py": "plain text",
            "": {"summary": "ignored"},
        },
        workspace_root=tmp_path,
    )

    assert normalized["a.py"]["summary"] == "absolute"
    assert normalized["b.py"]["summary"] == "plain text"
    assert "" not in normalized


def test_invalidate_stale_file_summaries_dict_removes_changed_entries(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\n", encoding="utf-8")
    summaries = {}
    memorylib.set_file_summary_dict(summaries, "sample.txt", "old", workspace_root=tmp_path)

    target.write_text("beta\n", encoding="utf-8")
    invalidated = memorylib.invalidate_stale_file_summaries_dict(summaries, workspace_root=tmp_path)

    assert invalidated == ["sample.txt"]
    assert summaries == {}
```

- [ ] Run the helper tests and confirm they fail on missing functions:

```bash
uv run pytest tests/test_memory.py -q
```

- [ ] Add these helpers below `file_freshness()` in `pico/features/memory.py`:

```python
def normalize_file_summaries_dict(summaries, workspace_root=None) -> dict:
    if not isinstance(summaries, dict):
        return {}
    normalized = {}
    for raw_path, value in summaries.items():
        path = canonicalize_path(raw_path, workspace_root)
        if not path:
            continue
        if isinstance(value, dict):
            summary = value.get("summary", "")
            created_at = value.get("created_at") or now()
            freshness = value.get("freshness")
        else:
            summary = value
            created_at = now()
            freshness = file_freshness(path, workspace_root)
        normalized[path] = {
            "summary": clip(str(summary or "").strip(), 500),
            "created_at": str(created_at),
            "freshness": freshness,
        }
    return normalized


def set_file_summary_dict(summaries, path, summary, workspace_root=None) -> dict:
    canonical = canonicalize_path(path, workspace_root)
    if not canonical:
        return summaries
    summaries[canonical] = {
        "summary": clip(str(summary or "").strip(), 500),
        "created_at": now(),
        "freshness": file_freshness(canonical, workspace_root),
    }
    return summaries


def invalidate_file_summary_dict(summaries, path, workspace_root=None) -> dict:
    canonical = canonicalize_path(path, workspace_root)
    summaries.pop(canonical, None)
    return summaries


def invalidate_stale_file_summaries_dict(summaries, workspace_root=None) -> list[str]:
    invalidated = []
    for path, entry in list(summaries.items()):
        expected = entry.get("freshness") if isinstance(entry, dict) else None
        current = file_freshness(path, workspace_root)
        if expected != current:
            summaries.pop(path, None)
            invalidated.append(path)
    return invalidated
```

- [ ] Verify Task 2:

```bash
uv run pytest tests/test_memory.py -q
```

## Task 3: Switch Runtime Session State to `WorkingMemory`

Files:

- `pico/runtime.py`
- `pico/agent_loop.py`
- `pico/tool_executor.py`
- `tests/memory/test_runtime_wiring.py`
- `tests/test_pico.py`

Steps:

- [ ] Add runtime wiring tests to `tests/memory/test_runtime_wiring.py`:

```python
def test_runtime_uses_working_memory_shape(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    legacy_session = {
        "id": "legacy",
        "created_at": "2026-07-03T00:00:00",
        "workspace_root": str(tmp_path),
        "history": [],
        "memory": {
            "working": {"task_summary": "legacy task", "recent_files": ["old.py"]},
            "episodic_notes": [{"text": "old note"}],
            "file_summaries": {"old.py": {"summary": "old summary", "freshness": None}},
            "task": "legacy task",
            "files": ["old.py"],
            "notes": ["old note"],
            "next_note_index": 1,
        },
    }

    agent = Pico(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        session=legacy_session,
        approval_policy="auto",
    )

    assert type(agent.memory).__name__ == "WorkingMemory"
    assert agent.memory.task_summary == "legacy task"
    assert agent.session["working_memory"] == {
        "task_summary": "legacy task",
        "recent_files": ["old.py"],
    }
    assert set(agent.session["memory"]) == {"file_summaries"}
    assert agent.session["memory"]["file_summaries"]["old.py"]["summary"] == "old summary"


def test_tool_memory_updates_working_memory_and_raw_file_summaries(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    agent.update_memory_after_tool(
        "read_file",
        {"path": "sample.txt"},
        "alpha\nbeta\n",
        {"ok": True},
    )

    assert agent.session["working_memory"]["recent_files"] == ["sample.txt"]
    assert "sample.txt" in agent.session["memory"]["file_summaries"]

    agent.update_memory_after_tool(
        "write_file",
        {"path": "sample.txt"},
        "wrote sample.txt",
        {"ok": True},
    )

    assert agent.session["working_memory"]["recent_files"] == ["sample.txt"]
    assert "sample.txt" not in agent.session["memory"]["file_summaries"]
```

- [ ] Update `tests/test_pico.py::test_agent_runs_tool_then_final` to assert the new state shape:

```python
assert "hello.txt" in agent.session["working_memory"]["recent_files"]
assert set(agent.session["memory"]) == {"file_summaries"}
```

- [ ] Update `tests/test_pico.py::test_agent_updates_task_summary_on_each_request`:

```python
assert agent.session["working_memory"]["task_summary"] == "First request"
assert agent.session["working_memory"]["task_summary"] == "Second request"
```

- [ ] Delete the v1 episodic behavior expected by `tests/test_pico.py::test_agent_only_stores_reusable_epistemic_notes`. Replace it with an assertion that tool reads do not write v1 notes:

```python
assert "episodic_notes" not in agent.session["memory"]
assert "notes" not in agent.session["memory"]
```

- [ ] Modify `pico/runtime.py` imports:

```python
from .working_memory import WorkingMemory
```

- [ ] Replace runtime initialization with explicit working-memory normalization:

```python
working_source = self.session.get("working_memory") or self.session.get("memory") or {}
self.memory = WorkingMemory.from_dict(working_source, workspace_root=self.root)
legacy_memory = self.session.get("memory", {})
legacy_summaries = legacy_memory.get("file_summaries", {}) if isinstance(legacy_memory, dict) else {}
normalized_summaries = memorylib.normalize_file_summaries_dict(
    legacy_summaries,
    workspace_root=self.root,
)
self.session["working_memory"] = self.memory.to_dict()
self.session["memory"] = {"file_summaries": normalized_summaries}
```

- [ ] Add the runtime sync helper:

```python
def _sync_working_memory(self):
    self.session["working_memory"] = self.memory.to_dict()
    return self.session["working_memory"]
```

- [ ] Change `memory_text()`:

```python
def memory_text(self):
    return json.dumps(self.memory.to_dict(), sort_keys=True)
```

- [ ] Change `invalidate_stale_memory()`:

```python
def invalidate_stale_memory(self):
    memory_state = self.session.setdefault("memory", {})
    summaries = memory_state.setdefault("file_summaries", {})
    return memorylib.invalidate_stale_file_summaries_dict(summaries, self.root)
```

- [ ] Change `update_memory_after_tool()` so it mutates `WorkingMemory` and raw summaries only:

```python
def update_memory_after_tool(self, name, args, result_text, metadata):
    if not self.feature_flags.get("memory", True):
        return
    path = args.get("path") if isinstance(args, dict) else None
    if name not in {"read_file", "write_file", "patch_file"} or not path:
        return
    canonical_path = self.memory.canonical_path(path)
    self.memory.remember_file(canonical_path)
    self._sync_working_memory()
    summaries = self.session.setdefault("memory", {}).setdefault("file_summaries", {})
    if name == "read_file":
        summary = f"{canonical_path}: {clip(result_text, 240)}"
        memorylib.set_file_summary_dict(summaries, canonical_path, summary, self.root)
    else:
        memorylib.invalidate_file_summary_dict(summaries, canonical_path, self.root)
```

- [ ] Delete `record_process_note_for_tool()` from `pico/runtime.py`.
- [ ] Delete the call to `agent.record_process_note_for_tool(name, metadata)` from `pico/tool_executor.py`.
- [ ] Add `agent._sync_working_memory()` immediately after `agent.memory.set_task_summary(user_message)` in `pico/agent_loop.py`.
- [ ] Update `spawn_delegate()`:

```python
child.memory.set_task_summary(task)
child._sync_working_memory()
```

- [ ] Update `reset()`:

```python
self.session["history"] = []
self.memory = WorkingMemory(workspace_root=self.root)
self._sync_working_memory()
self.session["memory"] = {"file_summaries": {}}
```

- [ ] Add `working_memory` to `build_report()`:

```python
"working_memory": self.memory.to_dict(),
```

- [ ] Verify Task 3:

```bash
uv run pytest tests/test_working_memory.py tests/memory/test_runtime_wiring.py tests/test_pico.py -q
rg -n "record_process_note_for_tool" pico/ tests/ --glob '*.py'
```

Expected for the `rg` command: no output.

## Task 4: Collapse Prompt Layout and Stable Cache Metadata

Files:

- `pico/context_manager.py`
- `pico/runtime.py`
- `tests/test_context_manager.py`
- `tests/memory/test_prompt_layout.py`

Steps:

- [ ] Add prompt layout assertions to `tests/memory/test_prompt_layout.py`:

```python
from pico.context_manager import SECTION_ORDER


def test_runtime_prompt_sections_exclude_v1_memory(tmp_path):
    (tmp_path / ".pico" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "notes" / "auth.md").write_text(
        "# Auth\n- ship clue\n",
        encoding="utf-8",
    )
    agent = _agent(tmp_path)

    prompt, metadata = agent._build_prompt_and_metadata("Inspect state")

    assert SECTION_ORDER == ("prefix", "history", "current_request")
    assert "Working memory:" not in prompt
    assert "Relevant memory:" not in prompt
    assert "<memory_index>" in prompt
    assert "memory" not in metadata["sections"]
    assert "relevant_memory" not in metadata["sections"]
    assert "relevant_memory" not in metadata
```

- [ ] Add cache-key assertions to `tests/test_context_manager.py`:

```python
def test_prompt_cache_key_tracks_stable_prefix_not_workspace_state(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])

    first_prompt, first_metadata = agent._build_prompt_and_metadata("first")
    agent.workspace.branch = "feature/cache-test"
    agent.workspace.status = " M README.md"
    agent.workspace.recent_commits = ["abc1234 volatile commit"]
    second_prompt, second_metadata = agent._build_prompt_and_metadata("second")

    assert first_prompt != second_prompt
    assert first_metadata["prompt_cache_key"] == second_metadata["prompt_cache_key"]
    assert first_metadata["base_prefix_hash"]
    assert first_metadata["stable_prefix_hash"]


def test_prompt_cache_key_changes_when_memory_index_changes(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])

    _, first_metadata = agent._build_prompt_and_metadata("first")
    memory_dir = tmp_path / ".pico" / "memory" / "notes"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "cache.md").write_text("# Cache\n- new clue\n", encoding="utf-8")
    _, second_metadata = agent._build_prompt_and_metadata("second")

    assert first_metadata["prompt_cache_key"] != second_metadata["prompt_cache_key"]
```

- [ ] Change prompt constants in `pico/context_manager.py`:

```python
DEFAULT_TOTAL_BUDGET = 15000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 7000,
    "history": 8000,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 1200,
    "history": 1500,
}
DEFAULT_REDUCTION_ORDER = ("history", "prefix")
SECTION_ORDER = ("prefix", "history", "current_request")
```

- [ ] Add `hashlib` to the `pico/context_manager.py` imports:

```python
import hashlib
```

- [ ] Delete runtime calls to `agent.memory.retrieval_candidates` and remove `_render_relevant_memory`.
- [ ] Keep `MEMORY_USAGE_GUIDANCE`, `MEMORY_READING_GUIDANCE`, `<project_structure>`, and `<memory_index>` in the prefix.
- [ ] Move `<workspace_state>` and `render_checkpoint_text()` into the head of the history section, ordered as workspace state, task checkpoint, transcript.
- [ ] Change `_reusable_file_summary(path)`:

```python
def _reusable_file_summary(self, path):
    entry = self.agent.session.get("memory", {}).get("file_summaries", {}).get(path)
    if isinstance(entry, dict):
        return entry.get("summary", "")
    return ""
```

- [ ] Compute stable prefix metadata after the final prefix text is rendered:

```python
stable_prefix_hash = hashlib.sha256(rendered["prefix"].rendered.encode("utf-8")).hexdigest()
metadata["base_prefix_hash"] = self.agent.prefix_state.hash
metadata["stable_prefix_hash"] = stable_prefix_hash
metadata["prefix_hash"] = stable_prefix_hash
metadata["prompt_cache_key"] = stable_prefix_hash
```

- [ ] Preserve `prompt_cache_key` from `ContextManager.build()` inside `Pico._build_prompt_and_metadata()` by replacing the current `prefix_hash` and `prompt_cache_key` update entries:

```python
stable_prefix_hash = metadata.get("stable_prefix_hash", metadata.get("prefix_hash", self.prefix_state.hash))
metadata.update(
    {
        "prefix_chars": len(self.prefix),
        "workspace_chars": len(self.workspace.text()),
        "memory_chars": len(self.memory_text()),
        "history_chars": len(self.history_text()),
        "request_chars": len(user_message),
        "tool_count": len(self.tools),
        "workspace_docs": len(self.workspace.project_docs),
        "recent_commits": len(self.workspace.recent_commits),
        "base_prefix_hash": self.prefix_state.hash,
        "stable_prefix_hash": stable_prefix_hash,
        "prefix_hash": stable_prefix_hash,
        "prompt_cache_key": stable_prefix_hash,
        "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
        "tool_signature": self.prefix_state.tool_signature,
        "workspace_changed": refresh["workspace_changed"],
        "prefix_changed": refresh["prefix_changed"],
        "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
        "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
        "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
        "stale_paths": list(self.resume_state.get("stale_paths", [])),
        "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
    }
)
```

- [ ] Verify Task 4:

```bash
uv run pytest tests/test_context_manager.py tests/memory/test_prompt_layout.py -q
rg -n "retrieval_candidates|_render_relevant_memory|RELEVANT_MEMORY_LIMIT" pico/context_manager.py
```

Expected for the `rg` command: no output.

## Task 5: Update Resume-Summary Checkpoint Path

Files:

- `pico/checkpoint.py`
- `tests/test_checkpoint.py`
- `tests/test_recovery_e2e.py`

Steps:

- [ ] Add a checkpoint test that asserts recent files come from `WorkingMemory`:

```python
from pico import checkpoint as checkpointlib
from pico.task_state import TaskState


def test_create_checkpoint_uses_working_memory_recent_files(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    agent.memory.remember_file("sample.txt")
    agent._sync_working_memory()
    task_state = TaskState.create("task-1", "read sample", run_id="run-1")

    checkpoint = checkpointlib.create_checkpoint(
        agent,
        task_state,
        "answer",
        "final",
    )

    assert any(item["path"] == "sample.txt" for item in checkpoint["key_files"])
    assert "memory_state" not in checkpoint
```

- [ ] Change `pico/checkpoint.py` from `agent.memory.to_dict()["working"]["recent_files"]` to `agent.memory.recent_files`.
- [ ] Keep `evaluate_resume_state()` calling `agent.invalidate_stale_memory()`.
- [ ] Verify Task 5:

```bash
uv run pytest tests/test_checkpoint.py tests/test_recovery_e2e.py -q
rg -n "memory_state|checkpoint_store|recovery_checkpoint_writer|recovery_models" pico/checkpoint.py
```

Expected for the `rg` command: no output.

## Task 6: Update REPL `/memory`

Files:

- `pico/cli_commands.py`
- `tests/memory/test_repl_v2.py`
- `tests/memory/test_cli_memory_commands.py`

Steps:

- [ ] Update `/memory` tests to require the new compact output:

```python
assert "task:" in output
assert "recent:" in output
assert "Memory files:" in output
assert "Working memory:" not in output
assert "<working_memory>" not in output
```

- [ ] Replace the `/memory` renderer in `pico/cli_commands.py` with direct working-memory and memory-file output:

```python
print(f"task: {agent.memory.task_summary or '(empty)'}")
recent = ", ".join(agent.memory.recent_files) if agent.memory.recent_files else "(empty)"
print(f"recent: {recent}")
print()
print("Memory files:")
for item in agent.memory_store.list():
    print(f"- {item.path} ({item.size_chars} chars)")
```

- [ ] Do not call `agent.memory_text()` from the `/memory` command.
- [ ] Verify Task 6:

```bash
uv run pytest tests/memory/test_repl_v2.py tests/memory/test_cli_memory_commands.py -q
rg -n "memory_text\\(\\)" pico/cli_commands.py
```

Expected for the `rg` command: no output.

## Task 7: Update Evaluator, Metrics, and Benchmark Setup

Files:

- `pico/evaluation/evaluator.py`
- `pico/evaluation/metrics_experiments.py`
- `pico/evaluation/metrics_reports.py`
- `benchmarks/coding_tasks.json`
- `tests/test_evaluator.py`
- `tests/test_metrics.py`

Steps:

- [ ] Replace evaluator setup that calls `agent.memory.set_file_summary` with raw helper calls:

```python
summaries = agent.session.setdefault("memory", {}).setdefault("file_summaries", {})
memorylib.set_file_summary_dict(
    summaries,
    path,
    summary,
    workspace_root=agent.root,
)
```

- [ ] Remove evaluator setup calls to `agent.memory.append_note`.
- [ ] Update evaluator initial memory booleans:

```python
initial_task_summary_empty = not agent.memory.task_summary
initial_episodic_notes_empty = True
initial_memory_empty = initial_task_summary_empty and not agent.memory.recent_files
```

- [ ] Update `benchmarks/coding_tasks.json` context-reduction setup so it uses history volume and the new sections:

```json
{
  "section_budgets": {"prefix": 800, "history": 2400},
  "history_turns": 30
}
```

- [ ] Replace fake-client parsing of `memory:` and `relevant memory:` in `pico/evaluation/metrics_experiments.py` with prompt-wide checks:

```python
prompt_lower = prompt.lower()
if self.expected_fact.lower() in prompt_lower:
    return "<final>memory hit</final>"
return "<tool>{\"name\":\"read_file\",\"args\":{\"path\":\"notes.txt\"}}</tool>"
```

- [ ] Update `measure_feature_ablation_metrics()` so removed sections are reported through a compatibility adapter:

```python
sections = metadata.get("sections", {})
memory_section = sections.get("memory", {})
relevant_section = sections.get("relevant_memory", {})
return {
    "memory_chars": memory_section.get("chars", 0),
    "relevant_memory_chars": relevant_section.get("chars", 0),
    "prompt_chars": metadata.get("prompt_chars", 0),
}
```

- [ ] Keep artifact readers tolerant of both old and new JSON by using `.get(key, default)` calls in `pico/evaluation/metrics_reports.py`.
- [ ] Verify Task 7:

```bash
uv run pytest tests/test_evaluator.py tests/test_metrics.py -q
python - <<'PY'
from pathlib import Path
from pico.evaluation.metrics_experiments import (
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
)

run_context_ablation_v2(Path("/tmp/pico-context-ablation-v2.json"), repetitions=1)
run_memory_ablation_v2(Path("/tmp/pico-memory-ablation-v2.json"), repetitions=1)
run_recovery_ablation_v2(Path("/tmp/pico-recovery-ablation-v2.json"), repetitions=1)
PY
```

If the two known pre-existing evaluator failures appear, record them in the final implementation notes and continue only after confirming there are no new collection, import, or AttributeError failures.

## Task 8: Rewrite Broad v1 Prompt and Session Assertions

Files:

- `tests/test_context_manager.py`
- `tests/test_pico.py`
- `tests/memory/test_prompt_layout.py`
- `tests/memory/test_v1_durable_gone.py`
- `tests/memory/test_repl_v2.py`
- `tests/memory/test_migration.py`
- `tests/test_workspace_observer.py`

Steps:

- [ ] Locate remaining v1 assertions:

```bash
rg -n "Working memory:|Relevant memory:|episodic_notes|\\[\"memory\"\\]\\[\"working\"\\]|append_note|set_file_summary|retrieval_candidates|render_memory_text" tests pico --glob '*.py'
```

- [ ] In runtime tests, replace `session["memory"]["working"]` assertions with `session["working_memory"]`.
- [ ] In prompt tests, replace v1 prompt-section assertions with:

```python
assert "Working memory:" not in prompt
assert "Relevant memory:" not in prompt
assert "<memory_index>" in prompt
assert metadata["section_order"] == ["prefix", "history", "current_request"]
```

- [ ] In migration tests, assert legacy sessions normalize to:

```python
assert set(agent.session["memory"]) == {"file_summaries"}
assert "working_memory" in agent.session
```

- [ ] In durable v1 tests, keep the v1 deletion contract but assert `LayeredMemory` remains dormant in `pico/features/memory.py` only.
- [ ] Verify Task 8:

```bash
uv run pytest tests --co -q
uv run pytest tests/test_context_manager.py tests/test_pico.py tests/memory/test_prompt_layout.py tests/memory/test_v1_durable_gone.py tests/memory/test_migration.py -q
```

## Task 9: Documentation and Benchmark Result Rebaseline

Files:

- `README.md`
- `docs/superpowers/specs/2026-07-03-retire-v1-memory-design.md`
- `docs/superpowers/plans/2026-07-03-retire-v1-memory.md`
- `benchmarks/results/main-resume-repro-2026-06-07/harness-regression-v2.json`
- `benchmarks/results/main-resume-repro-2026-06-07/context-ablation-v2.json`
- `benchmarks/results/main-resume-repro-2026-06-07/memory-ablation-v2.json`
- `benchmarks/results/main-resume-repro-2026-06-07/recovery-ablation-v2.json`

Steps:

- [x] Update user-facing memory docs that mention `/memory` so they describe `task:`, `recent:`, and memory-file listing.
- [x] Record the final implementation notes in the rev3 spec under a short "Implementation Notes" section once code and tests are complete.
- [x] Regenerate the benchmark result files using the repository's benchmark functions:

```bash
uv run python - <<'PY'
from pathlib import Path
from pico.evaluation.evaluator import run_harness_regression_v2
from pico.evaluation.metrics_experiments import (
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
)

out = Path("benchmarks/results/main-resume-repro-2026-06-07")
run_harness_regression_v2(
    benchmark_path=Path("benchmarks/coding_tasks.json"),
    artifact_path=out / "harness-regression-v2.json",
    workspace_root=Path("/tmp/pico-main-resume-workspaces"),
)
run_context_ablation_v2(out / "context-ablation-v2.json", repetitions=5)
run_memory_ablation_v2(out / "memory-ablation-v2.json", repetitions=5)
run_recovery_ablation_v2(out / "recovery-ablation-v2.json", repetitions=3)
PY
```

- [x] Verify the result files are valid JSON:

```bash
python -m json.tool benchmarks/results/main-resume-repro-2026-06-07/harness-regression-v2.json >/tmp/pico-harness-regression-v2.pretty.json
python -m json.tool benchmarks/results/main-resume-repro-2026-06-07/context-ablation-v2.json >/tmp/pico-context-ablation-v2.pretty.json
python -m json.tool benchmarks/results/main-resume-repro-2026-06-07/memory-ablation-v2.json >/tmp/pico-memory-ablation-v2.pretty.json
python -m json.tool benchmarks/results/main-resume-repro-2026-06-07/recovery-ablation-v2.json >/tmp/pico-recovery-ablation-v2.pretty.json
```

## Final Verification Gates

Run all gates before marking implementation complete:

```bash
uv run pytest tests --co -q
uv run pytest tests -q
grep -rn "Working memory:\|Relevant memory:" pico/ --include='*.py' | grep -v "features/memory.py"
grep -rn "LayeredMemory" pico/ tests/ --include='*.py' | grep -v "features/memory.py\|test_memory.py\|test_recovery_cli.py\|test_v1_durable_gone.py\|test_public_api_contract.py"
grep -rn 'session\["memory"\]\|session\.get("memory"' pico/ --include='*.py'
grep -rn "record_process_note_for_tool" pico/ tests/ --include='*.py'
pico-cli doctor --format text
```

Expected results:

- [ ] Collection has zero errors.
- [ ] Full suite has no new failures beyond the two known pre-existing evaluator failures.
- [ ] Runtime prompt headers `Working memory:` and `Relevant memory:` are absent outside dormant v1 helper code.
- [ ] `LayeredMemory` has no runtime prompt-path hits.
- [ ] `session["memory"]` access is limited to runtime normalization, raw file-summary helpers, context reusable summaries, checkpoint stale-summary evaluation, and evaluator/metrics compatibility setup.
- [ ] `record_process_note_for_tool` has no hits.
- [ ] `pico-cli doctor --format text` still emits the `CLAUDE.md` guidance.

## Commit Plan

- [ ] Commit 1: `memory: add working memory model`
- [ ] Commit 2: `memory: add raw file summary helpers`
- [ ] Commit 3: `runtime: sync working memory session state`
- [ ] Commit 4: `prompt: retire v1 memory sections`
- [ ] Commit 5: `checkpoint: read working memory recent files`
- [ ] Commit 6: `cli: simplify memory command output`
- [ ] Commit 7: `evaluation: adapt memory metrics to v2 prompt`
- [ ] Commit 8: `tests: update v1 memory expectations`
- [ ] Commit 9: `docs: rebaseline retire v1 memory artifacts`

## Rollback Plan

- [ ] Revert the implementation commit range if the migration introduces broad prompt or session regressions.
- [ ] Keep `LayeredMemory` available during rollback; legacy sessions remain readable because rev3 sessions contain `working_memory` and `memory.file_summaries`, while legacy v1 sessions still contain the old nested state.
- [ ] Prefer forward fixes for isolated runtime or test issues because this plan deliberately keeps v1 helper code available as a reference.
