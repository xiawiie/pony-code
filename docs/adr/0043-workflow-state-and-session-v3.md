# ADR-0043: Workflow state and Session v3

## Status

Accepted for implementation. The feature is not released until the complete integration branch passes the full offline gate.

## Context

Pony already has append-only Session Trees, atomic tool exchanges, task checkpoints, rewind, and recovery. It does not have a durable user-selected workflow mode or a model-maintained active plan. Task checkpoints are finalized after a top-level turn and therefore cannot represent progress updated within that turn.

Adding these values to the Session projection changes the persisted contract. Reusing `session_info`, checkpoint records, Run traces, or a second plan file would create ambiguous writers or make old format version 2 artifacts mean two different things.

## Decision

- Session format 3 owns `workflow_mode` and `active_plan` on the active path. Defaults are `act` and the canonical empty plan.
- Human mode changes and plan clears use explicit `workflow_mode_change` and `plan_update` control entries.
- A successful model `update_plan` is projected from its existing atomic `tool_exchange`; the Plan is not copied into a second entry field.
- Workflow mode is a capability ceiling enforced before approval. It can only remove authority. `RuntimeOptions.read_only` and `approval=never` remain stronger limits.
- The model-visible tool schema is filtered for the frozen turn mode, while the shared executor independently enforces the same ceiling.
- Mode can be selected by `/mode` or an explicit `--mode` for `run` and `repl`. It is not a Provider setting, environment variable, TOML default, or `RuntimeOptions` field.
- Version 1 JSON and version 2 JSONL inspection remains read-only. Only explicit runtime resume migrates them to version 3.

## Consequences

- Version 3 has one writer and one active projection. Checkpoint, Run, Recovery, and Sandbox formats do not change.
- A version 2 tree is rewritten without changing entry identity, parentage, ordering, timestamps, types, or data. A legacy `model_change` entry is rejected because no production writer or projection defined its meaning.
- Reset clears the Plan but preserves the selected Mode. Fork and rewind restore both from the target path. Worktree clone copies both while clearing workspace-bound recovery state.
- Older binaries fail closed on version 3. No downgrade writer is provided.

## Rejected alternatives

- A Todo Store or `/todo` alias: duplicates Active Plan without independent value.
- `tool_exchange.data.plan`: duplicates the Plan already present in the validated tool call.
- Mode in `RuntimeOptions`, `.env`, or `pony.toml`: creates competing defaults for Session state.
- A policy DSL, event bus, generic state delta, or new runtime dependency: unnecessary for three fixed modes and one state tool.
- Shipping Session v3 separately from its policy and UI consumers: exposes an irreversible migration without a complete user workflow.
