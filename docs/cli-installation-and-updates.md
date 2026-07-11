# Pico CLI 安装与更新

本文只说明本地安装、环境解析、更新和 CLI 自救。命令语义与模块关系见[架构](architecture.md)。

## 在源码仓库中使用

Pico 需要 Python 3.11+。开发环境使用仓库已跟踪的 `uv.lock`：

```bash
cd /path/to/pico
uv sync --frozen --dev
source .venv/bin/activate
command -v pico
pico --help
```

`command -v pico` 应输出当前 `.venv/bin/pico`。macOS 可能已有系统同名程序，所以环境是否激活必须
以这条命令的结果为准。不能激活环境时使用：

```bash
uv run pico doctor --offline
uv run pico run "inspect the repository"
```

从构建产物做隔离安装时：

```bash
uv build --clear
python3 -m venv /tmp/pico-venv
source /tmp/pico-venv/bin/activate
python -m pip install --no-deps dist/pico-0.1.0-py3-none-any.whl
command -v pico
pico doctor --offline
```

## 项目配置与凭证

Pico 只读取当前 lexical repository root 的 `.env`。推荐先写非敏感配置，再通过 stdin 写 secret：

```bash
pico init
printf '%s' "$PROVIDER_KEY" | pico config set-secret PICO_DEEPSEEK_API_KEY --stdin
chmod 600 .env
pico doctor --offline
```

不要把真实 key 写入 shell history、命令参数、文档或测试 fixture。通用 `PICO_API_KEY` 只作为
DeepSeek、Anthropic-compatible 与 OpenAI-compatible 各自 resolver 的共享 fallback；Provider 不会借用
另一个 Provider 的专用 key。Ollama 不需要 API key。

配置优先级为显式 CLI 参数、Project Environment、当前进程环境、代码默认值。`pico.toml` 只用
stdlib TOML parser 在一个 Pico 实例构造时读取一次；malformed 文件整份回退默认值。

## 更新

源码更新后重新同步锁定环境：

```bash
git pull --ff-only
uv lock --check
uv sync --frozen --dev
pico doctor --offline
```

修改 `pyproject.toml`、切换分支或 console entry 变化后必须重新同步。不要手工修改 `.venv/bin/pico`。
当前项目没有运行时第三方依赖，但 dev tools 仍由 lock 冻结。

## 本地自救

如果命令解析错误：

```bash
deactivate 2>/dev/null || true
source /path/to/pico/.venv/bin/activate
command -v pico
python -m pico --help
```

如果环境损坏，可删除并重建生成的 `.venv`，然后重新执行 `uv sync --frozen --dev`。这不会删除
仓库 `.pico/`、`~/.pico/` 或 recovery backup。

如果 `doctor --offline` 报告 `review_required`，先检查 `.env` 权限、trusted executable、private store
和 pending recovery evidence。不要通过降低权限检查或删除记录来让诊断变绿；处理方法见
[安全](security.md)与[恢复](recovery.md)。
