# CLI 安装、配置与更新

## 支持范围

- Python：3.11、3.12。
- Runtime dependencies：一个直接依赖 `prompt-toolkit`；锁定环境中同时安装其传递依赖 `wcwidth`。
- Host CLI：纯 Python，支持常规本地环境；Host 不是 OS sandbox。
- Docker Sandbox：1.0 仅支持 macOS arm64 + Docker Desktop + already-present exact image。

## 从 PyPI 安装

推荐在独立虚拟环境中安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install pico==1.0.0
pico --version
pico --help
```

如果 shell 找不到 `pico`，先检查 `python -m pip --version` 和 `command -v python` 是否来自同一个环境，再检查
`command -v pico`。不要通过修改 Pico 源码解决 PATH 问题。

## 从源码安装

```bash
git clone https://github.com/xiawiie/pico.git
cd pico
uv sync --frozen --dev
uv run pico --version
```

`uv.lock` 是开发和 CI 的锁定真源。日常验证使用 `uv run ...`，不要向 runtime dependency 添加仅供测试或构建使用的包。

## 项目初始化

进入需要 Pico 操作的仓库根目录：

```bash
pico init
pico config show
pico doctor
```

`init` 依次询问 API Base、模型和 API Key，将三个通用变量原子写入根目录 `.env`。输入已有 Key 时，留空会
保留原值；本地 Ollama 允许空 Key。该命令不联网。

也可以复制仓库提供的 `.env.example`：

```bash
cp .env.example .env
chmod 600 .env
```

然后编辑：

```dotenv
PICO_API_BASE=https://api.anthropic.com/v1
PICO_API_KEY=
PICO_MODEL=claude-sonnet-4-6
```

如只需安全更新 Key，可使用隐藏输入或标准输入：

```bash
pico config set-secret PICO_API_KEY
```

`.env` 规则：

- 只读取当前 lexical repository root，不搜索父目录或兄弟 worktree。
- 项目 `.env` 高于进程环境。
- 文件必须是普通 single-link private file；不安全文件会拒绝或进入 review-required 状态。
- 只解析键值，不执行 shell expansion，不把内容注入全局 `os.environ`。
- `PICO_DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等不配置 1.0 runtime。

## 交互与一次性入口

配置完成后，裸命令直接进入行内 TUI：

```bash
pico
```

以下三个入口有稳定且互不含糊的含义：

| 调用 | 含义 |
| --- | --- |
| `pico` | 默认交互 TUI |
| `pico repl` | 显式进入同一个交互会话，便于文档和排障 |
| `pico run "<prompt>"` | 执行一次请求并退出，适合脚本或 CI |

`pico "prompt"` 不会被当作隐式请求；首个未知 token 会返回 usage error 和接近命令建议。这保留了子命令扩展空间，
避免未来新增命令时改变旧脚本含义。

TUI 需要 stdin/stdout 同时为 TTY、`TERM` 不是 `dumb` 且终端至少 40 列，否则自动使用纯文本 REPL。颜色还会遵守
`--no-color` 和 `NO_COLOR`。输入 `/` 查看交互命令；`Ctrl+D` 退出，`Ctrl+C` 中断/清空，短时间内再次按下则退出。

## Provider 切换

切换 Provider 时修改同一组变量，不创建 profile 或 connection 文件。

| Provider | Variant | 默认 URL | 默认认证 | Key |
| --- | --- | --- | --- | --- |
| Anthropic | `messages` | `https://api.anthropic.com/v1` | `x-api-key` | 必需 |
| OpenAI | `responses` | `https://api.openai.com/v1` | `bearer` | 必需 |
| OpenAI-compatible | `chat_completions` | 用户显式填写 | 通常 `bearer` | 必需 |
| Ollama | `chat` | `http://127.0.0.1:11434` | `none` | 可空 |

切换后运行：

```bash
pico config show
pico doctor
pico doctor --check-api
```

最后一条会发送真实请求，可能收费。Pico 不自动探测模型、端点或协议，不在请求失败后切换 Provider。

## 更新

PyPI 安装：

```bash
python -m pip install --upgrade pico
pico --version
pico doctor
```

源码安装：

```bash
git pull --ff-only
uv sync --frozen --dev
uv run pico --version
uv run pico doctor
```

更新不会自动删除或迁移 `.pico/` 中的 Session、Run、Checkpoint、Memory 或 Sandbox 状态。执行前先阅读
[CHANGELOG](../CHANGELOG.md) 的 Migration 部分。

## 卸载

```bash
python -m pip uninstall pico
```

卸载 package 不会删除项目 `.env`、项目 `.pico/` 或用户目录 `~/.pico/`。这些目录可能包含凭证引用、Memory、
会话和恢复证据，应由用户在确认不再需要后单独处理。

## Sandbox 准备

```bash
pico sandbox status
pico sandbox prepare
```

两条命令都不会 pull、build、repair 或下载镜像。维护者需要构建本地开发镜像时使用：

```bash
uv run python scripts/sandbox/build_image.py --help
uv run python scripts/sandbox/verify_runtime.py --help
```

构建脚本是维护入口，不会扩大公开 runtime 的平台支持范围。

## 常见失败

| 现象 | 检查 |
| --- | --- |
| `api_key_not_configured` | 云 Provider 是否设置 `PICO_API_KEY` |
| `api_base_not_configured` | 是否设置 `PICO_API_BASE` |
| `insecure_api_base` | 非 loopback API Base 是否为 HTTPS |
| `model_session_mismatch` | 当前 Provider/model/URL 是否与恢复 Session 一致 |
| Sandbox platform error | 是否为 macOS arm64 与受支持 Docker endpoint |
| `pico` 找不到 | 虚拟环境与 PATH 是否一致 |
| 裸 `pico` 仍显示旧 help | `command -v pico` / `pico --version` 是否指向旧安装；从当前版本重新安装或使用 `uv run pico` |
| 没有 TUI 颜色或菜单 | stdin/stdout、`TERM`、终端宽度、`NO_COLOR` / `--no-color` 是否触发纯文本或无色模式 |
