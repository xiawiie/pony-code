# Pico Plan 5：构建、双平台与当前仓库最终收敛

状态：执行中

基线分支：`memory`

基线 HEAD：`8f024e87784bd1c9788bd40ce42913b73154a3cc`

设计真源：`docs/superpowers/specs/2026-07-11-pico-current-surface-hard-cut-design.md`

## 1. 前提与边界

Plan 1–4 已完成。Plan 4 最终本地全量为 `2012 passed, 6 skipped`，offline live assertions
为 `66 passed`，GitHub CI run `29166557573` 在 Ubuntu Python 3.11/3.12 均成功。

本计划只完成最终分发、macOS 安全门禁、八份当前维护者文档、历史资产硬删、精确仓库结构门禁和
最终证据。不得改变 Provider、持久化格式、Memory 模型或核心协调器语义；不得增加运行时依赖。

真实 Provider 请求必须等全部本地与 CI 门禁通过后获得新的明确授权，不能沿用此前授权。

以下七个未跟踪路径属于用户，禁止移动、删除、修改或 stage：

```text
.superpowers/brainstorm/
docs/superpowers/plans/2026-07-09-pico-action-kernel-model-connection.md
docs/superpowers/specs/2026-07-06-pico-full-review-design.md
docs/superpowers/specs/2026-07-08-pico-action-kernel-provider-parity-design.md
findings.md
progress.md
task_plan.md
```

## 2. Task 1：冻结构建与归档边界

提交：`build: define minimal distribution surface`。

- `pyproject.toml` 补充准确 description、`README.md` 元数据和显式 package/archive 选择；唯一 console
  entry 保持 `pico = "pico.cli:main"`，运行时依赖继续为零。
- sdist 只允许 `pyproject.toml`、`README.md`、`pico/**` 与 backend metadata；wheel 只允许
  `pico/**` 与 `.dist-info`。
- 增加可复现 archive inspection 与 clean-venv wheel smoke：环境内 `command -v pico`、
  `pico --help`、`pico doctor --offline` 必须通过。
- CI 在冻结的 uv/lock 环境中执行相同 build smoke。

## 3. Task 2：加入 macOS 安全门禁并消除 fork 警告

提交：`ci: add macos security durability gate`。

- FIFO 参数化测试改用 `multiprocessing.get_context("spawn")`，只在平台缺少真实 FIFO 能力时跳过；
  不过滤、不压制 warning。
- 新增 Python 3.12 `macos-latest` focused job，覆盖 project env security、文件锁与私有路径、artifact
  security、安全 subprocess/shell corpus、recovery durability、Memory reader 与 append lock。
- Ubuntu Python 3.11/3.12 的 lint、全量 pytest 与 offline harness 保持不变。

## 4. Task 3：重写八份当前维护者文档

提交：`docs: define current maintainer surface`。

最终 tracked 维护者 Markdown 精确为：

```text
README.md
CONTEXT.md
docs/cli-installation-and-updates.md
docs/architecture.md
docs/security.md
docs/recovery.md
docs/verification.md
docs/memory.md
```

两份测试输入 Markdown 保留但不计入文档面：

```text
benchmarks/live_e2e/fixtures/seed_cache_note.md
tests/fixtures/bench_repo_readme/README.md
```

文档分别负责产品定位与起步、领域语言与模块边界、安装更新、架构、安全、恢复、验证和 Memory；
交叉链接而不复制。验证文档只记录可复现命令、平台边界和真实 Provider 的授权边界，不写 key 或
live JSON。

`.gitignore` 显式允许八份文档，`CONTEXT.md` 不再被忽略，其余本地 docs 草稿默认忽略。

## 5. Task 4：删除历史资产并锁定精确仓库结构

提交：`refactor(repo): hard cut historical repository surface`。

- 删除所有其他 tracked Markdown，包括 `docs/superpowers/**`、`.superpowers/sdd/**`、review pack、
  旧架构/Memory 文档、ADR 和 benchmark README。
- 删除 `benchmarks/results/**` 及其 `DATA_PROVENANCE.md`，不建立 archive；Git 历史是唯一档案。
- 新增 `tests/test_repository_structure.py`，以 `git ls-files` 校验精确 Markdown manifest、禁止历史
  目录和旧 surface，并避免扫描器命中测试自身的 manifest/token 常量。
- 确认七个 protected untracked 路径仍逐项原样存在且未 stage。

## 6. Task 5：最终本地、CI 与独立审查

最终 HEAD 依次通过：

```bash
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
```

此外执行 archive root/METADATA inspection、隔离 venv wheel install/CLI smoke、Memory fake benchmark、
适用的本地 performance/quality harness、精确 tracked-doc/link/path/command 扫描，以及 C901 ratchet。
不得把临时 benchmark/live JSON 提交。

推送最终 HEAD 后等待 Ubuntu Python 3.11/3.12 与 macOS Python 3.12 全部成功；再由独立 reviewer
按 master spec 建立证据矩阵并检查 C0/I0/M0。

全部离线门禁和审查通过后，单独请求用户对一次最终 HEAD 的单一真实 Provider E2E 新授权。只有在
授权后真实 E2E 成功并完成 secret/artifact/fixture restoration 审查，或用户明确选择不运行该门禁时，
本目标才可结束。

## 7. 回滚边界

每个 Task 独立提交。构建、CI、文档/删除和结构门禁分别可回滚，不修改 Plan 1–4 已迁移的私有数据，
也不删除其 backup/journal。任何失败只回滚当前 Task 引入的 tracked 改动，不处理用户未跟踪文件。
