# Retire v1 Memory - Execution Design

**Status:** Draft (rev 3, execution-ready after design audit)
**Date:** 2026-07-03
**Branch:** `memory`
**Predecessor spec:** `docs/superpowers/specs/2026-07-02-pico-memory-v2-design.md`
**Related PRs:** #1 (cli->main draft), #2 (memory->cli)

## Revision Notes

Rev 1 treated this as a surgical `Pico.memory` type switch. Rev 2
expanded scope after a feasibility audit found that the v1 memory shape
was still consumed by runtime, prompt assembly, checkpoint resume, tests,
and evaluator code.

Rev 3 keeps the same product goal but corrects the execution contract:

- `WorkingMemory` mutation must be explicitly synchronized back to
  `session["working_memory"]`.
- `session["memory"]` is not removed in this spec; it is narrowed to a
  temporary internal `file_summaries` channel for partial-stale and
  history-compression compatibility.
- `pico/checkpoint.py` and the recoverable-editing checkpoint store are
  separate systems; this spec changes only the resume-summary checkpoint
  reads in `pico/checkpoint.py`.
- `tool_executor.py` is an explicit caller to update when deleting
  `record_process_note_for_tool`.
- Existing metrics experiments that depended on v1 prompt sections must
  be kept importable/runnable, but their old v1-recall semantics are not
  preserved by pretending hidden v1 notes still reach the prompt.
- `prompt_cache_key` must hash the rendered stable prefix that includes
  v2 memory index/project structure, not only the base prefix built by
  `build_prompt_prefix()`.

## Implementation Notes

The retire-v1-memory implementation landed on branch `memory` as a
sequence of focused commits:

- `7fad9f8` added `WorkingMemory` and public import coverage.
- `00e5a54` kept `LayeredMemory` dormant while adding raw
  `file_summaries` helper functions.
- `b904f69` switched runtime session state to synchronized
  `working_memory` plus internal `session["memory"].file_summaries`
  compatibility.
- `c773b66` removed v1 `Working memory:` and `Relevant memory:` prompt
  sections while keeping v2 `<memory_index>` and stable-prefix cache-key
  behavior.
- `8421224` updated resume checkpoints to read `WorkingMemory` recent
  files without changing recoverable-editing checkpoint records.
- `f8c7405` changed `/memory` to the compact `task:`, `recent:`, blank
  line, and `Memory files:` output.
- `d659769` and `101f3b3` adapted evaluator and metrics code to the v2
  prompt while preserving a prompt-wide memory-ablation signal.
- `450b3bc` rewrote broad v1 prompt and session expectations.

The important deviation from the first draft is intentional:
`pico/features/memory.py` and `LayeredMemory` remain available as
dormant helper code during the transition. `session["memory"]` is also
not deleted; it is narrowed to the internal `file_summaries` channel so
partial-stale detection, old read-summary compression, and compatibility
setup continue to work. The design was self-reviewed in `b918333` before
execution, and the final runtime behavior is covered by the focused
commit-level tests plus the re-baselined benchmark artifacts.

## 1. Goal and Scope

### Goal

Remove v1 memory from Pico's runtime public type surface and from prompt
output. After this spec, the prompt should have one memory story:
v2 memory files surfaced through `<memory_index>`, plus memory tools.
The old `Working memory:` and `Relevant memory:` prompt sections go away.

`Pico.memory` becomes a small `WorkingMemory` object that holds only:

- `task_summary`
- `recent_files`

This spec deliberately does **not** delete `pico/features/memory.py`.
`LayeredMemory` and selected helper functions stay temporarily for
legacy tests, evaluator setup, and the internal `file_summaries`
compatibility channel.

### In Scope

1. Add `pico/working_memory.py`.
2. Retype `Pico.memory` from `LayeredMemory` to `WorkingMemory`.
3. Introduce a clear session sync contract for `WorkingMemory`.
4. Replace v1 prompt sections with a three-section prompt order:
   `prefix`, `history`, `current_request`.
5. Move `<workspace_state>` to the head of the history section.
6. Keep v2 memory guidance, `<project_structure>`, and `<memory_index>`
   in the stable prefix.
7. Recompute prompt cache keys from the rendered stable prefix.
8. Narrow `session["memory"]` to an internal legacy channel containing
   only `file_summaries`.
9. Keep `file_summaries` raw-dict helpers for partial-stale detection
   and old read-summary compression.
10. Update `runtime`, `context_manager`, `checkpoint`, `tool_executor`,
    `cli_commands`, `evaluator`, `metrics_experiments`, benchmark data,
    and tests where they directly consume v1 shape.
11. Re-baseline benchmark result JSONs after behavior is intentionally
    changed.

### Out of Scope

- Deleting `pico/features/memory.py`.
- Deleting `LayeredMemory`.
- Redesigning the partial-stale mechanism.
- Moving `file_summaries` to a new first-class schema. That belongs in a
  follow-up partial-stale spec.
- Redesigning the v2 memory-quality benchmark suite. This spec only
  keeps existing metrics code collecting and runnable.
- Removing `DEFAULT_FEATURE_FLAGS["relevant_memory"]`. The flag becomes
  prompt-layout no-op compatibility until a separate cleanup spec.
- Changing recoverable-editing checkpoint records in
  `.pico/checkpoints/`.
- Splitting large files such as `test_pico.py` or `cli_commands.py`.
- Fixing the two known pre-existing red tests:
  - `tests/test_allowed_tools.py::test_benchmark_evaluator_applies_allowed_tools_to_runtime_prompt`
  - `tests/test_evaluator.py::test_benchmark_verifier_runs_with_reproducibility_locale`

## 2. Current Code Facts

These facts are the implementation anchors for this spec:

- `Pico.__init__` currently creates `memorylib.LayeredMemory` over
  `session["memory"]`.
- `LayeredMemory.to_dict()` currently returns nested v1 shape:
  `working`, `episodic_notes`, `file_summaries`, plus flat mirrors
  `task`, `files`, and `notes`.
- `ContextManager` currently renders `memory` and `relevant_memory`
  sections and includes metadata for both.
- `ToolExecutor` currently calls
  `agent.record_process_note_for_tool(name, metadata)` after tool
  execution.
- `pico/checkpoint.py` currently reads recent files from
  `agent.memory.to_dict()["working"]["recent_files"]`.
- `pico/checkpoint.py` stores resume-summary checkpoints under
  `session["checkpoints"]`.
- Recoverable-editing checkpoint records are a separate system under
  `pico/checkpoint_store.py` and `pico/recovery_checkpoint_writer.py`.
  They are not the schema changed by this spec.
- `pico-cli sessions show` currently prints raw session JSON.
- `pico-cli runs show` currently prints raw run artifacts.

## 3. Architecture After This Spec

### 3.1 Runtime Shape

`Pico.memory` is a `WorkingMemory` instance:

```python
@dataclass
class WorkingMemory:
    task_summary: str = ""
    recent_files: list[str] = field(default_factory=list)
    workspace_root: str | Path | None = None

    TASK_SUMMARY_LIMIT = 300
    RECENT_FILES_LIMIT = 8

    def set_task_summary(self, summary: str) -> None: ...
    def remember_file(self, path: str) -> None: ...
    def canonical_path(self, path: str) -> str: ...
    def to_dict(self) -> dict[str, object]: ...

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
        *,
        workspace_root: str | Path | None = None,
    ) -> "WorkingMemory": ...
```

`workspace_root` is runtime-only. It is used for path canonicalization
and is not written by `to_dict()`.

`WorkingMemory.from_dict()` accepts:

- new shape: `{"task_summary": str, "recent_files": [str]}`
- v1 nested shape: `{"working": {"task_summary": str, "recent_files": [str]}}`
- v1 flat shape: `{"task": str, "files": [str]}`
- `None` or invalid data, which produce an empty object

It ignores unknown keys. It does not expose:

- `append_note`
- `set_file_summary`
- `invalidate_file_summary`
- `invalidate_stale_file_summaries`
- `retrieval_candidates`
- `retrieval_view`
- `render_memory_text`
- `episodic_notes`
- `next_note_index`
- `promote_durable`

### 3.2 Session Shape

This spec writes a new public working-memory key:

```json
{
  "working_memory": {
    "task_summary": "...",
    "recent_files": ["src/app.py"]
  },
  "memory": {
    "file_summaries": {
      "src/app.py": {
        "summary": "short summary",
        "created_at": "...",
        "freshness": "sha256..."
      }
    }
  }
}
```

`session["memory"]` remains during this spec, but it is explicitly a
temporary internal legacy channel. It must contain only
`file_summaries` after normalization. The following fields are no longer
written on save:

- `working`
- `episodic_notes`
- `task`
- `files`
- `notes`
- `next_note_index`
- `durable_topics`

Do not call `session.pop("memory", None)` in this spec. That would break
the file-summary compatibility channel described above.

### 3.3 WorkingMemory Synchronization

Because `WorkingMemory` is not a wrapper around a mutable session dict,
runtime code must synchronize it explicitly.

Add a small runtime helper:

```python
def _sync_working_memory(self):
    self.session["working_memory"] = self.memory.to_dict()
    return self.session["working_memory"]
```

Call it after every `WorkingMemory` mutation:

- `AgentLoop.run()` after `agent.memory.set_task_summary(user_message)`
- `runtime.update_memory_after_tool()` after `remember_file`
- `runtime.spawn_delegate()` after setting the child task summary
- `runtime.reset()`
- any test or evaluator helper that mutates `agent.memory`

Session saves already happen through `record()`, run finalization, or
explicit setup saves. The sync helper only keeps the in-memory session
dict coherent; callers that need immediate persistence should continue
to save through `session_store`.

### 3.4 Legacy File Summaries

File summaries stay out of `WorkingMemory`, but they remain useful for:

- partial-stale detection
- `ContextManager` collapsing old repeated reads into short lines
- resume benchmark setup

Add raw-dict helpers in `pico/features/memory.py`:

```python
def normalize_file_summaries_dict(summaries, workspace_root=None) -> dict: ...
def set_file_summary_dict(summaries, path, summary, workspace_root=None) -> dict: ...
def invalidate_file_summary_dict(summaries, path, workspace_root=None) -> dict: ...
def invalidate_stale_file_summaries_dict(summaries, workspace_root=None) -> list[str]: ...
```

These helpers operate on the `file_summaries` dict itself, not on a full
v1 memory state. They may reuse `canonicalize_path`, `file_freshness`,
`clip`, and `now`.

Mutation contract:

- `normalize_file_summaries_dict` returns a normalized dict and does not
  require callers to pass a mutable object.
- `set_file_summary_dict` and `invalidate_file_summary_dict` mutate the
  passed dict in place and return the same dict for convenience.
- `invalidate_stale_file_summaries_dict` mutates the passed dict in place
  and returns only the invalidated path list.
- Call sites may assign returned dicts, but the runtime invalidation path
  must not rely on assignment for stale entries to be removed.

`runtime.update_memory_after_tool()` should:

- keep `self.memory.remember_file(canonical_path)` for
  `read_file`, `write_file`, and `patch_file`
- for `read_file`, write the short summary into
  `session["memory"]["file_summaries"]` using
  `set_file_summary_dict`
- for `write_file` and `patch_file`, remove the path using
  `invalidate_file_summary_dict`
- never append episodic notes
- never call `self.memory.set_file_summary(...)`

This keeps the internal compression/staleness behavior without exposing
file summaries through `Pico.memory`.

## 4. Component Changes

### 4.1 `pico/runtime.py`

Initialization:

1. Build `WorkingMemory` from:
   `session.get("working_memory") or session.get("memory") or {}`.
2. Extract legacy `file_summaries` from `session.get("memory", {})`.
3. Normalize the session to:
   - `session["working_memory"] = self.memory.to_dict()`
   - `session["memory"] = {"file_summaries": normalized_summaries}`

`memory_text()` returns a compact JSON string for metrics continuity:

```python
return json.dumps(self.memory.to_dict(), sort_keys=True)
```

`invalidate_stale_memory()` operates only on
`session["memory"]["file_summaries"]`:

```python
summaries = self.session.setdefault("memory", {}).setdefault("file_summaries", {})
return memorylib.invalidate_stale_file_summaries_dict(summaries, self.root)
```

`record_process_note_for_tool()` is deleted. Runtime no longer records
process notes into v1 episodic memory.

`spawn_delegate()`:

- calls `child.memory.set_task_summary(task)`
- calls `child._sync_working_memory()`
- does not write `child.session["memory"]["notes"]`

`reset()`:

- clears history
- resets `self.memory = WorkingMemory(workspace_root=self.root)`
- writes `session["working_memory"] = self.memory.to_dict()`
- keeps `session["memory"] = {"file_summaries": {}}`

`build_report()` adds:

```json
"working_memory": {"task_summary": "...", "recent_files": [...]}
```

It does not claim that run reports previously had a top-level
`memory_chars` field. Memory character counts remain in
`prompt_metadata`.

### 4.2 `pico/tool_executor.py`

Delete the call to:

```python
agent.record_process_note_for_tool(name, metadata)
```

No replacement is added in this spec. Tool failures and partial success
already flow through:

- tool result metadata
- trace events
- recoverable-editing tool change records
- resume-summary checkpoints

### 4.3 `pico/agent_loop.py`

No algorithmic change is needed. The loop still calls:

```python
agent.memory.set_task_summary(user_message)
```

Add the sync call immediately after it:

```python
agent._sync_working_memory()
```

### 4.4 `pico/context_manager.py`

Collapse prompt sections to:

```python
SECTION_ORDER = ("prefix", "history", "current_request")
```

Use budgets:

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
```

Remove:

- `_render_relevant_memory`
- `RELEVANT_MEMORY_LIMIT`
- selected-note metadata
- `relevant_memory` metadata
- memory section metadata
- code paths that call `agent.memory.retrieval_candidates`

Keep:

- `MEMORY_USAGE_GUIDANCE`
- `MEMORY_READING_GUIDANCE`
- `<project_structure>`
- `<memory_index>`

`<workspace_state>` moves to the beginning of the rendered history
section:

```text
<workspace_state>
...
</workspace_state>

Transcript:
...
```

`render_checkpoint_text()` is also volatile. It must move with
`<workspace_state>` into the history head instead of being appended to
the stable prefix. Recommended history-head order:

```text
<workspace_state>
...
</workspace_state>

Task checkpoint:
...

Transcript:
...
```

If either workspace state or checkpoint text is empty, omit only that
block and keep the remaining order stable.

`_reusable_file_summary(path)` reads:

```python
summary = agent.session.get("memory", {}).get("file_summaries", {}).get(path)
```

It returns `summary["summary"]` when the stored value is a dict, and an
empty string otherwise.

### 4.5 Prompt Cache Key

The current runtime uses `self.prefix_state.hash` as `prompt_cache_key`,
but `ContextManager` appends stable v2 content (`MEMORY_USAGE_GUIDANCE`,
`MEMORY_READING_GUIDANCE`, `<project_structure>`, `<memory_index>`) after
`build_prompt_prefix()` has already computed that hash.

Rev 3 fixes the contract:

- `prefix_state.hash` remains the hash of the base prefix.
- `ContextManager` computes `stable_prefix_hash` from the final rendered
  prefix section before any history/current request text is appended.
- runtime uses `stable_prefix_hash` as `prompt_cache_key`.
- `Pico._build_prompt_and_metadata()` must not overwrite
  `prompt_cache_key` with `self.prefix_state.hash` after
  `ContextManager.build()` returns metadata. It should preserve the
  context-manager value and expose the base hash separately.
- metadata keeps both:
  - `base_prefix_hash`
  - `stable_prefix_hash`
- existing `prefix_hash` may remain as an alias for
  `stable_prefix_hash` if that reduces test churn.

Acceptance:

- two consecutive turns with unchanged AGENTS/project structure/memory
  files produce the same `prompt_cache_key`
- changing `.pico/memory/...` changes `prompt_cache_key`
- changing project structure that appears in `<project_structure>`
  changes `prompt_cache_key`
- changing branch/status/recent commits does not change
  `prompt_cache_key`
- changing resume checkpoint text does not change
  `prompt_cache_key`

### 4.6 `pico/checkpoint.py`

This spec changes the resume-summary checkpoint only.

Do not add `memory_state` to recoverable-editing checkpoint records.
Do not change `pico/checkpoint_store.py`,
`pico/recovery_checkpoint_writer.py`, or `pico/recovery_models.py`.

Required changes:

- `evaluate_resume_state()` continues to call
  `agent.invalidate_stale_memory()`.
- `create_checkpoint()` changes:

```python
for path in agent.memory.to_dict()["working"]["recent_files"]:
```

to:

```python
for path in agent.memory.recent_files:
```

The checkpoint's existing `key_files` and `freshness` fields remain the
resume truth. No checkpoint `memory_state` field is introduced in this
spec.

### 4.7 REPL `/memory`

`/memory` prints:

```text
task: ...
recent: ...

Memory files:
- workspace/notes/auth.md (123 chars)
```

It does not call `agent.memory_text()`. It does not print
`Working memory:`. It does not print XML.

### 4.8 `pico-cli sessions show` and `pico-cli runs show`

Current commands print raw JSON/artifacts. No custom renderer rewrite is
required.

Acceptance is limited to:

- legacy sessions can still be loaded and shown
- new sessions show `working_memory`
- new sessions do not write v1 episodic fields
- run reports include `working_memory`

### 4.9 `pico/evaluation/evaluator.py`

Setup helpers that need legacy file summaries should use raw-dict helper
functions, not `agent.memory.set_file_summary`.

Context-reduction setup no longer seeds episodic notes. The setup should
use history volume and section budgets to exercise prompt reduction.

Initial memory booleans become:

```python
initial_task_summary_empty = not agent.memory.task_summary
initial_episodic_notes_empty = True
initial_memory_empty = (
    initial_task_summary_empty
    and not agent.memory.recent_files
)
```

Golden JSON keeps the existing keys, but values shift where expected.

### 4.10 `pico/evaluation/metrics_experiments.py`

Do not preserve old v1-recall semantics by routing hidden notes through
`_v1_view` and pretending they still affect the prompt. After this spec,
v1 episodic notes are not prompt input.

Execution contract:

- imports must stay green
- `run_context_ablation_v2(...)` must complete
- `run_memory_ablation_v2(...)` must complete
- `run_recovery_ablation_v2(...)` must complete
- existing artifact schemas should remain readable by
  `metrics_reports.py`

Allowed changes:

- Remove or no-op note injection that previously relied on
  `agent.memory.append_note`.
- Keep artifact fields such as `memory_hit_rate`, but document that this
  artifact is a legacy smoke/continuity metric until the v2
  memory-quality benchmark replaces it.
- Update fake clients that parse `memory:` / `relevant memory:` so they
  do not require removed prompt sections.
- Continue using raw `file_summaries` helpers for recovery setup.
- Update `measure_feature_ablation_metrics()` so removed sections are
  reported as `0` or absent through a documented compatibility adapter.
  Do not assume `metadata["sections"]["memory"]` or
  `metadata["relevant_memory"]` exists after this spec.

Follow-up work:

- A separate v2 memory-quality spec should replace old v1 memory
  ablation with scenarios based on memory files, memory tools, and
  explicit recall/eval traces.

## 5. Data Migration and Compatibility

### 5.1 Reading Existing Sessions

On load:

1. Read `working_memory` if present.
2. Otherwise derive working state from legacy `memory`.
3. Extract legacy `file_summaries` from legacy `memory`.
4. Normalize the in-memory session to the rev-3 shape.

No batch migration command is required.

### 5.2 Writing Sessions

On save, new sessions write:

```json
{
  "working_memory": {
    "task_summary": "...",
    "recent_files": ["..."]
  },
  "memory": {
    "file_summaries": {}
  }
}
```

They do not write v1 notes, episodic notes, or working mirrors under
`session["memory"]`.

### 5.3 Checkpoint Compatibility

Existing resume-summary checkpoints remain compatible because their
resume contract is based on:

- `schema_version`
- `key_files`
- `freshness`
- `runtime_identity`

This spec does not depend on `memory_state` in checkpoint payloads.

### 5.4 Feature Flag Compatibility

`DEFAULT_FEATURE_FLAGS["relevant_memory"]` remains for now. It no
longer causes a prompt section to render. It remains in runtime identity
until a separate compatibility cleanup removes it deliberately.

## 6. Testing Strategy

### 6.1 New `tests/test_working_memory.py`

Add focused tests:

- defaults are empty
- `set_task_summary` truncates to 300 chars
- `remember_file` dedupes and keeps the latest 8
- path canonicalization uses `workspace_root`
- `to_dict` round-trips flat shape
- `from_dict` reads new shape
- `from_dict` reads v1 nested shape
- `from_dict` reads v1 flat shape
- invalid or `None` input produces empty memory
- deprecated methods are absent

### 6.2 Prompt/Layout Tests

Update or add tests for:

- `SECTION_ORDER == ["prefix", "history", "current_request"]`
- prompt has no `Working memory:` section
- prompt has no `Relevant memory:` section
- prompt still contains v2 memory guidance
- prompt still contains `<memory_index>`
- `<workspace_state>` is at the head of history, after stable prefix
- checkpoint text, when present, is in history rather than prefix
- metadata has no `memory` or `relevant_memory` section entries
- current request is never clipped

### 6.3 Prompt Cache Tests

Add tests for:

- unchanged memory files and project docs keep `prompt_cache_key` stable
- editing/adding a memory file changes `prompt_cache_key`
- changing project structure that appears in `<project_structure>`
  changes `prompt_cache_key`
- branch/status-only volatility does not change `prompt_cache_key`
- checkpoint/resume-status volatility does not change `prompt_cache_key`

### 6.4 Runtime and Session Tests

Update tests for:

- `type(agent.memory).__name__ == "WorkingMemory"`
- `AgentLoop.run()` syncs task summary into `session["working_memory"]`
- read/write/patch tools update `recent_files`
- read tools write raw `session["memory"]["file_summaries"]`
- write/patch tools invalidate raw file summaries
- new sessions do not write v1 note fields
- legacy sessions load and normalize without crashing
- `reset()` clears history and working memory while preserving the
  narrowed `memory.file_summaries` shape

### 6.5 Tool Executor Tests

Update tests that expected process notes. Verification should move to
tool metadata, trace, or checkpoint records. There should be no call to
`record_process_note_for_tool`.

### 6.6 Checkpoint Tests

Update tests for:

- `create_checkpoint()` reads `agent.memory.recent_files`
- stale summary invalidation operates on
  `session["memory"]["file_summaries"]`
- partial-stale resume remains green
- recoverable-editing checkpoint records are unchanged

### 6.7 CLI Tests

Update `/memory` tests to assert:

- `task:`
- `recent:`
- `Memory files:`
- no `Working memory:`

For `sessions show` and `runs show`, verify that raw output still works
for both legacy and new shape.

### 6.8 Evaluator and Metrics Tests

Update tests so:

- evaluator imports
- metrics imports
- `run_fixed_benchmark(...)` completes without AttributeError
- `run_context_ablation_v2(...)` completes
- `run_memory_ablation_v2(...)` completes
- `run_recovery_ablation_v2(...)` completes
- artifact readers in `metrics_reports.py` still handle produced JSON

### 6.9 Full-Suite Expectations

Required:

- `pytest tests --co -q` has zero collection errors.
- Full suite has only the two known pre-existing failures listed in this
  spec.
- No new failures are accepted.

## 7. Grep Gates

Run these before merge.

### Gate 1: Runtime prompt headers are gone

```bash
grep -rn "Working memory:\|Relevant memory:" pico/ --include='*.py' \
  | grep -v "features/memory.py"
```

Expected: no output.

`features/memory.py` may still contain dormant v1 render helpers because
`LayeredMemory` is not deleted in this spec.

### Gate 2: `LayeredMemory` use is constrained

```bash
grep -rn "LayeredMemory" pico/ tests/ --include='*.py' \
  | grep -v "features/memory.py\|test_memory.py\|test_recovery_cli.py\|test_v1_durable_gone.py\|test_public_api_contract.py"
```

Expected: no runtime prompt path hits. If evaluator/metrics still need
explicit legacy setup, those hits must be documented and covered by
import/run smoke tests.

### Gate 3: Session legacy memory access is constrained

```bash
grep -rn 'session\["memory"\]\|session\.get("memory"' pico/ --include='*.py'
```

Allowed categories:

- runtime session normalization
- runtime raw file-summary helpers
- context-manager reusable summary reads
- checkpoint stale-summary evaluation
- evaluator/metrics legacy setup

No dedicated `memory` or `relevant_memory` prompt section may depend on
`session["memory"]`, because those sections no longer exist. The history
section may still read `session["memory"]["file_summaries"]` only for
old read-summary compression.

### Gate 4: Removed method is gone

```bash
grep -rn "record_process_note_for_tool" pico/ tests/ --include='*.py'
```

Expected: no output, unless a test is asserting absence by source scan.

## 8. Execution Sequence

This sequence is designed to minimize ambiguous breakage. Some commits
may be red while tests are being migrated, but each commit should have a
clear local proof target.

### Commit 1: Add `WorkingMemory`

Files:

- `pico/working_memory.py`
- `tests/test_working_memory.py`
- `tests/test_public_api_contract.py`

Proof:

```bash
uv run pytest tests/test_working_memory.py tests/test_public_api_contract.py -q
```

### Commit 2: Add raw file-summary helpers

Files:

- `pico/features/memory.py`
- focused tests in `tests/test_memory.py` or a new small test file

Proof:

```bash
uv run pytest tests/test_memory.py -q
```

### Commit 3: Switch runtime to `WorkingMemory`

Files:

- `pico/runtime.py`
- `pico/agent_loop.py`
- `pico/tool_executor.py`
- `tests/test_working_memory_runtime.py`

Includes:

- session normalization
- `_sync_working_memory`
- raw file-summary writes/invalidations
- deletion of process-note recording
- delegate task-summary sync

Proof:

```bash
uv run pytest tests/test_working_memory.py tests/test_working_memory_runtime.py -q
```

Do not use the full `tests/test_pico.py` file as the proof for this
commit if it still contains known v1 prompt assertions. Broad `test_pico`
cleanup happens in Commit 8.

### Commit 4: Collapse prompt layout and fix cache key

Files:

- `pico/context_manager.py`
- prompt layout/cache tests

Includes:

- three-section prompt order
- workspace state at history head
- removal of v1 relevant-memory metadata
- `stable_prefix_hash`/`prompt_cache_key` contract

Proof:

```bash
uv run pytest tests/test_context_manager.py tests/memory/test_prompt_layout.py -q
```

### Commit 5: Update resume-summary checkpoint path

Files:

- `pico/checkpoint.py`
- `tests/test_working_memory_checkpoint.py`

Proof:

```bash
uv run pytest tests/test_working_memory_checkpoint.py tests/test_recovery_e2e.py -q
```

### Commit 6: Update CLI `/memory`

Files:

- `pico/cli_commands.py`
- `tests/memory/test_repl_v2.py`

Proof:

```bash
uv run pytest tests/memory/test_repl_v2.py tests/memory/test_cli_memory_commands.py -q
```

### Commit 7: Update evaluator, metrics, and benchmark setup

Files:

- `pico/evaluation/evaluator.py`
- `pico/evaluation/metrics_experiments.py`
- `benchmarks/coding_tasks.json`
- related evaluator/metrics tests

Proof:

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

Known pre-existing evaluator failures may remain, but collection and
import errors are not acceptable.

### Commit 8: Rewrite broad v1 prompt tests

Files:

- `tests/test_context_manager.py`
- `tests/test_pico.py`
- `tests/memory/test_v1_durable_gone.py`
- `tests/test_workspace.py`
- any other tests with v1 prompt assertions

Proof:

```bash
uv run pytest tests --co -q
uv run pytest tests -q
```

The full-suite command is expected to exit non-zero until the two
pre-existing failures are fixed. Treat it as an inventory gate: the only
allowed failing tests are the two named pre-existing failures in this
spec.

### Commit 9: Docs and benchmark result re-baseline

Files:

- README or memory model docs that mention `/memory`
- `benchmarks/results/harness-regression-v2.json`
- `benchmarks/results/benchmark-v1.json`

Proof:

```bash
uv run pytest tests --co -q
```

Run the benchmark command used to regenerate the two result files and
record it in the commit message.

## 9. Acceptance Checklist

- [ ] `type(Pico(...).memory).__name__ == "WorkingMemory"`.
- [ ] `WorkingMemory` exposes no v1 note/retrieval/render methods.
- [ ] `session["working_memory"]` is written and synchronized.
- [ ] `session["memory"]` contains only `file_summaries`.
- [ ] Prompt section order is `prefix`, `history`, `current_request`.
- [ ] Prompt has no runtime `Working memory:` section.
- [ ] Prompt has no runtime `Relevant memory:` section.
- [ ] `<memory_index>` still renders when memory files exist.
- [ ] `<workspace_state>` renders at the head of history.
- [ ] `prompt_cache_key` changes when memory index/project structure
      changes.
- [ ] `prompt_cache_key` does not change for branch/status-only
      volatility.
- [ ] `/memory` prints `task:`, `recent:`, and `Memory files:`.
- [ ] `tool_executor.py` no longer calls `record_process_note_for_tool`.
- [ ] `create_checkpoint()` reads `agent.memory.recent_files`.
- [ ] partial-stale resume remains green.
- [ ] evaluator and metrics modules import.
- [ ] `run_context_ablation_v2`, `run_memory_ablation_v2`, and
      `run_recovery_ablation_v2` complete without AttributeError.
- [ ] `pico-cli doctor --format text` still emits the CLAUDE.md hint.
- [ ] `pico-cli sessions show <legacy-session-id>` does not crash.
- [ ] `pytest tests --co -q` reports zero collection errors.
- [ ] `pytest tests -q` reports only the two named pre-existing failures.

## 10. Rollback

Rollback is straightforward because v1 code remains present:

1. Revert the merge commit or the commit range.
2. Existing legacy sessions still contain enough v1 shape for
   `LayeredMemory` to load.
3. New rev-3 sessions contain `working_memory` plus a narrowed
   `memory.file_summaries` channel; `LayeredMemory` can still derive an
   empty/default v1 state from missing note fields if needed.

Forward fix is preferred for small follow-up issues because the old v1
implementation remains available for reference during this transition.

## 11. Follow-Up Specs

1. Partial-stale redesign: move `file_summaries` out of
   `session["memory"]` and into a first-class schema.
2. Metrics redesign: replace v1 memory ablation with v2 memory-file and
   memory-tool quality benchmarks.
3. Remove `relevant_memory` feature flag from runtime identity.
4. Delete `LayeredMemory` and move path/freshness helpers to a smaller
   module.
5. Delegate context redesign: replace parent v1 notes with an explicit
   v2 `<parent_context>` block if needed.
