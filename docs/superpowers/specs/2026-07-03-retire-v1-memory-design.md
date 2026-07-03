# Retire v1 Memory · Design Spec

**Status:** Draft (rev 2, post feasibility audit)
**Date:** 2026-07-03
**Branch:** `memory`
**Predecessor spec:** `docs/superpowers/specs/2026-07-02-pico-memory-v2-design.md`
**Related PRs:** #1 (cli→main draft), #2 (memory→cli)

**Revision notes.** Rev 1 framed this as "surgical Pico.memory type
switch" with metrics_experiments / evaluator kept out of scope. An
independent feasibility audit surfaced 8 blockers where v1 shape was
consumed by paths the rev-1 scope explicitly disowned
(`invalidate_stale_memory`, `_reusable_file_summary`,
`create_checkpoint` recent_files iteration, evaluator `_apply_task_setup`,
metrics_experiments call sites). Rev 2 pulls those consumers into
scope through a `_v1_view(agent)` helper and dedicated rewrites; the
red band across commits widens accordingly (green only at commit 10
of 12, per §5.2).

---

## 1 · Goal & Scope

### Goal

Remove v1 memory concepts from prompt output and runtime type surface.
Collapse the two-of-a-kind "Memory" language (v1 `Memory:` / `Relevant memory:`
sections vs v2 `<memory_index>`) into a single v2 story. Reduce
`Pico.memory` to a ~120 LOC `WorkingMemory` dataclass holding only
`task_summary` and `recent_files`.

Do **not** delete `pico/features/memory.py`. The v1 helpers
(`canonicalize_path`, `file_freshness`) and the `LayeredMemory` class
remain in place because (a) `metrics_experiments.py` has 74 direct
references we are not touching this cycle, (b) `checkpoint.py` still
uses `file_freshness` for partial-stale bookkeeping, and (c) one test
end-to-end verifies partial-stale via `set_file_summary`. True deletion
of the v1 file is the subject of a subsequent spec.

### In scope

1. Add `pico/working_memory.py` — `WorkingMemory` dataclass (≤120 LOC).
2. Retype `Pico.memory` from `LayeredMemory` to `WorkingMemory`.
3. Delete "Working memory:" and "Relevant memory:" sections from the
   prompt. Collapse `SECTION_ORDER` to `[prefix, history, current_request]`.
4. Rebase budgets/floors/reduction order to two-section reality.
5. Move `<workspace_state>` from its current pre-memory position to the
   head of the `history` section (still volatile; stays out of the
   stable prefix so prompt cache remains hot).
6. REPL `/memory` prints two top lines (`task:` / `recent:`) followed by
   memory-file listing. No XML wrapping.
7. Session JSON: write `working_memory` key; read either
   `working_memory` or legacy `memory` shape.
8. Checkpoint payload: write `working_memory` for the WorkingMemory
   subset; keep `file_summaries` as a separate v1-shaped sub-key for the
   partial-stale channel until its own spec.
9. `spawn_delegate`: pass `task_summary` via `set_task_summary`; stop
   writing `session["memory"]["notes"]`.
10. `evaluator.py`: hardcode `initial_episodic_notes_empty = True`
    (schema retained; value constant).
11. `benchmarks/coding_tasks.json`: merge the
    `context_reduction_checkpoint` task's `memory`/`relevant_memory`
    budget keys into a single `history` key of equal total.
12. Test updates: extend `test_v1_durable_gone.py`; rewrite the
    context_manager / repl / workspace / public_api_contract assertions
    that referenced v1 headers; surgical edits to `test_pico.py`.
13. Re-baseline `benchmarks/results/harness-regression-v2.json` and
    `benchmark-v1.json` in a single dedicated commit.

### Out of scope (each becomes its own spec)

- Deleting `pico/features/memory.py` outright (v1 class kept in place).
- Deleting `pico/evaluation/metrics_experiments.py`'s v1 concepts
  entirely — this spec rewires call sites through a `_v1_view` helper
  (§2 C10) but does not redesign the ablation experiments.
- Partial-stale mechanism redesign; the `file_summaries` channel
  survives this spec (session-hosted; not on WorkingMemory).
- Delegate context-injection redesign — `notes` cross-agent transfer
  is dropped; only `task_summary` crosses.
- `test_pico.py` (1864 LOC) file split.
- `cli_commands.py` (1025 LOC) split.
- Sessions/*.json prefix naming alignment.
- `pico/checkpoint.py` vs `pico/checkpoint_store.py` naming collision.
- Filling in the missing `recoverable-editing` design spec.
- Fixing the two pre-existing red tests:
  - `tests/test_allowed_tools.py::test_benchmark_evaluator_applies_allowed_tools_to_runtime_prompt`
  - `tests/test_evaluator.py::test_benchmark_verifier_runs_with_reproducibility_locale`

### Success criteria

- `type(Pico(...).memory).__name__ == "WorkingMemory"`.
- `grep -rn "Working memory:\|Relevant memory:" pico/` returns 0.
- `SECTION_ORDER == ["prefix", "history", "current_request"]`.
- Prompt contains no `Memory:` or `Relevant memory:` header outside the
  v2 `<memory_index>` block (which uses "Memory files:" listing form).
- REPL `/memory` prints `task:` / `recent:` / `Memory files:`.
- `pico-cli doctor --format text` still shows the CLAUDE.md hint.
- `import pico.evaluation.metrics_experiments` and
  `import pico.evaluation.evaluator` succeed.
- `run_fixed_benchmark(...)` and one `run_*_experiment(...)` call
  each complete without AttributeError (usability, not just import).
- Full test suite: 2 pre-existing red / 0 collection errors / all
  others pass.
- `prompt_cache_key` remains identical across two consecutive turns
  when memory files and AGENTS.md are unchanged.

---

## 2 · Component boundaries & interfaces

### C1 · `pico/working_memory.py` (NEW)

```python
@dataclass
class WorkingMemory:
    task_summary: str = ""
    recent_files: list[str] = field(default_factory=list)

    TASK_SUMMARY_LIMIT = 300
    RECENT_FILES_LIMIT = 8

    def set_task_summary(self, summary: str) -> None: ...
    def remember_file(self, path: str, workspace_root: str | None = None) -> None:
        """LRU-append; canonicalize via features.memory.canonicalize_path
        when workspace_root is provided."""
    def canonical_path(self, path: str) -> str: ...
    def to_dict(self) -> dict: ...

    @classmethod
    def from_dict(cls, data: dict | None) -> "WorkingMemory":
        """Accepts:
        - new shape:      {"task_summary": str, "recent_files": [str]}
        - v1 shape:       {"working": {"task_summary": str, "recent_files": [str]}, ...}
        - v1 top-level:   {"task": str, "files": [str], ...}
        Unknown keys are silently ignored."""
```

Depends on `pico.features.memory.canonicalize_path`. No other v1
dependency. Does not expose `append_note`, `set_file_summary`,
`retrieval_view`, `retrieval_candidates`, `render_memory_text`,
`episodic_notes`, `next_note_index`, or `promote_durable`.

### C2 · `pico/features/memory.py` (UNCHANGED)

Retained for:
- `canonicalize_path(path, workspace_root)` — consumed by
  `WorkingMemory.remember_file`.
- `file_freshness(path, workspace_root)` — consumed by
  `checkpoint.py:76,154`.
- `LayeredMemory` class — constructed directly in
  `test_resume_invalidates_stale_file_summaries_and_marks_partial_stale`
  and in `metrics_experiments.py`.

`Pico.memory` is no longer an instance of `LayeredMemory`; nothing
else in `pico/runtime.py` or `pico/agent_loop.py` reaches into v1
state.

### C3 · `pico/runtime.py`

- `Pico.__init__`: `self.memory = WorkingMemory.from_dict(session.get("working_memory") or session.get("memory") or {})`.
- `self.memory_text()`: returns `json.dumps(self.memory.to_dict())` — preserves the `memory_chars` field shape for golden JSON.
- `_ensure_session_shape`: `session.setdefault("working_memory", {})`; on next save, `session.pop("memory", None)` clears the legacy key.
- `update_memory_after_tool` (`runtime.py:387-420`):
  - Keep: `self.memory.set_task_summary(...)`, `self.memory.remember_file(...)`.
  - Delete calls: `self.memory.append_note(...)`, `self.memory.set_file_summary(...)`.
- `record_process_note_for_tool` (`runtime.py:425-439`): delete method and its call sites.
- `invalidate_stale_memory` (`runtime.py:184`): rewrite to operate on
  `session["memory"]["file_summaries"]` directly:
  ```python
  def invalidate_stale_memory(self):
      summaries = self.session.setdefault("memory", {}).setdefault("file_summaries", {})
      dropped = memorylib.invalidate_stale_file_summaries_dict(summaries, self.root)
      return dropped
  ```
  where `memorylib.invalidate_stale_file_summaries_dict` is a new
  module-level helper added to `pico/features/memory.py` operating on
  raw dicts (not `LayeredMemory` instances). Called by
  `checkpoint.evaluate_resume_state` (`checkpoint.py:62`).
- `spawn_delegate` (`runtime.py:593-594`):
  - `child.session["memory"]["task"] = task` → `child.memory.set_task_summary(task)`.
  - Delete `child.session["memory"]["notes"] = [...]`.
- `Pico.reset()`: `self.memory = WorkingMemory()`; `self.session["working_memory"] = {}`.
- `build_report`: `memory_chars = len(self.memory_text())` (key retained; value shrinks).

### C4 · `pico/agent_loop.py`

No changes. `agent.memory.set_task_summary(x)` and
`agent.memory.remember_file(y)` remain valid.

### C5 · `pico/context_manager.py`

- `SECTION_ORDER = ["prefix", "history", "current_request"]`.
- `DEFAULT_SECTION_BUDGETS = {"prefix": 7000, "history": 8000}`.
- `DEFAULT_SECTION_FLOORS = {"prefix": 1200, "history": 1500}`.
- `DEFAULT_REDUCTION_ORDER = ("history", "prefix")`.
- `DEFAULT_TOTAL_BUDGET = 15000`.
- Delete: `_render_memory_section`, `_render_relevant_memory`,
  `RELEVANT_MEMORY_LIMIT`, `selected_notes` metadata,
  `selected_durable_count`.
- Keep: `MEMORY_USAGE_GUIDANCE`, `MEMORY_READING_GUIDANCE` (both v2).
- `workspace_state` placement:
  `section_texts["history"] = workspace_state_text + "\n\n" + history_body`
  — `<workspace_state>` moves to the head of the `history` section.
- `_reusable_file_summary` (`context_manager.py:462-470`): rewrite to
  read from the session file_summaries channel rather than
  `agent.memory.to_dict()`:
  ```python
  def _reusable_file_summary(self, path):
      summaries = getattr(self.agent, "session", {}).get("memory", {}).get("file_summaries", {})
      return summaries.get(path)
  ```
  `sample.txt -> alpha | beta` collapse logic downstream is
  unchanged; only the source of `file_summaries` moves from
  `agent.memory` (WorkingMemory has no such attribute) to the raw
  session channel that checkpoint restore already hydrates.

### C6 · `pico/workspace.py`

No changes to signatures. `volatile_text()` continues to return only
branch/status/commits. `task_summary`/`recent_files` do **not** enter
`WorkspaceContext.fingerprint`.

### C7 · REPL `/memory` (in `pico/cli_commands.py`)

```python
if user_input == "/memory":
    wm = agent.memory.to_dict()
    task = wm.get("task_summary", "") or "-"
    files = ", ".join(wm.get("recent_files") or []) or "-"
    print(f"task: {task}")
    print(f"recent: {files}")
    entries = agent.memory_store.list()
    if entries:
        print("\nMemory files:")
        for e in entries:
            print(f"- {e.path} ({e.size_chars} chars)")
    else:
        print("\nMemory files: (none)")
    continue
```

No calls to `agent.memory_text()`. No `<workspace_state>` XML rendering.

### C8 · `pico/checkpoint.py`

- Write: `record.memory_state = {"working_memory": self.memory.to_dict(), "file_summaries": self.session.get("memory", {}).get("file_summaries", {})}`.
- Read: `working_dict = mem_state.get("working_memory") or mem_state.get("working") or mem_state`; then `self.memory = WorkingMemory.from_dict(working_dict)`; `file_summaries = mem_state.get("file_summaries", {})`.
- `create_checkpoint` (`checkpoint.py:153`): change
  `agent.memory.to_dict()["working"]["recent_files"]` to a direct
  attribute read `agent.memory.recent_files`. Rationale:
  `WorkingMemory.to_dict()` is flat (no `"working"` sub-dict); the
  original v1 nesting is gone.
- Checkpoint restore path additionally seeds
  `session["memory"]["file_summaries"] = mem_state.get("file_summaries", {})`
  so subsequent `invalidate_stale_memory` calls and
  `_reusable_file_summary` reads find the summaries in the session
  channel where the rewritten code expects them.
- `file_freshness` invocations at lines 76 and 154 are unchanged.

### C9 · `pico/evaluation/evaluator.py`

Every call site that reaches into `agent.memory` for v1 shape must
route through a locally-constructed `LayeredMemory` view against the
same session dict, because `agent.memory` is now `WorkingMemory` and
does not expose `append_note` / `set_file_summary` / `episodic_notes`
/ `file_summaries`. Rewrite the affected sites:

```python
def _v1_view(agent):
    """Ephemeral LayeredMemory over agent.session['memory']. Used only
    for v1-shape probes in evaluator setup / assertions."""
    from pico.features.memory import LayeredMemory
    return LayeredMemory(agent.session.get("memory", {}), workspace_root=agent.root)
```

Affected lines (`evaluator.py`):
- L323, L328: `agent.memory.append_note(...)` /
  `agent.memory.set_file_summary(...)` in `_apply_task_setup`
  → `view = _v1_view(agent); view.append_note(...); view.set_file_summary(...)`;
  then persist back into `agent.session["memory"] = view.to_dict()`.
- L341-344: `agent.memory.to_dict()["file_summaries"]` /
  `["working"]["task_summary"]` → read from
  `agent.session.get("memory", {}).get("file_summaries", {})` and
  from `agent.memory.task_summary` (WorkingMemory attribute) directly.
- L479-482: `initial_memory_state["working"]["task_summary"]`,
  `initial_memory_state["episodic_notes"]`, and
  `memorylib.is_effectively_empty(initial_memory_state)` — replace
  with:
  - `initial_task_summary_empty = not agent.memory.task_summary`
  - `initial_episodic_notes_empty = True` (constant; WorkingMemory
    does not carry episodic_notes)
  - `initial_memory_empty = initial_task_summary_empty and not agent.memory.recent_files`

Golden JSON schema for `benchmark-v1.json` /
`harness-regression-v2.json` retains all three key names; only values
shift for `initial_episodic_notes_empty` (always `True`).

### C10 · `pico/evaluation/metrics_experiments.py`

Cannot be left "no changes." Every call site that references
`agent.memory` reaches an instance whose type has changed. Rewrite
each such site with the `_v1_view(agent)` helper introduced in C9:

Affected lines (`metrics_experiments.py`):
- L68, L326, L805: `agent.memory.append_note(...)` →
  `view = _v1_view(agent); view.append_note(...); agent.session["memory"] = view.to_dict()`.
- L131-145 (`_set_irrelevant_memory`) and L228-242
  (`_set_irrelevant_memory_for_task`): same pattern — build a
  LayeredMemory view, mutate, write back.
- L1049-1091 (`_apply_recovery_setup`): same.
- Any read of `agent.memory.to_dict()["file_summaries"]` etc: use
  `agent.session["memory"]["file_summaries"]` directly.

Extract `_v1_view` into a small module-level helper in
`pico/evaluation/_v1_view.py` (or inline the 3-line body per file if
duplication is preferable) — one canonical construction point avoids
drift.

Impact: `import pico.evaluation.metrics_experiments` remains green,
AND `run_*_experiment()` continues to function. Both must be verified
at commit close.

### C11 · `benchmarks/coding_tasks.json`

`context_reduction_checkpoint.setup.section_budgets`:

```
Before: {"prefix": 800, "memory": 800, "relevant_memory": 800, "history": 2400}
After:  {"prefix": 800, "history": 4000}
```

Total budget (4800) and verifier contract unchanged.

### Dependency graph after change

```
runtime           → WorkingMemory
                  → memorylib.invalidate_stale_file_summaries_dict (new helper)
agent_loop        → WorkingMemory (via runtime)
context_manager   → session["memory"]["file_summaries"]  (raw read for _reusable_file_summary)
workspace         → (unchanged)
REPL /memory      → WorkingMemory.to_dict + BlockStore.list

checkpoint        → WorkingMemory.to_dict / from_dict
                  → features.memory.file_freshness   (kept)
                  → session["memory"]["file_summaries"]  (mixed-shape channel)
                  → agent.memory.recent_files  (direct attribute read)

evaluator         → LayeredMemory (via _v1_view helper) for setup/probe
                  → agent.memory.task_summary / .recent_files direct
metrics_exp       → LayeredMemory (via _v1_view helper) for setup/probe
tests             → WorkingMemory + LayeredMemory (partial-stale only)
```

---

## 3 · Data migration & compat

### 3.1 · Session JSON

**Old (v1):**

```json
{
  "id": "...",
  "memory": {
    "working": {"task_summary": "...", "recent_files": [...]},
    "episodic_notes": [...],
    "file_summaries": {...},
    "task": "...",
    "files": [...],
    "notes": [...],
    "next_note_index": 3,
    "durable_topics": []
  }
}
```

**New:**

```json
{
  "id": "...",
  "working_memory": {
    "task_summary": "...",
    "recent_files": [...]
  }
}
```

- Write: `self.session["working_memory"] = self.memory.to_dict()`; `self.session.pop("memory", None)` on first save.
- Read: `session.get("working_memory") or session.get("memory") or {}` handed to `WorkingMemory.from_dict`.
- Fields dropped: `episodic_notes`, `notes`, `next_note_index`, `durable_topics`, `file_summaries` (session channel only — checkpoint retains file_summaries via 3.2).
- User-visible impact: none (v1 dashboard no longer rendered).

### 3.2 · Checkpoint record

Mixed shape (explicit tradeoff):

```json
{
  "memory_state": {
    "working_memory": {"task_summary": "...", "recent_files": [...]},
    "file_summaries": {"path/to/file.py": {"summary": "...", "freshness": "..."}}
  }
}
```

- `working_memory`: consumed by `WorkingMemory.from_dict`.
- `file_summaries`: read as a raw dict by the partial-stale channel.
  `WorkingMemory` itself does not expose file_summaries.
- Compatibility with pre-spec checkpoints:
  - `mem_state["working"]` (nested v1 shape) → picked up by `from_dict`.
  - `mem_state["task"] + mem_state["files"]` (flat v1 top-level) →
    picked up by `from_dict` top-level branch.

### 3.3 · Run report JSON

- Keep: `memory_chars` (value reflects `len(json.dumps(working_memory.to_dict()))`).
- Keep: `initial_episodic_notes_empty` (constant `True`).
- Add: `working_memory: {"task_summary": ..., "recent_files": [...]}`.
- Drop: `episodic_notes`, `file_summaries`, `notes`, `durable_topics`.
- `benchmarks/results/*.json` golden artifacts re-baselined in a single
  dedicated commit (Section 5.2 commit 9).

### 3.4 · `sessions show` / `runs show` renderers

Any read of `data["memory"]["episodic_notes"]` /
`data["memory"]["working"]["task_summary"]` becomes
`data.get("working_memory", {}).get("task_summary")` etc. Missing
fields render as `-`.

### 3.5 · Not doing

- No batch migration script.
- No `episodic_notes` → `.pico/memory/agent_notes.md` data movement.
- No deprecation warning.
- No CI check for v1 session reverse-compat.

### 3.6 · Deprecation timeline

- This spec: write new shape; read compat retained; partial-stale
  channel still reads v1 `file_summaries`.
- Follow-up A (partial-stale redesign): checkpoint schema collapses
  to pure new shape.
- Follow-up B (`pico/features/memory.py` teardown): `LayeredMemory`
  deleted; `file_freshness` and `canonicalize_path` move to a
  standalone module (candidate: `pico/paths.py`).

---

## 4 · Testing strategy

### 4.1 · New tests

`tests/test_working_memory.py`:
- `test_defaults_are_empty`
- `test_set_task_summary_truncates_to_300`
- `test_remember_file_dedups_and_keeps_last_8`
- `test_remember_file_canonicalizes_when_workspace_root_given`
- `test_to_dict_roundtrip`
- `test_from_dict_reads_new_shape`
- `test_from_dict_reads_v1_nested_shape`
- `test_from_dict_reads_v1_top_level_shape`
- `test_from_dict_none_or_empty`
- `test_from_dict_ignores_extra_keys`
- `test_no_deprecated_methods` (`hasattr` false-checks for
  `append_note`, `set_file_summary`, `retrieval_view`).

### 4.2 · Extend `tests/memory/test_v1_durable_gone.py`

- `test_pico_memory_is_working_memory_not_layered`.
- `test_working_memory_header_absent_from_prompt` (allowing the v2
  `<memory_index>` "Memory files:" listing form).
- `test_relevant_memory_section_absent`.
- `test_section_order_collapsed_to_three`.

### 4.3 · Extend `tests/test_workspace.py`

- `test_volatile_text_signature_unchanged` (no `working=` kwarg;
  output does not contain `task:` or `recent_files:`).

### 4.4 · Rewrite `tests/test_context_manager.py`

Remove:
- Any assertion on `Memory:` or `Relevant memory:` string.
- Any mock of `agent.memory.render_memory_text` /
  `agent.memory.retrieval_view` / `agent.memory.retrieval_candidates`.

Add:
- `test_prompt_section_order_is_prefix_history_current`.
- `test_workspace_state_prepended_to_history`.
- `test_no_memory_or_relevant_memory_metadata_keys`.

Modify:
- `test_context_manager_collapses_older_duplicate_reads_into_one_summary_line`:
  fixture injects
  `session["memory"]["file_summaries"] = {...}` directly (v1 read
  channel), no longer via `agent.memory.set_file_summary(...)`.

### 4.5 · `tests/memory/test_repl_v2.py`

Change `assert "Working memory:" in out` to
`assert "task:" in out and "Memory files:" in out`.

### 4.6 · `tests/test_public_api_contract.py`

Update the import path check: `LayeredMemory` remains importable from
`pico.features.memory`; new addition `WorkingMemory` importable from
`pico.working_memory`.

### 4.7 · `tests/test_pico.py`

Full inventory of affected sites (not "surgical" — this is ~9 test
cases with concrete changes):

| Line(s)     | Test / area                                                                            | Change                                                                                                                                                                                    |
|-------------|----------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| L61         | `agent.session["memory"]["files"]` flat mirror                                          | Delete assertion; new shape has no top-level `files`.                                                                                                                                     |
| L74, L77    | `agent.session["memory"]["working"]["task_summary"]`                                    | Read `agent.memory.task_summary` (attribute).                                                                                                                                             |
| L80-L108    | `test_agent_only_stores_reusable_epistemic_notes` (episodic_notes + "Relevant memory")  | **Delete entire test** — episodic-notes recall no longer exists.                                                                                                                          |
| L111-L133   | `test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling`          | **Delete** — file_summary set/read via `agent.memory` is gone; partial-stale coverage moved to L1415 rewrite.                                                                             |
| L1262-L1303 | `test_prompt_budget_metadata_records_budget_decisions` (`append_note` x3 + `relevant_memory`) | **Delete** — relevant_memory section removed; metadata key gone.                                                                                                                          |
| L1326-L1345 | `test_agent_creates_checkpoint_when_context_reduction_happens...` (`append_note` + `memory`/`relevant_memory` budgets) | Rewrite: remove `append_note`; change budget dict to `{prefix, history}`; drop metadata assertions on `memory`/`relevant_memory`.                                                          |
| L1336, L1596 | Repeated same pattern                                                                  | Same fix.                                                                                                                                                                                 |
| L1415-L1456 | `test_resume_invalidates_stale_file_summaries_and_marks_partial_stale`                   | Rewrite: seed `session["memory"]["file_summaries"] = {...}` directly; assert `resume_status == "partial-stale"` through the rewritten `Pico.invalidate_stale_memory` path (§2 C3).         |
| L1734-L1754 | `test_partial_success_creates_process_note_for_exploration_history`                     | **Delete** — `record_process_note_for_tool` removed (§2 C3).                                                                                                                              |
| L1747       | `agent.memory.to_dict()["episodic_notes"]` inside above test                            | Removed by the delete above.                                                                                                                                                              |

Total: 4 deletes + 5 rewrites. Commit 7 in §5.2 is a substantial
rewrite of ~250 LOC across `test_pico.py`, not a small patch.

### 4.8 · `tests/test_memory.py`

File retained. Any assertion that `Pico.memory` is a `LayeredMemory`
is rewritten to construct a standalone `LayeredMemory` instance.

### 4.9 · Regression fences (must stay green)

- `tests/memory/test_v1_durable_gone.py` (including new lines).
- `tests/memory/test_invariants.py` (INV-1..5).
- `tests/memory/test_cli_diagnostics_v2.py` (doctor hint text + json).
- `tests/memory/test_prompt_layout.py::test_stable_prefix_no_branch_content`.
- `test_agent_records_model_cache_metadata_in_last_prompt_metadata`.

### 4.10 · Named pre-existing red tests (stay red, must still collect)

- `tests/test_allowed_tools.py::test_benchmark_evaluator_applies_allowed_tools_to_runtime_prompt`.
- `tests/test_evaluator.py::test_benchmark_verifier_runs_with_reproducibility_locale`.

After the spec, `pytest tests --co -q` must succeed with zero
collection errors.

### 4.11 · Coverage sanity (pre-merge grep gates)

```bash
# Gate 1: LayeredMemory references must be confined to whitelist
grep -rn "LayeredMemory" pico/ tests/ --include='*.py' | \
  grep -v "features/memory.py\|test_memory.py\|test_recovery_cli.py\|metrics_experiments.py\|test_v1_durable_gone.py\|test_public_api_contract.py\|evaluator.py\|_v1_view.py"
# Expect: 0 hits.
# Whitelist rationale:
#   - features/memory.py, test_memory.py, test_recovery_cli.py: v1 kept in-place
#   - metrics_experiments.py, evaluator.py, _v1_view.py: use _v1_view helper (§2 C9/C10)
#   - test_v1_durable_gone.py, test_public_api_contract.py: assertion strings referencing v1

# Gate 2: v1 prompt headers must be gone from runtime prompt path
grep -rn "Working memory:\|Relevant memory:" pico/ --include='*.py' | grep -v "features/memory.py"
# Expect: 0 hits.
# features/memory.py:347,361 still contain the v1 header strings inside
# LayeredMemory.render_memory_text; that class is not called by any prompt
# path after §2 C3, so those strings are dormant.

# Gate 3: session["memory"] access constrained to known channels
grep -rn 'session\["memory"\]\|session\.get("memory"' pico/ --include='*.py'
# Expect hits ONLY in:
#   - runtime._ensure_session_shape (compat-read + pop)
#   - runtime.invalidate_stale_memory (file_summaries channel)
#   - context_manager._reusable_file_summary (file_summaries channel)
#   - checkpoint.py create/restore (mixed shape)
#   - evaluator.py / metrics_experiments.py (via _v1_view helper)
```

### 4.12 · TDD sequence (feeds the plan)

1. Write `test_working_memory.py` RED → implement `WorkingMemory` GREEN.
2. Extend `test_v1_durable_gone.py` with 4 assertions RED → retype
   `Pico.memory` + collapse `SECTION_ORDER` + retarget REPL `/memory`
   GREEN (some legacy tests will be red during this step).
3. Fix `test_context_manager.py` and `test_workspace.py` GREEN.
4. Fix `test_repl_v2.py:94` GREEN.
5. Fix `test_public_api_contract.py` GREEN.
6. Delete `test_partial_success_creates_process_note_for_exploration_history`;
   rewrite the partial-stale test GREEN.
7. Hardcode `initial_episodic_notes_empty = True` in `evaluator.py`;
   flatten `benchmarks/coding_tasks.json` budgets GREEN.
8. Full suite: 2 pre-existing red; all others GREEN.

---

## 5 · Risks, rollout, out-of-scope

### 5.1 · Risks

**R1 — Mixed-shape checkpoint payload is not aesthetic.**
Explicit tradeoff. Comment the schema in `checkpoint.py` header; a
follow-up spec unifies partial-stale.

**R2 — Prompt cache stability.**
`workspace_state` moves to the head of the `history` section, keeping
the stable prefix byte-identical. Regression fence: `test_stable_prefix_no_branch_content` and
`test_agent_records_model_cache_metadata_in_last_prompt_metadata`.

**R3 — `initial_episodic_notes_empty` is now a constant.**
Documented in `evaluator.py`. Not user-facing.

**R4 — Delegate context injection weaker.**
Child agent no longer receives parent transcript excerpts in its
memory notes. Task summary still crosses. If regression surfaces, a
separate spec designs a v2 `<parent_context>` block.

**R5 — Run report golden diff.**
Accepted. Single dedicated commit re-baselines the two golden JSONs
under `benchmarks/results/`.

**R6 — Stray v1 references in `test_pico.py`.**
`grep` gates at 4.11 backstop the surgical changes. Any leakage
becomes a hot-fix mini-PR.

**R7 — `metrics_experiments.py` needs the `_v1_view` rewrite per §2 C10.**
Import-only sanity check is insufficient; the `run_*_experiment()`
callables must be exercised. Acceptance: at least one experiment run
per family (`_run_memory_ablation_v2`, `_run_recovery_experiment`,
`_run_reproducibility_experiment`) completes without AttributeError
after commit close.

**R8 — Reduction floor tight.**
`DEFAULT_SECTION_BUDGETS` sums exactly to `DEFAULT_TOTAL_BUDGET`
(15000). Once user_message is appended, the assembled prompt is
`>=15000 + len(user_message)`, so the reduction loop fires every
turn. Add `prefix` and `history` `DEFAULT_SECTION_FLOORS` values
(1200 and 1500) sum to 2700 — reduction has ~12300 chars of runway.
Acceptable but tight; document in `context_manager.py` comment.

**R9 — Delegate task summary propagation is untested.**
`spawn_delegate` writes `child.memory.set_task_summary(task)` inside
`Pico.__init__` flow. Confirm order: `_ensure_session_shape` runs
during `Pico.__init__`; `set_task_summary` runs after child
construction returns to `spawn_delegate`. Order is safe. Add a test
`test_spawn_delegate_carries_task_summary` in `test_pico.py` that
asserts child agent has non-empty `child.memory.task_summary`.

### 5.2 · Rollout & commit sequence

Base: current `memory` branch. Target: single PR into `main` after
PRs #1 and #2 merge.

Commit sequence (feeds SDD one-commit-per-task cadence):

1. `feat: add pico/working_memory.py` + unit tests.
2. `feat: add memorylib.invalidate_stale_file_summaries_dict` +
   `_v1_view` helper for evaluator/metrics_experiments.
3. `refactor: runtime + agent_loop switch Pico.memory to WorkingMemory`
   (includes `invalidate_stale_memory` rewrite, delegate rewire,
   `record_process_note_for_tool` deletion).
4. `refactor: context_manager collapses to 3 sections; workspace_state
   → history head; _reusable_file_summary reads session channel`.
5. `refactor: checkpoint mixed shape (working_memory + legacy
   file_summaries); create_checkpoint reads attribute not to_dict`.
6. `refactor: REPL /memory prints task/recent + memory files (no XML)`.
7. `refactor: evaluator + metrics_experiments use _v1_view helper`
   (touches evaluator.py L323/L328/L341-344/L479-482 and
   metrics_experiments.py L68/L131-145/L228-242/L326/L805/L1049-1091).
8. `chore: benchmarks/coding_tasks.json budget merge for
   context_reduction_checkpoint`.
9. `test: rewrite test_context_manager, test_public_api_contract,
   test_repl_v2; extend test_v1_durable_gone, test_workspace`
   (targeted edits).
10. `test: rewrite test_pico.py — 4 deletes + 5 rewrites per §4.7
    inventory` (~250 LOC touched; single dedicated commit).
11. `docs: update memory-model.md + README /memory copy`.
12. `chore: re-baseline benchmarks/results/*.json` (isolated commit).

**Red-band expectation**: commits 3-9 leave `pytest tests -q` red;
green is restored only at commit 10 (test_pico rewrite is the final
piece unblocking the suite). Commits 1, 2, 11, and 12 are green on
their own. This is a deliberate revision of the earlier "each commit
passes" claim.

### 5.3 · Rollback

- Preferred: forward fix (v1 code still present; type switch reverts
  cheaply).
- Full revert: `git revert <merge-commit>` — session/checkpoint
  compat channels ensure smooth downgrade.
- Observability: run `pico-cli benchmark memory` scaffold to catch
  ImportError or collection error; run
  `python -c "import pico.evaluation.metrics_experiments"`.

### 5.4 · Out of scope reminder

See §1. Each item is a follow-up spec.

### 5.5 · Acceptance checklist

- [ ] `type(Pico(...).memory).__name__ == "WorkingMemory"`.
- [ ] `grep -rn "Working memory:\|Relevant memory:" pico/` returns 0.
- [ ] `SECTION_ORDER == ["prefix", "history", "current_request"]`.
- [ ] `<workspace_state>` renders at the head of the `history` section.
- [ ] `pico-cli sessions show <legacy-session-id>` reads v1 shape without crash.
- [ ] REPL `/memory` prints `task:`, `recent:`, `Memory files:`.
- [ ] `pico-cli doctor --format text` still emits the CLAUDE.md hint.
- [ ] `import pico.evaluation.metrics_experiments` no ImportError.
- [ ] `import pico.evaluation.evaluator` no ImportError.
- [ ] `run_fixed_benchmark(Path("benchmarks/coding_tasks.json"), ...)` completes without AttributeError.
- [ ] At least one `_run_*_experiment(...)` call in `metrics_experiments` completes without AttributeError.
- [ ] `pytest tests --co -q` reports 0 collection errors.
- [ ] `pytest tests -q` reports the two named pre-existing failures and no others.
- [ ] `prompt_cache_key` stable across two consecutive turns with no file changes.
- [ ] `test_resume_invalidates_stale_file_summaries_and_marks_partial_stale` green.
- [ ] `test_spawn_delegate_carries_task_summary` (new, per R9) green.
- [ ] `Pico.invalidate_stale_memory` operates on session file_summaries channel (verified via partial-stale test).
- [ ] `create_checkpoint` uses `agent.memory.recent_files` attribute (not `to_dict()["working"]`).
