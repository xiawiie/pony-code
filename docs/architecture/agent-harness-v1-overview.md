# Agent Harness v1 Overview

> **Archived / superseded:** This historical overview is retained for reference. The current runtime is Action Kernel and Messages v3; see the [current review pack](../review-pack/README.md).

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

## Run Artifact Terminology

Every user request creates one run directory under `.pico/runs/<run_id>/`. The run directory is an audit bundle for reviewing what happened during that request; it is not the recovery truth for restoring files.

| File | Role | Use it for | Do not use it for |
| --- | --- | --- | --- |
| `task_state.json` | Mutable state-machine snapshot, rewritten atomically throughout the run. | Checking the current or terminal run status, attempts, tool step count, last tool, stop reason, final answer, resume status, and linked checkpoint ids. | Reconstructing every event in order or restoring file contents. |
| `trace.jsonl` | Append-only JSON Lines event journal. | Replaying the sequence of `run_started`, prompt/model/tool, checkpoint, verification, and `run_finished` events with timing and cross-reference ids. | Serving as the source of checkpoint records, file blobs, or restore decisions. |
| `report.json` | Final run summary written at terminal state. | Reading the compact review summary: status, stop reason, final answer, embedded task state, prompt metadata, durable memory changes, and redacted environment summary. | Streaming progress or preserving the full event timeline. |

These artifacts are redacted before persistence. They may contain checkpoint ids, tool change ids, verification ids, and prompt/cache metadata so reviewers can jump to the owning store, but `.pico/checkpoints/` remains the source of restorable state.

## State Boundaries

- `.pico/runs/<run_id>/task_state.json` tracks the state of one execution attempt.
- `.pico/runs/<run_id>/trace.jsonl` tracks what happened during that execution.
- `.pico/runs/<run_id>/report.json` summarizes the finished execution for review and aggregation.
- `.pico/checkpoints/records/*.json` stores user-facing Checkpoint Records. A `checkpoint_type="turn"` record is created once for a user request that produced one or more repository-changing Tool Change Records; `checkpoint_type="restore"` records preserve restore provenance.
- `.pico/checkpoints/tool_changes/*.json` stores internal per-tool Tool Change Records. Turn Checkpoints link these records through `tool_change_ids` instead of exposing each tool invocation as its own restore entrypoint.
- `.pico/sessions/*.json` stores conversation continuity, resume summaries, and the latest recovery checkpoint pointer.
