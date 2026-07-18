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
- 本地 Sandbox 镜像构建/验证维护入口。
- Session v3 的 `plan/act/review` Workflow Mode、bounded Active Plan、`update_plan`、`/mode`、`/plan` 与
  run/repl-only `--mode`。
- v1/v2/v3 Session inspection、`session inspect latest`、显式 resume migration，以及 TUI/plain 一次性 Resume 卡和
  active Canonical prompt history。
- 可省略的 `PONY_PROVIDER`、OpenAI family selector、`openai-chat`/`openai-responses` 强制值，以及发送真实任务前的
  bounded synthetic Provider resolution；`pony init` 可持久化结果，doctor 保持只读。

### Changed

- 裸 `pony` 现在直接进入交互 TUI；`pony repl` 保留为显式同义入口，`pony run <prompt...>` 与管理子命令继续使用
  生产分支的显式 CLI 合同。
- 删除旧式猫形欢迎卡；完整 TUI 固定显示 5/7/11 行响应式马形 Logo 与像素字标，纯文本 fallback 不显示装饰性
  banner，`pony run` 只输出执行结果。
- TUI 与纯文本 fallback 共用一个 REPL 输入处理器；`prompt-toolkit` 成为唯一直接 runtime dependency，distribution
  smoke 在隔离环境中离线验证锁定依赖和 TUI import。
- TUI 运行事件收束为瞬态 `Working…`、单行 Tool 摘要和明确的失败/中断；自动 checkpoint 不再进入对话区，footer
  不再显示绝对路径、Session ID、API Base 或 checkpoint ID。Provider reasoning 与 streaming 不属于 1.0 展示面。
- 产品代码按 `agent`、`cli`、`config`、`context`、`memory`、`providers`、`recovery`、`runtime`、`sandbox`、
  `security`、`state`、`tui`、`tools`、`workspace` 十四个领域包归位；`pony/` 顶层只保留 `__init__.py` 与
  `__main__.py`。
- Provider 连接最多使用 `PONY_PROVIDER`、`PONY_API_BASE`、`PONY_API_KEY`、`PONY_MODEL`；项目 `.env` 高于
  进程环境。强制 Provider 静态路由；missing/auto/OpenAI family 可在发送真实任务前做 bounded
  synthetic resolution。
- Provider factory 从 transport shared helpers 中独立，用户 Provider 与内部四种 wire transport 解耦。
- Evaluation 移至 `benchmarks/evaluation`，维护脚本按 evaluation/release/sandbox 分类。
- 全量检查入口同时覆盖 lock、Ruff、pytest、evaluation、build、archive 与隔离安装。
- Pony 装配、Context request、Shell approval、私有/Workspace 原子写、Source Apply 和 Sandbox Session 创建按职责拆分，
  保持原有 CAS、回滚和 fail-closed 语义。
- 每个 top-level turn 冻结 Mode、Plan context 和模型可见 tool schemas；固定 prefix 不再列举 native tools，Executor
  仍在 approval 前执行同一 Mode ceiling。Workflow working set 与 checkpoint 使用独立 required chunk。
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
- `run_shell` native schema 和 missing-executable 拒绝只列出本轮已冻结的可信 executable 名称，不暴露路径，便于模型在
  identity/approval/policy 不放宽的前提下选择真实可执行入口。
- G7 `sandbox-real` 分开验证 readiness 与真实 Docker security/apply vertical；覆盖 no-network、secret/mount 隔离、
  timeout/output cleanup、final capture、CAS conflict、敏感 diff 和 resume 状态机，避免把镜像就绪冒充纵向通过。

### Removed

- 固定 DeepSeek-first 公开路径，以及旧 `PONY_DEEPSEEK_API_KEY` runtime 兼容读取。
- 未发布的 distributed Sandbox authority、candidate、product enablement、aggregate/release controller 与远程 cache/download 路径。
- 重复的 `examples/mini-pony` 实现和过期的分布式 Sandbox 规格文档。
- Evaluation 从 runtime wheel/sdist 中完全移除。
- 无运行时调用的 NetworkControl、sandbox governance evaluator、Provider compatibility 分支、Fake Provider 产品模块、
  `/save`、`--max-new-tokens`、未引用截图与 `MANIFEST.in`。

### Security

- TUI renderer 只在 durable trace 写入后收到副本；审批 UI 异常 fail closed，离开 TUI 后恢复原 runtime hook。
- Sandbox 只保留每次重算的 sealed local authorization；公开 runtime 仅接受 `local`，失败不回退 Host。
- Provider URL 继续拒绝 userinfo、query、fragment、凭证和非 loopback HTTP；Ollama `auth=none` 是显式例外。
- 通用 `PONY_API_KEY` 纳入项目环境优先级、diagnostics 脱敏、runtime redaction 与安全测试。
- Plan 在脱敏前执行 strict 12 KiB/schema/secret validation；Session v1/v2 inspection 零写，迁移发布前复验
  source、backup、candidate identity 与 exact bytes。
- Sandbox 只复用唯一 `ready` staging；未审查/cleanup 未决状态继续阻断，完整 `applied/discarded` 历史保持不可变并在
  同一 Pony Session resume 时从当前 Source Root 创建清除旧 recovery/freshness 绑定的新 staging。

### Migration

1.0 是模型配置硬切换。旧 `.env` 中的 `PONY_DEEPSEEK_API_KEY`、厂商 Key、Provider/Profile/Connection 字段不会
配置 runtime。运行 `pony init` 写入四个通用变量，或按 `.env.example` 手工迁移。切换 Provider、model、protocol
或 endpoint 后，旧 Session 可能因 Model Session Binding 不一致而返回 `model_session_mismatch`；应新建 Session。

升级不会自动删除 `.pony/` 或 `~/.pony/` 中的旧 Session、Run、Checkpoint、Memory 与 Sandbox artifact。
Session v1 JSON 和 v2 JSONL 只在 CLI `--resume` 或 `Pony.from_session()` 时迁移到 v3；其他 writer 返回
`session_migration_required`。含未定义 `model_change` entry 的 v2 artifact 返回 `unsupported_legacy_entry`。

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
