# Pico Release A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Release A: current-contract observability, Context hard budget, unified Tool/Memory policy, transactional contract migrations, and fail-closed macOS SRT Sandbox GA.

**Architecture:** Extend existing `ContextManager`, `ToolExecutor`, `RunStore`, recovery stores, and CLI. Each persistent contract change ships with its writer, strict current reader, converter, migration cutover/recovery, inspection consumer, and tests in the same slice. SRT is a narrow approved-shell runner; Linux Sandbox Release B is out of scope.

**Tech Stack:** Python 3.11+, standard-library runtime only, pytest, ruff, existing JSON/JSONL stores, macOS Seatbelt through exact SRT 0.0.65 and Node >=20.11.0.

## Global Constraints

- Canonical Messages remains the only transcript.
- One Model Attempt produces and executes one Action.
- Unknown tools and invalid tool definitions fail closed.
- Latest user input is never silently truncated.
- Approved Shell always goes through effect observation and terminalization.
- Explicit `--sandbox` never falls back to host execution.
- Python runtime dependencies remain zero.
- Runtime reads only current persistence contracts; legacy readers exist only inside migration commands.
- User Notes remain agent-read-only; Agent Notes remain append-only and private.
- Release A supports macOS Sandbox only; Linux Sandbox Release B is not implemented here.
- Every benchmark, migration, and audit artifact records commit and dirty state.

---

## File Map

- Create `pico/migration.py`: journal, state machine, identity checks, crash recovery.
- Create `pico/trace_contract.py`, `pico/report_contract.py`, `pico/summary.py`: safe evidence contracts.
- Create `pico/context/snapshot.py`: immutable source snapshots and recall commit data.
- Create `pico/tool_policy.py`: frozen Policy Decision.
- Create `pico/shell_runner.py`, `pico/sandbox.py`: approved execution and macOS SRT.
- Modify existing `pico/runtime.py`, `agent_loop.py`, `context_manager.py`, `context/renderer.py`, `memory/recall.py`, `tools.py`, `tool_executor.py`, `tool_context.py`, `run_store.py`, `cli*.py` only at their existing integration points.
- Create focused tests under `tests/` for each contract; do not replace existing regression tests until the new contract assertions exist.

---

### Task 1: Establish macOS SRT feasibility gate

**Files:** Create `scripts/srt_feasibility.py`, `tests/test_srt_feasibility.py`.

**Interfaces:** `probe_srt() -> dict` returns `status`, exact `version`, Node version, platform, and capability statuses. It must not modify production execution.

- [ ] Write tests for missing launcher, wrong version, unsupported platform, and target-not-executed; run `uv run pytest tests/test_srt_feasibility.py -q` and confirm failure.
- [ ] Implement offline discovery and opt-in macOS smoke; require SRT `0.0.65`, Node `>=20.11.0`, regular non-workspace launcher, and stable status codes.
- [ ] Run fake tests, then macOS smoke if SRT is installed; record `git rev-parse HEAD` and dirty state in output.
- [ ] Run `uv run ruff check scripts/srt_feasibility.py tests/test_srt_feasibility.py`.
- [ ] Commit `test: establish macos srt feasibility gate`.

### Task 2: Implement transactional migration primitive

**Files:** Create `pico/migration.py`, `tests/test_migration.py`.

**Interfaces:** `MigrationJournal.from_dict()/to_dict()`, `MigrationManager.apply(contract, source_version, target_version, converter)`, `MigrationManager.recover()`, and states `ABSENT`, `PREPARING`, `CANDIDATE_READY`, `OLD_MOVED`, `NEW_INSTALLED`, `VALIDATED`, `COMMITTED`, `ROLLBACK_REQUIRED`, `ROLLED_BACK`, `ROLLBACK_FAILED`.

- [ ] Write tests for all transitions, duplicate-key journals, symlink/identity mismatch, cross-filesystem staging, disk-full simulation, and crashes before/after both renames.
- [ ] Implement `.pico/.migration/{lock,journal.json,candidate,rollback}` with owner-only permissions and relative paths only.
- [ ] After each rename, fsync the changed parent, atomically write and fsync the journal, then fsync `.migration`; write states only after their disk facts are durable.
- [ ] Implement startup recovery: `VALIDATED` resumes cleanup, `NEW_INSTALLED` validates with current readers, inconsistent layouts and `ROLLBACK_FAILED` fail closed without deleting data.
- [ ] Run `uv run pytest tests/test_migration.py -q` and `uv run ruff check pico/migration.py tests/test_migration.py`.
- [ ] Commit `feat: add transactional migration state machine`.

### Task 3: Add Trace Envelope and safe projection

**Files:** Create `pico/trace_contract.py`, `tests/test_trace_contract.py`; modify `pico/runtime.py`, `pico/agent_loop.py`, `tests/test_artifact_security.py`.

**Interfaces:** `new_trace_event(task_state, event, payload, *, attempt=None, tool_use_id=None) -> dict`, `project_trace_payload(event, payload) -> dict`.

- [ ] Add tests requiring `trace_schema_version`, unique `event_id`, `run_id`, `task_id`, and rejecting prompt, completion, args, results, shell output, verification command, memory body, secrets, and full paths.
- [ ] Implement the projector with an explicit allowlist; change `Pico.emit_trace()` to return the event ID and automatically add envelope fields.
- [ ] Remove `user_request` from `run_started`, raw tool payloads from tool events, full verification commands, and `final_answer` from `run_finished`.
- [ ] Update existing trace assertions to check safe status/count fields rather than content.
- [ ] Run `uv run pytest tests/test_trace_contract.py tests/test_artifact_security.py tests/test_runtime_report.py -q` and `uv run ruff check .`.
- [ ] Commit `feat: harden trace envelope and content projection`.

### Task 4: Add Report v2 and Summary v1

**Files:** Create `pico/report_contract.py`, `pico/summary.py`, `tests/test_report_contract.py`, `tests/test_summary.py`; modify `pico/runtime.py`, `pico/run_store.py`, `pico/cli_commands.py`, `pico/cli_parser.py`, `pico/cli.py`, `pico/cli_output.py`.

**Interfaces:** `validate_run_report(payload) -> dict`, `build_run_report(task_state, aggregates) -> dict`, `inspect_trace(trace_path, run_id, task_id) -> dict`, `build_run_summary(report, trace_path) -> dict`.

- [ ] Add tests for successful, interrupted/partial-effect, corrupt-trace, missing-terminal, correlation-mismatch, and Report/TaskState-mismatch cases.
- [ ] Implement strict `record_type=run_report`, `format_version=2`; keep final answer, full TaskState, working-memory text, prompt, completion, tool content, shell output, memory body, and full paths out of Report.
- [ ] Refactor `Pico.build_report()` to produce the validated aggregate; keep private content in Session/TaskState.
- [ ] Implement inspection-only `run_summary` v1 from Report plus Trace integrity; corrupt Trace sets `summary_complete=false` and never guesses state.
- [ ] Add `pico runs summary <run_id|latest> [--format json]`; text and JSON must use the same payload.
- [ ] Run focused tests and `uv run ruff check .`.
- [ ] Commit `feat: add report v2 and run summary`.

### Task 5: Add OBS migration slice

**Files:** Modify `pico/migration.py`; create `tests/test_migration_observability.py`.

**Interfaces:** `migrate_run_artifacts(source_dir: Path, destination_dir: Path) -> dict` converts legacy Trace/Report only; it must not modify Session, Checkpoint, Blob, User Note, or Agent Note.

- [ ] Add tests for safe legacy event projection, ambiguous event failure, invalid JSONL, Report rebuild, and successful deletion only after current-reader validation.
- [ ] Implement whole-slice conversion: unsafe or ambiguous rows abort the slice; never copy legacy forbidden content into new artifacts.
- [ ] Add migration inventory entries containing commit, dirty state, source/target hashes, and relative paths only.
- [ ] Run `uv run pytest tests/test_migration_observability.py tests/test_migration.py -q`.
- [ ] Commit `feat: migrate legacy observability artifacts safely`.

### Task 6: Implement immutable Context Snapshot and recall commit

**Files:** Create `pico/context/snapshot.py`, `tests/test_context_snapshot.py`, `tests/test_memory_recall_commit.py`; modify `pico/context/renderer.py`, `pico/memory/recall.py`, `pico/agent_loop.py`.

**Interfaces:** `InjectionSource`, `InjectionSnapshot`, `RecallSelection`, `render_injection_snapshot(agent, user_message, runtime_feedback='') -> tuple[InjectionSnapshot, dict]`, `commit_recalled_memory(agent, paths) -> None`.

- [ ] Add tests proving one snapshot per attempt, no second recall/scan, required current user/runtime feedback, and no Session mutation during rendering.
- [ ] Move recall selection out of direct Session mutation; return selected paths and filtered counts in `RecallSelection`.
- [ ] Make source statuses explicit: `included`, `empty`, `truncated`, `failed`; reserve `dropped_budget` for final planning.
- [ ] Define required sources (`system`, `tools`, `current_user`, non-empty runtime feedback, required checkpoint) and optional sources; make required checkpoints bounded at generation.
- [ ] Change drop order to `memory_index -> project_structure -> workspace_state -> recalled_memory -> optional_checkpoint`; keep history deletion at complete-turn boundaries.
- [ ] Commit `feat: add immutable context source snapshots`.

### Task 7: Implement full request budget and Breakdown

**Files:** Create `tests/test_context_budget.py`; modify `pico/context_manager.py`, `pico/context/renderer.py`, `pico/runtime.py`, `pico/agent_loop.py`.

**Interfaces:** `ContextManager.build_request(...)` returns `(request, metadata)` with `context_breakdown`; internal count mode is `provider_request|provider_text|estimate`.

- [ ] Add tests for required-only overflow with provider call count zero, latest-input preservation, tool-pair preservation, deterministic optional drops, and final hard-cap assertion.
- [ ] Lock one count mode per request; if any provider count fails, recalculate every component with estimate instead of mixing modes.
- [ ] Apply `input_limit = total_budget_hard_cap - max_new_tokens - 512`; perform sanitize, required feasibility, history soft-cap, optional drop, final recount, and only then return the request.
- [ ] Add Breakdown fields for mode, caps, final/headroom, required feasibility, source status/reason, soft/hard dropped turns, and digest count without content.
- [ ] Commit `feat: enforce full context request budget`.

### Task 8: Add unified Tool Policy and registry effects

**Files:** Create `pico/tool_policy.py`, `tests/test_tool_policy.py`; modify `pico/tools.py`, `pico/tool_executor.py`, `pico/tool_context.py`.

**Interfaces:** `PolicyDecision` contains `schema_version`, `decision`, `reason_code`, `effect_class`, `risk_class`, and approval metadata; `resolve_policy(agent, name, args) -> PolicyDecision`.

- [ ] Add tests for unknown tool, invalid definition, invalid args, read-only, repeated call, denied approval, changed approval args, trusted executable failure, and allowed runner count exactly one.
- [ ] Move `effect_class` into the fixed registry; reject registered tools missing effect; keep unknown tools conservatively `workspace_write` plus deny.
- [ ] Separate `risky`, dynamic `risk_class`, approval, sandbox outcome, tool status, and recovery status; remove runner/exit code from approval metadata.
- [ ] Freeze `allow` only after all pre-execution gates; persist the decision in pending Tool Change when a state-changing call begins.
- [ ] Run existing tool security/recovery suites plus new tests.
- [ ] Commit `feat: unify tool policy decisions`.

### Task 9: Enforce explicit Memory writes and recall telemetry

**Files:** Create `tests/test_memory_intent.py`; modify `pico/runtime.py`, `pico/tool_context.py`, `pico/tools.py`, `pico/tool_executor.py`, `pico/memory/recall.py`; add repository structure tests under `tests/`.

**Interfaces:** `memory_write_intent(user_message) -> bool`; ToolContext carries an immutable per-turn boolean; `memory_save` denies with `explicit_memory_request_required` when false.

- [ ] Add tests for explicit Chinese/English save requests, non-explicit requests, history-only keywords, delegate default false, secret rejection, and User Notes write rejection.
- [ ] Compute intent deterministically from the current top-level input; never let model args or history set it.
- [ ] Add candidate/selected/included/dropped-budget telemetry; update `recently_recalled` only after final source inclusion.
- [ ] Add `git check-ignore` structure tests proving User Notes can be tracked while Agent Notes, runs, sessions, checkpoints, and locks remain ignored.
- [ ] Run `uv run pytest tests/test_memory_intent.py tests/memory tests/test_sensitive_tools.py -q`.
- [ ] Commit `feat: enforce explicit durable memory writes`.

### Task 10: Unify approved Shell execution without SRT

**Files:** Create `pico/shell_runner.py`, `tests/test_shell_runner.py`; modify `pico/tools.py`, `pico/tool_executor.py`, `pico/safe_subprocess.py`.

**Interfaces:** `run_approved_shell(plan, sandbox_context=None) -> StructuredShellResult`; host mode must preserve current behavior. `ApprovedShellExecution` is immutable and includes argv, exact command, mode, executable, cwd, env, and timeout.

- [ ] Add tests proving argv, complex shell, and Git all use one entry point while host execution remains unchanged; assert frozen executable and exact approved command.
- [ ] Move Git pure filesystem checks apart from subprocess probes; identify `rev-parse`, `config --includes`, `ls-files`, and target Git as sandboxable operations.
- [ ] Implement host runner and process-group termination behind the new entry point; do not add a backend registry or fallback callback.
- [ ] Run all existing shell/security/Git tests plus `tests/test_shell_runner.py`.
- [ ] Commit `refactor: unify approved shell execution boundary`.

### Task 11: Implement macOS SRT bootstrap and policy

**Files:** Create `pico/sandbox.py`, `tests/test_sandbox_macos.py`; modify `pico/cli.py`, `pico/cli_parser.py`, `pico/cli_diagnostics.py`, `pico/runtime.py`, `pico/shell_runner.py`.

**Interfaces:** `SandboxIdentity`, `SandboxContext`, `bootstrap_sandbox(workspace) -> SandboxContext`, `build_settings(context, plan) -> Path`, `run_with_srt(plan, context) -> StructuredShellResult`.

- [ ] Add fake-runner tests for missing/version mismatch/identity changed/policy invalid and assert host runner count zero.
- [ ] Implement exact SRT 0.0.65 and Node identity checks; do not use the ordinary trusted-executable registry; reject workspace paths and symlink escape.
- [ ] Generate owner-only per-call HOME/TMP/cache/settings; deny `.env`, `.pico`, `.git`, User Notes, SRT installation writes; default network/localhost/listener/Unix sockets denied.
- [ ] Add global `--sandbox` for `run` and `repl`; bootstrap before Agent/run/session creation; unsupported or unavailable states fail closed.
- [ ] Add `sandbox` section to `pico doctor --offline` with `supported|unsupported|not_ready|incompatible|unavailable|error`.
- [ ] Run fake contract tests and macOS smoke for ordinary argv, complex shell, sensitive paths, write denial, network/socket denial, and zero fallback.
- [ ] Commit `feat: add fail-closed macos sandbox runner`.

### Task 12: Integrate sandboxed Git, timeout, effects, and recovery

**Files:** Modify `pico/shell_runner.py`, `pico/sandbox.py`, `pico/tool_executor.py`, `pico/workspace_observer.py`, `pico/tool_change_recorder.py`; add `tests/test_sandbox_process_tree.py`, `tests/test_sandbox_git.py`.

**Interfaces:** `run_with_srt()` must cover ordinary command, complex shell, Git probes, and target Git; timeout returns structured `timeout` outcome and never bypasses effect observation.

- [ ] Add tests for Git probe/target invocation inside SRT, child/grandchild timeout, SIGTERM-resistant child, partial workspace modification, and pending Tool Change terminalization.
- [ ] Execute pure filesystem Git preflight on host, but execute `rev-parse`, `config --includes`, `ls-files`, and target Git under the same sandbox context/policy hash.
- [ ] Use `Popen(start_new_session=True)`, TERM, bounded grace, KILL, wait, residue check, effect observation, and terminalization.
- [ ] Record sandbox requested/active/outcome/host fallback count without shell output or full paths.
- [ ] Run `uv run pytest tests/test_sandbox_process_tree.py tests/test_sandbox_git.py tests/test_tool_executor.py tests/test_recovery_e2e.py -q`.
- [ ] Commit `feat: integrate sandbox git and process recovery`.

### Task 13: Add evaluation suites and leakage gate

**Files:** Create `scripts/evaluate.py`, `tests/test_evaluate.py`; modify `pico/evaluation/metrics_common.py`, `pico/evaluation/metrics_reports.py`, existing evaluation modules only to expose structured results.

**Interfaces:** `evaluate_suite(suite, output_dir, *, provider=None) -> dict`; suites are `core-fast`, `core-full`, `sandbox-contract`, `sandbox-real`, `live`.

- [ ] Add tests for suite dispatch, scenario IDs, strict artifact headers, commit/dirty/platform provenance, relative paths, and failure reporting.
- [ ] Implement orchestration only: invoke existing pytest/ruff/benchmark/ablation/build runners; do not copy scoring logic or parse free-text as truth.
- [ ] Add canary scanning for prompt, completion, tool args/results, shell output, memory query/body, secrets, and absolute paths in Trace/Report/Summary/Eval artifacts.
- [ ] Implement performance gate: same scenario and machine class, median comparison, fail only at 2x and +5ms, p95 report-only, one confirmation rerun.
- [ ] Keep `live` explicitly authorized and never a normal PR gate.
- [ ] Run `uv run pytest tests/test_evaluate.py -q` and `uv run ruff check .`.
- [ ] Commit `feat: add release a evaluation suites`.

### Task 14: Release A migration cutover, docs, and full verification

**Files:** Modify `pico/cli_commands.py`, `pico/cli_diagnostics.py`, `docs/architecture.md`, `docs/security.md`, `docs/verification.md`, `README.md`; add `tests/test_release_a_gate.py`.

**Interfaces:** `pico migrate status/apply/abort/recover`, `pico doctor --offline`, and `scripts/evaluate.py --suite core-fast|core-full|sandbox-contract|sandbox-real` are the Release A operator surface.

- [ ] Add end-to-end tests for migration cutover, rollback, stale journal recovery, Report/Summary, Context overflow zero-call, policy runner 0/1, explicit memory gate, and macOS SRT fail-closed.
- [ ] Implement migration status/apply/abort/recover only for journal states; identity mismatch and `ROLLBACK_FAILED` never auto-delete or guess a live root.
- [ ] Update docs to state macOS Sandbox GA and Linux Sandbox Release B explicitly; document that sandbox mode does not support Git index/metadata writes.
- [ ] Run `uv run ruff check .`, `uv run pytest -q`, `uv run python scripts/evaluate.py --suite core-fast`, `uv run python scripts/evaluate.py --suite core-full`, and macOS real sandbox smoke.
- [ ] Run distribution checks: `uv build`, clean-install smoke, and existing distribution verifier.
- [ ] Record final commit, dirty state, platform, Python, SRT, and evaluation artifact paths.
- [ ] Commit `release: ship pico release a macos sandbox`.

---

## Self-Review Checklist

- [ ] Every persistent contract change has writer, strict current reader, converter, cutover/recovery, consumer, and tests in the same task or immediately preceding contract task.
- [ ] Migration state machine covers crash points, fsync order, recovery, identity mismatch, and no mixed state.
- [ ] Report and Summary have strict headers, required fields, terminal statuses, success/interrupted/corrupt examples, and integrity rules.
- [ ] Context has immutable snapshots, required/optional sources, fixed drop order, single count mode, final recount, and recall commit timing.
- [ ] macOS is Release A; Linux is explicitly Release B and never a hidden Release A gate.
- [ ] No task introduces runtime dependencies, plugins, generic registries, remote token-count calls, or host fallback.
- [ ] All tasks have exact files, interfaces, focused commands, expected test intent, and a commit point.
- [ ] Linux Sandbox implementation, CI GA, and Linux release gate are intentionally excluded.
