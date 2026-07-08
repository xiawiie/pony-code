# Pico Live-API End-to-End Test — Design Spec

Date: 2026-07-08
Status: Draft — awaiting user review before writing implementation plan

---

## 1 · Motivation

The two prior specs (`2026-07-07-pico-memory-context-redesign` and
`2026-07-08-pico-review-and-optimize`) landed 74 commits worth of design.
Test coverage is **strong at the unit + integration layer** (668 pytest
tests) and includes 3 E2E tests using a sniffer stub provider. But the
whole design ships assumptions that only a **live provider** can prove:

1. **cache_control breakpoints actually reduce Anthropic input tokens** —
   only observable via real `cache_creation_input_tokens` /
   `cache_read_input_tokens` in provider responses.
2. **`<system-reminder>` + `<pico:*>` blocks reach a real model** without
   being mis-parsed as text — no unit test can prove this end-to-end.
3. **history budget drop, injection budget drop, digest, recall four
   guards** — all wired via a mocked provider today. A live 5-turn
   session lights up the same code paths against a real model and
   observes their emergent behavior on real content.

This spec adds ONE script — `benchmarks/live_e2e/run_live_session.py` —
that runs 5 designed turns through the real Anthropic API, hard-asserts
27+ concrete invariants, and emits a JSON report. It is **not** a
pytest test and **not** CI-gated: it consumes a real API key, produces
tokens, and requires operator intent to run.

## 2 · Non-Goals

- **Not a quality benchmark**: this does not score model output correctness.
  Correctness of `read_file` results / final answers is not asserted.
  `benchmarks/memory_quality/` already covers quality scoring.
- **Not a performance benchmark**: latency is not asserted.
  `benchmarks/perf/` covers latency benchmarks locally.
- **Not CI-gated**: script depends on live API keys + tokens.
- **Not multi-provider**: this spec covers Anthropic only. Fallback
  adapter parity is already covered by the sniffer-based
  `tests/e2e/test_fallback_provider_parity.py`.
- **Not autonomous re-tries**: API errors abort the run — the caller
  chooses whether to re-run.
- **Not a pytest test**: adding it to pytest would either make CI cost
  money (bad) or require a permanent skip marker (silent).
- **Does NOT modify the pico repo's source**: `read_only=True` enforced
  at Pico construction time. `write_file`/`patch_file`/`run_shell`
  writes are refused by `tool_executor`.
- **Does NOT touch main branch**: run only from the `memory` branch
  (or any branch containing the completed optimization stack).

## 3 · Architecture Overview

Single-file script with 5 internal components + fixtures directory:

```
benchmarks/live_e2e/
├── __init__.py
├── README.md                  # env / cost / how to run
├── run_live_session.py        # entry (main + 5 components)
├── fixtures/
│   └── seed_cache_note.md     # frontmatter+body for recall trigger
├── tests/
│   └── test_assertions.py     # unit tests for AssertionEngine
└── results/                   # gitignored except README
    ├── README.md              # explains the dir
    └── live-e2e-<ns>.json
```

Entry: `uv run python -m benchmarks.live_e2e.run_live_session`
Reset: `uv run python -m benchmarks.live_e2e.run_live_session --reset`

Components (see §7 for full API): `Config`, `FixtureManager`,
`TurnRunner`, `AssertionEngine`, `Reporter`, plus `main()`.

## 4 · Five-Turn Task Design

All five turns share one `Pico` instance. Session state accumulates.
Every turn's `agent.last_prompt_metadata` is captured immediately after
`pico.ask()` returns.

### Turn 1 — Recall trigger

- **Prompt** (verbatim): `"上次讨论过 cache invariant 的问题，帮我看看这个仓库的 cache 相关代码"`
- **Rationale**: "上次" is in the `recall` intent's keyword list. The
  seeded memory note `agent/cache-invariant.md` matches the "cache"
  keyword. The renderer's `recall_for_turn` should surface it.

### Turn 2 — Digest trigger

- **Prompt**: `"读一下 pico/runtime.py 完整内容并告诉我它主要做什么"`
- **Rationale**: `pico/runtime.py` is ~800 lines / 30 KB. The
  `read_file` tool_result output easily exceeds the
  digest threshold. Fixture pico.toml sets
  `digest.size_threshold_chars = 800` to force digest at even
  moderately-sized reads.

### Turn 3 — Injection budget drop

- **Prompt**: `"再看一下 pico/context_manager.py"`
- **Rationale**: Fixture pico.toml sets
  `injection_budget_ratio = 0.005` so the aggregate injection budget
  is ~500 tokens. Multiple sources (`workspace_state`, `memory_index`,
  `project_structure`, `recalled_memory`, `checkpoint`) all try to
  render at once and exceed the cap. DROP_PRIORITY takes over from
  the tail (checkpoint first).

### Turn 4 — History budget drop

- **Prompt**: `"总结一下我们目前讨论的所有内容"`
- **Rationale**: By turn 4, `session["messages"]` has accumulated the
  user turns + tool_use pairs + assistant text from turns 1-3
  (~15-25 messages, several kilobytes). Fixture pico.toml sets
  `history_soft_cap = 1200` — build_v2's `_drop_old_turns` drops
  the oldest turn units.

### Turn 5 — Cache anchor + closure

- **Prompt**: `"最后 done"`
- **Rationale**: A very short prompt. Purpose: cross-turn cache
  anchor stability check (`system_cache_key` identical across all
  five turns), and verifies Anthropic API round-trip closure with
  minimal token spend.

### Fixture pico.toml (written by FixtureManager)

```toml
[context]
history_soft_cap = 1200
history_floor_messages = 4
injection_budget_ratio = 0.005
total_budget_hard_cap = 100000
system_tools_hard_cap = 30000

[context.digest]
size_threshold_chars = 800

[memory.recall]
min_score = 0.2
```

Choices are deliberately aggressive to force behaviors within 5 turns.

### Seed note (fixture `seed_cache_note.md` → `.pico/memory/agent/cache-invariant.md`)

```markdown
---
name: cache-invariant
type: feedback
description: prompt-cache invariant — stable prefix must not include mtime content
tags: [context, cache]
aliases: []
supersedes: []
---

Pico's cache anchor lives in Layer 1 (system content block). Anything
that changes per turn (workspace state, memory index) must go through
<system-reminder> injection on the user message, not into system.
```

## 5 · Hard Assertions — 27 Total

### Turn 1 — Recall (6 assertions)

1. `metadata["intent"]["name"] == "recall"`
2. Rendered current user content contains `"<pico:recalled_memory"`
3. Rendered current user content contains `"cache-invariant"`
4. `metadata["injection_tokens"]["recalled_memory"] > 0`
5. `metadata["recall.error_count"] == 0`
6. Provider response `stop_reason` is `end_turn` or `tool_use`

### Turn 2 — Digest (5)

7. Last `tool_result` message has `_pico_meta["digest_applied"] is True`
8. `tool_result` content starts with `"[digest]"`
9. `tool_result` content contains `"raw at "`
10. Raw file at `<run_dir>/tool_results/<hash>.txt` exists on disk
11. Raw file byte size equals original `read_file` output size

### Turn 3 — Injection drop (4)

12. `metadata["injection_budget"] > 0` (guard: budget was set, not zero)
13. `len(metadata["injection_dropped"]) >= 1`
14. `"checkpoint" in metadata["injection_dropped"]` OR checkpoint had zero
    tokens (couldn't be dropped because never rendered — accept either)
15. `"recalled_memory" not in metadata["injection_dropped"]` (last in
    DROP_PRIORITY, hardest to drop)

### Turn 4 — History drop (5)

16. `metadata["dropped_messages"] > 0`
17. `metadata["messages_tokens"] <= 1500` (soft_cap 1200 + slop)
18. Every kept `tool_use.id` has a matching `tool_result.tool_use_id`
    (pairing invariant preserved)
19. Provider-received `messages` length < `len(session["messages"])`
    (drop reached the wire)
20. Session `session["messages"]` first entry unchanged (immutability
    check — the pre-drop old entry still lives in session state)

### Turn 5 — Cache anchor + closure (5)

21. `metadata["cache_control_breakpoints"]` non-empty
22. Cumulative provider usage contains
    `cache_creation_input_tokens > 0` OR `cache_read_input_tokens > 0`
    across turns 2-5
23. `metadata["system_cache_key"]` identical across all five turns
24. All 15 required metadata fields present per
    `test_metadata_completeness.py::REQUIRED_METADATA_FIELDS`
25. `pico.session["_recall_errors"]["count"] == 0`

### Global (cross-turn invariants) (2)

26. Total provider calls ≤ 15
27. Total input tokens + output tokens ≤ 200,000

## 6 · JSON Report Schema

```json
{
  "schema_version": 1,
  "run_id": "live-e2e-<ns_timestamp>",
  "provider": "anthropic",
  "model": "claude-sonnet-4-5-20250929",
  "started_at": "ISO8601",
  "wall_time_ms": 48321,
  "config": {
    "max_provider_calls": 15,
    "max_total_tokens": 200000,
    "timeout_seconds": 300
  },
  "turns": [
    {
      "turn": 1,
      "user_prompt": "...",
      "expected_behavior": "recall_triggered",
      "duration_ms": 8410,
      "provider_calls_this_turn": 1,
      "final_answer": "...",
      "stopped_at_step_limit": false,
      "error": null,
      "usage": {
        "input_tokens": 1234,
        "output_tokens": 456,
        "cache_creation_input_tokens": 800,
        "cache_read_input_tokens": 0
      },
      "metadata_subset": {
        "intent": {"name": "recall", "matched_keyword": "上次",
                   "matched_reason": "keyword:'上次' via profile:recall"},
        "injection_tokens": {...},
        "injection_dropped": [],
        "recall.error_count": 0,
        "dropped_messages": 0,
        "messages_tokens": 342,
        "system_cache_key": "abc123..."
      },
      "assertions": [
        {"name": "intent_name_recall", "passed": true,
         "expected": "recall", "actual": "recall"},
        ...
      ]
    },
    ...
  ],
  "global_assertions": [...],
  "totals": {
    "provider_calls": 8,
    "input_tokens": 8921,
    "output_tokens": 2144,
    "cache_creation_input_tokens": 3200,
    "cache_read_input_tokens": 6200
  },
  "assertion_summary": {
    "total": 27,
    "passed": 27,
    "failed": 0
  },
  "overall_pass": true
}
```

`schema_version = 1`. Future breaking changes bump this.

## 7 · Component Internal API

### 7.1 `Config` — CLI + env parsing

```python
@dataclass(frozen=True)
class RunConfig:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-5-20250929"
    max_provider_calls: int = 15
    max_total_tokens: int = 200_000
    timeout_seconds: int = 300
    reset: bool = False
    verbose: bool = False


def parse_args() -> RunConfig: ...
def check_env(config: RunConfig) -> None:
    """SystemExit(2) if PICO_ANTHROPIC_API_KEY missing/empty."""
def verify_pico_repo(root: Path) -> None:
    """SystemExit(2) if not a pico repo."""
```

CLI: `--model`, `--max-provider-calls`, `--max-total-tokens`,
`--timeout-seconds`, `--reset`, `--verbose`.

### 7.2 `FixtureManager` — setup/teardown context manager

```python
class FixtureManager:
    def __init__(self, repo_root: Path): ...
    def __enter__(self):
        # 1. Snapshot pre-existing pico.toml → results/pre-run-pico.toml.bak
        # 2. Write fixture pico.toml (§4)
        # 3. Write seed note (§4)
        # 4. Return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        # 1. Remove seed note
        # 2. Restore or delete pico.toml
        # 3. Never raise (log teardown errors, continue)
```

### 7.3 `TurnRunner` — one-turn execution

```python
class TurnRunner:
    def __init__(self, pico: Pico, config: RunConfig): ...

    def run_turn(self, turn: int, user_prompt: str,
                 expected_behavior: str) -> TurnResult:
        """Calls pico.ask; snapshots metadata + session state; returns TurnResult."""
```

### 7.4 `AssertionEngine` — hard checks

```python
@dataclass(frozen=True)
class Assertion:
    name: str
    passed: bool
    expected: str   # human readable
    actual: str     # human readable


class AssertionEngine:
    def dispatch(self, turn: int, result: TurnResult, pico: Pico,
                 all_results: list[TurnResult]) -> list[Assertion]:
        """Turn-index-aware dispatch to per-turn check_*."""

    def check_turn_1_recall(self, r: TurnResult) -> list[Assertion]: ...
    def check_turn_2_digest(self, r: TurnResult, pico: Pico) -> list[Assertion]: ...
    def check_turn_3_injection_drop(self, r: TurnResult) -> list[Assertion]: ...
    def check_turn_4_history_drop(self, r: TurnResult, pico: Pico) -> list[Assertion]: ...
    def check_turn_5_cache_anchor(self, r: TurnResult,
                                   all_results: list[TurnResult]) -> list[Assertion]: ...
    def check_global(self, all_results: list[TurnResult],
                     pico: Pico) -> list[Assertion]: ...
```

Never raises. Every failure is a `passed=False` Assertion.

### 7.5 `Reporter` — terminal + JSON output

```python
class Reporter:
    def __init__(self, config: RunConfig, output_dir: Path): ...

    def render_turn_summary(self, turn: int | str,
                             expected: str,
                             assertions: list[Assertion]) -> None: ...

    def write_json(self, all_turns: list[TurnResult],
                    all_assertions: dict,
                    config: RunConfig,
                    totals: dict,
                    wall_time_ms: int) -> Path: ...

    def render_final(self, overall_pass: bool,
                     totals: dict,
                     wall_time_ms: int,
                     report_path: Path,
                     assertion_summary: tuple[int, int]) -> None: ...
```

ANSI colors only if `sys.stdout.isatty()`.

### 7.6 `main() -> int`

Orchestrates the above. See §4.6 of the brainstorm phase for pseudocode.
Returns exit code:
- 0: all pass
- 1: at least one assertion failed
- 2: preflight failure (env/repo/fixture conflict)
- 3: provider error (API 4xx/5xx/timeout)
- 4: uncaught pico exception
- 5: token budget exceeded
- 6: wall-time timeout

### 7.7 `TurnResult` — frozen data class

```python
@dataclass(frozen=True)
class TurnResult:
    turn: int
    user_prompt: str
    expected_behavior: str
    final_answer: str
    metadata: dict          # full agent.last_prompt_metadata
    session_message_count_before: int
    session_message_count_after: int
    provider_call_count_this_turn: int
    duration_ms: int
    usage: dict
    stopped_at_step_limit: bool
    error: str | None
    # captured for cross-turn analysis:
    provider_input_messages_len: int
    current_user_content: str
```

## 8 · Error Handling & Cost Guards

### 8.1 Error taxonomy

| Event | Behavior | Exit |
| -- | -- | -- |
| Missing env key | Print error, abort | 2 |
| Not a pico repo | Print error, abort | 2 |
| Existing seed note (unclean previous run) | Suggest `--reset`, abort | 2 |
| API 4xx/5xx/timeout | Mark turn `INCOMPLETE`, abort remaining turns, write partial JSON, exit | 3 |
| Assertion failure (per-turn) | Record `passed=False`, continue to next turn | (final 1) |
| Uncaught pico exception | Capture traceback into turn's `error` field, abort remaining, write partial JSON, exit | 4 |
| Token budget exceeded | Skip remaining turns, run global assertions, exit | 5 |
| Wall-time exceeded | Same as above | 6 |
| Step limit reached | Turn marks `stopped_at_step_limit=true`, continue | (per-assertion) |

### 8.2 Cost guards (three layers)

- `--max-provider-calls=15` — after each turn, if
  `sum(provider_call_count_this_turn) > cap`, skip remaining turns.
- `--max-total-tokens=200_000` — same check on cumulative usage.
- `--timeout-seconds=300` — check `time.monotonic_ns()` between turns.

Guards are opt-in tunable via CLI. Defaults are ~$1 max cost on Sonnet.

## 9 · Reset Path

`uv run python -m benchmarks.live_e2e.run_live_session --reset` triggers
`do_reset(repo_root)`:

1. Remove `.pico/memory/agent/cache-invariant.md` if it exists
2. Restore pico.toml from `results/pre-run-pico.toml.bak` if present
3. Delete `results/*.json` (keep `results/README.md`)
4. Delete `results/pre-run-pico.toml.bak`
5. Print summary of what was cleaned

`do_reset` never touches `.pico/sessions/` or `.pico/runs/` (they're
useful for replay analysis; user can rm manually if desired).

## 10 · Gitignore

Add to root `.gitignore`:

```
benchmarks/live_e2e/results/*.json
benchmarks/live_e2e/results/pre-run-pico.toml.bak
```

Do NOT ignore `benchmarks/live_e2e/results/README.md` — commit it so
the dir exists in git.

## 11 · Testing Strategy for This Script

The script itself is a test runner; adding pytest tests for it would
create infinite regression. But the `AssertionEngine` is testable
in isolation with tiny fixture data.

`benchmarks/live_e2e/tests/test_assertions.py`:

- `test_check_turn_1_recall_passes_on_valid_metadata`
- `test_check_turn_1_recall_fails_when_intent_not_recall`
- `test_check_turn_2_digest_fails_when_no_digest_applied`
- `test_check_turn_2_digest_verifies_raw_file_exists`
- `test_check_turn_3_injection_drop_accepts_checkpoint_zero_tokens`
- `test_check_turn_4_pairing_invariant_catches_orphan_tool_use`
- `test_check_turn_5_cache_key_stable_across_turns`
- `test_check_global_budget_exceeded`

Unit tests run offline (no API), covered by regular pytest, ~10 tests.

## 12 · README Content

`benchmarks/live_e2e/README.md` covers:

1. **Purpose**: end-to-end verification of P1+P2+P3 optimizations
   against a real Anthropic model.
2. **Prerequisites**: `PICO_ANTHROPIC_API_KEY` in `.env`;
   pico repo checked out on `memory` branch or newer.
3. **How to run**:
   ```
   uv run python -m benchmarks.live_e2e.run_live_session
   uv run python -m benchmarks.live_e2e.run_live_session --model claude-haiku-*
   uv run python -m benchmarks.live_e2e.run_live_session --reset
   ```
4. **Cost estimate**: ~$0.20 (Sonnet), ~$0.05 (Haiku) per full run.
5. **What it validates**: 27 assertions across 5 turns (§5 checklist).
6. **What it doesn't validate**: quality of model output, latency.
   Refers to `benchmarks/memory_quality/` and `benchmarks/perf/` for
   those.
7. **Interpretation of exit codes**: 0/1/2/3/4/5/6 per §7.6.

## 13 · Definition of Done

1. `benchmarks/live_e2e/` directory + all files exist per §3.
2. `uv run python -m benchmarks.live_e2e.run_live_session --reset`
   runs to completion (no error) on an already-clean repo.
3. `uv run python -m benchmarks.live_e2e.run_live_session` with a
   valid `PICO_ANTHROPIC_API_KEY` produces JSON report matching §6
   schema, `overall_pass == true`, exit 0.
4. `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -v`
   passes (all AssertionEngine unit tests).
5. `.gitignore` updated per §10.
6. `README.md` matches §12.
7. Regular pytest suite unchanged (668 passed / 1 skipped).

## 14 · Open Questions

- **Q1**: Should the report include the full `session["messages"]`
  after all 5 turns? Answer: no. It would be large and mostly
  redundant with `.pico/sessions/*.json`. Report contains only
  `metadata_subset` for each turn. Users needing the full session
  can inspect `.pico/sessions/`.
- **Q2**: Should the CLI accept a `--dry-run` that skips the API but
  runs everything else? Answer: no. Dry-run would need a mock
  provider — but that's what the sniffer-based e2e tests already
  do. The distinguishing value of this script is the real API.
- **Q3**: Should we test `haiku` as well? Answer: not by default —
  the model is an operator choice via `--model`. Sonnet default
  balances cost and behavior visibility.

All three questions have their answers baked into the current design;
no follow-up needed before implementation.
