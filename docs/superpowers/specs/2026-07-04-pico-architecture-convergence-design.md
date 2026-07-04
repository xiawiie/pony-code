# Pico Architecture Convergence Design

**Status:** Draft for user review
**Date:** 2026-07-04
**Branch:** `memory`
**Baseline commit:** `c33020f fix: improve real provider benchmark reliability`

## 1. Context

Pico is already more than a thin command wrapper around a model. It is a
local coding-agent harness with explicit runtime policy, bounded context
assembly, constrained tools, recoverable editing artifacts, run traces,
provider adapters, memory surfaces, and benchmark evidence.

The most recent real-provider work proved that the core harness can work
with an actual configured model. The `deepseek` provider configured with
`qwen3.7-max` passed:

- direct final-answer smoke;
- `read_file` tool-call smoke with trace evidence;
- the 10-task real provider benchmark;
- the local quality gate, `./scripts/check.sh`.

That same work also exposed where Pico is now most fragile. The failures
were not simply "the model is weak." They crossed several boundaries:

- provider response extraction did not handle reasoning-heavy responses
  well enough;
- the provider benchmark token budget was too small for models that emit
  thinking blocks before text;
- rejected tool calls consumed effective tool-step budget;
- benchmark prompts hid verifier success criteria from real models;
- report metadata could overwrite the initial resume status with a later
  prompt status.

These are architecture signals. Pico's main risk is no longer a missing
feature. The main risk is that several valuable capabilities now meet in a
small number of large files and overloaded semantic boundaries.

This design therefore focuses on architecture convergence: preserving the
current behavior while making the system easier to reason about, test, and
extend in the next small version.

## 2. Problem Statement

Pico has coherent domain language and real behavior, but the codebase is
starting to concentrate too much responsibility in a few places:

- `pico/cli_commands.py` owns many command families, renderers, parsing
  helpers, and compatibility behavior.
- `pico/evaluation/metrics_experiments.py` mixes synthetic experiments,
  real provider benchmark setup, provider profile construction, reporting
  helpers, and recovery experiment code.
- `pico/providers/clients.py` owns request construction, response text
  extraction, usage extraction, streaming parsing, prompt-cache plumbing,
  retry behavior, and provider-specific error messages.
- `pico/runtime.py`, `pico/agent_loop.py`, and evaluator code all need to
  understand pieces of resume-summary checkpoints and recoverable editing
  checkpoints.
- `tests/test_pico.py` has become a broad integration and regression
  bucket, which makes future failures harder to localize.

The current design is not broken. The risk is that further feature work
will become slower and less reliable unless these responsibilities are
split along existing domain boundaries.

## 3. Goals

1. Reduce concentrated complexity without changing the user-facing Pico
   workflow.
2. Preserve all current public import surfaces and CLI JSON contracts.
3. Preserve the real provider behavior proven by the current benchmark.
4. Make provider failures easier to classify as auth, network, response
   shape, token budget, or verifier/runtime failure.
5. Make the two checkpoint systems harder to confuse:
   resume-summary checkpoints for prompt continuity, and recoverable
   checkpoints for restore and review.
6. Separate deterministic benchmark logic from real provider benchmark
   logic.
7. Keep every step independently verifiable with focused tests plus the
   full local quality gate.

## 4. Non-Goals

This version does not add a new product surface. It deliberately avoids:

- a TUI or web UI;
- semantic or embedding-based memory retrieval;
- new provider SDK dependencies;
- a rewrite of recovery storage schemas;
- a plugin marketplace;
- GitHub PR automation;
- a multi-provider dashboard;
- a broad prompt rewrite;
- a global project manager layer;
- changing `.pico/checkpoints/` compatibility.

The purpose is to make the existing harness easier to maintain, not to
expand the product surface.

## 5. Current Architecture Map

The current runtime path is:

```text
pico.cli
  -> pico.runtime.Pico
    -> pico.agent_loop.AgentLoop
      -> pico.context_manager.ContextManager
      -> pico.providers.clients.*
      -> pico.tool_executor.ToolExecutor
        -> pico.tools
        -> pico.tool_change_recorder
        -> pico.checkpoint_store
      -> pico.run_store
      -> pico.checkpoint
      -> pico.recovery_checkpoint_writer
      -> pico.recovery_manager
```

The intended layer responsibilities are:

```text
CLI Surface
  Parse user intent, construct the runtime, render text and JSON output.

Runtime Orchestration
  Coordinate one agent turn, task state, prompt building, provider calls,
  tool execution, trace events, and final reports.

Context Assembly
  Build bounded prompts from stable prefix, memory index, workspace state,
  checkpoint text, history, and current request.

Tool Boundary
  Validate model-requested tools, enforce approval and command policy,
  execute tools, observe side effects, and return tool metadata.

Recovery Boundary
  Persist Tool Change Records, Checkpoint Records, file-state blobs, and
  restore plans. Decide restore, conflict, and review outcomes.

Provider Adapters
  Flatten provider-specific HTTP protocols and response shapes into a
  stable `complete()` and `stream_complete()` contract.

Evaluation Harness
  Run deterministic and real-provider benchmarks, collect reports, and
  explain failures.
```

This shape should be kept. The work is to make the code match these
boundaries more directly.

## 6. Proposed Architecture Boundaries

### 6.1 CLI Surface

The CLI should only own command parsing, command dispatch, and output
rendering. It should not own provider protocol details or recovery
decision logic.

Target modules:

- `pico/cli_start.py`
  - `run_agent_once`
  - `run_repl`
  - REPL slash command routing

- `pico/cli_inspect.py`
  - `handle_status`
  - `handle_doctor`
  - `handle_config`

- `pico/cli_recovery.py`
  - `handle_runs`
  - `handle_sessions`
  - `handle_checkpoints`
  - restore preview, restore apply, and prune argument parsing

- `pico/cli_memory.py`
  - `handle_memory`
  - `memory list/show/search/review/migrate`

- `pico/cli_renderers.py`
  - shared text renderers
  - JSON body rendering helpers
  - common output line helpers

- `pico/cli_commands.py`
  - compatibility export layer and thin forwarding surface

`pico/cli.py` should continue to own top-level argument parsing and
agent construction.

### 6.2 Runtime Orchestration

`Pico` should remain the runtime facade. `AgentLoop` should remain the
turn control loop.

This version should not aggressively split `runtime.py`. The useful
convergence point is to remove semantic assembly that belongs elsewhere:

- report checkpoint metadata should be delegated to a checkpoint facade;
- provider error metadata should be passed through from provider clients;
- run diagnostics should be derived from task state and trace facts;
- direct understanding of two checkpoint systems should be reduced.

Potential future extraction:

- `pico/run_reporting.py` for report and diagnostic summary construction.

That extraction is optional for this version unless it falls naturally out
of the checkpoint facade work.

### 6.3 Context Assembly

`ContextManager` is already focused enough. Its contract should be made
more explicit rather than moved:

- current request is never reduced;
- stable prefix hash is the prompt-cache key;
- volatile workspace state belongs with history, not stable prefix;
- prompt metadata must explain budget reductions and resume state;
- checkpoint text is context input, not recovery truth.

This version should avoid a broad prompt rewrite. Any change here should
be driven by diagnostics or provider benchmark stability.

### 6.4 Tool Boundary

`ToolExecutor.execute()` should remain the only path for executing model
tool requests.

The executor may delegate helper responsibilities:

- command policy evaluation;
- shell side-effect capture;
- file-entry construction;
- recovery finalization helpers.

But the orchestration rule stays the same:

```text
validate -> reject/approve -> start pending change if needed
-> execute -> observe side effects -> finalize record -> return metadata
```

Rejected tool calls should not consume effective `tool_steps`, but they
must remain visible for diagnostics.

### 6.5 Recovery Boundary

The project currently has two checkpoint systems:

1. Resume-summary checkpoint
   - owner: `pico/checkpoint.py`
   - storage: session data
   - purpose: prompt continuity, stale state detection, workspace mismatch
   - not for file restore

2. Recoverable editing checkpoint
   - owner: `pico/checkpoint_store.py`,
     `pico/recovery_checkpoint_writer.py`, `pico/recovery_manager.py`
   - storage: `.pico/checkpoints/`
   - purpose: review, conflict detection, restore planning, file-state
     blobs
   - not for prompt transcript continuity

The systems should not be merged in this version. Instead, add a thin
facade with explicit names so callers do not need to reconstruct the
semantic distinction.

Candidate module:

```text
pico/checkpoint_facade.py
```

Candidate functions:

```python
def evaluate_prompt_resume(agent) -> dict:
    ...

def create_resume_summary(agent, task_state, user_message, trigger) -> dict:
    ...

def finalize_turn_recovery(agent, task_state, tool_change_ids, verification_evidence, trigger):
    ...

def build_report_checkpoint_metadata(task_state, last_prompt_metadata) -> dict:
    ...
```

The facade should not invent a new schema. It should clarify existing
ownership and reduce repeated interpretation at call sites.

### 6.6 Provider Adapters

The public contract should stay small:

```python
class ModelClient:
    supports_prompt_cache: bool
    last_completion_metadata: dict

    def complete(
        self,
        prompt,
        max_new_tokens,
        *,
        prompt_cache_key=None,
        prompt_cache_retention=None,
    ) -> str:
        ...

    def stream_complete(...):
        ...
```

Internal provider code should be split by concern:

- request payload and headers;
- response text extraction;
- usage and cache metadata extraction;
- SSE parsing;
- error mapping.

Target modules:

- `pico/providers/openai_compatible.py`
- `pico/providers/anthropic_compatible.py`
- `pico/providers/ollama.py`
- `pico/providers/usage.py`
- `pico/providers/errors.py`
- `pico/providers/clients.py` as compatibility exports

The recent real-provider fixes must be preserved:

- Anthropic-compatible content blocks with `text` but missing `type` must
  still be accepted.
- Thinking-only responses that stop with `max_tokens` must produce a
  clear output-budget error.
- Provider benchmark default output budget must remain 2048.

### 6.7 Evaluation Harness

Evaluation should be split into deterministic, real-provider, and report
layers.

Target modules:

- `pico/evaluation/benchmark_schema.py`
  - schema constants
  - `validate_benchmark`
  - `load_benchmark`
  - fixture helpers

- `pico/evaluation/fixed_benchmark.py`
  - `BenchmarkEvaluator`
  - `run_fixed_benchmark`
  - scripted fake model outputs

- `pico/evaluation/provider_benchmark.py`
  - provider profile resolution
  - real provider client factories
  - `run_provider_experiments`
  - real provider benchmark defaults

- `pico/evaluation/metrics_reports.py`
  - report aggregation and markdown rendering, already partly split

- `pico/evaluation/metrics.py`
  - compatibility export layer

The real-provider benchmark should be able to say whether a failure came
from provider auth, provider network, provider response shape, output
budget, benchmark verifier failure, runtime stop reason, or missing
artifact.

## 7. Work Packages

### P0: Protection Net

Purpose: freeze behavior before structural movement.

Tasks:

1. List public compatibility imports:
   - `pico.cli.main`
   - `pico.cli.build_agent`
   - `pico.cli.build_arg_parser`
   - `pico.runtime.Pico`
   - `pico.runtime.SessionStore`
   - `pico.providers.clients.FakeModelClient`
   - `pico.providers.clients.OllamaModelClient`
   - `pico.providers.clients.OpenAICompatibleModelClient`
   - `pico.providers.clients.AnthropicCompatibleModelClient`
   - `pico.evaluation.metrics.run_fixed_benchmark`
   - `pico.evaluation.metrics.run_provider_experiments`

2. Add or confirm CLI JSON contract tests for:
   - `config show`
   - `doctor`
   - `status`
   - `runs show`
   - `checkpoints preview-restore`

3. Document manual real-provider checks:
   - `pico-cli doctor`
   - direct final smoke
   - `read_file` tool smoke
   - current provider 10-task benchmark

4. Confirm benchmark artifact fields:
   - `summary.total_tasks`
   - `summary.pass_rate`
   - `summary.verifier_pass_rate`
   - `rows[].failure_category`
   - `rows[].report.prompt_metadata`

Acceptance:

- `./scripts/check.sh` passes.
- Manual real-provider checks are documented.
- No public import is removed.

### P1: Evaluation and Provider Benchmark Boundary

Purpose: separate deterministic benchmark logic from real-provider logic.

Tasks:

1. Extract benchmark schema helpers to `benchmark_schema.py`.
2. Move `BenchmarkEvaluator` and fake scripted outputs to
   `fixed_benchmark.py`.
3. Move provider profile and provider experiment code to
   `provider_benchmark.py`.
4. Keep `metrics.py` exporting the same public functions and constants.
5. Keep `scripts/run_provider_experiments.py` using public exports.

Acceptance:

- `tests/test_evaluator.py` passes.
- `tests/test_metrics.py` passes.
- `tests/test_scripts.py` passes.
- `run_provider_experiments` remains import-compatible.
- Provider benchmark default `max_new_tokens` remains 2048.

### P2: Provider Adapter Contract

Purpose: make provider behavior easier to test and diagnose.

Tasks:

1. Extract usage metadata helpers.
2. Extract provider error helpers.
3. Extract OpenAI-compatible text and SSE extraction.
4. Extract Anthropic-compatible text and no-text diagnostics.
5. Preserve public classes through `providers/clients.py`.
6. Add focused tests for response shapes and error categories.

Acceptance:

- OpenAI JSON extraction is covered.
- OpenAI SSE extraction is covered.
- Anthropic text block extraction is covered, including missing `type`.
- Thinking-only max-token exhaustion is covered.
- `pico-cli doctor` behavior is unchanged.
- Real provider smoke still passes.

### P3: Checkpoint and Recovery Facade

Purpose: reduce misuse of the two checkpoint systems.

Tasks:

1. Add a thin checkpoint facade with explicit function names.
2. Route `AgentLoop` resume-summary checkpoint creation through it.
3. Route turn recovery finalization through it or a closely named helper.
4. Route report checkpoint metadata through it.
5. Update architecture docs to state the owner and purpose of each
   checkpoint system.

Acceptance:

- Recovery tests pass.
- Freshness mismatch benchmark passes.
- Workspace mismatch benchmark passes.
- Report `resume_status` and `last_prompt_resume_status` remain stable.
- `.pico/checkpoints/` schema does not change.

### P4: CLI Surface Split

Purpose: reduce `cli_commands.py` size without changing command behavior.

Tasks:

1. Extract start and REPL command handling.
2. Extract inspect commands.
3. Extract recovery commands.
4. Extract memory commands.
5. Extract renderers.
6. Keep `cli_commands.py` as compatibility forwarding.

Acceptance:

- CLI tests pass after each command-family extraction.
- JSON output stays JSON-only on stdout.
- Help output remains useful and stable.
- No third-party CLI framework is introduced.

### P5: Test Layout Cleanup

Purpose: make failures easier to localize.

Tasks:

1. Move provider client tests to `tests/test_provider_clients.py`.
2. Move runtime report tests to `tests/test_runtime_report.py`.
3. Move provider benchmark tests to
   `tests/test_evaluation_provider_benchmark.py`.
4. Keep `tests/test_pico.py` focused on public API and integration smoke.
5. Confirm pytest collection stays stable.

Acceptance:

- Test count stays effectively stable unless intentionally changed.
- Test names map to architecture boundaries.
- No assertions are weakened during moves.

## 8. Data Flow and Diagnostics

The run lifecycle should preserve facts at the layer where they happen:

```text
user request
  -> CLI command invocation
  -> task_state + run_started trace
  -> prompt + prompt_metadata
  -> provider call + completion_metadata
  -> parsed model output
  -> tool execution + tool metadata
  -> recovery checkpoint records if needed
  -> final report
```

Facts should not be overwritten by later layers:

- `prompt_metadata` explains prompt construction.
- `completion_metadata` explains provider behavior.
- `tool metadata` explains validation, rejection, execution, and side
  effects.
- recovery metadata explains restore eligibility and checkpoint links.
- report summarizes; it does not become the source of event truth.
- trace records events; it does not become the restore truth.
- checkpoint store remains the restore truth.

Suggested report diagnostics:

```json
{
  "diagnostics": {
    "model_turns": 2,
    "tool_attempts": 1,
    "rejected_tool_calls": 0,
    "provider_error_category": "",
    "last_provider_http_status": "",
    "resume_status": "no-checkpoint",
    "last_prompt_resume_status": "full-valid",
    "verification_count": 1
  }
}
```

This does not need to be added in the first implementation step. It is
the target shape for clearer run summaries.

## 9. Error Handling

Provider errors should be classifiable:

- `provider_auth_error`
  - HTTP 401 or 403, invalid key, no quota, missing permission.

- `provider_network_error`
  - timeout, DNS failure, connection reset, remote disconnect.

- `provider_response_shape_error`
  - response parsed but text cannot be extracted.

- `provider_output_budget_error`
  - response contains thinking blocks but no text before `max_tokens`.

- `provider_usage_metadata_missing`
  - text exists, but usage or cache metadata is absent.

Benchmark failures should remain classifiable:

- `missing_artifact`
- `budget_exceeded`
- `verifier_failed`
- `failure_stop_reason`
- `provider_error`
- `unknown`

Runtime rules:

- provider errors must not be silently converted into successful final
  answers;
- rejected tool calls must not consume effective `tool_steps`;
- rejected tool calls must still be visible for diagnostics;
- `run_shell` nonzero exit is not automatically a runtime failure;
- verifier failure remains a benchmark failure;
- restore apply only writes when expected hashes match;
- conflict and review states never auto-write files.

## 10. Compatibility Contract

This convergence work must preserve:

- CLI entrypoints:
  - `pico`
  - `pico-cli`
  - `python -m pico`

- primary commands:
  - `run`
  - `repl`
  - `status`
  - `doctor`
  - `config show`
  - `runs list/show`
  - `sessions list/show`
  - `checkpoints list/show/preview-restore/restore/prune`
  - `memory list/show/search/review/migrate`

- JSON envelope shape:
  - `ok`
  - `kind`
  - `data` on success
  - `error.code`, `error.message`, optional `error.hint` on failure

- provider public classes:
  - `FakeModelClient`
  - `OllamaModelClient`
  - `OpenAICompatibleModelClient`
  - `AnthropicCompatibleModelClient`

- evaluation public exports from `pico.evaluation.metrics`.

The work may add fields to reports or metadata, but it should not remove
fields currently used by tests or benchmark artifacts without a focused
compatibility decision.

## 11. Validation Plan

Every phase should end with:

```bash
./scripts/check.sh
```

Focused validation by phase:

```bash
# Evaluation/provider split
uv run pytest tests/test_evaluator.py tests/test_metrics.py tests/test_scripts.py -q

# Provider adapter split
uv run pytest tests/test_pico.py tests/test_scripts.py -q

# Checkpoint/recovery facade
uv run pytest tests/test_checkpoint.py tests/test_recovery_*.py tests/test_evaluator.py -q

# CLI split
uv run pytest tests/test_cli_*.py tests/memory/test_cli_*.py -q
```

Manual real-provider validation:

```bash
pico-cli --cwd /Users/wei/Desktop/pico doctor

pico-cli --cwd /Users/wei/Desktop/pico --format json run --max-new-tokens 2048 \
  "Reply exactly with <final>REAL_PROVIDER_SMOKE_OK</final> and no other text."

pico-cli --cwd /Users/wei/Desktop/pico --format json run --max-new-tokens 2048 \
  "Use the read_file tool to read README.md, then reply exactly with <final>REAL_PROVIDER_TOOL_OK</final> and no other text."
```

The 10-task current-provider benchmark can be run through the provider
benchmark helper or a targeted `run_fixed_benchmark` invocation using the
current provider factory. The expected result for the current
`deepseek/qwen3.7-max` setup is 10/10 unless external provider state has
changed.

## 12. Rollout Order

Recommended order:

1. P0 Protection Net
2. P1 Evaluation and Provider Benchmark Boundary
3. P2 Provider Adapter Contract
4. P3 Checkpoint and Recovery Facade
5. P4 CLI Surface Split
6. P5 Test Layout Cleanup

Reasoning:

- The real-provider failure happened at the provider/evaluation/runtime
  metadata boundary, so that area has the highest immediate leverage.
- Provider extraction before CLI extraction keeps the recent correctness
  fixes easy to protect.
- The checkpoint facade should happen before more report or evaluator
  work, because it clarifies resume and recovery metadata.
- CLI splitting can then proceed with clearer downstream contracts.
- Test movement should be done opportunistically when each boundary is
  touched, then completed as a final cleanup phase.

Each phase should be small enough to commit independently.

## 13. Next-Version Notes

The next version can consider these directions only after this
convergence work is complete:

- richer run diagnostics such as `pico-cli runs diagnose <run-id>`;
- memory search improvements beyond keyword and CJK bigram matching;
- a provider matrix report that compares configured providers without
  blocking on unrelated provider failures;
- better human-facing recovery review flows;
- optional semantic memory or indexing backends.

These are deliberately not part of this spec. The immediate goal is to
make the current harness easier to change safely.

## 14. Design Approval Status

The design was reviewed in sections:

1. goal and scope;
2. architecture layers and module boundaries;
3. concrete optimization work packages and priority;
4. data flow, error handling, and diagnostics;
5. rollout phases and validation.

The user approved each section before this spec was written.
