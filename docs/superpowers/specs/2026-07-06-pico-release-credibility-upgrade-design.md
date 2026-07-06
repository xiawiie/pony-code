# Pico Release Credibility Upgrade Design

- Date: 2026-07-06
- Status: Revised after implementation-readiness review
- Scope: Design only. No implementation is included in this document.
- Branch context: `memory`

## 1. Summary

Pico already has a substantial local coding-agent harness: explicit tools,
runtime policy, provider clients, context management, memory v2, recovery
records, run artifacts, and a recorded green local baseline. The next useful
upgrade is not another broad feature layer. It is a release-credibility pass
that makes Pico's behavior easier to prove, cheaper to smoke-test, and safer
to automate.

This design groups the next work into five implementation packages:

1. Provider Control Pack
2. Memory Evidence Pack
3. Performance Gate Pack
4. Machine Output Pack
5. Provider Doctor Matrix

The recommended execution order is phased, not all-at-once:

1. Add a provider selector to provider benchmark runs.
2. Turn `memory_quality` from scaffold-only into real tool-trace scoring.
3. Add deterministic memory/repo-map performance budgets and a release gate script.
4. Make `run --format json` produce a clean machine-readable envelope.
5. Add optional all-provider doctor diagnostics.

The next implementation plan should cover only Phase 1:

1. Provider Control Pack
2. Memory Evidence Pack

The remaining packages stay in the same design so the direction is coherent,
but they should not be bundled into the first implementation plan.

### 1.1 Review Findings Applied

The implementation-readiness review found six adjustments that matter before
planning:

- The original scope was too broad for one implementation plan. It is now
  decomposed into Phase 1 evidence work and later release/automation work.
- Provider naming had to be separated: provider benchmark labels are
  `gpt`, `claude`, and `deepseek`; CLI/runtime provider names are
  `openai`, `anthropic`, `deepseek`, and `ollama`.
- `memory_quality` needs a deterministic fake-model mode that proves Pico's
  tool trace and memory-file behavior without claiming to measure live LLM
  judgment.
- `run --format json` needed an explicit empty-prompt behavior instead of
  leaving an ambiguous no-output success path.
- `doctor --all-providers` should check inactive provider credentials without
  making their absence a default doctor failure.
- Documentation drift must be handled in the same package that changes release
  evidence, especially `benchmarks/memory_quality/README.md` and
  `docs/review-pack/README.md`.

## 2. Current Context

The recorded baseline is strong but uneven:

- `./scripts/check.sh` is the canonical local gate; the review-pack records a
  `452 passed` baseline for the current `memory` branch.
- `pico-cli doctor --format text` validates the active provider, storage, recovery store, and provider connectivity.
- The provider benchmark path can run real providers, but
  `run_provider_experiments()` currently loops over `gpt`, `claude`, and
  `deepseek` together.
- `run_provider_experiments()` is re-exported through
  `pico.evaluation.metrics`, so its public import surface must remain stable.
- `benchmarks/memory_quality/run_benchmark.py` currently loads scenarios and
  creates workspaces but has no `--mode`, no `--format`, and returns
  `scaffold_only`.
- `RepoMap` and `MemoryRefresher` have bounded scans and lazy refresh, but there is no fixed performance budget script.
- Inspection commands already support JSON envelopes, but the normal `run` path still prints human output and a welcome banner unless `--quiet` is used.
- `COMMAND_SPECS["doctor"]` currently only accepts `--offline` as a doctor
  subcommand flag.

The main problem is therefore not lack of features. It is that some release claims still depend on human interpretation instead of stable local evidence.

## 3. Goals

### 3.1 Release Evidence

Make memory quality, provider benchmark selection, and memory/repo-map performance measurable with repeatable commands.

### 3.2 Lower Smoke-Test Cost

Allow maintainers to run a single provider benchmark without requiring all configured providers to be present and funded.

### 3.3 Stable Machine Output

Make `pico-cli --format json run ...` suitable for scripts and other agents by removing human-only output from stdout and returning a stable envelope.

### 3.4 Preserve Existing User Experience

Keep default human-facing commands pleasant and compatible. New stricter behavior should be opt-in through flags or release scripts unless there is already an established JSON contract.

## 4. Non-Goals

- Do not rewrite the runtime loop.
- Do not change provider protocol behavior beyond provider selection and diagnostics.
- Do not introduce non-stdlib dependencies.
- Do not run live provider benchmarks inside the default fast `scripts/check.sh`.
- Do not make inactive provider credentials required for normal `pico-cli doctor`.
- Do not convert memory retrieval into semantic embedding search.
- Do not replace existing recovery/checkpoint behavior.
- Do not solve all CLI help or documentation drift in this pass.

## 5. Approaches Considered

### Option A: Evidence Core First

Implement provider benchmark selection and deterministic memory trace scoring
first. Follow with performance gates, JSON run output, and all-provider doctor
diagnostics as separate later packages.

Pros:

- Gives the project better evidence quickly without a giant change.
- Directly addresses the two current scaffold/cost problems.
- Keeps live-provider costs controlled.
- Produces a small first implementation plan with clear tests.

Cons:

- JSON automation remains inconsistent until a later package.
- Doctor still reports only the active provider until the later diagnostics package.

### Option B: Full Release-Credibility Sweep

Implement all five packages in one plan.

Pros:

- Delivers the whole release story in one pass.
- Avoids temporarily maintaining a partially upgraded release gate.

Cons:

- Too much independent surface area for one implementation plan.
- Harder to review and bisect.
- Increases the chance of mixing docs, benchmarks, CLI behavior, and
  diagnostics in one broad commit sequence.

### Option C: Automation First

Implement `run --format json`, provider selector, and all-provider doctor before
memory-quality scoring.

Pros:

- Improves scripting and agentic workflow ergonomics quickly.
- Makes future benchmark orchestration cleaner.

Cons:

- Leaves memory quality scaffold-only for longer.
- Does not immediately improve the weakest release evidence.

## 6. Recommended Approach

Use Option A: Evidence Core First.

The reason is simple: Pico's next bottleneck is proof, not surface area. The
codebase already has many harness primitives. The weak spots are the places
where the repo cannot yet produce stable evidence: memory-quality behavior and
provider smoke targeting. Performance gates, JSON run output, and all-provider
doctor diagnostics are still valuable, but they should follow after Phase 1
lands cleanly.

## 7. Work Package 1: Provider Control Pack

### 7.1 Purpose

Allow provider benchmark runs to target one provider or the existing full set.

### 7.2 User-Facing Behavior

Add:

```bash
python scripts/run_provider_experiments.py --provider deepseek ...
python scripts/run_provider_experiments.py --provider claude ...
python scripts/run_provider_experiments.py --provider gpt ...
python scripts/run_provider_experiments.py --provider all ...
```

Default behavior remains equivalent to `--provider all`.

### 7.3 Internal Shape

- Add a provider selection argument to `scripts/run_provider_experiments.py`.
- Add a `providers` parameter to `pico.evaluation.provider_benchmark.run_provider_experiments()`.
- Preserve the re-export through `pico.evaluation.metrics`.
- Normalize accepted benchmark provider labels through one helper.
- Keep benchmark provider labels separate from CLI runtime provider names:
  - accepted benchmark labels: `gpt`, `claude`, `deepseek`, `all`
  - runtime provider names remain: `openai`, `anthropic`, `deepseek`, `ollama`
- Preserve the existing result shape:

```json
{
  "providers": [
    {
      "provider": "deepseek",
      "status": "completed"
    }
  ]
}
```

### 7.4 Error Handling

- Invalid provider names fail before running benchmarks.
- Missing credentials block only the selected provider.
- `--provider all` preserves per-provider blocked/error rows rather than failing the whole command early.
- A selected provider that is blocked should still write the output JSON with
  that blocked row, matching the existing all-provider artifact style.

### 7.5 Tests

- CLI parser accepts `deepseek`, `claude`, `gpt`, and `all`.
- Selecting one provider calls only that provider path.
- `all` preserves the existing three-provider behavior.
- Missing key for the selected provider produces a blocked row for that provider only.
- Public import compatibility remains intact:
  `from pico.evaluation.metrics import run_provider_experiments`.

## 8. Work Package 2: Memory Evidence Pack

### 8.1 Purpose

Turn `benchmarks/memory_quality/run_benchmark.py` from scaffold-only into a real memory behavior benchmark that scores tool traces and memory file effects.

### 8.2 Scenario Semantics

Existing scenario files already describe the expected behavior:

- Recall scenarios should call `memory_search` and surface the expected note path.
- Chinese search scenarios should hit CJK notes.
- Update scenarios should call `memory_save` and append the expected lesson.
- Multi-note scenarios should retrieve all expected top hits.
- No-noise scenarios should avoid irrelevant high-confidence memory hits.

### 8.3 Deterministic Mode Semantics

`--mode fake` should use a benchmark-local scripted model client. Its purpose is
to prove that Pico can execute memory tools, write trace artifacts, and score
the observed behavior deterministically.

It should not be described as live LLM memory quality. It is release evidence
for the memory harness and scoring path.

Suggested fake behavior:

- recall and CJK scenarios: call `memory_search` with the user turn as the
  query, then return a final answer.
- update scenarios: call `memory_save` with the expected remembered content,
  then return a final answer.
- multi-note scenarios: call `memory_search` once with the user turn as the
  query, then return a final answer.
- no-noise scenarios: return a final answer without calling `memory_search`.

### 8.4 Runner Flow

For each scenario:

1. Create a temporary workspace.
2. Seed `AGENTS.md` and `.pico/memory` files.
3. Run a Pico agent against each `session_turn`.
4. Read the latest run trace JSONL.
5. Score memory tool calls and memory-file effects.
6. Emit a row with scenario id, status, evidence, and failure reason.

Scenario setup should become strict: malformed setup paths should fail the
scenario instead of being silently ignored.

### 8.5 Evidence Model

The top-level output should be stable JSON:

```json
{
  "schema_version": 1,
  "mode": "fake",
  "summary": {
    "total": 5,
    "passed": 5,
    "failed": 0,
    "pass_rate": 1.0
  },
  "rows": []
}
```

Each row should include:

```json
{
  "id": "recall_bcrypt",
  "status": "pass",
  "tool_calls": ["memory_search"],
  "expected_hits": ["workspace/notes/auth.md"],
  "observed_hits": ["workspace/notes/auth.md"],
  "agent_notes_changed": false,
  "failure_reason": ""
}
```

Use `status: "pass"` and `status: "fail"` to stay consistent with the existing
benchmark artifact vocabulary.

Search evidence should be parsed from `tool_executed` trace events:

- tool name: `memory_search`
- tool args: query and limit
- tool result: hit paths and scores from lines such as
  `- workspace/notes/auth.md (score=1.23)`

Save evidence should be checked through both trace and file state:

- tool name: `memory_save`
- tool args: note content and scope
- final `.pico/memory/agent_notes.md` contains the expected old and new content
  for update scenarios

### 8.6 Provider Strategy

The first implementation should support two modes:

- `--mode fake`: deterministic fake-model traces for CI and local release gates.
- `--mode live --provider deepseek|claude|gpt`: optional live-model validation.

The deterministic mode is required for release automation. Live mode is useful evidence but should not be required by the fast gate.

`--format json` should print only the JSON payload. Text mode may keep the
summary table for humans.

### 8.7 Error Handling

- A scenario with no rows exits non-zero.
- Malformed scenario JSON exits non-zero with file and line.
- Missing expected evidence marks the scenario failed but continues to score later scenarios.
- `--fail-fast` can stop at the first failed scenario.
- Invalid scenario fields, including invalid setup paths, produce a failed row
  or a load error with the scenario id and source file.

### 8.8 Tests

- Scenario loading rejects malformed records with a useful message.
- Fake mode produces `status: "pass"` rows for recall/update/multi-note
  scenarios.
- No-noise scenario passes only when no high-scoring irrelevant memory hit is observed.
- JSON output can be parsed and contains evidence rows.
- `--format json` has no human summary text on stdout.
- `benchmarks/memory_quality/README.md` no longer says scoring is manual once
  this package lands.

## 9. Work Package 3: Performance Gate Pack

### 9.1 Purpose

Add fixed performance budgets for memory and repo-map operations, then make them part of a release gate.

### 9.2 New Script

Add:

```bash
python scripts/check_memory_perf.py --format json
```

The script should generate deterministic temporary fixtures and measure:

- cold `RepoMap.scan()`
- warm `RepoMap.refresh_if_stale()` with no changes
- warm refresh with one changed file
- `BlockStore.list()`
- `Retrieval.search()`
- `MemoryRefresher.refresh_if_stale()`

The script should not measure the user's current checkout directly. It should
build small and medium temporary fixtures so release numbers are comparable
across machines and not dominated by unrelated workspace contents.

The JSON output should include operation name, fixture size, measured
milliseconds, threshold milliseconds, and pass/fail status.

### 9.3 Budget Policy

Use conservative thresholds that are stable on local development machines:

- Small fixture budgets should be strict.
- Medium fixture budgets should catch obvious regressions.
- The script should print measured values and thresholds.
- Measurements should use `time.perf_counter()`, a warm-up pass, and a fixed
  number of repeated rounds. Use the median for the pass/fail decision and keep
  the max in the JSON output for diagnosis.

Exact thresholds belong in implementation after measuring the generated fixture
baseline once. The design requirement is that thresholds are explicit, checked,
and stored in the script.

### 9.4 Release Gate Split

Keep:

```bash
./scripts/check.sh
```

as the fast gate: ruff and pytest.

Add:

```bash
./scripts/check_release.sh
```

for the broader gate:

1. `./scripts/check.sh`
2. `uv run pytest examples/mini-pico/tests -q`
3. `uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json`
4. `uv run python scripts/check_memory_perf.py --format json`

Live provider benchmarks remain opt-in.

### 9.5 Error Handling

- Perf budget failures exit non-zero.
- Output includes the operation name, measured milliseconds, threshold, and fixture size.
- Release gate stops at the first failing command because it is a shell script with `set -eu`.

### 9.6 Tests

- Perf helper functions can be unit tested without depending on wall-clock thresholds.
- Script argument parsing is covered.
- Release script content is tested similarly to the existing `scripts/check.sh` contract test.
- JSON schema and non-zero exit behavior are covered with deliberately tiny
  thresholds in unit tests, rather than relying on real slowdowns.

## 10. Work Package 4: Machine Output Pack

### 10.1 Purpose

Make one-shot runs usable by scripts when `--format json` is requested.

### 10.2 Behavior

Text mode stays as-is.

JSON mode:

- suppresses the welcome banner
- prints no leading blank line
- emits exactly one JSON object on stdout
- sends runtime errors to a JSON error envelope
- treats an empty prompt as a JSON usage error instead of a silent no-output
  success

Example:

```json
{
  "ok": true,
  "kind": "run",
  "data": {
    "answer": "PICO_SMOKE_OK",
    "run_id": "run_...",
    "task_id": "task_...",
    "session_id": "20260706-...",
    "status": "completed",
    "stop_reason": "final_answer_returned",
    "report_path": ".pico/runs/run_.../report.json",
    "trace_path": ".pico/runs/run_.../trace.jsonl"
  }
}
```

### 10.3 Internal Shape

- Add a JSON-aware one-shot helper in `pico.cli_start`, or wrap
  `run_agent_once()` without changing runtime behavior.
- Branch in `pico.cli.main()` before printing the welcome banner when
  `invocation.command == "run"` and `args.format == "json"`.
- Reuse `success_envelope()`, `error_envelope()`, and `format_json()`.
- Read `agent.current_task_state` after `agent.ask()` to populate metadata.

### 10.4 Error Handling

- `RuntimeError` returns `{ "ok": false, "error": ... }` and exit code `1`.
- Runtime error messages should be passed through the agent's redaction path
  where available.
- Empty prompt keeps current no-op behavior in text mode; JSON mode returns a
  usage error envelope with code `empty_prompt` and exit code `2`.
- The JSON contract should not include secrets or raw prompt text.

### 10.5 Tests

- `pico-cli --format json run "..."` stdout is valid JSON.
- No welcome text appears in JSON mode.
- JSON response includes answer, run id, session id, and stop reason.
- JSON empty prompt returns an error envelope and no model call.
- Runtime errors use an error envelope without leaking configured secrets.
- Text mode remains compatible with existing tests.

## 11. Work Package 5: Provider Doctor Matrix

### 11.1 Purpose

Expose all-provider credential and connectivity status without changing the default active-provider doctor behavior.

### 11.2 User-Facing Behavior

Add:

```bash
pico-cli doctor --all-providers
pico-cli doctor --all-providers --offline
```

Default `pico-cli doctor` remains active-provider only.

The provider matrix uses CLI/runtime provider names:

- `openai`
- `anthropic`
- `deepseek`
- `ollama`

Do not use benchmark labels (`gpt`, `claude`) in doctor output.

### 11.3 Output Shape

Text mode adds a provider matrix:

```text
Providers
  deepseek      ready       key present    connectivity ok
  openai        missing     key missing    skipped_missing_credentials
  anthropic     ready       key present    connectivity ok
  ollama        ready       no key needed  connectivity ok
```

JSON mode adds:

```json
{
  "providers": [
    {
      "provider": "deepseek",
      "credentials": "present",
      "connectivity": "ok"
    }
  ]
}
```

### 11.4 Error Handling

- Missing inactive provider credentials are not global doctor failures.
- `--offline` skips all connectivity checks and still reports credentials.
- Connectivity checks run only for providers that have required credentials, and
  for `ollama` because it needs no API key.
- Connectivity failures are per-provider diagnostic rows.
- Secret values are never printed.
- Doctor token parsing accepts `--all-providers` and `--offline` in either
  order.

### 11.5 Tests

- Default doctor output remains active-provider only.
- `--all-providers` includes all configured provider families.
- `--offline` skips connectivity for every provider row.
- JSON output redacts URLs and credentials as current diagnostics do.
- `COMMAND_SPECS["doctor"]` and usage text include `--all-providers`.

## 12. Cross-Package Dependencies

The packages are mostly independent, but they should be planned in phases:

### Phase 1: Evidence Core

1. Provider Control Pack
2. Memory Evidence Pack

### Phase 2: Release Gate

3. Performance Gate Pack

### Phase 3: Automation and Diagnostics

4. Machine Output Pack
5. Provider Doctor Matrix

Provider Control helps optional live memory benchmarks target one provider.
Memory Evidence should land before Performance Gate so `check_release.sh` does
not depend on scaffold-only output. Machine Output and Provider Doctor Matrix
can be implemented independently after Phase 1.

## 13. Validation Strategy

### 13.1 Fast Gate

`./scripts/check.sh` continues to run:

- `uv run ruff check .`
- `uv run pytest -q`

### 13.2 Release Gate

New `./scripts/check_release.sh` runs:

- fast gate
- mini-pico example tests
- deterministic memory-quality benchmark
- memory/repo-map performance budget script

This belongs to phase 2, after `memory_quality --mode fake --format json`
exists.

### 13.3 Optional Live Evidence

Live provider benchmarks are explicit:

```bash
python scripts/run_provider_experiments.py --provider deepseek ...
python benchmarks/memory_quality/run_benchmark.py --mode live --provider deepseek ...
```

Live checks should be recorded in release notes or review-pack docs when used, but they should not be mandatory for every local edit.

## 14. Documentation Updates

Update these docs as each package lands:

- `benchmarks/memory_quality/README.md`: mode, format, scoring semantics, and
  whether live mode is optional.
- `docs/review-pack/README.md`: release gate commands and benchmark caveats.
- `docs/review-pack/dashboard.md`: current baseline and completed package IDs.
- `README.md`: only if user-facing CLI behavior changes.
- `docs/architecture/agent-harness-v1-overview.md`: only if run artifact or JSON output semantics change materially.

Avoid copying long implementation details into multiple docs. Put operational command summaries in review-pack and stable user-facing behavior in README.

Phase 1 must update `benchmarks/memory_quality/README.md` and
`docs/review-pack/README.md` in the same commit sequence that changes the
benchmark behavior. Otherwise the release-credibility work creates the same
documentation drift it is meant to reduce.

## 15. Risks

### 15.1 Live Provider Flakiness

Live provider calls can fail due to quota, auth, network, or model behavior. Mitigation: make deterministic fake mode the release gate and live mode optional evidence.

### 15.2 Overly Tight Performance Budgets

Wall-clock checks can be noisy. Mitigation: measure deterministic fixtures, use conservative thresholds, and keep the script focused on catching large regressions.

### 15.3 JSON Contract Drift

Once `run --format json` exists, scripts may depend on it. Mitigation: add contract tests and avoid embedding unstable full reports in the envelope.

### 15.4 Doctor Becoming Too Noisy

All-provider checks can overwhelm users who only configured one provider. Mitigation: keep default doctor active-provider only.

## 16. Acceptance Criteria

### 16.1 Phase 1 Acceptance

The first implementation plan is complete when:

- Provider benchmark can target exactly one provider or all providers.
- Memory quality benchmark no longer reports `scaffold_only` in deterministic release mode.
- Memory quality rows include trace-backed evidence.
- Memory quality JSON uses `schema_version`, `mode`, `summary`, and `rows`.
- Memory quality fake mode is deterministic and does not require live provider credentials.
- Provider benchmark and memory-quality docs are updated with the new behavior.
- `./scripts/check.sh` remains green.

### 16.2 Full Upgrade Acceptance

The full upgrade is complete when:

- Phase 1 acceptance is complete.
- Memory/repo-map performance budgets are checked by a script.
- `check_release.sh` exists and runs the broader non-live gate.
- `pico-cli --format json run ...` emits one valid JSON envelope without welcome text.
- `pico-cli doctor --all-providers` reports all provider credential/connectivity rows without making inactive missing keys a default failure.
- Existing text-mode CLI behavior remains compatible.
- `./scripts/check.sh` remains green.

## 17. Implementation Boundaries

Each phase should be implemented as a separate plan and commit sequence. Do not
combine all packages in one large change.

The next implementation plan should cover only Phase 1:

1. Provider Control Pack
2. Memory Evidence Pack

Phase 2 and Phase 3 should wait until Phase 1 has passed review and the updated
docs no longer describe `memory_quality` as scaffold-only.
