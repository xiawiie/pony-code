# Pico Optimization Dashboard

This dashboard tracks the review follow-up work after the recovery and CI hardening pass.

Execution rule: keep exactly one task in `In Progress`. Finish, verify, update this file, then move to the next task.

## Current Status

- PR: https://github.com/xiawiie/pico/pull/1
- Branch: `cli`
- Latest pushed head: see PR current head
- CI: expected on Python 3.10 and 3.12 for each pushed dashboard task
- Local baseline: `./scripts/check.sh` passed with 307 tests after LOCK-001

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
| SEC-001 | Done | Expanded secret-shaped token detection and limited broad redaction for short secret values | Local `291 passed` |
| CFG-001 | Done | Made project `.env` parsing warn and skip malformed lines while preserving valid entries | Local `293 passed` |
| DX-001 | Done | Added a CI-matching local check script and documented it in the development workflow | Local `294 passed` |
| DOC-001 | Done | Documented run artifact terminology and state-store boundaries in the architecture overview | Local `294 passed` |
| WS-001 | Done | Extracted workspace snapshot helpers into a bounded module while preserving runtime compatibility methods | Local `297 passed` |
| LOOP-001 | Done | Extracted shared AgentLoop terminal finalization for checkpoint, trace, verification, and report writes | Local `298 passed` |
| PARSE-001 | Done | Added self-closing XML tool call parsing while preserving paired XML and JSON tool formats | Local `300 passed` |
| REDACT-002 | Done | Made configured secret redaction token-aware to avoid replacing embedded identifier substrings | Local `301 passed` |
| DEFAULT-001 | Done | Raised agent and shell defaults for real coding-agent runs while keeping provider HTTP timeouts at 300 seconds | Local `304 passed` |
| LOCK-001 | Done | Added repo-local file locks around session and checkpoint store writes | Local `307 passed` |

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
| SEC-001 | P2 | Done | Expand secret-shape detection and short-secret redaction policy | Common token families are rejected from durable memory; short values avoid broad accidental replacement | `uv run pytest -q` -> 291 passed |
| CFG-001 | P3 | Done | Make `.env` parsing tolerant of malformed lines | Bad local `.env` lines warn/skip instead of crashing the CLI | `uv run pytest -q` -> 293 passed |
| DX-001 | P3 | Done | Add local lint/test ergonomics | Optional pre-commit or documented lint/test shortcuts exist | `./scripts/check.sh` -> 294 passed |
| DOC-001 | P3 | Done | Document run artifact terminology | `task_state.json`, `trace.jsonl`, and `report.json` are explained in architecture docs | `./scripts/check.sh` -> 294 passed |

## User Issue Reconciliation

This table reconciles the external issue list against the current `cli` branch. Some items in the original list were stale after the previous dashboard pass.

| Original issue | Current status | Current evidence / next action |
| --- | --- | --- |
| P0: 4 tests failing | Done | Latest local baseline is `./scripts/check.sh` -> 294 passed; PR CI passed on Python 3.10 and 3.12. |
| P1: valuable changes scattered in worktree | Done | `cli` is clean and pushed; previous large diff was split into task commits. |
| P1: `runtime.py` God Class | Done for identified slices | Model output parsing lives in `pico/model_output_parser.py`; workspace snapshot helpers now live in `pico/workspace_snapshot.py`. |
| P1: `evaluation/metrics.py` monolith | Done | `metrics.py` is now a compatibility export layer; implementation lives in `metrics_common.py`, `metrics_experiments.py`, and `metrics_reports.py`. |
| P2: AgentLoop finalize/report duplication | Done | Terminal paths now share `_finish_run` for checkpoint, recovery checkpoint, verification evidence, trace, and report finalization. |
| P2: repeated tool-call only blocks A-A | Done | Runtime now uses a sliding six-tool history window and blocks repeated calls that recur in that window. |
| P2: tool examples duplicated | Done | Prompt prefix imports examples from `pico.tools.TOOL_EXAMPLES`; no separate prompt-prefix example table remains. |
| P2: default `max_steps` / `max_new_tokens` / timeout too small | Done | Defaults are now `max_steps=12`, `max_new_tokens=2048`, provider HTTP timeout 300s, and `run_shell` timeout 60s. |
| P2: Anthropic prompt cache not wired | Done | Anthropic-compatible client now sends guarded `cache_control` metadata and reports cache usage. |
| P2: `redact_text` direct `str.replace` may over-redact | Done | Configured secret values now redact with token boundaries, while short values still require exact whole-string matches. |
| P2: secret-shape detection too narrow | Done | Common token families including `ghp_`, `github_pat_`, Slack, Hugging Face, AWS, and Google API keys are covered. |
| P3: `parse_xml_tool` lacks self-closing support | Done | `<tool name="list_files" path="." />` now parses into a tool payload; nameless self-closing tools retry cleanly. |
| P3: `checkpoint_created` event mixed meanings | Done | Recovery checkpoints now emit `recovery_checkpoint_created` separately. |
| P3: no session/checkpoint file locks | Done | Session and checkpoint stores now serialize writes through repo-local lock files while preserving atomic replace. |
| P3: `capture_workspace_snapshot` large-repo O(n) | Done for fallback bounds | Snapshot fallback now prunes ignored directories and stops at explicit file/byte limits. |
| P3: provider no streaming | Backlog | Clients still return full completion text after the request completes. Tracked as `STREAM-001`. |

## Follow-Up Implementation Queue

| ID | Priority | Status | Task | Acceptance | Verification |
| --- | --- | --- | --- | --- | --- |
| WS-001 | P1 | Done | Extract workspace snapshot helpers and bound fallback scanning | `runtime.py` delegates snapshot capture/diff to a dedicated module; snapshot fallback has explicit limits and tests | `./scripts/check.sh` -> 297 passed |
| LOOP-001 | P2 | Done | Extract AgentLoop terminal finalization helper | Model-error, final-answer, and limit-stop paths share one report/checkpoint/trace finalizer | `./scripts/check.sh` -> 298 passed |
| PARSE-001 | P3 | Done | Support self-closing XML tool calls | `<tool name="list_files" path="." />` parses into a tool payload; malformed self-closing forms retry cleanly | `./scripts/check.sh` -> 300 passed |
| REDACT-002 | P2 | Done | Make long secret redaction token-aware | Long configured secrets are redacted without replacing substrings inside larger non-token text | `./scripts/check.sh` -> 301 passed |
| DEFAULT-001 | P2 | Done | Revisit agent and generation defaults | CLI defaults are less brittle for real coding-agent runs and docs/tests reflect them | `./scripts/check.sh` -> 304 passed |
| LOCK-001 | P3 | Done | Add repo-local session/checkpoint file locking | Session and checkpoint writes are protected against overlapping Pico processes where the platform supports locks | `./scripts/check.sh` -> 307 passed |
| STREAM-001 | P3 | In Progress | Add provider streaming plumbing | Provider clients can expose streamed chunks while preserving existing `complete()` compatibility | Provider tests; `./scripts/check.sh` |

## Workflow Notes

- Do not start a new task until the current `In Progress` task is verified and marked `Done`.
- Prefer small commits that match one dashboard ID.
- Keep `--format json` output stable unless the task explicitly changes machine-readable contracts.
- Update this dashboard and `.planning/pico-review-optimization/progress.md` after every completed task.
