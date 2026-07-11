# Pico Plan 1 Safety and Reproducibility Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 Memory review 越界读取与 Agent Notes 并发丢写，冻结当前本地数据 manifest，统一 exact-root `.env` 来源诊断，并让 `memory` 分支使用可复现的 frozen CI。

**Architecture:** 复用 `BlockStore`、`locked_file`、private file primitives 和现有 CLI collectors，不新增安全 helper、配置框架或迁移框架。真实 `.pico` 只在 preflight 中收紧一个 lock mode 并生成 repo 外私有 manifest；业务提交按 Memory read、Memory append、Project Environment provenance、CI/lock 四个独立审查单元推进。

**Tech Stack:** Python 3.11+ stdlib、pytest、Ruff、uv 0.11.26、GitHub Actions、POSIX `fcntl`/file modes。

## Global Constraints

- 权威设计：`docs/superpowers/specs/2026-07-11-pico-current-surface-hard-cut-design.md`。
- 分支必须是 `memory`；`586802c` 必须是当前 HEAD 的祖先。
- 源码仍以 `5f359bd18fb3a59968167bfe0196352d41a23a01` 为代码基线；进入任务前不得已有 tracked code change。
- 本计划不修改 Provider、Context、CLI 主入口、TOML、持久化格式、`prompt_cache`、per-topic writer 或核心协调器。
- 本计划不执行 `PICO_RIGHT_CODES_API_KEY` → `PICO_API_KEY`；该 rename 属于 Plan 2。
- 本计划不删除 `pico-cli`、bare prompt、`LayeredMemory`、`write_agent_topic` 或 `stat_all`。
- 不增加运行时或开发依赖；`pyproject.toml` 不变。
- 不调用真实 Provider API；只运行 offline live assertions。
- 所有安全错误必须 content-free，不输出 canary、secret value 或工作区外真实路径。
- 所有手工文件修改使用 `apply_patch`；唯一例外是 `uv.lock` 必须由固定版本 uv 生成，不得用
  shell 或手工编辑重写仓库文件。
- 每个任务只 stage 本任务 allowlist；现有 ignored/untracked 用户文件不移动、不删除、不 stage。
- 受保护的当前 untracked 集合：
  - `.superpowers/brainstorm/`
  - `docs/superpowers/plans/2026-07-09-pico-action-kernel-model-connection.md`
  - `docs/superpowers/specs/2026-07-06-pico-full-review-design.md`
  - `docs/superpowers/specs/2026-07-08-pico-action-kernel-provider-parity-design.md`
  - `findings.md`
  - `progress.md`
  - `task_plan.md`

---

## File Responsibility Map

| 文件 | 本计划职责 |
| --- | --- |
| `pico/cli_memory.py` | `memory review` 只通过 `BlockStore` 读取；删除两套 Memory migration CLI |
| `pico/memory/block_store.py` | Agent-owned read 保留 unsafe/missing 区分；Agent Notes read-modify-write 使用跨进程锁 |
| `pico/cli_commands.py` | 删除 Memory migration help；为 `init` 输出 Project Environment provenance |
| `pico/config.py` | 在现有 parser 上返回 values + exact-root provenance，不改变 Provider 解析 |
| `pico/cli_diagnostics.py` | `config show`、`doctor`、`config set-secret` 输出同一 provenance |
| `tests/memory/test_cli_memory_commands.py` | Memory review canary 与 migration 命令消失合同 |
| `tests/memory/test_block_store.py` | Agent Notes unsafe read 与跨进程 append lock |
| `tests/memory/test_migration.py` | 只暂存仍需到 Plan 3 的 session migration 合同 |
| `tests/test_cli_memory_migrate.py` | 整文件删除 |
| `tests/test_project_env_security.py` | `read_project_env_with_status` 的 loaded/missing/review_required 合同 |
| `tests/test_cli_diagnostics.py` | config/doctor provenance、redaction、linked-worktree isolation |
| `tests/test_cli_commands.py` | init/set-secret provenance 与路径 redaction |
| `.gitignore` | 跟踪 `uv.lock` |
| `uv.lock` | uv 0.11.26 生成的冻结依赖图 |
| `.github/workflows/ci.yml` | `main`/`memory` push；frozen sync |
| `tests/test_scripts.py` | CI/lock 的精确仓库合同 |

## Commit Allowlist

| Commit | 唯一允许 stage 的路径 |
| --- | --- |
| Task 1 | `pico/cli_memory.py`、`pico/memory/block_store.py`、`pico/cli_commands.py`、`tests/memory/test_cli_memory_commands.py`、`tests/memory/test_block_store.py`、`tests/memory/test_migration.py`、`tests/test_cli_memory_migrate.py` |
| Task 2 | `pico/memory/block_store.py`、`tests/memory/test_block_store.py` |
| Task 3 | `pico/config.py`、`pico/cli_diagnostics.py`、`pico/cli_commands.py`、`tests/test_project_env_security.py`、`tests/test_cli_diagnostics.py`、`tests/test_cli_commands.py` |
| Task 4 | `.gitignore`、`uv.lock`、`.github/workflows/ci.yml`、`tests/test_scripts.py` |

## Execution Preflight — 必须先完成，不产生代码提交

- [ ] **Step 0.1: 验证分支、设计祖先和 tracked code 基线**

Run:

```bash
test "$(git branch --show-current)" = "memory"
git merge-base --is-ancestor 586802c HEAD
test -z "$(git status --porcelain --untracked-files=no)"
git diff --quiet 5f359bd18fb3a59968167bfe0196352d41a23a01 HEAD -- \
  pico tests benchmarks scripts examples pyproject.toml .github .gitignore
git status --short
```

Expected:

- 所有命令 exit 0；
- `git status --short` 只列 Global Constraints 中七个 `??` 路径；
- 若出现任何 tracked change 或额外 untracked path，停止，不自动清理。

- [ ] **Step 0.2: 关闭正在使用当前 repo `.pico` 的 Pico 进程**

Run:

```bash
pgrep -fal '(^|/)(pico|python)( |$)' || true
```

Expected: 人工核对输出中没有以 `/Users/wei/Desktop/pico` 为 cwd 或参数、并正在写
`.pico` 的进程。无法确认时停止；不得 kill 不相关进程。

- [ ] **Step 0.3: 用现有安全 primitives 收紧 checkpoint lock 并生成私有 manifest**

Run from `/Users/wei/Desktop/pico`:

```bash
uv run python - <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from pico.file_lock import locked_file
from pico.security import (
    ensure_private_dir,
    private_directory_identity,
    read_private_bytes,
    write_private_bytes_atomic,
)

root = Path.cwd().resolve()
pico_root = root / ".pico"
user_memory_root = Path.home() / ".pico" / "memory"
try:
    private_directory_identity(user_memory_root)
except FileNotFoundError:
    user_memory_entries = []
else:
    with os.scandir(user_memory_root) as scanned:
        user_memory_entries = [entry.name for entry in scanned]
if user_memory_entries:
    raise SystemExit("unexpected user memory data")

# Validate before any private reader can repair a mode. The spec authorizes
# only the existing checkpoint lock to move from 0644 to 0600.
checkpoint_root = pico_root / "checkpoints"
checkpoint_lock = checkpoint_root / ".checkpoint_store.lock"
pico_root_info = pico_root.lstat()
checkpoint_root_info = checkpoint_root.lstat()
checkpoint_lock_info = checkpoint_lock.lstat()
if not stat.S_ISDIR(pico_root_info.st_mode):
    raise SystemExit("unsafe .pico root")
if (
    not stat.S_ISDIR(checkpoint_root_info.st_mode)
    or os.name == "posix"
    and stat.S_IMODE(checkpoint_root_info.st_mode) != 0o700
):
    raise SystemExit("unsafe checkpoint root")
if (
    not stat.S_ISREG(checkpoint_lock_info.st_mode)
    or checkpoint_lock_info.st_nlink != 1
    or os.name == "posix"
    and stat.S_IMODE(checkpoint_lock_info.st_mode) not in {0o600, 0o644}
):
    raise SystemExit("unsafe checkpoint lock")
checkpoint_lock_mode_before = stat.S_IMODE(checkpoint_lock_info.st_mode)

for path in sorted(pico_root.rglob("*")):
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        continue
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit(f"unsafe .pico entry: {path.relative_to(pico_root)}")
    if (
        os.name == "posix"
        and path != checkpoint_lock
        and stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SystemExit(f"unexpected .pico mode: {path.relative_to(pico_root)}")

with locked_file(checkpoint_lock, require_lock=True):
    pass

pico_identity = private_directory_identity(pico_root)

entries = []
for path in sorted(pico_root.rglob("*")):
    before = path.lstat()
    if stat.S_ISDIR(before.st_mode):
        continue
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SystemExit(f"unsafe .pico entry: {path.relative_to(pico_root)}")
    data = read_private_bytes(
        path,
        trusted_root=pico_root,
        trusted_root_identity=pico_identity,
    )
    after = path.lstat()
    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or after.st_nlink != 1
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        raise SystemExit(f"unstable .pico entry: {path.relative_to(pico_root)}")

    relative = path.relative_to(pico_root)
    parts = relative.parts
    transform = (
        len(parts) == 2
        and parts[0] == "sessions"
        and path.suffix == ".json"
    ) or (
        len(parts) == 3
        and parts[0] == "checkpoints"
        and parts[1] in {"records", "tool_changes"}
        and path.suffix == ".json"
    )
    entries.append(
        {
            "path": relative.as_posix(),
            "role": "transform" if transform else "verify_only",
            "device": after.st_dev,
            "inode": after.st_ino,
            "nlink": after.st_nlink,
            "mode": stat.S_IMODE(after.st_mode),
            "mtime_ns": after.st_mtime_ns,
            "size": after.st_size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )

summary = {
    "total": len(entries),
    "transform": sum(item["role"] == "transform" for item in entries),
    "verify_only": sum(item["role"] == "verify_only" for item in entries),
    "sessions": sum(item["path"].startswith("sessions/") for item in entries),
    "runs": sum(item["path"].startswith("runs/") for item in entries),
    "checkpoints": sum(item["path"].startswith("checkpoints/") for item in entries),
    "memory": sum(item["path"].startswith("memory/") for item in entries),
    "user_memory": len(user_memory_entries),
}
expected = {
    "total": 46,
    "transform": 8,
    "verify_only": 38,
    "sessions": 5,
    "runs": 36,
    "checkpoints": 5,
    "memory": 0,
    "user_memory": 0,
}
if summary != expected:
    raise SystemExit(f"unexpected .pico manifest summary: {summary}")

repo_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
manifest_root = ensure_private_dir(
    Path.home() / ".pico" / "backups" / repo_hash / f"preflight-{stamp}"
)
manifest_identity = private_directory_identity(manifest_root)
manifest_path = manifest_root / "manifest.json"
payload = {
    "record_type": "current_surface_preflight",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "git_head": subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
    ).strip(),
    "repo_hash": repo_hash,
    "checkpoint_lock_mode_before": f"{checkpoint_lock_mode_before:04o}",
    "summary": summary,
    "entries": entries,
}
write_private_bytes_atomic(
    manifest_path,
    (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    trusted_root=manifest_root,
    trusted_root_identity=manifest_identity,
)
print(json.dumps({
    "manifest_path": str(manifest_path),
    "checkpoint_lock_mode_before": f"{checkpoint_lock_mode_before:04o}",
    "summary": summary,
}))
PY
```

Expected: exit 0 and one JSON line whose `summary` object is exactly:

```json
{
  "summary": {
    "total": 46,
    "transform": 8,
    "verify_only": 38,
    "sessions": 5,
    "runs": 36,
    "checkpoints": 5,
    "memory": 0,
    "user_memory": 0
  }
}
```

The actual output also includes the private `manifest_path` and `checkpoint_lock_mode_before` (`0644` on
the approved baseline; `0600` is accepted only when resuming an already completed preflight). Record the
path in the execution log as `PICO_PLAN1_MANIFEST`. Do not print manifest contents; they contain metadata
and hashes, never file bytes.
If the summary differs, stop and revise the spec before Task 1.

---

### Task 1: Close Memory Review Escape and Delete Obsolete Migration CLI

**Files:**

- Modify: `pico/cli_memory.py:1-340`
- Modify: `pico/memory/block_store.py:278-295`
- Modify: `pico/cli_commands.py:5-62`
- Modify: `tests/memory/test_cli_memory_commands.py:1-177`
- Modify: `tests/memory/test_block_store.py:340-374`
- Modify: `tests/memory/test_migration.py:1-101`
- Delete: `tests/test_cli_memory_migrate.py`

**Interfaces:**

- Consumes: `BlockStore.read(rel_path: str) -> str` and existing `CliError` rendering.
- Produces: `memory review` reads only `workspace/agent_notes.md` through `BlockStore`; unsafe agent-owned leaves raise a content-free error; `memory migrate` is not a command.

- [ ] **Step 1.1: Add failing unsafe-review and removed-command tests**

At the top of `tests/memory/test_cli_memory_commands.py`, import pytest and change the module contract to
`list/show/search/review` only:

```python
"""CLI memory list/show/search/review command tests."""

from types import SimpleNamespace

import pytest
```

Delete `test_memory_migrate_preview`, `test_memory_migrate_apply` and
`test_memory_migrate_no_legacy`. Add:

```python
@pytest.mark.parametrize("output_format", ("text", "json"))
def test_memory_review_rejects_symlink_without_reading_canary(
    tmp_path,
    capsys,
    output_format,
):
    from pico.cli import main
    from pico.cli_errors import CLI_EXIT_CONFIG

    canary = "memory-review-outside-canary"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-agent-notes"
    outside.write_text(canary, encoding="utf-8")
    memory_root = tmp_path / ".pico" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "agent_notes.md").symlink_to(outside)

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        output_format,
        "memory",
        "review",
    ])

    captured = capsys.readouterr()
    assert code == CLI_EXIT_CONFIG
    assert "agent notes could not be read safely" in captured.out + captured.err
    assert canary not in captured.out + captured.err
    assert str(outside) not in captured.out + captured.err
    if output_format == "json":
        import json

        payload = json.loads(captured.out)
        assert payload["error"]["code"] == "memory_unavailable"


@pytest.mark.parametrize("tokens", (["migrate"], ["migrate", "--apply"]))
def test_memory_migrate_is_not_a_command(tmp_path, tokens):
    from pico.cli_commands import handle_memory
    from pico.cli_errors import CliError

    with pytest.raises(CliError) as raised:
        handle_memory(tokens, str(tmp_path), _args(tmp_path))

    assert raised.value.code == "usage"
    assert "migrate" not in raised.value.message
```

In `tests/memory/test_block_store.py`, import `os` and add the agent-owned leaf regressions:

```python
@pytest.mark.parametrize(
    "unsafe_kind",
    ("symlink", "hardlink", "directory", "fifo"),
)
def test_read_rejects_unsafe_agent_notes_leaf(tmp_path, unsafe_kind):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    outside = tmp_path / "outside-agent-notes.md"
    outside.write_text("outside-canary", encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user)
    agent_notes = workspace / "agent_notes.md"
    if unsafe_kind == "symlink":
        agent_notes.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, agent_notes)
    elif unsafe_kind == "directory":
        agent_notes.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(agent_notes)

    with pytest.raises(ValueError, match="symlink|private|regular"):
        store.read("workspace/agent_notes.md")

    assert outside.read_text(encoding="utf-8") == "outside-canary"
```

Run:

```bash
uv run pytest \
  tests/memory/test_cli_memory_commands.py::test_memory_review_rejects_symlink_without_reading_canary \
  tests/memory/test_cli_memory_commands.py::test_memory_migrate_is_not_a_command \
  tests/memory/test_block_store.py::test_read_rejects_unsafe_agent_notes_leaf \
  -q
```

Expected: FAIL. Current review prints the canary, current `migrate` dispatches, and agent-owned
`BlockStore.read` masks the unsafe leaf as missing.

- [ ] **Step 1.2: Make agent-owned reads preserve unsafe-vs-missing**

Replace `BlockStore.read` with:

```python
def read(self, rel_path: str) -> str:
    target = self._resolve(rel_path)
    agent_owned = _is_agent_owned_path(rel_path)
    if not agent_owned:
        root = (
            self.workspace_root
            if rel_path.startswith("workspace/")
            else self.user_root
        )
        target = _safe_index_file(root, target)
        if target is None:
            raise FileNotFoundError(rel_path)
    data, _ = _read_bounded_regular(
        target,
        MAX_MEMORY_FILE_BYTES,
        private=agent_owned,
    )
    return data.decode("utf-8", errors="replace")
```

This reuses the existing private no-follow reader for agent-owned files. Do not add another path validator.

- [ ] **Step 1.3: Route review through BlockStore and delete both migrations**

In `pico/cli_memory.py`:

1. import `CLI_EXIT_CONFIG` with `CLI_EXIT_USAGE`;
2. remove unused `shutil` and `time` imports;
3. change the review dispatch to:

```python
if sub == "review":
    return _memory_review_cmd(store, rest, args)
```

4. replace the command docstring/usage with only `list | show | search | review`;
5. replace `_memory_review_cmd` with:

```python
def _memory_review_cmd(store, rest, args):
    if rest:
        raise CliError(
            code="usage",
            message="usage: pico-cli memory review",
            exit_code=CLI_EXIT_USAGE,
        )
    try:
        content = store.read("workspace/agent_notes.md")
    except FileNotFoundError:
        return print_result(
            "memory_review",
            {"exists": False},
            args,
            lambda _: "(no agent_notes.md yet — empty)",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="memory_unavailable",
            message="agent notes could not be read safely",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc

    data = {"exists": True, "chars": len(content), "content": content}

    def render(item):
        return (
            f"agent_notes.md ({item['chars']} chars):\n\n"
            f"{item['content']}\n"
            "To edit: vim .pico/memory/agent_notes.md\n"
            "To clear: rm .pico/memory/agent_notes.md"
        )

    return print_result("memory_review", data, args, render)
```

6. delete `_memory_migrate_cmd` and `cli_memory_migrate` in full.

In `pico/cli_commands.py` change the Memory help line to:

```text
  memory       Inspect and search memory files
```

- [ ] **Step 1.4: Delete only obsolete migration tests**

- Delete `tests/test_cli_memory_migrate.py`.
- In `tests/memory/test_migration.py` delete the three topics-to-notes tests, their
  `SimpleNamespace` helper/import, and retain only
  `test_legacy_session_memory_normalizes_to_v2_shape` with this module docstring:

```python
"""Session memory migration coverage retained until the Plan 3 hard cut."""
```

Do not delete that session migration test in Plan 1.

- [ ] **Step 1.5: Run focused tests**

Run:

```bash
uv run pytest \
  tests/memory/test_cli_memory_commands.py \
  tests/memory/test_block_store.py \
  tests/memory/test_migration.py \
  tests/memory/test_reader_bounds.py \
  tests/test_artifact_security.py \
  tests/test_security.py \
  -q
uv run ruff check \
  pico/cli_memory.py \
  pico/memory/block_store.py \
  pico/cli_commands.py \
  tests/memory/test_cli_memory_commands.py \
  tests/memory/test_block_store.py \
  tests/memory/test_migration.py
git diff --check
```

Expected: all selected tests PASS; Ruff and diff check exit 0; `rg -n 'cli_memory_migrate|_memory_migrate_cmd' pico tests` returns no matches.

- [ ] **Step 1.6: Commit**

Run:

```bash
git status --short
git add -- \
  pico/cli_memory.py \
  pico/memory/block_store.py \
  pico/cli_commands.py \
  tests/memory/test_cli_memory_commands.py \
  tests/memory/test_block_store.py \
  tests/memory/test_migration.py \
  tests/test_cli_memory_migrate.py
git diff --cached --check
git diff --cached --name-only
git commit -m "fix(memory): close review boundary and remove migrations"
```

Expected: cached names are a subset of Task 1 allowlist; commit succeeds.

---

### Task 2: Serialize Agent Notes Append Across Processes

**Files:**

- Modify: `pico/memory/block_store.py:312-347`
- Modify: `tests/memory/test_block_store.py:1-330`

**Interfaces:**

- Consumes: `locked_file(path, require_lock=True)`.
- Produces: `append_agent_note(scope, note) -> int` performs its entire read-modify-write while holding
  `workspace_root/.agent_notes.lock` or `user_root/.agent_notes.lock`.

- [ ] **Step 2.1: Write the failing cross-process lock test**

Add imports:

```python
import multiprocessing
```

Add this module-level spawn worker to `tests/memory/test_block_store.py`:

```python
def _append_agent_note_process(workspace, user, note, started, finished):
    store = BlockStore(workspace_root=workspace, user_root=user)
    started.set()
    store.append_agent_note(scope="workspace", note=note)
    finished.set()
```

Add the test:

```python
def test_append_agent_note_waits_for_cross_process_scope_lock(tmp_path):
    from pico import file_lock

    if file_lock.fcntl is None:
        pytest.skip("cross-process file locks unavailable")

    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    context = multiprocessing.get_context("spawn")
    notes = ("child-note-one", "child-note-two")
    started = [context.Event() for _ in notes]
    finished = [context.Event() for _ in notes]
    processes = [
        context.Process(
            target=_append_agent_note_process,
            args=(workspace, user, note, started[index], finished[index]),
        )
        for index, note in enumerate(notes)
    ]

    try:
        with file_lock.locked_file(
            workspace / ".agent_notes.lock",
            require_lock=True,
        ):
            for process in processes:
                process.start()
            assert all(event.wait(timeout=5) for event in started)
            assert not any(event.wait(timeout=0.25) for event in finished)

        assert all(event.wait(timeout=5) for event in finished)
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        lines = (workspace / "agent_notes.md").read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(lines) == 2
        assert all(sum(note in line for line in lines) == 1 for note in notes)
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
```

Run:

```bash
uv run pytest \
  tests/memory/test_block_store.py::test_append_agent_note_waits_for_cross_process_scope_lock \
  -q
```

Expected: FAIL because current append ignores `.agent_notes.lock` and finishes while the parent owns it.

- [ ] **Step 2.2: Hold the existing lock over the complete mutation**

Import:

```python
from pico.file_lock import locked_file
```

Keep note/scope/secret validation before lock acquisition. Replace the mutation body beginning at
`target = self._agent_notes_path(scope)` with:

```python
target = self._agent_notes_path(scope)
ensure_private_dir(target.parent)
lock_path = target.parent / ".agent_notes.lock"

with locked_file(lock_path, require_lock=True):
    target = require_regular_no_symlink(target, allow_missing=True)
    try:
        target.lstat()
    except FileNotFoundError:
        existing = ""
    else:
        existing = read_private_text(target)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_line = f"- {timestamp}  {note}\n"
    new_content = (
        existing + new_line
        if existing.endswith("\n") or not existing
        else existing + "\n" + new_line
    )
    self._reject_sensitive_content(new_content)
    self._atomic_write(target, new_content)
    size = len(new_content)
```

Leave the soft-limit warning and `return size` after the `with` block. Do not replace the file lock with a
thread-only mutex and do not use direct append without the existing atomic writer.

- [ ] **Step 2.3: Run append and security tests**

Run:

```bash
uv run pytest \
  tests/memory/test_block_store.py \
  tests/memory/test_invariants.py \
  tests/memory/test_memory_tools.py \
  tests/test_file_lock.py \
  tests/test_artifact_security.py \
  -q
uv run ruff check pico/memory/block_store.py tests/memory/test_block_store.py
git diff --check
```

Expected: all selected tests PASS; no deadlock; Ruff/diff check exit 0.

- [ ] **Step 2.4: Commit**

Run:

```bash
git add -- pico/memory/block_store.py tests/memory/test_block_store.py
git diff --cached --check
test "$(git diff --cached --name-only | wc -l | tr -d ' ')" = "2"
git commit -m "fix(memory): serialize agent note appends"
```

Expected: commit succeeds with exactly the two Task 2 files.

---

### Task 3: Expose One Exact-Root Project Environment Provenance

**Files:**

- Modify: `pico/config.py:96-136`
- Modify: `pico/cli_diagnostics.py:35-170,217-335,482-560,685-810`
- Modify: `pico/cli_commands.py:78-155`
- Modify: `tests/test_project_env_security.py:1-165`
- Modify: `tests/test_cli_diagnostics.py:1-460`
- Modify: `tests/test_cli_commands.py:150-410`

**Interfaces:**

- Produces: `project_env_metadata(workspace_root, status) -> dict[str, str]`.
- Produces: `read_project_env_with_status(start, warn=True) -> tuple[dict[str, str], dict[str, str]]`.
- Consumes later: Plan 2 shared Provider resolver uses the returned project values and metadata; no second
  parser is introduced.

- [ ] **Step 3.1: Add failing parser provenance tests**

Import `read_project_env_with_status` in `tests/test_project_env_security.py` and add:

```python
def test_project_env_status_distinguishes_missing_loaded_and_rejected_lines(
    tmp_path,
    capsys,
):
    values, metadata = read_project_env_with_status(tmp_path)
    assert values == {}
    assert metadata == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "missing",
    }

    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)
    values, metadata = read_project_env_with_status(tmp_path)
    assert values == {"PICO_PROVIDER": "deepseek"}
    assert metadata["status"] == "loaded"

    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\ninvalid project env line\n",
        encoding="utf-8",
    )
    values, metadata = read_project_env_with_status(tmp_path)
    captured = capsys.readouterr()
    assert values == {"PICO_PROVIDER": "deepseek"}
    assert metadata["status"] == "review_required"
    assert "invalid project env line" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_project_env_status_records_permission_repair(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    values, metadata = read_project_env_with_status(tmp_path, warn=False)

    assert values == {"PICO_PROVIDER": "deepseek"}
    assert metadata["status"] == "review_required"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
```

Run:

```bash
uv run pytest \
  tests/test_project_env_security.py::test_project_env_status_distinguishes_missing_loaded_and_rejected_lines \
  tests/test_project_env_security.py::test_project_env_status_records_permission_repair \
  -q
```

Expected: FAIL with import error because `read_project_env_with_status` does not exist.

- [ ] **Step 3.2: Implement values + provenance in the existing parser**

Import `stat` in `pico/config.py`. Add:

```python
def project_env_metadata(workspace_root, status):
    return {
        "path": str(project_env_path(workspace_root)),
        "scope": "repo_root_exact",
        "status": str(status),
    }


def read_project_env_with_status(start, warn=True):
    env_path = project_env_path(start)
    try:
        initial_mode = env_path.lstat().st_mode
        text = read_private_text(env_path)
    except FileNotFoundError:
        return {}, project_env_metadata(start, "missing")

    loaded = {}
    status = (
        "review_required"
        if os.name == "posix" and stat.S_IMODE(initial_mode) != 0o600
        else "loaded"
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            parsed = _parse_env_line(line)
        except ValueError as exc:
            status = "review_required"
            if warn:
                _warn_invalid_env_line(env_path, line_number, exc)
            continue
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
    return loaded, project_env_metadata(start, status)
```

Replace `read_project_env` with:

```python
def read_project_env(start, warn=True):
    loaded, _ = read_project_env_with_status(start, warn=warn)
    return loaded
```

Unsafe path/type/read errors must continue to propagate from `read_private_text`; only FileNotFound maps to
`missing`.

- [ ] **Step 3.3: Add failing CLI and linked-worktree tests**

In `tests/test_cli_diagnostics.py` import `Path`, `shutil`, `stat`, `subprocess` and
`collect_config`, then add:

```python
def _run_git(cwd, *args):
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_config_show_reports_exact_project_env_path(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["workspace"] == {"repo_root": str(tmp_path.resolve())}
    assert payload["project_env"] == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }


def test_doctor_reports_the_same_project_env_contract(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
        "--offline",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["project_env"] == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert payload["security"]["project_env"] == {
        "status": "loaded",
        "mode": "0600",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_config_show_preserves_permission_review_after_redactor(
    tmp_path,
    capsys,
):
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
    ]) == 0

    project_env = json.loads(capsys.readouterr().out)["data"]["project_env"]
    assert project_env["status"] == "review_required"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_config_isolates_main_and_linked_worktree_env(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")

    main_root = tmp_path / "main"
    linked_root = tmp_path / "linked"
    main_root.mkdir()
    _run_git(main_root, "init", "-q")
    _run_git(main_root, "config", "user.name", "Pico Test")
    _run_git(main_root, "config", "user.email", "pico@example.invalid")
    (main_root / "README.md").write_text("fixture\n", encoding="utf-8")
    _run_git(main_root, "add", "README.md")
    _run_git(main_root, "commit", "-qm", "fixture")
    _run_git(
        main_root,
        "worktree",
        "add",
        "-q",
        "-b",
        "linked",
        str(linked_root),
    )

    (main_root / ".env").write_text(
        "PICO_PROVIDER=openai\n",
        encoding="utf-8",
    )
    (main_root / ".env").chmod(0o600)
    (linked_root / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (linked_root / ".env").chmod(0o600)
    child = linked_root / "src"
    child.mkdir()
    monkeypatch.delenv("PICO_PROVIDER", raising=False)

    main_data = collect_config(main_root)
    linked_data = collect_config(child)

    assert main_data["provider"]["value"] == "openai"
    assert linked_data["provider"]["value"] == "deepseek"
    assert main_data["project_env"]["path"] == str(main_root / ".env")
    assert linked_data["project_env"]["path"] == str(linked_root / ".env")
    assert main_data["project_env"]["path"] != linked_data["project_env"]["path"]

    (linked_root / ".env").unlink()
    missing = collect_config(child)
    assert missing["project_env"]["status"] == "missing"
    assert missing["provider"]["value"] != "openai"
```

Also add unsafe-path redaction coverage for each approved link/type boundary:

```python
@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "directory"))
def test_config_show_marks_unsafe_project_env_for_review_without_canary(
    tmp_path,
    capsys,
    unsafe_kind,
):
    canary = "project-env-outside-canary"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    env_path = tmp_path / ".env"
    if unsafe_kind == "directory":
        env_path.mkdir()
        (env_path / "canary").write_text(canary, encoding="utf-8")
    else:
        outside.write_text(
            f"PICO_PROVIDER=deepseek\n{canary}\n",
            encoding="utf-8",
        )
        if unsafe_kind == "symlink":
            env_path.symlink_to(outside)
        else:
            os.link(outside, env_path)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
    ]) == 0

    captured = capsys.readouterr()
    metadata = json.loads(captured.out)["data"]["project_env"]
    assert metadata["scope"] == "repo_root_exact"
    assert metadata["status"] == "review_required"
    assert canary not in captured.out + captured.err
    assert str(outside) not in captured.out + captured.err
```

Run:

```bash
uv run pytest \
  tests/test_cli_diagnostics.py::test_config_show_reports_exact_project_env_path \
  tests/test_cli_diagnostics.py::test_doctor_reports_the_same_project_env_contract \
  tests/test_cli_diagnostics.py::test_config_show_preserves_permission_review_after_redactor \
  tests/test_cli_diagnostics.py::test_config_isolates_main_and_linked_worktree_env \
  tests/test_cli_diagnostics.py::test_config_show_marks_unsafe_project_env_for_review_without_canary \
  -q
```

Expected: FAIL because collectors do not expose `workspace/project_env` and unsafe `config show` is not folded.

- [ ] **Step 3.4: Wire one provenance object through config and doctor**

Import `project_env_metadata` and `read_project_env_with_status` in `pico/cli_diagnostics.py`. Replace
`_read_project_env` with:

```python
def _read_project_env_for_diagnostics(root):
    try:
        return read_project_env_with_status(root)
    except (OSError, RuntimeError, ValueError):
        return {}, project_env_metadata(root, "review_required")
```

Update `collect_config`:

```python
def collect_config(cwd, args=None):
    workspace = WorkspaceContext.build(cwd)
    project_env, project_env_info = _read_project_env_for_diagnostics(
        workspace.repo_root
    )
    provider = _resolve_provider(args, project_env)
    model = _resolve_model(args, provider["value"], project_env)
    api_key = _resolve_api_key(provider["value"], project_env)
    _resolve_base_url(args, provider["value"], project_env)
    return {
        "workspace": {"repo_root": workspace.repo_root},
        "project_env": project_env_info,
        "provider": provider,
        "model": model,
        "api_key": api_key,
    }
```

In `collect_doctor` use the same tuple and add `"project_env": project_env_info` to the top-level return.
Call `_collect_security_status(root, project_env_info, pico_root)` and apply this exact change at the
collector boundary; all subsequent private-storage, executable and recovery statements remain unchanged:

```diff
-def _collect_security_status(root, env_path, pico_root):
-    project_env = _project_env_security_status(env_path)
+def _collect_security_status(root, project_env_info, pico_root):
+    project_env = _project_env_security_status(
+        Path(project_env_info["path"]),
+        project_env_info["status"],
+    )
```

Implement
`_project_env_security_status` as follows so malformed content remains `review_required` after the mode
check:

```python
def _project_env_security_status(path, read_status):
    try:
        mode = Path(path).lstat().st_mode
    except FileNotFoundError:
        return {"status": "missing", "mode": ""}
    except OSError:
        return {"status": "review_required", "mode": ""}
    if not stat.S_ISREG(mode):
        return {"status": "review_required", "mode": ""}
    permission_mode = (
        f"{stat.S_IMODE(mode):04o}"
        if os.name == "posix"
        else ""
    )
    status = str(read_status)
    if permission_mode and permission_mode != "0600":
        status = "review_required"
    return {"status": status, "mode": permission_mode}
```

The only allowed statuses are `loaded`, `missing` and `review_required`. Update
`_unavailable_workspace_doctor` with:

```python
"project_env": {
    "path": "",
    "scope": "repo_root_exact",
    "status": "review_required",
},
```

In `handle_doctor` redact `data["project_env"]` with the same redactor. In `_render_doctor`, insert the
following block after the existing Workspace block and before Config:

```python
"Project environment",
_line("path", data["project_env"]["path"]),
_line("scope", data["project_env"]["scope"]),
_line("status", data["project_env"]["status"]),
"",
```

In `_render_config`, add the following before Provider:

```python
"Workspace",
_line("repo root", data["workspace"]["repo_root"]),
"",
"Project environment",
_line("path", data["project_env"]["path"]),
_line("scope", data["project_env"]["scope"]),
_line("status", data["project_env"]["status"]),
"",
```

Do not resolve Provider values in either renderer; Plan 2 owns the shared resolver.

`handle_config` must fold an unsafe `.env` before building the inspection redactor:

```python
if sub == "show" and not rest:
    data = collect_config(cwd, args)
    try:
        redactor = build_inspection_redactor(
            data["workspace"]["repo_root"],
            args,
        )
    except (OSError, RuntimeError, ValueError):
        redactor = securitylib.redact_artifact
    return print_result(
        "config_show",
        _redact_mapping_values(data, redactor),
        args,
        _render_config,
    )
```

This fallback must not read or render the unsafe target. Do not catch errors inside the normal project-env
reader used by runtime or `init`.

- [ ] **Step 3.5: Write failing init/set-secret provenance tests**

Replace `test_config_write_output_uses_canonical_env_path_without_workspace_path` in
`tests/test_cli_commands.py` with:

```python
def test_config_write_output_uses_canonical_project_env_metadata(
    tmp_path,
    monkeypatch,
    capsys,
):
    root = tmp_path / "repo"
    root.mkdir()

    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "init",
        "--provider",
        "deepseek",
    ]) == 0
    init_payload = json.loads(capsys.readouterr().out)["data"]
    assert init_payload["workspace"] == {
        "repo_root": str(root.resolve()),
    }
    assert init_payload["project_env"] == {
        "path": str(root.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert "env_path" not in init_payload

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("sk-output-test-secret-123456789\n"),
    )
    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ]) == 0
    secret_payload = json.loads(capsys.readouterr().out)["data"]
    assert secret_payload["workspace"] == {
        "repo_root": str(root.resolve()),
    }
    assert secret_payload["project_env"] == {
        "path": str(root.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert "env_path" not in secret_payload


def test_config_writes_redact_secret_shaped_workspace_in_project_env_path(
    tmp_path,
    monkeypatch,
    capsys,
):
    marker = "sk-workspace-path-123456789"
    root = tmp_path / marker
    root.mkdir()

    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "init",
        "--provider",
        "deepseek",
    ]) == 0

    init_output = capsys.readouterr().out
    init_data = json.loads(init_output)["data"]
    assert set(init_data["workspace"]) == {"repo_root"}
    assert init_data["project_env"]["scope"] == "repo_root_exact"
    assert init_data["project_env"]["status"] == "loaded"
    assert marker not in init_output

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("sk-output-test-secret-123456789\n"),
    )
    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ]) == 0
    secret_output = capsys.readouterr().out
    secret_data = json.loads(secret_output)["data"]
    assert set(secret_data["workspace"]) == {"repo_root"}
    assert secret_data["project_env"]["scope"] == "repo_root_exact"
    assert secret_data["project_env"]["status"] == "loaded"
    assert marker not in secret_output
    assert "sk-output-test-secret" not in secret_output


def test_config_writes_keep_review_required_for_preserved_invalid_line(
    tmp_path,
    monkeypatch,
    capsys,
):
    marker = "sk-" + "preserved-invalid-line-123456789"
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"PICO_PROVIDER=deepseek\n{marker}\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
        "--provider",
        "deepseek",
    ]) == 0
    init_capture = capsys.readouterr()
    init_data = json.loads(init_capture.out)["data"]
    assert init_data["project_env"]["status"] == "review_required"
    assert marker not in init_capture.out + init_capture.err

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("sk-output-test-secret-123456789\n"),
    )
    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ]) == 0
    secret_capture = capsys.readouterr()
    secret_data = json.loads(secret_capture.out)["data"]
    assert secret_data["project_env"]["status"] == "review_required"
    assert marker not in secret_capture.out + secret_capture.err
    assert "sk-output-test-secret" not in secret_capture.out + secret_capture.err
    assert marker in env_path.read_text(encoding="utf-8")
```

Run:

```bash
uv run pytest \
  tests/test_cli_commands.py::test_config_write_output_uses_canonical_project_env_metadata \
  tests/test_cli_commands.py::test_config_writes_redact_secret_shaped_workspace_in_project_env_path \
  tests/test_cli_commands.py::test_config_writes_keep_review_required_for_preserved_invalid_line \
  -q
```

Expected: FAIL because init/set-secret still expose only `env_path = ".env"`.

- [ ] **Step 3.6: Wire provenance through init and set-secret**

In `pico/cli_commands.py`, import `security as securitylib`, `project_env_metadata` and
`read_project_env_with_status`. After a successful `init` write, reread status without duplicating
warnings, fold a post-write race to `review_required`, then redact both source objects:

```python
try:
    _, project_env = read_project_env_with_status(root, warn=False)
except (OSError, RuntimeError, ValueError):
    project_env = project_env_metadata(root, "review_required")
try:
    redactor = build_inspection_redactor(root, args)
except (OSError, RuntimeError, ValueError):
    redactor = securitylib.redact_artifact
workspace_info = redactor({"repo_root": str(root)})
project_env = redactor(project_env)
```

Replace `"env_path": env_path.name` in init data with:

```python
"workspace": workspace_info,
"project_env": project_env,
```

Replace the old env-file render line with:

```python
"Workspace",
_line("repo root", data["workspace"]["repo_root"]),
"",
"Project environment",
_line("env file", data["project_env"]["path"]),
_line("env scope", data["project_env"]["scope"]),
_line("env status", data["project_env"]["status"]),
"",
```

Remove the now-unused local `env_path` and `project_env_path` import in `pico/cli_commands.py`.

In `pico/cli_diagnostics.py::_handle_set_secret`, build and redact both objects after the write:

```python
_, project_env = _read_project_env_for_diagnostics(root)
try:
    redactor = build_inspection_redactor(root, args)
except (OSError, RuntimeError, ValueError):
    redactor = securitylib.redact_artifact
workspace_info = redactor({"repo_root": str(root)})
project_env = redactor(project_env)
```

Replace `env_path` in JSON with `workspace` and `project_env`, and render the same Workspace and Project
environment blocks used by init before `permission`. Keep `permission` unchanged.
Also remove the old `read_project_env` import and import
`project_env_metadata/read_project_env_with_status` instead; Ruff must report no orphaned import.

- [ ] **Step 3.7: Update remaining exact assertions and run focused suites**

In `tests/test_cli_diagnostics.py`:

- replace safe `security["project_env"]["status"] == "ok"` with `"loaded"`;
- keep the nested security keys exactly `{"status", "mode"}`;
- add the top-level `project_env` object to unavailable-workspace exact assertions;
- assert malformed lines set top-level and nested status to `review_required`.
- extend `test_config_show_text_uses_grouped_cli_output_without_secret_value` to assert the rendered
  `Workspace` and `Project environment` sections contain the exact root path, `repo_root_exact` and
  `loaded`;
- extend `test_doctor_text_uses_grouped_cli_output` with the same Project environment path/scope/status
  assertions.

In `tests/test_cli_commands.py`, extend the existing text-mode init and set-secret tests to assert their
rendered Workspace and Project environment sections contain the exact root path, `repo_root_exact` and
`loaded`, while retaining their secret non-disclosure assertions.

Then run:

```bash
uv run pytest \
  tests/test_project_env_security.py \
  tests/test_cli_diagnostics.py \
  tests/test_cli_commands.py \
  tests/test_cli_error_envelope.py \
  tests/test_safety_invariants.py \
  -q
uv run ruff check \
  pico/config.py \
  pico/cli_diagnostics.py \
  pico/cli_commands.py \
  tests/test_project_env_security.py \
  tests/test_cli_diagnostics.py \
  tests/test_cli_commands.py
git diff --check
```

Expected: all selected tests PASS; linked worktree resolves its own exact `.env`; no canary/secret appears;
Ruff/diff check exit 0.

- [ ] **Step 3.8: Commit**

Run:

```bash
git add -- \
  pico/config.py \
  pico/cli_diagnostics.py \
  pico/cli_commands.py \
  tests/test_project_env_security.py \
  tests/test_cli_diagnostics.py \
  tests/test_cli_commands.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(config): expose exact project environment source"
```

Expected: cached names are exactly the six Task 3 files; commit succeeds.

---

### Task 4: Track the Lockfile and Freeze Memory-Branch CI

**Files:**

- Modify: `.gitignore:1-15`
- Create: `uv.lock`
- Modify: `.github/workflows/ci.yml:1-40`
- Modify: `tests/test_scripts.py:1-50`

**Interfaces:**

- Produces: repository lock generated by uv 0.11.26.
- Produces: CI runs on direct pushes to `main` and `memory` and installs with
  `uv sync --frozen --dev`.

- [ ] **Step 4.1: Write the failing repository contract**

Add to `tests/test_scripts.py`:

```python
def test_ci_tracks_and_uses_frozen_uv_lock():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert Path("uv.lock").is_file()
    assert "uv.lock" not in ignored
    assert 'version: "0.11.26"' in workflow
    assert "      - main" in workflow
    assert "      - memory" in workflow
    assert "run: uv sync --frozen --dev" in workflow
```

Run:

```bash
uv run pytest tests/test_scripts.py::test_ci_tracks_and_uses_frozen_uv_lock -q
```

Expected: FAIL because `uv.lock` is absent/ignored, `memory` is not a push branch and CI is not frozen.

- [ ] **Step 4.2: Generate the lock with the CI-pinned uv**

Remove only the `uv.lock` line from `.gitignore`. Then run:

```bash
uvx --from 'uv==0.11.26' uv --version
uvx --from 'uv==0.11.26' uv lock
uvx --from 'uv==0.11.26' uv lock --check
```

Expected:

- first command prints `uv 0.11.26`;
- `uv.lock` is created;
- lock check exits 0;
- `git diff -- pyproject.toml` is empty;
- no dependency version is manually edited.

- [ ] **Step 4.3: Freeze CI and include memory pushes**

Change the workflow trigger and install step to:

```yaml
on:
  pull_request:
  push:
    branches:
      - main
      - memory
```

```yaml
      - name: Install dependencies
        run: uv sync --frozen --dev
```

Do not add macOS or build jobs here; those belong to Plan 5.

- [ ] **Step 4.4: Run lock and CI structure checks**

Run:

```bash
uv lock --check
uv sync --frozen --dev
uv run pytest tests/test_scripts.py -q
uv run ruff check tests/test_scripts.py
git diff --check
git diff -- pyproject.toml
```

Expected: all commands exit 0; the final diff is empty.

- [ ] **Step 4.5: Commit**

Run:

```bash
git add -- .gitignore uv.lock .github/workflows/ci.yml tests/test_scripts.py
git diff --cached --check
test "$(git diff --cached --name-only | wc -l | tr -d ' ')" = "4"
git commit -m "ci: freeze dependencies on memory pushes"
```

Expected: commit succeeds with exactly the four Task 4 paths.

---

## Plan 1 Completion Gate

- [ ] **Gate 1: Exact obsolete-surface scan**

Run:

```bash
test -z "$(rg -n 'cli_memory_migrate|_memory_migrate_cmd' pico tests || true)"
test -z "$(rg -n 'memory \\{[^}]*migrate|memory.*inspection & migration' pico tests || true)"
test -f uv.lock
if git check-ignore -q uv.lock; then
  exit 1
fi
```

Expected: exit 0 and no obsolete Memory migration symbol/help matches.

- [ ] **Gate 2: Full local validation**

Run:

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
git diff --check
```

Expected:

- all commands exit 0;
- full pytest has zero failures;
- offline live assertions report `60 passed`;
- wheel and sdist build successfully;
- no real network/Provider call occurs.

- [ ] **Gate 3: Revalidate the private `.pico` manifest**

Set `PICO_PLAN1_MANIFEST` to the exact path printed in Step 0.3, then run:

```bash
export PICO_PLAN1_MANIFEST
test -n "$PICO_PLAN1_MANIFEST"
uv run python - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from pico.security import (
    private_directory_identity,
    read_private_bytes,
    read_private_text,
)

manifest_path = Path(os.environ["PICO_PLAN1_MANIFEST"]).resolve()
manifest_root = manifest_path.parent
manifest = json.loads(
    read_private_text(
        manifest_path,
        trusted_root=manifest_root,
        trusted_root_identity=private_directory_identity(manifest_root),
    )
)
expected_summary = {
    "total": 46,
    "transform": 8,
    "verify_only": 38,
    "sessions": 5,
    "runs": 36,
    "checkpoints": 5,
    "memory": 0,
    "user_memory": 0,
}
if (
    manifest.get("record_type") != "current_surface_preflight"
    or manifest.get("checkpoint_lock_mode_before") not in {"0600", "0644"}
    or manifest.get("summary") != expected_summary
    or len(manifest.get("entries", ())) != expected_summary["total"]
):
    raise SystemExit("unexpected Plan 1 manifest")
root = Path.cwd().resolve() / ".pico"
root_identity = private_directory_identity(root)
expected_paths = {item["path"] for item in manifest["entries"]}
current_paths = set()
user_memory_root = Path.home() / ".pico" / "memory"

try:
    private_directory_identity(user_memory_root)
except FileNotFoundError:
    pass
else:
    with os.scandir(user_memory_root) as scanned:
        if any(True for _ in scanned):
            raise SystemExit("unexpected user memory data")

for path in sorted(root.rglob("*")):
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        continue
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit(f"unsafe .pico entry: {path.relative_to(root)}")
    current_paths.add(path.relative_to(root).as_posix())

if current_paths != expected_paths:
    raise SystemExit(".pico manifest path set drift")

for expected in manifest["entries"]:
    path = root / expected["path"]
    data = read_private_bytes(
        path,
        trusted_root=root,
        trusted_root_identity=root_identity,
    )
    info = path.lstat()
    actual = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "nlink": info.st_nlink,
        "mode": stat.S_IMODE(info.st_mode),
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    for key, value in actual.items():
        if value != expected[key]:
            raise SystemExit(
                f".pico manifest drift: {expected['path']} field={key}"
            )

print(json.dumps({"verified": len(manifest["entries"]), "summary": manifest["summary"]}))
PY
```

Expected: `{"verified": 46, ...}` with transform 8 and verify-only 38. No path drift and no file bytes
are printed.

- [ ] **Gate 4: Verify staged/untracked boundary and commit sequence**

Run:

```bash
test -z "$(git status --porcelain --untracked-files=no)"
git status --short
git log -4 --oneline
```

Expected:

- no tracked changes;
- only the seven protected untracked paths remain;
- the latest four implementation commits correspond to Tasks 1–4 in order.

- [ ] **Gate 5: Push `memory` and verify real GitHub CI**

Run only after Gates 1–4 pass:

```bash
git push origin memory
head_sha="$(git rev-parse HEAD)"
run_id=""
for attempt in $(seq 1 30); do
  run_id="$(
    gh run list \
      --workflow CI \
      --branch memory \
      --commit "$head_sha" \
      --limit 1 \
      --json databaseId \
      --jq '.[0].databaseId'
  )"
  if test -n "$run_id"; then
    break
  fi
  sleep 2
done
test -n "$run_id"
gh run watch "$run_id" --exit-status
test "$(
  gh run view "$run_id" --json headSha --jq '.headSha'
)" = "$head_sha"
gh run view "$run_id" --json headSha,status,conclusion,jobs
```

Expected:

- push succeeds to `origin/memory`;
- CI run `headSha` equals local `head_sha`;
- Ubuntu Python 3.11 and 3.12 jobs conclude `success`;
- no macOS/build job is expected until Plan 5.

## Plan 1 Handoff Record

At completion, report:

1. four implementation commit SHAs;
2. private manifest path and summary only;
3. focused/full/offline/build results;
4. GitHub CI run ID and conclusion;
5. exact `git status --short` showing protected untracked files untouched;
6. any deviation from this plan. A deviation requires spec/plan review before Plan 2 is written.
