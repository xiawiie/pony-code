# Data Provenance

This directory is the C-stage Action Kernel and Messages v3 evidence set generated from the repository state immediately before the evidence-only commit.

Authoritative commands:

- `uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/action-kernel-messages-v3-2026-07-10/harness-regression-v2.json")'`
- `uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/context-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/memory-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/action-kernel-messages-v3-2026-07-10/recovery-ablation-v2.json", repetitions=3)'`

Interpretation boundaries:

- Harness regression proves the deterministic runtime contract, not live Provider quality.
- Context ablation compares bounded and effectively unbounded actual request-message views.
- Memory ablation is valid only because every variant records that the bootstrap tool turn was dropped.
- Recovery ablation covers existing recovery behavior; it does not claim A-stage restore/security hardening.
- Real native Provider evidence is produced separately by the ignored local report in `benchmarks/live_e2e/results/`.
