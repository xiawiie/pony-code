# A1 Task 7 Report: Non-Following Bootstrap and Index Readers

## Status

PASS — implementation and local verification complete on top of `dc6e8b6b2141875ebb373e197af6f4d798b6dac0`.

## Scope

- Added one shared lexical index predicate for in-root, non-sensitive, regular, no-symlink files plus a matching no-follow directory predicate for scan roots.
- Routed WorkspaceContext project docs, RepoMap, both BlockStore scopes, memory/context rendering, and WorkspaceObserver metadata reads through those predicates before `stat()` or `read_text()`.
- Global `~/.pico/AGENTS.md` remains the only allowed outside-workspace bootstrap file; its complete chain is checked and its full text is redacted before clipping.
- Replaced WorkspaceObserver and ToolExecutor HEAD fallback bare Git calls with `run_hardened_git()` and a frozen absolute Git path. Missing Git now selects the filesystem observer or leaves HEAD state unavailable without executing a bare name.
- Replaced runtime `shutil.which("rg")` and bare rg execution with `ToolContext.trusted_executables` plus `run_hardened_rg()`. Search uses `-e <pattern> -- <path>`, so option-shaped user patterns stay data.
- Froze the startup executable map on Pico and propagated it through prefix refresh, WorkspaceObserver, ToolContext, and delegate construction without rescanning inherited PATH.
- Preserved existing relative RepoMap/BlockStore root behavior with lexical `abspath()` normalization; no `resolve()` is used where it would erase symlink evidence.
- Added no dependency, public class, Task 8 direct-tool/snapshot content policy, or Task 11 shell execution policy. The controller-owned `.superpowers/sdd/progress.md` remains excluded.

## RED

Primary bootstrap/index and observer slice before implementation:

```text
uv run pytest tests/test_bootstrap_read_safety.py tests/test_context_sources.py tests/memory/test_repo_map.py tests/memory/test_block_store.py tests/memory/test_refresher.py tests/test_workspace_observer.py -q
22 failed, 43 passed in 0.76s
```

The failures proved that project/global docs, RepoMap, and both BlockStore scopes followed symlinks; stale RepoMap symbols survived as outside symbols; sensitive cached entries rendered into context; and WorkspaceObserver had no frozen hardened-Git interface.

Frozen rg/Git and runtime propagation slice before implementation:

```text
uv run pytest tests/test_tools.py tests/test_tool_executor.py tests/test_safety_invariants.py -q
7 failed, 62 passed in 2.45s
```

The failures proved ToolContext had no executable map, search still rescanned PATH, HEAD fallback executed bare Git, prefix refresh rediscovered executables, and delegates did not retain a controller-owned frozen map.

Two focused self-review regressions were also observed before their fixes:

```text
uv run pytest tests/test_workspace_observer.py::test_workspace_observer_rejects_symlinked_root -q
1 failed in 0.06s

uv run pytest tests/memory/test_repo_map.py::test_relative_repo_root_keeps_existing_scan_behavior tests/memory/test_block_store.py::test_relative_scope_roots_keep_existing_listing_behavior -q
2 failed in 0.10s
```

They proved `WorkspaceObserver.resolve()` erased a symlinked root and that the first lexical helper integration had accidentally doubled non-dot relative roots.

## GREEN and Verification

Required Task 7 gate:

```text
uv run pytest tests/test_bootstrap_read_safety.py tests/test_context_sources.py tests/memory/test_repo_map.py tests/memory/test_block_store.py tests/test_workspace_observer.py tests/test_tools.py tests/test_tool_executor.py tests/test_safe_subprocess.py tests/test_safety_invariants.py -q
151 passed in 3.09s
```

Adjacent runtime, prefix, delegate, Provider-loop, recovery, and global-AGENTS regressions:

```text
uv run pytest tests/test_pico.py tests/memory/test_runtime_wiring.py tests/test_prompt_prefix.py tests/test_agent_loop.py tests/test_runtime_report.py tests/test_context_manager.py tests/test_agent_loop_injection_sent.py tests/test_recovery_e2e.py tests/memory/test_global_agents_md.py -q
127 passed in 10.84s
```

Broader memory/config/CLI/context regressions:

```text
uv run pytest tests/memory tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_config_context.py tests/test_context_manager_v2.py tests/test_message_invariants.py -q
186 passed in 2.45s
```

Static checks:

```text
uv run ruff check .
All checks passed!

git diff --check
exit 0
```

Fresh final full gate after the relative-root repair:

```text
./scripts/check.sh
All checks passed!
1049 passed in 62.28s
```

No real Provider or live E2E call was made.

## Files

- `pico/workspace.py`, `pico/repo_map.py`, `pico/workspace_observer.py`
- `pico/memory/block_store.py`, `pico/memory/refresher.py`, `pico/context/sources.py`
- `pico/runtime.py`, `pico/tool_context.py`, `pico/tools.py`, `pico/tool_executor.py`
- `tests/test_bootstrap_read_safety.py`, `tests/test_context_sources.py`
- `tests/memory/test_repo_map.py`, `tests/memory/test_block_store.py`, `tests/memory/test_refresher.py`
- `tests/test_workspace_observer.py`, `tests/test_tools.py`, `tests/test_tool_executor.py`, `tests/test_safety_invariants.py`

## Self-check

- No Task 7-owned production path contains bare internal Git or runtime rg discovery.
- Workspace/global docs are checked before any existence/read operation; global and local docs are redacted before clipping.
- RepoMap filters before its first size/mtime/content read and removes stale entries when a regular file becomes a symlink or sensitive path.
- BlockStore validates workspace and user roots independently and rejects leaf, parent, in-root, and out-of-root memory symlinks during listing/read.
- WorkspaceObserver keeps lexically safe tracked-deletion markers while only statting safe regular files; a symlinked observer root yields an empty filesystem snapshot.
- Frozen executable values survive refresh and delegate construction even if PATH or the original WorkspaceContext mapping changes.
- Search with inherited malicious `RIPGREP_CONFIG_PATH` returns the normal match without executing its preprocessor; repository `core.fsmonitor` is disabled for bootstrap and observer calls.
- Missing frozen Git/rg produces zero bare runner calls.

## Remaining concerns

No known Task 7 defect remains. Sensitive direct-tool paths/content, Python fallback result filtering, snapshot eligibility/content scans, and BlockStore write-boundary secret rejection remain intentionally owned by A1 Task 8. Benchmark-only Git metadata collection outside Task 7 runtime/bootstrap paths is unchanged.
