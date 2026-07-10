# A1 Task 8 Report: Sensitive Direct Tools, Memory, Search, and Snapshot Inputs

## Status

PASS — implementation and local verification complete on top of
`d4fff8e50cbfa66475f71546a500910aa6a23f37`. The controller-owned
`.superpowers/sdd/progress.md` remains excluded.

## Scope

- Added a pre-run `SensitiveToolError(code)` boundary with stable
  `sensitive_path_block` / `sensitive_content_block` metadata and
  `security_event_type=sensitive_access_block`.
- Validated raw lexical list/read/search/write/patch targets before
  `Pico.path()` can resolve away symlink evidence. Sensitive names are
  case-folded after lexical normalization; every existing symlink component
  is rejected without following it.
- Made `list_files` sort by name only. A sensitive child is classified from
  its lexical name and rendered only as `<basename> [sensitive]`; no child
  stat, lstat, readlink, preview, digest, or content read occurs.
- Hardened both search lanes. Trusted rg uses the frozen absolute executable,
  a child `PATH` containing only that executable's frozen parent directory,
  fixed case-insensitive sensitive globs, forced filename/NUL framing, and a
  fail-closed result-path parser. A second hardened rg invocation receives
  only safely discovered, regular, no-follow `.env.example/.sample/.template`
  file paths, keeping native regex and smart-case semantics without changing
  primary rg ignore behavior. The Python fallback filters sensitive and
  ignored paths before a no-follow regular-file guard.
- Made BlockStore append/topic writes independently reject supplied and
  complete would-be persisted secret content, consume Pico's immutable
  redaction snapshot, and reject symlinked note leaves or agent directories
  before read/write. This also protects `memory_save` and REPL `/save`;
  benign prose such as `password policy` remains accepted.
- Made `snapshot_eligibility()` pure and lexical with the frozen
  `env=None, secret_env_names=()` interface: sensitive paths are rejected
  before filesystem access, the existing chain is checked without following
  symlinks, and one bounded full read performs size, binary, and secret-content
  decisions. It never hashes or writes a blob.
- Made workspace snapshots filter sensitive paths before stat/hash, skip
  symlink files, and route hashing through `hash_file_bytes()`.
- Made current-file and Git-HEAD recovery paths pass redaction configuration,
  reject sensitive paths/content before `write_blob()`, decode HEAD stdout
  with UTF-8 replacement, and rescan the exact bytes currently handed to the
  blob store.
- Redacted full runner output before the 4000-character clip boundary, then
  rebuilt every `ToolExecutionResult` at `Pico.execute_tool()` as a
  redacted copy without mutating the original. Nested metadata container types
  are preserved.
- Added no dependency, Provider call, shell-policy rewrite, artifact-CLI
  policy, public bytes-eligibility interface, or OS sandbox.

## RED

Required Task 8 focused slice plus the explicitly requested REPL and safety
regressions before production changes:

```text
uv run pytest tests/test_sensitive_tools.py tests/test_tools.py +  tests/test_tool_executor.py tests/memory/test_memory_tools.py +  tests/memory/test_block_store.py tests/test_recovery_policy.py +  tests/test_workspace_snapshot.py tests/memory/test_repl_v2.py +  tests/test_safety_invariants.py -q
33 failed, 131 passed in 3.29s
```

The failures proved that sensitive direct paths and write content reached
runners, benign symlinks were followed, list sorting statted `.env`, both
search lanes exposed sensitive matches, BlockStore and `/save` persisted
tokens, snapshot eligibility read only 4096 bytes and followed symlinks,
workspace snapshots hashed sensitive/symlink files, HEAD fallback wrote raw
secret bytes, and Pico returned the original unredacted result.

The allowed-template parity probe also failed before its narrow supplement:

```text
uv run pytest +  tests/test_sensitive_tools.py::test_directory_search_excludes_sensitive_paths_without_path_rescan -q
1 failed, 1 passed in 0.13s
```

Trusted rg safely excluded all `.env.*` files but thereby hid the three
explicitly allowed templates. The initial implementation supplemented them
through Python; the formal-review follow-up below replaces that supplement
with a second hardened rg invocation over only safe exact template-file paths
so one engine owns the search semantics.

The rg return-code edge failed before the early-return fix:

```text
uv run pytest +  tests/test_tools.py::test_rg_search_keeps_allowed_env_template_when_rg_has_no_other_match -q
1 failed in 0.03s
```

It proved that an rg return code of 1 skipped the safe template supplement.

## GREEN and Verification

Final focused Task 8 gate, including REPL and safety invariants:

```text
uv run pytest tests/test_sensitive_tools.py tests/test_tools.py +  tests/test_tool_executor.py tests/memory/test_memory_tools.py +  tests/memory/test_block_store.py tests/test_recovery_policy.py +  tests/test_workspace_snapshot.py tests/memory/test_repl_v2.py +  tests/test_safety_invariants.py -q
168 passed in 2.84s
```

Adjacent Pico/runtime, AgentLoop, context, CLI, recovery, observer, store, and
memory regressions:

```text
uv run pytest tests/test_pico.py tests/test_agent_loop.py +  tests/test_context_manager.py tests/test_context_sources.py +  tests/test_bootstrap_read_safety.py tests/test_recovery_manager.py +  tests/test_recovery_checkpoint_writer.py tests/test_recovery_cli.py +  tests/test_cli_commands.py tests/test_cli_diagnostics.py +  tests/test_workspace_observer.py tests/test_session_store.py +  tests/test_run_store.py tests/test_memory_save_topic.py +  tests/memory/test_retrieval.py tests/memory/test_invariants.py -q
223 passed in 7.94s
```

Post-review recovery/snapshot slice:

```text
uv run pytest tests/test_recovery_policy.py tests/test_tool_executor.py +  tests/test_workspace_snapshot.py -q
64 passed in 1.90s
```

Static checks:

```text
uv run ruff check pico/tools.py pico/tool_context.py pico/tool_executor.py +  pico/runtime.py pico/security.py pico/memory pico/recovery_policy.py +  pico/workspace_snapshot.py tests/test_sensitive_tools.py tests/test_tools.py +  tests/test_tool_executor.py tests/memory/test_memory_tools.py +  tests/memory/test_block_store.py tests/test_recovery_policy.py +  tests/test_workspace_snapshot.py tests/memory/test_repl_v2.py
All checks passed!

git diff --check
exit 0
```

Fresh full local gate:

```text
./scripts/check.sh
All checks passed!
1095 passed in 61.26s
```

No real Provider or live E2E call was made.

## Formal Review Follow-up

Formal review of `409466d6bfdeab11b8e767ece7d888a13203f36c` returned
five Important findings and one semantic Minor. All six are closed in the
follow-up commit containing this report:

- Sensitive component rules now apply to every normalized path component.
  Allowed env-template names are exceptions only when they are the final leaf,
  so `.env/child`, `.env.example/child`, credential-name directories, key-name
  directories, direct tools, search, snapshots, and Git-HEAD fallback all fail
  closed before read/Git/blob work.
- Risky tools revalidate immediately after the single approval callback and
  before Tool Change pending state, snapshots, blobs, or the runner. Approval
  callbacks that swap a target to a symlink or alter patch input to contain a
  secret now yield a stable rejection with zero runner/blob calls.
- A directly constructed `BlockStore(redaction_env=None)` snapshots
  `os.environ` at construction. Later process-environment changes cannot alter
  its redaction decisions.
- BlockStore screens note, topic, type, and scope before validation that could
  reveal a value. Invalid topic and scope errors are stable and contain no
  rejected input.
- Hardened rg no longer calls the live-PATH filtering path. Its child `PATH`
  is exactly the parent directory of the already frozen absolute executable;
  Git keeps its existing environment behavior.
- The Python env-template content-search supplement was removed. A second
  hardened rg invocation receives only safely discovered exact template-file
  paths, preserving regex and smart-case behavior while ordinary source,
  ignore rules, and hidden `.git`/`.pico` exclusions remain intact.

The follow-up RED probes failed before each production fix:

```text
sensitive descendant boundary probes: 10 failed
post-approval write/patch revalidation probes: 2 failed
BlockStore default-environment snapshot probe: 1 failed
BlockStore pre-validation screening probes: 2 failed
frozen-rg child-PATH probe: 1 failed
allowed-template rg regex/smart-case probes: 2 failed
```

Final focused Task 8 gate after the review fixes:

```text
uv run pytest tests/test_sensitive_tools.py tests/test_tools.py \
  tests/test_tool_executor.py tests/memory/test_memory_tools.py \
  tests/memory/test_block_store.py tests/test_recovery_policy.py \
  tests/test_workspace_snapshot.py tests/test_security.py \
  tests/test_safe_subprocess.py tests/memory/test_repl_v2.py \
  tests/test_safety_invariants.py -q
297 passed in 2.93s
```

Adjacent runtime/recovery/CLI/context verification remained green:

```text
223 passed in 7.76s
```

Static and whitespace verification:

```text
uv run ruff check <all follow-up production and test files>
All checks passed!

git diff --check
exit 0
```

Fresh full local gate after all follow-up changes:

```text
./scripts/check.sh
All checks passed!
1113 passed in 61.63s
```

No real Provider or live E2E call was made during the follow-up.

## Additional Adversarial Follow-up

A read-only adversarial audit immediately after `3d5527c` found three more
Important edge cases. They are closed in the subsequent commit containing
this report:

- Approval receives a deep copy of the validated arguments. Mutation of that
  copy cannot alter execution input, any mutation makes the approval stale,
  and validation plus command-risk policy are rerun before pending state or
  the runner. One prompt is preserved and final risk metadata still describes
  the immutable execution command.
- Primary rg now has negative sensitive globs only. Removing positive
  whitelist globs restores normal `.gitignore` behavior for ordinary files
  and directories.
- Allowed env templates are lexically discovered, accepted only as regular
  no-follow files, and passed to a separate hardened rg call as exact paths.
  Template-named directories and their descendants are never passed to rg;
  their content is absent even from raw rg output before defensive filtering.

The four narrow tests failed together before these fixes:

```text
uv run pytest \
  tests/test_tool_executor.py::test_run_shell_rechecks_command_policy_after_approval_mutation \
  tests/test_tool_executor.py::test_run_shell_rejects_safe_arguments_changed_after_approval \
  tests/test_sensitive_tools.py::test_rg_search_preserves_ignore_rules_for_ordinary_files \
  tests/test_sensitive_tools.py::test_rg_excludes_env_template_directory_descendants_before_filtering -q
4 failed in 0.21s
```

The same exact gate after the fixes:

```text
4 passed in 0.13s
```

Final focused Task 8 gate after the additional audit:

```text
uv run pytest tests/test_sensitive_tools.py tests/test_tools.py \
  tests/test_tool_executor.py tests/memory/test_memory_tools.py \
  tests/memory/test_block_store.py tests/test_recovery_policy.py \
  tests/test_workspace_snapshot.py tests/test_security.py \
  tests/test_safe_subprocess.py tests/memory/test_repl_v2.py \
  tests/test_safety_invariants.py -q
301 passed in 3.04s
```

Adjacent verification and static checks:

```text
adjacent runtime/recovery/CLI/context gate: 223 passed in 7.56s
uv run ruff check <additional follow-up production and test files>
All checks passed!
git diff --check
exit 0
```

Fresh full local gate:

```text
./scripts/check.sh
All checks passed!
1117 passed in 61.41s
```

No real Provider or live E2E call was made during the additional follow-up.

## Files

- `pico/tools.py`, `pico/tool_context.py`, `pico/tool_executor.py`,
  `pico/runtime.py`, `pico/security.py`, `pico/safe_subprocess.py`
- `pico/memory/block_store.py`, `pico/recovery_policy.py`,
  `pico/workspace_snapshot.py`
- `tests/test_sensitive_tools.py`, `tests/test_tools.py`,
  `tests/test_tool_executor.py`
- `tests/memory/test_memory_tools.py`,
  `tests/memory/test_block_store.py`, `tests/memory/test_repl_v2.py`
- `tests/test_recovery_policy.py`, `tests/test_workspace_snapshot.py`,
  `tests/test_security.py`, `tests/test_safe_subprocess.py`
- `.superpowers/sdd/a1-task-8-report.md`

## Self-check

- Every sensitive direct-path/content rejection occurs before runner,
  approval, Tool Change start, checkpoint/blob write, or workspace mutation.
- Outside paths retain the prior `path escapes workspace` family; benign
  symlinks are rejected as invalid arguments, while classified sensitive
  paths/content receive only the two new stable codes.
- rg never rescans PATH or accepts preprocessing/config arguments. Its actual
  trusted-binary NUL output, a fake mixed sensitive/safe stream, return codes,
  option-shaped patterns, and malformed records are covered.
- Python search never stats or reads a sensitive candidate and never follows a
  leaf symlink. Both lanes show normal source and allowed env templates while
  hiding `.env`, credential basenames, and sensitive extensions/components.
- Direct BlockStore, `memory_save`, and REPL `/save` reject concrete and
  configured opaque values; complete existing+new content is checked before
  replacement, and symlink targets remain byte-untouched.
- Snapshot eligibility rejects `.env` before root/path resolution, rejects
  an in-workspace symlink, reads a safe candidate exactly once with
  `max_blob_size + 1`, detects a configured secret after byte 4096, preserves
  the root-directory decision, and creates no artifact.
- Workspace snapshot calls `hash_file_bytes()` only for safe no-follow files.
  A safe-named known-secret file is rejected before the path-snapshot hash.
- Git HEAD receives no sensitive path; safe paths whose stdout contains a
  configured secret make one hardened Git call and zero blob calls.
- Runner content is redacted before clipping can split a known opaque value.
  The final Pico boundary returns a new safe object, preserves nested
  dict/list/tuple types, and updates only safe last-result metadata.

## Intentional Deferrals

- A1 Task 9 owns private/redacted CheckpointStore JSON and safe CLI artifact
  inspection.
- A1 Tasks 10–11 own pure exact shell assessment, one approval/execution gate,
  and removal of the public raw `Pico.tool_*` proxies. Those proxies remain
  unchanged here and are not claimed safe by Task 8.
- A1 Task 12 owns exact verification-evidence admission and the A1 integration
  canary.
- A2 Task 4 owns public `snapshot_bytes_eligibility()` and the atomic
  same-eligible-bytes-to-blob FileEntry rewrite. Task 8 adds only private,
  surgical exact-byte rescans at current blob writes; it does not introduce the
  A2 interface early.
