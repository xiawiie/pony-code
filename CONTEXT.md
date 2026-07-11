# Pico

Pico is a coding-agent harness for repository-grounded engineering work. This glossary defines the project language used to discuss its domain boundaries.

## Language

**Coding-Agent Harness**:
The runtime boundary around a coding model that controls tool access, execution policy, task state, checkpoints, traces, and verification artifacts for repository-grounded engineering work.
_Avoid_: coding agent product, chat assistant, IDE clone

**Pico CLI**:
The single `pico` console command used to run Pico, inspect harness state, and request user-initiated recovery actions.
_Avoid_: alternate console command, bare prompt dispatch, TUI, IDE shell

**Model Request**:
The runtime request containing system instructions, tools, canonical messages, token budget, and cache breakpoints.
_Avoid_: flattened prompt, provider payload, transport string

**Model Response**:
The provider-neutral `Response` returned to the runtime.
_Avoid_: raw JSON, SSE event, provider-specific response

**Action**:
The Tool, Final, or Retry decision produced by `decode_action` from a Model Response.
_Avoid_: provider tool call, raw model text, command string

**Canonical Messages**:
The Session's single transcript used to construct each Model Request.
_Avoid_: duplicate history, provider transcript, compatibility history

**Text Protocol Adapter**:
The explicit boundary that converts a structured Model Request to a text transport prompt.
_Avoid_: automatic wrapping, provider registry, runtime capability probing

**Project Environment**:
The `.env` file at the current lexical repository root, read without global environment injection.
_Avoid_: parent repository search, process mutation, cross-project env

**Format Version**:
The encoding version stored inside a record family that can be parsed independently.
_Avoid_: release version, nested-object version, global benchmark version

**Query Snapshot**:
The path, metadata, frontmatter, and raw content shared within one memory query and released afterward.
_Avoid_: process cache, watcher, persistent index copy

**Recovery Record**:
A persisted top-level checkpoint or tool-change record.
_Avoid_: nested schema version, Git commit, conversation snapshot

**Recoverable Editing**:
A harness capability that makes agent-produced repository changes inspectable, explainable, resumable, and restorable across a task session.
_Avoid_: undo feature, backup system, version control replacement, recoverable editing harness

**Recovery Boundary**:
The scope of repository and session state that Recoverable Editing promises to restore or preserve during agent work.
_Avoid_: full conversation rewind, VM snapshot, Git replacement

**Checkpoint Record**:
A session-local recovery point that captures enough repository and task state for agent work to be inspected, resumed, or restored within its Recovery Boundary.
_Avoid_: Git commit, backup archive, conversation snapshot

**Checkpoint Store**:
The persistence boundary for Checkpoint Records, Tool Change Records, and file-state blobs used by Recoverable Editing.
_Avoid_: recovery engine, session store, Git storage

**Checkpoint Pruning**:
A user-initiated cleanup operation that removes checkpoint artifacts only after previewing what will be deleted and verifying they are outside active recovery references.
_Avoid_: automatic GC, history rewrite, silent cleanup

**File-State Blob**:
An immutable stored representation of a file state referenced by checkpoint and tool-change records.
_Avoid_: diff, patch, backup file

**Workspace-Relative Path**:
A normalized repository path recorded relative to the active workspace root for checkpoint and restore decisions.
_Avoid_: absolute path, current-directory path, display path

**Affected Path**:
A Workspace-Relative Path that a Tool Change Record identifies as changed or requiring recovery review.
_Avoid_: full workspace snapshot, declared-only path, display-only path

**Snapshot Eligibility**:
The harness rule that decides whether a file state may be stored as a fully restorable File-State Blob.
_Avoid_: file type check, backup filter, ignore list only

**Turn Checkpoint**:
A user-facing Checkpoint Record that represents the recoverable state around one user-directed agent turn.
_Avoid_: per-tool checkpoint, chat message snapshot, Git commit

**Restore Checkpoint**:
A Checkpoint Record created after a restore action to preserve the post-restore repository state and its restoration provenance.
_Avoid_: rewound pointer, deleted history, restored commit

**Automatic Checkpointing**:
A harness behavior that records recoverable state during agent work without requiring the user to request a checkpoint.
_Avoid_: auto-restore, background backup, implicit Git commit

**Tool Change Record**:
An internal record of a tool invocation's repository effects and the recovery obligations implied by its Tool Effect Class.
_Avoid_: trace event, diff summary only, log line

**Pending Tool Change**:
A Tool Change Record created before a tool runs and finalized after the tool succeeds, fails, or leaves observable side effects.
_Avoid_: completed tool log, success-only record, trace event

**Interrupted Tool Change**:
A Pending Tool Change found during resume without a completed finalization outcome.
_Avoid_: failed tool, successful tool, ignored pending record

**Delegated Change**:
A Tool Change Record produced through a delegate boundary and attributed to the parent Turn Checkpoint that requested the delegated work.
_Avoid_: child checkpoint, independent subagent restore, hidden delegate edit

**Trace Timeline**:
An append-only audit sequence that explains model, tool, checkpoint, verification, and finish events during agent work.
_Avoid_: checkpoint store, recovery state, full transcript

**Recovery Review**:
A decision point where agent-produced changes are inspected before the user chooses what to retain, restore, or investigate further.
_Avoid_: automatic rollback, blind restore, diff viewer only

**Recovery Manager**:
The policy boundary that decides whether a Checkpoint Record can be restored, conflicts with current state, or must enter Recovery Review.
_Avoid_: checkpoint store, blob store, diff viewer

**Restore Plan**:
A Recovery Manager decision artifact that describes which checkpointed changes can be restored, skipped, or must enter Recovery Review.
_Avoid_: restore command, rollback log, patch plan

**Restore Preview**:
A non-mutating presentation of a Restore Plan before repository files are changed.
_Avoid_: dry-run flag, no-op restore, hidden validation

**User-Initiated Restore**:
A recovery rule that repository-changing restore actions begin only after an explicit user request.
_Avoid_: automatic rollback, self-healing restore, silent recovery

**Selective Restore**:
A recovery choice that applies only selected fully restorable file entries from a Restore Plan.
_Avoid_: hunk restore, partial patch replay, all-or-nothing rollback

**Snapshot Restore**:
A recovery strategy that restores files from recorded file states only when the current state still matches the expected agent-produced state.
_Avoid_: reverse patch, blind overwrite, VM restore

**Restore Conflict**:
A recovery condition where the current repository state no longer matches the state expected by a Checkpoint Record.
_Avoid_: merge conflict, corrupted checkpoint, auto-merge case

**Git Review Context**:
Git-derived repository facts used to explain or review checkpoint and restore decisions without serving as the restore mechanism.
_Avoid_: Git restore engine, Git checkpoint, stash-based recovery

**Verification Evidence**:
Task evidence that records whether an agent-produced state was checked by commands such as tests, linters, or type checks.
_Avoid_: recovery truth, pass badge, CI replacement

**Tool Effect Class**:
A harness-level category that describes what state a tool may affect and what recovery or review obligations follow from that effect.
_Avoid_: risky flag, permission group, tool type

**Safe Execution**:
A harness constraint that bounds model-initiated actions through tool policy, approval flow, sandbox boundary, and auditable execution records.
_Avoid_: security feature, permission prompt, sandbox only

**Command Boundary**:
The Safe Execution boundary around command execution where policy, approval, runtime metadata, and Shell Side Effects are recorded.
_Avoid_: OS sandbox, shell wrapper only, unrestricted bash

**Command Risk Class**:
A policy category that describes the expected risk of a command before execution and drives approval or rejection decisions.
_Avoid_: allowlist entry, denylist match, shell tool type

**Command Approval**:
A Safe Execution decision that allows, rejects, or asks the user before command execution based on command risk and recovery boundaries.
_Avoid_: permission prompt only, global allow, user annoyance

**Shell Side Effect**:
A repository or environment change produced by command execution rather than by a harness-mediated file editing operation.
_Avoid_: hidden change, bash diff, tracked edit

**AGENTS.md**:
The project-convention file read at session start. Pico loads AGENTS.md; it does not load CLAUDE.md. `pico doctor` flags a repo that ships CLAUDE.md without AGENTS.md.
_Avoid_: CLAUDE.md loader, README fallback, prompt boilerplate

**User Notes**:
Free-form Markdown files under `.pico/memory/notes/` (or `~/.pico/memory/notes/`) that the agent may read (via `memory_read` / `memory_search`) but must not modify.
_Avoid_: agent scratchpad, chat log, editable prompt

**Agent Notes**:
The one append-only `agent_notes.md` file for each memory scope, where the agent records short timestamped lessons when the user explicitly asks it to remember something. Soft cap 8000 chars.
_Avoid_: unbounded journal, user notes, generic scratch file

**Repo Map**:
The AST/regex-derived symbol index served via the `repo_lookup` tool. Kept out of the prompt prefix; queried on demand.
_Avoid_: LSP replacement, ctags mirror, prompt-injected index

**Memory Index**:
The auto-rendered listing of memory files (mtime + first line) injected into the stable prompt prefix. Byte-identical across turns when nothing changes so prompt-cache remains hot.
_Avoid_: full memory dump, dynamic memory tail, chat summary

## pico.toml Configuration Surface

Pico reads optional configuration from `<repo>/pico.toml`. Every key
falls back to a hard-coded default if the file is missing, the section
is missing, or the value has a bad type. Sample:

    [context]
    history_soft_cap = 40000        # tokens; messages array trim threshold
    history_floor_messages = 6      # tail messages always preserved
    injection_budget_ratio = 0.15   # fraction of total budget for <system-reminder> blocks
    system_tools_hard_cap = 20000   # tokens; request build fails loud if system+tools exceed

    [context.digest]
    size_threshold_chars = 1200     # tool_result char count above which digest applies

    [memory.recall]
    min_score = 0.3                 # normalized BM25 gate
    top_k = 2                       # max notes recalled per turn
    max_tokens_per_note = 400       # per-note cap in the recall block
    skip_recent_turns = 2           # don't re-recall notes shown in last N turns

    [memory.retrieval.field_boost]
    name = 5.0
    description = 3.0
    tags = 4.0
    aliases = 4.0
    body = 1.0

    [memory.retrieval.link]
    max_added = 3                   # neighbors per query via [[name]] expansion
    decay = 0.4                     # neighbor score multiplier

**When to change**: `history_soft_cap` if your model returns 413 on
long sessions; `recall.min_score` if recall surfaces irrelevant memory
too often; `field_boost.name` and friends if a domain-specific note
naming convention benefits from re-weighting. The `intent_profiles`
keywords are NOT overridable via `pico.toml` — edit
`pico/context/intent.py` directly.
