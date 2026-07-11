# Pico 当前架构

Pico 的主数据流是：

```text
CLI → config → context → provider → response → action → tools → persistence
```

领域术语以 [`CONTEXT.md`](../CONTEXT.md) 为准；本文描述当前实现边界，不保存历史方案。

## 1. CLI 与构造

`pico.cli` 接收唯一 console entry，`pico.cli_parser` 只分派显式命令。`run` 与 `repl` 最终通过
`pico.cli_start` 构造 `Pico`；inspection 命令读取 run/session/checkpoint/memory，而不启动模型请求。

构造阶段由 `pico.config` 解析一次 `pico.toml`，读取 exact-root Project Environment，并通过共享
Provider resolver 得到 provider、model、base URL、credential source 和 redaction snapshot。CLI runtime、
doctor、benchmark 与 live harness 使用同一解析真源。

## 2. Context 与 Model Request

`WorkspaceContext` 锚定 lexical repo root、可信 executable 和工作区信息。`ContextManager` 组合
AGENTS.md、repo map、Memory recall、工作记忆和 Canonical Messages，并在 token budget 内构造一次
Model Request。每个 top-level turn 共享一个 injection snapshot；每个 attempt 只 build 一次 request。

## 3. Provider 边界

Anthropic-compatible 与 DeepSeek client 实现 runtime-facing `complete(...)` 并返回 Provider-neutral
`Response`。OpenAI-compatible 与 Ollama 只实现 `complete_text(...)`；构造时由显式
`TextProtocolAdapter` 把结构化请求转成 text transport。AgentLoop 不使用 `hasattr` 判断 Provider 代际，
也不维护 registry 或第二套 transcript。

真实 Anthropic/DeepSeek prompt-cache capability 保留在 Provider/Context 边界；它不是 feature flag。
OpenAI-compatible/Ollama 不伪装 native structured tools。

## 4. Response、Action 与循环

`pico.agent_loop.AgentLoop` 每个 attempt 调用一次 `complete`，累计一次 usage，再由
`pico.action_codec.decode_action` 产生一个 Tool、Final 或 Retry Action。native response 含多个工具时只
执行第一个，忽略数进入 trace。Retry 消耗 attempt；被 policy 拒绝的工具不消耗 tool step。

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

## 6. Persistence 与恢复

`SessionStore` 保存 Canonical Messages、working memory、embedded task checkpoints 与 runtime identity。
`RunStore` 保存当前运行的 task/report/trace 审计输出。`CheckpointStore` 独立保存 Checkpoint Record、
Tool Change Record 与 blobs；`RecoveryManager` 只在用户请求后 preview/apply restore。

可独立读取的 session/checkpoint/tool-change 使用各自 `record_type + format_version` 当前合同。embedded
task checkpoint、verification evidence、restore preview 和 run artifacts 不拥有独立格式版本。详情见
[恢复](recovery.md)。

## 7. Memory

`BlockStore` 在 workspace/user 两个 scope 安全读取 User Notes，并以 per-scope lock 原子追加唯一
Agent Notes。`Retrieval` 为每次 query 建立一次 snapshot，每个文件最多读取一次，查询后释放。
详见 [Memory](memory.md)。
