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
uv run pico doctor
uv run pico run "inspect the repository"
```

从构建产物做隔离安装时：

```bash
uv build --clear
python3 -m venv /tmp/pico-venv
source /tmp/pico-venv/bin/activate
python -m pip install --no-deps dist/pico-0.2.0-py3-none-any.whl
command -v pico
pico doctor
```

## 项目配置与凭证

Pico 只读取当前 lexical repository root 的 `.env`。推荐通过交互式初始化同时配置精确 API 根和凭证：

```bash
pico init
chmod 600 .env
pico doctor
```

`init` 依次询问 API URL 和 API Key。URL 留空时使用 `https://api.deepseek.com/anthropic/v1`；Key 使用隐藏输入，已有 Key
时直接回车即可保留。`init` 只做本地校验和原子写入，不发送 API 请求。若只需轮换凭证，可运行：

```bash
pico config set-secret PICO_DEEPSEEK_API_KEY
```

最终模型配置只有：

```dotenv
PICO_API_URL=https://api.deepseek.com/anthropic/v1
PICO_DEEPSEEK_API_KEY=
```

模型固定为 `deepseek-v4-flash`，协议固定为 Anthropic Messages，认证固定为 `x-api-key`。`PICO_API_URL`
必须是精确、已版本化 API 根，Pico 只追加 `/messages`，不自动补 `/v1`。第三方服务必须兼容相同协议和认证；
例如 Lumina 的 Anthropic 根应写为 `https://lumina.tripo3d.com/v1`。
URL 禁止 query、fragment、userinfo 或嵌入凭据；除 loopback 外只允许 HTTPS。

项目 `.env` 优先于当前进程环境；只读取上述两个运行时变量，不回退标准厂商 Key 或旧 Pico 配置。
不要把真实 Key 写入 shell history、命令参数、文档或测试 fixture。普通 `pico doctor` 不联网；仅
`pico doctor --check-api` 执行可能产生费用的文本、工具调用与 tool result 续接验证。

模型预算和 Context 可以通过 `pico.toml` 调整，不改变固定 API 协议：

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

CLI 的当前名称是 `--context-window` 和 `--max-output-tokens`；Context history 只通过 compaction 退出 active
request，不按旧 soft cap 静默丢弃。

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

`pico sessions list/show` 是只读摘要入口；`pico session` 提供 Session Tree 操作。旧 JSON Session 只有显式 resume
才自动迁移，inspection 不写磁盘。普通 rewind 不改工作区；`--workspace` 总是先 preview，并且只接受合法 task
checkpoint。详细合同见 [Context、Session 与长会话](context-and-sessions.md)。

## 更新

源码更新后重新同步锁定环境：

```bash
git pull --ff-only
uv lock --check
uv sync --frozen --dev
pico doctor
```

修改 `pyproject.toml`、切换分支或 console entry 变化后必须重新同步。不要手工修改 `.venv/bin/pico`。
当前项目没有运行时第三方依赖，但 dev tools 仍由 lock 冻结。

## Docker Sandbox

普通 `run/repl` 不下载 Sandbox。v0.2.0 的公开 `--sandbox run/repl` 只在 macOS arm64 可用；其他宿主返回
`sandbox_local_platform_not_released`。受支持宿主每次生成 sealed local authorization，并验证当前安装树、
packaged image 合同、already-present exact `linux/arm64` image 和 Docker Desktop；任一失败都发生在 Provider、
Session staging 和 target 之前，且不会回退 Host。状态命令可用：

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
本地记录；`linux/amd64`、Linux GA与registry-backed distributed release仍延期。运行时不会隐式pull、build、
repair或读取用户Docker config。

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

legacy SRT 模块、package data、platform adapters 和 offline-bundle verifier 已从 v0.2.0 wheel 删除，不保留兼容
alias。开发面的 `pico.evaluation` 也不进入 runtime wheel；benchmark、scripts 和源码测试仍保留在仓库。升级不会
自动删除用户 `~/.pico` 下的旧数据。

Sandbox 始终 explicit-on。production public key/KMS、registry 双架构 image、真实多平台 artifacts 和 detached
Product Enablement 均未完成，因此 distributed 发布保持 `NO-GO`。macOS arm64 本地稳定版不代表 Linux、amd64、
四平台 GA 或 hostile multi-tenant 安全边界。

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

如果 `doctor` 报告 `review_required`，先检查 `.env` 权限、trusted executable、private store
和 pending recovery evidence。不要通过降低权限检查或删除记录来让诊断变绿；处理方法见
[安全](security.md)与[恢复](recovery.md)。
