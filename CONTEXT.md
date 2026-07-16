# Pico 维护上下文

本文是维护 Pico 时使用的领域语言和模块边界。操作说明见
[CLI 安装与更新](docs/cli-installation-and-updates.md)，实现流向见[架构](docs/architecture.md)。

## 当前领域语言

**Pico CLI**：唯一 console command `pico`。执行入口只有 `pico run`、`pico repl` 与
`python -m pico`；inspection 与 recovery 使用显式子命令。

**Model Request**：由 system、tools、Canonical Messages、token budget 和 cache breakpoints 组成的
runtime 请求，不等同于 Provider payload。

**Model Response**：Provider-neutral `Response`。Provider wire JSON 不进入 AgentLoop 合同；当前不支持
streaming。

**Model Attempt**：AgentLoop 为当前 Run 构建一次 Model Request 并尝试取得一个 Action 的逻辑轮次。
它由 `TaskState.attempts` 计数，不等于工具执行次数或底层网络请求次数。

**Model Retry**：可重试 Provider 失败后，由 AgentLoop 重新发起的 Model Attempt。最多两次，延迟为
0.5 秒和 1.0 秒；429 可使用上限 10 秒的 `Retry-After`。它保留尚未完成的 `RetryAction` 反馈，但不复用
已经得到成功响应的请求，也不改变 endpoint、协议或 model。

**Transport Attempt**：一个 Model Attempt 在 Provider client 内的一次真实 transport 执行，例如一次
HTTP POST。它包含首次执行与可能的 Transport Retry，不等于 Model Attempt。

**Transport Retry**：同一 Model Attempt 中，首次 Transport Attempt 之后对同一 Model Request 的再次
执行。当前 production Provider client 每个 Model Attempt 最多执行一次 HTTP 请求，因此该值应为零；
live 证据若观察到非零值只能标记为 degraded。它不同于 `RetryAction`；后者会开始新的 Model Attempt。

**Model Turn / Model Failure**：Model Turn 是成功取得并解码 Action 的 Model Attempt；Model Failure 是
Provider complete 或 response processing 阶段失败的 Model Attempt。`model_attempts` 可以大于两者之和，
例如 request build 或进程强杀发生在可配对 trace 写入之前。

维护文档不得用含义不明确的 “Provider Call” 同时指代 Model Attempt 与 Transport Attempt。

**Action**：`decode_action` 从 Model Response 产生的 Tool、Final 或 Retry 决策。一次 attempt 只处理
一个 Action。

**Canonical Messages**：Session 中唯一 transcript。Provider transport 不维护第二份历史。

**Model API Configuration**：CLI 唯一可配置的模型连接信息：精确 API 根 `PICO_API_URL` 与凭证
`PICO_DEEPSEEK_API_KEY`。model、协议和认证不是用户选项。
_Avoid_：Provider、Profile、Connection、Preset

**CLI Protocol Family**：Pico CLI 与 Model API 交换 Model Request/Response 的固定 wire contract，即 Anthropic
Messages。运行时不自动探测、降级或切换协议。
_Avoid_：SDK、Model Family、Compatibility Mode

**Model API Endpoint**：实现 Anthropic Messages 的精确、已版本化 API root；Pico 只追加
`/messages`，不补 `/v1`。第三方 endpoint 必须满足相同协议和 `x-api-key` 认证。
_Avoid_：Provider Type、Profile、Vendor

**Model Session Binding**：Session 固化的 `protocol_family`、`model` 与 `endpoint_hash`。恢复时必须与当前
固定模型配置完全一致；旧 binding 不读取、不迁移。
_Avoid_：自动迁移、Provider fallback、Endpoint Cache

**Provider State**：native tool continuation 中保存的受限 opaque state。当前包括 OpenAI Responses encrypted
reasoning item，以及 Anthropic `thinking` / `redacted_thinking` block。它不渲染、不进入普通日志，只在同一
Model Session Binding 中按原协议重放并计入上下文预算。
_Avoid_：Prompt Text、Working Memory、Cross-provider State

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

**Source Root**：用户拥有的规范项目树；Sandbox 从它建立基线，只有 Source Apply Transaction 可以把
已审查变更写回它。
_Avoid_：Workspace Root、Container Workspace

**Execution Root**：当前运行中所有模型可见工具共享的项目视图根；Host 模式使用 Source Root，Sandbox
模式使用独立 staging。
_Avoid_：Pico Root、Shell Root

**Project State Root**：与 Source Root 关联的 Pico 私有项目状态根，保存 Session、Run、Checkpoint 和
Memory；它不是 Execution Root。
_Avoid_：Workspace State、Sandbox Files

**Sandbox State Root**：某个 Sandbox Session 的私有宿主状态根，保存 staging、身份和审计状态；它与
Project State Root 分属不同恢复域。
_Avoid_：Project `.pico`、Container State

**Workspace View**：Execution Root 的物理位置与模型所见逻辑 `/workspace` 之间的单一映射。
_Avoid_：Virtual Filesystem、Sandbox Backend

**Sandbox Session**：把一个 Pico Session、Source Root、Execution Root、Staging Baseline、Sandbox Identity
和最终 diff/apply 状态绑定成同一生命周期的实体。
_Avoid_：Container、Shell Call

**Staging Baseline**：Sandbox Session 启动时对获准 source 内容形成的不可变可信快照；它是 effect 和
Session diff 的比较起点。
_Avoid_：Git Commit、Source HEAD

**Source Apply Transaction**：把同一 immutable reviewed diff 从 Sandbox Session 写回 Source Root 的独立
授权事务；external authority reservation先于journal/guard/Session applying发布，冲突或事实不明时不产生部分
安全路径写入。
_Avoid_：Restore、Auto Merge、Sandbox Save

**Source Apply Authority**：按lexical Source Root索引的external、owner-only恢复锚点；完整绑定source、
Sandbox/state root、control-directory identity、journal和diff，使source root replacement后仍能阻断mutation并由
`sandbox reconcile --yes`定位证据。
_Avoid_：Session Pointer、Apply Cache、Orphan Hint

**Sandbox Contract**：Pico 对一次 sandboxed 执行定义的稳定合同，覆盖已批准输入、资源边界、启动与
失败语义、结果和证据；合同不由具体 OS 隔离实现或其输出决定。
_Avoid_：Docker Flags、Sandbox Backend

**Sandbox Identity**：Pico 对installed distribution、Docker CLI/endpoint、canonical image set及宿主选择的OCI
record、policy、corpus和当前runtime authorization形成的可验证执行身份；身份无法确认时target不得启动。
_Avoid_：PATH Identity、Docker Tag、Executable Path

**Sandbox Local Authorization**：每次本机启动由可信Pico代码密封生成、绑定当前安装树与packaged
image/policy/corpus/platform的非发布执行能力；只接受already-present exact image，不缓存、不联网、不由环境开关提供。
_Avoid_：Product Enablement、Development Approval、Local License

**Sandbox Feasibility Approval**：D1 对 exact candidate envelope 和版本化 mandatory corpus 的不可变结论，
只允许进入 D2-D6 实现，本身不解锁 local 或 distributed Sandbox 产品入口，也不跨 corpus 版本等价。
_Avoid_：Sandbox Enabled、Release Approval

**Sandbox Product Enablement**：D7 可信四目标聚合后签发、与 exact distribution/image/policy/evidence 绑定的
detached 分布式发布执行门；它不等同于ADR-0042的严格本机授权。
_Avoid_：Architecture Accepted、D1 Approved、Candidate Available

**Sandbox Candidate Attestation**：release controller在92-job production aggregate后签发的24小时nonce-bound
能力，只允许四平台最终public CLI smoke；它不供`prepare`下载、不写Product cache，也不表示产品已启用。
_Avoid_：Product Enablement、Preview License、Cached Candidate

**Sandbox Policy**：Pico 为 sandboxed Shell 及其完整子进程树定义的文件、网络与 IPC 资源边界；
它不表达工具是否符合用户意图。
_Avoid_：Tool Policy、Approval Policy、Permission Prompt

**Sandbox Outcome**：Pico 对一次 sandboxed Shell call 的 wrapper、target lifecycle 与 cleanup 事实的
规范化分类；它不同于 readiness、target exit code、Shell Result 和 Tool Change status。
_Avoid_：Command Result、Exit Code、Tool Status

## 模块边界

| 边界 | 当前职责 |
| --- | --- |
| `pico.cli*` | 解析显式命令、展示结果、调用 runtime/inspection/recovery API |
| `pico.config` | 精确根目录 `.env`、固定模型配置解析、stdlib TOML 读取与安全 secret 写入 |
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
- Sandbox 中所有模型可见工具共享同一 Workspace View；Source Root、Project State Root、Sandbox State Root
  parent、host HOME 和 Docker socket 均不得进入 guest。
- `sandbox status/list/inspect/diff/prune --dry-run` 零 mutation；Feasibility Approval缺失阻断实现/发布而非runtime。
  本机runtime要求sealed local authorization，分布式release runtime要求Product Enablement；任一identity不一致均在
  模型请求/target前fail closed。Candidate Attestation仅是controller-owned final smoke例外，不能缓存或正式发布。
- Source Apply固定为external control lock → source mutation lock → exact external reservation → journal/blobs →
  source-local guard → Session applying → source mutation；authority清理使用anchored full-record CAS，公开diff reader
  不得改变artifact ctime，显式`reconcile --yes`不依赖Session inventory猜测state root。

详细不变量分别见[安全](docs/security.md)、[恢复](docs/recovery.md)与
[Memory](docs/memory.md)。
