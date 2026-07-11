# Pico

Pico 是一个面向代码仓库的轻量本地 coding-agent harness。它从当前仓库构建上下文，让模型通过
受约束的工具读取、修改和验证文件，并把会话、运行证据与恢复记录保存在本地 `.pico/` 中。

Pico 适合排查测试失败、实施小步代码改动、审阅仓库和继续先前任务。它不是聊天 UI、IDE 或 OS
sandbox，也不会替代 Git。模型发起的复杂 shell 命令仍需要人类审批；恢复操作也必须由用户主动触发。

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
- 当前持久化格式是硬切合同，不提供旧格式 converter 或兼容 alias。
- 经审批的复杂 shell 是授权逃生口，不代表命令已被 OS 隔离。
- 真实 Provider 验证会产生网络请求和费用，必须单独授权；离线诊断不会连接 Provider。

## 维护者入口

- [领域语言与模块边界](CONTEXT.md)
- [架构](docs/architecture.md)
- [安全](docs/security.md)
- [恢复](docs/recovery.md)
- [验证](docs/verification.md)
- [Memory](docs/memory.md)

![Pico CLI help](assets/screenshots/pico-help.png)
