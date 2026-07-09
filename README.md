# pico

`pico` 是一个面向代码仓库的轻量本地 coding agent。它直接跑在终端里，先看当前工作区，再用一组受约束的工具去读文件、改文件、跑命令，并把会话状态保存在本地 `.pico/` 目录里。

它更像一个能在仓库里持续工作的命令行助手，不是纯聊天窗口。你可以拿它做代码排查、测试修复、仓库分析，或者让它在当前项目里执行一次性的工程任务。

## 适合做什么

- 在本地仓库里排查测试失败
- 读取当前代码结构并给出修改建议
- 基于现有文件做小步迭代，而不是脱离仓库空想
- 在会话中保留上下文，支持继续上一次工作

## 主要特性

- 包名是 `pico`
- 推荐 CLI 命令是 `pico-cli`
- 兼容 CLI 命令 `pico` 仍保留，但在 macOS 上可能和系统自带的 `/usr/bin/pico` 编辑器重名
- 模块入口是 `python -m pico`
- 会话保存在 `.pico/sessions/`
- 每次运行的工件保存在 `.pico/runs/<run_id>/`
- 支持四类模型后端：
  - Ollama
  - OpenAI 兼容 Responses API
  - Anthropic 兼容 Messages API
  - DeepSeek Anthropic 兼容 API

## 使用截图

CLI 帮助信息：

![pico help](assets/screenshots/pico-help.png)

启动界面：

![pico start](assets/screenshots/pico-start.png)

REPL 内置命令与会话路径：

![pico repl](assets/screenshots/pico-repl.png)

## 安装

需要 Python 3.10+。

如果你用 `uv`，直接安装依赖：

```bash
uv sync
source .venv/bin/activate
```

如果你已经在自己的 Python 环境里工作，也可以直接装成可编辑模式：

```bash
pip install -e .
```

安装并激活对应环境后，可以直接使用 `pico-cli`。如果不想激活虚拟环境，在当前仓库里也可以把下面所有 `pico-cli ...` 命令写成 `uv run pico-cli ...`。

更完整的安装、激活和后续迭代说明见 [Pico CLI installation and update guide](docs/cli-installation-and-updates.md)。

## 快速开始

在当前仓库里启动交互模式。模型连接来自项目的 `pico.toml`：

```bash
pico-cli
```

指定另一个工作目录：

```bash
pico-cli --cwd /path/to/repo
```

直接跑一次性任务：

```bash
pico-cli run "inspect the test failures and propose a fix"
```

旧形式 `pico-cli "prompt"` 仍然兼容；新示例使用显式的 `run` 子命令，便于和交互、诊断、恢复等命令区分。旧入口 `pico` 也仍可用，但在 macOS 上可能和系统自带的 `/usr/bin/pico` 编辑器重名。

也可以通过模块入口启动：

```bash
python -m pico
```

## 模型后端

Pico 从项目根目录的 `pico.toml` 读取模型连接，并在启动时加载同目录 `.env` 里的密钥。`pico.toml` 保存模型名、base URL、API 类型和密钥变量名；真实 key 只放进 `.env`，不要提交。

本地第一次配置可以用 `init` 生成文件：

```bash
pico-cli init \
  --model deepseek-chat \
  --model-base-url https://api.deepseek.com/anthropic \
  --model-api-key-env DEEPSEEK_API_KEY \
  --model-api anthropic-messages
```

然后在 `.env` 写入对应变量：

```bash
DEEPSEEK_API_KEY="your-api-key"
```

这会生成等价的 `[model]` 配置：

```toml
[model]
name = "deepseek-chat"
base_url = "https://api.deepseek.com/anthropic"
api_key_env = "DEEPSEEK_API_KEY"
api = "anthropic-messages"
```

OpenAI Responses API 示例：

```bash
pico-cli init \
  --model gpt-5.4 \
  --model-base-url https://api.openai.com/v1 \
  --model-api-key-env OPENAI_API_KEY \
  --model-api openai-responses
```

OpenAI Chat Completions 兼容接口示例：

```bash
pico-cli init \
  --model qwen-max \
  --model-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model-api-key-env DASHSCOPE_API_KEY \
  --model-api openai-chat
```

本地 Ollama 示例：

```bash
ollama serve
ollama pull qwen3.5:4b
pico-cli init \
  --model qwen3.5:4b \
  --model-base-url http://127.0.0.1:11434 \
  --model-api ollama
```

支持的 `api` 值是：

- `anthropic-messages`
- `openai-responses`
- `openai-chat`
- `ollama`

如果有额外的敏感环境变量需要从 trace/report 里脱敏，可以用 `PICO_SECRET_ENV_NAMES` 配置逗号分隔的变量名，或启动时重复传 `--secret-env-name NAME`。

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：显示紧凑 working memory：先打印 `task:` 和 `recent:`，随后空一行，再以 `Memory files:` 列出 `.pico/memory/` 文件及字符数
- `/memory-review`：打印 `agent_notes.md` 内容以及编辑提示
- `/save <text>`：把一条 note 追加进 workspace 的 `agent_notes.md`
- `/session`：查看当前会话文件路径
- `/reset`：清空当前会话状态
- `/exit` 或 `/quit`：退出 REPL

## 记忆系统

Pico v2 把项目知识分成三处：`AGENTS.md` 里的项目约定、`.pico/memory/notes/*.md` 里的用户手写笔记、以及 `.pico/memory/agent_notes.md` 里 agent 显式追加的短笔记。用户笔记 agent 只读不写；agent 笔记只追加、原子写入。CLI 侧 `pico-cli memory list/show/search/review/migrate` 全部对齐这套模型，细节见 [`docs/memory-model.md`](docs/memory-model.md)。

## CLI Surface

常用命令入口如下：

- `pico-cli run [prompt...]`：执行一个 prompt，然后退出。
- `pico-cli repl`：启动交互式 REPL。
- `pico-cli help`：显示 CLI 帮助。
- `pico-cli status`：显示本地 harness 状态，不启动模型会话。
- `pico-cli doctor`：运行就绪诊断，包括 provider 连通性检查。
- `pico-cli doctor --offline`：只运行本地诊断，不检查 provider 连通性。
- `pico-cli config show`：显示最终生效的配置以及来源。
- `pico-cli runs list` / `pico-cli runs show <run-id>`：查看历史 run 列表或单个 run 详情。
- `pico-cli sessions list` / `pico-cli sessions show <session-id>`：查看历史 session 列表或单个 session 详情。
- `pico-cli checkpoints list` / `pico-cli checkpoints show <checkpoint-id>`：查看 checkpoint 列表或详情。
- `pico-cli checkpoints preview-restore <checkpoint-id>`：预览恢复 checkpoint 会带来的变化。
- `pico-cli checkpoints restore <checkpoint-id> --apply`：实际恢复 checkpoint。
- `pico-cli checkpoints prune` / `pico-cli checkpoints prune --older-than=7d --apply`：预览或实际清理 checkpoint。

这里的 checkpoint 指用户可恢复入口：一次用户请求产生的文件改动会聚合成一个 Turn Checkpoint；逐次工具调用的影响会作为内部 Tool Change Record 记录，并由 Turn Checkpoint 引用。

诊断和 inspection 命令需要机器可读输出时，可以在命令前加 `--format json`：

```bash
pico-cli --format json status
pico-cli --format json checkpoints list
```

恢复类命令默认先预览；`restore` 和 `prune` 只有显式传入 `--apply` 才会修改本地状态。

## 安全与持久化

`pico` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

- `--approval ask`
- `--approval auto`
- `--approval never`

每次运行结束后，都会在 `.pico/runs/<run_id>/` 下写出这些文件：

- `task_state.json`
- `trace.jsonl`
- `report.json`

这些内容默认只保存在本地，不需要跟仓库一起提交。

## 开发

常用本地检查：

```bash
./scripts/check.sh
```

这个脚本执行和 CI 相同的本地检查：

```bash
uv run ruff check .
uv run pytest -q
```

内部代码现在按较轻的边界拆分：`pico/evaluation/` 放 benchmark 和 metrics，`pico/providers/` 放模型 provider client，`pico/features/` 放可选运行时能力。新代码应直接使用这些包路径；旧的 `pico.evaluator`、`pico.metrics`、`pico.models` 和 `pico.memory` import 不再作为公共入口保留。
