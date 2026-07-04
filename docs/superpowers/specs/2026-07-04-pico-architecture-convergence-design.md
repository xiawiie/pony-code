# Pico Architecture Convergence Design, Rev 2

Date: 2026-07-04
Branch: memory
Status: design approved for planning, implementation not started

## 1. Context

Pico has passed the point where the core feature direction is unclear. The
current branch already contains substantial work in these areas:

- working-memory runtime surface
- recoverable editing and recovery checkpoints
- CLI diagnostics and JSON surfaces
- real-provider benchmark reliability
- public API compatibility tests

The next useful step is not a new feature layer. It is architecture convergence:
make the implementation easier to evolve by moving large mixed-responsibility
files toward clearer ownership boundaries, while preserving the behavior already
validated by the current tests and manual real-provider flows.

This document supersedes the first architecture convergence design by
integrating review findings directly into the main plan. In particular, it
corrects four over-broad assumptions from the first draft:

- runtime report metadata has already been hardened and should now be protected
  as an invariant, not treated as an open bug
- checkpoint writing and recovery checkpoint writing are already mostly
  separated, so the remaining work is boundary tightening, not a new facade
- provider extraction should start with three provider files and one shared
  helper module, not a speculative five-file taxonomy
- CLI diagnostics should build on `pico/cli_diagnostics.py`, not introduce a
  parallel `cli_inspect.py`

## 2. Current Architecture Snapshot

The current codebase has good user-facing behavior but several files are now
carrying too many roles:

- `pico/cli_commands.py` is about 1025 lines and mixes command handlers,
  rendering, dispatch, diagnostics, recovery, session listing, memory commands,
  and help coupling.
- `pico/evaluation/metrics_experiments.py` is about 1311 lines and contains
  fixed benchmark execution, provider benchmark execution, experiment schemas,
  synthetic memory experiments, context stress experiments, synthetic security
  experiments, real-provider experiments, and recovery ablation experiments.
- `pico/providers/clients.py` is about 641 lines and contains OpenAI-compatible,
  Anthropic-compatible, and Ollama behavior in one file.
- `tests/test_pico.py` is about 2026 lines and still hosts multiple test
  clusters that now map to different subsystems.
- `pico/runtime.py` is about 695 lines and remains near the upper bound, but it
  is a deliberate orchestration module and is explicitly exempted from the
  Section 12 line-count target; it is not an extraction target in this work.

There are also important counter-facts that should shape the plan:

- `pico/checkpoint.py` and `pico/recovery_checkpoint_writer.py` are already
  separate modules with different purposes.
- `pico/cli_diagnostics.py` already owns diagnostic data collection through
  `collect_status`, `collect_config`, and `collect_doctor`.
- `pico/cli_commands.py` has a hidden import dependency on `HELP_DETAILS` from
  `pico/cli.py`, which should be removed before deeper CLI extraction.
- `pico/evaluation/metrics_experiments.py` contains more than the simple
  fixed/provider benchmark split; ablation code must be handled deliberately.
- the retire-v1-memory work has landed far enough that `WorkingMemory` is the
  public runtime surface, while `features.memory` and `LayeredMemory` remain as
  intentional compatibility/helper surfaces.

## 3. Problem Statement

The current risk is not that Pico lacks capabilities. The risk is that future
changes require reasoning across too many mixed-responsibility files.

The architecture convergence problem has five concrete symptoms:

1. CLI changes are hard to review because command handlers, rendering,
   diagnostics, recovery, memory, help, and startup flow live together.
2. Evaluation changes are hard to isolate because benchmarks, experiment
   schemas, provider-profile execution, synthetic scenarios, and recovery
   ablations are packed into one module.
3. Provider changes are higher-risk than necessary because unrelated provider
   dialects share one implementation file.
4. Runtime report/checkpoint semantics are subtle because resume status and
   recovery checkpoint metadata coexist and can be confused conceptually.
5. Tests still protect the behavior, but the layout does not clearly show which
   subsystem each test cluster owns.

The plan should reduce these risks without changing the product contract.

## 4. Goals

- Preserve current user-facing CLI behavior.
- Preserve current public imports and compatibility re-exports where practical.
- Preserve current JSON output contracts for diagnostics, recovery, benchmark
  artifacts, and provider execution.
- Split large files along existing conceptual boundaries.
- Keep each phase independently reviewable.
- Add protection tests before moving behavior that has historically been easy
  to regress.
- Keep the first implementation version focused on architecture A: make the
  existing product easier to maintain before designing the next product layer.

## 5. Non-Goals

- Do not redesign the CLI command set.
- Do not add new memory semantics.
- Do not redesign recovery checkpoint storage.
- Do not introduce a broad checkpoint facade. Section 7.4 / P3 adds one narrow
  report-metadata helper only; that is not the "facade" ruled out here.
- Do not change benchmark artifact formats unless a compatibility shim is added
  in the same phase.
- Do not remove public compatibility imports just because the internal module
  layout changes.
- Do not solve every future experiment taxonomy problem in one pass.
- Do not begin implementation from this document alone; write a separate
  execution plan after design approval.

## 6. Plan Overlap Ledger

The repository has several nearby design and plan documents. The convergence
work should respect them instead of reopening their scope.

### 6.1 Recoverable Editing Phase 1

Documents:

- `docs/superpowers/plans/2026-07-01-recoverable-editing-phase-1.md`
- `docs/superpowers/plans/2026-07-01-recoverable-editing-phase-1-v2.md`

Current interpretation:

- the original phase 1 plan is superseded by the v2 plan
- much of the v2 behavior appears represented in the current runtime and tests
- architecture convergence should not change recovery checkpoint schema or
  restore semantics

Convergence impact:

- recovery-related CLI extraction may move handlers, but must keep JSON keys,
  preview behavior, restore behavior, and checkpoint listing behavior stable
- recovery checkpoint writing remains owned by
  `pico/recovery_checkpoint_writer.py`

### 6.2 CLI Surface Redesign

Document:

- `docs/superpowers/plans/2026-07-02-pico-cli-surface-redesign.md`

Current interpretation:

- the command surface and diagnostics direction are already represented in the
  current branch
- convergence should inherit that surface rather than redesign it

Convergence impact:

- status, doctor, and config should build from `pico/cli_diagnostics.py`
- command registry behavior should be preserved
- JSON mode must remain stable

### 6.3 Pico Memory V2

Documents:

- `docs/superpowers/specs/2026-07-02-pico-memory-v2-design.md`
- `docs/superpowers/plans/2026-07-02-pico-memory-v2.md`

Current interpretation:

- memory v2 has landed far enough that memory docs, runtime surfaces, and tools
  exist in the branch
- convergence should not reopen memory product design

Convergence impact:

- memory CLI extraction should move command handlers and rendering only
- memory data model, compatibility imports, and public API should remain stable

### 6.4 Retire V1 Memory

Documents:

- `docs/superpowers/specs/2026-07-03-retire-v1-memory-design.md`
- `docs/superpowers/plans/2026-07-03-retire-v1-memory.md`

Current interpretation:

- retire-v1-memory has landed as a runtime direction
- `WorkingMemory` is the public runtime-facing surface
- `features.memory` and `LayeredMemory` remain intentionally available as
  dormant helper/compatibility code
- `session["memory"].file_summaries` remains as internal compatibility

Convergence impact:

- imports from `features.memory` are not automatically evidence of unfinished
  retire-v1 work
- P1 and P5 may move tests or evaluation helpers that use compatibility memory
  code, but must not silently remove those surfaces

## 7. Architecture Boundaries

### 7.1 CLI Boundary

The target CLI layout is:

- `pico/cli.py`: argument parser, top-level entrypoint, and command wiring
- `pico/cli_help.py`: help detail data shared by parser and command handlers
- `pico/cli_commands.py`: small compatibility/dispatch layer during migration
- `pico/cli_diagnostics.py`: diagnostic data collection and the corresponding
  status/config/doctor command handlers, colocated by default; split into a
  sibling `pico/cli_diagnostics_commands.py` only if the merged file exceeds
  the 500-line directional threshold from Section 12 after handlers are moved
- `pico/cli_recovery.py`: runs, sessions, checkpoints, preview restore, restore
- `pico/cli_memory.py`: memory-related command handlers
- `pico/cli_start.py`: run-once, REPL, slash routing, and startup orchestration
- `pico/cli_renderers.py`: shared rendering helpers that remain after feature
  handlers move out

Important sequencing:

1. Move `HELP_DETAILS` out of `pico/cli.py` first.
2. Keep `pico/cli_commands.py` as a compatibility shell during extraction.
3. Move diagnostics into the existing diagnostics module before creating any
   new module.
4. Extract recovery and memory after JSON contract tests are pinned.
5. Extract startup flow only after simpler command families are stable.

### 7.2 Evaluation Boundary

The target evaluation layout is staged, not all-at-once:

- `pico/evaluation/metrics.py`: public re-export surface and compatibility
  layer
- `pico/evaluation/benchmark_schema.py`: dataclasses, records, summary types,
  serialization helpers
- `pico/evaluation/fixed_benchmark.py`: deterministic local benchmark flow
- `pico/evaluation/provider_benchmark.py`: real-provider benchmark flow,
  provider profiles, provider artifact writing
- `pico/evaluation/metrics_experiments.py`: continues to hold experiment
  clusters not extracted in phase 1. It stops being "temporary" only when
  phase 1.5 fires; if phase 1.5 does not fire, this file remains the
  long-lived home for those clusters and is not otherwise a problem

The first extraction should not pretend that every experiment has a final home.
After the three benchmark-focused modules are split, remaining experiment
clusters can be evaluated with real line counts. If the remaining file is still
too large, split it into:

- `pico/evaluation/experiments_synthetic.py`
- `pico/evaluation/experiments_real.py`
- `pico/evaluation/experiments_recovery.py`

Known clusters that must not be lost:

- feature ablation
- synthetic memory experiments
- context stress experiments
- synthetic security scenarios
- provider profile/provider experiment execution
- real memory/context/security experiments
- recovery ablation v2

### 7.3 Provider Boundary

The target provider layout starts small:

- `pico/providers/clients.py`: public compatibility imports and factory surface
- `pico/providers/openai_compatible.py`: OpenAI-compatible behavior
- `pico/providers/anthropic_compatible.py`: Anthropic-compatible behavior
- `pico/providers/ollama.py`: Ollama behavior
- `pico/providers/_shared.py`: shared request, response, and environment
  helpers

Optional future split:

- `pico/providers/usage.py`
- `pico/providers/errors.py`

The optional split should only happen if shared usage/error logic grows beyond a
meaningful threshold. A useful rule of thumb is about 120 lines of cohesive
shared logic. Below that, a single `_shared.py` is easier to maintain.

Provider regressions to pin before extraction:

- Anthropic text extraction accepts text blocks even when `type` is missing.
- Anthropic thinking-only responses with `stop_reason=max_tokens` produce a
  clear no-text error.
- OpenAI-compatible usage accounting stays stable.
- Ollama request/response behavior stays stable.
- public client imports remain stable.

### 7.4 Runtime Report and Checkpoint Boundary

There are two related but distinct concepts:

- resume status: whether and how the current run was resumed
- recovery checkpoint metadata: what can be used for preview/restore and
  recoverable editing

The current `runtime.build_report()` has already been hardened so
`last_prompt_resume_status` and `task_state.resume_status` are preserved with
the right precedence. Architecture convergence should protect this invariant
rather than redesign it.

The target change is narrow:

- add one helper, likely named
  `build_report_checkpoint_metadata(task_state, last_prompt_metadata)`
- route `runtime.build_report()` through that helper
- keep checkpoint writing in `pico/checkpoint.py`
- keep recovery checkpoint writing in `pico/recovery_checkpoint_writer.py`
- do not introduce generic facade functions such as
  `write_checkpoint_for_mode(...)`

### 7.5 Test Boundary

The target test layout should make subsystem ownership easier to see:

- keep broad integration coverage where it already protects multi-module flows
- add section banners in `tests/test_pico.py` before extraction
- move provider-client clusters to `tests/test_provider_clients.py`
- move runtime-report and provider-benchmark clusters only if the moved tests remain
  in one file, no imports change, and no assertions are weakened during the
  move
- avoid large test rewrites during the same phase as production module movement

## 8. Work Packages

### Pre-Flight: Confirm Current Ground Truth

Purpose:

Make sure implementation starts from the actual current state, not a stale
design assumption.

Actions:

- verify retire-v1-memory compatibility imports are intentional and still
  covered by public API tests
- list overlapping plan documents and mark which scopes are inherited versus
  reopened
- record current line counts for the large files being split
- record the exact manual real-provider commands that should remain valid
- identify existing JSON contract tests before adding new ones

Acceptance:

- convergence plan does not treat compatibility imports as unfinished work
- convergence plan does not restart CLI, memory, or recovery product design
- implementation checklist has the specific tests/commands to run per phase

### P0: Protection Net

Purpose:

Freeze the behavior most likely to regress during movement.

Actions:

- audit existing runtime report tests first; the invariant is already covered
  by `test_pico.py::test_report_prompt_metadata_preserves_initial_resume_status`
  and `test_pico.py::test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup`,
  which assert `report["resume_status"]`,
  `report["prompt_metadata"]["resume_status"]`, and
  `report["prompt_metadata"]["last_prompt_resume_status"]` on the dict returned
  by `runtime.build_report()`. Add a new test only if one of these is deleted
  or weakened during refactor
- freeze benchmark artifact fields introduced by the real-provider benchmark
  reliability work
- confirm existing diagnostics/recovery JSON contract tests cover status,
  doctor, config, runs, checkpoints, and preview restore. Primary anchors to
  audit: `tests/test_cli_diagnostics.py`, `tests/test_recovery_cli.py`,
  `tests/test_cli_commands.py`, and the relevant portions of
  `tests/test_pico.py`. List which command x JSON-key pairs already have
  coverage and which are missing before writing anything new
- add only missing JSON contract tests; do not duplicate already-strong coverage
- protect both help paths, which are independent (CLI uses `ROOT_HELP` at
  `pico/cli_commands.py:27`; REPL uses lazy `HELP_DETAILS` at
  `pico/cli_commands.py:543-546` / `pico/cli.py:122`):
  - CLI path is already covered by
    `tests/test_cli_commands.py::test_help_command_shows_examples` and
    `tests/test_cli_commands.py::test_help_flag_uses_root_help_without_argparse_dump`;
    confirm these still exist and do not need extension
  - REPL path has no dedicated test today
    (`tests/test_cli_commands.py::test_repl_command_exits_on_eof` covers
    EOF only). Add one focused REPL test: start `run_repl`, issue `/help`,
    and assert the rendered output contains expected slash-command sections.
  Together this pins both help paths so P4.0 cannot silently drop either
  `ROOT_HELP` or `HELP_DETAILS`
- extend `tests/test_public_api_contract.py` to directly import all four
  provider classes (`FakeModelClient`, `OllamaModelClient`,
  `OpenAICompatibleModelClient`, `AnthropicCompatibleModelClient`) from
  `pico.providers.clients`. The existing test only imports `FakeModelClient`,
  so the other three lack contract protection for P2
- document the manual real-provider regression commands in the implementation
  plan

Acceptance:

- targeted tests fail if resume/checkpoint metadata semantics regress
- benchmark artifact compatibility is protected: at minimum `failure_category`
  (values include `missing_artifact`, `budget_exceeded`, `verifier_failed`)
  and `failure_category_counts` in the summary — see
  `pico/evaluation/evaluator.py:552, 569-581`
- CLI JSON contracts are protected enough to allow command-handler movement
- both help paths (CLI `ROOT_HELP` and REPL `HELP_DETAILS`) have a live test
- `tests/test_public_api_contract.py` covers direct import of all four
  provider classes from `pico.providers.clients`
- the manual real-provider regression commands are recorded verbatim in the
  implementation plan for reuse in P1/P2/P3

### P1: Evaluation Split

Purpose:

Reduce `metrics_experiments.py` risk without losing experiment clusters.

Phase 1 extraction:

- create `benchmark_schema.py`
- create `fixed_benchmark.py`
- create `provider_benchmark.py`
- keep `metrics.py` as the public re-export layer
- keep remaining ablation/experiment clusters in `metrics_experiments.py`;
  a cluster may be extracted opportunistically during phase 1 only if it is
  self-contained (no cross-cluster imports back into the remaining file) and
  the extraction adds no new coupling

Possible phase 1.5 extraction:

- trigger: if `pico/evaluation/metrics_experiments.py` remains above the
  directional 500-line threshold from Section 12 after phase 1, and the
  clusters are cleanly separable, split it into synthetic, real, and recovery
  experiment modules
- if the file falls at or below the threshold naturally, phase 1.5 is skipped

Acceptance:

- existing evaluation imports continue to work
- provider benchmark artifacts are byte-shape compatible at the field level
- fixed benchmark behavior remains stable
- real-provider benchmark command still works with local API keys
- no experiment cluster is orphaned

### P2: Provider Split

Purpose:

Make provider-specific behavior easier to modify and test independently.

Actions:

- add provider regression tests for the Anthropic edge cases before moving code
- split OpenAI-compatible behavior into `openai_compatible.py`
- split Anthropic-compatible behavior into `anthropic_compatible.py`
- split Ollama behavior into `ollama.py`
- place shared helpers in `_shared.py`
- leave `clients.py` as the public compatibility/import surface
- split `usage.py` and `errors.py` only if the shared surface is large enough
  to justify it

Acceptance:

- provider tests pass
- the extended `tests/test_public_api_contract.py` from P0 (covering all four
  provider classes) still passes without modification after the provider
  split, confirming public imports from `pico.providers.clients` continue to
  work
- real-provider smoke commands continue to work
- provider-specific changes no longer require editing one large mixed file

### P3: Runtime Report Boundary Tightening

Purpose:

Clarify the one remaining place where resume status and recovery checkpoint
metadata can be conceptually confused.

Actions:

- add the narrow report metadata helper
- route `runtime.build_report()` through it
- preserve existing report output
- keep checkpoint module ownership unchanged
- do not add a new regression test here; instead, confirm the two existing
  tests referenced in P0 still pass unmodified after the helper is introduced,
  and confirm they still reference dict keys on the return value of
  `runtime.build_report()` (not attributes on an object). Because
  `build_report()` returns a dict, the helper must also return a dict-shaped
  fragment or update a dict in place; it must not change the report's shape

Acceptance:

- runtime report tests pass
- recovery checkpoint tests pass
- no broad checkpoint facade is introduced
- future readers can identify the resume/checkpoint metadata rule in one helper

### P4: CLI Split

Purpose:

Reduce `cli_commands.py` into coherent command families while preserving the
current CLI surface.

P4.0 Help Decoupling:

- move `HELP_DETAILS` to `pico/cli_help.py`
- update `pico/cli.py` and `pico/cli_commands.py` to import from the new module
- verify parser help and `help` command behavior

P4.1 Diagnostics:

- colocate `handle_status`, `handle_doctor`, and `handle_config` with the
  existing diagnostics flow inside `pico/cli_diagnostics.py`
- split into `pico/cli_diagnostics_commands.py` only if the merged file
  exceeds the 500-line directional threshold
- preserve `collect_status`, `collect_config`, and `collect_doctor`
- verify JSON and text output

P4.2 Recovery:

- extract runs, sessions, checkpoints, preview restore, and restore handlers to
  `pico/cli_recovery.py`
- preserve recovery JSON output

P4.3 Memory:

- extract memory command handlers to `pico/cli_memory.py`
- preserve memory output and public behavior

P4.4 Startup:

- extract run-once, REPL, slash routing, and startup orchestration to
  `pico/cli_start.py`
- preserve interactive and non-interactive behavior

P4.5 Rendering:

- extract remaining shared rendering helpers to `pico/cli_renderers.py` only
  after command families have moved

Acceptance:

- each sub-step has focused tests
- full `./scripts/check.sh` passes at the end of P4
- `cli_commands.py` is reduced to a small compatibility/dispatch layer
- users see the same commands and output contracts

### P5: Test Layout Cleanup

Purpose:

Make tests easier to navigate after production boundaries are clearer.

Actions:

- add section banners to `tests/test_pico.py`
- move provider-client tests to `tests/test_provider_clients.py`
- move runtime-report tests only if it can be done without hiding integration
  coverage
- move provider-benchmark tests only if they naturally align with P1 modules

Acceptance:

- tests are easier to map to modules
- no coverage is lost
- test extraction is not mixed with risky production movement in the same commit

## 9. Validation Strategy

Use a layered verification strategy. Each phase should run the narrowest useful
tests first, then broader checks at phase boundaries.

Baseline checks:

- `./scripts/check.sh`
- focused pytest commands for touched modules
- CLI JSON command checks where command output is moved
- manual real-provider benchmark smoke commands before and after provider or
  evaluation movement

Suggested focused checks by phase:

- P0: runtime report tests, public API contract tests, CLI diagnostics/recovery
  JSON tests
- P1: evaluation benchmark tests and real-provider benchmark smoke
- P2: provider client tests and real-provider smoke
- P3: runtime report tests and recovery checkpoint tests
- P4: CLI diagnostics, recovery, memory, startup, and public contract tests
- P5: moved test files plus full check script

Real-provider validation should use the local configured API keys. Any command
that cannot run because an external provider is unavailable should be recorded
as an environment/provider failure, not silently counted as product success.

## 10. Compatibility Contract

The convergence work must preserve these contracts unless a later document
explicitly changes them:

- public imports from `pico` and documented submodules
- `pico.providers.clients` imports
- evaluation imports from `pico.evaluation.metrics`
- CLI command names
- CLI JSON keys for diagnostics and recovery commands
- provider benchmark artifact fields
- recovery checkpoint schema
- runtime report metadata keys
- memory public runtime surface
- dormant compatibility helpers retained by retire-v1-memory

Compatibility shims are acceptable when they make module movement safe. Removing
them should be a separate, explicitly reviewed change.

Forwarding-shell lifetime:

- `pico/cli_commands.py`, `pico/providers/clients.py`,
  `pico/evaluation/metrics.py`, and the top-level `pico/__init__.py`
  re-export block (which currently exposes `Pico`, `SessionStore`,
  `WorkspaceContext`, `build_agent`, `build_arg_parser`, `build_welcome`,
  `main`, and the four provider classes) remain as compatibility shells
  after this convergence work
- their internal contents may thin down to re-exports, but the modules
  themselves must not be deleted here
- deprecation or removal requires a later, explicitly scoped change (with its
  own design note) that also updates every external importer, including tests
  under `tests/memory/`

## 11. Error Handling Principles

- Provider-specific error interpretation should live near the provider
  implementation.
- Shared transport/configuration errors may live in `_shared.py`.
- Anthropic no-text and thinking-only edge cases should remain explicit.
- CLI JSON errors should remain machine-readable.
- Recovery errors should continue to distinguish missing runs, missing
  checkpoints, invalid restore targets, and preview/restore failures.
- Benchmark provider failures should remain visible as provider failures, not
  collapsed into generic benchmark failures.

## 12. Convergence Metrics

These are directional metrics, not hard acceptance gates:

- newly extracted command modules (for example the diagnostics/recovery/
  memory/startup command families in Section 7.1): soft threshold of about
  500 lines per module. If a merge lands under the threshold with clean
  responsibility, do not split further; if it exceeds the threshold, split
  along the next obvious responsibility line
- `pico/cli_commands.py`: from about 1025 lines toward a forwarding/dispatch
  compatibility shell (target under about 300 lines)
- `pico/evaluation/metrics.py`: retained as a public re-export compatibility
  shell
- `pico/evaluation/metrics_experiments.py`: from about 1311 lines toward
  several files with no benchmark file over about 500 lines
- `pico/providers/clients.py`: from about 641 lines toward a forwarding/public
  compatibility shell
- provider implementation files: ideally about 250 lines or less each
- no `pico/` production file should remain over about 700 lines unless it is a
  deliberate orchestration module; `pico/runtime.py` is currently the only
  such exempted module

Functional acceptance wins over line-count purity. A small overshoot is better
than an awkward split.

## 13. Rollout Order

Recommended rollout:

1. Pre-flight ground-truth check
2. P0 protection net
3. P1 evaluation split
4. P2 provider split
5. P3 runtime report boundary tightening
6. P4 CLI split
7. P5 test layout cleanup

Highest-leverage subset if time is constrained:

1. Pre-flight
2. P0
3. P1
4. P2
5. P3
6. P4.0 and P4.1

P4.2 through P4.5 and P5 are valuable, but they are primarily organizational
hygiene once diagnostics and provider/evaluation boundaries are under control.

## 14. Implementation Commit Shape

Prefer small commits:

1. tests: protect report and artifact contracts (P0)
2. tests: extend public API contract with all four provider imports (P0)
3. tests: add REPL /help smoke covering HELP_DETAILS lazy import (P0)
4. refactor: split evaluation benchmark modules (P1)
5. tests: pin Anthropic edge cases (missing `type`, thinking-only + max_tokens) (pre-P2)
6. refactor: split provider clients (P2)
7. refactor: isolate runtime report checkpoint metadata (P3)
8. refactor: decouple cli help details (P4.0)
9. refactor: move cli diagnostics handlers (P4.1)
10. refactor: move cli recovery handlers (P4.2)
11. refactor: move cli memory handlers (P4.3)
12. refactor: move cli startup flow (P4.4)
13. refactor: extract remaining cli renderers (P4.5)
14. test: organize large test clusters (P5)

Each commit should be reversible and should avoid mixing production movement
with unrelated formatting.

## 15. Design Approval Status

The design direction is approved by the user at the brainstorming stage:

- use option 2: rewrite the current design as an integrated rev2
- do not over-plan future product layers
- focus on doing architecture A well before considering the next version

Next step after this document is reviewed:

- write a concrete implementation plan with exact files, tests, and checkpoints
- then execute the plan phase by phase
