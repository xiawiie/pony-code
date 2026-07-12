# Pico 维护上下文

本文是维护 Pico 时使用的领域语言和模块边界。操作说明见
[CLI 安装与更新](docs/cli-installation-and-updates.md)，实现流向见[架构](docs/architecture.md)。

## 当前领域语言

**Pico CLI**：唯一 console command `pico`。执行入口只有 `pico run`、`pico repl` 与
`python -m pico`；inspection 与 recovery 使用显式子命令。

**Model Request**：由 system、tools、Canonical Messages、token budget 和 cache breakpoints 组成的
runtime 请求，不等同于 Provider payload。

**Model Response**：Provider-neutral `Response`。Provider wire JSON 和 streaming event 不进入
AgentLoop 合同。

**Model Attempt**：AgentLoop 为当前 Run 构建一次 Model Request 并尝试取得一个 Action 的逻辑轮次。
它由 `TaskState.attempts` 计数，不等于工具执行次数或底层网络请求次数。

**Transport Attempt**：一个 Model Attempt 在 Provider client 内的一次真实 transport 执行，例如一次
HTTP POST。它包含首次执行与可能的 Transport Retry，不等于 Model Attempt。

**Transport Retry**：同一 Model Attempt 中，首次 Transport Attempt 之后对同一 Model Request 的再次
执行。它不同于 `RetryAction`；后者会开始新的 Model Attempt。

维护文档不得用含义不明确的 “Provider Call” 同时指代 Model Attempt 与 Transport Attempt。

**Action**：`decode_action` 从 Model Response 产生的 Tool、Final 或 Retry 决策。一次 attempt 只处理
一个 Action。

**Canonical Messages**：Session 中唯一 transcript。Provider transport 不维护第二份历史。

**Text Protocol Adapter**：把结构化 Model Request 显式转换为 text transport prompt 的边界，用于
OpenAI-compatible 与 Ollama；它不是自动 capability probe 或 Provider registry。

**Project Environment**：当前 lexical repository root 下唯一允许读取的 `.env`。读取不搜索父仓库，
也不把值注入全局 `os.environ`。

**Format Version**：能被独立 reader 消费的顶层 record family 内部编码版本，不是项目 release 版本。

**Recovery Record**：顶层 Checkpoint Record 或 Tool Change Record。它们与 embedded task checkpoint、
trace event 和 Git commit 是不同概念。

**Recovery Review**：发现 pending、interrupted、partial 或 conflict evidence 后，用户决定检查或恢复的
显式步骤；runtime 不做静默回滚。

**User Notes**：用户在 workspace 或 user scope 的 `notes/*.md` 中维护、agent 只读的 Markdown。

**Agent Notes**：每个 scope 唯一的 append-only `agent_notes.md`，只在用户明确要求记忆时追加。

**Query Snapshot**：一次 retrieval query 内共享的 path、metadata、frontmatter 与原文；查询结束即释放，
不形成跨查询 cache。

## 模块边界

| 边界 | 当前职责 |
| --- | --- |
| `pico.cli*` | 解析显式命令、展示结果、调用 runtime/inspection/recovery API |
| `pico.config` | 精确根目录 `.env`、Provider resolver、stdlib TOML 读取与安全 secret 写入 |
| `pico.context*`、`pico.prompt_prefix` | 构建请求上下文、预算、注入、digest 与稳定前缀 |
| `pico.providers.*` | Provider wire transport 与 Provider-neutral `Response` |
| `pico.action_codec`、`pico.agent_loop` | Action 解码与单 attempt/单 action 协调 |
| `pico.tools`、`pico.tool_executor` | 工具 schema、policy/approval、单次执行与 effect terminalization |
| `pico.session_store`、`pico.run_store` | Canonical session 与 run/report/trace persistence |
| `pico.checkpoint_store`、`pico.recovery_*` | recovery records、preview、restore 与 durability |
| `pico.memory.*` | User Notes、Agent Notes、query snapshot 与 retrieval |

## 维护不变量

- 删除优先于兼容：没有 deprecated alias、读时 converter 或多代 runtime 分派。
- 外部输入继续 fail closed；路径必须锚定 trusted root，私有文件拒绝 symlink、hardlink 和特殊文件。
- secret 在 session、trace、report、tool result 与 recovery artifact 之前统一脱敏。
- approval 发生在 mutation lock 之前；primary exception 不被 cleanup/finalizer 的次生错误覆盖。
- Session、Checkpoint Record 与 Tool Change Record 只接受各自当前 type/version。
- 运行时第三方依赖保持为零；安全与恢复代码不因复杂度指标机械拆分。

详细不变量分别见[安全](docs/security.md)、[恢复](docs/recovery.md)与
[Memory](docs/memory.md)。
