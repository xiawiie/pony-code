# Retire v1 Memory · Design Spec

**Status:** Draft
**Date:** 2026-07-03
**Branch:** `memory`
**Predecessor spec:** `docs/superpowers/specs/2026-07-02-pico-memory-v2-design.md`
**Related PRs:** #1 (cli→main draft), #2 (memory→cli)

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

- Deleting `pico/features/memory.py` outright.
- `pico/evaluation/metrics_experiments.py` v1 memory migration
  (74 refs — needs separate scope).
- Partial-stale mechanism redesign; the `file_summaries` v1 channel
  survives this spec.
- Delegate context-injection redesign.
- `test_pico.py` (1864 LOC) split.
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
- `file_freshness` invocations at lines 76 and 154 are unchanged.

### C9 · `pico/evaluation/evaluator.py`

- Hardcode `initial_episodic_notes_empty = True` (do not read `agent.memory.episodic_notes`, which no longer exists on `WorkingMemory`).
- No other change.

### C10 · `pico/evaluation/metrics_experiments.py`

No changes. All 74 v1 references continue to construct `LayeredMemory`
directly. Migration is a separate spec.

### C11 · `benchmarks/coding_tasks.json`

`context_reduction_checkpoint.setup.section_budgets`:

```
Before: {"prefix": 800, "memory": 800, "relevant_memory": 800, "history": 2400}
After:  {"prefix": 800, "history": 4000}
```

Total budget (4800) and verifier contract unchanged.

### Dependency graph after change

```
runtime      → WorkingMemory
agent_loop   → WorkingMemory (via runtime)
context_mgr  → (no memory dep)
workspace    → (unchanged)
REPL /memory → WorkingMemory.to_dict + BlockStore.list

checkpoint   → WorkingMemory.to_dict / from_dict
             → features.memory.file_freshness   (kept)
             → session["memory"]["file_summaries"]   (v1 read-compat channel)

evaluator      → (no v1 read; hardcoded True field)
metrics_exp    → features.memory.LayeredMemory   (kept intact)
tests          → WorkingMemory + LayeredMemory (partial-stale only)
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

Surgical changes:
- Delete `test_partial_success_creates_process_note_for_exploration_history`.
- Rewrite `test_resume_invalidates_stale_file_summaries_and_marks_partial_stale`
  to inject `session["memory"]["file_summaries"]` directly rather
  than through `agent.memory.set_file_summary(...)`.
- Remove all assertions on
  `agent.memory.render_memory_text() / .retrieval_view() /
  .retrieval_candidates() / .append_note()`.

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
grep -rn "LayeredMemory" pico/ tests/ --include='*.py' | \
  grep -v "features/memory.py\|test_memory.py\|test_recovery_cli.py\|metrics_experiments.py"
# Expect: 0 hits.

grep -rn "Working memory:\|Relevant memory:" pico/ --include='*.py'
# Expect: 0 hits.

grep -rn 'session\["memory"\]\|session\.get("memory"' pico/ --include='*.py'
# Expect: only (a) runtime._ensure_session_shape compat-read + pop,
#              (b) checkpoint file_summaries channel.
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

**R7 — `metrics_experiments.py` untouched.**
Sanity check: `python -c "import pico.evaluation.metrics_experiments"`
must succeed post-merge. All 74 v1 references remain valid because
`LayeredMemory` still exists.

### 5.2 · Rollout & commit sequence

Base: current `memory` branch. Target: single PR into `main` after
PRs #1 and #2 merge.

Commit sequence (feeds SDD one-commit-per-task cadence):

1. `feat: add pico/working_memory.py`.
2. `refactor: runtime + agent_loop switch Pico.memory to WorkingMemory`.
3. `refactor: context_manager collapses to 3 sections; workspace_state -> history head`.
4. `refactor: checkpoint keeps mixed shape (working_memory + legacy file_summaries)`.
5. `refactor: REPL /memory prints task/recent + memory files (no XML)`.
6. `chore: evaluator hardcodes initial_episodic_notes_empty; benchmarks/coding_tasks.json budget merge`.
7. `test: rewrite test_context_manager, test_public_api_contract, test_repl_v2, test_pico surgical patches`.
8. `docs: update memory-model.md + README /memory copy`.
9. `chore: re-baseline benchmarks/results/*.json` (isolated commit).

Commits 1 and 9 are green on their own. Commits 2–3 may leave the
suite transiently red between check-ins; the plan runs the full
suite green only at commit 7. This is a deliberate expectation shift
from the earlier "each commit passes" claim.

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
- [ ] `pytest tests --co -q` reports 0 collection errors.
- [ ] `pytest tests -q` reports the two named pre-existing failures and no others.
- [ ] `prompt_cache_key` stable across two consecutive turns with no file changes.
- [ ] `test_resume_invalidates_stale_file_summaries_and_marks_partial_stale` green.
