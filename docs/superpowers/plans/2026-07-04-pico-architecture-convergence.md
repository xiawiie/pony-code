# Pico Architecture Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the four largest mixed-responsibility files in `pico/` (`cli_commands.py`, `evaluation/metrics_experiments.py`, `providers/clients.py`, and `tests/test_pico.py`) into focused modules along existing conceptual boundaries, without changing any user-facing CLI, JSON contract, provider behavior, or benchmark artifact shape.

**Architecture:** Preserve current public import surfaces by keeping the four largest files as compatibility shells that re-export from smaller modules. Extraction follows the sequence: Pre-flight → protection tests (P0) → evaluation split (P1) → provider split (P2) → runtime report helper (P3) → CLI split (P4) → test layout cleanup (P5). Each phase is one or more independently reversible commits.

**Tech Stack:** Python 3.11, `uv`, `pytest`, `ruff`. No new dependencies introduced.

## Global Constraints

- Every phase MUST end with `./scripts/check.sh` passing (runs `ruff check .` and `pytest -q`).
- Public imports from `pico`, `pico.providers.clients`, `pico.evaluation.metrics`, and `pico/__init__.py` MUST continue to work without modification to their callers.
- CLI command names, CLI JSON envelope keys, provider benchmark artifact fields (including `failure_category` and `failure_category_counts`), recovery checkpoint schema, and runtime report metadata keys MUST NOT change.
- No file rename or delete is permitted for: `pico/cli_commands.py`, `pico/providers/clients.py`, `pico/evaluation/metrics.py`, `pico/__init__.py`. These are compatibility shells.
- `pico/runtime.py` is a deliberate orchestration module and is NOT an extraction target.
- Newly extracted command modules have a soft threshold of ~500 lines; benchmark files have a soft threshold of ~500 lines; provider files ~250 lines each. Functional acceptance wins over line-count purity — small overshoot is acceptable, but a clean split under the threshold is preferred.
- No commit may mix production movement with unrelated formatting or drive-by refactors.
- Baseline commit for this work: `c33020f fix: improve real provider benchmark reliability`.
- Design spec anchor: `docs/superpowers/specs/2026-07-04-pico-architecture-convergence-design.md`.

---

## Pre-flight: Ground-Truth Audit

### Task 0: Record current state and audit spec references

**Files:**
- Create: `docs/superpowers/plans/2026-07-04-pre-flight-notes.md`

**Interfaces:**
- Consumes: nothing.
- Produces: an anchor file that subsequent tasks reference for line counts, existing test IDs, and manual real-provider commands.

- [ ] **Step 1: Record current line counts**

Run:
```bash
wc -l pico/cli_commands.py pico/evaluation/metrics_experiments.py pico/providers/clients.py pico/runtime.py tests/test_pico.py
```

Expected output (approximate — capture actual values):
```
1025 pico/cli_commands.py
1311 pico/evaluation/metrics_experiments.py
 641 pico/providers/clients.py
 695 pico/runtime.py
2026 tests/test_pico.py
```

Save the actual numbers into the pre-flight notes file.

- [ ] **Step 2: Audit resume_status invariant test names**

Run:
```bash
grep -n "def test_report_prompt_metadata_preserves_initial_resume_status\|def test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup" tests/test_pico.py
```

Expected: both function definitions found (currently at `tests/test_pico.py:1534` and `:1584`). Record the current line numbers in the notes.

- [ ] **Step 3: Audit Anthropic edge case tests**

Run:
```bash
grep -n "test_anthropic_compatible_client_extracts_text_block_without_type\|test_anthropic_compatible_client_explains_thinking_only_token_exhaustion" tests/test_pico.py
```

Expected: both function definitions found (currently at `tests/test_pico.py:807` and `:841`). Record current line numbers.

- [ ] **Step 4: Audit CLI help path tests**

Run:
```bash
grep -n "def test_help_command_shows_examples\|def test_help_flag_uses_root_help_without_argparse_dump\|def test_repl_command_exits_on_eof" tests/test_cli_commands.py
```

Expected: all three functions found. Record.

- [ ] **Step 5: Audit diagnostics/recovery JSON contract tests**

Run:
```bash
grep -c "^def test" tests/test_cli_diagnostics.py tests/test_recovery_cli.py tests/test_cli_commands.py
```

Expected: three counts (~16, ~15, ~13). Record what each file covers.

- [ ] **Step 6: Capture `metrics.py` public `__all__`**

Run:
```bash
python3 -c "from pico.evaluation import metrics; print('\n'.join(sorted(metrics.__all__)))"
```

Copy the full symbol list verbatim into the pre-flight notes. This is the exact surface P1 must preserve.

- [ ] **Step 7: Capture `pico.providers.clients` public classes**

Run:
```bash
python3 -c "from pico.providers import clients; print([n for n in dir(clients) if not n.startswith('_') and n[0].isupper()])"
```

Expected includes at minimum: `AnthropicCompatibleModelClient`, `FakeModelClient`, `OllamaModelClient`, `OpenAICompatibleModelClient`. Record.

- [ ] **Step 8: Record manual real-provider commands**

Write these three commands verbatim into the pre-flight notes:
```bash
pico-cli --cwd /Users/wei/Desktop/pico doctor

pico-cli --cwd /Users/wei/Desktop/pico --format json run --max-new-tokens 2048 \
  "Reply exactly with <final>REAL_PROVIDER_SMOKE_OK</final> and no other text."

pico-cli --cwd /Users/wei/Desktop/pico --format json run --max-new-tokens 2048 \
  "Use the read_file tool to read README.md, then reply exactly with <final>REAL_PROVIDER_TOOL_OK</final> and no other text."
```

These are the exact regression commands to reuse in P1/P2/P3 acceptance.

- [ ] **Step 9: Confirm baseline check passes**

Run: `./scripts/check.sh`
Expected: exits 0.

- [ ] **Step 10: Commit**

```bash
git add docs/superpowers/plans/2026-07-04-pre-flight-notes.md
git commit -m "docs: pre-flight audit for architecture convergence"
```

---

## P0: Protection Net

Purpose: freeze the behavior most likely to regress during subsequent movement.

### Task 1: Extend public API contract with all four provider classes

**Files:**
- Modify: `tests/test_public_api_contract.py`

**Interfaces:**
- Consumes: existing `pico.providers.clients` module.
- Produces: contract test guarding direct import of all four provider classes; consumed by P2 acceptance.

- [ ] **Step 1: Read current test**

Run: `grep -n "FakeModelClient\|OllamaModelClient\|OpenAICompatibleModelClient\|AnthropicCompatibleModelClient" tests/test_public_api_contract.py`
Expected: only `FakeModelClient` at line 32.

- [ ] **Step 2: Write the failing test extension**

Edit `tests/test_public_api_contract.py`. Locate the existing function `test_lightweight_package_split_uses_package_paths_without_legacy_shims`. Below it add:

```python
def test_all_four_provider_classes_importable_directly():
    from pico.providers.clients import (
        AnthropicCompatibleModelClient,
        FakeModelClient,
        OllamaModelClient,
        OpenAICompatibleModelClient,
    )

    for cls in (
        FakeModelClient,
        OllamaModelClient,
        OpenAICompatibleModelClient,
        AnthropicCompatibleModelClient,
    ):
        assert isinstance(cls, type), f"{cls!r} should be a class"
```

- [ ] **Step 3: Run test to verify it passes today**

Run: `uv run pytest tests/test_public_api_contract.py::test_all_four_provider_classes_importable_directly -v`
Expected: PASS (all four classes already live in `pico/providers/clients.py`). The test's job is to fail if P2 breaks the import surface later.

- [ ] **Step 4: Commit**

```bash
git add tests/test_public_api_contract.py
git commit -m "test: pin public import surface for all four provider clients"
```

### Task 2: Add REPL /help smoke test

**Files:**
- Modify: `tests/test_cli_commands.py`

**Interfaces:**
- Consumes: `pico.cli_commands.run_repl`, `pico.cli.HELP_DETAILS`.
- Produces: a test that fails if the lazy `HELP_DETAILS` import in `pico/cli_commands.py:544` is silently dropped by P4.0.

- [ ] **Step 1: Read the existing REPL EOF test for style**

Run: `sed -n '47,80p' tests/test_cli_commands.py`
Expected: shows `test_repl_command_exits_on_eof` at line 47.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_cli_commands.py`:

```python
def test_repl_help_renders_help_details(tmp_path, monkeypatch, capsys):
    from pico.cli import HELP_DETAILS
    from pico.cli_commands import run_repl
    from pico.runtime import Pico, SessionStore
    from pico.workspace import WorkspaceContext

    workspace = WorkspaceContext.build(tmp_path)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    session_store = SessionStore(tmp_path / ".pico" / "sessions")

    class _FakeModel:
        supports_prompt_cache = False
        last_completion_metadata = {}

        def complete(self, prompt, max_new_tokens, **kwargs):
            return "<final>ok</final>"

        def stream_complete(self, *args, **kwargs):
            return self.complete(*args, **kwargs)

    agent = Pico(
        model=_FakeModel(),
        workspace=workspace,
        session_store=session_store,
        approval_policy="auto",
    )

    inputs = iter(["/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    class _Args:
        format = "text"
        quiet = False

    run_repl(agent, _Args())
    out = capsys.readouterr().out
    assert HELP_DETAILS.strip().splitlines()[0] in out
```

- [ ] **Step 3: Run test — expected to PASS today**

Run: `uv run pytest tests/test_cli_commands.py::test_repl_help_renders_help_details -v`
Expected: PASS. The test's purpose is to fail during P4.0 if `HELP_DETAILS` is moved without updating the REPL path.

- [ ] **Step 4: If test fails, adjust to match actual `run_repl` signature**

If step 3 fails because `Pico`/`run_repl` signatures differ, read `pico/cli_commands.py:518-598` and `pico/runtime.py` and adapt the fixture. Do NOT loosen the assertion — the point is that `HELP_DETAILS` text reaches stdout.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cli_commands.py
git commit -m "test: pin REPL /help path against silent HELP_DETAILS drop"
```

### Task 3: Freeze benchmark artifact `failure_category` field

**Files:**
- Modify: `tests/test_evaluator.py`

**Interfaces:**
- Consumes: `pico.evaluation.evaluator.BenchmarkEvaluator._failure_category` (defined at `pico/evaluation/evaluator.py:569`).
- Produces: an explicit contract test guarding the string values of `failure_category`.

- [ ] **Step 1: Confirm the enum values still exist**

Run:
```bash
grep -n "missing_artifact\|budget_exceeded\|verifier_failed\|failure_stop_reason\|provider_error" pico/evaluation/evaluator.py
```
Expected: at least four literals found in `_failure_category`.

- [ ] **Step 2: Write the contract test**

Append to `tests/test_evaluator.py`:

```python
def test_failure_category_enum_is_stable():
    from pico.evaluation import evaluator as evaluator_mod
    source = evaluator_mod.__file__
    text = open(source, encoding="utf-8").read()
    for literal in (
        "missing_artifact",
        "budget_exceeded",
        "verifier_failed",
    ):
        assert f'"{literal}"' in text, (
            f"expected failure_category literal {literal!r} in evaluator source"
        )
```

Rationale: benchmark artifact consumers depend on the exact string values.

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/test_evaluator.py::test_failure_category_enum_is_stable -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_evaluator.py
git commit -m "test: freeze failure_category enum literals for benchmark artifacts"
```

### Task 4: Confirm resume_status invariant tests still pass unmodified

**Files:**
- No file changes — audit only.

**Interfaces:**
- Consumes: `tests/test_pico.py::test_report_prompt_metadata_preserves_initial_resume_status`, `tests/test_pico.py::test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup`.
- Produces: confirmation that P3 has adequate coverage.

- [ ] **Step 1: Locate the two tests**

Run:
```bash
grep -n "def test_report_prompt_metadata_preserves_initial_resume_status\|def test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup" tests/test_pico.py
```
Expected: two hits.

- [ ] **Step 2: Run the two tests**

Run:
```bash
uv run pytest tests/test_pico.py::test_report_prompt_metadata_preserves_initial_resume_status tests/test_pico.py::test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup -v
```
Expected: both PASS.

- [ ] **Step 3: Verify assertions reference dict keys**

Run:
```bash
sed -n '1575,1590p;1618,1626p' tests/test_pico.py
```
Expected: assertions of shape `report["resume_status"]`, `report["prompt_metadata"]["resume_status"]`, `report["prompt_metadata"]["last_prompt_resume_status"]`.

No commit — audit-only task. If the tests are missing or assertions have drifted, stop and open an issue.

### Task 5: P0 acceptance gate

**Files:** none.

- [ ] **Step 1: Run full quality gate**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 2: Confirm test counts increased by exactly 3**

Run: `git log --oneline HEAD~4..HEAD`
Expected: four commits — pre-flight docs, and three test-adding commits from Tasks 1–3.

---

## P1: Evaluation Split

Purpose: reduce `metrics_experiments.py` from 1311 lines to focused benchmark modules without losing the `metrics.py` public `__all__` surface.

### Task 6: Extract `benchmark_schema.py`

**Files:**
- Create: `pico/evaluation/benchmark_schema.py`
- Modify: `pico/evaluation/evaluator.py`
- Modify: `pico/evaluation/metrics.py`

**Interfaces:**
- Consumes: existing `BENCHMARK_SCHEMA_VERSION`, `validate_benchmark`, `load_benchmark`, `_fixture_snapshot_id`, `_scripted_outputs_for_task`, `_artifact_path_for_task`, `_workspace_relative`, `summarize_rows`, `_digest_file` (all currently in `pico/evaluation/evaluator.py`).
- Produces: `pico.evaluation.benchmark_schema.{BENCHMARK_SCHEMA_VERSION, validate_benchmark, load_benchmark, summarize_rows}` publicly; the underscored helpers remain module-private inside the new file.

- [ ] **Step 1: Identify exact line ranges to move**

Run:
```bash
grep -n "^def validate_benchmark\|^def load_benchmark\|^def summarize_rows\|^def _fixture_snapshot_id\|^def _scripted_outputs_for_task\|^def _artifact_path_for_task\|^def _workspace_relative\|^def _digest_file\|BENCHMARK_SCHEMA_VERSION = " pico/evaluation/evaluator.py
```
Expected: 9 matches. Record line numbers.

- [ ] **Step 2: Create the new module with copied content**

Copy `BENCHMARK_SCHEMA_VERSION`, `validate_benchmark`, `load_benchmark`, `summarize_rows`, `_fixture_snapshot_id`, `_scripted_outputs_for_task`, `_artifact_path_for_task`, `_workspace_relative`, `_digest_file` and their imports (json, hashlib, pathlib, etc.) into `pico/evaluation/benchmark_schema.py`. Do NOT alter behavior.

- [ ] **Step 3: Replace originals in `evaluator.py` with re-imports**

At the top of `pico/evaluation/evaluator.py`, add:
```python
from .benchmark_schema import (
    BENCHMARK_SCHEMA_VERSION,
    _artifact_path_for_task,
    _digest_file,
    _fixture_snapshot_id,
    _scripted_outputs_for_task,
    _workspace_relative,
    load_benchmark,
    summarize_rows,
    validate_benchmark,
)
```
Remove the original definitions of those symbols from `evaluator.py`.

- [ ] **Step 4: Ensure `metrics.py` re-export list unchanged**

Run: `python3 -c "from pico.evaluation import metrics; print(sorted(metrics.__all__))"`
Diff against the list captured in Task 0 Step 6. Expected: identical.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_evaluator.py tests/test_metrics.py tests/test_scripts.py tests/test_public_api_contract.py -q`
Expected: all pass.

- [ ] **Step 6: Run full quality gate**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add pico/evaluation/benchmark_schema.py pico/evaluation/evaluator.py
git commit -m "refactor: extract benchmark_schema module from evaluator"
```

### Task 7: Extract `fixed_benchmark.py`

**Files:**
- Create: `pico/evaluation/fixed_benchmark.py`
- Modify: `pico/evaluation/evaluator.py`

**Interfaces:**
- Consumes: `BenchmarkEvaluator` class (currently `pico/evaluation/evaluator.py:395`), `run_fixed_benchmark` (currently `:595`), `run_harness_regression_v2` (currently `:622`), plus their private helpers `_checkpoint_payload`, `_apply_task_setup`, `_agent_prompt_for_task`.
- Produces: `pico.evaluation.fixed_benchmark.{BenchmarkEvaluator, run_fixed_benchmark, run_harness_regression_v2}`.

- [ ] **Step 1: Identify boundaries**

Run:
```bash
grep -n "^class BenchmarkEvaluator\|^def _checkpoint_payload\|^def _apply_task_setup\|^def _agent_prompt_for_task\|^def run_fixed_benchmark\|^def run_harness_regression_v2" pico/evaluation/evaluator.py
```
Record line ranges.

- [ ] **Step 2: Create the new module**

Move all identified symbols into `pico/evaluation/fixed_benchmark.py`. Add necessary imports: `from .benchmark_schema import ...` for `validate_benchmark`, `load_benchmark`, `_scripted_outputs_for_task`, etc.

- [ ] **Step 3: Reduce `evaluator.py` to re-exports**

Replace `evaluator.py` body (after the benchmark_schema re-imports from Task 6) with:
```python
from .fixed_benchmark import (
    BenchmarkEvaluator,
    run_fixed_benchmark,
    run_harness_regression_v2,
)

__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "BenchmarkEvaluator",
    "load_benchmark",
    "run_fixed_benchmark",
    "run_harness_regression_v2",
    "summarize_rows",
    "validate_benchmark",
]
```
Keep the `benchmark_schema` re-imports above so existing `from pico.evaluation.evaluator import ...` sites still work.

- [ ] **Step 4: Verify `test_public_api_contract.py::test_lightweight_package_split_uses_package_paths_without_legacy_shims` still finds `BenchmarkEvaluator`**

Run: `uv run pytest tests/test_public_api_contract.py -q`
Expected: pass.

- [ ] **Step 5: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add pico/evaluation/fixed_benchmark.py pico/evaluation/evaluator.py
git commit -m "refactor: extract fixed_benchmark from evaluator"
```

### Task 8: Extract `provider_benchmark.py`

**Files:**
- Create: `pico/evaluation/provider_benchmark.py`
- Modify: `pico/evaluation/metrics_experiments.py`
- Modify: `pico/evaluation/metrics.py`

**Interfaces:**
- Consumes: `DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS` (currently `pico/evaluation/metrics_experiments.py:22`), `_provider_summary_from_artifact` (`:524`), `_provider_profile` (`:550`), `_make_provider_client` (`:592`), `_normalize_text` (`:614`), `run_provider_experiments` (`:621`).
- Produces: `pico.evaluation.provider_benchmark.{DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS, run_provider_experiments, _provider_profile, _provider_summary_from_artifact}`. Note: `_provider_profile` MUST remain a re-exportable name because `pico.evaluation.metrics.__all__` includes it (see `metrics.py:56`).

- [ ] **Step 1: Confirm the `metrics.py` symbols we must preserve**

Run:
```bash
grep -n "_provider_profile\|DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS\|run_provider_experiments" pico/evaluation/metrics.py
```
Expected: three symbols appear in both the `from .metrics_experiments import` block and `__all__`.

- [ ] **Step 2: Move symbols to the new module**

Cut the following from `pico/evaluation/metrics_experiments.py` and paste into `pico/evaluation/provider_benchmark.py`:
- `DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS`
- `_provider_summary_from_artifact`
- `_provider_profile`
- `_make_provider_client`
- `_normalize_text`
- `run_provider_experiments`

Add imports at the top of `provider_benchmark.py`:
```python
from ..config import load_project_env, provider_env
from ..providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OpenAICompatibleModelClient,
)
from ..workspace import WorkspaceContext
from .fixed_benchmark import run_fixed_benchmark
```

- [ ] **Step 3: Re-export from `metrics_experiments.py`**

At the top of `pico/evaluation/metrics_experiments.py`, add:
```python
from .provider_benchmark import (
    DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS,
    _make_provider_client,
    _normalize_text,
    _provider_profile,
    _provider_summary_from_artifact,
    run_provider_experiments,
)
```
This keeps `metrics.py` re-exports (which import from `metrics_experiments`) working unchanged.

- [ ] **Step 4: Verify `metrics.py.__all__` unchanged**

Run: `python3 -c "from pico.evaluation import metrics; print(sorted(metrics.__all__))"`
Diff against Task 0 Step 6 baseline. Expected: identical.

- [ ] **Step 5: Verify the CLI script still imports the constant**

Run:
```bash
python3 -c "from pico.evaluation.metrics import DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS, run_provider_experiments; print(DEFAULT_PROVIDER_EXPERIMENT_MAX_NEW_TOKENS)"
```
Expected: `2048`.

- [ ] **Step 6: Run scripts test**

Run: `uv run pytest tests/test_scripts.py tests/test_metrics.py tests/test_evaluator.py -q`
Expected: pass.

- [ ] **Step 7: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add pico/evaluation/provider_benchmark.py pico/evaluation/metrics_experiments.py
git commit -m "refactor: extract provider_benchmark module"
```

### Task 9: Evaluate phase 1.5 trigger

**Files:**
- Modify (conditional): `pico/evaluation/experiments_synthetic.py`, `pico/evaluation/experiments_real.py`, `pico/evaluation/experiments_recovery.py` — only if trigger fires.

**Interfaces:**
- Consumes: whatever remains in `metrics_experiments.py` after Task 8.
- Produces: possibly nothing; possibly three new modules.

- [ ] **Step 1: Measure remaining size**

Run: `wc -l pico/evaluation/metrics_experiments.py`

- [ ] **Step 2: Decide**

If line count ≤ 500: skip phase 1.5. Write a one-line note in the pre-flight notes file: `Phase 1.5 skipped — metrics_experiments.py is N lines, at or below 500-line soft threshold.` Commit that note only. Move to P2.

If > 500: split remaining clusters as follows:
- `experiments_synthetic.py` — `_MemoryExperimentModelClient` and everything from line ~97 to ~304 (memory/context stress/security synthetic scenarios); include `measure_feature_ablation_metrics`, `build_stress_agent_metrics`, `run_memory_dependency_experiment`, `run_large_scale_memory_experiment`, `run_context_stress_matrix`, `run_security_experiment_suite`.
- `experiments_real.py` — `_followup_trace_metrics` through `run_real_security_experiment_suite` (`_build_real_agent`, `run_real_memory_experiment`, `run_real_context_experiment`, `_setup_real_security_workspace`, `_security_result_row`, `_run_real_repeated_call_scenario`, `run_real_security_experiment_suite`).
- `experiments_recovery.py` — `_RecoveryScenarioModelClient`, `_build_recovery_agent`, `_apply_recovery_setup`, `_run_recovery_task_variant`, `_recovery_variant_summary`, `run_context_ablation_v2`, `run_memory_ablation_v2`, `run_recovery_ablation_v2`.

Add re-exports in `metrics_experiments.py` for every symbol currently in `metrics.py.__all__`.

- [ ] **Step 3: Run full check after decision**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 4: Commit (only if a split was performed)**

```bash
git add pico/evaluation/experiments_*.py pico/evaluation/metrics_experiments.py docs/superpowers/plans/2026-07-04-pre-flight-notes.md
git commit -m "refactor: split experiment clusters from metrics_experiments"
```

---

## P2: Provider Split

### Task 10: Audit Anthropic edge case tests (no changes)

**Files:** none — audit only.

**Interfaces:**
- Consumes: `tests/test_pico.py::test_anthropic_compatible_client_extracts_text_block_without_type`, `tests/test_pico.py::test_anthropic_compatible_client_explains_thinking_only_token_exhaustion`.
- Produces: confirmation.

- [ ] **Step 1: Locate**

Run:
```bash
grep -n "def test_anthropic_compatible_client_extracts_text_block_without_type\|def test_anthropic_compatible_client_explains_thinking_only_token_exhaustion" tests/test_pico.py
```
Expected: two hits (currently at `:807` and `:841`).

- [ ] **Step 2: Run**

Run:
```bash
uv run pytest tests/test_pico.py::test_anthropic_compatible_client_extracts_text_block_without_type tests/test_pico.py::test_anthropic_compatible_client_explains_thinking_only_token_exhaustion -v
```
Expected: both PASS. No commit.

### Task 11: Extract `pico/providers/_shared.py`

**Files:**
- Create: `pico/providers/_shared.py`
- Modify: `pico/providers/clients.py`

**Interfaces:**
- Consumes: `_normalize_versioned_base_url` (`clients.py:90`), `_iter_sse_data_payloads` (`:215`), `_iter_openai_stream_chunks` (`:226`), `_extract_usage_cache_details` (`:261`), `_optional_int` (`:520`).
- Produces: `pico.providers._shared.{_normalize_versioned_base_url, _iter_sse_data_payloads, _iter_openai_stream_chunks, _extract_usage_cache_details, _optional_int}`.

- [ ] **Step 1: Copy helpers into new module**

Create `pico/providers/_shared.py` with the five helpers, plus needed imports (`json`, typing). Do not alter logic.

- [ ] **Step 2: Replace originals with re-imports in `clients.py`**

Near the top of `pico/providers/clients.py` (after existing imports), add:
```python
from ._shared import (
    _extract_usage_cache_details,
    _iter_openai_stream_chunks,
    _iter_sse_data_payloads,
    _normalize_versioned_base_url,
    _optional_int,
)
```
Delete the original function definitions in `clients.py`.

- [ ] **Step 3: Full check**

Run: `./scripts/check.sh`
Expected: exit 0. All provider tests in `tests/test_pico.py` between lines 349–913 must pass.

- [ ] **Step 4: Commit**

```bash
git add pico/providers/_shared.py pico/providers/clients.py
git commit -m "refactor: extract provider shared helpers"
```

### Task 12: Extract `pico/providers/ollama.py`

**Files:**
- Create: `pico/providers/ollama.py`
- Modify: `pico/providers/clients.py`

**Interfaces:**
- Consumes: `FakeModelClient` (still in `clients.py`), `OllamaModelClient` class (currently `clients.py:36`).
- Produces: `pico.providers.ollama.OllamaModelClient`; re-exported from `clients.py`.

- [ ] **Step 1: Move `OllamaModelClient`**

Cut lines 36–87 of `clients.py` into `pico/providers/ollama.py`. Add imports the class needs (json, urllib, `._shared` helpers if any).

- [ ] **Step 2: Re-export in `clients.py`**

Add near the top:
```python
from .ollama import OllamaModelClient
```

- [ ] **Step 3: Run provider client tests**

Run: `uv run pytest tests/test_pico.py -k "ollama" -q`
Expected: all pass.

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pico/providers/ollama.py pico/providers/clients.py
git commit -m "refactor: extract OllamaModelClient into its own module"
```

### Task 13: Extract `pico/providers/openai_compatible.py`

**Files:**
- Create: `pico/providers/openai_compatible.py`
- Modify: `pico/providers/clients.py`

**Interfaces:**
- Consumes: `_extract_openai_text`, `_extract_openai_text_from_sse`, `_extract_openai_response_from_sse`, `OpenAICompatibleModelClient` (currently `clients.py:97–486`).
- Produces: `pico.providers.openai_compatible.OpenAICompatibleModelClient`.

- [ ] **Step 1: Move symbols**

Cut `_extract_openai_text`, `_extract_openai_text_from_sse`, `_extract_openai_response_from_sse`, and the entire `OpenAICompatibleModelClient` class into `pico/providers/openai_compatible.py`. Add imports:
```python
from ._shared import (
    _extract_usage_cache_details,
    _iter_openai_stream_chunks,
    _iter_sse_data_payloads,
    _normalize_versioned_base_url,
)
```
plus json, urllib, etc.

- [ ] **Step 2: Re-export**

In `clients.py`:
```python
from .openai_compatible import OpenAICompatibleModelClient
```

- [ ] **Step 3: Run OpenAI-compatible tests**

Run: `uv run pytest tests/test_pico.py -k "openai_compatible or sse" -q`
Expected: all pass.

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pico/providers/openai_compatible.py pico/providers/clients.py
git commit -m "refactor: extract OpenAICompatibleModelClient module"
```

### Task 14: Extract `pico/providers/anthropic_compatible.py`

**Files:**
- Create: `pico/providers/anthropic_compatible.py`
- Modify: `pico/providers/clients.py`

**Interfaces:**
- Consumes: `_extract_anthropic_text` (`clients.py:489`), `_anthropic_no_text_error` (`:498`), `_supports_anthropic_prompt_cache` (`:509`), `_anthropic_cache_control` (`:513`), `_extract_anthropic_usage_cache_details` (`:529`), `AnthropicCompatibleModelClient` class (`:549`).
- Produces: `pico.providers.anthropic_compatible.AnthropicCompatibleModelClient` and re-export via `clients.py`.

- [ ] **Step 1: Move**

Cut all six symbols into `pico/providers/anthropic_compatible.py`. Preserve the exact edge-case behavior at lines 489–495 (missing-type text block) and 498–506 (thinking-only + max_tokens error).

- [ ] **Step 2: Re-export**

```python
from .anthropic_compatible import AnthropicCompatibleModelClient
```

- [ ] **Step 3: Run Anthropic tests**

Run: `uv run pytest tests/test_pico.py -k "anthropic" -q`
Expected: all pass, including the two edge-case tests audited in Task 10.

- [ ] **Step 4: Run public API contract test**

Run: `uv run pytest tests/test_public_api_contract.py -q`
Expected: pass — proves the compatibility shell works.

- [ ] **Step 5: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 6: Verify `clients.py` line count**

Run: `wc -l pico/providers/clients.py`
Expected: dramatically reduced. Compatibility shell should be well under 100 lines.

- [ ] **Step 7: Commit**

```bash
git add pico/providers/anthropic_compatible.py pico/providers/clients.py
git commit -m "refactor: extract AnthropicCompatibleModelClient module"
```

### Task 15: P2 manual real-provider smoke

**Files:** none.

- [ ] **Step 1: Run the three manual commands from Task 0 Step 8**

Copy them from `docs/superpowers/plans/2026-07-04-pre-flight-notes.md` and execute. Expected: same behavior as before P2 (doctor OK; final answers match).

- [ ] **Step 2: Note result**

Append a one-line result to the pre-flight notes: `P2 real-provider smoke: PASS / FAIL (details)`. Commit only if annotations were added.

---

## P3: Runtime Report Boundary Tightening

### Task 16: Introduce `build_report_checkpoint_metadata` helper

**Files:**
- Modify: `pico/runtime.py`

**Interfaces:**
- Consumes: `task_state.resume_status`, `self.last_prompt_metadata` (both used at `pico/runtime.py:542-546`).
- Produces: module-level function `build_report_checkpoint_metadata(task_state, last_prompt_metadata: dict) -> dict` that returns a new dict of prompt-metadata fragment; `runtime.build_report` calls it.

- [ ] **Step 1: Read the current logic**

Run: `sed -n '540,565p' pico/runtime.py`
Expected: see the `setdefault("last_prompt_resume_status", ...)` block.

- [ ] **Step 2: Add the helper at module scope**

Above the `class Pico`, or near existing module-level utility functions in `pico/runtime.py`, add:

```python
def build_report_checkpoint_metadata(task_state, last_prompt_metadata):
    """Return a dict fragment to merge into report prompt_metadata.

    Preserves the invariant that the initial prompt-time resume_status is kept
    under last_prompt_resume_status when a later task_state.resume_status is
    promoted into report metadata.
    """
    fragment = dict(last_prompt_metadata)
    if task_state.resume_status:
        fragment.setdefault(
            "last_prompt_resume_status",
            fragment.get("resume_status", ""),
        )
        fragment["resume_status"] = task_state.resume_status
    return fragment
```

- [ ] **Step 3: Replace the inline block in `build_report`**

Change:
```python
def build_report(self, task_state):
    prompt_metadata = dict(self.last_prompt_metadata)
    if task_state.resume_status:
        prompt_metadata.setdefault("last_prompt_resume_status", prompt_metadata.get("resume_status", ""))
        prompt_metadata["resume_status"] = task_state.resume_status
```
to:
```python
def build_report(self, task_state):
    prompt_metadata = build_report_checkpoint_metadata(task_state, self.last_prompt_metadata)
```

- [ ] **Step 4: Verify shape unchanged**

Run:
```bash
uv run pytest tests/test_pico.py::test_report_prompt_metadata_preserves_initial_resume_status tests/test_pico.py::test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup -v
```
Expected: both pass without modification.

- [ ] **Step 5: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add pico/runtime.py
git commit -m "refactor: isolate report checkpoint metadata into helper"
```

---

## P4: CLI Split

### Task 17: P4.0 — Move `HELP_DETAILS` to `pico/cli_help.py`

**Files:**
- Create: `pico/cli_help.py`
- Modify: `pico/cli.py`
- Modify: `pico/cli_commands.py`

**Interfaces:**
- Consumes: existing `HELP_DETAILS` string constant at `pico/cli.py:122`.
- Produces: `pico.cli_help.HELP_DETAILS`; consumed by `pico.cli` and `pico.cli_commands.run_repl`.

- [ ] **Step 1: Locate `HELP_DETAILS` and its lazy import site**

Run:
```bash
grep -n "HELP_DETAILS" pico/cli.py pico/cli_commands.py
```
Expected: definition at `pico/cli.py:122`; lazy import at `pico/cli_commands.py:544`.

- [ ] **Step 2: Create `pico/cli_help.py`**

Move the entire multi-line `HELP_DETAILS = textwrap.dedent(...)` block into `pico/cli_help.py`. Include the `import textwrap` at the top of the new file.

- [ ] **Step 3: Re-export from `cli.py` for backward compatibility**

At the location where `HELP_DETAILS` was defined in `pico/cli.py`, add:
```python
from .cli_help import HELP_DETAILS  # noqa: F401
```

- [ ] **Step 4: Update REPL lazy import**

In `pico/cli_commands.py:544`, change:
```python
from .cli import HELP_DETAILS
```
to:
```python
from .cli_help import HELP_DETAILS
```
This breaks the `cli_commands → cli` circular import edge.

- [ ] **Step 5: Run REPL /help protection test**

Run: `uv run pytest tests/test_cli_commands.py::test_repl_help_renders_help_details -v`
Expected: PASS. If it fails, `HELP_DETAILS` text has changed shape — investigate.

- [ ] **Step 6: Run CLI help tests**

Run: `uv run pytest tests/test_cli_commands.py::test_help_command_shows_examples tests/test_cli_commands.py::test_help_flag_uses_root_help_without_argparse_dump -v`
Expected: PASS.

- [ ] **Step 7: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add pico/cli_help.py pico/cli.py pico/cli_commands.py
git commit -m "refactor: decouple HELP_DETAILS into cli_help module"
```

### Task 18: P4.1 — Colocate diagnostics handlers into `cli_diagnostics.py`

**Files:**
- Modify: `pico/cli_diagnostics.py`
- Modify: `pico/cli_commands.py`

**Interfaces:**
- Consumes: `handle_status` (`cli_commands.py:151`), `handle_doctor` (`:155`), `handle_config` (`:168`), `_render_status` (`:797`), `_render_config` (`:700`), `_render_doctor` (`:732`), and their helpers `_source_label`, `_line`, `_presence_text`, `_value_with_source`, `_ok_missing` (`:670-698`).
- Produces: `pico.cli_diagnostics.{handle_status, handle_doctor, handle_config}`; consumers `pico.cli` and existing tests keep using `pico.cli_commands` re-exports.

- [ ] **Step 1: Confirm consumers**

Run: `grep -n "handle_status\|handle_doctor\|handle_config" pico/cli.py`
Expected: three references in `_dispatch_*` at `:416, :420, :428`.

- [ ] **Step 2: Move handlers and renderers into `cli_diagnostics.py`**

Cut `handle_status`, `handle_doctor`, `handle_config`, `_render_status`, `_render_config`, `_render_doctor`, `_source_label`, `_line`, `_presence_text`, `_value_with_source`, `_ok_missing` from `cli_commands.py` and paste into `pico/cli_diagnostics.py`. Add imports needed by handlers (`.cli_diagnostics.collect_*` already lives there; add `.cli_errors`, `.cli_output`, `.workspace`, `print_result` as required).

- [ ] **Step 3: Check merged size**

Run: `wc -l pico/cli_diagnostics.py`
Expected: under ~550 (the soft threshold is ~500; small overshoot acceptable). If well over 500 (e.g., 700+), split handlers into `pico/cli_diagnostics_commands.py`. Otherwise keep merged.

- [ ] **Step 4: Keep re-export shims in `cli_commands.py`**

At the top of `pico/cli_commands.py`, add:
```python
from .cli_diagnostics import handle_config, handle_doctor, handle_status  # noqa: F401
```

- [ ] **Step 5: Update `pico/cli.py` to import from the new home (optional but preferred)**

In `pico/cli.py`, change the imports at line 15-25 so `handle_config`, `handle_doctor`, `handle_status` come from `.cli_diagnostics`. Leave the rest importing from `.cli_commands`.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/test_cli_diagnostics.py tests/test_cli_commands.py tests/memory/test_cli_diagnostics_v2.py -q`
Expected: all pass.

- [ ] **Step 7: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add pico/cli_diagnostics.py pico/cli_commands.py pico/cli.py
git commit -m "refactor: colocate diagnostics command handlers with data collectors"
```

### Task 19: P4.2 — Extract `pico/cli_recovery.py`

**Files:**
- Create: `pico/cli_recovery.py`
- Modify: `pico/cli_commands.py`

**Interfaces:**
- Consumes: `handle_checkpoints` (`cli_commands.py:78`), `handle_runs` (`:124`), `handle_sessions` (`:244`), `_render_checkpoints_list` (`:600`), `_render_restore_plan` (`:646`), `_render_runs_list` (`:779`), `_render_sessions_list` (`:783`), `_render_runs_show` (`:825`), plus helpers `_resolve_checkpoint_id` (`:841`), `_load_checkpoint_record` (`:875`), `_preview_restore` (`:887`), `_apply_restore` (`:899`), `_load_run_artifacts` (`:832`), `_session_files` (`:787`), `_is_restore_args` (`:607`), `_parse_prune_args` (`:611`), `_prune_usage_error` (`:634`).
- Produces: `pico.cli_recovery.{handle_checkpoints, handle_runs, handle_sessions}`; re-exported from `cli_commands.py`.

- [ ] **Step 1: Move all named symbols into `cli_recovery.py`**

Preserve imports: `CheckpointStore`, `RecoveryCheckpointWriter`, `RecoveryManager`, `WorkspaceContext`, `print_result`, `format_json`, `success_envelope`, `CliError`, `CLI_EXIT_USAGE`.

- [ ] **Step 2: Add compatibility re-exports to `cli_commands.py`**

```python
from .cli_recovery import handle_checkpoints, handle_runs, handle_sessions  # noqa: F401
```

- [ ] **Step 3: Run recovery tests**

Run: `uv run pytest tests/test_recovery_cli.py tests/test_recovery_e2e.py -q`
Expected: all pass.

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pico/cli_recovery.py pico/cli_commands.py
git commit -m "refactor: extract recovery command handlers into cli_recovery"
```

### Task 20: P4.3 — Extract `pico/cli_memory.py`

**Files:**
- Create: `pico/cli_memory.py`
- Modify: `pico/cli_commands.py`

**Interfaces:**
- Consumes: `handle_memory` (`cli_commands.py:271`), `_memory_list_cmd` (`:304`), `_memory_show_cmd` (`:342`), `_memory_search_cmd` (`:377`), `_memory_review_cmd` (`:419`), `_memory_migrate_cmd` (`:443`).
- Produces: `pico.cli_memory.handle_memory`; re-exported from `cli_commands.py`.

- [ ] **Step 1: Move symbols**

Cut all six named functions plus their nested `render` closures into `pico/cli_memory.py`. Preserve the lazy `BlockStore` import at the top of `handle_memory`.

- [ ] **Step 2: Re-export**

```python
from .cli_memory import handle_memory  # noqa: F401
```

- [ ] **Step 3: Run memory CLI tests**

Run: `uv run pytest tests/memory/test_cli_memory_commands.py tests/memory/test_migration.py tests/memory/test_repl_v2.py -q`
Expected: all pass. These currently import `handle_memory` from `pico.cli_commands`; the re-export must keep working.

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pico/cli_memory.py pico/cli_commands.py
git commit -m "refactor: extract memory command handlers into cli_memory"
```

### Task 21: P4.4 — Extract `pico/cli_start.py`

**Files:**
- Create: `pico/cli_start.py`
- Modify: `pico/cli_commands.py`

**Interfaces:**
- Consumes: `run_agent_once` (`cli_commands.py:518`), `run_repl` (`:531`).
- Produces: `pico.cli_start.{run_agent_once, run_repl}`; re-exported from `cli_commands.py`.

- [ ] **Step 1: Move both functions**

Cut `run_agent_once` and `run_repl` (with slash-command routing) into `pico/cli_start.py`. Preserve the `.cli_help import HELP_DETAILS` from Task 17 — it must remain at the point of use to keep import graph clean.

- [ ] **Step 2: Re-export**

```python
from .cli_start import run_agent_once, run_repl  # noqa: F401
```

- [ ] **Step 3: Run REPL tests**

Run: `uv run pytest tests/test_cli_commands.py tests/memory/test_repl_v2.py -q`
Expected: all pass, including the REPL /help protection test from Task 2.

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pico/cli_start.py pico/cli_commands.py
git commit -m "refactor: extract run-once and REPL flow into cli_start"
```

### Task 22: P4.5 — Extract `pico/cli_renderers.py`

**Files:**
- Create: `pico/cli_renderers.py`
- Modify: `pico/cli_commands.py`

**Interfaces:**
- Consumes: whatever `_render_*` and rendering helpers remain in `cli_commands.py` after Tasks 18–21.
- Produces: those symbols under `pico.cli_renderers`.

- [ ] **Step 1: List remaining `_render_*` symbols**

Run: `grep -n "^def _render_\|^def print_result" pico/cli_commands.py`

- [ ] **Step 2: Decide**

If fewer than three remain and they are consumed by only one command family, leave them where they are and skip this task. If three or more remain and are consumed cross-family (e.g., `print_result`), move them to `pico/cli_renderers.py`.

- [ ] **Step 3: If extracted, re-export**

```python
from .cli_renderers import print_result  # noqa: F401
# plus any other symbols moved
```

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Measure `cli_commands.py` (always run, even if Step 2 skipped extraction)**

Run: `wc -l pico/cli_commands.py`
Expected: at or under 300 lines (soft target from Section 12). If well over 300, list what remains via `grep -n "^def " pico/cli_commands.py` and record in the pre-flight notes as a follow-up task; do not force-cut.

- [ ] **Step 6: Commit (only if extraction happened in Step 2)**

```bash
git add pico/cli_renderers.py pico/cli_commands.py
git commit -m "refactor: extract shared cli rendering helpers"
```

### Task 23: P4 manual smoke

**Files:** none.

- [ ] **Step 1: Rerun manual real-provider commands**

Same three commands from Task 0 Step 8. Expected: unchanged behavior.

- [ ] **Step 2: Verify JSON envelope shape**

Run:
```bash
pico-cli --cwd /Users/wei/Desktop/pico --format json status
```
Expected: JSON with `ok`, `kind`, `data` keys.

- [ ] **Step 3: Interactive REPL smoke**

Run `pico-cli --cwd /Users/wei/Desktop/pico`; issue `/help`, `/memory`, `/exit`. Expected: same output as before P4.

- [ ] **Step 4: Note result in pre-flight notes**

Append `P4 manual smoke: PASS/FAIL` line. If notes changed, commit:
```bash
git add docs/superpowers/plans/2026-07-04-pre-flight-notes.md
git commit -m "docs: record P4 manual smoke result"
```

---

## P5: Test Layout Cleanup

### Task 24: Add section banners to `tests/test_pico.py`

**Files:**
- Modify: `tests/test_pico.py`

**Interfaces:**
- Consumes: existing 68 tests in `tests/test_pico.py`.
- Produces: in-file section banners marking four clusters. No test moved.

- [ ] **Step 1: Identify clusters**

Approximate line ranges:
- Provider client tests: 349–913
- Build agent / arg parser / packaging tests: 914–1214, 1970–2018
- Runtime/report/resume tests: 1242–1948
- Agent integration smoke: 52–349 (top portion)

- [ ] **Step 2: Add banners**

Insert banner comments at each cluster boundary. Example:
```python
# =============================================================================
# Provider client tests
# =============================================================================
```

Place one immediately before line 349, one before line 914, one before line 1242, one before line 52 (if practical), and one before 1970.

- [ ] **Step 3: Run tests to prove no accidental collection break**

Run: `uv run pytest tests/test_pico.py -q --collect-only | tail -20`
Expected: same count (68) as before.

- [ ] **Step 4: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add tests/test_pico.py
git commit -m "test: add section banners to test_pico for cluster visibility"
```

### Task 25: Extract provider-client cluster into `tests/test_provider_clients.py`

**Files:**
- Create: `tests/test_provider_clients.py`
- Modify: `tests/test_pico.py`

**Interfaces:**
- Consumes: the ~13 provider-client tests currently at `tests/test_pico.py:349–913`.
- Produces: same tests, now in the new file, unchanged assertions.

- [ ] **Step 1: List tests to move**

Run:
```bash
grep -n "^def test_ollama_client\|^def test_openai_compatible_client\|^def test_anthropic_compatible_client\|^def test_anthropic_stream_complete\|^def test_openai_compatible_streaming" tests/test_pico.py
```
Expected: ~13 matches between lines 349–913.

- [ ] **Step 2: Copy tests and imports**

Copy all matched test functions plus the imports they need (from `pico.providers.clients`, urllib mocking helpers, `pytest`) into `tests/test_provider_clients.py`. Keep function bodies byte-identical.

- [ ] **Step 3: Delete from `tests/test_pico.py`**

Remove the same functions from `tests/test_pico.py`. Preserve the section banner if it now bookends nothing — replace it with a one-line note: `# Provider client tests moved to tests/test_provider_clients.py`.

- [ ] **Step 4: Verify collection unchanged**

Run:
```bash
uv run pytest tests/test_pico.py tests/test_provider_clients.py -q --collect-only | wc -l
```
Expected: total test count unchanged.

- [ ] **Step 5: Run moved tests**

Run: `uv run pytest tests/test_provider_clients.py -q`
Expected: pass.

- [ ] **Step 6: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add tests/test_provider_clients.py tests/test_pico.py
git commit -m "test: extract provider-client cluster into dedicated file"
```

### Task 26: Assess further extraction opt-in

**Files:** none unless triggered.

- [ ] **Step 1: Measure `tests/test_pico.py`**

Run: `wc -l tests/test_pico.py`

- [ ] **Step 2: Decide**

If < 1000 lines: stop. P5 is complete.

If ≥ 1000 lines and runtime-report cluster can be moved without shared fixtures: extract it into `tests/test_runtime_report.py`. Same procedure as Task 25 (copy, delete, verify count, commit).

Otherwise: leave in place and record why in pre-flight notes.

- [ ] **Step 3: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

---

## Final Acceptance

### Task 27: End-to-end acceptance

**Files:** none.

- [ ] **Step 1: Line count summary**

Run:
```bash
wc -l pico/cli_commands.py pico/evaluation/metrics_experiments.py pico/providers/clients.py pico/runtime.py tests/test_pico.py pico/evaluation/*.py pico/providers/*.py pico/cli_*.py
```

Compare to baseline in Task 0 Step 1. Expected:
- `pico/cli_commands.py`: ≤ 300 (soft target)
- `pico/evaluation/metrics_experiments.py`: ≤ 500 or unchanged (if phase 1.5 skipped)
- `pico/providers/clients.py`: forwarding shell, < 100 lines
- New files each ≤ 500 lines (or documented reason otherwise)

- [ ] **Step 2: Public API contract**

Run: `uv run pytest tests/test_public_api_contract.py -q`
Expected: pass.

- [ ] **Step 3: Full check**

Run: `./scripts/check.sh`
Expected: exit 0.

- [ ] **Step 4: Manual real-provider one more time**

Same three commands. Expected: unchanged behavior.

- [ ] **Step 5: Update pre-flight notes with final line counts**

Append a "Final state" section to `docs/superpowers/plans/2026-07-04-pre-flight-notes.md` with the wc output from step 1.

- [ ] **Step 6: Commit notes**

```bash
git add docs/superpowers/plans/2026-07-04-pre-flight-notes.md
git commit -m "docs: record final line counts after convergence"
```

---

## Summary of Commits (target order)

1. `docs: pre-flight audit for architecture convergence` (Task 0)
2. `test: pin public import surface for all four provider clients` (Task 1)
3. `test: pin REPL /help path against silent HELP_DETAILS drop` (Task 2)
4. `test: freeze failure_category enum literals for benchmark artifacts` (Task 3)
5. `refactor: extract benchmark_schema module from evaluator` (Task 6)
6. `refactor: extract fixed_benchmark from evaluator` (Task 7)
7. `refactor: extract provider_benchmark module` (Task 8)
8. (conditional) `refactor: split experiment clusters from metrics_experiments` (Task 9)
9. `refactor: extract provider shared helpers` (Task 11)
10. `refactor: extract OllamaModelClient into its own module` (Task 12)
11. `refactor: extract OpenAICompatibleModelClient module` (Task 13)
12. `refactor: extract AnthropicCompatibleModelClient module` (Task 14)
13. `refactor: isolate report checkpoint metadata into helper` (Task 16)
14. `refactor: decouple HELP_DETAILS into cli_help module` (Task 17)
15. `refactor: colocate diagnostics command handlers with data collectors` (Task 18)
16. `refactor: extract recovery command handlers into cli_recovery` (Task 19)
17. `refactor: extract memory command handlers into cli_memory` (Task 20)
18. `refactor: extract run-once and REPL flow into cli_start` (Task 21)
19. (conditional) `refactor: extract shared cli rendering helpers` (Task 22)
20. `test: add section banners to test_pico for cluster visibility` (Task 24)
21. `test: extract provider-client cluster into dedicated file` (Task 25)
22. `docs: record final line counts after convergence` (Task 27)

Between-phase manual smoke results (Tasks 15, 23) commit only if notes were appended.
