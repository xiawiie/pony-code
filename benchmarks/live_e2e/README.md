# Live-API End-to-End Test

Runs 5 designed turns through the real Anthropic API and hard-asserts
**27 concrete invariants** covering pico's four post-migration
optimizations: `<system-reminder>` injection, memory recall, tool_result
digest, and history-budget drop. See
`docs/superpowers/specs/2026-07-08-pico-live-e2e-test-design.md` for
the full design.

**This is not a pytest test.** It is a standalone script that consumes
real API credits. Run it manually before shipping large changes to the
memory/context subsystems.

## Prerequisites

- `PICO_ANTHROPIC_API_KEY` set in `.env` (or the environment)
- Working directory: pico repo root, on the `memory` branch or later

Optional environment overrides:

- `PICO_ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`)
- `PICO_ANTHROPIC_MODEL` (default `claude-sonnet-4-5-20250929`)

## How to run

Full run (writes a JSON report to `benchmarks/live_e2e/results/`):

    uv run python -m benchmarks.live_e2e.run_live_session

Cheaper model:

    uv run python -m benchmarks.live_e2e.run_live_session --model claude-haiku-...

Clean up after a failed / partial run:

    uv run python -m benchmarks.live_e2e.run_live_session --reset

Tune cost guards (defaults shown):

    uv run python -m benchmarks.live_e2e.run_live_session \
        --max-provider-calls 15 \
        --max-total-tokens 200000 \
        --timeout-seconds 300

## Cost estimate

- claude-sonnet-4-5: **~$0.20 per full run**
- claude-haiku-...:  **~$0.05 per full run**

Both estimates are with default cost caps (15 calls / 200K tokens / 5min).

## What it validates

Five turns, each targeting one optimization:

| Turn | Purpose | Assertion count |
| ---- | ------- | --------------- |
| 1    | Recall keyword hits + memory injection reaches provider | 6 |
| 2    | Large `read_file` result gets digested; raw file lands on disk | 5 |
| 3    | Injection budget drop honors `DROP_PRIORITY` (checkpoint first, recalled_memory last) | 4 |
| 4    | History-budget drop preserves tool_use/tool_result pairing invariant | 5 |
| 5    | `cache_control` breakpoints yield cache tokens; `system_cache_key` stable across turns | 5 |
| —    | Global: total provider calls ≤ 15, total tokens ≤ 200K | 2 |

## What it does NOT validate

- Quality of the model's output — see `benchmarks/memory_quality/`
- Latency — see `benchmarks/perf/`
- Non-Anthropic providers — fallback path is covered by
  `tests/e2e/test_fallback_provider_parity.py`

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0    | All 27 assertions passed |
| 1    | At least one assertion failed (JSON report written) |
| 2    | Preflight failure: missing env, not a pico repo, unclean seed |
| 3    | Provider error mid-run (API 4xx/5xx/timeout) |
| 4    | Uncaught pico exception (JSON report written with `error` field) |
| 5    | Cost budget exceeded (provider_calls or tokens) |
| 6    | Wall-time timeout exceeded |

## Interpreting the JSON report

Every run writes `benchmarks/live_e2e/results/live-e2e-<ns_timestamp>.json`.
Schema is documented in the spec §6. The most useful top-level fields:

- `overall_pass`: single boolean summary
- `assertion_summary`: `{total, passed, failed}`
- `turns[i].assertions`: per-assertion `{name, passed, expected, actual}`
- `totals`: cumulative provider calls + token usage across all turns

## Safety

- `read_only=True` is enforced at Pico construction — `write_file`,
  `patch_file`, and `run_shell` writes are refused by `tool_executor`.
- The script snapshots and restores `pico.toml` around the run.
- The seed note lives at `.pico/memory/agent/cache-invariant.md` for
  the duration of a run and is removed on exit (even on failure).
- `.pico/sessions/` and `.pico/runs/` are preserved after the run
  (useful for replay). Remove manually if desired.
