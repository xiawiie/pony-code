# Pre-Flight Audit: Architecture Convergence (Task 0)

**Baseline commit:** 10954fd  
**Date:** 2026-07-04  
**Branch:** memory

This document records the current state of the pico codebase BEFORE the architecture convergence refactor begins. Subsequent phases reference these values for line counts, test line numbers, and regression commands.

---

## 1. Current Line Counts

Baseline file sizes (Step 1):

```
1025 pico/cli_commands.py
1311 pico/evaluation/metrics_experiments.py
 641 pico/providers/clients.py
 695 pico/runtime.py
2026 tests/test_pico.py
----
5698 total
```

---

## 2. resume_status Invariant Tests

Tests that enforce resume_status preservation across checkpoints (Step 2):

| Test | File | Line |
|------|------|------|
| `test_report_prompt_metadata_preserves_initial_resume_status` | tests/test_pico.py | 1534 |
| `test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup` | tests/test_pico.py | 1584 |

---

## 3. Anthropic Compatible Client Edge Cases

Tests for edge-case behavior in AnthropicCompatibleModelClient (Step 3):

| Test | File | Line |
|------|------|------|
| `test_anthropic_compatible_client_extracts_text_block_without_type` | tests/test_pico.py | 807 |
| `test_anthropic_compatible_client_explains_thinking_only_token_exhaustion` | tests/test_pico.py | 841 |

---

## 4. CLI Help Path Tests

Critical tests for CLI UX and help output (Step 4):

| Test | File | Line |
|------|------|------|
| `test_repl_command_exits_on_eof` | tests/test_cli_commands.py | 47 |
| `test_help_command_shows_examples` | tests/test_cli_commands.py | 70 |
| `test_help_flag_uses_root_help_without_argparse_dump` | tests/test_cli_commands.py | 82 |

---

## 5. Diagnostics & Recovery Test Counts

Test coverage for CLI diagnostics, recovery, and command flows (Step 5):

| File | Test Count | Coverage |
|------|-----------|----------|
| tests/test_cli_commands.py | 13 | Core CLI command routing, help, REPL, agent integration |
| tests/test_recovery_cli.py | 24 | Checkpoint recovery, restoration workflows, JSON contracts |
| tests/test_cli_diagnostics.py | 16 | Provider connectivity checks, diagnostic output formats |

---

## 6. Public Symbols in `pico.evaluation.metrics.__all__`

Exact public API surface (Step 6):

```
DEFAULT_CONTEXT_ABLATION_V2_PATH
DEFAULT_CORE_REPORT_PATH
DEFAULT_HARNESS_REGRESSION_V2_PATH
DEFAULT_MEMORY_ABLATION_V2_PATH
DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS
DEFAULT_RECOVERY_ABLATION_V2_PATH
MEMORY_EXPERIMENT_TASKS
METRICS_SCHEMA_VERSION
REAL_SECURITY_SCENARIOS
RECOVERY_ABLATION_TASKS
SECURITY_SCENARIOS
_parse_iso8601
_provider_profile
_safe_mean
_safe_ratio
_utc_timestamp
aggregate_benchmark_artifact
aggregate_run_artifacts
build_stress_agent_metrics
collect_resume_metrics
measure_feature_ablation_metrics
render_large_scale_experiment_report
render_resume_metrics_markdown
run_context_ablation_v2
run_context_stress_matrix
run_large_scale_memory_experiment
run_memory_ablation_v2
run_memory_dependency_experiment
run_provider_experiments
run_real_context_experiment
run_real_memory_experiment
run_real_security_experiment_suite
run_recovery_ablation_v2
run_security_experiment_suite
write_benchmark_core_report
```

---

## 7. Public Provider Classes in `pico.providers.clients`

Model client implementations (Step 7):

```
AnthropicCompatibleModelClient
FakeModelClient
OPENAI_COMPATIBLE_USER_AGENT
OllamaModelClient
OpenAICompatibleModelClient
RemoteDisconnected
```

Note: `OPENAI_COMPATIBLE_USER_AGENT` and `RemoteDisconnected` are non-class exports; the primary client classes are the first four.

---

## 8. Manual Real-Provider Smoke Tests

Exact commands for P1/P2/P3 acceptance regression (Step 8):

```bash
pico-cli --cwd /Users/wei/Desktop/pico doctor
```

```bash
pico-cli --cwd /Users/wei/Desktop/pico --format json run --max-new-tokens 2048 \
  "Reply exactly with <final>REAL_PROVIDER_SMOKE_OK</final> and no other text."
```

```bash
pico-cli --cwd /Users/wei/Desktop/pico --format json run --max-new-tokens 2048 \
  "Use the read_file tool to read README.md, then reply exactly with <final>REAL_PROVIDER_TOOL_OK</final> and no other text."
```

---

## 9. Baseline Health Check

`./scripts/check.sh` result (Step 9):

```
All checks passed!
446 passed in 67.23s
```

**Status:** PASS ✓

---

## 10. Audit Sign-Off

- [x] Step 1: Line counts recorded
- [x] Step 2: resume_status tests verified
- [x] Step 3: Anthropic edge cases verified
- [x] Step 4: CLI help tests verified
- [x] Step 5: Diagnostics/recovery test counts recorded
- [x] Step 6: metrics.__all__ captured
- [x] Step 7: Provider classes captured
- [x] Step 8: Manual commands recorded
- [x] Step 9: Baseline check passed

All pre-flight audits complete. Codebase is in stable, tested state. Ready for architecture convergence phases.
P2 real-provider smoke: PASS (doctor OK; direct final OK; read_file tool final OK)
