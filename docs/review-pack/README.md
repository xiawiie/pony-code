# Pico Review Pack

## Current local snapshot

- Branch: `memory`
- Current local baseline: `./scripts/check.sh` passes with `476 passed`
- Phase 1 targeted tests: `uv run pytest tests/test_scripts.py tests/test_metrics.py tests/test_memory_quality_benchmark.py -q` passes with `34 passed`
- Memory-quality gate: `uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json` reports `total=8`, `failed=0`
- Provider benchmark help: `uv run python scripts/run_provider_experiments.py --help` exposes `--provider {all,gpt,claude,deepseek}`
- Provider smoke: `pico-cli doctor --format text` reports storage/recovery ok and provider connectivity ok
- One-shot smoke: `pico-cli --no-input --approval never --max-steps 1 --max-new-tokens 32 --quiet run "Return exactly PICO_SMOKE_OK. Do not call tools."` returns `PICO_SMOKE_OK`
- Worktree triage: Phase 1 tracked changes are committed on `memory`; two unrelated untracked design docs remain excluded from the branch.

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

Memory quality evidence: `benchmarks/memory_quality/run_benchmark.py --mode fake --format json` runs deterministic tool-trace scoring through the real Pico runtime. Live-provider memory-quality evidence remains optional because it depends on provider credentials, quota, and model behavior.

## Sample run artifact list

- `.pico/runs/<run_id>/task_state.json`
- `.pico/runs/<run_id>/trace.jsonl`
- `.pico/runs/<run_id>/report.json`
