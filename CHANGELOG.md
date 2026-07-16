# Changelog

## 0.2.0 — 2026-07-15

Pico Sandbox 的首个本地稳定版。它是 pre-1.0 的单机交付，不代表 distributed production readiness。

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
- 第三方 Anthropic-compatible relay 只需替换 `PICO_API_URL`；客户端始终只追加 `/messages`。
- `memory_save` 只接受当前 top-level user request 的明确授权；历史授权不继承，delegate 不能写。
- InjectionSnapshot 直接使用结构化 source blocks，retry/tool-followup 复用同一 immutable snapshot。
- Staging 改为流式复制；shell 调用期 capture 使用可失效的进程内增量 cache，最终 diff 仍强制全量 capture。
- Watchdog 使用自适应扫描间隔，并在容器退出后强制最终 workspace measure。
- RepoMap 改为同步惰性构建和 atomic snapshot publish；长 Tool Result 不再向模型暴露 Host artifact path。
- CLI 与 `pico.toml` 的 token、timeout、step、Context 和 recall 参数增加系统上限与安全默认回退。

### Removed

- legacy SRT runtime、macOS/Linux adapters、toolchain package data、Provider/Profile/Connection resolver 和兼容别名。
- 未接线的 `ToolRegistry`/`ToolDefinition`。
- `pico.evaluation` 不再进入 runtime wheel；开发源码、benchmark、scripts 和 tests 仍保留在仓库。

### Security

- 修复最终 open 前 symlink/parent/root 交换导致的 Host workspace escape。
- 未授权 Memory 写入、结构化 injection 边界、非法/带凭证 URL 和模型 Session binding 漂移均 fail closed。
- Source staging 不整文件缓冲，跨 chunk known-secret 扫描和 source identity/mode 复验保持启用。

### Migration

本版本对模型配置执行硬切换，不读取或迁移旧 Provider/Profile/Connection 字段。项目 `.env` 必须显式保存
`PICO_API_URL` 与 `PICO_DEEPSEEK_API_KEY`；可通过 `pico init` 隐藏输入写入。旧变量单独存在时按未配置失败。

升级不迁移 Sandbox capture/diff schema，也不自动删除 `~/.pico` 中的旧数据。Linux、amd64、registry、KMS 和
distributed Product Enablement 继续 `NO-GO`。
