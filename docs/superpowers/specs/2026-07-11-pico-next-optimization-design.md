# Pico 下一阶段工程收敛与可维护性优化设计

- 日期：2026-07-11
- 状态：方案 A 已获用户批准；本文为实施前设计规范，待用户审阅后再进入 writing-plans
- 基线分支：`memory`
- 基线提交：`5f359bd18fb3a59968167bfe0196352d41a23a01`
- 前置成果：Action Kernel、Messages v3、安全与可信基线均已完成
- 实施原则：不新增产品功能，不发布，不扩大 Provider 或平台范围

## 1. 执行结论

Pico 当前已经不是“功能不完整”的原型。现有项目具备完整的本地 coding-agent
运行链、安全与恢复边界、确定性 benchmark、一次真实 DeepSeek E2E 证据，以及接近
两千条自动化测试。下一阶段最有价值的工作不是再增加能力，而是让这套能力更容易
被正确运行、更容易被持续验证，也更容易被后续维护。

本设计按以下顺序推进：

1. **P0：运行与证据收敛。** 让开发者明确看到 Pico 实际读取哪个 `.env`，让
   `memory` 分支的直接提交也触发 CI，并消除 Review Pack 中“已完成”与“仍 pending”
   同时存在的叙述冲突。
2. **P1：复现性与平台信心。** 提交 `uv.lock`、让 CI 使用 frozen lock，并增加一个
   聚焦 POSIX 文件安全语义的 macOS job。
3. **P2：删除无效与重复结构。** 删除没有生产消费者的旧 `LayeredMemory` 状态层，
   删除没有调用方的 `prompt_cache` feature flag，但保留真正工作的 Provider prompt
   cache 能力。
4. **P3：渐进式降低核心维护成本。** 在行为锁定后，分别缩短
   `ToolExecutor.execute()` 与 `AgentLoop.run()`；同步合并测试样板并整理历史设计文档。

这四步的共同目标是：**以后修改 Pico 时，开发者更快知道配置是否正确、CI 是否真的
覆盖当前分支、哪个状态结构才是真源、核心失败发生在哪一段，同时不牺牲已经获得的
安全保证。**

## 2. 当前项目基线

### 2.1 已完成的能力与验证

当前 `memory` 与 `origin/memory` 在本设计开始前均指向 `5f359bd`。本地主工作区通过
fast-forward 更新到该提交，原有未跟踪资料保持原样。

最近一次完整验证证据为：

- 本地全量门禁：`1997 passed, 6 skipped`，只有两条 Python fork deprecation warning。
- live harness 离线断言：`60 passed`。
- 最终真实 E2E：DeepSeek，模型 `qwen3.7-max`，`43/43` assertions。
- 真实 E2E 内包含 8 个 native actions、10/15 次 Provider calls。
- token 统计：13,842 input、1,330 output、5,248 cache-read。
- wall time：44.253 秒。
- API key、Provider payload、active artifact、private mode、fixture restoration、session v3、
  terminal artifact、call cap 与 token cap 检查均通过。
- 最终独立 review：C0 / I0 / M0。
- 三项 perf smoke 的有效样本计数为 3 / 3 / 4 / 4；当前不建立本机延迟硬阈值。

这些结果是下一阶段的回归基线，不是要求通过重跑来制造的新功能指标。

### 2.2 当前规模

在基线提交上：

- `pico/**/*.py`：23,624 行。
- `tests/**/*.py`：31,388 行。
- 已跟踪的 `docs/superpowers/specs` 与 `docs/superpowers/plans`：47,085 行。
- 运行时依赖：0；`pyproject.toml` 的 `dependencies = []`。
- 最大的生产文件包括：
  - `pico/recovery_manager.py`：1,500 行；
  - `pico/tool_executor.py`：1,407 行；
  - `pico/checkpoint_store.py`：1,345 行；
  - `pico/safe_subprocess.py`：1,062 行；
  - `pico/recovery_policy.py`：1,048 行；
  - `pico/agent_loop.py`：977 行；
  - `pico/runtime.py`：973 行；
  - `pico/security.py`：923 行。

大文件本身不是缺陷。恢复、文件身份校验和 shell grammar 的显式代码换来了可审计的
安全语义，不能仅为减少行数而压缩。真正需要优先处理的是单一协调函数同时承担过多
生命周期步骤：

- `ToolExecutor.execute()` 约 496 行；
- `AgentLoop.run()` 约 404 行。

### 2.3 已确认的工程缺口

| 编号 | 当前事实 | 直接影响 |
| --- | --- | --- |
| G-01 | `.env` 采用正确的 exact-root 语义，但 `config show` / `doctor` 没有清楚展示完整读取路径 | 在主工作区与 worktree 之间切换时，用户容易把“读取了另一个 `.env`”误判为 key 无效，最终表现为 401 |
| G-02 | CI 的 `push` 只覆盖 `main`；`memory` 只有通过 PR 才会触发 | 直接推送到当前主开发分支不能自动证明全量门禁仍为绿色 |
| G-03 | `uv.lock` 已在本地生成，但被 `.gitignore` 忽略；CI 执行 `uv sync --dev` | pytest、ruff 及其传递依赖会随时间漂移，同一提交不能严格复现同一开发环境 |
| G-04 | CI 只有 Ubuntu 3.11 / 3.12 | Pico 的 private-file、no-follow、mode、fsync 与 worktree 行为主要在 POSIX/macOS 本地使用，却没有 macOS 持续验证 |
| G-05 | Review Pack 先说 A-05 pending，后面又记录最终 43/43 | 当前真值与历史诊断混在同一层级，读者无法快速判断最终状态 |
| G-06 | `DEFAULT_FEATURE_FLAGS` 含 `prompt_cache`，但生产代码没有 `feature_enabled("prompt_cache")` 调用 | 看似可配置，实际上不能控制任何行为；checkpoint identity 还会保存这个无效值 |
| G-07 | `pico/features/memory.py` 仍有 514 行旧 layered-state API 和 legacy mirrors；生产代码只使用其中的路径与 file-summary helpers | 项目同时呈现两套 memory 心智模型，测试维护了生产运行时根本不走的 API |
| G-08 | 两个核心协调函数过长，测试样板也集中在少数超大测试文件 | 修改一个阶段时很难隔离失败，review 需要重新理解整条生命周期 |
| G-09 | 历史设计与执行计划全部留在 active `specs/`、`plans/` 目录 | 查找当前权威规范困难，但这些资料仍有审计价值，不应直接丢失 |

## 3. 目标

### 3.1 必须达成

1. 任意工作区运行配置诊断时，都能看见 Pico 实际使用的 repo root 与完整 `.env` 路径。
2. exact-root 安全语义保持不变：绝不向父目录、兄弟 worktree 或主工作区搜索 `.env`。
3. 直接 push 到 `memory` 与 push 到 `main` 一样触发完整 Ubuntu CI。
4. CI 与本地开发使用同一份已提交 lockfile，CI 不得静默更新依赖。
5. macOS 持续验证最依赖 POSIX 文件系统语义的安全、存储与恢复测试。
6. Review Pack 只给出一个当前结论；历史失败明确标为 historical diagnostics。
7. 删除无生产消费者的旧 memory facade 和无效 feature flag，不用新抽象替换旧抽象。
8. 将 `ToolExecutor.execute()` 和 `AgentLoop.run()` 收敛为可读的生命周期协调器，行为不变。
9. 保留全部安全攻击面、recovery durability 和 live-harness 离线断言。
10. 当前权威设计、实施计划和证据可在一个索引中快速定位。

### 3.2 优化后的实际收益

| 优化 | 直接好处 | 长期意义 |
| --- | --- | --- |
| 显示精确 `.env` 路径 | 401 与配置来源问题可在一次命令中定位 | 降低真实 E2E 和日常调试中的误操作，不以降低安全边界换便利 |
| `memory` push CI | 当前开发分支每次推送都有自动回归结果 | GitHub 上的“最新代码”与“最新验证”重新对齐 |
| lockfile + frozen sync | 同一提交解析出同一套开发工具 | 测试失败更可能是代码变化，而不是依赖漂移 |
| macOS focused CI | 真实本地平台的 mode、symlink、fsync 行为持续被检查 | 安全基线不再只依赖单台开发机的偶发验证 |
| 删除旧 memory facade | memory 真源和生产消费者更清楚 | 后续优化 recall 时不会误改一套未接线的实现 |
| 删除无效 flag | 配置表只表达真实行为 | 避免“开关存在所以功能可控”的错误假设 |
| 核心协调函数拆分 | 失败能定位到 prepare / execute / persist / finalize 阶段 | 降低修改工具或模型循环时引入跨阶段回归的概率 |
| 文档生命周期 | 当前规范与历史审计材料分层 | 后续 planning 不再从 4.7 万行历史资料里猜权威来源 |

## 4. 非目标

本阶段明确不包含：

- 发布、部署、打包发布流程或对外兼容承诺；
- Windows 支持；
- OS sandbox、容器隔离或权限代理；
- 新 Provider、Provider matrix、gateway、model registry 或 streaming 重构；
- embeddings、向量数据库或新的 memory 产品能力；
- 并行工具调用、多 agent runtime 或微服务拆分；
- mypy、coverage、benchmark 第三方依赖；
- 新运行时依赖；
- 以任意 LOC 数字为目的重写 recovery、安全或存储核心；
- 为通过指标删除安全 case、放宽 guard 或减少 live assertion；
- 自动读取兄弟 worktree、父目录或主 checkout 的 `.env`；
- 默认重跑付费真实 Provider E2E。

## 5. 设计原则

### 5.1 真源优先

- 配置真源是当前 lexical repo root 下的 `.env`，不是最近找到的 `.env`。
- session 真源是 v3 canonical messages 与 canonical working-memory fields。
- 依赖真源是已提交的 `pyproject.toml` + `uv.lock`。
- 当前验证真源是 Review Pack 指向的 committed benchmark artifacts；live JSON 继续忽略。

### 5.2 删除优先

如果一个开关没有调用方、一个 facade 没有生产消费者，先删除。不得为了“以后可能使用”
增加 registry、adapter、compatibility class 或配置层。

### 5.3 行为保持型拆分

核心函数拆分只移动已有阶段，不借机改语义。每次只改一个协调边界，先加或确认聚焦测试，
再做机械提取，最后跑完整门禁。

### 5.4 安全约束高于便利

`.env` 可见性通过诊断实现，不能通过扩大搜索路径实现。macOS CI 是增加证据，不是降低
POSIX 检查。恢复与 shell security 的显式分支即使较长，也只有在证明重复且行为等价时
才允许修改。

## 6. P0：运行与证据收敛

### 6.1 O-01：配置来源与 `.env` 路径可见性

#### 当前行为

`pico/config.py::project_env_path()` 已经正确返回：

```text
<resolved repo root>/.env
```

`pico/cli.py` 在构建 Provider client 前按 `WorkspaceContext.repo_root` 加载它；
`pico/cli_diagnostics.py` 也从同一 root 读取。问题只在输出层：用户可以看到 provider、model
和来源名，却不容易确认当前命令到底属于哪个 worktree，以及 `.env` 缺失发生在哪个路径。

#### 目标输出合同

`pico-cli config show --format json` 新增以下 additive 字段：

```json
{
  "workspace": {
    "repo_root": "/absolute/path/to/current/worktree"
  },
  "project_env": {
    "path": "/absolute/path/to/current/worktree/.env",
    "scope": "repo_root_exact",
    "status": "loaded"
  }
}
```

`status` 只允许：

- `loaded`：当前 exact-root `.env` 是可读取、通过既有安全检查的 regular private file；
- `missing`：该精确路径不存在；
- `review_required`：路径存在但类型、权限或读取安全需要人工检查。

它不列出值，不输出 API key，不输出 header，不扫描其他 `.env`。provider、model、base URL
和 API-key presence 继续使用现有 redacted source contract。

human-readable `config show` 与 `doctor` 至少显示：

```text
repo root    /absolute/path/to/current/worktree
.env file    /absolute/path/to/current/worktree/.env
.env scope   repo root only
.env status  loaded | missing | review required
```

当状态为 `missing` 时附加固定提示：每个 git worktree 都有自己的 repo-root `.env`；Pico
不会读取兄弟 worktree 或主 checkout 的 `.env`。

`init` 与 `config set-secret` 目前只显示 `.env` 文件名，也改为显示同一个绝对路径，避免
写入成功后仍无法判断写到了哪个 worktree。

#### 修改位置

- `pico/cli_diagnostics.py`
- `pico/cli_commands.py`
- 必要时在 `pico/config.py` 增加一个只返回非敏感状态的 helper
- `README.md`
- `tests/test_cli_diagnostics.py`
- `tests/test_cli_commands.py`
- `tests/test_project_env_security.py`
- `tests/test_artifact_security.py`

#### 必须覆盖的测试

1. 从 repo root 运行时显示 `<root>/.env`。
2. 从 repo 子目录运行时仍显示同一 repo root 的 `.env`。
3. 主 checkout 与 worktree 的路径不同，各自只读取自己的 `.env`。
4. 当前 worktree 缺少 `.env` 时，不回退到兄弟或父目录。
5. symlink、directory、hardlink 或非 private mode 的既有 fail-closed 语义不变。
6. text 与 JSON 输出都不包含 secret value。
7. `--format json` 仍是稳定的 success envelope，只增加字段，不改变已有字段含义。

#### 完成意义

这项优化直接解决此前“`.env` 明明存在却得到 401”的定位困难，同时保留 exact-root
隔离。它把隐含安全行为变成可观察合同，而不是用更宽松的搜索规则掩盖问题。

### 6.2 O-02：`memory` 分支直接 push 触发 CI

#### 修改

`.github/workflows/ci.yml` 的 push branches 从：

```yaml
branches:
  - main
```

改为：

```yaml
branches:
  - main
  - memory
```

`pull_request` 继续覆盖所有目标分支。Ubuntu 3.11 / 3.12 的 lint、全量 pytest 与 offline
live harness 保持不变。

#### 验收

- 本地 YAML review 确认 `main` 与 `memory` 均在 `push.branches`。
- 推送实施提交后，GitHub 上 `memory` branch 的 CI workflow 实际启动并通过。
- 不增加临时 feature branch pattern，不把所有 branch push 都纳入，避免无价值的重复成本。

#### 完成意义

当前开发真源已经是 `memory`。如果它不触发 CI，远端最新提交与自动验证状态就会分离。
加入该分支后，每次代码同步都能留下对应的机器验证证据。

### 6.3 O-03：Review Pack 改为 final-state-first

#### 当前冲突

`docs/review-pack/README.md` 前段写着 A-05 remains pending，后段又正确记录最终
DeepSeek `qwen3.7-max` 通过 43/43。两者分别描述历史诊断与最终结果，但现在看起来像
两个同时有效的当前结论。

#### 目标结构

1. 首段直接给出当前状态：A-05 complete、43/43、8 native actions、10/15 calls、token 与
   wall-time 安全摘要。
2. 第二段说明当前 committed deterministic evidence 与 C0/I0/M0 review。
3. 历史 network error、401、诊断 false negative 移到 `Historical diagnostics` 小节，并明确
   它们已被最终 post-repair run supersede。
4. `docs/review-pack/dashboard.md` 与 README 使用同一状态词：`Done` / `complete`，不能再出现
   pending。
5. live JSON 继续被忽略，不提交 key、prompt、answer、request URL 或 header。

#### 验收

```bash
rg -n "A-05 remains pending|A-05.*Pending" docs/review-pack
```

必须无结果；`43/43`、`8 native actions`、`10/15` 只作为最终安全摘要出现，不复制敏感
payload。Review Pack 所有链接仍可解析到已提交的 benchmark evidence。

#### 完成意义

证据包的价值在于让读者不依赖聊天上下文就能判断项目状态。final-state-first 使当前真值
一眼可见，同时不删除失败历史，保留故障排查和修复链路。

## 7. P1：依赖复现与 macOS 验证

### 7.1 R-01：提交 `uv.lock` 并冻结 CI sync

#### 修改

1. 从 `.gitignore` 删除 `uv.lock`。
2. 使用当前 CI 已固定的 uv `0.11.26` 和当前 `pyproject.toml` 重新生成并检查 lockfile；
   本次设计自审时本机 uv 为 `0.11.19`，虽然 `uv lock --check` 已通过，但不能让本机与 CI
   的版本差异成为 lock 生成来源不明确的理由。
3. 提交 `uv.lock`。
4. CI 安装步骤改为：

```bash
uv sync --frozen --dev
```

5. 本地验证增加：

```bash
uv lock --check
uv sync --frozen --dev
```

#### 边界

- Pico 的运行时依赖仍必须为 0。
- 不增加依赖更新 bot，不建立 lock refresh automation。
- lockfile 更新必须是独立、可 review 的提交；业务代码提交不得顺手刷新版本。
- 如果 `pyproject.toml` 与 lock 不一致，CI 失败，不能自动重写 lock 后继续。

#### 验收

- clean checkout 可执行 `uv sync --frozen --dev`。
- `uv lock --check` 退出 0。
- lockfile 的生成/最终检查记录 uv `0.11.26`，与 CI pin 一致。
- Ubuntu 3.11 与 3.12 使用同一 lockfile 通过现有门禁。
- `git status` 不再出现被忽略且无法审计的本地 lock drift。

#### 完成意义

项目的测试与 lint 依赖已经比运行时依赖更重要。提交 lockfile 后，历史提交的验证环境
可以重建，CI 失败的归因更可靠，也避免未来某个 pytest/ruff 版本变化让未改代码突然失败。

### 7.2 R-02：增加 focused macOS CI job

#### Job 设计

保留 Ubuntu 3.11 / 3.12 全量 job，另加一个 Python 3.12 的 `macos-latest` focused job。
该 job 使用 frozen lock，只运行最依赖 POSIX/macOS 文件系统语义的测试：

```bash
uv run pytest -q \
  tests/test_project_env_security.py \
  tests/test_private_paths.py \
  tests/test_artifact_security.py \
  tests/test_file_lock.py \
  tests/test_recovery_durability_e2e.py \
  tests/test_shell_security_corpus.py
```

如果实施时其中某个文件包含明确的 Linux-only assumption，先将 assumption 写成可解释的
platform skip，不能直接从 job 中静默删除整个安全域。

#### 为什么不是完整 macOS matrix

本阶段要验证的是 mode、regular-file、no-follow、hardlink、atomic write、fsync、lock、
restore durability 与 shell grammar 的跨 POSIX 行为。完整 1,997-case macOS matrix 会增加
成本，却不会给纯 Provider/parser 测试带来相同比例的证据增量。focused job 是当前最小有用
范围。

#### 验收

- Ubuntu full job 与 macOS focused job 均通过。
- macOS skip 都有明确 platform reason；不能用 blanket skip。
- 不因 macOS 差异放宽 private mode、symlink 或 mutation guard。

#### 完成意义

Pico 当前主要在 Mac 本地开发和使用，而安全基线大量依赖真实文件系统语义。这个 job 将
“我本机跑过”升级为每次相关提交都能复现的平台证据。

## 8. P2：删除无效与重复结构

### 8.1 D-01：删除无调用方的 `prompt_cache` feature flag

#### 源码事实

`pico/runtime.py::DEFAULT_FEATURE_FLAGS` 当前包含：

```python
{
    "memory": True,
    "prompt_cache": True,
}
```

但生产代码只有 `feature_enabled("memory")` 的消费者，没有
`feature_enabled("prompt_cache")`。真正的 prompt caching 由 Provider 的
`supports_prompt_cache`、request metadata 和 provider-specific payload 控制。

#### 修改

- 从 `DEFAULT_FEATURE_FLAGS` 删除 `prompt_cache`。
- 不增加新的 cache flag、registry 或 capability facade。
- Provider 的现有 prompt cache 支持与测试全部保留。
- checkpoint runtime identity 只序列化真正影响运行行为的 flags。
- 读取旧 checkpoint identity 时，比较前过滤已经删除的 `prompt_cache` key，避免升级后只因
  无效字段产生虚假的 runtime mismatch；写回只写 canonical active flags。

#### 测试

- `DEFAULT_FEATURE_FLAGS` 只含当前有消费者的 key。
- 旧 identity 含 `prompt_cache` 时不会单独导致 resume mismatch。
- `memory` flag 差异仍会导致既有 mismatch。
- Anthropic/OpenAI-compatible prompt cache payload 与 usage tests 原样通过。

#### 完成意义

删除的是“假开关”，不是 cache 功能。这样配置、checkpoint identity 和实际行为重新一致，
后续使用者不会误以为 `feature_flags={"prompt_cache": False}` 可以关闭 Provider cache。

### 8.2 D-02：删除 tests-only `LayeredMemory` 状态系统

#### 源码事实

`pico/features/memory.py` 仍维护以下第二套状态：

- `working.task_summary` / `working.recent_files`；
- `episodic_notes`；
- `file_summaries`；
- legacy mirrors：`task` / `files` / `notes`；
- `LayeredMemory` facade 与一组 state-level mutation / retrieval / rendering helpers。

当前生产消费者实际只使用：

- `canonicalize_path`
- `file_freshness`
- `normalize_file_summaries_dict`
- `set_file_summary_dict`
- `invalidate_file_summary_dict`
- `invalidate_stale_file_summaries_dict`
- `summarize_read_result`

`LayeredMemory` 只被测试导入；真实 runtime 使用 `runtime.WorkingMemory`、
`session["working_memory"]`、`session["memory"]["file_summaries"]`，durable notes 则由
`pico/memory/block_store.py` 管理。

项目当前未发布，也没有需要维护的外部 `LayeredMemory` 使用者，因此这里采用直接删除，
不增加 deprecation cycle、warning 或 import shim。旧 session 数据兼容仍由下面定义的存储
迁移边界负责；它与保留一个未接线的 Python facade 是两件不同的事。

#### 删除范围

从 `pico/features/memory.py` 删除没有生产消费者的：

- `default_memory_state`
- `normalize_memory_state`
- state-level `set_task_summary` / `remember_file`
- `append_note`
- state-level `set_file_summary` / `invalidate_file_summary` /
  `invalidate_stale_file_summaries`
- `retrieval_candidates` / `retrieval_view`
- `render_memory_text` / `is_effectively_empty`
- `LayeredMemory`
- 只服务上述代码的常量与私有 helpers
- legacy `task` / `files` / `notes` mirror 写入

保留上述七个生产 helper；如果删除后模块名 `features.memory` 已经不准确，可在**不新建
compatibility shim** 的前提下把它们迁到一个现有 memory 模块，并一次性更新生产 imports。
默认选择是先保留文件名，避免为命名进行额外改动。

#### legacy session 边界

这项删除不能破坏 session v1/v2 到 v3 的迁移：

- 旧输入可以在 `migrate_session_to_v3()` / `WorkingMemory.from_dict()` 的单一读取边界被接受；
- runtime 内存与再次保存后的 v3 session 只写 canonical
  `working_memory={task_summary,recent_files}` 和 `memory={file_summaries}`；
- 不在正常运行路径继续双写 `task` / `files` / `notes`；
- 迁移 backup、private mode、atomic replace 合同不变。

#### 测试调整

- 删除只证明 `LayeredMemory` facade 存在的 public-contract assertion。
- 将 `tests/test_memory.py` 收敛为七个实际 helper 的行为测试。
- 增加 legacy session read-old / write-new fixture，明确 re-save 后无 legacy mirror。
- memory recall、context injection、checkpoint freshness、memory quality 与 ablation 全部保留。
- `tests/memory/test_v1_durable_gone.py` 应继续证明旧 durable system 已消失，但不再通过导入
  另一个 tests-only facade 来证明。

#### 约束与衡量

- `pico/features/memory.py` 应显著小于当前 514 行；建议上限 260 行，用于防止删除 facade
  后又建立同等规模的新抽象。
- 不以行数为唯一验收；七个生产 helper 的调用点与行为必须全部保留。
- 不增加 embeddings、notes schema 或新 memory 配置。

#### 完成意义

Pico 的 memory 将只剩三类清楚的东西：turn working state、file summaries、durable block
store。删除未接线的第四套 layered state 后，后续 review 不会再把 tests-only retrieval 当成
真实召回链，也能减少近一半 memory helper 的维护面。

## 9. P3：渐进式核心维护性优化

### 9.1 M-01：先收敛 `ToolExecutor.execute()`

#### 当前职责

`ToolExecutor.execute()` 目前在一个约 496 行函数中串联：

- tool lookup 与参数 validation；
- effect classification；
- shell grammar / risk / approval；
- before snapshot 与 Tool Change pending record；
- runner 调用与异常归一化；
- workspace observation；
- verification evidence；
- memory update；
- Tool Change terminalization；
- metadata 与 `ToolExecutionResult` 构造。

模块已经有多组纯 helper，下一步不是建立 executor framework，而是让 `execute()` 只保留
生命周期顺序。

#### 目标形态

```text
execute
  -> prepare and validate
  -> assess policy / approval
  -> capture pre-state and pending evidence
  -> invoke one runner
  -> observe and finalize side effects
  -> return one structured result
```

允许在同一模块增加少量 private dataclass 或 helper，但必须满足：

- helper 对应真实生命周期阶段，不是每十行包一个函数；
- 不创建 registry、plugin layer、event bus 或新的执行状态机；
- pending record 一旦创建，每个返回与异常出口都必须 terminalize 或留下明确 interrupted
  evidence；
- 原始异常优先级、redaction、approval binding、mutation lock 与 verification evidence 不变；
- shell command 仍只经过一个 assess gate 和一个 execute gate。

`execute()` 的建议软上限为 250 行。该数字用于促使阶段边界清楚，不得通过隐藏复杂度、
压缩可读代码或删除 guard 达成。

#### 先锁定的行为矩阵

- unknown tool / invalid args / disallowed tool；
- read-only tool success / error；
- recoverable workspace write success / partial / failure；
- `memory_write` 不创建空 workspace restore；
- shell blocked / ask / approved / stale approval；
- runner exception、observation exception、persistence exception；
- Tool Change pending → terminal 的所有出口；
- secret redaction 与 structured metadata。

#### 完成意义

工具执行是安全、恢复和产品行为的交界处。把生命周期阶段显式化后，新增或修复一个工具
不需要在 496 行分支中同时推理 approval、snapshot 和 terminalization。

### 9.2 M-02：再收敛 `AgentLoop.run()`

#### 当前职责

`AgentLoop.run()` 约 404 行，负责 preflight、Provider attempts、usage 聚合、Action decode、
tool pairing、retry feedback、session commit、checkpoint、verification 与 terminal finalization。

#### 目标形态

```text
run
  -> preflight one user turn
  -> request / decode one model attempt
  -> apply ToolAction | RetryAction | FinalAction
  -> finalize exactly once
```

优先复用当前已有的 `_run_turn_preflight()`、`_commit_session()`、`_prepare_tool_result()`、
`_finalize_run()`。只提取仍嵌在 `run()` 内且具有独立不变式的 model-attempt 与 action-apply
阶段，不建立 Action handler class hierarchy。

必须保持：

- `Response -> decode_action -> Action` 是唯一决策路径；
- retry feedback 对下一 attempt 可见且 one-shot；
- tool_use / tool_result 成对持久化；
- Provider usage 按每 call 聚合；
- Provider、session persistence 与 finalizer 异常的 primary/secondary 顺序不变；
- terminal artifact 只 finalize 一次；
- max-step 与 recovery checkpoint 行为不变。

`run()` 的建议软上限为 250 行，适用同样的“不得隐藏 guard”规则。

#### 完成意义

AgentLoop 是 Pico 的运行时内核。收敛后，Provider 请求失败、Action decode 失败、工具失败
与最终持久化失败分别落在可测试阶段，后续改 context 或 Provider 时不必重新触碰整条 turn
生命周期。

### 9.3 M-03：暂不重写 recovery 与 security 大文件

`RecoveryManager`、`CheckpointStore`、`RecoveryPolicy`、`safe_subprocess` 和 `security.py`
虽然较大，但它们刚完成多轮对抗性 review，分支多来自明确的 fail-closed 行为。本阶段：

- 只允许修复由 P0-P3 直接触发的回归；
- 不按文件大小拆模块；
- 不统一成通用 storage abstraction；
- 不合并看似相似但安全前置条件不同的路径；
- 任何后续 refactor 必须单独提出安全不变式 spec。

这项“不做”很重要：它避免刚获得的安全可信基线在无产品收益的整理中重新失去证据。

### 9.4 M-04：测试样板收敛

测试代码 31,388 行并非问题本身。shell grammar、安全攻击 corpus 与 recovery journal 的显式
case 是资产。只合并不会改变 case 可读性和失败定位的重复 setup。

优先候选：

- `tests/test_provider_clients.py` 的 HTTP response / request capture setup；
- `tests/test_tool_executor.py` 的 agent / tool fixture；
- `tests/test_agent_loop.py` 的 scripted Provider 与 session assertions；
- `tests/test_cli_diagnostics.py` 的 output/source matrix。

明确不以压缩为目标的文件：

- `tests/test_shell_execution_security.py`
- `tests/test_safe_subprocess.py`
- `tests/test_shell_assessment.py`
- `tests/test_shell_security_corpus.py`
- `tests/test_artifact_security.py`
- `tests/test_recovery_journal.py`

这些文件只有在能保持攻击样本名称、输入与期望结果直接可见时才允许参数化。

#### 收敛规则

- helper 放在最接近消费者的测试模块；至少三个文件真正共享时才进入 `conftest.py`。
- parametrization 的 case id 必须描述行为，不能只显示序号。
- 不把断言藏进返回 bool 的通用 helper。
- 不删除 security case，不以 pytest case count 作为 KPI。
- 对被整理文件，目标是减少至少 10% 的重复 setup 行；如果可读性变差则不做。
- test-only commit 不得修改生产代码。

#### 完成意义

减少的是建立测试场景的重复劳动，不是覆盖面。核心重构时可以更快增加一个行为 case，
失败输出仍能指出具体 Provider、Action 或安全条件。

### 9.5 M-05：历史设计文档生命周期

#### 当前问题

当前已跟踪的 `specs/` 与 `plans/` 共 30 个文件、47,085 行。它们记录了重要决策，但所有
文件都位于 active 目录，完成、取代、调查材料与当前规范没有清楚分层。

#### 目标目录

```text
docs/superpowers/
  INDEX.md
  specs/       # 当前有效或下一阶段设计
  plans/       # 当前正在执行的 implementation plan
  archive/
    specs/     # 已完成或被取代的设计
    plans/     # 已完成的执行计划
```

#### 规则

- 新增 `INDEX.md`，每个文档只标记 `active`、`completed`、`superseded` 三种状态之一。
- active specs 保留当前总体设计、安全基线设计和本文；后续经批准的设计加入此处。
- active plans 只保留正在执行的 plan；完成后移动到 archive。
- 历史文件使用 `git mv`，不复制，不在 active 与 archive 保留两份。
- 当前 ignore 规则已允许跟踪 `docs/superpowers/archive/`；实施时用 `git check-ignore`
  再验证，不为不存在的问题修改 `.gitignore`。
- Review Pack 与 benchmark result 不移动。
- 本地现有未跟踪设计、plan 与 `task_plan.md` / `findings.md` / `progress.md` 不在本阶段
  自动移动、删除或提交。
- 如果链接目标移动，必须同一提交修复链接并运行 link/path 检查。

#### 衡量

- active `specs/` 与 `plans/` 各自不超过 5 个已跟踪文件。
- 每个 active 文档在 INDEX 中有唯一状态与用途。
- 47,085 行历史材料仍在 Git 与 archive 中，因此收益是可导航性，不虚称仓库体积下降。

#### 完成意义

这会让下一位维护者先看到当前真值，同时仍能追溯每次设计和执行。文档从“时间顺序的
堆积”变成有生命周期的工程资产。

## 10. 关键数据流

### 10.1 配置流

```text
CLI cwd
  -> WorkspaceContext.build(cwd)
  -> lexical repo_root
  -> repo_root/.env only
  -> safe read / parse
  -> CLI > project_env > process environment > default
  -> Provider client

diagnostics
  -> same repo_root and exact path
  -> path/status/source only
  -> never secret values
```

任何实现若出现 parent search、sibling worktree search 或“找到第一个 `.env`”即违反本设计。

### 10.2 CI 与依赖流

```text
push main | push memory | pull request
  -> checkout
  -> pinned uv action
  -> uv sync --frozen --dev
  -> Ubuntu: ruff + full pytest + offline live harness
  -> macOS: focused POSIX security/storage/recovery suite
```

### 10.3 Memory canonicalization

```text
legacy session input
  -> one migration/read normalization boundary
  -> session v3
       working_memory {task_summary, recent_files}
       memory {file_summaries}
       durable notes in BlockStore
  -> canonical writes only
```

tests-only `LayeredMemory` 不再位于这条流中，因此直接删除。

### 10.4 核心协调流

```text
AgentLoop
  -> one model attempt
  -> one Action
  -> ToolExecutor when needed
       prepare -> policy -> execute -> observe -> persist evidence -> result
  -> canonical session commit
  -> terminal finalization exactly once
```

## 11. 分阶段交付顺序

### Phase 0：本地主工作区同步

状态：本设计开始前已完成。

- `/Users/wei/Desktop/pico` 的 `memory` 已 fast-forward 到 `5f359bd`。
- `HEAD == origin/memory`。
- 没有已跟踪本地改动。
- 原有未跟踪资料全部保留。

### Phase 1：P0 配置、CI 与证据

1. 配置诊断新增 exact `.env` path/status/scope。
2. README 解释 worktree exact-root 规则。
3. CI push 增加 `memory`。
4. Review Pack 改为 final-state-first。
5. 运行配置、安全输出与全量本地门禁。

该阶段完成后，即使后续维护性工作暂缓，日常运行与远端验证已经明显改善。

### Phase 2：P1 lockfile 与 macOS

1. 提交 `uv.lock`，CI 改 frozen sync。
2. 增加 macOS focused job。
3. 本地运行 lock check、Ubuntu 等价门禁。
4. 推送后确认两个 CI job 的真实结果。

### Phase 3：P2 删除死结构

1. 删除无效 `prompt_cache` feature flag，并处理旧 checkpoint identity normalization。
2. 删除 tests-only `LayeredMemory` 状态系统。
3. 运行 session migration、memory、context、checkpoint 与全量测试。
4. 重跑 deterministic memory evidence；不得改变当前定义的 benchmark 语义。

### Phase 4：P3 核心协调器拆分

1. 锁定 `ToolExecutor.execute()` 行为矩阵。
2. 只拆 ToolExecutor，聚焦测试与全量门禁通过后提交。
3. 锁定 `AgentLoop.run()` 行为矩阵。
4. 只拆 AgentLoop，聚焦测试与全量门禁通过后提交。
5. 任何一步发现需要改变行为，停止机械重构，为该行为另写小 spec；不能暗中扩大。

### Phase 5：测试与文档整理

1. 只整理被 Phase 3/4 触及的重复测试 setup。
2. 建立 `docs/superpowers/INDEX.md` 并移动已跟踪历史文档到 archive。
3. 修复所有内部链接。
4. 运行最终门禁与结构检查。

## 12. 文件到需求映射

| 需求 | 主要生产/配置文件 | 主要验证文件 |
| --- | --- | --- |
| `.env` path/status/scope | `pico/config.py`, `pico/cli_diagnostics.py`, `pico/cli_commands.py`, `README.md` | `tests/test_cli_diagnostics.py`, `tests/test_cli_commands.py`, `tests/test_project_env_security.py`, `tests/test_artifact_security.py` |
| `memory` push CI | `.github/workflows/ci.yml` | GitHub Actions 实际 run |
| Review Pack 一致性 | `docs/review-pack/README.md`, `docs/review-pack/dashboard.md` | static `rg` + link check |
| lockfile | `.gitignore`, `uv.lock`, `.github/workflows/ci.yml` | `uv lock --check`, `uv sync --frozen --dev` |
| macOS focused CI | `.github/workflows/ci.yml` | 六个 POSIX/security/recovery test files |
| 删除 dead flag | `pico/runtime.py`, `pico/checkpoint.py` | `tests/test_clean_up.py`, checkpoint/resume tests, provider cache tests |
| 删除旧 memory facade | `pico/features/memory.py`, 必要的 import sites | `tests/test_memory.py`, `tests/test_public_api_contract.py`, session migrator、memory/context/benchmark tests |
| ToolExecutor 收敛 | `pico/tool_executor.py` | tool executor、shell、safety、verification、recovery tests |
| AgentLoop 收敛 | `pico/agent_loop.py` | agent loop、action codec、message invariant、runtime report、E2E v2 tests |
| 测试样板 | 仅候选 test files | full pytest 与 case matrix review |
| 文档生命周期 | `docs/superpowers/INDEX.md`, tracked specs/plans | `git check-ignore`、path/link check，`git status` 确认未跟踪资料未动 |

## 13. 验证策略

### 13.1 每个小阶段

- 先运行最接近改动的 focused tests。
- `uv run ruff check <touched paths>`。
- `git diff --check`。
- 检查没有 secret、live result JSON 或用户未跟踪文件进入 staged set。

### 13.2 每个阶段边界

```bash
./scripts/check.sh
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
```

要求：

- Ruff 0 errors。
- pytest 全绿；平台 skips 必须可解释。
- offline live assertions 全绿。
- 不以当前恰好 1,997 个 case 的数字为硬合同；参数化可以改变显示数量，但行为矩阵不能缩减。

### 13.3 删除 memory facade 后

除全量门禁外运行：

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2(repetitions=5)'
```

要求 committed evidence 仍符合当前 benchmark 定义；删除 tests-only memory 不能改变真实 recall
或 file-summary injection。

### 13.4 核心协调器拆分后

运行：

```bash
uv run python -m benchmarks.perf.bench_build_v2
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
uv run python -m benchmarks.perf.bench_security_recovery
```

要求输出合法 JSON，样本全部有效；不设 noisy latency 硬阈值，但需记录与基线相比是否出现
数量级回退。

### 13.5 真实 Provider E2E

本阶段默认不重跑付费 E2E，因为 P0-P2 不改变 Provider wire contract；现有 43/43 仍是当前
真实证据。只有 Phase 4 实际改变 model/tool runtime 行为，且 deterministic/local gates 全绿
后，才提出一次新的显式授权 gate。不得自动复用已经消费过的 prior live-run authorization。

## 14. 完成标准

全部条件同时满足才算本阶段完成：

### 运行与配置

- `config show`、`doctor`、`init`、`config set-secret` 明确显示 exact `.env` 路径。
- worktree 缺少 `.env` 时给出 repo-root-only 提示，不读取其他 checkout。
- 所有输出继续 redacted，401 可从 path/source/status 层定位。

### CI 与复现

- `main` 和 `memory` push 均触发 CI。
- `uv.lock` 已跟踪；CI 使用 `uv sync --frozen --dev`。
- Ubuntu full CI 与 macOS focused CI 均通过。

### 证据

- Review Pack 的当前结论唯一且为 A-05 complete。
- 历史 network/401/diagnostic 记录明确标为 superseded historical evidence。
- deterministic artifacts 与 live 安全摘要保持可追溯。

### 删除与维护性

- `prompt_cache` 不再是 feature flag；真实 Provider cache 仍工作。
- 生产代码和 public-contract test 不再引用 `LayeredMemory`。
- `pico/features/memory.py` 只保留真实生产 helpers。
- legacy session 仍能 read-old，v3 只 write-new。
- `ToolExecutor.execute()` 与 `AgentLoop.run()` 的生命周期阶段清楚，软上限达到或有基于安全
  可读性的书面例外。
- 没有新增 runtime dependency、framework、registry 或兼容 shim。

### 测试与文档

- 全量本地门禁、offline live assertions、memory evidence 与 perf smoke 通过。
- 安全 corpus、recovery durability 与 session migration 行为没有减少。
- active specs/plans 有唯一索引；历史资料已归档，未跟踪用户资料未动。

## 15. 风险与回滚

| 风险 | 控制 | 回滚边界 |
| --- | --- | --- |
| 新诊断字段意外泄露 key | 只输出 path/status/source，复用 inspection redactor 与 secret-boundary tests | 单独回滚 O-01 提交 |
| worktree 提示诱导共享 `.env` | 文案明确“每个 worktree 独立”，不实现 fallback | O-01 无配置语义改动 |
| lockfile 引入不兼容版本 | 独立提交、3.11/3.12 双 CI、frozen check | 回滚 lock/CI 提交，不触及业务代码 |
| macOS 暴露真实平台差异 | 先分类为实现 bug或明确 platform constraint，禁止 blanket skip | macOS job 独立；不能通过放宽安全 guard 回滚 |
| 删除 memory facade 误删生产 helper | 先以 `rg` 固化消费者清单，按 symbol 删除，跑 memory/context/evidence | D-02 独立提交，可整体 revert |
| 删除 flag 使旧 checkpoint 假 mismatch | 比较前过滤已删除 key，保留 active flag mismatch tests | D-01 独立提交 |
| 核心拆分改变异常/terminal 顺序 | 一次只拆一个协调器，锁定 failure matrix，保留原异常为 primary | M-01、M-02 各自原子提交 |
| 测试参数化隐藏攻击 case | 安全 corpus 默认不压缩，case id 与输入保持可见 | test-only commit 可独立 revert |
| 文档移动破坏链接或误碰本地资料 | 只 `git mv` 已跟踪文件，link check，stage allowlist | archive commit 独立 revert |

## 16. 明确延后项

只有在本设计全部完成且出现真实需求时，才重新评估：

- model connection / provider axis 重设计；
- Windows 兼容；
- OS sandbox；
- streaming；
- parallel tools；
- vector memory；
- recovery/security 模块级重构；
- 完整 macOS Python matrix；
- 性能硬阈值；
- 发布与对外兼容政策。

## 17. 下一步交接

本文批准后，下一步使用 `writing-plans` 生成逐任务 implementation plan。该 plan 必须：

1. 按 Phase 1 → Phase 5 排序，不并行修改同一核心文件。
2. 每个任务给出精确文件、先失败的测试、最小实现、验证命令和独立 commit 边界。
3. 明确 stage allowlist，保护当前所有未跟踪资料。
4. 将真实 GitHub CI 检查放在 push 后，不用本地推断替代。
5. 不把本文的延后项重新塞入实施范围。

在用户审阅本文前，不开始写 implementation plan，也不实施上述优化项。
