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

模型和 Context 可显式配置：

```toml
[model]
context_window = 128000
output_limit = 16384

[context]
system_tools_hard_cap = 24576
source_pool_tokens = 16384

[context.compaction]
enabled = true
reserve_tokens = 16384
keep_recent_tokens = 20000
```

CLI 的当前名称是 `--context-window` 和 `--max-output-tokens`。旧 `--max-new-tokens` 只作为带 warning 的迁移
alias；`history_soft_cap`、`history_floor_messages` 和 `injection_budget_ratio` 已移除。

## Session 与长任务

```bash
pico session inspect <session-id>
pico session tree <session-id>
pico session compact <session-id> [focus]
pico session checkpoint <session-id> [label]
pico session fork <session-id> <entry-id>
pico session rewind <session-id> <entry-or-checkpoint-id> [--summary] [--workspace --yes]
pico session clone <session-id> --to-worktree <path>
pico session tail-repair <session-id> --yes
```

`pico sessions list/show` 是兼容的只读摘要入口；`pico session` 提供 Session Tree 操作。旧 JSON Session 只有
显式 resume 才自动迁移，inspection 不写磁盘。普通 rewind 不改工作区；`--workspace` 总是先 preview，并且只
接受合法 task checkpoint。详细预算、compaction、worktree 和 crash reconciliation 见
[Context、Session 与长会话](context-and-sessions.md)。

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

## Docker Sandbox

普通`run/repl`不下载Sandbox。显式`--sandbox`每次生成sealed local authorization并验证当前安装树、packaged
image合同和本机Docker；任一失败都发生在Provider、Session staging和target之前，且不会回退Host。状态命令可用：

```bash
pico --format json sandbox status
pico --format json sandbox list
pico --format json sandbox inspect <sandbox-id>
pico --format json sandbox diff <sandbox-id>
pico --format json sandbox prune --dry-run
```

`status/list/inspect/diff/prune --dry-run`不联网、不创建state root或lock、不reconcile，也不启动container。
`status/list`同时报告active/pending/cleanup-pending数量、当前已验证staging bytes、oldest age和orphan/
reconciliation计数。unknown state只计数且不公开path；它会阻止`prune --apply`。

本机MVP的`pico sandbox prepare`只检查already-present exact image：不下载Product Enablement、不pull或build、
不写release cache，返回`network_performed=false`与`mutation_performed=false`。当前image-set只有`linux/arm64`
本地记录；`linux/amd64`与registry-backed distributed release仍延期。运行时不会隐式pull、build、repair或读取
用户Docker config。

`PICO_SANDBOX_CANDIDATE_ATTESTATION`与`PICO_SANDBOX_CANDIDATE_NONCE`仅由release controller用于四平台最终
public smoke；candidate不可下载、不可缓存、不可正式启用产品，用户不应配置这两个变量。

Session结束后，有变更的staging进入`pending_review`；无变更自动discard。写回Source Root和丢弃staging都需
单独显式操作，`--yes`只跳过CLI确认，不跳过CAS、identity或policy校验：

```bash
pico sandbox apply <sandbox-id>
pico sandbox discard <sandbox-id>
pico sandbox prune --apply
```

Apply崩溃后若external authority仍在，或原Source Root整体被替换导致普通Session inventory无法定位状态，使用：

```bash
pico --cwd <原 lexical Source Root> sandbox reconcile --yes
```

该命令只从external authority O(1)定位exact Sandbox state与journal并收敛到
`review_required/apply_review_required`；它不扫描猜测、不自动apply/rollback，也不放宽identity校验。没有`--yes`
时必须交互确认；`--no-input`不会代替确认。只读`status/list/inspect/diff/prune --dry-run`永远不会隐式执行它。

旧SRT `install/repair/export-bundle/import-bundle`命令已删除且返回usage error。legacy package data和offline
verifier只在registry production vertical通过前保留为迁移审计面，不是支持接口或交付指引。

Sandbox始终explicit-on。本机MVP只声明exact local image匹配的平台，不声明GA。historical D1-v1 Development
Gate不证明D7 Corpus V2；production key/KMS、registry双架构image、D6真实
vertical、Git distribution authority、独立Review、D7的92+4个四目标artifact和detached Product Enablement均未
完成；当前任何目标平台都不得标记为GA。

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
