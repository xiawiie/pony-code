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
python -m pip install --no-deps dist/pico-0.2.0-py3-none-any.whl
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
stdlib TOML parser 在一个 Pico 实例构造时读取一次；malformed 文件整份回退默认值，单字段越界则告警并只回退
该字段。Context caps 中 `system_tools_hard_cap > total_budget_hard_cap` 时整组回退默认。

Provider 默认地址为：

| Provider | 默认地址 |
| --- | --- |
| OpenAI | `https://api.openai.com/v1` |
| Anthropic | `https://api.anthropic.com` |
| DeepSeek | `https://api.deepseek.com/anthropic` |
| Ollama | `http://127.0.0.1:11434` |

OpenAI/Anthropic/DeepSeek 的标准 key 在没有显式 base URL 时只发送到对应 official host。企业网关、自建代理或
其他 relay 必须用 `--base-url`、`PICO_<PROVIDER>_API_BASE` 或对应进程环境变量显式配置；`doctor` 会显示
`explicit_third_party`、host 和配置来源，但不显示凭证。不要在 URL userinfo、query 或 fragment 中放 secret。

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

如果 `doctor --offline` 报告 `review_required`，先检查 `.env` 权限、trusted executable、private store
和 pending recovery evidence。不要通过降低权限检查或删除记录来让诊断变绿；处理方法见
[安全](security.md)与[恢复](recovery.md)。
