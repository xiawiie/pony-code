# Agent Harness v1 Overview

Agent Harness v1 is Pico's current runtime shape: a local control loop around a model, repository context, constrained tools, task state, memory, and auditable run artifacts.

## Runtime Flow

1. Build workspace context and runtime prefix.
2. Record the user request in session history.
3. Create task state for the run.
4. Build bounded prompt context.
5. Request the model response.
6. Parse the response into a tool call, retry notice, or final answer.
7. Execute tools through runtime policy.
8. Write task state, trace events, checkpoints, and report artifacts.

## State Artifacts

- `task_state.json` records attempts, tool steps, status, stop reason, and final answer.
- `trace.jsonl` records the event timeline for prompt, model, tool, checkpoint, and finish phases.
- `report.json` records the review summary, prompt metadata, durable memory changes, and execution metadata.
- `.pico/checkpoints/records/*.json` stores user-facing Checkpoint Records. A `checkpoint_type="turn"` record is created once for a user request that produced one or more repository-changing Tool Change Records; `checkpoint_type="restore"` records preserve restore provenance.
- `.pico/checkpoints/tool_changes/*.json` stores internal per-tool Tool Change Records. Turn Checkpoints link these records through `tool_change_ids` instead of exposing each tool invocation as its own restore entrypoint.
