# Pico Optimization Dashboard

This dashboard tracks the review follow-up work after the recovery and CI hardening pass.

Execution rule: keep exactly one task in `In Progress`. Finish, verify, update this file, then move to the next task.

## Current Status

- PR: https://github.com/xiawiie/pico/pull/1
- Branch: `cli`
- Latest pushed head: see PR current head
- CI: expected on Python 3.10 and 3.12 for each pushed dashboard task
- Local baseline: `uv run pytest -q` passed with 288 tests after PROV-001

## Done In This Review Pass

| ID | Status | Summary | Evidence |
| --- | --- | --- | --- |
| D-001 | Done | Fixed evaluator and allowed-tools red tests | Covered by PR CI |
| D-002 | Done | Hardened recovery, verification evidence, redaction, and session persistence | Commit `e5be267` |
| D-003 | Done | Single-sourced tool prompt examples | Commit `bf45881` |
| D-004 | Done | Tightened repeated tool-call loop detection | Commit `bf45881` |
| D-005 | Done | Kept run artifacts aligned to the final checkpoint on step-limit exits | Commit `7ce74cf` |
| D-006 | Done | Added GitHub Actions CI for lint and tests | Commits `1a64bf0`, `879f72f` |
| CLI-001 | Done | Accepted unique checkpoint id prefixes for checkpoint commands | Local `266 passed` |
| CLI-002 | Done | Rendered human-readable restore preview text by default | Local `268 passed` |
| CLI-003 | Done | Moved provider defaults to a single shared source | Local `269 passed` |
| CLI-004 | Done | Replaced pre-agent command dispatch with a command registry | Local `270 passed` |
| CLI-005 | Done | Added `pico-cli init` for guided `.env` provider setup | Local `274 passed` |
| REC-001 | Done | Aligned Turn Checkpoint semantics to one user request with internal Tool Change Records | Local `275 passed` |
| REC-002 | Done | Extracted shared ToolExecutor side-effect finalization | Local `276 passed` |
| REC-003 | Done | Added time-based checkpoint pruning with preview/apply support | Local `279 passed` |
| REC-004 | Done | Made ineligible restore preview entries explicit about missing restorable snapshots | Local `281 passed` |
| ARCH-001 | Done | Moved model output parsing into a dedicated parser module while preserving `Pico` compatibility methods | Local `285 passed` |
| ARCH-002 | Done | Split evaluation metrics into common, experiment, and report modules with a compatibility export layer | Local `286 passed` |
| PROV-001 | Done | Added guarded Anthropic-compatible prompt cache request metadata and cache usage reporting | Local `288 passed` |

## Sequential Queue

| ID | Priority | Status | Task | Acceptance | Verification |
| --- | --- | --- | --- | --- | --- |
| CLI-001 | P1 | Done | Accept unique checkpoint id prefixes for checkpoint commands | `checkpoints show`, `preview-restore`, and `restore` accept a unique prefix; ambiguous and missing prefixes produce clear CLI errors | `uv run pytest -q` -> 266 passed |
| CLI-002 | P1 | Done | Render human-readable restore previews by default | Text mode shows compact restore/review/conflict rows; `--format json` remains unchanged | `uv run pytest -q` -> 268 passed |
| CLI-003 | P1 | Done | Move provider defaults to a single source | `cli.py` and `cli_diagnostics.py` import the same default provider/model/base URL constants | `uv run pytest -q` -> 269 passed |
| CLI-004 | P1 | Done | Replace command dispatch if-chain with a command registry | Commands, namespace suggestions, and help routing derive from one table | `uv run pytest -q` -> 270 passed |
| CLI-005 | P1 | Done | Add `pico-cli init` for guided `.env` setup | User can create/update local provider config without hand-editing from scratch | `uv run pytest -q` -> 274 passed |
| REC-001 | P2 | Done | Align recovery checkpoint naming or semantics | Code and docs agree whether recovery records are per tool step or per user turn | `uv run pytest -q` -> 275 passed |
| REC-002 | P2 | Done | Extract ToolExecutor side-effect finalization | Success and exception paths share one side-effect finalizer | `uv run pytest -q` -> 276 passed |
| REC-003 | P2 | Done | Add time-based checkpoint pruning | `checkpoints prune --older-than=7d` previews and applies expected deletions | `uv run pytest -q` -> 279 passed |
| REC-004 | P2 | Done | Improve binary/ineligible change tracking | Restore preview explains ineligible binary changes without implying backup exists | `uv run pytest -q` -> 281 passed |
| ARCH-001 | P2 | Done | Move model output parsing out of `runtime.py` | Parser behavior preserved while `runtime.py` sheds parser implementation and `Pico` keeps compatibility methods | `uv run pytest -q` -> 285 passed |
| ARCH-002 | P2 | Done | Split `evaluation/metrics.py` by report/experiment boundary | Existing public imports and metrics tests keep working | `uv run pytest -q` -> 286 passed |
| PROV-001 | P2 | Done | Add prompt cache support for Anthropic-compatible clients | Supported clients send cache-control metadata for stable prompt prefix | `uv run pytest -q` -> 288 passed |
| SEC-001 | P2 | In Progress | Expand secret-shape detection and short-secret redaction policy | Common token families are rejected from durable memory; short values avoid broad accidental replacement | Security/runtime tests |
| CFG-001 | P3 | Backlog | Make `.env` parsing tolerant of malformed lines | Bad local `.env` lines warn/skip instead of crashing the CLI | Config tests |
| DX-001 | P3 | Backlog | Add local lint/test ergonomics | Optional pre-commit or documented lint/test shortcuts exist | Docs/config validation |
| DOC-001 | P3 | Backlog | Document run artifact terminology | `task_state.json`, `trace.jsonl`, and `report.json` are explained in architecture docs | Docs review |

## Workflow Notes

- Do not start a new task until the current `In Progress` task is verified and marked `Done`.
- Prefer small commits that match one dashboard ID.
- Keep `--format json` output stable unless the task explicitly changes machine-readable contracts.
- Update this dashboard and `.planning/pico-review-optimization/progress.md` after every completed task.
