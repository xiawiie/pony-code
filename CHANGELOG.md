# Changelog

## 1.0.0 — 2026-07-16

Pony 1.0 将预发布仓库收束为一个可安装、可验证、可发布的本地 coding-agent 产品。

### Added

- 三个用户可见 Provider：Anthropic、OpenAI、Ollama；OpenAI 支持 Responses 与 Chat Completions 两个 Variant。
- 统一的四变量 `.env` 合同，以及能写全配置的交互式 `pony init`。
- 参考 Claude Code/Pi 交互习惯的行内 TUI：slash command menu、可增长多行输入、历史搜索、状态栏、审批卡片与
  Tool/Checkpoint activity。
- 参考 SuperHermes 马形轮廓重绘的黑白 Unicode TUI Logo，以及与其同步缩放的像素 `PONY CODE` 字标；状态语义色
  保持独立。
- `pony --version`、MIT License、完整 package metadata、Project URLs 与 tag-bound release workflow。
- PyPI Trusted Publishing、GitHub Release、SHA-256 release assets 和 clean-install distribution smoke。
- 本地 Sandbox 镜像构建/验证维护入口。

### Changed

- 裸 `pony` 现在直接进入交互 TUI；`pony repl` 保留为显式同义入口，`pony run <prompt...>` 与管理子命令继续使用
  生产分支的显式 CLI 合同。
- 删除旧式猫形欢迎卡；纯文本 fallback 不显示装饰性 banner，`pony run` 只输出执行结果。
- TUI 与纯文本 fallback 共用一个 REPL 输入处理器；`prompt-toolkit` 成为唯一直接 runtime dependency，distribution
  smoke 在隔离环境中离线验证锁定依赖和 TUI import。
- 产品代码按 `agent`、`cli`、`config`、`context`、`memory`、`providers`、`recovery`、`runtime`、`sandbox`、
  `security`、`state`、`tui`、`tools`、`workspace` 十四个领域包归位；`pony/` 顶层只保留 `__init__.py` 与
  `__main__.py`。
- Provider 连接统一使用 `PONY_PROVIDER`、`PONY_API_BASE`、`PONY_API_KEY`、`PONY_MODEL`；项目 `.env` 高于
  进程环境，Provider 与 API Base 静态决定内部 Transport 和认证方式。
- Provider factory 从 transport shared helpers 中独立，用户 Provider 与内部四种 wire transport 解耦。
- Evaluation 移至 `benchmarks/evaluation`，维护脚本按 evaluation/release/sandbox 分类。
- 全量检查入口同时覆盖 lock、Ruff、pytest、evaluation、build、archive 与隔离安装。
- Pony 装配、Context request、Shell approval、私有/Workspace 原子写、Source Apply 和 Sandbox Session 创建按职责拆分，
  保持原有 CAS、回滚和 fail-closed 语义。

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

### Migration

1.0 是模型配置硬切换。旧 `.env` 中的 `PONY_DEEPSEEK_API_KEY`、厂商 Key、Provider/Profile/Connection 字段不会
配置 runtime。运行 `pony init` 写入四个通用变量，或按 `.env.example` 手工迁移。切换 Provider、model、protocol
或 endpoint 后，旧 Session 可能因 Model Session Binding 不一致而返回 `model_session_mismatch`；应新建 Session。

升级不会自动删除 `.pony/` 或 `~/.pony/` 中的旧 Session、Run、Checkpoint、Memory 与 Sandbox artifact。

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
