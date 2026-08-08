# Pony 1.0 验证与发布

发布证据只对 exact Git HEAD 有效。旧 commit、dirty worktree 或另一版本 wheel 的结果不能继承。本文件区分可重复的离线
产品门禁，以及会产生费用的 Provider live 验收。

## 一键离线门禁

```bash
./scripts/check.sh
```

该脚本要求起始 worktree clean，并依次验证 lock、单次 Ruff、单次全量 pytest（包含 offline live-harness assertions）、
独立 deterministic core-functional evaluation、单次离线 sdist/wheel build、单次 archive/clean-install verifier，最后复验
Git HEAD 未变化且 worktree 仍 clean。Evaluation 与构建产物都写入本轮临时目录并在退出时清理；任何一步失败都停止。

## 门禁矩阵

| Gate | 类型 | 必须结果 | 主要证据 |
| --- | --- | --- | --- |
| G0 Source | 离线 | clean exact HEAD、版本/lock/CHANGELOG 一致 | Git、repository structure tests |
| G1 Static | 离线 | Ruff 零错误 | `uv run ruff check .` |
| G2 Functional | 离线 | 全量 pytest 零失败，裸/显式 TUI 入口一致 | `uv run pytest -q` |
| G3 Security | 离线 | escape/secret/permission/mutation/legacy-reader 专项零失败 | security/durability test groups |
| G4 Evaluation | 离线 | deterministic core-functional 与 offline assertions 通过 | evaluation scripts + pytest |
| G5 Distribution | 离线 | sdist/wheel 精确内容、metadata、License | distribution verifier |
| G6 Clean install | 离线 | 新虚拟环境中 CLI/version/help/doctor 与 TUI import 通过 | install smoke |
| G8 Provider live | 条件/收费 | 每个声明可用的账号/模型完成最小 probe | `doctor --check-api` 或 bounded harness |
| G9 Documentation | 离线 | 路径、命令、Provider 表与实现一致 | structure/docs tests |

G0-G6 与 G9 是 package 发布 mandatory gate。G8 需要用户拥有的账号、Key 和费用授权，是部署/Provider 组合验收；
离线 wire-contract tests 必须始终通过，但不能冒充真实账号结果。

## 支持与兼容矩阵

| 维度 | 1.0 声明 | 发布证据 |
| --- | --- | --- |
| Python | 3.11、3.12 | Linux 全量测试；macOS 3.12 安全与耐久性专项 |
| OS | macOS、Linux | CI；Windows 原生支持正在按 [ADR-0050](adr/0050-windows-native-support.md) 实施，完整实机门禁前仍不受支持 |
| Anthropic Messages | 实现支持 | 离线 wire contract；每个账号/model 的 G8 单独验收 |
| OpenAI Responses | 实现支持 | 离线 wire contract；每个 endpoint/model 的 G8 单独验收 |
| OpenAI Chat Completions | 实现支持 | 离线 wire contract；每个 endpoint/model 的 G8 单独验收 |
| Ollama Chat | 实现支持 | 离线 wire contract；每个本地 model 的 G8 单独验收 |
| Execution | 受信仓库中的 Host | 无 OS sandbox；旧 Sandbox 只读 inspection，不可 resume |

“实现支持”不代表所有网关兼容。发布或部署结论必须写明 exact HEAD、Provider、protocol、endpoint 类别、model 和
G8 是否执行；不得把某一组合的 live 结果外推到其他组合。

### Windows 支持晋级门禁

CI 的 Windows 3.11/3.12 capability job 分开运行 symbol probe 与安全语义 probe。后者在 hosted Windows runner 上验证
`NtCreateFile` root-handle-relative 逐层打开、reparse point 打开后识别、稳定 File ID、hardlink count；DACL probe 验证文件和
目录的当前用户 owner、单一无继承 full-control ACE、protected DACL、handle/path 双重复验，以及 owner/DACL 漂移拒绝；
raw atomic-write capability probe 仍验证同目录 durable temp、失败时保留旧内容以及 `ReplaceFileW` 的系统语义，但 production
private-state migration promotion 与 workspace atomic writer 的发布权威已改为持续持有并复验 source/temp handle，再通过目标
parent handle 执行 `NtSetInformationFile(FileRenameInformation)`；发布后复验 File ID、DACL、大小与 digest，失败时从已验证的
restore handle 回滚，不依赖 `MoveFileExW`/`ReplaceFileW` 路径调用决定提交对象。production private-state probe 通过公共 API
验证 private directory、create/read/append/replace、post-install validation rollback 与 tree hardening；production
workspace-file probe 验证 root-handle-relative create/read/list、bounded I/O、CAS、hardlink/reparse 拒绝、完整 parent chain
deny-delete、replace rollback、commit ambiguity rollback 与 long path；production `LockFileEx` probe 验证跨进程 timeout、
释放后重获、同线程重入拒绝、hardlink 拒绝、`require_existing` 零写，以及持锁期间禁止删除 leaf/重命名 parent；Job Object
probe 还验证 suspended child 在执行前加入带 `KILL_ON_JOB_CLOSE` 的 Job，并在根进程正常退出、timeout、output-limit 与关闭
Job 后终止完整 descendant tree。
Session、migration、memory 与 Git metadata 已有 Windows production-backend probe；剩余门禁主要是 clean-host Python 3.11/3.12
矩阵、受保护 machine-scope Git/Python 上的 shell/evaluation、Windows Terminal 交互式 TUI 宽度回归，以及 clean exact HEAD 的
完整一键门禁。
Windows 仍是未支持平台。只有以下证据在同一 exact HEAD
全部成立后，才可增加 Windows classifier 和公开支持声明：

- Windows 11 x64 的 Python 3.11、3.12 全量 pytest、Ruff、build、distribution verifier 与 clean-install smoke 通过；
- root-relative traversal、reparse point/junction、hardlink、ADS、DACL/File ID 漂移和 atomic-write fault injection 通过；
- `LockFileEx` 互斥/timeout/identity race 与 Job Object timeout/output-limit/完整进程树清理通过；
- PowerShell command policy、原生 Git/rg、Windows Terminal/cmd/PowerShell 启动、TUI 40/80/120 列回归通过；
- Windows 专项不是由 WSL、Git Bash 或大面积 `skipif Windows` 获得绿色结果。

普通 CI 与 `v*` Tag 发布共用 `.github/workflows/windows-verification.yml` 的 Windows 3.11/3.12 标准用户矩阵；
`publish` job 必须等待该矩阵完成。完整 Windows pytest 通过显式 skip policy 拒绝未知 skip/xfail，并以 `-ra` 和
`--durations=50` 输出按原因统计与慢测试证据；末尾的 `windows_skip_audit=<JSON>` 是 schema v1 的机器可读汇总，包含
每个原因的实际数量、未知原因和总审核结论。数量只用于比较同一 exact candidate SHA 的 3.11/3.12 clean-host 结果，
不得把 dirty worktree 或不受信宿主的本地数量冻结成发布阈值。该门禁结构本身不构成通过证据；仍须由候选 exact tag
的实际结果和 Windows Terminal 实机验收完成 Phase 5。

Windows workflow 在切换到受控标准用户前，先从 `uv.lock` 导出仅运行时依赖，并通过隔离 primer 环境把对应归档写入共享
uv cache；标准用户只在取得该 cache 的显式 ACL 后以 `UV_OFFLINE=1` 执行 distribution clean-install smoke。普通
`uv sync` 只证明开发环境可安装，不能替代“锁定依赖已预热且离线隔离安装成功”的发布证据。

#### 2026-08-08 Windows 11 x64 本地实施证据

以下结果来自 `467dc7bd1df91b528050e0013fb708b234f8a0da` 上的未提交实现工作区，只用于说明当前分支进度；由于 worktree
不是 clean exact HEAD、Python 3.11 未运行且 G4 仍被宿主可执行文件信任门禁阻塞，因此不构成发布证据，也不改变 Windows
“尚未支持”的状态：

- Windows 11 x64、Python 3.12.13：完整 `pytest -q --maxfail=20 -ra` 为 `2575 passed, 150 skipped`；安全聚焦组为
  `175 passed, 23 skipped`。聚焦 skip 对应当前用户没有 symbolic-link privilege 以及 FIFO/POSIX mode 等明确平台条件，不能
  将其替代为放宽产品安全策略；完整 skip 集仍须在 clean-host CI 中复核。
- `uv lock --check`、`uv run --frozen ruff check .` 与 `git diff --check` 通过。
- 13 个 Windows probe 通过：atomic-write capability、capabilities、DACL、production file lock、file semantics、Git metadata、
  Job Object、lock semantics、memory、migration、private files、process、workspace files。workspace probe 已覆盖
  `commit_ambiguity_rollback`、`rollback`、long path、hardlink rejection 与 root rename denial；private probe 已覆盖 create、
  replace、rollback 与 private DACL。
- `probe_shell_backend.py` 在本机正确 fail closed：`C:\Git\cmd\git.exe` 及父目录对当前用户可写，因而不能作为 trusted
  executable。产品策略不得为本机环境放宽；正式 probe 需要受保护目录中的 machine-scope Git（通常位于
  `C:\Program Files\Git`）。
- `core-functional` 中 `core.memory-quality-fake` 通过，`core.fixed-benchmark` 因
  `trusted verifier executable unavailable` 失败。本机 `.venv`、用户级 Python 和 PATH 中其他 Python 候选的父目录均不满足
  immutable executable directory 合同；这属于 clean-host 环境门禁，不能通过信任用户可写 Python 修复。
- 仓库外 `%TEMP%` 目录的完全离线 `uv build` 成功，生成 `pony_code-1.0.0.tar.gz` 与
  `pony_code-1.0.0-py3-none-any.whl`；`verify_distribution.py --install-smoke --offline-bundle-smoke` 通过。
- PowerShell 与原生 `cmd.exe` 的 `pony --help` 均退出 0；`pony status` 退出 0 并在不可信 Git 环境下将 Git 状态标记为
  unavailable；`prompt_toolkit` 和 `pony.tui.app` 原生导入通过。Windows Terminal 的实际交互、40/80/120 列视觉回归仍待
  clean-host Phase 5 验收。

在同日 `cde4029f9fb2f8fa8c989e0f7210c0f5589e3bdc` 的 dirty worktree 上，Python 3.12 全量测试被拆成四个互斥 shard，
合计 `2709 passed, 152 skipped`（共收集 2861 项）。其中 151 个 skip 属于审核集合，另一个
`trusted git is unavailable` 被 skip policy 正确拒绝；这与本机用户可写 Git 安装的 fail-closed 结果一致。最慢 shard
耗时 29 分 19 秒，说明正式 workflow 还必须观察 hosted runner 的超时裕量。该诊断不是 clean exact HEAD、没有 Python
3.11 对照，也未运行完整 `scripts/check.sh`，因此不是发布通过证据，不应据此增加 Windows classifier。

同一 HEAD 的首次 clean-host CI 在 Python 3.11/3.12 上各暴露 18 个相同失败：reparse point 已正确 fail closed，但
private-state、file-lock、Git 和 migration 收到了 Win32 底层文本，而不是既有稳定领域错误。随后的候选修复把该事实改为
专用异常并在领域边界归一化；本机对应七个测试文件为 `271 passed, 55 skipped`，13 个非 shell Windows production probe
全部通过，skip audit 为 `approved=true`、`unknown_reasons=[]`，其中 junction fixture 明确证明 reparse point 在遍历前被
拒绝。这些结果只证明候选修复方向，仍须在提交后的
Windows 3.11/3.12 capability-rich clean host 上确认原 18 项归零，并让完整门禁继续执行 evaluation、build、distribution、
clean-install 和 CLI tail。

同一 dirty 候选继续执行远端未到达的门禁后半段：wheel/sdist 构建、精确 distribution 校验、install smoke、offline bundle
smoke、PowerShell/cmd `pony --help`、`pony status` 与 TUI import 均通过。core-functional 的 memory-quality fake 场景通过，
fixed benchmark 因本机 PATH 中没有满足 executable trust 的 Python 而 fail closed；shell probe 同样因本机 Git 安装目录可写而
拒绝，固定系统 PowerShell 本身可被信任。不得为消除这两个宿主阻断降低 executable trust；正式结论仍等待 standard-user CI
wrapper 提供受保护的 machine-scope Python/Git 并在 clean exact HEAD 上从头执行。

同一 dirty 候选的 TUI 与 CLI 自动合同组为 `273 passed, 6 skipped`，覆盖 responsive app、Markdown、安全 renderer、
runtime hook、parser、commands、error envelope、output、diagnostics、migration、Session 与 memory CLI；它排除了当前实现的
自动化交互回归，但仍不能替代下述真实 Windows Terminal Phase 5。

#### Windows Terminal Phase 5 实机验收

此项是候选版本的人工发布门禁。TUI import、单元测试或截图不能替代本清单；任何必做项未执行、证据缺失或结果不一致，
Phase 5 均为 `FAIL`，Windows 继续保持未支持。验收必须使用与自动门禁相同的 clean exact candidate SHA，不得从 WSL、
Git Bash、IDE 内嵌终端或 dirty worktree 外推结果。

准备条件：

- 使用 Windows 11 x64 标准用户，以及系统受保护目录中的 machine-scope Python、Git 和 `rg`；先确认同一 SHA 的 Windows
  3.11/3.12 自动门禁均通过，并安装该 SHA 生成且已由 distribution verifier 验证的 wheel；
- 在一次性、受信、已完成 `pony init` 的测试仓库中操作；记录 Windows build、Windows Terminal 版本、终端 profile、字体、
  Python 版本、`pony --version`、exact candidate SHA 和显示缩放比例；
- 动态 Assistant 渲染步骤会发出一次最小 Provider 请求，必须取得当轮费用/网络授权并记录 Provider、protocol、endpoint
  类别与 model；未获授权时标记“未执行”，Phase 5 不得判为 `PASS`。

按以下顺序执行并逐项记录 `PASS`/`FAIL`、观察值和证据文件：

1. 分别从 Windows Terminal 的 PowerShell 与 Command Prompt profile 直接运行 `pony --help`、`pony` 和 `pony repl`；两种
   交互入口必须进入同一 TUI，`/exit` 后终端输入、光标和按键处理恢复正常。不得只验证 CLI 帮助或 import。
2. 在 PowerShell profile 中把可用内容区依次调整为 40、80、120 列，每次记录终端实际报告的列数并重新启动 `pony`。
   三种宽度都必须保留马形 Logo、块状 `PONY CODE` 字标和既定视觉意图，无裁切、重叠、残留重绘或水平滚动；40 列使用
   compact 布局。footer 按宽度降级，但始终不得显示绝对路径、Session ID、API Base 或 checkpoint ID。
3. 在 120 列会话中输入 `/`，确认 completion 菜单最多五项；输入七行文本，确认输入框最多增长六行且光标/滚动正常；
   输入中文、英文和 emoji，确认用户消息为无角色标签的低对比块。使用已授权 Provider 发送固定最小请求，要求返回标题、
   列表、行内代码、代码块和表格，确认 Markdown 降级可读、控制字符不可见且 `Working…` 在正式输出前清除。
4. 会话空闲时按一次 `Ctrl+C` 清空非空输入，再按两次 `Ctrl+C` 验证退出提示与退出；重新进入后以 `Ctrl+D` 退出。
   退出后键盘、光标和终端模式必须恢复，不能遗留输入 hook。
5. 分别运行 `pony --no-color` 和设置 `NO_COLOR=1` 后运行 `pony`，确认布局与文本仍完整且没有 ANSI 颜色；清除环境变量后
   重新启动，确认颜色能力恢复。再进行一次 120→40→80 的运行中缩放，确认没有旧 footer、菜单或消息残影。

验收记录至少包含以下字段，并作为候选 tag 的发布附件或 CI 关联 artifact 保存；截图必须先检查不含 Key、完整 API Base、
私有 prompt、绝对私有路径或 Session 标识：

```text
exact candidate SHA:
Windows build / Windows Terminal version:
Python / pony / profile / font / scaling:
PowerShell launch: PASS 或 FAIL（证据）
Command Prompt launch: PASS 或 FAIL（证据）
40、80、120 列与运行中缩放: PASS 或 FAIL（证据）
输入、completion、Markdown、中文与 emoji: PASS 或 FAIL（证据）
Ctrl+C / Ctrl+D / hook 恢复: PASS 或 FAIL（证据）
--no-color / NO_COLOR: PASS 或 FAIL（证据）
Provider / protocol / endpoint 类别 / model / G8 授权与结果:
Phase 5 结论：PASS 或 FAIL
验收人 / 时间 / artifact 链接:
```

跨机器或跨 OS 复制 active Session 的支持声明还必须通过 [ADR-0050](adr/0050-windows-native-support.md) 定义的 logical identity/physical binding 格式迁移；
否则只声明 Windows 本机新建与恢复 Session。

## 聚焦测试

### Provider 与 `.env`

```bash
uv run pytest -q \
  tests/test_config.py \
  tests/test_cli_commands.py \
  tests/test_cli_diagnostics.py \
  tests/test_provider_clients.py \
  tests/test_provider_anthropic.py \
  tests/test_provider_openai_chat_completions.py \
  tests/test_provider_response.py \
  tests/test_provider_probe.py
```

必须覆盖三 Provider、四 Transport、项目环境优先级、Ollama 无 Key、非法组合 fail closed、API 路径、认证 header、
tool call/result continuation、stop reason 与 usage。

### 安全与耐久性

```bash
uv run pytest -q \
  tests/test_project_env_security.py \
  tests/test_private_paths.py \
  tests/test_workspace_io_security.py \
  tests/test_shell_execution_security.py \
  tests/test_shell_security_corpus.py \
  tests/test_secret_boundaries.py \
  tests/test_legacy_artifacts.py
```

macOS CI 额外以 `-W error::DeprecationWarning` 运行关键安全组，确保 spawn/subprocess 行为没有平台警告退化。

### Host mutation 与 legacy inspection

```bash
uv run pytest -q \
  tests/test_tool_executor_mutation_lock.py \
  tests/test_shell_execution_security.py \
  tests/test_workspace_observer.py \
  tests/test_recovery_cli.py \
  tests/test_cli_session_commands.py
```

必须证明 approval 在 lock 之前、runner 与 before/after observer 处于同一 lock、非零退出写后返回 `partial_success`、
Checkpoint CLI 只读且 removed mutation commands 零写失败、`/rewind --workspace` 被拒绝、legacy Sandbox-bound Session
在 Provider resolution 前 fail closed。旧 Sandbox/Recovery writer 已删除，不是公开产品能力。

### CLI 与 TUI

```bash
uv run pytest -q \
  tests/test_cli_parser.py \
  tests/test_cli_commands.py \
  tests/test_cli_error_envelope.py \
  tests/test_cli_workflow.py \
  tests/test_permissions.py \
  tests/tui
```

必须覆盖裸 `pony` 与 `pony repl` 的同一分派、`pony run` 纯结果输出、未知命令建议、TTY/`TERM=dumb`/窄终端
fallback、`NO_COLOR`、40/80/120 列响应式马形 `PONY CODE` 欢迎页与精简 footer、五项 slash completion、
六行输入、换行/中断，以及中文、英文、emoji、标题、列表、代码块、表格降级、非法 Markdown 和控制字符清理。
欢迎页测试必须同时锁定马形 Logo、块状字标、版本、模型/permission 摘要、宽度上限和 compact 变体；只有用户明确要求
修改设计时才可更新这些断言，不能把门禁弱化为“包含任意 PONY 文本”。

Input queue 测试必须同时覆盖 plain/TUI：单 worker FIFO、五条 pending 上限、满队列拒绝、`/queue clear` 零 Session
写、queued prompt 只在 dequeue 后按序进入 Canonical Messages、approval answer 由 UI 接收且不入队，以及 `/exit` 等待
active turn 而不声称取消 Provider/Tool。聚焦入口是 `tests/test_input_queue.py` 与 `tests/tui/test_app.py`。

事件投影测试必须证明 `Working…` 会在正式输出前清除、自动 checkpoint 零输出、成功 Tool 只输出一行、失败与中断
可见，并且 footer 不泄露绝对路径、Session ID、API Base 或 checkpoint ID。runtime hook 恢复、durable trace 顺序、
permission prompt 参数脱敏与 prompt fail closed 仍是阻断项；离线 contract 不得描述为 Provider reasoning 或 streaming 验证。

Permission/Plan 合同还必须覆盖：Session v5 的 `auto` runtime 默认值；v1-v4 inspection 零写与 crash-safe explicit
migration；六种公开 mode 与 `manual -> default` 边界；dangerous bypass 双开关、picker capability 与 resume preflight；
deny/ask/allow rule 优先级、allowed/disallowed flags、连续 rule/mode picker；mode x rule x read-only x shell；模型可见
schemas 与 Executor 双重约束；Plan text/revision/secret/byte 边界、SessionStore 原子 CAS、完整审批渲染、批准期间替换；
`/plan open` revision CAS、`open|share` 先进入 Plan 与空 artifact 不启动外部动作；fork/rewind/reset/clone；
`--permission-mode` one-shot；plain/TUI Resume；
active prompt history；以及 Session commit -> durable trace -> listener 顺序。聚焦入口包括：

```bash
uv run pytest -q \
  tests/test_workflow_state.py \
  tests/test_workflow_policy.py \
  tests/test_session_store.py \
  tests/test_runtime_resume.py \
  tests/test_cli_session_inspect.py \
  tests/test_cli_commands.py \
  tests/test_cli_workflow.py \
  tests/tui/test_app.py
```

Session model 合同必须覆盖：`/model` 零写显示与共享 REPL handler；`/model <model>` 和 `run/repl --model`；`.env` 零修改；
Session model 持久化与 resume 优先级；相同 protocol/endpoint 的 binding CAS；跨 endpoint、异常 factory binding、secret
model name 与 opaque Provider state 的 fail-closed 零写；切换后的模型预算、token accounting、delegate factory 与 TUI
footer。聚焦入口包括：

```bash
uv run pytest -q \
  tests/test_config.py \
  tests/test_pony.py \
  tests/test_cli_parser.py \
  tests/test_cli_commands.py \
  tests/test_cli_workflow.py \
  tests/tui/test_app.py
```

Worktree Agent 合同使用真实本地 Git 覆盖：clean exact-HEAD setup、独立 branch/worktree/client/Session/Run、readonly
拒写、write 只改 child、`max_parallel`、factory client reuse cleanup、private child/batch manifest、sealed commit/test
evidence、model-free review、project-trusted ordered merge-all、完整顺序 conflict preflight、merge crash-safe resume 与
执行中断后的显式 discard cleanup：

```bash
uv run pytest -q tests/test_worktree_agents.py
```

Worktree 的 `passed` 只来自 Executor 生成的结构化 verification evidence；`--version`、`--help`、`--collect-only` 等
不执行检查的命令不得算作通过。subprocess 专项还必须覆盖脱离进程组但继承 capture pipe 的后代，证明 timeout 不会挂死。

## Distribution 验证

```bash
./scripts/check.sh
UV_OFFLINE=1 uv build --offline --clear --no-create-gitignore --out-dir dist
UV_OFFLINE=1 uv run --frozen python scripts/release/verify_distribution.py \
  --dist-dir dist --install-smoke --offline-bundle-smoke
```

普通门禁的分发包随系统临时目录清理。Tag 发布流程在门禁通过后有意重新构建固定 `dist/`，并再次运行 verifier；
随后生成 hash 并发布的正是这次重新验证过的 wheel 和 sdist。发布不复用或搬运普通门禁的临时归档。

Verifier 使用 `git ls-files pony` 建立产品文件真源并检查：

- sdist 单一 wrapper、无 link/special file；
- wheel/sdist 无 tests、benchmarks、scripts、docs、`.github` 或 development evaluation；
- wheel 精确包含 tracked runtime Python 文件；
- Name、Version、Summary、Python `>=3.11,<3.13`、MIT、Project URLs、README 与 console entry 正确；
- Runtime `Requires-Dist` 精确为 `prompt-toolkit>=3.0.52,<4`，wheel 为 `py3-none-any`；
- clean venv 从锁定 uv cache 离线解析 prompt-toolkit/wcwidth，安装后 TUI 可导入，且 `pony --version`、help、doctor
  行为正确；removed Sandbox/Checkpoint mutation commands 由 CLI/parser 聚焦测试验证；
- smoke 环境不继承 `PONY_*`、厂商 Key、`PYTHONHOME` 或 `PYTHONPATH`。

本轮正式 benchmark/evaluation 结果见
[`benchmark-evaluation-results-2026-08-05.md`](benchmark-evaluation-results-2026-08-05.md)。

## Decision-driven coding benchmark

[`ADR-0049`](adr/0049-benchmark-evaluation-design.md) 定义 Q suite 的目的：判断一个 exact commit、Provider/model 或 feature
是否提高 **Safe Correct Completion（SCC）**，并把结果映射为 `accept`、`reject` 或 `inconclusive`。它不输出综合分，也不把
更快失败、Fake Provider 脚本通过或公开测试通过解释为 coding capability。

当前 8-task pilot 只代表受信、小到中型、离线 Python maintenance task。它想观察四件事：

- 有公开失败时，Agent 是否能定位根因并做最小修复；
- 没有公开失败时，Agent 是否能导航到 latent edge case；
- 多文件合同是否一致修改且不破坏回归；
- I/O/config/CLI 边界是否 fail closed 且无越界副作用。

### 先 qualification 测量工具

```bash
uv run --frozen python benchmarks/coding_quality/run_benchmark.py qualify \
  --tasks benchmarks/coding_quality/tasks.json \
  --output /private/tmp/pony-coding-quality-qualification.json
```

Qualification 的目的不是测试模型，而是证明 benchmark 自身有区分度且可重复。成功必须同时观察到：broken target 连续
`3/3` 失败；diagnosis broken fixture 的 public tests 失败、其他 slice 的 public tests 通过；reference 的 target、regression、
public 连续 `5/5` 通过；允许范围内的 reference patch 通过；模拟越界修改被拒绝。作为 hard gate 的 `allowed_changes` 必须显式
出现在 Agent 可见 task prompt 中；任一 task 失败都禁止 live run。

Qualification artifact 是 live condition 的强制输入。Runner 在 Provider resolution 前验证 exact-key schema、零失败、task 数以及
corpus/grader digest；缺失、手工矛盾或过期 artifact fail closed。Artifact 只写入私有临时目录，不提交。

### 再冻结 Evaluation Brief

看到结果前创建 exact-key brief；字段和示例见 ADR-0049 第 6 节。Brief 必须写清：

1. 要做什么决定；
2. 可证伪问题和假设；
3. baseline/candidate exact SHA；
4. 目标 slice、SCC expected effect 和 hard-gate guardrail；
5. 每 task trial 数；
6. 哪些机器可执行条件必须判为 inconclusive。基础 provenance、dirty、invalid trial 和 non-live 条件强制存在；代码 feature
   evaluation 通常再加入 `provider_transport_failure`，Provider/model 可靠性 evaluation 可省略它并把 transport failure 计入 outcome。

没有预先冻结 brief 的运行只能用于探索，不能支持 feature 改善、合并或发布结论。

### 两个 worktree 分别运行 condition

Baseline 和 candidate 必须各自在自己的 clean exact-HEAD worktree 中运行；不要在一个 Python 进程里 checkout 或动态 import 两个
commit。两边使用同一 tasks、qualification、Provider target、预算和 brief：

```bash
uv run --frozen python benchmarks/coding_quality/run_benchmark.py run \
  --brief /private/tmp/evaluation-brief.json \
  --condition baseline \
  --qualification /private/tmp/pony-coding-quality-qualification.json \
  --tasks benchmarks/coding_quality/tasks.json \
  --output /private/tmp/baseline-condition.json

uv run --frozen python benchmarks/coding_quality/run_benchmark.py run \
  --brief /private/tmp/evaluation-brief.json \
  --condition candidate \
  --qualification /private/tmp/pony-coding-quality-qualification.json \
  --tasks benchmarks/coding_quality/tasks.json \
  --output /private/tmp/candidate-condition.json
```

真实 run 复用仓库 `.env`、production resolver、transport factory、`Pony`、Session/Run、permission rule 和 hardened tool path。
`run_shell=allow` 仍经过 command/path/secret/mutation policy；不使用 `bypassPermissions`。`--allow-dirty` 只允许有界调试 smoke，
artifact 会标记为 non-confirmatory，comparator 不会接受它。

Coding-quality condition artifact 当前为 format version 3。每个正常 trial 都记录排序、去重、bounded 的 relative
`changed_files`/`forbidden_files`，validator 要求 forbidden 是 changed 的子集，并与 `integrity_pass` 一致；缺少 scope evidence 的
trial 不能声明 integrity pass。这样 scope hard gate 对 Agent 可见，失败时也能从低敏 artifact 审计具体越界文件。

### 纯 artifact comparison

```bash
uv run --frozen python benchmarks/coding_quality/run_benchmark.py compare \
  --brief /private/tmp/evaluation-brief.json \
  --baseline /private/tmp/baseline-condition.json \
  --candidate /private/tmp/candidate-condition.json \
  --output /private/tmp/coding-quality-comparison.json
```

Comparator 从 trial 重算 task SCC count 和 summary，并验证 commit、brief、corpus/grader digest、Provider、protocol、预算、trial 数、
task set、clean state 和 live claim：

- `accept`：预先声明的 task wins/losses 与全部 guardrail 通过；
- `reject`：frozen conditions 有效，但 expected effect 或 guardrail 失败；
- `inconclusive`：provenance/condition 不一致、dirty、invalid trial、scripted Provider 或 artifact 内部矛盾。

被 policy 成功拒绝且没有产生 workspace effect 的 tool attempt 记录为 `policy_rejections` 诊断项，不直接构成 hard gate；否则会
反向惩罚 fail-closed。只有 scope/integrity 失败、未知 workspace effect、durability/finalization 失败或边界实际失守才是不可抵消的
hard gate。若拒绝后 Agent 未完成任务，target/finalization/budget outcome 仍会使 SCC 失败。

Fake Provider 单测只证明 fresh workspace、hidden grader 隔离、production runner plumbing 和 SCC 计算合同；不得放入 Q capability
结果。完整 live coding benchmark 属于收费 G8，必须记录 exact SHA、Provider/protocol/model、task/trial 数和费用边界。该 suite
不加入默认 `scripts/check.sh`，因为默认发布门禁必须保持离线、确定且零费用。

2026-08-05 的 scope-explicit fresh pilot 在 clean exact commit 上得到 `24/24 SCC`、`24/24` scope evidence、零 forbidden file 和
零 hard gate。它只把当前 corpus 资格提升为 must-pass canary；达到 ceiling 后不得用于 Provider/model/feature 的 confirmatory
comparison。完整证据和旧 `23/24` 历史见本页前述结果文档。


## Compaction efficiency 与非流式 Provider latency

`scripts/evaluation/run_efficiency_evaluation.py` 只回答两个有明确决策的问题：

1. compaction 在单次/重复 compaction 且 resume 后，能否保持 active state 的 SCC，并在六个后续 turn 内偿还 summary
   成本、产生正的净 token 收益；
2. 当前 canonical `.env` 选定的单一 Provider target，在固定 short-final、read-tool-continuation、long-context workload
   下，完整非流式响应、首 action 可用和最终完成需要多久。

它不把字符压缩率称为 token 节省率。第 `k` 个 follow-up 后使用 Provider usage 计算：

```text
gross_input_saved(k) = Σ baseline follow-up input - Σ compacted follow-up input
baseline_total(k) = Σ baseline(input + output)
compacted_total(k) = summary(input + output) + Σ compacted(input + output)
net_saved_tokens(k) = baseline_total(k) - compacted_total(k)
break_even_turn = first k where net_saved_tokens(k) >= 0
```

每个 paired trial 只有 baseline SCC 有效、compacted SCC 通过、usage 完整且 frozen horizon 内 break even 才能 `accept`；
compacted 事实保留失败或 horizon 内仍无净收益为 `reject`；Provider 失败、baseline 无效或 usage 不完整为 `inconclusive`。
默认每个场景运行三次：至少两个 trial `accept` 且没有 `reject` 才接受；任一 `reject` 即拒绝；其余为 `inconclusive`。这样最多
容忍一个无效 pair 的随机噪声，但不隐藏 compacted regression。dirty worktree 的 measured effect 只作探索，顶层 decision
强制 `inconclusive`。

Production adapter 当前全部 `stream=False` 并在 body 完整读取后返回，所以真实首 token 时间不可观察。Artifact 固定写
`ttft_status: unavailable_non_streaming`，不得用 `provider_complete_ms` 冒充 TTFT。延迟只在成功 trial 上解释并报告
p50/p95；Provider failure type/count、retry 和 usage completeness 单独报告，即使 Agent 后续 retry 成功也不抹除失败。

已授权收费请求时，在 clean exact HEAD 上运行：

```bash
uv run --frozen python scripts/evaluation/run_efficiency_evaluation.py \
  --repo-root /path/to/repository-with-canonical-env \
  --compaction-repetitions 3 \
  --latency-repetitions 3 \
  --output-json /private/tmp/pony-efficiency-evaluation.json
```

Runner 复用 production config resolver 和 Transport factory；不拥有 Provider selector、registry 或第二配置面。Artifact
只保存 SHA/dirty、Provider/protocol/model、聚合 usage、延迟、重试和 opaque grader 结果，不保存 `.env`、API Base/Key、
prompt、answer、raw response 或 reasoning。当前一次运行只能描述一个 target；Provider 间比较必须分别从各自 canonical
repo root 产生脱敏 artifact，并在 Provider、model、workload、预算和 trial 数一致时离线比较。

## Provider live

真实 API 会产生网络请求、token 消耗和费用。只有用户明确授权后执行：

```bash
pony doctor --check-api
```

Probe 以两次调用验证 native tool call 与 tool-result continuation；continuation 同时证明最终文本能力。forced Provider 使用
exact target；外部 missing/auto/OpenAI family 只在同一 configured origin 上按固定 Chat/Responses 顺序解析，最多
两个候选、四次请求（loopback auto 最多三个、六次），单请求最多 30 秒、总计最多 90 秒且 detection 零 retry。
Anthropic-compatible gateway 必须显式选择 `anthropic`。真实用户请求不做 fallback。维护者 live harness 还必须设置
model-attempt、request-timeout、token 与 wall-time cap。

Live harness 的每个 designed turn 必须由 task state、report 与 trace 一致证明为
`completed/final_answer_returned`，且 final answer 非空。`step_limit_reached`、`retry_limit_reached`、空 final 或终态证据
缺失/不一致都必须使 Behavior gate 失败，并停止后续收费 turn。

Harness 仅暴露 `read_file` 与 `memory_read` 两个只读工具：前者验证 workspace tool round-trip，后者允许模型按
已注入的 Memory 索引读取命中笔记。每 turn 最多三个 tool step，为“读取 Memory、读取 workspace、返回结论”保留
完成路径。Memory recall turn 固定为一次 `memory_read` 后返回结论；workspace tool round-trip 由独立 digest turn
验证。它不暴露写入、shell、delegate 或 Memory 写入能力。

Live report 不应保存 prompt、answer、raw response、Key、header 或完整 URL；只记录 Provider、模型、exact SHA、固定 caps、
行为标签、计数、usage、wall time 和稳定错误码。账号错误、配额、模型不可用与协议失败应明确区分。
当前 live report format v3 还单独记录 bounded Provider resolution evidence：source、protocol、candidate count、probe
model-call count 与 usage status。它不把 probe 调用并入 workload 的 model/HTTP attempt totals；发布 evaluator 对 v3
字段做 exact-schema 校验，旧格式、未知字段、非法计数或不完整终态一律失败。
Usage 缺失可标记为 `usage_unavailable` 并允许基本 tool contract 使用，但不能宣称 transport-cost gate 通过。
此时 harness 继续受 model/HTTP attempt、单次输出和 wall-time 上限约束；完整五轮的功能、安全和持久化 gate 全部通过、
请求证据完整且零 retry 时，结果只能报告为 `PASS WITH DEGRADED USAGE`，Transport 保持 `DEGRADED`。其他 transport
降级或任一硬 gate 失败不得借此变成成功。
自动 compaction 的 summary 请求也必须由 durable trace/report 计入 model attempts、transport、失败原因和成功 usage；
live harness 的内存 sniffer 只补充旧 trace 或尚未开始 Run 的辅助调用，不能对当前 durable evidence 重复计数。
对 `invalid_arguments` 或 `workspace_entry_unsafe` 的拒绝，Agent Loop 最多向下一次请求加入一次非持久化、无路径的
修正提示；同一 `(tool, rejection code)` 再次出现即以 retry limit 停止，避免付费循环。测试必须证明提示不进入
Canonical Messages 或 durable trace，也不得推荐当前 permission mode 隐藏的工具。
Provider auto 的 G8 证据至少覆盖：省略 Provider、`openai` family、init 写 resolved 值、doctor 零写、
run/repl 进程内解析、native tool continuation 和 usage complete/degraded。比较 `.env` 时必须记录
bytes、inode、mtime 与 mode；报告仍不得保存真实 prompt/answer/response。

## 版本晋级

当前源码版本为 `1.0.0`，但在创建并推送 exact `v1.0.0` tag 前仍是未发布状态。若先发布候选版，使用
`1.0.0rc1`，同步 `pyproject.toml` 与 `uv.lock`，并在候选 exact HEAD 运行完整离线门禁和适用的 live 验收。修复所有
阻断后再晋级为 `1.0.0`、把 development classifier 从 Beta 改为 Production/Stable、更新 CHANGELOG，并在最终 exact
HEAD 从头重跑门禁。

## Tag 发布

`.github/workflows/release.yml` 只响应 `v*` tag，并要求 tag 精确等于 `v<project.version>`。工作流在全新 runner 中重复
静态、功能、评估、临时构建和 clean-install 门禁；同时复用与普通 CI 相同的 Windows 3.11/3.12 标准用户完整门禁与
原生攻击 probe。只有 Windows 矩阵全部通过后，`publish` job 才有意重建固定 `dist/`、再次验证实际待发布归档，再使用
GitHub OIDC / PyPI Trusted Publishing 上传 wheel 与 sdist，生成 SHA-256 文件并创建 GitHub Release。

发布前外部一次性配置：

- PyPI 项目 Trusted Publisher 绑定 repository、workflow `release.yml` 与 environment `pypi`；
- GitHub environment `pypi` 配置必要的保护规则；
- branch/tag protection 与维护者 review 生效。

发布工作流不存储长期 PyPI token。创建 tag、push 或发布到外部服务仍是单独的维护者授权动作。

## NO-GO 条件

- 任一 mandatory gate 失败或出现未解释 skip；
- tracked tree dirty、tag/version/lock/CHANGELOG 不一致；
- archive 多出开发文件或缺少产品文件；
- security failure 被普通功能测试掩盖；
- 用离线 contract 冒充真实 Provider 结果；
- live 证据包含凭证或私有内容；
- 发布产物不是 exact tag workflow 构建的同一组文件。
