# Pico 当前架构

Pico 的主数据流是：

```text
CLI → config → context → provider → response → action → tools → persistence
```

领域术语以 [`CONTEXT.md`](../CONTEXT.md) 为准；本文只描述当前实现。Sandbox 决策见
[ADR-0040](adr/0040-docker-filtered-staging.md)、[ADR-0041](adr/0041-distributed-release-authority.md) 和
[ADR-0042](adr/0042-sealed-local-authorization.md)。

## 1. CLI 与启动边界

`pico.cli` 是唯一 console entry，`pico.cli_parser` 只分派显式命令。`run`/`repl` 通过 `pico.cli_start` 构造并
执行 `Pico`；status、doctor、sandbox inspection、run/session/checkpoint/memory 命令不构造模型请求。

启动时先发现 lexical Source Root，检查迁移和 source mutation authority，再读取 anchored `pico.toml`、精确根目录
`.env` 和进程环境。Provider resolver 一次性确定 provider、model、base URL、credential source、destination
classification 和 redaction snapshot。CLI runtime、doctor、benchmark 与 live harness 使用同一 resolver。

公开 `--sandbox` 在构造 Provider、Session staging 或 target 前验证宿主平台。v0.2.0 只接受 Darwin arm64；随后
重算 sealed local authorization，绑定当前安装树、packaged canonical image set、policy 和 corpus。already-present
exact image 不匹配时 fail closed，不隐式 pull/build/repair，也不回退 Host。

## 2. Workspace 与 Context

Host 模式的 Execution Root 等于 Source Root。Sandbox 模式分离：

| 边界 | 职责 |
| --- | --- |
| Source Root | host 配置、审计和最终 Source Apply target |
| Execution Root | filtered staging；模型、Context、RepoMap 和所有文件工具的唯一 workspace |
| Project State Root | Host session/run/checkpoint/private state |
| Sandbox State Root | Sandbox manifest、capture、diff、recovery 和 apply journal |
| Provider | 接收最终构造的 model request |

Sandbox 逻辑路径统一渲染为 `/workspace`。Source `.git` 不复制；synthetic `.git` 仅用于受限 workspace view，不作为
source 事实。RepoMap 在第一个真实消费者处同步惰性构建，在局部变量完成 snapshot 后一次发布；fingerprint 使用
inode、size、mtime_ns，避免启动后台扫描和共享 dict 原地更新竞态。

`ContextManager` 组合 AGENTS.md、RepoMap、Memory recall、working memory 和 Canonical Messages。renderer 直接构造
有序 injection source blocks，再 join 成当前 user message；不从最终文本反向 split。同一 top-level user turn 的
retry/tool-followup 复用同一个 immutable `InjectionSnapshot`，实际 Provider payload 与 snapshot render 逐字一致。

## 3. Provider

默认地址由 `pico.providers.defaults` 统一提供：

- OpenAI：`https://api.openai.com/v1`
- Anthropic：`https://api.anthropic.com`
- DeepSeek：`https://api.deepseek.com/anthropic`
- Ollama：loopback `http://127.0.0.1:11434`

destination classifier 不维护 relay 黑名单。默认 remote host 必须是该 Provider 的 official host；loopback 是
`local`；仅 CLI、Project Environment 或进程环境显式提供的其他 host 才是 `explicit_third_party`。URL credential、
query、fragment、redirect 和 secret-bearing诊断均被拒绝或脱敏。

Anthropic-compatible 与 DeepSeek client 实现结构化 `complete(...)`；OpenAI-compatible 与 Ollama 经
`TextProtocolAdapter` 转为 text transport。四类 production client 每次 Model Attempt 至多发送一个 HTTP request；
AgentLoop 仅对明确 retryable failure 执行 bounded model retry。Run evidence 区分 Model Attempt、Transport Attempt、
retry 和 failure；缺失证据时不把局部数据冒充完整 aggregate。

## 4. Action 与工具

`AgentLoop` 每次 attempt 只接受一个 Tool、Final 或 Retry Action。Tool Action 通过 `make_tool_pair` 原子追加到
Canonical Messages；session commit 失败会回滚内存态，不继续请求 Provider。

实际工具真源是 `BASE_TOOL_SPECS` 和 delegate specs，其中包含 schema 与 `effect_class`。`ToolExecutor` 的顺序为：

```text
validate policy → current-turn memory authorization → approval
→ mutation lock / pending Tool Change → execute once
→ observe actual effects once → verify / terminalize
```

未知或 effect metadata 不合法的工具按 high-risk `workspace_write` 拒绝。`memory_save` 只看当前
`TaskState.user_request`，delegate depth 大于 0 永远不能执行；无授权不会创建 note、checkpoint 或 mutation side
effect。

Host 与 Sandbox 内建文件工具共享 anchored、bounded I/O：从可信 root dirfd 逐层 no-follow 打开，只接受普通
single-link 文件；write/patch 使用同目录 temp、fsync、atomic replace，patch 以读取 digest 做 CAS。目录和 search
有深度、entry、文件、总字节、结果和超时上限。Sandbox 模式传入的是 Execution Root，所以这些工具不会回退 Source
Root。

长 Tool Result 对模型只显示逻辑 raw-result id 和完整内容 SHA-256；Project State 中的 Host artifact 绝对路径只留在
本地私有证据中。

## 5. Docker Sandbox

Session 通过 anchored、streaming staging builder 复制获准 source 文件；普通文件不整文件缓冲，known secret 扫描
支持跨 chunk carry，env template 单独受 1 MiB 上限。源文件读前/读后 identity、digest 和 mode 必须一致，失败会清理
temp 和未完成 destination。

Shell 使用 exact image 的短生命周期无网络容器。唯一 host bind 是 Execution Root；Source/Project State/Sandbox
State/HOME/Docker socket 均不挂载。调用开始和结束仍遍历 metadata，只有 fingerprint 变化的文件重新读取和 hash；
resume、非 shell mutation、blob 缺失或 capture 异常会强制恢复全量路径。容器退出后无条件进行最终 workspace
measure，快速命令也不能绕过容量或特殊文件检查。

Session 结束后完整 capture 生成 immutable final manifest 和 redacted diff。无变更自动 discard；有变更进入
`pending_review`。Source Apply 在独立 lock/authority/journal/guard 下重验刚展示的 exact digest 和 source baseline，
使用 source-local private quarantine 与原子发布；不确定时整次零 source writes。

## 6. Persistence 与恢复

`SessionStore` 保存 Canonical Messages、working memory、task checkpoint 和 runtime identity；`RunStore` 保存低敏感
run/report/trace；`CheckpointStore` 保存 Checkpoint、Tool Change 和 blobs。三个 Sandbox recovery domain 严格分离：

- Host Recovery：Project State → Source Root；
- Staging Recovery：Sandbox State `recovery` → Execution Root；
- Source Apply：Sandbox State `sandbox_apply` → Source Root。

可独立读取的当前格式是 Session v1、Checkpoint v1、Tool Change v2。旧 OBS/Tool Change 只允许显式事务化
`pico migrate` 转换，正常 runtime 不保留 compatibility reader。详情见[恢复](recovery.md)。

## 7. Memory 与发布范围

`BlockStore` 在 workspace/user 两个 scope 读取 User Notes，并在明确授权后原子追加唯一 Agent Notes。
`Retrieval` 每次 query 建立 bounded snapshot并在结束后释放。召回文本会进入当前 injection source；远程 Provider 会
收到该文本，本地 private recovery/audit artifact 也可能保留副本。详情见 [Memory](memory.md)。

v0.2.0 wheel 不包含 legacy SRT 模块/package data，也不包含开发面的 `pico.evaluation`。distributed authority 合同
继续存在，但 production key/KMS、registry、amd64 image 和真实多平台证据不存在，所以 Product Enablement 保持
`NO-GO`。
