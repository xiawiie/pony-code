# Pico 当前架构

Pico 的主数据流是：

```text
CLI → config → context → provider → response → action → tools → persistence
```

领域术语以 [`CONTEXT.md`](../CONTEXT.md) 为准；本文描述当前实现边界，不保存历史方案。

Sandbox 是唯一例外：ADR-0040接受Docker + filtered staging架构，ADR-0042接受严格本机MVP。D2-D6 owners已实现；
registry production vertical、Git distribution authority、Linux performance baseline、D7四目标门禁和Product
Enablement仍未完成。下文区分“当前本机能力”和“尚未完成的分布式发布”。

## 1. CLI 与构造

`pico.cli` 接收唯一 console entry，`pico.cli_parser` 只分派显式命令。`run` 与 `repl` 最终通过
`pico.cli_start` 构造 `Pico`；inspection 命令读取 run/session/checkpoint/memory，而不启动模型请求。

构造阶段读取 exact-root Project Environment，并由 `pico.config.resolve_model_config` 解析唯一模型配置。
模型固定为 `deepseek-v4-flash`，wire contract 固定为 OpenAI Chat Completions，认证固定为 Bearer；只有精确
API 根 `PICO_API_URL` 与凭证 `PICO_DEEPSEEK_API_KEY` 可配置。`pico.cli` 直接构造
`OpenAIChatCompletionsModelClient`，不经过 Provider/Profile/Connection factory。`run/repl` 缺少 Key 时
fail closed；`config show` 与普通 `doctor` 在未配置 Key 时仍可离线工作。

显式`--sandbox`每次在模型请求、Sandbox state和target前重算sealed local authorization，绑定当前Pico安装树与
packaged image/policy/corpus/platform；already-present本机image必须精确匹配。它不进入superseded SRT代码，
不下载、不隐式pull，也不会回退Host。distributed release reader仍冻结为
RSA-PSS-SHA256（3072-bit、e=65537、32-byte salt）、canonical ASCII JSON和domain separation，并固定stable
GitHub Releases channel、禁用proxy、HTTPS redirect allowlist与256 KiB上限。wheel内不可变public key map当前为空，
production public key/KMS也不存在，因此仍没有可接受的distributed release attestation。

release controller可在四平台public smoke中通过`PICO_SANDBOX_CANDIDATE_ATTESTATION`和
`PICO_SANDBOX_CANDIDATE_NONCE`注入nonce-bound candidate；它不能由`prepare`下载、不能写Product Enablement
cache，产物也必须保持`product_enablement=false`。正式cache固定为
`~/.pico/releases/docker-sandbox/product-enablement.json`。

本机构造顺序固定为：发现Source Root与迁移状态；在host冻结project config、Project Environment与redaction；
生成并验证local authorization，本地重算installed-distribution与canonical image-set、核对内置policy
constant，并把image-set内的packaged corpus claim与签名provenance对齐；创建
Sandbox Session、filtered Execution Root、Staging Baseline与durable Session-manifest link；从Workspace View
构造context/store/model client/session；最后才发送首次Model Request。wheel/sdist/commit/expected manifest/aggregate
digest及corpus是controller签名前核验、再由签名认证的provenance claims，普通安装目录不能反推出原wheel SHA
或mandatory corpus。任何失败都不回退host runner。universal wheel绑定canonical image-set v2，host只选择对应
`linux/arm64`或`linux/amd64`
OCI record；当前manifest只有无registry reference的arm64记录，amd64与distributed registry记录尚不存在。

## 2. Context 与 Model Request

Host 模式的 `WorkspaceContext` 锚定 lexical Source Root、可信 executable 和工作区信息。Sandbox 目标架构中，
Context、RepoMap、内建工具、snapshot 与 Working Memory 全部锚定 filtered Execution Root，并只向模型渲染
Workspace View 的逻辑 `/workspace`；Source Root 只供 host 配置、审计和 Source Apply。host 在 staging 建成后
不得解析 synthetic `.git`。`ContextManager` 组合 AGENTS.md、repo map、Memory recall、工作记忆和 Canonical
Messages，并在 token budget 内构造一次 Model Request。每个 top-level turn 共享一个 injection snapshot；每个
attempt 只 build 一次 request。

## 3. 模型 API 边界

CLI 只有一条生产路径：

```text
PICO_API_URL + PICO_DEEPSEEK_API_KEY
    → OpenAIChatCompletionsModelClient
    → POST {exact_api_root}/chat/completions
    → model=deepseek-v4-flash, Bearer auth, thinking disabled
```

默认 API 根为 `https://api.deepseek.com`。第三方网关通过替换精确 API 根接入，但必须提供相同 Chat
Completions wire contract 与 Bearer 认证。URL 不得包含 query、fragment、userinfo 或凭据；除 loopback 外必须
使用 HTTPS。Adapter 不补 `/v1`，不按域名或模型推断能力，不探测候选路径，也不自动切换协议或模型。

Chat client 将 Canonical Messages 转换为 system/user/assistant/tool roles，保留 tool call ID 与 tool result
配对，解析 `finish_reason`、usage 和 `reasoning_content`。DeepSeek 路径显式关闭 thinking；若工具响应仍携带
reasoning content，则 fail closed，不丢失 reasoning 后继续提交 tool result。只有规范化 stop reason 为
`TOOL_USE` 时 AgentLoop 才允许执行工具。

Anthropic Messages、OpenAI Responses 与 Ollama Chat client 仍保留为内部协议实现和离线测试对象，但不接入
本轮 CLI，也不参与配置解析。所有 client 都返回统一 `Response`；不存在文本 action adapter、运行时 registry
或第二套 transcript。

`TaskState.attempts` 记录 AgentLoop 的 Model Attempt；Provider client 内部的 Transport Attempt 与
Transport Retry 不属于这个计数。三类 production client 都执行至多一次 HTTP request，不在 client 内部
重试；AgentLoop 仅对分类为 retryable 的 `_ProviderFailure` 执行最多两次 Model Retry。每次 attempt 的
origin 为 `initial`、`tool_followup`、`retry_action` 或 `model_retry`。

Provider 完成后，AgentLoop 立即读取 client 的 `last_transport_attempts`。成功写入 `model_turn`，失败写入
`model_failed`；两者都携带 transport evidence。run report 聚合 model attempts/turns/failures、HTTP
attempts/retries 与 failure reasons。自定义 Provider 不提供证据时，aggregate 为 `null` 且
`transport_evidence_complete=false`，不会用已知局部值冒充全量。

网络、timeout、429 与 5xx 只由 AgentLoop 在相同 endpoint/协议/model 上最多重试两次。429 的
`Retry-After` 上限为 10 秒；其他延迟为 0.5/1 秒。client 每个 Model Attempt 只执行一次 HTTP request，因此
不会在 transport 层制造不可见重试。

## 4. Response、Action 与循环

`pico.agent_loop.AgentLoop` 每个 attempt 调用一次 `complete`，成功响应累计一次 usage，再由
`pico.action_codec.decode_action` 产生一个 Tool、Final 或 Retry Action。无工具且有文本时返回 Final；恰好一个
合法 native tool call 时返回 Tool；多个调用时返回 `multiple_actions_not_supported`，一个也不执行。
`RetryAction` 最多允许一次同协议纠正；Model Retry 最多两次且只处理明确的
retryable Provider failure。两者都消耗 Model Attempt，被 policy 拒绝的工具不消耗 tool step。

Tool Action 通过 `make_tool_pair` 原子追加到 Canonical Messages。session commit 失败会回滚内存态，
不会继续请求 Provider。Final、limit、model/runtime/persistence error 与 interrupt 都只走一次 run finalizer。

## 5. 工具与 effect

`pico.tools` 定义工具 schema 和基础校验，`pico.tool_executor.ToolExecutor` 协调：

```text
validate / approve → prepare / execute once → observe effects once → terminalize
```

未知工具 fail-safe 视为 workspace write。shell 先经过 syntax/risk assessment、trusted executable 与
hardened Git/RG 边界；复杂 shell 只有用户批准后才可执行。mutation lock 覆盖 pending record、runner、
effect observation、verification 和 terminalization。详见[安全](security.md)。

当前SRT adapters是superseded且不可达的历史实现；release-only candidate public smoke不改变本机或发布状态。
已实现的production owner只有一个 Docker execution runner：内建工具在 host 操作同一 filtered Execution Root，任意
Shell 由 exact managed image 的短生命周期 container 执行，唯一 host bind 是该 root，source/state/HOME/
Docker socket不挂载，container外网络禁用。Tool approval只授权staging mutation；Source Apply是结束后的独立
授权事务。本机MVP不等于D7 distributed release。

## 6. Persistence 与恢复

`SessionStore` 保存 Canonical Messages、working memory、embedded task checkpoints、runtime identity 与
Model Session Binding（`protocol_family`、`model`、`endpoint_hash`）。Session format v2 只接受这三个字段；
URL、协议或模型任一变化都会以 `model_session_mismatch` 拒绝恢复。旧 Session binding 不读取、不迁移，也不
在首次请求后补写兼容字段。内部 OpenAI Responses client 的 encrypted reasoning state 只存在于 assistant tool
message 的 `_pico_provider_state`，经过结构/大小校验，不渲染、不进入普通 trace，并且只有相同 binding 才能重放。
`RunStore` 保存当前运行的 task/report/trace 审计输出。`CheckpointStore` 独立保存 Checkpoint Record、
Tool Change Record 与 blobs；`RecoveryManager` 只在用户请求后 preview/apply restore。

Sandbox 目标架构把恢复域分开：Host Recovery 使用 Project State Root 原 store 并作用于 Source Root；
Staging Recovery 使用 `Sandbox State Root/recovery` 并只作用于 Execution Root；Source Apply 使用
`Sandbox State Root/sandbox_apply` 并只作用于 Source Root。三类 record/blob 不得互读。Project State 中的
strict sidecar 与 Sandbox manifest 必须形成 durable one-to-one link，resume/report 对缺失或不匹配 fail closed。

旧 OBS/Tool Change 合同只由显式 `pico migrate` converter 读取。正常 runtime 先检查 migration journal，
只读取 current contract；cutover 使用 same-filesystem candidate/rollback 与可重入 durable-state recovery。

可独立读取的 session/checkpoint/tool-change 使用各自 `record_type + format_version` 当前合同。embedded
task checkpoint、verification evidence、restore preview 和 run artifacts 不拥有独立格式版本。详情见
[恢复](recovery.md)。

## 7. Memory

`BlockStore` 在 workspace/user 两个 scope 安全读取 User Notes，并以 per-scope lock 原子追加唯一
Agent Notes。`Retrieval` 为每次 query 建立一次 snapshot，每个文件最多读取一次，查询后释放。
详见 [Memory](memory.md)。
