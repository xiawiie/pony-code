# Model Target、预算、工具结果与 TUI 设计

- 状态：Proposed，尚未改变当前运行时行为
- 日期：2026-08-14
- 决策记录：[ADR-0051](adr/0051-model-compatibility-contract.md)、
  [ADR-0052](adr/0052-safe-streaming-preview.md)
- 修订：[ADR-0044](adr/0044-provider-auto-resolution.md)、
  [ADR-0047](adr/0047-session-scoped-model-switching.md)
- 起因：[PR #16](https://github.com/xiawiie/pony-code/pull/16)

## 1. 最终结论

最适合 Pony 的不是型号表、自动窗口探测或统一 OpenAI adapter，而是四条独立且可组合的合同：

| 主线 | 决策 |
| --- | --- |
| Model 接入 | model id 是不透明路由值；符合四种既有协议之一即可零登记进入对应 adapter |
| 请求预算 | 所有未显式配置的 Target 统一维持 `128000 context / 16384 output` |
| 工具结果 | 可分页结果返回原文页和 continuation；不可重放大结果私有保存并返回可恢复 preview |
| 交互状态 | 借鉴 Pi 的状态可见性，保留 Pony 行内 TUI，以 Working、Running、Result、Answer、Input 五态区分聊天与卡死 |
| 流式反馈 | 交互 TUI 由 `--stream` 显式启用安全文本 preview；终态 `Response`、Canonical Messages 和工具执行路径保持唯一权威 |

`256000` context 复用现有 `--context-window 256000` 或 `pony.toml` 的
`[model].context_window = 256000` 显式启用。它是 Pony 的请求策略，不是对远端模型物理上限的认证；未显式修改
output 时仍保持 `16384`。Pony 不因 model id 陌生而发 warning、降到 `32K/8K`、探测最大窗口或修改配置。

`openai_responses` 与 `openai_chat_completions` 不能合并为一个 wire protocol。二者可以继续共用 HTTP、JSON、
错误脱敏和 usage 规范化原语，但 request envelope、tool call/result continuation、opaque state 与 output 字段不同，
必须保留两个 adapter 和两个 Session protocol。

## 2. Review 后否决的方案

以下方向不进入 Pony 1.0：

- **未知型号成功后写入内置表**：一次检查不能证明长期可用、最大窗口或其他 endpoint，写表会重新引入更新、过期、
  误命中和 warning 故障面；检查只报告当次事实。
- **`32K/8K portable_default`**：它会把现行 `128K/16K` 静默降配，且不能保证更小部署一定兼容。
- **按 model name 猜预算或协议**：同名 model 在官方 endpoint、自定义 gateway 和本地部署上可能完全不同。
- **运行真实任务失败后在 Responses/Chat 间 fallback**：这会重复用户任务、工具副作用和费用，违反 Session binding。
- **自动二分探测最大 context/output**：费用高、延迟大、结论容易受账户、gateway、限流和瞬时状态影响。
- **预建 `TargetOptions`/model-family registry**：它是换名后的型号 catalog；真实 wire incompatibility 出现后再在所属
  adapter 内做最窄、带 contract test 的修复。
- **继续只返回 200 行或把正文压成 512-token digest**：两者都会让大窗口名义存在、实际不可用。
- **全屏 transcript、主题系统或第二事件总线**：现有行内 TUI 足以修复聊天识别和活动反馈，不需要新依赖。
- **把四种流协议合并成通用 event parser**：SSE/NDJSON 只是 framing，事件类型、终止条件、usage、tool call 和错误语义仍由
  各 adapter 所有；统一 parser 会把协议差异藏进分支。
- **逐 delta 直接打印或持久化 preview**：secret 可能跨 chunk，tool arguments/reasoning 也可能混入；preview 必须先经过
  有界安全投影，且只存在于内存动态区域。

## 3. 已确认的根因

### 3.1 Model 与预算

- `pony/agent/model_capabilities.py` 只有一个内置型号记录，数值与 fallback 相同，却让所有其他型号进入
  `unknown model` warning 分支。
- 当前默认已经是 `128000/16384`，CLI 和 `pony.toml` 也已有显式覆盖入口；不需要新增 `.env` 字段、profile picker
  或 model metadata。
- CLI 的 `--max-output-tokens` 当前上限为 32768，而 `[model].output_limit` 允许到 384000；同一策略入口不一致，
  会让 CLI 用户遭遇额外的隐性限制。
- model 的路由身份、Pony 的请求预算和 Provider 的物理上限被 `ModelCapabilities` 一个名字混在一起。
- `doctor --check-api` 的最小 tool loop 只能证明当次 request/tool continuation，不证明 128K/256K 请求、质量或长期稳定。

### 3.2 文件读取与工具结果

- `read_file` 的 200 行限制同时存在于 schema、validation 和 implementation，用户显式请求更大范围也被拒绝。
- Agent Loop 默认在 4096 token 后把工具结果改成最多 512 token 的语义 digest。
- 完整脱敏结果虽写入当前 Run 的私有目录，Provider 只得到 `raw_result_id`，却没有可调用的恢复工具。
- 因此少于 200 行的文件也可能丢失正文。例如 196 行 `CHANGELOG.md` 约 5145 token，当前结果只剩头尾摘要；
  180 行 `docs/security.md` 约 4142 token，也会触发 digest。

对 365 个 Git 管理或未忽略的 UTF-8 源码/文档文件进行离线测量：

| 单页策略 | 完整覆盖 | 总分页数 |
| --- | ---: | ---: |
| 当前 200 行 | 53.7% | 795 |
| 10 KiB fallback 近似 | 63.8% | 639 |
| 2000 行 / 50 KiB | 97.0% | 377 |
| 2000 行 / 50 KiB / 16384 token | 97.0% | 377 |

仓库文件行数中位数为 180、P95 为 1154、最大 3071；字节 P95 为 41310、最大 118638。三重边界在当前样本
中的单页最大约 15290 token，能显著减少无意义翻页，同时保持结果有界。

### 3.3 TUI

- `tool_started` 立即写入永久工具行，但 `tool_executed(ok)` 完全静默，开始和完成无法区分。
- 工具开始时 `Working…` 被清除，长时间 shell/read 期间界面像卡住。
- TUI listener 没有收到分页、截断、范围或恢复状态，只能显示 `read <path>`。
- `--no-color` 时用户块缺少非颜色边界，输入 prompt 只是一个空格，用户难以识别“正在聊天”和“等待输入”。
- 启动时 `<112` 列会正确拒绝，但运行中缩窄仍可能让冻结的完整 Logo 溢出。

### 3.4 Streaming

- `pony/providers/transport.py` 当前一次读取完整 response body，四个 adapter 只有终态 `complete()`；OpenAI/Ollama 显式发送
  `stream=false`，真实 TTFT 不可观察。
- 现有 UI listener 只在 durable trace append 后收到脱敏副本，这对 Tool/Model 状态正确，但不能承载先于终态 durable event
  出现的文本 preview。
- 把 raw delta 直接交给 renderer 会让跨 chunk secret 绕过逐字符串脱敏；把 preview 写入 trace/Session 又会制造第二份、不完整
  且可能与 Final 不一致的 transcript。

## 4. Model 与 Protocol 合同

### 4.1 Exact ModelTarget

```text
identity = protocol_family + model + endpoint_hash
```

- `model` 是不透明字符串，只做长度、单行、结构和 secret 校验。
- Target 不包含 `known`、`supported`、quality、price、context 或 max-output 布尔/数值。
- 同名 model 位于不同 endpoint 时是不同 Target。
- Session binding、诊断和 optional endpoint behavior 都以 exact Target 的 protocol/endpoint 为边界。
- 真实用户任务失败后不切换 protocol、endpoint 或 model 重放。

### 4.2 四种固定协议

| Provider | Session protocol | Adapter 责任 |
| --- | --- | --- |
| `anthropic` | `anthropic_messages` | Messages、tool use/result、usage、opaque thinking state |
| `openai-responses` | `openai_responses` | Responses input/items、function call/output、usage、opaque state |
| `openai-chat` | `openai_chat_completions` | Chat messages、tool calls/results、finish reason |
| `ollama` | `ollama_chat` | Ollama chat、tools、tool continuation |

`openai` 仍只是 Chat/Responses family selector，不是最终 Session binding。共享代码只下沉已经存在的 transport、JSON、
redaction 和 error helper；不创建一个带大量 mode 分支的统一 OpenAI adapter。

每个 adapter 只提供：

1. 所属 protocol 的必要 `ProtocolCore`；
2. 有一手证据、只按 exact canonical endpoint 生效的 `EndpointOptions`。

认证继续由四个强制 Provider 静态决定，不属于 endpoint option。Pony 不预建 Target/model-family option registry。以后若真实
型号必须使用不同 wire 字段才能完成基础工具回路，应在所属 adapter 内单独 review、实现最窄条件并增加 contract test；该条件
不得影响预算、准入、推荐、UI 或 Session 切换。

### 4.3 Resolution 与检查

- 显式 `anthropic|openai-responses|openai-chat|ollama` 直接选择 adapter，不因 model id 陌生而 probe。
- `auto` 或 `openai` family 无法唯一解析时，发送用户任务前执行既有 bounded synthetic resolution；它只选择 protocol。
- `doctor --check-api` 在用户显式执行时验证一次最小 native tool call 和 tool-result continuation。
- 成功事实命名为 `tool_loop_checked`，只投影 exact Target、protocol、probe call count、tool call、continuation 和 usage
  是否存在；不写 catalog、cache 或 `supported=true`。
- 普通 `status` 零网络；`init` 只有在 resolution 完整成功后才写 resolved Provider。

这修订 ADR-0044 的 generic gateway 表述：强制探针属于 `auto/openai` 的 protocol resolution，不是所有显式 protocol Target
的资格门槛。

## 5. 请求预算合同

### 5.1 物理事实与产品策略分离

Pony 只能控制本地请求规划，不能从 model name 知道远端物理上限：

```text
W = configured context_window
O = configured output_limit
R = max(configured compaction reserve, O)
I = W - R
```

整个 profile 仍使用现有交叉约束：`I` 至少保留 16384 token，system/tools、source pool、recent tail 与 summary 不得
超过各自 hard cap。提高 output 必须同步提高 reserve；提高 context 而不提高 output 时，额外容量归 history，不虚增
Provider 的生成上限。

### 5.2 默认和显式扩展

| 场景 | Context | Output | 来源 |
| --- | ---: | ---: | --- |
| 未显式配置 | 128000 | 16384 | Pony 产品默认 |
| 显式扩大窗口 | 256000 | 16384 | CLI 或 `[model]` |
| 显式自定义 | 用户值 | 用户值 | CLI 优先于 `[model]` |

规则：

- 所有 model id 使用相同默认，不存在 known/unknown 分支或 warning。
- `256000` 不是命名 profile，也不写型号表；用户可配置其他通过现有 validator 的数值。
- CLI 与 `[model]` 必须复用同一个 output safety ceiling 和同一组 `W/O/R/I` 交叉校验。实施时以当前项目配置上限
  384000 为单一 Pony ceiling，删除 CLI 独有的 32768 cap；它仍不是 Provider capability 声明。
- 小窗口 deployment 由用户显式调低；Pony 不在远端报错后自动降档或永久改写配置。
- Provider 返回 limit error 时报告 exact Target、request stage 和稳定错误码，不回显不可信远端正文。
- 高级 `[context]` 字段继续按当前公式派生/校验；本方案不新增逐字段 provenance 公共模型。
- 诊断只投影 `policy_source=default|project|cli|mixed` 和最终有效数值，不声称它们是 Provider capability。

当前 `ModelCapabilities` 最终应收敛为 Provider-neutral 的 `RequestBudget`/`RequestPolicy` 语义：删除
`BUILTIN_MODEL_CAPABILITIES` 和 warning，保留 token counter 与现有预算公式。模型切换重新装配 client、budget、counter 和
delegate factory，但不查询型号 catalog。有效 `O` 等于通过交叉校验的显式/默认策略值，不再与 model-name max 做静默
`min()`。这修订 ADR-0047 中“安装 model capabilities”的表述。

### 5.3 远端 output enforcement

`O` 始终为 Pony 的本地规划和 reserve 上限；是否能写入 Provider payload 是独立事实：

| 值 | 含义 |
| --- | --- |
| `enforced` | adapter 使用已验证的协议字段把 output limit 发送给远端 |
| `local_only` | Pony 只为本地 context/reserve 使用该值，不能证明远端生成受同一数值硬限制 |

Phase 1 保留每个 adapter 当前已经有 fixture 覆盖的 request shape，不为追求“统一”一次性删除所有 output 字段。后续只有在一手
schema 和 contract test 证明字段作用域后才调整。`local_only` 作为 status/doctor/JSON 的低干扰事实，不在每次交互前重复 warning；
一次性 `pony run` 如需提示，只在 stderr 输出一次。

Transport 的 response-body bound、timeout 和“一次 Model Attempt 最多一次请求”只保护本地资源，不能描述为远端 generation
或计费上限。

## 6. 工具结果交付合同

### 6.1 先分类，再决定截断方式

| 结果类型 | 示例 | 交付方式 |
| --- | --- | --- |
| 可分页原文 | `read_file`、`memory_read`、raw result read | 原文 head page + 准确 continuation |
| 可重新查询列表 | `list_files`、`search`、`memory_search` | 工具自身有界，必要时增加查询级 continuation |
| 不可重放输出 | `run_shell` | head-tail preview + 当前 Run 私有完整结果 |
| 小型状态 | write/patch/permission/plan 结果 | 直接 inline |

可分页工具不得进入语义 digest。不可重放结果不得只留下无法操作的 bullets 或 hash。
delegate 继续使用现有 bounded 返回合同；它的跨 child Run 完整结果归属与保留不是本阶段问题，出现真实恢复需求后独立设计。

### 6.2 `read_file` 页面

保留现有 `start/end`，不迁移为 `offset/limit`。`start` 默认 1；`end` 省略表示读到 EOF，显式 `end` 才限制请求范围。
请求范围不是绕过安全上限的方法：显式范围再大，也只返回一个安全页。

默认页面同时受三个上限约束，任一先到即停止：

```text
max_lines  = 2000
max_bytes  = 50 KiB UTF-8
max_tokens = context.tool_results.inline_tokens  # 默认 16384
```

计算顺序固定为：安全读取并解码、脱敏、构造页面头/正文/continuation、再以模型可见的最终文本计算 UTF-8 bytes 和 token。
因此 redaction 改变长度时不会让模型可见内容越界。普通页面只在完整行边界停止；读取使用 `splitlines(keepends=True)`，
正文保留原始 `LF`/`CRLF` 和末尾换行。

若首个未读逻辑行本身超过 byte/token 上限，`start_byte` 表示**脱敏后当前 `start` 行内**的 UTF-8 byte offset。
分页器只产生 code-point boundary 上的 offset，外部传入落在多字节字符中间的值稳定拒绝。该行仍有剩余时，continuation
保持同一个 `start` 并推进 `start_byte`；该行完成后，下一页使用 `start + 1` 且省略 `start_byte`。

第一页同时返回完整脱敏 source 的 SHA-256；后续 continuation 携带 `expected_sha256`。文件、Memory 或 redaction snapshot
在分页间变化时返回 `tool_result_source_changed`，不得把两个版本静默拼接。raw result 的 id 本身就是 expected hash。

Provider-visible 格式保持简单且可操作：

```text
[page] {"path":"src/app.py","start":1,"end":802,"total_lines":3071,"range_complete":false,"file_complete":false}
<脱敏后的原始正文，不添加逐行行号>
[continuation] {"path":"src/app.py","start":803,"expected_sha256":"<64 hex>"}
```

- schema 不再为 `end` 注入数值默认；2000 只是单页行数上限。validation 不再以请求范围超过 2000 为错误。
- 正文不添加行号，便于直接复用为 `patch_file.old_text`；路径和实际范围只在头部出现一次。
- continuation 是下一次工具参数，不是自然语言建议；完成本次请求范围时 `range_complete=true` 且没有 continuation；
  显式 `end` 早于 EOF 时允许 `range_complete=true,file_complete=false`。
- 空范围、EOF、超长单行 fragment 和显式子范围都有稳定、无循环语义。
- `inline_tokens` 的公开最小值从 1 提高到 256，`digest_tokens` 的最小值提高到 128；更小的旧配置以稳定配置错误拒绝，
  不静默改用默认值。若异常长 path 仍使最小 envelope 无法容纳，返回 `tool_result_budget_too_small`，不得返回空页或
  生成指向同一位置的 continuation。
- `memory_read` 复用相同页面合同，不再维护第二套 200 行逻辑。

64 KiB ASCII 单行 continuation 示例（32768 必然位于 UTF-8 code-point boundary）：

```text
[page] {"start":40,"start_byte":0,"end":40,"range_complete":false}
<第 40 行的有界前缀>
[continuation] {"path":"data.min.json","start":40,"start_byte":32768,
                "end":40,"expected_sha256":"<64 hex>"}
```

### 6.3 不可重放大结果与恢复

默认 `context.tool_results.inline_tokens` 从 4096 提高到 16384。结果超过该值时：

1. 先脱敏；
2. 把完整脱敏文本写入当前 Run 的私有 `tool_results` 存储；
3. 写入成功后返回有界 head-tail preview、完整 content hash 和 `raw_result_id`；
4. 写入失败时仍返回 preview，但必须标记 `recoverable=false`，不得生成 `raw_result_id`，也不得重跑原工具；
5. TUI 明确显示 truncated 及真实恢复状态。

新增一个模型可见的只读恢复工具，名称在实现前固定为 `read_tool_result`：

- 只接受 `tool_result:<64 lowercase sha256>`、`start/end` 和必要时的 `start_byte`；16 位 `source_hash` 不能继续作为
  文件身份；
- 只通过 runtime 提供的 `read_current_tool_result(id)` capability 解析当前 top-level Run，不把 RunStore/root 暴露给工具，
  也不接受 host path、run id、Session id、glob 或 traversal；
- 复用 `read_file` 的三重页面和 continuation；
- capability 只在 `current_task_state.status == running` 时可用；历史或 terminal Run 的 id 返回 `tool_result_expired`；
- RunStore 对完整 hash 使用 private bounded read，重算 SHA-256 后必须与 id 相等；id 不存在、identity drift、hardlink、
  symlink/special file、超限或内容变化均稳定 fail closed；
- 单个 raw result 最多 4 MiB，复用现有 process capture hard cap；每个 Run 的 `tool_results` 最多 8 MiB、最多 100 个
  distinct full-hash 文件。写入前计算新内容和当前 subtree 总量，读取时重新检查实际 stat；同 hash 已存在时先验证完整内容，
  不重复计数；达到任一上限即 `tool_result_retention_failed`，不淘汰旧结果；
- write 只持久化已经脱敏的 `safe_content`，并同时设置 existing-file 上限；
- tool result 的可恢复生命周期明确为当前 top-level Run。Provider-visible preview 必须包含
  `scope=current_run expires=end_of_turn`，不能暗示跨 turn 可用。

Session 仍保存模型实际看到的 preview 和 expiry marker 作为 Canonical Messages。后续 turn 会看到明确已过期的引用，调用
`read_tool_result` 稳定返回 `tool_result_expired`。跨 turn 永久恢复需要 Session-to-Run ownership/index 和保留策略，超出 Pony 1.0
边界；本方案不以全仓扫描 `.pony/runs` 或放宽当前 Run capability 冒充实现。

当前 `digest_tokens` 在第一阶段只作为 overflow preview 的上限继续生效，不用于可分页原文；不为字段改名新增兼容层。等真实使用
证明需要重命名时，再做独立配置迁移。

### 6.4 单一 shaping owner

不得让工具层分页后又被 Agent Loop 二次 digest。最小实现边界是：

- runtime 在 top-level turn 开始时从当前 `inline_tokens`、`RequestBudget`、`TokenAccounting` 和 redactor 构造冻结的
  `ResultPagePolicy`，与 permission/tool schema 一起交给 `Executor.begin_permission_turn()`；executor 在每次调用时把该 snapshot
  交给分页 owner，长期存活的 runner/`ToolContext` 不捕获 counter。model switch 只影响下一 turn 的新 snapshot；
- `read_file`/`memory_read` 返回一个内部 `ToolOutput(content, result_view)`；其他现有 string runner 由 executor 规范化为
  `ToolOutput(content, {})`；
- executor 把 `result_view` 放入现有 `ToolExecutionResult.metadata`；
- Agent Loop 看到 `result_view.delivery=page` 时直接使用已成形正文，只有不可分页结果进入 spill/preview；
- Session 继续保存模型实际看到的脱敏正文/preview，不保存原始 secret 或 host path。

`ToolOutput` 与唯一分页器位于窄化的 `pony/tools/result_view.py`，由 workspace read、memory read、raw-result read 和 executor
四个真实调用方共用；它不是新 registry、service 或公共 API。

`result_view` 的合法组合在 B/C 开始前冻结如下，不能只冻结 key/type 而让 producer 和 renderer 分别猜值语义：

| `delivery` | `truncated` | range / continuation | `reasons` | `recoverable` |
| --- | --- | --- | --- | --- |
| `inline` | 必须为 `false` | 全部省略 | 省略或空 | 省略 |
| `page` | 必须为 `false` | range 必须完整；未完成时 `next_start` / `next_start_byte` 二选一 | 未完成时列出触发的页面边界 | 省略 |
| `preview` | 必须为 `true` | 全部省略 | 必须包含至少一个截断原因 | 必须为 bool，只表示完整脱敏结果是否保留到本 turn 结束 |

`page` 是无损分页而不是截断；是否还有内容只由 `next_*` 表示。完成页没有 `next_*`，`reasons` 省略或为空。`preview` 的
`recoverable=true` 不承诺跨 turn。任一未知 key、错误类型、非法组合或互斥 continuation 同时出现时，TUI 只写安全 generic
receipt（例如 `! read completed · result details unavailable`），不得抛错、猜测恢复能力或改变 Agent 结果。

## 7. TUI 交互合同

### 7.1 参考 Pi，但不复制 Pi

本设计以 Pi 官方 `Using Pi`、`TUI Components` 和固定源码版本
`9d2ec7ffabe927bfad2214c1cee25b6632a78dcf`（2026-08-13）为交互参考。借鉴对象是状态可见性和组件生命周期，不是品牌外观、
扩展架构或功能清单：

| Pi 的机制 | 解决的问题 | Pony 的采用方式 |
| --- | --- | --- |
| 稳定的 transcript / status / editor / footer 排列 | 用户始终知道输出和输入分别在哪里 | 欢迎资产只在启动出现；其后保持对话区、瞬态活动行、输入框和 footer 的稳定顺序 |
| 用户消息有低对比背景，Assistant 使用无背景 Markdown | 角色依靠版式而非重复标签区分 | 用户块保留无标签并增加细侧轨；Assistant 增加低对比 `Pony` 标识并继续使用安全 Markdown |
| Working indicator 有明确创建、更新和 dispose 生命周期 | 长模型请求或工具执行不再像卡死 | `TuiRenderer` 只维护一个可清除活动行，在 Working 与具体工具动作间原位替换 |
| Tool 有 pending / success / error 外观并可折叠 | 调用、完成和失败不会混为一谈 | Phase 1 用“瞬态运行 + 一条永久语义回执”表达，不复制大块 tool card，也不显示完整工具正文 |
| 忙碌时仍可输入，pending 状态靠近 editor | follow-up 不会被误当成已经发送的对话 | 沿用 Pony 单一 FIFO、最多五条；editor header 显示瞬态 `Queued N/5`，真正开始执行时才渲染用户块 |
| Footer 按宽度保留最重要事实 | 状态可扫描且不挤压输入 | 保留 Pony 已冻结的仓库/分支、permission、Provider/model，并继续按安全和模型信息优先降级 |

以下 Pi 能力明确不进入本阶段：fullscreen transcript、主题系统、Extension TUI API、可展开完整工具输出、两类 queue delivery、
shell 快捷模式和 Provider reasoning block。Pony 只有一个现有 prompt-toolkit UI、一个 listener 和一个 FIFO；`Working…` 仅表示
模型请求未完成，绝不展示或持久化 Provider reasoning。

### 7.2 视觉层级和状态

现有行内 TUI、完整欢迎资产、toolbar、approval、queue 和 Markdown renderer 全部保留。运行期固定为四层，不能让临时状态写进
Assistant 正文，也不能把 footer 当活动日志：

```text
conversation  用户块 / Pony 回答 / 永久工具回执
activity      当前唯一的 Working / Reading / Running 状态
editor        可选 Queued N/5 header + 非空 › 提示 + 最多六行输入
footer        repo(branch)                  permission · provider/model
```

| 状态 | 可见行为 | 是否进入永久对话区 |
| --- | --- | --- |
| Waiting input | 非空 `› ` prompt、footer；没有残留活动文字 | 否 |
| Model working | 瞬态 `Working…`，只说明请求进行中 | 否 |
| Tool running | 瞬态 `Reading …` / `Searching …` / `Running …` / `Writing …` | 否 |
| Tool result | 一条完成、分页、截断、拒绝、中断或失败回执 | 是 |
| Assistant answer | 低对比 `Pony` 标识 + 安全 Markdown | 是 |

典型状态序列是：

```text
Input -> Working -> Tool running -> Tool result -> Working -> Answer -> Input
```

该序列由真实事件驱动，不补造中间状态。`tool_executed` 后只有收到下一次 `model_requested` 才重新显示 `Working…`；Final、
error、interrupt、approval 和关闭路径都必须幂等清除活动行。

最小无色稳态线框如下；颜色只能增强，不得成为唯一差异：

```text
│ 帮我检查这个文件为什么只读到一部分

✓ read docs/security.md · lines 1-802/1519 · more from 803

Pony
文件只完成了第一页读取；下一页应从 803 行继续。

────────────────────────────────────────────────────────────────
›
 project (main)                         manual · openai/gpt-5.6
```

用户消息继续使用低对比块且不加角色标签；左侧 `│` 在彩色和 `NO_COLOR` 下都存在。Assistant 标识只在每个 Assistant answer
块开头出现一次，不包卡片、不增加背景。消息块之间只保留一个视觉间距。

### 7.3 唯一活动行

`TuiRenderer` 只把现有 `_working_visible/_working_width` 瞬态字段扩展为 `kind/summary/visible/width`，由现有 listener 事件
投影；不新增组件类型、第二事件总线、Session 状态或 durable trace。活动摘要只使用已经脱敏的 listener-only 参数，并继续
执行单行、控制字符和宽度约束：

| 事件 | UI 动作 |
| --- | --- |
| `model_requested` | `set_activity(model, "Working…")` |
| `tool_started` | 原位替换为工具特定动作，不写永久行 |
| `tool_executed` | 清除活动行，再写恰好一条永久结果回执 |
| `tool_interrupted` | 清除活动行，再写永久中断回执 |
| Final / error | 清除活动行，再写 Answer 或稳定错误 |
| approval | 隐藏活动行但保留 pending tool summary，把 approval 和 `[y/N]` 输入置于前台 |
| close | 幂等清除活动行并恢复终端状态 |

Phase 1 不要求动画。静态但准确的 `Working…`、`Reading …` 比新增 timer/thread 更可靠；若后续真实可用性测试证明需要动画，
只能在同一活动行内增加可关闭的低频帧，不能改变状态语义、写入 scrollback 或成为无色模式的唯一反馈。

`tool_started` 早于 permission prompt，因此 approval 不能丢弃 pending tool summary。approval callback 返回 `true` 后直接恢复原
Tool running 活动行，不等待不存在的第二个 `tool_started`；返回 `false` 后保持隐藏，等待随后的 `rejected` 永久回执。approval
异常仍按拒绝/中断 fail closed。该恢复只属于 presentation state，不新增 runtime event。

工具动作映射保持窄而稳定：read/memory/raw-result 使用 `Reading`，list/search/repo lookup 使用 `Listing/Searching`，shell 使用
`Running`，write/patch/memory save 使用 `Writing/Patching`，delegate 使用 `Delegating`，未知工具使用 `Running <tool>`。不得显示
绝对路径、未脱敏 command、raw id 或完整参数。

### 7.4 工具结果、queue 与 approval

`tool_executed` 清除活动状态并写一条语义回执，例如：

- `✓ read docs/security.md · lines 1-180/180`
- `✓ read pony/agent/loop.py · lines 1-802/1519 · more from 803`
- `! run tests · exit 0 · output truncated · recoverable until this turn ends`
- `× patch src/app.py · rejected · code=permission_denied`

成功、分页、可恢复截断、不可恢复截断和失败都必须从固定前缀及文字上可区分，不能只靠颜色。`partial_success`、`rejected`、
`error`、`interrupted` 和 effect unknown 永久可见。失败时优先完整保留第一行的 status 和 stable code；宽度不足时把安全 detail
放到缩进的后续行，不能裁掉 stable code。

TUI 不解析工具正文。runtime listener 在 durable trace append 之后，只注入已脱敏、仅内存的 `result_view` 白名单：

```text
delivery: "inline" | "page" | "preview"
truncated: bool
start_line/end_line/total_lines: int | omitted
next_start/next_start_byte: int | omitted
reasons: list["lines" | "bytes" | "tokens"]
recoverable: bool | omitted
exit_code: int | omitted
```

key、类型和 omission 规则是 Worktree B/C 的冻结合同；不包含 path、content、host path、run id 或 raw id。path 仅来自已有
listener-only 脱敏 tool args。durable trace 不新增工具正文、host path、raw id 或 secret；终端不显示 raw id。

忙碌期间提交的文字继续只进入一个最多五条的内存 FIFO。`Queued N/5` 是 prompt 动态区域中的 editor header，不占用唯一
activity、不进入 scrollback、footer 或 Canonical Messages，因此 `Running …` 与 `Queued 2/5` 可以同时可见。输入真正出队并触发
`on_start` 时才渲染用户块。Pony 不复制 Pi 的 steering/follow-up 两套 delivery。`/queue` 与 `/queue clear` 继续查询或清空
同一 FIFO，approval 始终优先占用输入；approval 回答不得进入 queue 或对话区。

### 7.5 Resize 和刷新纪律

112 列只作为启动和完整 Logo 门槛。运行中缩窄到 112 列以下时保留输入 buffer，并在 editor header 显示按当前宽度裁切的单行
扩宽提示；若同时有 pending input，同一行固定组合为 `Widen terminal · Queued N/5`，安全提示优先且两项都不能丢。conversation、
activity、approval、editor 和 footer 仍按当前真实宽度 wrap/bound，Tool result、Final 和 error 不能被
暂停或丢弃。恢复后清除提示并重绘 active activity、queue header、input 和 footer；不重复输出启动 Logo，也不创建缩小 Logo。
终端历史区如何 reflow 不属于应用可保证范围。

每次状态变化最多产生一次活动行清除/替换和一次必要的永久 append；不要轮询 transcript、重绘历史消息或为静态状态启动后台
刷新线程。listener/resize 的次生渲染异常不得覆盖已有 primary result，并必须恢复 hook 和终端输入所有权。approval UI 的
创建、渲染、读取或清理任一步异常都必须 fail closed：按拒绝/中断处理，工具不得执行，同时恢复 hook 和终端输入所有权。

## 8. Streaming 传输与瞬态 Preview 合同

### 8.1 启用范围与公共行为

首版 Streaming 是显式、瞬态的交互能力：

- 裸 `pony --stream` 与 `pony repl --stream` 只在完整交互 TUI 可用时启用；没有 `--stream` 时维持当前 final-only 行为。
- `pony run`、非 TTY fallback、doctor、probe、protocol resolution、compaction、benchmark grading 和 delegate child 保持
  final-only；在不支持的入口使用 `--stream` 返回稳定 usage error，不静默忽略。
- `stream` 请求选项只进入冻结的 `RuntimeOptions`，不作为配置或恢复状态写入 `.env`、`pony.toml`、Session、Run 或 model
  binding；resume 后仍由本次 CLI 显式选择。当前 Run 仍按 8.4 记录不含内容的低敏执行事实。
- 自定义 ModelClient 的现有 `complete()` 合同不变。Streaming 路径要求 client 提供独立 `complete_stream(...,
  on_stream_committed=..., on_text_delta=...)`；显式请求 streaming 而 client 不支持时，在发送网络请求前返回
  `streaming_unavailable`，不 fallback。
- `complete_stream()` 与 `complete()` 最终都返回相同 Provider-neutral `Response`。Agent 只在完整流成功结束、终态 Response
  通过 `decode_action()` 后才允许执行 Tool、追加 Canonical Messages 或 finalize。
- CLI 在 `build_agent` 前拒绝 `run`、管理命令和非完整 TUI 的 `--stream`。assembly 在得到 model client 后、调用
  `Pony(...)`/`Pony.from_session(...)` 前验证 `complete_stream` 可调用；Python API 的 `RuntimeOptions.stream=true` 还必须在
  `Pony._initialize()` 最前部、任何 Session/Run writer 配置前防御性复核。`run_repl` 在 plain fallback 和 prompt loop 前再复核
  TUI/client capability。失败返回 `streaming_unavailable`，Session 目录不得创建、已有 revision 不得变化，compaction/network
  均为零写。无 flag 时不检查该方法；delegate/worktree child 强制 `stream=false`。

这不增加统一 Provider adapter、第二 Session writer 或新的 runtime dependency。每个 adapter 可用私有共享函数复用本协议的
request building 和终态 decode，但不通过 boolean mode 把四种 wire protocol 合并。

内置 adapter 的可选公共方法冻结为：

```python
complete_stream(
    *,
    system,
    tools,
    messages,
    max_tokens,
    cache_breakpoints=None,
    on_stream_committed,
    on_text_delta,
) -> Response
```

前五个请求参数与 `complete()` 含义完全相同。两个 callback 在 Provider worker/调用线程内串行调用：adapter 先把首个有效 event
记为 committed 并冻结后续 failure 为不可 retry，再恰好调用一次 `on_stream_committed()`；同一 event 及后续 event 的普通文本才
按 wire 顺序调用零到多次 `on_text_delta(str)`。callback 返回值被 adapter 忽略，任何 presentation callback 异常只会禁用本次
请求的后续 callback，不能改变 committed、终态 assembly 或 `Response`；方法 return/raise 后不得再调用。Agent 提供这两个
exception-safe callback：`on_text_delta` 进入 `SafeTextPreview`，而后文 `on_safe_preview(snapshot) -> bool` 只是
`SafeTextPreview -> TUI` 的第二层 sink，不属于 ModelClient API。

### 8.2 四种 wire ownership

| Protocol | Framing | 普通文本 preview 来源 | 权威终态 |
| --- | --- | --- | --- |
| `anthropic_messages` | SSE | text content block 的 `text_delta` | message/content block/message delta 完整组装后 `message_stop` |
| `openai_responses` | SSE | `response.output_text.delta` | `response.completed` 中的完整 response，并与已见 item identity 校验 |
| `openai_chat_completions` | SSE | `choices[].delta.content` 的字符串部分 | 按 choice/index 组装 content/tool calls 和 usage-only chunk，直到 `[DONE]` |
| `ollama_chat` | NDJSON | 非终态 `message.content` | 所有 message/tool_calls 合并并由 `done=true` 对象提供 stop/usage |

Chat Streaming 请求固定发送标准 `stream_options={"include_usage": true}`；允许 `[DONE]` 前 `choices=[]` 的 usage-only chunk。
endpoint 拒绝该标准字段时准确失败，不移除字段重试；终态仍允许 usage 缺失并沿用现有 degraded usage 事实。

Adapter 必须拒绝未知 framing、非法 UTF-8、畸形 JSON、重复/倒退 wire index、缺失终止帧及超过边界的流。每个 adapter 提取一个
协议私有终态 decoder，由 `complete()` 与 `complete_stream()` 共用；多 tool call、文本/tool action 冲突和其他 action-level
事实继续交给现有 `decode_action()` 统一形成 RetryAction，Streaming 不增加更严格或不同的终态语义。reasoning/thinking、tool
argument delta、refusal detail、opaque Provider state 和错误正文永不进入 preview；其中协议所需的 opaque state 只进入终态
`Response.provider_state`。

共享 transport 只负责 HTTPS/redirect/credential 约束、response lifetime、严格 UTF-8、SSE/NDJSON framing 和资源上限。首版固定
单行 256 KiB、总计 16 MiB 和 100000 个 wire event；任一上限到达即稳定失败，不返回部分 Response。协议事件解析、tool
assembly、usage、stop reason 和 effective model 仍由所属 adapter 负责。

### 8.3 安全投影与 TUI 生命周期

Adapter 的 callback 只发已经识别为普通 Assistant 文本的 raw delta。Agent-side `SafeTextPreview` 是唯一安全投影 owner：

1. 以有界 buffer 累计到完整逻辑行，避免 secret 横跨 transport chunk；构造时若冻结 redaction snapshot 含 CR/LF，或任一已知
   secret 短于 `MIN_SECRET_SUBSTRING_REDACTION_LENGTH`，则整次禁用文本 preview；
2. 对完整行调用现有 runtime redaction，再执行控制字符和单行安全规整；Agent 不读取终端宽度，也不做显示裁剪；
3. 只把最新一条完整、有界的安全文本 snapshot 发送到 TUI 的单一动态 activity 区；
4. 未结束单行只在终态到达时随 Final 渲染，不作为 delta preview；单行超过 64 KiB 时停止本次文本 preview，继续显示
   `Receiving...` 并正常组装权威 Response。

即使 snapshot 可预检，跨行 PEM 也可能只存在于模型输出：任一完整行匹配 private-key BEGIN marker 时，必须在显示该行前永久
禁用本次剩余文本 preview。上述任一 fail-closed 条件都只保留 `Receiving...`，权威 Response 仍正常完成。空白、只含控制字符
或 redaction 后为空的行也只更新
`Receiving...`，不补造文本。TUI 只保留最新一条不超过 64 KiB 的完整安全 snapshot，并独占 `Pony - ` 前缀、当前终端宽度
裁剪和 resize 重投影；终端重新扩宽时必须从同一 snapshot 重算显示，不依赖新的 model delta。该 snapshot 不回传 Agent，也不进入
scrollback。

该 callback 是 `complete_stream()` 调用期间的单向 presentation capability，不是 trace listener 或事件总线：

- 不写 Session、Run trace、Canonical Messages、Checkpoint、Memory、log 或 benchmark artifact；
- 不携带 protocol event、request id、tool args、reasoning、raw id、usage 或 endpoint；
- callback/render 异常或 false acknowledgment 只禁用本次 preview，Provider 流继续完成，次生 UI 错误不得覆盖 primary result；
- success、Provider error、interrupt 和 close 都在 `finally` 幂等清除 preview，随后 Final Markdown 只渲染一次；
- `model_requested` durable listener 仍先显示 `Working...`。Adapter 在内部把首个合法 wire event 标记 committed 后调用无
  payload 的 `on_stream_committed()`，把状态改为 `Receiving...`；它不携带协议事件、不计 TTFT，异常只禁用状态更新。
- `SafeTextPreview` 调用 `on_safe_preview(snapshot) -> bool`；TUI 只有在同步完成有界投影且实际显示至少一个模型文本字符时
  返回 true。收到安全行后显示 `Pony - <preview>`；Tool 流只显示 `Receiving...`，不泄露参数。

为避免 UI 反压阻塞 Provider socket，首个安全 snapshot 只同步渲染一次以取得 TTFT acknowledgment；之后使用单槽
latest-snapshot coalescer，Provider worker 只覆盖槽位且不等待 UI，prompt-toolkit event loop 最多保留一个待刷新 callback，并以
100 ms 最小间隔限频。Final/error/close 先使当前 preview generation 失效，再 flush-clear；迟到 callback 必须因 generation mismatch
变成 no-op，不能在 Final 后重画。该机制不新增线程、transcript buffer 或第二事件总线。

因此“UI listener 只能在 durable trace append 后收到脱敏副本”的不变量保持不变；Streaming 使用的是更窄、无持久化能力的
preview callback，不向 listener 注入伪造 trace event。

### 8.4 失败、重试与可观察性

- HTTP status 或首个有效 wire event 前的连接失败沿用现有 retry classification，但 transport/adapter 不得在同一 Model Attempt
  内透明发送第二个 HTTP request；是否产生新 Model Attempt 由现有 Agent retry 策略决定，并复用 immutable snapshot。一旦收到
  任一有效 SSE/NDJSON event，该请求即 committed，之后的断连、timeout、decode、终止帧缺失或 callback-independent assembly
  failure 全部 `retryable=false`。
- 不允许从 Streaming 切到非流、切 protocol/endpoint/model 或重放同一真实用户任务。已经显示的 preview 只作为不完整瞬态
  反馈清除，错误区显示稳定 code；不得把 preview 当作 Final 保存或恢复。
- `last_transport_attempts`、usage 和现有 model failure trace 继续记录终态事实。Streaming 证据只放入既有
  `request_metadata.streaming` 固定子结构：`requested=true`、`committed: bool`、`preview_emitted: bool`、
  `first_preview_ms: nonnegative int | null`，不保存 delta 数量、文本或 wire event。
- Streaming attempt 的 `model_requested.request_metadata` 写初始 snapshot（committed/preview false、time null）；成功
  `model_turn` 或失败 `model_failed` 必须写终态 snapshot。`action_decoded` 因既有合同携带相同终态 request metadata，但不作为
  completion 证据。字段只能 false→true 单调，`preview_emitted => committed`，且 `first_preview_ms` 非空当且仅当
  `preview_emitted=true`。final-only attempt 完全省略 `streaming` key。
- `request_metadata` 已是 trace v1 的通用安全 mapping 字段；实现为 `streaming` 增加 exact-key/type/invariant validator 和 report
  projection，拒绝额外 key、错误类型及违反 `preview_emitted => committed`/time 等价的结构；不新增顶层 trace 字段、不 bump
  `TRACE_SCHEMA_VERSION`。失败路径必须把同一个 mutable attempt metadata 冻结为副本后交给
  `_record_model_failure()`，确保 committed partial failure 也有终态证据。
- `first_preview_ms` 是从 Agent 即将调用 `complete_stream()` 时取得的 monotonic timestamp，到首个已脱敏、实际可见的非空
  文本 preview；它是可复现的 safe-preview latency，不声称排除 adapter payload 构造时间或等同 wire-level TTFT。
  `SafeTextPreview` 使用可注入的
  monotonic clock，只有 `on_safe_preview()` 首次返回 true 才记录 `first_preview_ms`；false、异常、重复 callback、prefix、
  `Receiving...`、被宽度完全裁掉的文本、完整 body 和首 action 都不能冒充 TTFT。
- Streaming 不改变 `context_window`、`output_limit`、compaction reserve、HTTP body cap 或计费语义。

## 9. Session、错误与可观察事实

Session 继续遵循：

- `/model` 只在相同 protocol/endpoint 且 active path 无 opaque Provider state 时切换；
- 新 model 不查 catalog；切换前重新构造 client 和 RequestBudget，CAS 失败零写；
- 跨 protocol/endpoint、opaque state 或 concurrent leaf change 返回 `model_session_mismatch`；
- Canonical Messages 是唯一 transcript，不做跨协议 state 转换。

至少区分以下稳定失败：

- Target：`authentication_failed`、`forbidden`、`model_not_found`、`protocol_resolution_failed`；
- Wire：`request_contract_invalid`、`response_contract_invalid`、`tool_call_invalid`、
  `tool_continuation_failed`；
- Budget：`context_length_exceeded`、`output_limit_rejected`、`system_context_too_large`；
- Delivery：`tool_result_page_invalid`、`tool_result_unavailable`、`tool_result_expired`、
  `tool_result_retention_failed`、`tool_result_budget_too_small`、`tool_result_source_changed`；
- Runtime：timeout、rate limit、Provider unavailable、tool partial success/interrupted。

远端错误正文和工具正文都是不可信数据。只使用结构化字段、固定 marker 和安全投影；不据此执行参数、修改配置、切换
Target 或扩大权限。

兼容性按事实报告，不使用单一 `supported`：

| 事实 | 已证明 | 未证明 |
| --- | --- | --- |
| `routeable` | 本地 Target 与 protocol 可装配 | endpoint 可访问 |
| `tool_loop_checked` | exact Target 当次完成最小 tool call + continuation | 最大窗口、质量、长期稳定 |
| `request_succeeded` | 一次真实 Agent request 成功 | 其他任务或 endpoint |
| `coding_evaluated` | 明确授权的代表性收费任务通过 | 普遍支持 |

## 10. 分阶段实现与 worktree

在已确认的干净集成 worktree 上冻结本设计、`result_view` key/type、Streaming callback 合同和测试基线，再从其 exact HEAD 创建四个独立
worktree。基线必须显式记录并可复现，不要求先 fetch 或切换到 `origin/main`；当前 dirty/detached 设计 worktree 不用于生产合并。

### Worktree A：Model Target 与预算

- 删除型号 tuple lookup 和 unknown warning，默认保持 `128000/16384`；
- 保留现有 CLI/`pony.toml` 覆盖，验证 `256000/16384`；
- 统一 CLI/`pony.toml` 的 output ceiling 和交叉校验，删除 CLI 独有 32768 上限；
- future model id 在四种显式 protocol 中不查 catalog、不额外 probe；
- 修订 ADR-0044/0047 的实现与测试表述；
- 不在同批重写所有 adapter optional fields。

### Worktree B：工具结果页面与恢复

- 实现共享页面函数和内部 `ToolOutput/result_view`；
- `read_file`、`memory_read` 使用 2000 行 / 50 KiB / inline token 三重边界；
- 超长单行使用 `start_byte` continuation，验证 UTF-8 boundary；
- pageable 结果绕过 digest；
- RunStore 使用完整 SHA-256 增加 bounded raw-result read，注册仅限当前 running Run 的 `read_tool_result`；
- 不可重放大结果原子 spill 后才暴露可恢复 id；
- 默认 inline budget 改为 16384，并将 inline/digest 最小值迁移为 256/128；
- continuation 使用 expected source hash，raw storage 执行 4 MiB/item、8 MiB/Run、100 files 上限。
- `ResultPagePolicy` 在 top-level turn 的 `begin_permission_turn()` 与 permission/tool schema 一起冻结，由 Executor/唯一 shaping owner
  按本 turn 使用；runner 不捕获长期 TokenAccounting。`/model` 成功只影响下一 turn，避免工具 registry 持有旧 counter。

### Worktree C：TUI 状态机

- C1 可与 A/B 并行：增加非空 prompt、用户侧轨、Assistant 标识、复用现有 working 字段的唯一 `Working…` 活动态、工具动作映射、
  queue editor header 和普通成功/失败单条回执；
- C1 同时收口 approval 的 pending-tool 恢复、Final/error/close 的幂等清理和运行中窄屏有界渲染，不实现 spinner thread、
  fullscreen、主题、事件缓冲或 tool card；
- 只针对已冻结的 `result_view` key/type 编写 renderer fixture，不自行实现或猜测 B 的 metadata producer；
- C2 在 B 合入集成分支后完成 listener/result-view 集成、分页/截断回执和 Fake Provider E2E；
- 不修改欢迎资产、不引入全屏 transcript、主题或依赖。

### Worktree D：安全 Streaming

- 增加 transient `RuntimeOptions.stream` 和交互 CLI 边界，不修改 `.env`、`pony.toml` 或 Session format；
- 在标准库 transport 中实现有界 SSE/NDJSON reader，四个 adapter 各自实现 `complete_stream()`、committed/text callback、
  事件校验和终态组装；
- Agent 增加当前 request 私有的 `SafeTextPreview`，TUI 只更新既有动态 activity 区；
- D1 可与 A/B/C1 并行完成 framing、四协议 fixture、终态 Response 等价和 preview callback 安全测试；
- D2 在 C 合入后完成 TUI lifecycle、resize/NO_COLOR、异常清理和 Fake Provider E2E；
- 不实现 stream cancel、partial transcript resume、reasoning/tool-argument preview、非 TTY streaming 或通用 event bus。

A/B、C1 和 D1 可以并行；C2 明确依赖 B，D2 明确依赖 C 的单一 activity lifecycle。合并顺序固定为：A review/merge；B rebase
到集成分支后 review/merge；C rebase 到 A+B 后完成 C2、review/merge；D1 只包含 transport/adapter，随后 D rebase 到 A+B+C 后
完成 Runtime/CLI/Agent/TUI 的 D2、review/merge。合并后进行一次完整 finding-first review、全量离线门禁、伪终端 E2E 和用户
明确授权的收费 live API。任何 worktree 不自动 merge；发生契约冲突先修来源分支。

## 11. 验收矩阵

### 11.1 Model 与预算

- 任意 future model id 在四种显式 protocol 中零 catalog、零 unknown warning、零额外 probe；
- 默认始终是 `128000/16384`，没有 `32K/8K` 残留；
- CLI 和 `[model]` 的 `256000/16384` assembly、resume、`/model` 和 delegate 一致；
- CLI/`pony.toml` 对相同 context/output 值产生相同结果，显式 output 不被型号表或入口专属上限静默 clamp；
- `auto/openai` probe 只选择 protocol，真实任务失败后无 fallback；
- Responses 与 Chat 分别验证 request/tool continuation，Session protocol 不合并；
- `doctor --check-api` 只报告 `tool_loop_checked`，不持久化支持状态。

### 11.2 工具结果

- 短文件、3000 个短行、宽 ASCII、密集 CJK、60 KiB 单行和 EOF 边界均有直接测试；
- 任一页面不超过 2000 行、50 KiB 和配置 token，单行 fragment 仍能前进；
- 无法容纳最小 envelope 的 token budget 稳定失败，不产生空页或同位置 continuation；
- 默认首段、显式大范围、后续 continuation 拼接后等于脱敏原文，不重复、不漏行/字符；
- LF、CRLF、无末尾换行、超长单行和分页间 source mutation 均有 fixture；body 拼接保留规范化前的原始换行，source
  变化稳定失败；
- page 结果从 Agent 到 Provider 保持原文，不出现 `[digest]`；
- shell 大输出使用 head-tail preview，只有 durable private write 成功才显示 recoverable/id；
- raw retention 对 4 MiB item、8 MiB Run total、100 distinct files 的边界前后各有测试，超限不重跑/不淘汰；
- `read_tool_result` 拒绝跨 Run、terminal Run、伪造/16 位 id、path、hardlink、symlink、special file、identity drift、
  hash mismatch 和超限；
- redaction 在 page/token/hash/persistence 之前生效，secret 不进入 Session、Run 或 UI。

### 11.3 TUI

- 彩色与 `NO_COLOR` 下，`›` 输入、`│` 用户块、`Pony` Answer、活动行和工具回执均可结构区分且不越界；
- `model_requested -> tool_started -> tool_executed -> model_requested -> Answer` 精确投影为
  `Working -> Reading/Running -> Result -> Working -> Answer`，每个工具只有一条永久回执；
- Final、error、interrupt、approval、close 和重复 clear 都不残留活动文字，不把 `Working` 描述成 Provider reasoning；
- 完整 read 显示真实范围；分页 read 显示 `more from`；截断 shell 显示真实 recoverable 状态；
- success/page/truncated/failure 在 `NO_COLOR` 下仍由前缀和文字区分；所有失败/中断/partial success 可见，stable code 不被裁掉；
- inline/page/preview 合法组合分别正确渲染；preview 的 recoverable true/false 和畸形 metadata 都有安全回执；
- busy 时 activity 与 `Queued N/5` 可同时显示，出队时才出现用户块；approval 始终前台，批准后恢复 pending Tool running，
  拒绝后显示 rejected，approval 回答不进入 queue/Canonical Messages；
- 80/111 列启动拒绝，112/120 完整大版；运行中缩到 80 列时到达的 Tool result、Final、error、approval 仍有界可见且不丢
  输入，扩宽后不重复 Logo；
- listener 数据在 durable append 后提供且已脱敏，trace 不扩大低敏字段。

### 11.4 Streaming

- 无 `--stream` 的所有入口 request shape 和输出保持不变；`pony --stream` 与 `pony repl --stream` 只在完整交互 TUI 启用，
  其他入口稳定拒绝；
- custom client 缺 `complete_stream` 时在 Pony 初始化前拒绝，并断言 Session 目录不存在或已有 revision 不变、compaction/network
  零调用；delegate/worktree child 不继承 stream；
- Anthropic SSE、Responses SSE、Chat SSE、Ollama NDJSON 各覆盖文本、tool call、usage、stop、错误帧、非法 UTF-8、超长行、
  缺失终止、16 MiB/100000 event 边界和终态 `Response` 等价；
- tool arguments、reasoning、opaque state、refusal/error detail 和 request id 不触发 preview；完整流解码前工具零执行；
- secret 跨 2/3 个 chunk、跨 UTF-8 byte chunk、redaction snapshot 含多行或短 secret、跨 chunk/跨行 PEM private key、64 KiB
  无换行文本和 callback 抛错均不泄露、不写 durable state、不改变 Final；
- 首事件后断连/timeout/畸形终态均不可 retry，且不切换 final-only/protocol；首事件前失败只服从现有一次 Model Attempt 策略；
- preview 仅更新有界动态区，Final 只渲染一次；success/error/interrupt/close、运行中 resize 和 `NO_COLOR` 均清理且不残留；
- preview 与 `Queued N/5` 可同时可见；进入 Tool running/approval 前先清 preview；preview 后 Provider error 只显示一次 stable
  error；callback 异常仍只显示一次 Final；120→80→120 不留残影，扩宽后能从保留的同一安全 snapshot 重投影，无需等待新 delta；
- 首 preview 同步 acknowledgment 后，后续 burst 由单槽 latest snapshot 以不高于 10 Hz 合并；Provider reader 不等待 UI，
  Final/error/close 使迟到刷新 no-op；
- busy Ctrl+C 只清 pending queue 且当前流继续，不能被实现或显示为 Streaming cancel；只有真实 Provider interruption、terminal
  turn failure、error 或 close 才永久清理 preview；
- durable trace/artifact 不含文本，只允许布尔状态与可空 `first_preview_ms`；TTFT 只在首个实际可见安全文本出现时成立。

### 11.5 集成与真实 API

- Fake Provider 完成 `page read -> continuation -> final`，四个 adapter 至少各有离线 tool-loop fixture；
- exact HEAD 运行 `scripts/check.sh`，不能用局部测试替代失败的完整门禁；
- live 前先使用共享 config/resolver 执行 `doctor --check-api`，再运行一个会触发工具 continuation 的真实请求；
- live 使用用户明确指定的主工作区 `.env`，只读取、不复制、不打印 Key 或完整 response；
- live 只证明该 exact Target 当次结果，不声称验证 256K 物理窗口；未发送接近窗口上限的请求就必须写“256K live 未验证”。
- 至少对 canonical `.env` 的 exact Target 分别运行 final-only 与 `--stream` 只读 turn；streaming report 必须证明单次 HTTP
  attempt、非空 Final、零重复工具、preview 无持久化和 TTFT 状态，不能把单一 target 外推到其他三种协议。

## 12. Review 结论

本方案通过设计 review 的条件是：

1. 不把 `128K/16K` 静默降为 `32K/8K`；
2. 不以成功 check 为由写型号表或永久支持状态；
3. 不合并 Responses/Chat wire adapter；
4. 不让 pageable 正文经过不可恢复 digest；
5. `raw_result_id` 与真实恢复能力同批交付；
6. 截断、运行中和失败在 TUI 中始终可见；
7. 不新增第二配置面、动态 registry、新依赖或跨协议 fallback。
8. Streaming 只显式启用，普通文本 preview 不能泄露 reasoning、tool args、secret 或形成第二 transcript；
9. 任一流事件后失败不重放，完整终态解码前绝不执行工具；
10. Safe preview 对多行、短已知 secret 和跨行 PEM fail closed，不能依赖逐行 redactor 作出错误安全承诺。

按这些条件，本方案通过设计 review，推荐进入实现。核心路径复用现有预算入口、RunStore、ToolExecutionResult metadata、
listener 和行内 renderer，删除无价值的型号分类与 warning；非流式数据路径只新增一个共享页面值对象和一个严格受限的恢复
工具。Streaming 作为独立 opt-in 切片，只增加有界 framing、四个 adapter 私有组装器和 request-scoped 安全 preview，不引入
通用 Provider event model、第二 transcript、动态 registry 或新依赖。

## 13. 参考资料

- OpenAI Codex：[Configuration reference](https://developers.openai.com/codex/config-reference)、
  [Hooks](https://developers.openai.com/codex/hooks)
- Pi：[Using Pi](https://pi.dev/docs/latest/usage)、[TUI Components](https://pi.dev/docs/latest/tui)、
  [Keybindings](https://pi.dev/docs/latest/keybindings)、[News](https://pi.dev/news)、
  固定源码 [user message](https://github.com/earendil-works/pi/blob/9d2ec7ffabe927bfad2214c1cee25b6632a78dcf/packages/coding-agent/src/modes/interactive/components/user-message.ts)、
  [assistant message](https://github.com/earendil-works/pi/blob/9d2ec7ffabe927bfad2214c1cee25b6632a78dcf/packages/coding-agent/src/modes/interactive/components/assistant-message.ts)、
  [tool execution](https://github.com/earendil-works/pi/blob/9d2ec7ffabe927bfad2214c1cee25b6632a78dcf/packages/coding-agent/src/modes/interactive/components/tool-execution.ts)、
  [status indicator](https://github.com/earendil-works/pi/blob/9d2ec7ffabe927bfad2214c1cee25b6632a78dcf/packages/coding-agent/src/modes/interactive/components/status-indicator.ts)、
  [interactive mode](https://github.com/earendil-works/pi/blob/9d2ec7ffabe927bfad2214c1cee25b6632a78dcf/packages/coding-agent/src/modes/interactive/interactive-mode.ts)
- OpenAI API：[Responses](https://developers.openai.com/api/reference/resources/responses/methods/create)、
  [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- TRAE：[内置模型与自定义模型](https://docs.trae.ai/ide/models)
- nanobot：[configuration](https://github.com/HKUDS/nanobot/blob/main/docs/configuration.md)、
  [OpenAI compatibility](https://github.com/HKUDS/nanobot/blob/main/nanobot/providers/openai_compat_provider.py)
- WorkBuddy：[模型配置](https://www.workbuddy.ai/docs/zh/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Model)
- Cursor：[模型与价格](https://cursor.com/docs/models-and-pricing)
