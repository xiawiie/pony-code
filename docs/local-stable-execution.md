# Pico Sandbox 本地稳定版 v0.2.0 执行与验收

本文是 v0.2.0 本地稳定版的发布执行真源。架构理由见
[ADR-0040](adr/0040-docker-filtered-staging.md)、[ADR-0041](adr/0041-distributed-release-authority.md) 和
[ADR-0042](adr/0042-sealed-local-authorization.md)。历史规划和旧 benchmark 不能替代最终 exact HEAD 证据。

## 交付范围

唯一正式 Sandbox 组合是 macOS arm64 + Docker Desktop + already-present exact `linux/arm64` image。Host 模式、
sealed local authorization、filtered staging、containerized shell、immutable diff 和显式 Source Apply 均保留。

以下内容明确延期：Linux/amd64 GA、registry、KMS、distributed Product Enablement、远程多租户、自定义 Sandbox
backend、插件系统、向量数据库和 Sandbox 网络配置。authority reader 合同与测试保留，但当前发布判定为 `NO-GO`。

## 不可违反的运行合同

- Source Root 在 Source Apply 前不变；Sandbox 的所有模型可见文件工具只访问 filtered staging。
- Source Root、Project State、Sandbox State、HOME、Docker socket 和凭证不挂载进容器。
- `status`、`prepare` 和只读 inspection 不联网、不 pull/build/repair、不创建 state、不隐式 reconcile。
- 不确定的文件类型、路径身份、capture 或恢复状态 fail closed。
- incremental capture 只优化 shell 调用期；finalize 始终完整 capture。
- `--yes` 只跳过交互，不能跳过 immutable artifact 加载、digest 绑定或 Source Apply CAS。
- runtime 零第三方 Python 依赖；wheel 不包含 legacy SRT 或 `pico.evaluation`。

## 一次 Sandbox Session

```text
platform/local authorization
  → source identity + filtered staging baseline
  → context/file tools on staging
  → ephemeral no-network shell containers
  → full final capture + immutable redacted diff
  → pending review
  → explicit apply(exact digest + source CAS) or discard
```

Apply 确认必须显示 sandbox id、exact diff digest、Source Root、candidate count/bytes、created/modified/deleted、
high-risk/blocked 数和最多 10 个高风险路径。确认后使用刚展示的 digest；确认期间 source 或 artifact 漂移会失败，
不会降级为部分写入。

## Provider 与 Memory 边界

公开 CLI 固定使用 `deepseek-v4-flash`、Anthropic Messages 和 `x-api-key`，默认精确 API 根为
`https://api.deepseek.com/anthropic/v1`。`PICO_API_URL` 未设置时使用该官方默认值；第三方 Anthropic-compatible
relay 只通过项目或进程环境中的该变量显式替换；凭证只读取 `PICO_DEEPSEEK_API_KEY`。客户端只追加 `/messages`，不按域名或模型名推断、
探测、回退或切换协议。`pico doctor` 默认离线且只显示低敏配置状态；`pico doctor --check-api` 才会联网。

OpenAI Responses、OpenAI Chat Completions、Anthropic Messages 与 Ollama Chat adapter 继续作为内部测试/benchmark
边界存在，但不是公开 CLI 的模型选择面。公开运行时不读取旧 Provider/Profile/Connection 配置。

`memory_save` 只接受当前 top-level user request 的明确记忆授权；历史请求不能继承，delegate 不能写。自动命中的
Memory 会进入当次 Provider prompt：使用远程 Provider 时即会发送到该 endpoint。Memory 原文或其副本还可能存在于
Agent Notes、Tool Change、checkpoint、recovery 和其他本地私有审计 artifact；删除当前 note 不代表所有历史副本消失。

## 发布门禁

在同一干净 exact HEAD 上执行：

```bash
uv lock --check
uv run ruff check .
uv run pytest -q
uv build --clear
uv run python scripts/verify_distribution.py --install-smoke --offline-bundle-smoke
uv run python scripts/evaluate.py --suite core-fast
uv run python scripts/evaluate.py --suite core-functional
uv run python scripts/evaluate.py --suite sandbox-contract
uv run python scripts/evaluate.py --suite sandbox-real
uv run python -m benchmarks.perf.bench_sandbox
uv run python -m benchmarks.perf.bench_sandbox --real
git diff --check
```

Darwin 的 `sandbox-real` 还要求两个外部 fixture：`PICO_SANDBOX_MOUNT_FIXTURE` 指向一个包含跨设备子挂载点的
Source tree，`PICO_SANDBOX_DEVICE_FIXTURE` 指向包含 character/block device 的目录（macOS 通常使用 `/dev`）。
这两个 fixture 必须在调用前由发布执行者显式准备并通过环境变量传入；评估命令不会隐式 mount、创建设备或降级跳过，
缺失时以 `mount_boundary_fixture_required` / mandatory check failure 保持 `NO-GO`。

门禁按 Authority、Static、Functional、Distribution、P0 Security、Sandbox Real、Performance、Provider Live 和
Documentation 九组判定。任一 mandatory gate 失败、出现未解释 skip，或默认 DeepSeek exact-HEAD live 未获得新的
费用授权/凭证并通过，发布保持 `NO-GO`，不得创建 stable 标签。

性能只做同机回归：普通场景 5 次 warmup + 20 次 measured；5000 文件和 128 MiB 场景 1 + 5 次。artifact 必须记录
commit、dirty state、机器、OS、Python、Docker、image digest、median 和 p95。目标包括 128 MiB staging 额外 Python
峰值不超过 32 MiB、326 文件 no-op shell observed median 不超过 1.40 s/p95 不超过 1.60 s、5000 文件 watchdog
不超过单核 10% 且违规检测不超过 2 s。

## 交付与回滚

通过全部门禁后才构建最终 wheel/sdist、在隔离 venv/HOME 且无源码 `PYTHONPATH` 下 clean-install，并创建本地
`v0.2.0` 标签。外部 push、GitHub Release 和 PyPI 发布需要另行授权。

各阶段保持独立提交；固定模型路径、P0、安全 I/O、staging、incremental capture 和遗留删除可独立回滚。没有
持久化 schema migration，代码回滚不需要数据降级。旧 `~/.pico` 数据不自动删除；pending/interrupted/invalid
恢复记录必须先备份并人工 review。
