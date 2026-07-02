# Pico CLI Surface Redesign

## Goal

Make Pico's CLI Surface explicit, discoverable, scriptable, and aligned with the project's Recoverable Editing and Safe Execution language.

The redesign should improve the command experience without turning Pico into a broad product shell. Runtime behavior, checkpoint storage, recovery planning, sessions, runs, command policy, and model providers remain owned by their existing modules.

## Design Principles

- The CLI Surface is an explicit command layer for the Coding-Agent Harness, not a TUI, IDE shell, plugin marketplace, or global project manager.
- The first implementation should keep the recovery loop narrow while designing enough extension slots for later discovery commands.
- Human output may improve over time; machine output must be stable once exposed.
- Repository-changing recovery actions must stay user-initiated and preview-first.
- CLI commands should map to existing harness boundaries instead of merging sessions, runs, checkpoints, and traces into one display model too early.
- Bare prompt and no-argument REPL behavior remain compatibility paths, but documented workflows should prefer explicit subcommands.

## Review Findings

The design is feasible because the existing storage and runtime boundaries are already thin enough to wrap from CLI commands:

- `SessionStore`, `RunStore`, and `CheckpointStore` expose simple list/load/path-oriented behavior that can be adapted into command handlers without changing their ownership.
- `RecoveryManager` already owns restore preview/apply decisions, so CLI commands can remain presenters and dispatchers rather than recovery logic.
- `config.py` already has `.env`, `provider_env`, and `pico.toml` helpers; `config show` can start by reporting effective values and sources without replacing the configuration system.
- Existing tests already protect hidden `checkpoints` and `runs` behavior, so compatibility can be preserved while explicit commands are added.

The main risks are execution risks, not architectural blockers:

- `doctor` now performs provider connectivity by default. It must use bounded timeouts and classify failures as diagnostic results instead of letting network failures look like Pico runtime failures.
- `argparse` can make fully order-independent global flags awkward. The first implementation should support the normal forms cleanly and add broader flag-order compatibility only where it stays simple.
- `config show --format json` needs source metadata, but secret-bearing values must never be printed.
- `pico/cli.py` is already doing too much. Splitting it is justified, but the split should not turn into an agent runtime refactor.

## Implementation Guardrails

- Keep `pico.cli.main`, `build_arg_parser`, `build_agent`, and `build_welcome` import-compatible.
- Do not change agent loop behavior while adding CLI commands.
- Do not merge sessions, runs, checkpoints, and traces into a single query model in this pass.
- Do not expose reserved extension commands in help until they work.
- Do not introduce a third-party CLI framework.
- Do not make restore or prune mutate files without explicit `--apply`.
- Keep all new behavior covered by focused CLI tests before broad README updates.

## Command Model

### Primary Commands

These commands are part of the first stable CLI Surface.

```text
pico run [prompt...]
pico repl

pico status
pico doctor
pico config show

pico runs list
pico runs show <run-id>

pico sessions list
pico sessions show <session-id>

pico checkpoints list
pico checkpoints show <checkpoint-id>
pico checkpoints preview-restore <checkpoint-id>
pico checkpoints restore <checkpoint-id> --apply
pico checkpoints prune [--apply]
```

### Compatibility Commands

These remain supported for existing usage but should no longer be the primary documented path.

```text
pico
pico "inspect the test failures"
pico checkpoints list
pico runs show <run-id>
```

`pico` with no arguments enters REPL. `pico <prompt...>` runs a one-shot prompt only when the first token is not a known top-level command.

Bare prompt compatibility should remain silent. The CLI should not print migration tips during normal one-shot execution; `--help`, README, and examples should teach the explicit `pico run` form.

### Reserved Extension Commands

These are designed as future extension points, not required for the first implementation.

```text
pico tools list
pico tools show <tool-name>

pico providers list
pico providers doctor [provider]

pico schema show <name>
pico completion <shell>
```

The first implementation should not expose partial versions of these commands. If a command is shown in help, it should work.

## Global Flags

Global flags should be accepted before or after the subcommand where practical.

```text
--cwd <path>
--provider <ollama|openai|anthropic|deepseek>
--model <name>
--base-url <url>
--approval <ask|auto|never>
--resume <session-id|latest>
--format <text|json>
--quiet
--no-color
--no-input
--max-steps <n>
--max-new-tokens <n>
--temperature <float>
--top-p <float>
--secret-env-name <name>
```

Provider-specific flags such as `--host`, `--ollama-timeout`, and `--openai-timeout` stay supported but should be grouped under provider/runtime help text.

## Output Contract

### Text Output

Text output is optimized for humans. It should:

- state what happened;
- include important identifiers;
- show next useful commands after state-changing operations;
- avoid dumping raw JSON unless the user requested `--format json`;
- keep errors on stderr and result data on stdout.

Example:

```text
Checkpoint ckpt_123 can restore 2 files.
Conflicts: 0
Skipped: 1

Apply:
  pico checkpoints restore ckpt_123 --apply
```

### JSON Output

JSON output is the stable machine interface.

Success envelope:

```json
{
  "ok": true,
  "kind": "checkpoint_restore_preview",
  "data": {}
}
```

Error envelope:

```json
{
  "ok": false,
  "error": {
    "code": "checkpoint_not_found",
    "message": "Unknown checkpoint: ckpt_missing",
    "hint": "Run `pico checkpoints list`."
  }
}
```

Existing raw recovery records may appear inside `data`, but the CLI wrapper should own the outer shape.

## Exit Codes

```text
0 success
1 runtime_or_model_error
2 usage_or_validation_error
3 config_or_provider_error
4 approval_or_security_error
5 internal_error
```

Unknown subcommands and unknown flags should return `2` and include a suggestion when possible.

## Safety Rules

- `checkpoints preview-restore` never mutates repository files.
- `checkpoints restore` without `--apply` behaves as preview.
- `checkpoints restore --apply` writes only paths the Recovery Manager marks safely restorable.
- `checkpoints prune` defaults to preview/dry-run.
- `checkpoints prune --apply` removes only artifacts the Checkpoint Store reports as prunable.
- No CLI command may automatically restore repository files after a failed run.
- `--no-input` forbids interactive prompts. If an action requires confirmation, the command fails with a clear hint.

## Status And Diagnostics

`pico status` should summarize the active harness state without contacting model providers:

- workspace root and branch;
- dirty/clean repository summary;
- selected provider and model source;
- latest session id;
- latest run id;
- latest checkpoint id;
- basic `.pico/` storage presence.

`status` should not include session memory summaries. Session and memory inspection should stay behind explicit session-oriented commands so status remains short, low-risk, and predictable.

`pico doctor` should diagnose configuration and runtime readiness:

- workspace detection;
- `.env` parsing;
- selected provider;
- required API key presence without printing the key;
- provider base URL;
- provider connectivity with bounded timeouts;
- `.pico/sessions`, `.pico/runs`, and `.pico/checkpoints` readability;
- common recovery-store shape issues.

`doctor` should not mutate files or reveal secrets. It should perform a complete readiness check by default, including provider connectivity where relevant, and classify failures by area so users can see whether the problem is workspace detection, configuration, credentials, provider connectivity, storage permissions, or recovery-store shape.

`pico config show` should explain effective configuration and source precedence:

```text
CLI flag > shell environment > project .env > pico.toml > built-in default
```

For the current codebase, API keys remain environment-derived for compatibility. The CLI should avoid printing secrets and should report only presence, source, and redacted names.

`pico config show --format json` should expose both effective values and source metadata so scripts and agentic workflows can diagnose precedence problems. Secret-bearing fields must report presence and source without exposing the value.

Example:

```json
{
  "ok": true,
  "kind": "config_show",
  "data": {
    "provider": {
      "value": "deepseek",
      "source": "project_env",
      "name": "PICO_PROVIDER"
    },
    "model": {
      "value": "deepseek-v4-pro",
      "source": "default"
    },
    "api_key": {
      "present": true,
      "source": "shell_env",
      "name": "PICO_DEEPSEEK_API_KEY"
    }
  }
}
```

## Parser And Module Shape

Keep stdlib `argparse` and split the current CLI module into focused helpers:

```text
pico/cli.py              # console entry shim and main()
pico/cli_parser.py       # argparse command tree
pico/cli_commands.py     # command dispatch
pico/cli_output.py       # text/json rendering
pico/cli_errors.py       # typed CLI errors and exit code mapping
pico/cli_diagnostics.py  # status, doctor, config show helpers
```

The split is organizational. It should not change runtime ownership or introduce a new CLI framework.

## High-Feasibility Execution Plan

### Phase 0: Baseline Lock

Goal: prove current CLI recovery behavior before changing parser shape.

Actions:

- Run the existing recovery CLI and public API tests.
- Add any missing characterization tests for legacy bare prompt behavior if needed.
- Record the expected behavior for `pico`, `pico "prompt"`, `pico checkpoints ...`, and `pico runs ...`.

Acceptance:

- Existing recovery CLI tests pass.
- Public API tests pass.
- There is a clear failing-test target before each behavior change.

### Phase 1: Split CLI Infrastructure Without Behavior Change

Goal: make the CLI code small enough to evolve safely.

Actions:

- Add `pico/cli_errors.py` for typed CLI errors and exit-code mapping.
- Add `pico/cli_output.py` for text and JSON rendering helpers.
- Add `pico/cli_parser.py` for parser construction while keeping `pico.cli.build_arg_parser` as the public wrapper.
- Add `pico/cli_commands.py` for command dispatch.
- Add `pico/cli_diagnostics.py` for later `status`, `doctor`, and `config show` helpers.
- Keep `pico/cli.py` as the console entry shim and compatibility export module.

Acceptance:

- No user-visible behavior changes.
- `pico.cli` public imports remain valid.
- Existing tests still pass.

### Phase 2: Add Explicit `run` And `repl`

Goal: establish explicit startup commands while preserving compatibility.

Actions:

- Implement `pico run [prompt...]` as the primary one-shot path.
- Implement `pico repl` as the explicit interactive path.
- Preserve `pico` as no-argument REPL.
- Preserve `pico "prompt"` as silent bare-prompt compatibility.

Acceptance:

- `pico run "x"` calls the agent once.
- `pico "x"` still calls the agent once.
- `pico repl` enters the existing REPL loop.
- Bare prompt compatibility prints no migration warning.

### Phase 3: Protocolize Runs And Checkpoints

Goal: move existing hidden recovery commands behind the explicit dispatcher and add stable output behavior.

Actions:

- Implement explicit `runs list/show`.
- Implement explicit `checkpoints list/show/preview-restore/restore/prune`.
- Keep legacy `pico checkpoints ...` and `pico runs ...` working through compatibility dispatch.
- Add `--format text|json` for these commands.
- Wrap JSON output in the stable `{ok, kind, data}` envelope.
- Return typed error envelopes for missing runs/checkpoints.

Acceptance:

- `preview-restore` never mutates files.
- `restore` without `--apply` behaves as preview.
- `restore --apply` still delegates safety decisions to `RecoveryManager`.
- `prune` without `--apply` does not delete blobs.
- `prune --apply` only removes `CheckpointStore.prune` candidates.

### Phase 4: Add Status, Config, And Doctor

Goal: make harness state and configuration problems visible.

Actions:

- Implement `pico status` with workspace, branch, dirty/clean summary, selected provider/model, latest session/run/checkpoint ids, and storage presence.
- Implement `pico config show` with effective values and source metadata.
- Implement `pico doctor` as a complete diagnostic pass: workspace, `.env`, provider selection, credential presence, base URL, provider connectivity, storage readability, and recovery-store shape.
- Use bounded timeouts for connectivity checks.
- Categorize doctor failures by area: workspace, config, credentials, provider_connectivity, storage, recovery_store.

Acceptance:

- `status` does not include session memory summaries.
- `status` and `config show` do not build a full `Pico` agent.
- `doctor` does not mutate files, create sessions, or reveal secrets.
- `doctor` reports network/provider problems as diagnostic findings, not uncaught runtime exceptions.
- `config show --format json` includes source metadata and redacts secret values.

### Phase 5: Polish Errors, Help, And Documentation

Goal: make the CLI discoverable and scriptable.

Actions:

- Add root help grouping for startup, diagnostics, recovery inspection, and compatibility notes.
- Add unknown command and unknown flag suggestions.
- Use consistent exit codes.
- Ensure human errors go to stderr.
- Update README examples after behavior is stable.

Acceptance:

- Unknown subcommands exit with code `2`.
- Error JSON uses `{ok: false, error: {...}}`.
- Help does not list unimplemented reserved extension commands.
- README recommends `pico run` and `pico repl` while noting silent legacy compatibility.

### Phase 6: Reserve Future Discovery Commands

Goal: keep the design open without shipping partial commands.

Actions:

- Keep `tools`, `providers`, `schema`, and `completion` in the spec as reserved extension points.
- Do not expose them in CLI help until each command works.

Acceptance:

- Users cannot invoke half-implemented discovery commands.
- Future implementation has a clear command namespace.

## Migration Strategy

1. Add explicit parser branches for `run`, `repl`, `status`, `doctor`, `config`, `runs`, `sessions`, and `checkpoints`.
2. Keep current bare prompt detection as a compatibility path.
3. Move existing hidden `runs` and `checkpoints` helpers behind the explicit command dispatcher.
4. Add output and error helpers.
5. Update README and help screenshots after behavior is stable.
6. Add reserved command names to unknown-command suggestions only after they are implemented.

## Test Strategy

Focused tests should prove:

- `pico run <prompt>` calls the agent once.
- `pico repl` enters the existing REPL path.
- `pico <prompt>` still works as compatibility behavior.
- `pico checkpoints list/show/preview-restore/restore/prune` preserve current recovery behavior.
- `restore` without `--apply` does not mutate files.
- `prune` without `--apply` does not delete blobs.
- `--format json` wraps successful command data in a stable envelope.
- unknown subcommands return exit code `2` with a suggestion.
- `status` and `config show` do not require model client construction.
- `doctor` does not construct a full `Pico` agent or mutate session state.
- `--no-input` blocks interactive confirmations.

## Out Of Scope

- Full-screen TUI.
- Hunk-level restore UI.
- Cross-project global search.
- Plugin marketplace.
- Complex schema introspection.
- Shell completion generation.
- Secret migration to keychain or credential files.
- Switching from `argparse` to a third-party CLI framework.

## Resolved Design Questions

- `status` does not include session memory summaries; memory/session inspection is handled separately.
- `doctor` performs a complete readiness check by default, including provider connectivity, with bounded timeouts and clear failure categories.
- Bare prompt compatibility remains silent; no migration tip is printed during normal one-shot execution.
- `config show --format json` includes effective values and source metadata, while never exposing secret values.
