# CLI 安装、配置与更新

## 支持范围

- Python：3.11、3.12。
- Runtime dependencies：一个直接依赖 `prompt-toolkit`；锁定环境中同时安装其传递依赖 `wcwidth`。
- Host CLI：纯 Python，支持常规本地环境；Host 不是 OS sandbox。

## 从 PyPI 安装

`v1.0.0` tag 与对应 package 发布后，推荐在独立虚拟环境中安装；发布前请使用下方源码流程：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install pony-code==1.0.0
pony --version
pony --help
```

如果 shell 找不到 `pony`，先检查 `python -m pip --version` 和 `command -v python` 是否来自同一个环境，再检查
`command -v pony`。不要通过修改 Pony 源码解决 PATH 问题。

## 从源码安装

```bash
git clone https://github.com/xiawiie/pony-code.git
cd pony-code
uv sync --frozen --dev
uv run pony --version
```

`uv.lock` 是开发和 CI 的锁定真源。日常验证使用 `uv run ...`，不要向 runtime dependency 添加仅供测试或构建使用的包。

## 项目初始化

进入需要 Pony 操作的仓库根目录：

```bash
pony init
pony config show
pony doctor
```

`init` 依次询问 Provider（默认 `auto`）、API Base、模型和 API Key。输入已有 Key 时留空会保留
原值；本地 Ollama 允许空 Key。强制 Provider 只做本地校验；`auto` 或 `openai` 先执行 fixed synthetic
tool/continuation probe，只有完整通过后才把 resolved Provider 与另外三项一次性原子写入根目录 `.env`。
Probe 或写入失败不得留下部分更新。

也可以复制仓库提供的 `.env.example`：

```bash
cp .env.example .env
chmod 600 .env
```

然后编辑：

```dotenv
PONY_PROVIDER=anthropic
PONY_API_BASE=https://api.anthropic.com/v1
PONY_API_KEY=
PONY_MODEL=claude-sonnet-4-6
```

如只需安全更新 Key，可使用隐藏输入或标准输入：

```bash
pony config set-secret PONY_API_KEY
```

`.env` 规则：

- 只读取当前 lexical repository root，不搜索父目录或兄弟 worktree。
- 项目 `.env` 高于进程环境。
- 文件必须是普通 single-link private file；不安全文件会拒绝或进入 review-required 状态。
- 只解析键值，不执行 shell expansion，不把内容注入全局 `os.environ`。
- `PONY_DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等不配置 1.0 runtime。

## 交互与一次性入口

配置完成后，裸命令直接进入行内 TUI：

```bash
pony
```

以下三个入口有稳定且互不含糊的含义：

| 调用 | 含义 |
| --- | --- |
| `pony` | 默认交互 TUI |
| `pony repl` | 显式进入同一个交互会话，便于文档和排障 |
| `pony run "<prompt>"` | 执行一次请求并退出，适合脚本或 CI |

`pony "prompt"` 不会被当作隐式请求；首个未知 token 会返回 usage error 和接近命令建议。这保留了子命令扩展空间，
避免未来新增命令时改变旧脚本含义。

TUI 需要 stdin/stdout 同时为 TTY、`TERM` 不是 `dumb` 且终端至少 40 列，否则自动使用纯文本 REPL。颜色还会遵守
`--no-color` 和 `NO_COLOR`。输入 `/` 查看交互命令；`Ctrl+D` 退出，`Ctrl+C` 中断/清空，短时间内再次按下则退出。

### Permission mode 与 Plan

fresh Session 默认使用 `auto`。`pony run` 与 `pony repl` 可通过 `--permission-mode` 选择
`manual|auto|acceptEdits|bypassPermissions|dontAsk|plan`；该值只追加到当前 Session，不进入 `.env` 或 TOML。
`manual` 是公开名称，Session 内部将其存为 `default`。

| 模式 | 用户可见行为 |
| --- | --- |
| `manual` | 读操作直接执行；没有规则覆盖的变更显示一次性 permission prompt |
| `auto` | 本地确定性分类器自动执行内置编辑、明确授权的 Memory 保存和可证明安全的 shell；不确定时拒绝 |
| `acceptEdits` | 自动接受内置文件编辑；其他变更仍按规则决定或询问 |
| `bypassPermissions` | 绕过普通提示，但不绕过 trust、显式 deny、schema、路径、secret、可信 executable 或 mutation lock |
| `dontAsk` | 不询问；原本需要询问的变更直接拒绝，显式 allow 规则仍可执行 |
| `plan` | 只公开只读工具和 Plan 工具；离开 Plan 前展示精确内容与 revision 请求确认 |

`auto` 的用户操作方式与 Claude Code 同名模式对齐；Pony 当前使用本地确定性安全分类器，并不复刻 Claude 的内部
模型分类器。`/permissions` 可连续管理当前 Session 的 exact tool-name `allow`、`ask`、`deny` 规则并切换 mode；
`/allowed-tools` 是别名。CLI 的 `--allowed-tools` / `--allowedTools` 与
`--disallowed-tools` / `--disallowedTools` 写入同一种 exact-tool Session 规则。
交互 picker 还支持 `remove` 删除已有规则。一次性 `Approve once?` 只授权当前 Tool 调用，不会持久化为规则。

`bypassPermissions` 必须显式获得本次进程的危险 capability：

```bash
pony --permission-mode bypassPermissions \
  --allow-dangerously-skip-permissions run "apply the requested change"
pony --dangerously-skip-permissions run "apply the requested change"
```

`--allow-dangerously-skip-permissions` 本身不切换 mode；它允许本进程从 `/permissions` 选择 bypass，也允许恢复已持久化为
bypass 的 Session。第二种直接为当前 Session 选择 bypass。普通 bypass resume 必须重新提供 capability；显式使用
`--permission-mode` 改回其他 mode 不需要 dangerous flag。Capability 不写入 Session，Runtime 仍重复验证。permission
参数只适用于 `run/repl`，管理命令返回 usage error。

`/plan [description|open|share]` 进入或查看 Plan。首次 description 会提交规划请求；`open`/`share` 先进入 Plan，
空 artifact 只启用 mode。已有 artifact 时，`open` 通过 `$VISUAL`/`$EDITOR` 编辑并只在原 revision 未变化时保存；
本地 runtime 的 `share` 明确返回不可用。模型使用 `read_plan`、`write_plan` 和 `exit_plan_mode`；只有非空 Plan
通过精确内容与 revision 的一次性确认后，才恢复进入 Plan 前的 permission mode，并可在同一请求中继续实现。

显式交互 `--resume` 在首个 prompt 前显示一次 permission、checkpoint、resume state 与 Provider/model 摘要；
one-shot、JSON inspection 与管理命令保持无装饰输出。Session v1/v2/v3 只有在显式 resume 时迁移到 v4；其他
writer 返回 `session_migration_required`。

## Provider 切换

切换 Provider 时修改同一组变量，不创建 profile 或 connection 文件。

| Provider | Variant | 默认 URL | 默认认证 | Key |
| --- | --- | --- | --- | --- |
| Anthropic | `messages` | `https://api.anthropic.com/v1` | `x-api-key` | 必需 |
| `openai-responses` | `responses` | `https://api.openai.com/v1` | `bearer` | 必需 |
| `openai-chat` | `chat_completions` | `https://api.openai.com/v1` | `bearer` | 必需 |
| Ollama | `chat` | `http://127.0.0.1:11434` | `none` | 可空 |

`openai` 仅是 init/run/repl/doctor 可使用的 family selector；init 不会把它写作最终值。

切换后运行：

```bash
pony config show
pony doctor
pony doctor --check-api
```

最后一条会发送真实请求，可能收费。missing/auto/OpenAI family 可在发送用户任务前使用 fixed synthetic probe 解析协议；
Pony 不在真实用户请求失败后切换 Provider 或协议。
普通 benchmark 不探测；如 `config show` 仍显示 `probe_required`，先运行 `pony init`。收费 live harness 在 workload 前
调用与 CLI 相同的 resolver，并把 probe 调用与 workload 调用分开报告，不维护第二套识别逻辑。

## 更新

PyPI 安装：

```bash
python -m pip install --upgrade pony-code
pony --version
pony doctor
```

源码安装：

```bash
git pull --ff-only
uv sync --frozen --dev
uv run pony --version
uv run pony doctor
```

更新不会自动删除或迁移 `.pony/` 中的 Session、Run、Checkpoint、Memory 或旧 Sandbox 状态。执行前先阅读
[CHANGELOG](../CHANGELOG.md) 的 Migration 部分。

## 卸载

```bash
python -m pip uninstall pony-code
```

卸载 package 不会删除项目 `.env`、项目 `.pony/` 或用户目录 `~/.pony/`。这些目录可能包含凭证引用、Memory、
会话和恢复证据，应由用户在确认不再需要后单独处理。

## 旧 Sandbox 与 Checkpoint 数据

当前安装只提供 Host 执行。`--sandbox`、`pony sandbox`、Checkpoint restore/prune/resolve 和 `/rewind --workspace`
已经删除。升级不会自动清理旧 artifact；`pony checkpoints list/show/pending` 只读检查旧数据。旧 Sandbox-bound Session
resume 返回 `legacy_sandbox_session_unsupported`，不会静默切换到 Host。需要恢复文件时使用 Git 或外部备份。

## 常见失败

| 现象 | 检查 |
| --- | --- |
| `api_key_not_configured` | 云 Provider 是否设置 `PONY_API_KEY` |
| `provider_invalid` | Provider 是否为 `auto`、`openai`、`openai-chat`、`openai-responses`、`anthropic` 或 `ollama` |
| `provider_endpoint_conflict` | 强制 Provider 是否与 known API origin 冲突 |
| `provider_detection_failed` | endpoint/model 是否完成 native tool 与 continuation 合同；重跑 `pony doctor --check-api` |
| `provider_protocol_mismatch` | Provider 是否返回适配当前 protocol 的 tool call 结构 |
| `api_base_not_configured` | 是否设置 `PONY_API_BASE` |
| `insecure_api_base` | 非 loopback API Base 是否为 HTTPS |
| `model_session_mismatch` | 当前 Provider/model/URL 是否与恢复 Session 一致 |
| `legacy_sandbox_session_unsupported` | 该 Session 绑定旧 Sandbox；检查历史后创建新的 Host Session |
| `pony` 找不到 | 虚拟环境与 PATH 是否一致 |
| 裸 `pony` 仍显示旧 help | `command -v pony` / `pony --version` 是否指向旧安装；从当前版本重新安装或使用 `uv run pony` |
| 没有 TUI 颜色或菜单 | stdin/stdout、`TERM`、终端宽度、`NO_COLOR` / `--no-color` 是否触发纯文本或无色模式 |
