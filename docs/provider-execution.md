# Provider 执行与重试审计设计

状态：设计完成，尚未实施。

## 1. 问题与结论

当前 Pico 同时存在三种“调用”概念：

1. `TaskState.attempts`：AgentLoop 的逻辑模型轮次；
2. trace 中的 `model_turn`：只在取得有效 `Response` 后出现；
3. Provider client 内部的 `urlopen`：一次逻辑模型轮次可能执行多次 HTTP 请求。

live E2E 目前把 `model_turn` 数量报告为 `provider_calls`。这个名称会产生两个错误理解：

- 误以为工具 roundtrip 产生的多个 Model Attempt 都是失败重试；
- 误以为 `provider_calls` 已包含底层 HTTP retry，因而可以用于费用与稳定性审计。

目标设计将三层计数彻底拆开，并把“安全审计”拆成四个独立结论：执行正确性、费用/预算完整性、凭据与产物安全、fixture 与持久化完整性。只有四者全部通过，最终结果才可称为全量通过。

## 2. 目标与非目标

### 2.1 目标

- 精确回答一次 Run 发生了多少 Model Attempt、Provider Exchange、Transport Attempt 和 Transport Retry。
- 成功与失败的 Provider Exchange 都留下脱敏、终态化证据。
- 明确工具 roundtrip、`RetryAction` 与网络 retry 的差异。
- 防止自动重放生成请求造成隐藏费用或重复执行。
- 保持 OpenAI/Ollama text transport 与 Anthropic/DeepSeek native transport 的现有边界。
- 保持 stdlib-only、单一 Canonical Messages、RunStore 审计和 fail-closed 安全原则。

### 2.2 非目标

- 不增加 Provider registry、动态 capability probe 或第二套 transcript。
- 不记录 prompt、payload、URL、headers、response body、API key 或原始异常文本。
- 不把 Trace Timeline 变成逐 socket/逐字节网络日志。
- 不修改 Session、Checkpoint Record、Tool Change Record 或恢复语义。
- 不承诺第三方 Provider 永远可用，也不把网络超时包装成成功。
- 不为当前未配置的 Ollama 伪造真实 E2E 证据。

## 3. 统一领域模型

```text
Run
└── Model Attempt (0..N)
    ├── 构建 Model Request
    └── Provider Exchange (0..1)
        ├── Transport Attempt #1 (0..1)
        ├── Transport Retry #1（目标默认不允许）
        └── Model Response (0..1)
            └── Action
                ├── Tool Action → 执行工具 → 新 Model Attempt
                ├── Retry Action → runtime feedback → 新 Model Attempt
                └── Final Action → Run 完成
```

### 3.1 Model Attempt

AgentLoop 的一次逻辑迭代。它从 `TaskState.record_attempt()` 开始，目标是取得一个 Action。请求构建失败时可能没有 Provider Exchange，因此：

```text
model_attempts >= provider_exchanges
```

### 3.2 Provider Exchange

AgentLoop 把一个已构建的 Model Request 交给 Provider client 的一次逻辑交换。成功时返回一个 Provider-neutral `Response`；失败时产生结构化 Provider 错误。

每个已发送的 Model Request 对应且只对应一个 Provider Exchange。

### 3.3 Transport Attempt

Provider client 内部一次真实 transport 执行。当前四类真实 Provider 都是 HTTP，因此它通常对应一次 `urlopen`。这个概念不进入 Canonical Messages。

### 3.4 Transport Retry

同一 Provider Exchange 中，从第二次 Transport Attempt 开始的每一次执行。它复用同一个 Model Request，因此与产生新 Model Request 的 `RetryAction` 完全不同。

### 3.5 Tool Step

只有 Tool Action 真正进入执行阶段才增加。Final Action、Retry Action、Provider 失败和被 policy 拒绝的工具不应伪装成 Tool Step。

## 4. 必须成立的计数不变量

每个 terminal Run 必须满足：

```text
task_state.attempts == model_attempts
provider_exchanges_started == provider_exchanges_succeeded + provider_exchanges_failed
provider_exchanges_succeeded == model_turn_events + response_processing_failed_exchanges
provider_exchanges_started == transport_started_exchanges + transport_not_started_exchanges
transport_attempts >= transport_started_exchanges
transport_retries == transport_attempts - transport_started_exchanges
tool_steps <= model_attempts
```

目标默认禁止自动 Transport Retry，因此第一阶段还必须满足：

```text
transport_retries == 0
transport_attempts == transport_started_exchanges
```

Provider client 可能在发送网络请求前因 header、配置或本地安全校验失败，因此一个 Provider Exchange
可以有 0 个 Transport Attempt。审计必须显式统计 `transport_not_started_exchanges`，不能把它伪装成
一次网络调用。

异常终止或进程被强杀可能留下 started/finished 不配对。审计必须将其标记为 incomplete，而不是用零填充后通过。
Provider 已返回 Response、但 action decode/trace persistence 在 `model_turn` 前失败时，必须增加
`response_processing_failed_exchanges`；不能把成功收到的响应改记为 transport 失败。

## 5. 重试策略

### 5.1 默认策略：生成请求不自动重放

OpenAI-compatible 与 Anthropic-compatible 当前会对 5xx、`URLError`、断线和 timeout 最多尝试三次。对于生成类 POST，请求可能已经被 Provider 接收、执行或计费，但客户端没有收到完整响应；此时自动重放存在重复费用和不确定结果。

目标策略如下：

| 结果 | 自动重试 | 处理 |
| --- | --- | --- |
| 2xx 且响应合法 | 否 | 返回 Model Response |
| 4xx | 否 | 结构化 `http_error` |
| 5xx | 否 | 结构化 `http_error` |
| connect/DNS/network error | 否 | 结构化 `network_error` |
| timeout | 否 | 结构化 `timeout`，usage 标记 unknown |
| response 无法解析 | 否 | 结构化 `invalid_response` |
| 用户中断 | 否 | 保留 `KeyboardInterrupt` 语义 |

用户再次执行任务属于新的 Run，不是隐藏 Transport Retry。

### 5.2 未来允许自动重试的唯一条件

只有同时满足以下条件，才允许为某个具体 Provider path 增加 retry：

- Provider 官方或目标 gateway 明确支持幂等键；
- Pico 对同一 Provider Exchange 复用同一幂等键；
- retry 分类、次数和最终 outcome 可完整进入 Provider Execution Evidence；
- retry budget 默认有限且经过显式测试；
- 费用审计可以区分已知 usage 与 ambiguous usage。

在这些条件成立前，不增加通用 `--retries` 配置；避免把不安全策略变成用户可调旋钮。

## 6. Timeout 与墙钟预算

当前 `timeout_seconds` 同时被理解为单请求 timeout 和整场 E2E wall cap，语义不清。目标配置拆成：

- `request_timeout_seconds`：单个 Transport Attempt 的 socket/request timeout；
- `max_wall_seconds`：完整 live E2E 的观测上限；
- `max_provider_exchanges`：逻辑 Provider Exchange 上限；
- `max_transport_attempts`：真实 transport 执行上限；
- `max_total_tokens`：Provider 返回的 input+output token 上限。

stdlib `urlopen` 不能可靠取消已经阻塞的跨平台线程，因此内置 harness 不宣称绝对 hard wall。单请求由 `request_timeout_seconds` 限制；完整流程超过 `max_wall_seconds` 后 fail closed。需要强制杀进程的 CI 使用 job/process supervisor timeout，而不是在 Pico 内增加异步运行时。

## 7. Provider Execution Evidence

每个 Provider client 维护一份最近一次 Provider Exchange 的聚合证据，沿用现有 `last_completion_metadata` 模式，字段固定且有界：

```json
{
  "outcome": "success",
  "transport_attempts": 1,
  "transport_retries": 0,
  "duration_ms": 1234,
  "response_received": true,
  "usage_status": "complete",
  "error_code": "",
  "http_status": null,
  "retry_reason_counts": {}
}
```

### 7.1 字段规则

- `outcome`：`success | http_error | network_error | timeout | invalid_response | backend_error | interrupted | unknown_error`；
- `transport_attempts`：包含首次执行；
- `transport_retries`：`max(0, transport_attempts - 1)`；
- `duration_ms`：完整 Provider Exchange 耗时，不是单次 HTTP latency；
- `response_received`：是否收到可供解析的响应；
- `usage_status`：`complete | missing | unknown`；
- `error_code`：固定枚举，不保存原始异常文本；
- `http_status`：仅保存整数状态码；
- `retry_reason_counts`：固定错误类别的计数，不保存 URL 或消息。

### 7.2 严禁字段

下列信息不得进入证据、trace 或 report：

- prompt、system、tools、messages；
- Provider request/response body；
- base URL、完整 request URL、query；
- headers、API key、authorization；
- Provider 原始错误 body；
- 可能包含 URL、凭据或响应内容的原始 exception string。

RunStore 仍执行统一 redaction；Provider 层的固定 schema 是第一道边界，RunStore redaction 是第二道边界。

## 8. 最小实现形态

不新增 registry、事件总线或 callback framework。复用当前顺序执行模型：

1. Provider client 在 exchange 开始时清空 `last_transport_evidence`；
2. transport 执行时更新本地计数；
3. `finally` 中封存一份固定 schema 的 evidence；
4. text provider 由 `TextProtocolAdapter` 像同步 usage 一样同步 evidence；
5. AgentLoop 成功时立即复制 client evidence；`ProviderError` 失败时优先复制 exception 自带 evidence；
6. AgentLoop 只写一个 `provider_exchange_finished` trace event；
7. report 从 trace/运行期聚合数据生成 totals。

父 agent 与 delegate 当前同步复用同一 `model_client`。AgentLoop 必须在 `complete()` 返回或抛错后立即复制 evidence，不能延迟读取。未来若引入同一 client 的并发调用，这个 `last_*` 模式必须先改为 per-call return/envelope；本设计不提前为尚不存在的并发构造框架。

自定义或旧测试 Provider 没有 evidence 时，runtime 继续按现有 Provider contract 执行，但写入
`evidence_complete=false`。普通 Run 不因此崩溃；要求费用审计的 live/benchmark gate 必须 fail closed。
`FakeModelClient` 生成确定性的单 attempt evidence，保证离线测试不依赖网络。

## 9. 结构化错误

Provider 层增加一个小型 `ProviderError(RuntimeError)`，只暴露稳定、安全字段：

```text
code: http_error | network_error | timeout | invalid_response | backend_error
http_status: int | None
evidence: Provider Execution Evidence
```

AgentLoop 继续把 Provider 失败终态化为 `model_error`，不增加新的 TaskState stop reason。具体 Provider 错误类别进入 trace/report，避免 TaskState 变成 transport 细节容器。

## 10. Trace Timeline

新增一个聚合事件：

```json
{
  "event": "provider_exchange_finished",
  "attempt": 2,
  "outcome": "success",
  "transport_attempts": 1,
  "transport_retries": 0,
  "duration_ms": 1234,
  "response_received": true,
  "usage_status": "complete",
  "error_code": "",
  "http_status": null
}
```

事件必须在成功和失败路径都写入。成功路径随后写 `action_decoded` 与 `model_turn`；失败路径直接进入现有 `model_error` terminalization。

不为每次 HTTP attempt 单独写事件。这样符合 ADR-0036 的 focused trace 原则，同时仍能审计 retry 数量。

## 11. Run Report

在现有 report 中增加：

```json
{
  "provider_execution": {
    "model_attempts": 7,
    "provider_exchanges_started": 7,
    "provider_exchanges_succeeded": 7,
    "provider_exchanges_failed": 0,
    "response_processing_failed_exchanges": 0,
    "transport_started_exchanges": 7,
    "transport_not_started_exchanges": 0,
    "transport_attempts": 7,
    "transport_retries": 0,
    "usage_complete_exchanges": 7,
    "usage_unknown_exchanges": 0,
    "duration_ms": 62056,
    "outcome_counts": {"success": 7},
    "error_code_counts": {}
  }
}
```

现有 `completion_usage_totals` 保留，负责 token/cache 汇总。Provider Execution Evidence 负责执行次数、重试、错误与完整性，两者不合并。

TaskState 和 Session schema 不增加这些字段；它们属于 Run 审计，不属于恢复或会话连续性。

## 12. Live E2E report v2

live report 从 format version 1 硬切到 version 2，不提供 converter；live JSON 本来就被忽略且禁止提交。

删除含糊字段和参数：

```text
provider_calls                  → 删除
--max-provider-calls            → 删除
```

新增：

```text
provider_exchanges
provider_exchanges_succeeded
provider_exchanges_failed
response_processing_failed_exchanges
transport_started_exchanges
transport_not_started_exchanges
transport_attempts
transport_retries
usage_unknown_exchanges
--max-provider-exchanges
--max-transport-attempts
--request-timeout-seconds
--max-wall-seconds
```

报告示例应表达为：

```text
OpenAI: exchanges 7/15, transport attempts 7/15, retries 0, assertions all pass
Anthropic: exchanges 12/15, transport attempts 12/15, retries 0, assertions all pass
DeepSeek: exchanges 11/15, transport attempts 11/15, retries 0, assertions all pass
Ollama: not configured, live evidence unavailable
```

## 13. “全量通过”的正式定义

最终结果必须分别展示四个 gate，不能只写“安全审计通过”：

### Gate A：执行正确性

- 所有设计 turn 完成；
- `response_processing_failed_exchanges == 0`；
- Tool Action/Tool Result 规范配对；
- text/native transport 使用正确 action origin；
- 所有 task/report/trace 为 terminal。

### Gate B：费用与预算完整性

- Provider Exchange 与 Transport Attempt 都不超过上限；
- `transport_retries == 0`（第一阶段）；
- 成功 exchange 的 usage 全部可用；
- `usage_unknown_exchanges == 0`；
- input+output tokens 与 wall time 不超过上限。

### Gate C：凭据与产物安全

- Provider payload 不含 selected API key；
- active artifact secret hits 为 0；
- private files 为 0600，directories 为 0700；
- live report 不包含 prompt、answer、header、URL 或 response body。

### Gate D：恢复与持久化完整性

- fixture seed、临时 `pico.toml` 与 backup 全部恢复；
- Canonical Messages 当前且配对正确；
- session、task state、report、trace 完整终态化；
- live JSON 被忽略且不进入 Git。

只有 A/B/C/D 全部通过，才能报告“全量通过”。Provider 未配置时必须报告 `not configured`，不能计为通过或失败。

## 14. 失败分类

| 场景 | Run 结果 | usage | 审计结论 |
| --- | --- | --- | --- |
| Provider 正常返回 | 继续解码 Action | complete/missing | missing 时费用 gate 失败 |
| HTTP 4xx/5xx | `model_error` | unknown | 执行与费用 gate 失败 |
| timeout/断线 | `model_error` | unknown | 标记 ambiguous billing，不自动 retry |
| malformed response | `model_error` | unknown 或 complete | 执行 gate 失败 |
| `RetryAction` | 新 Model Attempt | 上一响应 usage 计入 | 不是 Transport Retry |
| 用户再次运行 | 新 Run | 独立统计 | 不是 Transport Retry |
| Provider 未配置 | 不启动 live run | unavailable | `not configured` |

## 15. 验收测试矩阵

### 15.1 Provider 单元测试

- 每类 Provider 成功时 evidence 为 attempts=1、retries=0；
- HTTP、network、timeout、invalid response 都产生固定 error code；
- error body、URL、header 与 key 不进入异常或 evidence；
- 默认错误只执行一次底层 HTTP；
- usage complete/missing/unknown 分类正确；
- Ollama token 字段保持当前映射。

### 15.2 Adapter 测试

- `TextProtocolAdapter` 同步 inner usage 与 transport evidence；
- evidence 不进入 text prompt；
- native/text action origin 保持现有合同。

### 15.3 AgentLoop 测试

- 每个 `model_requested` 配对一个 `provider_exchange_finished`；
- 成功 exchange 配对一个 `model_turn`；
- response 已返回但 action/trace 处理失败时单独计数，不能归因给 transport；
- 失败 exchange 仍写 evidence 并 terminalize；
- `RetryAction` 增加 Model Attempt，不增加 Transport Retry；
- tool roundtrip 的多个 exchange 被正确计数。

### 15.4 Report/安全测试

- aggregate 等式全部成立；
- incomplete evidence fail closed；
- secret/mode/redaction 现有测试继续通过；
- TaskState/Session/Checkpoint schema 不变。

### 15.5 Live E2E

- OpenAI、Anthropic、DeepSeek 分别授权、分别执行；
- Ollama 只有配置且 doctor 确认服务与模型可用时才执行；
- 每家都输出四个 gate，而不是一行含糊的“安全审计通过”。

## 16. 实施顺序

### 阶段 1：证据与错误合同

- 在 Provider-neutral 边界增加固定 evidence builder 与 `ProviderError`；
- 先补成功、失败、secret-free 单元测试；
- 不改 AgentLoop 行为。

### 阶段 2：Provider client

- OpenAI、Anthropic、Ollama 生成 evidence；DeepSeek复用 Anthropic client；
- 移除默认自动 transport replay；
- TextProtocolAdapter 同步 evidence。

### 阶段 3：AgentLoop 与 report

- 成功/失败都写一个 `provider_exchange_finished`；
- 聚合 `provider_execution`；
- 保持 TaskState、Session、Recovery schema 不变。

### 阶段 4：live harness v2

- 硬切字段、CLI 参数和报告版本；
- 新增 exchange/transport/usage consistency assertions；
- 分离 request timeout 与 observed wall cap。

### 阶段 5：文档与验证

- 更新 architecture、verification 和示例输出；
- Ruff、全量 pytest、provider 定向测试、build、CI；
- 最后取得逐 Provider 明确授权并生成 redacted live evidence。

## 17. 完成标准

- 不再出现含义不明确的 `provider_calls` 字段或用户文案；
- 任一 Run 都能区分 Model Attempt、Provider Exchange、Transport Attempt、Transport Retry；
- 任一 Provider 失败都有固定、脱敏、终态化 evidence；
- 默认不自动重放生成请求；
- live E2E 能证明四个 gate，并明确区分 passed、failed、not configured；
- 所有现有安全、恢复、持久化和 Provider transport 合同继续通过。

## 18. 文件级实施地图

| 文件 | 目标改动 | 明确不做 |
| --- | --- | --- |
| `pico/providers/response.py` | 定义最小 `ProviderError` 合同 | 不保存 wire body 或 URL |
| `pico/providers/_shared.py` | 固定 evidence 创建/校验 helper | 不做 Provider registry |
| `pico/providers/openai_compatible.py` | 生成 evidence，取消默认 replay | 不改变 Responses API payload |
| `pico/providers/anthropic_compatible.py` | 生成 evidence，取消默认 replay | 不改变 native tools/cache 合同 |
| `pico/providers/ollama.py` | 生成单-attempt evidence | 不要求未配置机器运行 Ollama |
| `pico/providers/text_protocol_adapter.py` | 同步 inner evidence | 不把 evidence 注入 prompt |
| `pico/providers/fake.py` | 生成确定性 evidence | 不模拟网络栈 |
| `pico/agent_loop.py` | 写 exchange event、聚合运行期计数 | 不解析 Provider wire data |
| `pico/runtime.py` | report 增加 `provider_execution` | 不修改 Session/Recovery schema |
| `benchmarks/live_e2e/run_live_session.py` | report v2、显式预算和四 gate | 不兼容读取 v1 live JSON |
| `tests/test_provider_*.py` | Provider 成功/失败/secret/replay 测试 | 不访问真实网络 |
| `tests/test_agent_loop.py` | trace pairing 与失败终态测试 | 不依赖具体 Provider |
| `tests/test_runtime_report.py` | aggregate 不变量测试 | 不复制 live harness 断言 |
| `benchmarks/live_e2e/tests/test_assertions.py` | v2 预算、安全与一致性 gate | 不弱化现有 43 项行为门禁 |
| `docs/verification.md` | 新证据口径与授权边界 | 不提交 live JSON |

## 19. 兼容与硬切策略

- Provider 的 runtime-facing `complete(...)` 合同保持不变；自定义 client 可暂时没有 evidence。
- `Response` 的 stop/content/usage 合同保持不变，Transport Evidence 不塞入 Model Response 内容。
- TaskState、Session、Checkpoint Record、Tool Change Record 均不升级格式。
- 普通 run report 只增加 `provider_execution`，不提供历史重写。
- live report 是独立 reader 消费的顶层 record，因此 format version 从 1 直接切到 2。
- `--max-provider-calls` 直接删除，不保留 alias；项目维护不变量明确禁止 deprecated alias。
- 旧 live JSON 继续保持 ignored/private，不提供 converter，也不作为新证据读取。

## 20. 风险与控制

| 风险 | 影响 | 控制 |
| --- | --- | --- |
| 取消自动 replay 降低瞬时可用性 | 网络抖动时 Run 更快失败 | 用户显式启动新 Run；保留完整 failure evidence |
| timeout 后 Provider 可能已计费 | 本地无法获得精确 token | `usage_status=unknown`，费用 gate fail closed |
| `last_transport_evidence` 是可变状态 | 并发共享 client 会串证据 | 当前同步模型立即复制；未来并发前先改 per-call envelope |
| 自定义 Provider 没有 evidence | 普通运行可用但审计不完整 | runtime 不崩溃，live/benchmark gate fail closed |
| 本地 wall cap 不能强制抢占 | 单请求可能让实际 wall 超限 | request timeout 限制阻塞；CI 用进程级 timeout |
| 过多 trace 事件增加噪声和泄漏面 | 审阅困难 | 每个 exchange 只写一个固定 schema 聚合事件 |
| 错误分类携带原始异常 | 可能泄漏 URL/凭据/body | 固定枚举与整数状态码；禁止 raw exception string |

## 21. 明确拒绝的替代方案

### 21.1 继续使用 `provider_calls`

拒绝。这个字段无法区分成功 Model Response、失败 Provider Exchange 与 HTTP retry，已经造成错误解读。

### 21.2 每次 HTTP attempt 写独立 trace

拒绝。它增加 Trace Timeline 噪声、持久化次数和泄漏面；聚合 evidence 已足够回答次数、retry 与 outcome。

### 21.3 新增通用 Provider registry/capability probe

拒绝。当前 transport 在构造时已经显式确定，registry 会引入第二套选择真源并违反现有架构。

### 21.4 暴露 `--retries N`

拒绝。没有幂等合同前，它只是把重复计费风险交给用户配置，不能把不安全策略变安全。

### 21.5 引入 requests/httpx/async runtime

拒绝。当前问题只需要固定 evidence 和保守 retry policy；新增运行时依赖不能解决幂等与计费不确定性。
