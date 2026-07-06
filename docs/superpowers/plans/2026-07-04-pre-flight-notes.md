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
P4 manual smoke: PASS (2026-07-06 11:25:19 CST; doctor exit 0 provider connectivity HTTP 200; direct final REAL_PROVIDER_SMOKE_OK exit 0; read_file tool final REAL_PROVIDER_TOOL_OK exit 0; status JSON ok/kind/data exit 0; REPL /help,/memory,/exit exit 0)

---

## Final state

Task 27 acceptance run: 2026-07-06 14:21:23 CST on branch `memory`.

Final line-count command:

```bash
wc -l pico/cli_commands.py pico/evaluation/metrics_experiments.py pico/providers/clients.py pico/runtime.py tests/test_pico.py pico/evaluation/*.py pico/providers/*.py pico/cli_*.py
```

Final line-count output:

```text
     258 pico/cli_commands.py
     126 pico/evaluation/metrics_experiments.py
      46 pico/providers/clients.py
     709 pico/runtime.py
     757 tests/test_pico.py
       1 pico/evaluation/__init__.py
     224 pico/evaluation/benchmark_schema.py
      44 pico/evaluation/evaluator.py
     295 pico/evaluation/experiments_real.py
     368 pico/evaluation/experiments_recovery.py
     508 pico/evaluation/experiments_synthetic.py
     439 pico/evaluation/fixed_benchmark.py
      79 pico/evaluation/metrics.py
      36 pico/evaluation/metrics_common.py
     126 pico/evaluation/metrics_experiments.py
     402 pico/evaluation/metrics_reports.py
     175 pico/evaluation/provider_benchmark.py
      10 pico/providers/__init__.py
      82 pico/providers/_shared.py
     155 pico/providers/anthropic_compatible.py
      46 pico/providers/clients.py
      56 pico/providers/defaults.py
      59 pico/providers/ollama.py
     344 pico/providers/openai_compatible.py
     258 pico/cli_commands.py
     422 pico/cli_diagnostics.py
      26 pico/cli_errors.py
      15 pico/cli_help.py
     253 pico/cli_memory.py
      55 pico/cli_output.py
      42 pico/cli_parser.py
     285 pico/cli_recovery.py
      86 pico/cli_start.py
    6787 total
```

Line-count conclusions:

- `pico/cli_commands.py`: 258 lines, under the <= 300 soft target.
- `pico/evaluation/metrics_experiments.py`: 126 lines, under the <= 500 soft target.
- `pico/providers/clients.py`: 46 lines, forwarding shell under 100 lines.
- `pico/runtime.py`: 709 lines; this remains the deliberate orchestration module and was not an extraction target.
- New/extracted files are <= 500 lines except `pico/evaluation/experiments_synthetic.py` at 508 lines. Reason documented: the 8-line soft-target overshoot keeps synthetic benchmark task definitions, fake-provider harness behavior, security checks, and aggregation entrypoints together so benchmark artifact shape stays stable after extraction.

Acceptance evidence:

- Public API contract: `uv run pytest tests/test_public_api_contract.py -q` -> `7 passed in 0.10s`.
- Full check: `./scripts/check.sh` -> ruff `All checks passed!`; pytest `449 passed in 62.77s`.
- Real-provider doctor: `pico-cli --cwd /Users/wei/Desktop/pico doctor` exited 0; sanitized output contained an OK-style diagnostic status marker.
- Real-provider direct final: command exited 0 and returned `REAL_PROVIDER_SMOKE_OK`; run `run_20260706-141927-8f2e2c` recorded `stop_reason=final_answer_returned` and `tool_steps=0`.
- Real-provider read_file final: command exited 0 and returned `REAL_PROVIDER_TOOL_OK`; run `run_20260706-141956-a8b44e` recorded `stop_reason=final_answer_returned`, `tool_steps=1`, and read_file tool trace events.
- No API key values or environment dumps were printed or recorded in this note.
