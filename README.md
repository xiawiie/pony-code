# Pico

Pico 是一个面向代码仓库的轻量本地 coding-agent harness。它从当前仓库构建上下文，让模型通过受约束的工具
读取、修改和验证文件，并把会话、运行证据与恢复记录保存在本地 `.pico/` 中。

Pico 适合排查测试失败、实施小步代码改动、审阅仓库和继续先前任务。它不是聊天 UI 或 IDE，也不会替代 Git。
默认 Host 模式不是 OS sandbox；显式 Sandbox 采用 Docker + filtered staging，并在任何身份或 readiness 失败时
fail closed，不回退 Host runner。

## 安装

需要 Python 3.11+。在源码仓库中推荐使用锁定的 uv 环境：

```bash
uv sync --frozen --dev
source .venv/bin/activate
command -v pico
pico --help
```

`command -v pico` 必须指向当前环境的 `bin/pico`。不能激活环境时使用 `uv run pico ...`。完整安装、更新和 PATH
排查见 [CLI 安装与更新](docs/cli-installation-and-updates.md)。

## 最短使用路径

在仓库根目录交互配置 API URL 与凭证：

```bash
pico init
pico doctor
```

`init` 只在本地校验并原子写入 `.env`，不会联网。URL 留空时使用
`https://api.deepseek.com/anthropic/v1`，API Key 通过隐藏输入保存。Pico 固定使用
`deepseek-v4-flash`、Anthropic Messages 与 `x-api-key` 认证；第三方服务必须提供相同协议，并输入已经包含
版本前缀的精确 API 根，例如 Lumina 使用 `https://lumina.tripo3d.com/v1`。
普通 `doctor` 不联网；只有显式执行 `pico doctor --check-api` 才会验证文本、工具调用和 tool result 续接，
并可能产生少量费用。

显式运行一次任务或进入交互模式：

```bash
pico run "inspect the failing tests and make the smallest safe fix"
pico repl
```

唯一 console command 是 `pico`。不带命令时显示帮助；一次性任务必须使用 `pico run`。

Session 使用 append-only JSONL Tree 保存 Canonical Messages、工具交换、compaction 和 checkpoint。`pico repl`
中的 `/tree`、`/compact`、`/checkpoint`、`/rewind` 用于检查和控制当前分支；恢复文件修改时使用
`pico checkpoint restore --workspace`，不会把会话回退与工作区写入混为一谈。

## 模型 API

公开 CLI 只有一条模型路径：`deepseek-v4-flash` + Anthropic Messages + `x-api-key`。`PICO_API_URL` 是已经包含
版本前缀的精确 API 根，客户端只追加 `/messages`；API Key 只读取 `PICO_DEEPSEEK_API_KEY`。项目 `.env` 优先于
进程环境，旧 Provider/Profile/Connection 变量不会激活运行时。

第三方网关通过替换 `PICO_API_URL` 接入，但必须实现相同协议和认证。运行时不按域名或模型推断能力，不探测候选
路径，也不自动切换协议或模型。OpenAI Responses、OpenAI Chat Completions 与 Ollama client 仅作为内部实现和
测试对象保留，不接入公开 CLI。

## Sandbox 本地稳定版

v0.2.0 唯一正式支持的 Sandbox 平台是 macOS arm64 + Docker Desktop + already-present exact
`linux/arm64` image。其他平台的公开 `pico --sandbox run/repl` 返回
`sandbox_local_platform_not_released`。Linux、amd64、registry、KMS 和 distributed Product Enablement 均为
`NO-GO`；这不是四平台 GA，也不是 hostile multi-tenant/microVM 边界。

```bash
pico sandbox status
pico sandbox prepare
pico --sandbox run "inspect and fix the failing test"
pico sandbox diff <sandbox-id>
pico sandbox apply <sandbox-id>
```

`status` 和 `prepare` 不联网、不 pull/build/repair，也不隐式修改状态。Sandbox 中 RepoMap、Context、`read_file`、
`write_file`、`patch_file`、`list_files`、search 和 shell 等所有模型可见文件能力都只面向 filtered staging；
Source Root 不挂载进容器。Session 结束后先生成 immutable redacted diff，只有用户审查同一 exact digest 并单独批准
Source Apply，Source Root 才可能改变。`--yes` 不能跳过 artifact 加载、digest 绑定或 CAS。

## Memory 与本地证据

Memory 分为用户维护的 User Notes 和 agent 追加的 Agent Notes。`memory_save` 只接受当前用户请求中的明确授权，
历史授权不会继承，delegate 不能写。自动召回的 Memory 会进入当次模型请求；配置远程 Provider 时会被发送到该
endpoint。Memory 还可能留存在 Agent Notes、Tool Change、checkpoint、recovery 和其他本地私有审计 artifact 中，
因此不要把“删除当前 note”等同于清除所有历史副本。

Canonical Messages 是唯一会话 transcript；run、trace、checkpoint 和 Tool Change 保存可恢复、可审查证据。
使用 `pico runs summary latest` 查看最近 Run 的低敏感摘要。真实 Provider 验证会产生网络请求和费用，必须针对
exact HEAD 单独授权；离线诊断不会连接 Provider。

## 维护者入口

- [本地稳定版执行与验收](docs/local-stable-execution.md)
- [领域语言与模块边界](CONTEXT.md)
- [架构](docs/architecture.md)
- [Context、Session 与长会话](docs/context-and-sessions.md)
- [安全](docs/security.md)
- [恢复](docs/recovery.md)
- [验证](docs/verification.md)
- [Memory](docs/memory.md)
- [ADR-0040：Docker + filtered staging](docs/adr/0040-docker-filtered-staging.md)
- [ADR-0041：distributed release authority](docs/adr/0041-distributed-release-authority.md)
- [ADR-0042：sealed local authorization](docs/adr/0042-sealed-local-authorization.md)
