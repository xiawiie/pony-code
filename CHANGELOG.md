# Changelog

## 1.0.0 — Unreleased

Pony 1.0 将预发布仓库收束为一个可安装、可验证、可发布的本地 coding-agent 产品。

### Added

- 三个用户可见 Provider：Anthropic、OpenAI、Ollama；OpenAI 支持 Responses 与 Chat Completions 两个 Variant。
- 统一的四变量 `.env` 合同，以及能写全配置的交互式 `pony init`。
- 参考 Pi 消息层级的行内 TUI：响应式马形 `PONY CODE` 欢迎页、低对比用户消息块、内置 Markdown、slash command menu、
  可增长多行输入、历史搜索、精简状态栏与 fail-closed 审批。
- `pony --version`、MIT License、完整 package metadata、Project URLs 与 tag-bound release workflow。
- PyPI Trusted Publishing、GitHub Release、SHA-256 release assets 和 clean-install distribution smoke。
- Session v5 的 `manual|auto|acceptEdits|bypassPermissions|dontAsk|plan` permission mode、exact tool-name
  allow/ask/deny rules、append-only Plan artifact，以及 run/repl-only `--permission-mode`。
- 可连续编辑的 `/permissions`（`/allowed-tools` 别名）、CLI `--allowed-tools`/`--disallowed-tools`、Claude-style 先进入
  Plan 的 `/plan [description|open|share]`，以及需要显式危险开关的
  `bypassPermissions`。
- v1/v2/v3/v4 Session inspection、`session inspect latest`、显式 resume migration，以及显示
  permission/checkpoint/resume/model 的 TUI/plain 一次性 Resume 卡和 active Canonical prompt history。
- Claude-style `/model [model]` 与 run/repl-only `--model`：在相同 protocol/endpoint 下切换并持久化 Session model，
  不修改 `.env`，不增加模型 registry、catalog 或失败 fallback。
- 可省略的 `PONY_PROVIDER`、OpenAI family selector、`openai-chat`/`openai-responses` 强制值，以及发送真实任务前的
  bounded synthetic Provider resolution；`pony init` 可持久化结果，doctor 保持只读。
- plain/TUI 共用的五条内存 follow-up queue 与 `/queue [clear]`；单 worker 在完整 turn 边界按 FIFO 执行，queued input
  直到 dequeue 才进入 Canonical Messages，approval、Tool 和 Provider request 不被抢占或伪取消。
- Project Skill 可通过一个逗号分隔的 `resources` 字段显式引用同目录内最多八个 bounded UTF-8 只读资源；不递归、不
  glob、不执行，并在 `/help` 与 `pony doctor` 提供不回显内容的稳定拒绝诊断。
- `delegate_worktrees` bounded batch：独立 Git worktree/`codex/pony-agent-*` branch/client/Session/Run、并发上限、
  private terminal manifest，以及 model-free `pony agents list|show|merge|cleanup`；完成后绝不自动 merge。merge 只接受
  封存 revision 并重新检查 project trust；未合入 terminal child 使用显式 `cleanup --discard` 回收。

### Changed

- 裸 `pony` 现在直接进入交互 TUI；`pony repl` 保留为显式同义入口，`pony run <prompt...>` 与管理子命令继续使用
  生产分支的显式 CLI 合同。
- 恢复并冻结响应式马形 `PONY CODE` 欢迎页；纯文本 fallback 不显示 banner，`pony run` 只输出执行结果。
- TUI 与纯文本 fallback 共用一个 REPL 输入处理器；`prompt-toolkit` 成为唯一直接 runtime dependency，distribution
  smoke 在隔离环境中离线验证锁定依赖和 TUI import。
- TUI 运行事件收束为瞬态 `Working…`、单行 Tool 摘要、一次性 permission prompt 和明确的失败/中断；自动 checkpoint
  不再进入对话区，footer 不再显示绝对路径、Session ID、API Base 或 checkpoint ID。Provider reasoning 与
  streaming 不属于 1.0 展示面。
- 产品代码按 `agent`、`cli`、`config`、`context`、`memory`、`providers`、`runtime`、`security`、`state`、
  `tui`、`tools`、`workspace` 等领域包归位；`pony/` 顶层只保留 `__init__.py` 与
  `__main__.py`。
- Provider 连接最多使用 `PONY_PROVIDER`、`PONY_API_BASE`、`PONY_API_KEY`、`PONY_MODEL`；项目 `.env` 高于
  进程环境。强制 Provider 静态路由；missing/auto/OpenAI family 可在发送真实任务前做 bounded
  synthetic resolution。
- Provider factory 从 transport shared helpers 中独立，用户 Provider 与内部四种 wire transport 解耦。
- Evaluation 移至 `benchmarks/evaluation`，维护脚本按 evaluation/release 分类。
- 全量检查入口同时覆盖 lock、Ruff、pytest、evaluation、build、archive 与隔离安装。
- 未发布 1.0 的 package metadata 使用 Beta 状态，只声明 CI 验证的 macOS/Linux，不再声明 OS Independent。
- Pony 装配、Context request、Shell permission decision 与私有/Workspace 原子写按职责拆分；Host mutation 使用独立
  workspace lock，并在锁内观察执行前后的实际文件 effect。
- 每个 top-level turn 冻结 permission mode、Plan artifact/revision 和模型可见 tool schemas；固定 prefix 不再列举
  native tools，Executor 仍在 permission prompt 前执行同一 mode ceiling。Plan 与 checkpoint 使用独立 required chunk。
- Generic gateway 使用 conservative Capability Profile；Provider protocol errors 保留安全 stage/reason，真实用户请求
  失败后不跨协议重放。
- Provider 错误在 one-shot、plain REPL、TUI 和 JSON 使用同一安全投影；普通 benchmark 对 unresolved target
  fail closed，收费 live harness 在 workload 前复用共享 resolver，并以新 production client 执行真实任务。
- Live report format v3 分开记录 bounded resolution probe 与 workload 调用，严格验证每轮终态和低敏 Tool/error 计数；
  usage 缺失只能形成 `PASS WITH DEGRADED USAGE`，其他降级仍失败。
- Provider request metadata 与 Run report 不再保存 endpoint origin；Provider request ID 与 usage counters 分开投影，
  避免合法 usage 因低敏 trace schema 拒绝未知字段而整体丢失；Session 继续只绑定 endpoint hash。
- Agent Loop 对参数 schema 或 unsafe workspace entry 的拒绝只提供一次非持久化修正；同一 `(tool, rejection code)`
  再次出现即停止，且提示不假设当前不可见的工具。
- 通用文件编辑在 permission 前拒绝 `.git/**` 与 `.pony/**` 控制面写入；自动 bootstrap 文档拒绝 hardlink，
  `pony agents show` 同时显示 sealed 与 live worktree diff 状态。
- Host、Git 与 RG runner 使用共享 bounded capture；stdout/stderr 聚合超限时终止进程组并以稳定错误 fail closed。
- `run_shell` native schema 和 missing-executable 拒绝只列出本轮已冻结的可信 executable 名称，不暴露路径，便于模型在
  identity、permission 和 policy 不放宽的前提下选择真实可执行入口。
- `pony session checkpoint|compact|rewind --summary` 重新使用 CLI assembly 构造 resumed runtime；短会话无法继续压缩时
  返回稳定的 `compaction_no_progress`，不再降级为泛化的 startup failure。

### Removed

- 固定 DeepSeek-first 公开路径，以及旧 `PONY_DEEPSEEK_API_KEY` runtime 兼容读取。
- Memory doctor 不再维护第二套 frontmatter、引用图和 Git ignore lint；只保留 bounded、no-follow 可读性健康检查。
- 未发布的 distributed Sandbox authority、candidate、product enablement、aggregate/release controller 与远程 cache/download 路径。
- 重复的 `examples/mini-pony` 实现和过期的分布式 Sandbox 规格文档。
- Evaluation 从 runtime wheel/sdist 中完全移除。
- 无运行时调用的 NetworkControl、sandbox governance evaluator、Provider compatibility 分支、Fake Provider 产品模块、
  `/save`、`--max-new-tokens`、未引用截图与 `MANIFEST.in`。
- 未发布的 Workflow Mode、Active Plan、`--mode`、`/mode`、`/plan clear` 与 runtime `approval_policy` 配置面。
- 公开 `--sandbox`、`pony sandbox ...`、Source Apply、workspace restore，以及 Checkpoint
  `preview-restore|restore|resolve-pending|prune` 写命令；Checkpoint CLI 只保留 `list|show|pending`。
- active runtime 中的 Docker Sandbox、RecoveryManager、RecoveryCheckpointWriter、ToolChangeRecorder 与 runtime
  CheckpointStore 装配；仅保留 bounded legacy artifact reader 用于兼容检查。

### Security

- TUI renderer 只在 durable trace 写入后收到副本；permission prompt UI 异常 fail closed，离开 TUI 后恢复原 runtime hook。
- 公开 runtime 只提供 Host 执行，并明确不构成 OS 隔离；审批与 policy revalidation 后，所有潜在 mutation 在独立
  workspace lock 内执行和观察，非零退出但已写入时返回 `partial_success`。
- Provider URL 继续拒绝 userinfo、query、fragment、凭证和非 loopback HTTP；Ollama `auth=none` 是显式例外。
- 通用 `PONY_API_KEY` 纳入项目环境优先级、diagnostics 脱敏、runtime redaction 与安全测试。
- Plan 在脱敏前执行 strict 12 KiB/schema/secret validation；Session v1-v4 inspection 零写，迁移发布前复验
  source、backup、candidate identity 与 exact bytes。
- 旧 Sandbox-bound Session 在 Provider resolution 前以 `legacy_sandbox_session_unsupported` fail closed；非法旧 binding
  返回 `sandbox_state_invalid`，不会静默回退 Host。

### Migration

1.0 是模型配置硬切换。旧 `.env` 中的 `PONY_DEEPSEEK_API_KEY`、厂商 Key、Provider/Profile/Connection 字段不会
配置 runtime。运行 `pony init` 写入四个通用变量，或按 `.env.example` 手工迁移。切换 Provider、protocol 或 endpoint
后，旧 Session 会因 Model Session Binding 不一致而返回 `model_session_mismatch`；应新建 Session。仅切换 model 时，
使用 `/model <model>` 或 `run/repl --model <model>` 更新当前 Session；含 opaque Provider state 的 Session 仍须新建。

升级不会自动删除 `.pony/` 或 `~/.pony/` 中的旧 Session、Run、Checkpoint、Memory 与 Sandbox artifact。
旧 Checkpoint/Sandbox artifact 仅支持 bounded 只读 inspection，不再提供 restore、prune、resolve 或 Source Apply。
Session v1 JSON 和 v2-v4 JSONL 只在 CLI `--resume` 或 `Pony.from_session()` 时迁移到 v5；其他 writer 返回
`session_migration_required`。v3 的 `act` 迁移为内部 `default`（公开显示 `manual`），`plan/review` 迁移为新 `plan`
permission mode，旧 Active Plan 不迁移为新的 Plan artifact。含未定义 `model_change` entry 的 v2 artifact 返回
`unsupported_legacy_entry`。

## 0.2.1 — 2026-07-16

完整合并 Sandbox local stable、Memory/Context/Session 与 DeepSeek-first Anthropic CLI，并修复 clean-wheel
`sandbox-real` 仍使用已删除文本工具协议的问题。发布门禁覆盖全量离线测试、distribution、真实 Docker Sandbox、
同机性能、DeepSeek 官方与 Lumina 第三方网关；distributed Product Enablement 仍不属于本地 stable 范围。

## 0.2.0 — 2026-07-15

Pony Sandbox 的首个本地稳定版。它是 pre-1.0 的单机交付，不代表 distributed production readiness。

### Added

- macOS arm64 + Docker Desktop + already-present exact `linux/arm64` image 的 sealed local authorization。
- Docker + filtered staging：所有模型可见文件工具与 shell 只操作 Execution Root，Source Root 通过 immutable diff
  和独立 Source Apply 写回。
- Apply 确认绑定 exact diff digest，并显示 source、数量、字节、变更分类与高风险摘要。
- 固定 DeepSeek Anthropic Messages 主路径，以及四种内部 native protocol adapter 的离线 wire contract。
- Anchored、bounded、atomic Workspace I/O，以及稳定的 unsafe/limit/CAS reason codes。

### Changed

- 公开 CLI 固定为 `deepseek-v4-flash`、Anthropic Messages、`x-api-key`，默认 API 根为
  `https://api.deepseek.com/anthropic/v1`。
- 第三方 Anthropic-compatible relay 只需替换 `PONY_API_URL`；客户端始终只追加 `/messages`。
- `memory_save` 只接受当前 top-level user request 的明确授权；历史授权不继承，delegate 不能写。
- InjectionSnapshot 直接使用结构化 source blocks，retry/tool-followup 复用同一 immutable snapshot。
- Staging 改为流式复制；shell 调用期 capture 使用可失效的进程内增量 cache，最终 diff 仍强制全量 capture。
- Watchdog 使用自适应扫描间隔，并在容器退出后强制最终 workspace measure。
- RepoMap 改为同步惰性构建和 atomic snapshot publish；长 Tool Result 不再向模型暴露 Host artifact path。
- CLI 与 `pony.toml` 的 token、timeout、step、Context 和 recall 参数增加系统上限与安全默认回退。

### Removed

- legacy SRT runtime、macOS/Linux adapters、toolchain package data、Provider/Profile/Connection resolver 和兼容别名。
- 未接线的 `ToolRegistry`/`ToolDefinition`。
- `pony.evaluation` 不再进入 runtime wheel；开发源码、benchmark、scripts 和 tests 仍保留在仓库。

### Security

- 修复最终 open 前 symlink/parent/root 交换导致的 Host workspace escape。
- 未授权 Memory 写入、结构化 injection 边界、非法/带凭证 URL 和模型 Session binding 漂移均 fail closed。
- Source staging 不整文件缓冲，跨 chunk known-secret 扫描和 source identity/mode 复验保持启用。

### Migration

本版本对模型配置执行硬切换，不读取或迁移旧 Provider/Profile/Connection 字段。项目 `.env` 必须显式保存
`PONY_API_URL` 与 `PONY_DEEPSEEK_API_KEY`；可通过 `pony init` 隐藏输入写入。旧变量单独存在时按未配置失败。

升级不迁移 Sandbox capture/diff schema，也不自动删除 `~/.pony` 中的旧数据。Linux、amd64、registry、KMS 和
distributed Product Enablement 继续 `NO-GO`。
