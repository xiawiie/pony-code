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

Host 模式的 `WorkspaceContext` 锚定 lexical Source Root、可信 executable 和工作区信息。Sandbox 目标架构中，
Context、RepoMap、内建工具、snapshot 与 Working Memory 全部锚定 filtered Execution Root，并只向模型渲染
Workspace View 的逻辑 `/workspace`；Source Root 只供 host 配置、审计和 Source Apply。host 在 staging 建成后
不得解析 synthetic `.git`。`ContextManager` 从统一 `ModelCapabilities` 得到 Context Window、输出上限与
compaction reserve；默认 128k/16k，最大输入为 `W - max(reserve, output)`。system/tools 受 24,576-token
hard cap 约束，动态 Context Sources 共用 16,384-token pool。每个 top-level turn 共享不可变 source/Memory
snapshot；history 只能经 compaction 退出 active request，不再按静态 soft cap 删除。完整预算、summary 与
telemetry 合同见 [Context、Session 与长会话](context-and-sessions.md)。

实际发送给 Provider 的注入内容等于该 turn 不可变 `InjectionSnapshot.render()` 的结果；同一 turn 内的 retry、
工具结果续接和 compaction 不重新扫描 Workspace 或 Memory。

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
Canonical Messages，并作为一个 JSONL `tool_exchange` 原子提交。message commit 只处理本次 batch 与小型 state
delta，不重写完整 transcript。session commit 失败会回滚内存态，
不会继续请求 Provider。Final、limit、model/runtime/persistence error 与 interrupt 都只走一次 run finalizer。

## 5. 工具与 effect

`pico.tools` 定义工具 schema 和基础校验，`pico.tool_executor.ToolExecutor` 协调：

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

## 6. Docker Sandbox

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

## 7. Persistence 与恢复

`SessionStore` 使用 append-only JSONL Session Tree：header 绑定 exact worktree，entry 以 `id/parent_id` 表示
active branch、fork 与 rewind。Canonical Messages、原子 tool exchange、compaction、branch summary 与 task
checkpoint 都是不可变 entries；Working Memory 和 file summaries 从 active branch 最新 checkpoint 派生。
旧线性 JSON 只在显式首次 resume 时经 candidate+backup 自动迁移。

Compaction 不删除 JSONL 历史，只把 active Model Request 重建为 latest summary + recent tail。summary 调用失败
不提交；Provider context-length error 只允许一次 forced compaction recovery。Session、预算、迁移、命令和
worktree clone 的完整合同见 [Context、Session 与长会话](context-and-sessions.md)。

`RunStore` 保存当前运行的 task/report/trace 审计输出。`CheckpointStore` 独立保存 Checkpoint Record、
Tool Change Record 与 blobs；`RecoveryManager` 只在用户请求后 preview/apply restore。

- Host Recovery：Project State → Source Root；
- Staging Recovery：Sandbox State `recovery` → Execution Root；
- Source Apply：Sandbox State `sandbox_apply` → Source Root。

可独立读取的当前格式是 Session v1、Checkpoint v1、Tool Change v2。旧 OBS/Tool Change 只允许显式事务化
`pico migrate` 转换，正常 runtime 不保留 compatibility reader。详情见[恢复](recovery.md)。

## 8. Memory 与发布范围

`BlockStore` 在 workspace/user 两个 scope 安全读取 User Notes，并以 per-scope lock 原子追加唯一
Agent Notes。一个 top-level turn 的 Memory index、recall 与 link expansion 共用 snapshot；跨 turn 只在
no-follow inventory 完全一致时复用 parsed snapshot。Recall 默认 top-6、每条 passage 1,024 tokens、总 cap
6,144 tokens，并选择最佳匹配段落。召回文本会进入当前 injection source；远程 Provider 会收到该文本，本地
private recovery/audit artifact 也可能保留副本。Compaction 与 delegated agent 都不能获得 Durable Memory 写权限。
详见 [Memory](memory.md)。

v0.2 发布范围不包含 legacy SRT 或 runtime development evaluation。没有 KMS、registry、amd64 和多平台真实证据时，
distributed Product Enablement 保持 `NO-GO`，不能由本地 Sandbox contract 或 report-only benchmark 替代。
