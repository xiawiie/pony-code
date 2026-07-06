# Pico Review Pack

## Current local snapshot

- Branch: `memory`
- Current local baseline: `./scripts/check.sh` passes with `452 passed`
- Provider smoke: `pico-cli doctor --format text` reports storage/recovery ok and provider connectivity ok
- One-shot smoke: `pico-cli --no-input --approval never --max-steps 1 --max-new-tokens 32 --quiet run "Return exactly PICO_SMOKE_OK. Do not call tools."` returns `PICO_SMOKE_OK`
- Worktree triage: keep the tracked `.env` inline-comment/header-validation fix set for submission; old untracked superpowers drafts were removed from the working tree

## Project pitch

Pico is a lightweight local coding agent harness for repository-grounded engineering tasks. It wraps a model with workspace context, explicit tools, state tracking, memory, run artifacts, and benchmark evidence.

## Architecture map

- `pico.cli` wires configuration, provider clients, workspace context, and the runtime.
- `pico.runtime.Pico` coordinates the agent control surface.
- `pico.context_manager` builds bounded model context from prefix, memory, history, and the current request.
- `pico.tools` defines the explicit tool allowlist used by the runtime.
- `pico.run_store` writes per-run artifacts for review and replay.

## Benchmark evidence

Benchmark runs should preserve reproducibility metadata, task rows, summary counts, and failure categories so reviewers can distinguish runtime regressions from task or provider failures.

Current caveat: `benchmarks/memory_quality/run_benchmark.py` still reports `scaffold_only`; it validates scenario loading and workspace setup, not live model/tool-trace memory quality.

## Sample run artifact list

- `.pico/runs/<run_id>/task_state.json`
- `.pico/runs/<run_id>/trace.jsonl`
- `.pico/runs/<run_id>/report.json`
