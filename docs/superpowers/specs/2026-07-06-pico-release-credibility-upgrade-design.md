# Pico Release Credibility Upgrade Design

- Date: 2026-07-06
- Status: Ready for user review
- Scope: Design only. No implementation is included in this document.
- Branch context: `memory`

## 1. Summary

Pico already has a substantial local coding-agent harness: explicit tools, runtime policy, provider clients, context management, memory v2, recovery records, run artifacts, and a green local baseline. The next useful upgrade is not another broad feature layer. It is a release-credibility pass that makes Pico's behavior easier to prove, cheaper to smoke-test, and safer to automate.

This design groups the next work into five implementation packages:

1. Provider Control Pack
2. Memory Evidence Pack
3. Performance Gate Pack
4. Machine Output Pack
5. Provider Doctor Matrix

The recommended execution order is evidence first, automation second:

1. Add a provider selector to provider benchmark runs.
2. Turn `memory_quality` from scaffold-only into real tool-trace scoring.
3. Add deterministic memory/repo-map performance budgets and a release gate script.
4. Make `run --format json` produce a clean machine-readable envelope.
5. Add optional all-provider doctor diagnostics.

## 2. Current Context

The current baseline is strong but uneven:

- `./scripts/check.sh` is the canonical local gate and currently passes with `452 passed`.
- `pico-cli doctor --format text` validates the active provider, storage, recovery store, and provider connectivity.
- The provider benchmark path can run real providers, but `run_provider_experiments()` currently loops over `gpt`, `claude`, and `deepseek` together.
- `benchmarks/memory_quality/run_benchmark.py` currently loads scenarios and creates workspaces but returns `scaffold_only`.
- `RepoMap` and `MemoryRefresher` have bounded scans and lazy refresh, but there is no fixed performance budget script.
- Inspection commands already support JSON envelopes, but the normal `run` path still prints human output and a welcome banner unless `--quiet` is used.

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

### Option A: Release Gate First

Implement provider selection, memory trace scoring, perf budgets, and a release gate before polishing CLI JSON output and all-provider diagnostics.

Pros:

- Gives the project better evidence quickly.
- Reduces release risk before expanding surfaces.
- Keeps live-provider costs controlled.

Cons:

- JSON automation remains inconsistent until a later package.
- Doctor still reports only the active provider until the later diagnostics package.

### Option B: Automation First

Implement `run --format json`, provider selector, and all-provider doctor first.

Pros:

- Improves scripting and agentic workflow ergonomics quickly.
- Makes future benchmark orchestration cleaner.

Cons:

- Leaves memory quality scaffold-only for longer.
- Does not immediately improve release confidence.

### Option C: Architecture First

Start by reducing module size and consolidating shared provider/CLI helpers.

Pros:

- Lowers long-term maintenance cost.
- Could make later packages easier to implement.

Cons:

- Does not directly prove runtime behavior.
- Higher chance of churn before the next useful release gate exists.

## 6. Recommended Approach

Use Option A: Release Gate First.

The reason is simple: Pico's next bottleneck is proof, not surface area. The codebase already has many harness primitives. The weak spots are the places where the repo cannot yet produce stable evidence: memory-quality behavior, provider smoke targeting, and memory/repo-map performance budgets.

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
- Normalize accepted provider names through one helper.
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

### 7.5 Tests

- CLI parser accepts `deepseek`, `claude`, `gpt`, and `all`.
- Selecting one provider calls only that provider path.
- `all` preserves the existing three-provider behavior.
- Missing key for the selected provider produces a blocked row for that provider only.

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

### 8.3 Runner Flow

For each scenario:

1. Create a temporary workspace.
2. Seed `AGENTS.md` and `.pico/memory` files.
3. Run a Pico agent against each `session_turn`.
4. Read the latest run trace JSONL.
5. Score memory tool calls and memory-file effects.
6. Emit a row with scenario id, status, evidence, and failure reason.

### 8.4 Evidence Model

Each row should include:

```json
{
  "id": "recall_bcrypt",
  "status": "passed",
  "tool_calls": ["memory_search"],
  "expected_hits": ["workspace/notes/auth.md"],
  "observed_hits": ["workspace/notes/auth.md"],
  "agent_notes_changed": false,
  "failure_reason": ""
}
```

### 8.5 Provider Strategy

The first implementation should support two modes:

- `--mode fake`: deterministic fake-model traces for CI and local release gates.
- `--mode live --provider deepseek|claude|gpt`: optional live-model validation.

The deterministic mode is required for release automation. Live mode is useful evidence but should not be required by the fast gate.

### 8.6 Error Handling

- A scenario with no rows exits non-zero.
- Malformed scenario JSON exits non-zero with file and line.
- Missing expected evidence marks the scenario failed but continues to score later scenarios.
- `--fail-fast` can stop at the first failed scenario.

### 8.7 Tests

- Scenario loading rejects malformed records with a useful message.
- Fake mode produces passed rows for recall/update/multi-note scenarios.
- No-noise scenario passes only when no high-scoring irrelevant memory hit is observed.
- JSON output can be parsed and contains evidence rows.

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

### 9.3 Budget Policy

Use conservative thresholds that are stable on local development machines:

- Small fixture budgets should be strict.
- Medium fixture budgets should catch obvious regressions.
- The script should print measured values and thresholds.

Exact thresholds belong in implementation after measuring the current baseline once. The design requirement is that thresholds are explicit, checked, and stored in the script.

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

Example:

```json
{
  "ok": true,
  "kind": "run",
  "data": {
    "answer": "PICO_SMOKE_OK",
    "run_id": "run_...",
    "session_id": "20260706-...",
    "stop_reason": "final_answer_returned",
    "report_path": ".pico/runs/run_.../report.json"
  }
}
```

### 10.3 Internal Shape

- Extend `run_agent_once()` or wrap it with a JSON-aware path.
- Reuse `success_envelope()`, `error_envelope()`, and `format_json()`.
- Read `agent.current_task_state` after `agent.ask()` to populate metadata.

### 10.4 Error Handling

- `RuntimeError` returns `{ "ok": false, "error": ... }`.
- Empty prompt keeps current no-op behavior in text mode; JSON mode should return an empty successful run envelope only if no model call is made. The implementation should choose one explicit behavior and test it.
- The JSON contract should not include secrets or raw prompt text.

### 10.5 Tests

- `pico-cli --format json run "..."` stdout is valid JSON.
- No welcome text appears in JSON mode.
- JSON response includes answer, run id, session id, and stop reason.
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

### 11.3 Output Shape

Text mode adds a provider matrix:

```text
Providers
  deepseek      ready       key present    connectivity ok
  gpt           missing     key missing    skipped
  claude        ready       key present    connectivity ok
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
- Connectivity failures are per-provider diagnostic rows.
- Secret values are never printed.

### 11.5 Tests

- Default doctor output remains active-provider only.
- `--all-providers` includes all configured provider families.
- `--offline` skips connectivity for every provider row.
- JSON output redacts URLs and credentials as current diagnostics do.

## 12. Cross-Package Dependencies

The packages are mostly independent, but the cleanest sequence is:

1. Provider Control Pack
2. Memory Evidence Pack
3. Performance Gate Pack
4. Machine Output Pack
5. Provider Doctor Matrix

Provider Control helps optional live memory benchmarks target one provider. Memory Evidence and Performance Gate together make `check_release.sh` meaningful. Machine Output can be implemented independently, but it becomes more valuable after release scripts need reliable JSON.

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

### 13.3 Optional Live Evidence

Live provider benchmarks are explicit:

```bash
python scripts/run_provider_experiments.py --provider deepseek ...
python benchmarks/memory_quality/run_benchmark.py --mode live --provider deepseek ...
```

Live checks should be recorded in release notes or review-pack docs when used, but they should not be mandatory for every local edit.

## 14. Documentation Updates

Update these docs as each package lands:

- `docs/review-pack/README.md`: release gate commands and benchmark caveats.
- `docs/review-pack/dashboard.md`: current baseline and completed package IDs.
- `README.md`: only if user-facing CLI behavior changes.
- `docs/architecture/agent-harness-v1-overview.md`: only if run artifact or JSON output semantics change materially.

Avoid copying long implementation details into multiple docs. Put operational command summaries in review-pack and stable user-facing behavior in README.

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

The full upgrade is complete when:

- Provider benchmark can target exactly one provider or all providers.
- Memory quality benchmark no longer reports `scaffold_only` in deterministic release mode.
- Memory quality rows include trace-backed evidence.
- Memory/repo-map performance budgets are checked by a script.
- `check_release.sh` exists and runs the broader non-live gate.
- `pico-cli --format json run ...` emits one valid JSON envelope without welcome text.
- `pico-cli doctor --all-providers` reports all provider credential/connectivity rows without making inactive missing keys a default failure.
- Existing text-mode CLI behavior remains compatible.
- `./scripts/check.sh` remains green.

## 17. Implementation Boundaries

Each work package should be implemented as a separate plan and commit sequence. Do not combine all packages in one large change. The recommended first implementation plan should cover only Provider Control Pack and Memory Evidence Pack, because those two most directly improve release evidence.
