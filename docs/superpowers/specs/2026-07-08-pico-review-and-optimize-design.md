# Pico Post-Migration Review & Optimize — Design Spec

Date: 2026-07-08
Status: Draft — awaiting user review before writing implementation plan
Baseline: `0189780` (branch `memory`, after critical injection fix from the
prior final review)

---

## 1 · Motivation

The prior memory/context redesign (28 tasks) shipped with a final code review
that surfaced **15 findings**. Only 1 CRITICAL got fixed in the same session
(injection subsystem was inert in the live runtime). The remaining findings
break down as:

- **2 IMPORTANT deferred**: history-message budget never implemented; `pico.toml`
  keys silently unread
- **12 MINOR**: correctness/security hardening, perf, observability gaps,
  test hygiene, and 8 minor items previously deferred per-task

The prior review also flagged that the E2E test story is thin — the only
sniffer-provider test is the one added with the CRITICAL fix. There is no
integration test that combines injection + digest + recall in one turn.

**This spec's mandate**: close all 14 remaining findings, add exhaustive
E2E coverage, and stand up a stdlib-only latency benchmark harness — while
staying aligned with the codebase's actual patterns rather than inventing
new ones.

## 2 · Non-Goals

- Not rewriting `_MemoryExperimentModelClient` — the third `legacy_string_path`
  skipped test (`test_metrics.py::test_run_memory_ablation_v2_writes_expected_artifact`)
  depends on evaluation-harness internals; rewriting it is a separate spec.
- Not switching pico.toml from the current custom parser to full `tomllib`
  wholesale — `tomllib` is added as an **optional** upgrade path with
  fallback to the existing simple parser (see §5.1).
- Not implementing `intent_profiles_overrides` from spec §10 of the prior
  design — spec §10 originally proposed nested-dict intent overrides
  (`[context.intent.debug] keywords = [...]`), but the current pico.toml
  parser (and reasonable UX) cannot support this cleanly. The intent
  keywords stay in-code; users who want different keywords can PR a change.
- Not adding a runtime dual-write drift assertion — the prior spec's
  Finding 11 mitigation is instead an optional CLI inspector (see §4.5).
- Not enforcing benchmark absolute-time thresholds in CI — benchmarks run
  manually, emit JSON for local comparison, and are not gate-blocking.
- Not upgrading `requires-python` beyond a minor bump to `>=3.11` (needed
  for the optional `tomllib` path).

## 3 · Architecture Overview: Six Streams

The work partitions into six independent streams. Each stream is a
sequence of atomic tasks with its own gate; a subagent can execute one
stream from start to finish without holding context on the others.

| Stream | Focus | Tasks | Depends on |
| -- | -- | -- | -- |
| **A** | Correctness & Safety | 5 (A1-A5) | Independent |
| **B** | Configuration surface | 7 (B1-B7) | A1 (needs history_soft_cap wiring point) |
| **C** | Observability & Consistency | 6 (C1-C6) | B (config supplies injection_budget_ratio) |
| **D** | Performance | 7 (D1-D7) | Independent (D1/D2 close findings; D3-D7 bench harness) |
| **E** | Testing (E2E + legacy rewrites) | 7 (E1-E5, E7-E9) | A/B/C/D landed |
| **F** | Docs alignment | 3 (F1-F3) | Everything else landed |

**Total: 35 atomic tasks.** Each task follows TDD: failing test → verify
fail → minimal implementation → verify pass → commit.

### 3.1 Alignment with existing code patterns

Every design choice below explicitly maps to a pattern already used in
`pico/`:

- **Config extension**: new `context_*` / `memory_*` helper functions in
  `pico/config.py` (not a new module) — same shape as existing
  `project_max_blob_size`.
- **Fallback-first parsing**: pico's custom `load_pico_toml` stays as the
  fallback; a new `load_pico_toml_full` prefers `tomllib` when the
  installed Python has it.
- **Runtime wiring**: `Pico.__init__` collects config values once and
  passes them as kwargs to `ContextManager` / `Retrieval` /
  `recall_for_turn`. No shared mutable "context" object.
- **Benchmarks**: new `benchmarks/perf/` subdir mirrors the existing
  `benchmarks/memory_quality/` layout (scenario + runner + README);
  the two suites serve different purposes (quality gate vs latency).
- **Tests**: E2E tests use the existing `_SniffProvider` pattern from
  `tests/test_agent_loop_injection_sent.py` — no new mocking framework.

## 4 · Stream A · Correctness & Safety

### 4.1 A1 — Turn-based history budget

**Problem**: `ContextManager.build_v2` copies `session["messages"]` verbatim.
Long sessions grow unbounded until Anthropic returns 413.

**Contract**:

- After copying messages, if `sum(_estimate_tokens(m))` exceeds
  `history_soft_cap` (config-driven, default 40000), drop **oldest turn
  units** until under cap, subject to a floor.
- **Turn unit** = one top-level user question plus every message it
  triggered before the next top-level user question. A turn unit contains
  0 or more `(assistant.tool_use, user.tool_result)` pairs and typically
  ends with an `assistant.text` final answer.
- **Floor**: the last `history_floor_messages` messages (default 6) are
  never dropped — even if that means the request exceeds soft_cap. The
  floor guarantees the model always sees the most recent context.
- **Pairing invariant**: within a dropped turn unit, all
  `(tool_use.id, tool_result.tool_use_id)` pairs must both be dropped
  together. No orphan tool_use blocks reach the provider.

**Detection**: `_pico_meta.tool_use_id` (present on both assistant.tool_use
and user.tool_result messages, added by Task 6/7).

**Telemetry**: `metadata["dropped_messages"]` = int count of messages
dropped; `metadata["messages_tokens"]` = estimated tokens after drop.

**Tests**:
- `test_history_soft_cap_respected` — 50 messages > cap → drop happens
- `test_floor_never_dropped` — floor=6, 100 messages total, cap=0 → still returns last 6
- `test_multi_tool_use_turn_drop_atomicity` — a turn with 3 tool_use pairs → all 3 dropped together
- `test_orphan_tool_use_never_produced` — after drop, every assistant.tool_use.id has a matching user.tool_result

### 4.2 A2 — `strip_pico_meta` helper for provider payloads

**Problem**: Provider adapters currently avoid `_pico_meta` in the payload
by picking only `role`/`content` when reconstructing dicts. This is a
brittle "avoid the field" pattern — a future adapter that JSON-dumps the
whole message would leak `_pico_meta` into the wire.

**Contract**:

- New module `pico/providers/message_utils.py`:
  ```python
  def strip_pico_meta(messages: list[dict]) -> list[dict]:
      """Return a new list of messages with _pico_meta keys removed.
      Deep enough: {role, content} shallow copy; content list-of-blocks
      is shared (blocks never carry _pico_meta)."""
  ```
- `AnthropicCompatibleModelClient.complete_v2` calls it before building
  the payload.
- `FallbackAdapter.complete_v2` calls it before flattening.
- `strip_pico_meta` is idempotent on already-stripped messages.

**Tests**: `test_strip_pico_meta_removes_key`, `test_strip_pico_meta_leaves_role_content_intact`, `test_strip_pico_meta_idempotent`.

### 4.3 A3 — Token estimate for `tools` uses JSON serialization

**Problem**: `context_manager.py:311` — `str(tools)` uses Python repr
(single quotes), off ~2× from the JSON wire format the provider actually
sees. `metadata["tools_tokens"]` is misleading.

**Contract**: Replace `str(tools)` with `json.dumps(tools, sort_keys=False)`.
One-line change. Update `test_build_v2_metadata_includes_system_and_tools_tokens`
to compute the expected value with the same `json.dumps`.

### 4.4 A4 — Session backup timestamp in nanoseconds

**Problem**: `session_store._write_backup` uses `int(time.time())`. Two
backups within the same wall-clock second collide on filename.

**Contract**: Use `time.time_ns()` for the backup filename suffix. Update
the existing `test_migrator_writes_backup` to accept the ns-precision
filename glob (`<id>.v1.*.json` — glob pattern still matches).

**Test**: `test_backup_within_same_second_produces_distinct_files` —
back-to-back migrations of two sessions produce two distinct backup files.

### 4.5 A5 — Optional CLI session inspector (in lieu of dual-write assertion)

**Problem**: Finding 11 asks for a dual-write drift check between
`session["messages"]` and `session["history"]`. A runtime assertion is
too risky (breaks tests if env is misconfigured); the assertion's value
window is narrow (the bridge is transitional).

**Contract**: Add a **read-only CLI subcommand**
`pico-cli session inspect <session_id>` that:

- Loads the session
- Counts user/assistant turns in both `session["messages"]` and
  `session["history"]`
- Reports mismatches to stdout with severity classification
- Exit 0 on match, exit 1 on mismatch (for CI use)
- Never mutates the session

**Files**: extend `pico/cli_commands.py` and add a `handle_session`
subcommand. Test: `tests/test_cli_session_inspect.py`.

## 5 · Stream B · Configuration Surface

### 5.1 B1 — `load_pico_toml_full` with tomllib preference

**Problem**: Current parser doesn't support arrays or nested dicts. Some
config keys (e.g., `field_boost.*`) benefit from nested-dict readability.

**Contract**:

- New function `pico/config.py::load_pico_toml_full(root)`:
  1. Try `import tomllib` (Python 3.11+)
  2. If available: parse with tomllib → return the nested dict as-is
  3. If not: fall back to `load_pico_toml(root)` (existing simple parser)
- Existing `load_pico_toml` **untouched** — all existing callers continue
  to work.
- `pyproject.toml`: bump `requires-python` from `>=3.10` to `>=3.11` so
  the tomllib path is always available. This is the "graduation" the
  original author's comment in `config.py:138` explicitly anticipated.

**Tests**: `test_load_pico_toml_full_uses_tomllib_when_available`,
`test_load_pico_toml_full_handles_arrays`, `test_load_pico_toml_full_returns_empty_for_missing_file`.

### 5.2 B2-B7 — Per-domain helper functions in `pico/config.py`

Following the `project_max_blob_size(root)` pattern already in `config.py`,
add **8 independent helper functions**, each with its own default fallback:

```python
# in pico/config.py
def context_history_soft_cap(root) -> int:                # default 40000
def context_history_floor_messages(root) -> int:          # default 6
def context_injection_budget_ratio(root) -> float:        # default 0.15
def context_system_tools_hard_cap(root) -> int:           # default 20000
def context_digest_size_threshold(root) -> int:           # default 1200
def memory_recall_config(root) -> dict:                   # min_score, top_k, max_tokens_per_note, skip_recent_turns
def memory_field_boosts(root) -> dict[str, float]:        # name, description, tags, aliases, body
def memory_link_config(root) -> tuple[int, float]:        # (max_added, decay)
```

Each helper:
- Reads via `load_pico_toml_full(root)`
- Falls back to the current hard-coded default if key missing / bad type
- Never raises on malformed input — logs a `warnings.warn` at most

**Runtime wiring** (`Pico.__init__`):
```python
self.context_config = {
    "history_soft_cap": context_history_soft_cap(self.root),
    "history_floor_messages": context_history_floor_messages(self.root),
    "injection_budget_ratio": context_injection_budget_ratio(self.root),
    "system_tools_hard_cap": context_system_tools_hard_cap(self.root),
    "digest_size_threshold": context_digest_size_threshold(self.root),
    "recall": memory_recall_config(self.root),
    "field_boosts": memory_field_boosts(self.root),
    "link": memory_link_config(self.root),
}
```

`ContextManager` reads via `self.agent.context_config[...]`. `Retrieval`
gets `field_boosts` and `link_config` via a new optional kwarg on its
constructor. `recall_for_turn` reads recall config via
`agent.context_config["recall"]`.

**Task breakdown**:
- **B2**: `context_history_soft_cap`, `context_history_floor_messages`,
  `context_injection_budget_ratio`, `context_system_tools_hard_cap` +
  wire into `ContextManager.build_v2`.
- **B3**: `context_digest_size_threshold` + wire into `_append_tool_result`.
- **B4**: (skipped — intent overrides deliberately dropped, see §2 non-goals)
- **B5**: `memory_recall_config` + wire into `recall_for_turn`.
- **B6**: `memory_field_boosts` + `memory_link_config` + wire into
  `Retrieval.__init__` (add optional `config=None` kwarg).
- **B7**: End-to-end integration test — write a full `pico.toml` covering
  all keys, spin up `Pico`, verify each override takes effect.

## 6 · Stream C · Observability & Consistency

### 6.1 C1 — Implement `injection_dropped` (Finding 5)

**Problem**: `renderer.py` initializes `injection_dropped: []` and never
mutates it. Spec §4.4.3 said: when sum of source tokens exceeds injection
budget, drop least-important sources first.

**Contract**:

- New constant `DROP_PRIORITY = ("checkpoint", "project_structure", "memory_index", "workspace_state", "recalled_memory")` — ordered least-important
  first.
- After rendering all blocks, if `sum(injection_tokens.values()) >
  injection_budget` (computed from `total_budget * injection_budget_ratio`),
  drop blocks by `DROP_PRIORITY` until under budget.
- Each dropped source: remove from `blocks` list, zero out
  `injection_tokens[source]`, append source name to `injection_dropped`.
- `recalled_memory` is last in `DROP_PRIORITY` because spec §4.4.3 marked
  it decision-critical.

**Test**: `test_injection_drops_checkpoint_before_recalled_memory` —
construct sources that all exceed budget; assert checkpoint dropped,
recalled_memory retained.

### 6.2 C2 — Recall errors reach telemetry (Finding 9)

**Problem**: `render_recalled_memory` in `sources.py` swallows all
exceptions silently.

**Contract**:

- `render_recalled_memory` catches exceptions but **also** records them:
  ```python
  counters = agent.session.setdefault("_recall_errors", {"count": 0, "last": ""})
  counters["count"] += 1
  counters["last"] = f"{type(exc).__name__}: {exc}"[:200]
  ```
- `ContextManager.build_v2` copies these into metadata as
  `recall.error_count` and `recall.last_error`.

**Test**: `test_recall_error_recorded_to_telemetry` — patch
`recall_for_turn` to raise, verify `metadata["recall.error_count"] >= 1`.

### 6.3 C3 — Debug-only logging hooks

**Problem**: Multiple silent failure paths (source renderers,
digest fallback, session migrator unknown role).

**Contract**: Add `logger = logging.getLogger("pico")` in five files:
`agent_loop.py`, `context/renderer.py`, `context/sources.py`, `memory/recall.py`, `session_store.py`.

Log at `logger.debug(...)` in existing catch blocks (do not change
behavior). Users opt in via `logging.basicConfig(level=logging.DEBUG)`.

**Test**: `test_recall_failure_logs_debug` using `caplog` fixture.

### 6.4 C4 — `intent.matched_reason` in telemetry

**Problem**: Spec §9 promised `intent.matched_reason`; renderer only
populates `intent.matched_keyword`.

**Contract**: In `renderer.py`, add
`matched_reason = f"keyword:'{matched_keyword}' via profile:{name}"` (or
`"default (no keyword)"` when no match). Include in telemetry.

**Test**: `test_intent_matched_reason_populated`.

### 6.5 C5 — Metadata completeness gate

**Problem**: Spec §9 lists 15+ fields but there's no single test asserting
they're all present.

**Contract**: New test `test_metadata_completeness` that runs
`build_v2` with a rich mock agent and asserts every field in this list is
present in metadata:
```
system_cache_key, system_tokens, tools_tokens,
messages_count, messages_tokens, cache_control_breakpoints,
injection_tokens, injection_truncated, injection_dropped,
intent, intent.name, intent.matched_keyword, intent.matched_reason,
recall.error_count, recall.last_error,
dropped_messages, prompt_cache_key
```

`cache_creation_input_tokens` / `cache_read_input_tokens` come from
provider response, tested separately in Anthropic adapter tests.

### 6.6 C6 — Unify legacy `history_text()` behavior for empty case

**Problem**: `runtime.history_text()` returns `"- empty"` when empty
(reads legacy `session["history"]`).

**Contract**: Not blocking — leave as-is with a docstring comment noting
this is the transitional bridge shape. **This task is a doc-only
adjustment**; primarily to close the loop on Finding 6.

## 7 · Stream D · Performance

### 7.1 D1 — Single-call `digest_tool_result` (Finding 8)

**Problem**: `_append_tool_result` calls `digest_tool_result` twice: once
for the source_hash, once with raw_path filled in.

**Contract**: `ToolResultDigest` stays `frozen=True`. Use
`dataclasses.replace(digest, raw_path=raw_path_str)` after writing the
raw file. Confirms via a mock counter that the per-tool summarizer
function runs exactly once per large tool_result.

**Test**: `test_digest_computed_exactly_once` — patch `_digest_read_file`
with a MagicMock; run `_append_tool_result` with a large read_file
payload; assert the mock was called once.

### 7.2 D2 — Recall builds a store index once per call (Finding 12)

**Problem**: `_lookup_type(store, path)` calls `store.list()` for every
recalled note.

**Contract**: `recall_for_turn` builds `store_index = {entry.path: entry}`
at the top; `_lookup_type` becomes `_lookup_type(store_index, path)`.
Function-local memoization — no global cache.

**Test**: `test_recall_uses_single_store_scan` — patch
`BlockStore.list` with counter; run recall with top-k=2; assert
`list()` invoked once.

### 7.3 D3 — Benchmark harness (`benchmarks/perf/`)

**Directory layout**:
```
benchmarks/
├── memory_quality/   (existing — scenario-based correctness gate)
└── perf/             (new — latency)
    ├── README.md
    ├── harness.py    (stdlib time.perf_counter_ns + statistics)
    ├── bench_build_v2.py
    ├── bench_retrieval.py
    └── bench_recall.py
```

**`harness.py`** provides:
```python
def bench(name, fn, iterations=100, warmup=5) -> dict:
    """Return {name, iterations, median_ns, p95_ns, min_ns}."""
```

- Stdlib only (`time.perf_counter_ns`, `statistics`)
- Not imported by `pytest` — bench scripts run standalone via
  `uv run python -m benchmarks.perf.bench_build_v2`
- Emits JSON to `benchmarks/results/perf-<script>-<ns>.json`

### 7.4 D4 — `bench_build_v2.py`

**Scenarios**: 3 session sizes (1, 30, 300 messages) × injection-on. 100
iterations each. Reports median + p95.

### 7.5 D5 — `bench_retrieval.py`

**Scenarios**: 3 note counts (10, 100, 1000). BM25 + field boost + link
expansion all on. Reports per-search latency.

### 7.6 D6 — `bench_recall.py`

**Scenarios**: 2 × 2 grid — recall history empty/full × 10/100 notes.
Reports per-turn recall latency. **Run before and after D2** to quantify
the store-index optimization gain.

### 7.7 D7 — Perf README

`benchmarks/perf/README.md`: how to run, what each script measures, when
to re-run (e.g., "after changing FIELD_BOOSTS", "after adding an injection
source"), how the JSON output is structured.

**Explicit non-goal**: perf benchmarks do NOT run in CI. They're a manual
tool for local before/after comparison.

## 8 · Stream E · Testing

### 8.1 E1 — Full-turn round-trip E2E (`tests/e2e/test_full_turn_roundtrip.py`)

**Scenario**: Sniffer provider + pre-seeded `.pico/memory/agent/cache.md`
matching "cache" keyword + first tool call returns 5KB output.

**Assertions**:
- Turn 1 provider receives injection blocks (workspace_state, memory_index, recalled_memory)
- Tool result >1200 chars → digested; raw file written to
  `runs/<run_id>/tool_results/<hash>.txt`
- Turn 2 provider receives the digested tool_result in history (not the
  raw 5KB)
- `session["_recall_errors"]["count"] == 0`
- `metadata["injection_tokens"]["recalled_memory"] > 0`

### 8.2 E2 — History budget trigger E2E

**Same file**. Construct 50 messages including 3 tool_use pairs. Enable
`history_soft_cap = 5000`. Run `ContextManager.build_v2`.

**Assertions**:
- Total tokens in returned messages ≤ 5000 + one-message-slop
- Floor (default 6) messages preserved
- No orphan tool_use in returned messages
- `metadata["dropped_messages"] > 0`

### 8.3 E3 — FallbackAdapter parity E2E (`tests/e2e/test_fallback_provider_parity.py`)

Same user input runs through:
1. `_SniffProvider` (native v2)
2. `FallbackAdapter(_XmlStubInner)` (fallback path)

**Assertions**:
- Both paths produce a valid `Response` with `stop_reason == END_TURN`
- Both paths' provider input contains `<pico:*>` blocks (native as-is,
  fallback as flattened text)

### 8.4 E4-E5 — Rewrite 2 legacy_string_path tests

- **E4**: `test_runtime_report::test_resume_prompt_uses_checkpoint_state_not_just_history`
  → assert `messages[-1].content` contains `<pico:checkpoint>` block
  with `current_goal` / `next_step` fields. Remove skip marker.
- **E5**: `test_runtime_report::test_recent_transcript_entries_stay_richer_than_older_ones`
  → assert `messages` array preserves last 6 messages verbatim; older
  tool_results carry `[digest]` prefix. Remove skip marker.

**Note**: The third legacy_string_path test (`test_metrics.py`) is
explicitly deferred — it depends on evaluation-harness internals.
Add a docstring TODO pointing to a follow-up spec.

### 8.5 E7 — Test hygiene补强

Three underspecified assertions from the prior review:
- `test_build_v2_metadata_contains_system_cache_key` — assert exact
  `hashlib.sha256(system_text.encode()).hexdigest()` value, not just
  length=64.
- `test_fallback_last_completion_metadata_mirrors_inner` — use a stub
  that returns different metadata across calls; verify mirroring, not
  accumulation.
- `test_agent_loop_e2e_v2::test_end_to_end_tool_call_then_final` — add
  assertion that `provider.calls[0]["messages"][-1]["content"]` contains
  `<system-reminder>`.

### 8.6 E8 — Task 15 minor sweep

Add missing assertions from the per-task minor list:
- `test_session_store_migrator`: add exact `_pico_meta.created_at` /
  `tool_use_id` assertions
- Add `test_int_schema_maps_to_integer` for tool schema conversion in
  `_convert_pico_tool_to_anthropic`
- Add `test_append_tool_use_result_meta_fields` for `_pico_meta` on
  tool_use/tool_result messages
- Add `test_migrator_idempotent_returns_v2_verbatim` asserting round-trip
  equality on already-v2 sessions

### 8.7 E9 — Property-style additions

- `test_message_immutability` — 3 turns, verify prior message bytes
  never change
- `test_recently_recalled_deque_bounded` — recall N turns, verify
  `session["recently_recalled"]` length ≤ `RECALL_SKIP_RECENT_TURNS + 1`
- `test_pico_meta_never_in_provider_payload` — sniffer provider assert
  no `_pico_meta` in received message dicts

## 9 · Stream F · Docs Alignment

### 9.1 F1 — `CONTEXT.md` config section

Append a "pico.toml Configuration Surface" section listing every key
introduced in Stream B with its default and rationale.

### 9.2 F2 — Update `docs/memory-model.md`

- Note that `agent_notes.md` is legacy (post-migration path)
- Describe recall four guards + digest workflow
- Mention `history_soft_cap` and how long sessions are managed

### 9.3 F3 — Update prior redesign spec

Add a "Post-review update (2026-07-08)" section to
`docs/superpowers/specs/2026-07-07-pico-memory-context-redesign-design.md`
that lists which findings this spec addressed, which stayed deferred,
and where to find the follow-up work.

## 10 · Findings Coverage Matrix

| Finding | Severity | Addressed by |
| -- | -- | -- |
| 1 (injection dead) | CRITICAL | Already fixed (commit 0189780 + regression tests) |
| 2 (history budget) | IMPORTANT | Stream A1 + E2 |
| 3 (pico.toml unread) | IMPORTANT | Stream B (B1-B7) |
| 4 (`_pico_meta` leak) | MINOR | Stream A2 + E9 |
| 5 (`injection_dropped` empty) | MINOR | Stream C1 |
| 6 (`history_text` returns "- empty") | MINOR | Stream C6 (doc-only) |
| 7 (tools_tokens repr not JSON) | MINOR | Stream A3 |
| 8 (digest double-call) | MINOR | Stream D1 |
| 9 (recall silent catch) | MINOR | Stream C2 + C3 |
| 10 (backup timestamp collision) | MINOR | Stream A4 |
| 11 (dual-write drift) | MINOR | Stream A5 (CLI, not runtime assert) |
| 12 (recall O(N) scan) | MINOR | Stream D2 |
| 13 (escape mechanism) | ACCEPTABLE | Already verified — no work |
| 14 (legacy skip tests) | ACCEPTABLE | Stream E4-E5 rewrites 2 of 3; test_metrics deferred to independent spec |
| 15 (per-task minors) | MINOR mix | Stream E8 sweep |

## 11 · Definition of Done

1. **All 35 tasks committed** with TDD trail (test → fail → impl → pass → commit).
2. **Every Stream's gate passes** — see per-stream tests above.
3. **Full suite**: `pytest -q` shows ≥ 620 passed (baseline 596), ≤ 1 skip
   (down from 3).
4. **`benchmarks/perf/` scripts run standalone**, produce JSON, and
   README describes how.
5. **Findings coverage matrix** — every non-ACCEPTABLE finding has a
   commit closing it.
6. **Post-migration whole-branch review** run again on the final HEAD;
   verdict must be `SHIP` (or `SHIP WITH MINOR FIXES` where those minors
   are net-new discoveries, not carried over).

## 12 · Open Questions

- **Q1**: `pico-cli session inspect` (A5) — does it need a `--json`
  output mode for CI use? Deferring to implementation-time; if a test
  ergonomics case emerges, add it, otherwise plain-text is enough.
- **Q2**: `benchmarks/perf/` scripts —should they gate the whole-branch
  review? Answer: **no**, they're manual tools; but the final review
  should mention when they were last run and their JSON path.
- **Q3**: `logging.getLogger("pico")` (C3) — should we also add a
  `PICO_DEBUG=1` env → auto-`basicConfig(DEBUG)` sugar? Answer:
  defer — users can `logging.basicConfig` in their entry point; adding
  env sugar is a UX polish, not required for closure.
