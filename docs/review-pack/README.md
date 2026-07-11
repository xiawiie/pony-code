# Pico Review Pack

## Current A-stage security and trust evidence

The current deterministic baseline is [Security and Trust Baseline](../../benchmarks/results/security-trust-baseline-2026-07-10/DATA_PROVENANCE.md). Local security truth comes from `tests/test_security_integration.py`, `tests/test_shell_security_corpus.py`, `tests/test_recovery_durability_e2e.py`, and `benchmarks/live_e2e/tests/test_assertions.py`.

The initial authorized DeepSeek attempt ended at turn 1 with `network_error`; one separately authorized follow-up ended with HTTP 401. Both stopped before successful Provider usage with 0 input/output tokens, neither was automatically retried, and A-05 remains pending. Both ignored reports passed key, private-artifact, and fixture-restoration checks.

After synchronizing the worktree's Lumina Base and model, a diagnostic run completed all five turns and exposed an offline-confirmed false negative: Turn 2 had zero injected tokens, so no reminder was expected. The assertion now validates prompt/reminder requirements per Provider call and has an independent C0/I0/M0 review.

The final post-repair DeepSeek `qwen3.7-max` run passed 43/43 assertions with 8 native actions and 10/15 Provider calls. It used 13,842 input, 1,330 output, and 5,248 cache-read tokens in 44.253 seconds. Provider payload, active artifact, private-mode, fixture-restoration, session-v3, terminal-artifact, call-cap, and token-cap checks all passed. The JSON report is intentionally ignored; only these safe facts are promoted.

- [Harness regression](../../benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json)
- [Context ablation](../../benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json)
- [Working-memory ablation](../../benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json)
- [Recovery ablation](../../benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json)
- [Memory quality](../../benchmarks/results/security-trust-baseline-2026-07-10/memory-quality.json)
- [Core report](../../benchmarks/results/security-trust-baseline-2026-07-10/pico-benchmark-core-report.md)
- [Data provenance](../../benchmarks/results/security-trust-baseline-2026-07-10/DATA_PROVENANCE.md)

## Prior C-stage evidence

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
