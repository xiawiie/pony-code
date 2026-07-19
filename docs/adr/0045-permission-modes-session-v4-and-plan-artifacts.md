# ADR-0045: Permission modes, Session v4, and Plan artifacts

## Status

Accepted and implemented for the unreleased Pony 1.0 line. This ADR supersedes
[ADR-0043](0043-workflow-state-and-session-v3.md).

## Context

Session v3 exposed Pony-specific `plan|act|review` workflow modes and a structured Active Plan. Their command surface and
interaction model did not match the six permission modes users encounter in Claude Code. The structured plan also mixed task
tracking with the plan a user reviews before implementation.

Pony still needs append-only Session state, deterministic enforcement, explicit approval, and migration that fails closed. User
familiarity with Claude Code does not justify claiming that Pony has Claude's internal model classifier or allowing a mode flag to
bypass trust, schema, path, secret, Memory, Sandbox, or Recovery boundaries.

## Decision

### Public modes and canonical storage

Pony exposes exactly these six user-facing modes:

| Public mode | Canonical Session value | Default behavior without an exact-tool rule |
| --- | --- | --- |
| `manual` | `default` | Allow reads; ask before mutations |
| `acceptEdits` | `acceptEdits` | Allow built-in file writes and patches; ask for other mutations |
| `auto` | `auto` | Allow only locally classified low-risk mutations; deny unmatched mutations |
| `bypassPermissions` | `bypassPermissions` | Allow unruled mutations without prompting; exact `ask` still prompts |
| `dontAsk` | `dontAsk` | Never prompt; deny mutations unless an exact allow rule applies |
| `plan` | `plan` | Expose non-shell reads and Plan tools; apply an ASK floor to other mutations |

`manual` is a public alias only. Validation normalizes it to `default`, and UI rendering maps `default` back to `manual`. A fresh
Runtime Session starts in `auto`; the v4 base projection retains `default` as its implicit canonical baseline so append-only control
entries make a fresh Runtime's explicit `auto` selection observable.

Pony's `auto` mode uses a local deterministic classifier. It allows built-in `write_file` / `patch_file`, a `memory_save` that also
passes the independent current-request authorization gate, and shell commands that the local grammar proves read-only. It is not
Claude Code's model classifier and is not claimed to be internally equivalent.

The process must receive explicit dangerous capability before it can select or resume `bypassPermissions`:

- `--allow-dangerously-skip-permissions` does not change mode. It allows the interactive permission picker to select bypass,
  permits an explicit `--permission-mode bypassPermissions`, and reauthorizes a persisted bypass Session on resume.
- `--dangerously-skip-permissions` directly selects the mode and conflicts with any other `--permission-mode`.

Direct selection and persisted resume are checked before Provider construction; interactive selection checks the transient capability
at the picker boundary. Explicitly resuming into another permission mode does not require dangerous capability. Bypass changes the
mode default for an unruled mutation to ALLOW; an exact `ask` still prompts. Project trust, explicit deny, `read_only`, tool
availability, schema, path and secret validation, shell hard rejects, current-request Memory authorization, Sandbox, and Recovery
remain enforced.

### Exact-tool rules and precedence

`permission_rules` contains three disjoint lists named `allow`, `ask`, and `deny`. Entries are exact legal tool names, not globs,
command patterns, or source-scoped rules. `/permissions`, `/allowed-tools`, `--allowed-tools`, and `--disallowed-tools` edit the same
Session state. The interactive editor accepts repeated changes before it closes.

The executor applies this order:

1. shell hard reject, tool availability/allowlist, and argument schema validation;
2. `read_only`, project trust, known effect class, and exact `deny` fail-closed checks;
3. in Plan mode, every mutation except `write_plan` has an ASK floor, so an exact allow cannot silently lower it;
4. remaining exact `allow` or `ask`, followed by the selected mode's default;
5. if the result is ASK, approval and exact-argument revalidation before one execution.

An exact `ask` becomes DENY in `dontAsk`. An exact `allow` remains usable in `dontAsk`. Schema hiding is not an authority boundary:
the shared executor repeats policy checks for a directly requested hidden tool.

### Plan artifact and approval

Plan mode exposes `read_plan`, `write_plan`, and `exit_plan_mode` plus non-shell read-only tools. A fixed system reminder tells the model
to use these tools and not request approval in ordinary text.

- `read_plan` returns the current artifact and never writes Session state.
- `write_plan` accepts one non-empty Markdown string. Before persistence, runtime validation rejects text above 12 KiB of UTF-8 or
  text containing a known secret. A changed value appends `plan_artifact {text, revision}` with a monotonically increasing revision;
  it is the only automatic Plan write.
- `exit_plan_mode` requires non-empty text and revision at least one. Approval receives the exact text and revision. After approval,
  the executor revalidates the tool arguments and compares both fields with the approved snapshot. Any rejection or CAS-style
  mismatch leaves the Session in `plan`.
- A successful exit restores `pre_plan_mode`, falling back to `auto` only when it is absent. The Agent Loop then refreshes the frozen
  mode and visible schemas so the same top-level request can continue implementation.
- `/plan open` uses `$VISUAL` or `$EDITOR` and saves only if the original revision still matches. `/plan share` is explicitly
  unavailable in the local runtime. Neither command changes the current permission mode. Explicit editor saves share the canonical
  Plan validation/persistence path with `write_plan` and add an expected-revision check.

The Plan artifact is not copied into a checkpoint, Run trace, resume card, system prefix, or request metadata. `task_working_set`
continues to project checkpoint and file facts; the model reads Plan text explicitly through `read_plan`.

### Session v4 and migration

Session v4 adds this active-path state:

| Projection field | Writer |
| --- | --- |
| `permission_mode` | `permission_mode_change {mode, pre_mode?}` |
| `permission_rules` | bounded `session_info` update from the permission editor |
| `plan_text` | `plan_artifact {text, revision}` |
| `plan_revision` | the same `plan_artifact` |
| `pre_plan_mode` | projection of entry into and exit from `plan` |

Ordinary Session save rejects direct changes to these fields. Fork, rewind, reset, and worktree clone derive them from the selected
active path rather than creating a second writer.

Version 1 JSON, version 2 JSONL, and version 3 JSONL inspection is read-only. Ordinary current-format readers and writers return
`session_migration_required`; only explicit runtime resume migrates. Migration runs under the Session lock and performs a stable source
read, digest-named backup creation or verification, full candidate parse/projection validation, source identity and digest recheck,
candidate identity recheck, and atomic replace.

Version 1 and 2 migrate to canonical `default` with empty rules and no Plan artifact. Version 3 maps `act` to `default` and maps its
other workflow modes to `plan` with `pre_plan_mode=default`. The old structured Active Plan is not reinterpreted as the new Markdown
artifact; old `plan_update` control data is retained in migration audit entries while the v4 Plan starts empty. A v2 `model_change`
entry remains unsupported and fails closed.

## Consequences

- Users get the Claude Code mode names and interaction shape without a false claim of classifier equivalence.
- Permission prompts are one layer in a larger fail-closed pipeline; bypass cannot widen hard authority.
- Plan review binds approval to one exact artifact revision and can continue into implementation in the same request.
- Session v4 has one active projection and explicit append-only writers. Older binaries fail closed; no downgrade writer is provided.
- The Session format changes, while Run, Checkpoint, Recovery, and Sandbox record formats remain independent.

## Rejected alternatives

- Keep `plan|act|review` as aliases: this preserves two user vocabularies and ambiguous semantics.
- Persist `manual` alongside `default`: this creates two canonical values for one behavior.
- Treat `auto` as Claude's internal classifier: Pony has no evidence or implementation that supports that claim.
- Let bypass skip trust or validation: prompt suppression is not authority to cross hard boundaries.
- Reuse Session v3 Active Plan or task checkpoints: their structured progress data is not the exact artifact approved before coding.
- Add glob/source rule DSLs or a second permission store: exact tool names satisfy the current product contract with one writer.
