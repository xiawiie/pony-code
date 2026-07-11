# Data Provenance

This directory is the Pico A-stage security and trust baseline deterministic evidence set.

Regenerated against code baseline `976804e909790cafc4145c0d89796fa9ed1946b2` plus the reviewed managed-sandbox working-tree diff. The diff remains uncommitted because this environment exposes `.git` read-only.

Authoritative generators:

- `uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json")'`
- `uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json", repetitions=3)'`
- `uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json`

Interpretation boundaries:

- Artifact canary, Shell bypass, restore durability, crash reconciliation, pending review, and private-mode claims come from deterministic adversarial pytest gates.
- Harness regression proves deterministic runtime behavior, not live Provider answer quality.
- Recovery ablation remains a resume-regression measure; it does not replace the A2 restore-journal tests.
- Performance files are local parseable smokes without machine-specific thresholds and are not committed here.
- One real DeepSeek E2E is a separate final integration gate and its JSON remains ignored.
