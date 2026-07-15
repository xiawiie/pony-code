# Pico

Pico 是一个面向代码仓库的轻量本地 coding-agent harness。它从当前仓库构建上下文，让模型通过
受约束的工具读取、修改和验证文件，并把会话、运行证据与恢复记录保存在本地 `.pico/` 中。

Pico 适合排查测试失败、实施小步代码改动、审阅仓库和继续先前任务。它不是聊天 UI 或 IDE，也不会替代 Git。
默认运行保持 Host 行为。Docker + filtered staging 架构已由 ADR-0040 接受；ADR-0042允许在当前安装树、
packaged image合同和本机Docker均精确验证时用sealed local authorization显式运行`--sandbox`。任一验证失败都在
Provider/target前fail closed，且不会回退Host runner。

## 安装

需要 Python 3.11+。在源码仓库中推荐使用锁定的 uv 环境：

```bash
uv sync --frozen --dev
source .venv/bin/activate
command -v pico
pico --help
```

激活环境后，`command -v pico` 必须指向该环境的 `bin/pico`，而不是系统中的同名程序。不激活环境时
可以使用 `uv run pico ...`。安装、更新与 PATH 排查见
[CLI 安装与更新](docs/cli-installation-and-updates.md)。

## 最短使用路径

在仓库根目录创建非敏感配置，并通过安全输入写入凭证：

```bash
pico init
pico config set-secret PICO_DEEPSEEK_API_KEY --stdin
pico doctor --offline
```

显式运行一次任务或进入交互模式：

```bash
pico run "inspect the failing tests and make the smallest safe fix"
pico repl
python -m pico repl
```

唯一 console command 是 `pico`。不带命令时显示帮助；一次性任务必须使用 `pico run`。

## 能力与限制

- 支持 DeepSeek、Anthropic-compatible、OpenAI-compatible 与 Ollama transport。
- Canonical Messages 是唯一会话 transcript；run、trace、checkpoint 和 tool-change 都保留本地证据。
- 文件访问、私有存储、secret redaction、shell policy 和恢复冲突检查是 runtime 边界的一部分。
- Memory 分为用户维护的 User Notes 和 agent 追加的 Agent Notes。
- SRT 路线已被 ADR-0040 supersede。目标 Sandbox 只支持用户已安装的 macOS Docker Desktop 或 Linux local
  rootless Docker，以 exact managed image、container 外断网和 filtered staging 运行；它不是 microVM 或
  hostile multi-tenant boundary，运行时也不会隐式 pull。
- Sandbox 中模型工具只修改 staging；Source Root 只有在 Session 结束后审查同一 immutable diff并单独批准
  Source Apply Transaction才会改变。本机授权不自动apply，也不接受custom image或host fallback。
- 当前本机MVP只支持packaged record与already-present exact image匹配的平台。production trust root/KMS、registry、
  `linux/amd64`与D7的92+4真实artifact仍缺失，因此分布式发布保持`NO-GO`，不冒充四平台GA。
- 当前 runtime 只读取硬切合同；旧 OBS/Tool Change 仅通过显式事务化 `pico migrate` converter 升级，不保留兼容 alias。
- 使用 `pico runs summary latest` 查看最近一次 Run 的低敏感摘要；指定 Run 时将 `latest` 替换为 `run_id`。
- 未显式启用 Sandbox 时，经审批的复杂 shell 仍具有当前用户权限；本机Sandbox是受限container边界，不是
  microVM或hostile multi-tenant隔离。
- 真实 Provider 验证会产生网络请求和费用，必须单独授权；离线诊断不会连接 Provider。

## 维护者入口

- [领域语言与模块边界](CONTEXT.md)
- [架构](docs/architecture.md)
- [安全](docs/security.md)
- [恢复](docs/recovery.md)
- [验证](docs/verification.md)
- [Memory](docs/memory.md)

![Pico CLI help](assets/screenshots/pico-help.png)
