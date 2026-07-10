# Pico Review Pack

## Current C-stage evidence

The current, committed evidence set is [Action Kernel and Messages v3](../../benchmarks/results/action-kernel-messages-v3-2026-07-10/DATA_PROVENANCE.md). Its JSON artifacts are the source of truth; the core Markdown report is generated from those artifacts.

- [Harness regression](../../benchmarks/results/action-kernel-messages-v3-2026-07-10/harness-regression-v2.json)
- [Context ablation](../../benchmarks/results/action-kernel-messages-v3-2026-07-10/context-ablation-v2.json)
- [Working-memory ablation](../../benchmarks/results/action-kernel-messages-v3-2026-07-10/memory-ablation-v2.json)
- [Recovery ablation](../../benchmarks/results/action-kernel-messages-v3-2026-07-10/recovery-ablation-v2.json)
- [Core report](../../benchmarks/results/action-kernel-messages-v3-2026-07-10/pico-benchmark-core-report.md)

## Project pitch

Pico is a local coding-agent harness for repository-grounded engineering work.
It combines canonical messages, explicit Actions, bounded request views, run
artifacts, and deterministic benchmark evidence.

## Architecture map

- Pico has one decision path: `Response -> decode_action -> Action -> AgentLoop`.
- Request construction applies an overlay to canonical messages; sent-message metrics describe the actual request view.
- Session v3 persists canonical messages and uses an atomic migration boundary for legacy sessions.
- Runtime artifacts remain `task_state.json`, `trace.jsonl`, and `report.json` for each run.

## Benchmark evidence

- Harness regression proves deterministic runtime behavior, not live Provider quality.
- Context, memory, and recovery ablations measure their stated local mechanisms only.
- C-06 is complete: DeepSeek `qwen3.7-max` passed 40/40 assertions with 6 native actions and 9/15 Provider calls. Its JSON report is intentionally ignored; the gate proves native-tool/runtime contracts rather than Provider answer quality.

## Sample run artifact list

- `.pico/runs/<run_id>/task_state.json`
- `.pico/runs/<run_id>/trace.jsonl`
- `.pico/runs/<run_id>/report.json`

## Historical material

The [2026-06-07 result directory](../../benchmarks/results/main-resume-repro-2026-06-07/DATA_PROVENANCE.md) is archived historical material and is not current Messages v3 evidence.
