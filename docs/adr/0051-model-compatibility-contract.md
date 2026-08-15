# ADR-0051: Model id 走协议基线，预算和工具结果不得静默降级

## Status

Accepted and implemented for the unreleased Pony 1.0 line. 完整方案见
[Model Target、预算、工具结果与 TUI 设计](../model-target-and-budget-design.md)。

本 ADR 修订：

- [ADR-0044](0044-provider-auto-resolution.md)：强制 synthetic probe 只属于 `auto/openai` 的 protocol
  resolution；显式 protocol 不因 model id 陌生而 probe。
- [ADR-0047](0047-session-scoped-model-switching.md)：模型切换重新装配统一 RequestBudget，不再查询 model
  capabilities catalog。

## Context

Pony 当前允许任意合法 model 字符串进入 Transport，却又用 model-name tuple 区分“内置”和“未知”型号。该表只有一个
与 fallback 数值相同的记录，但所有其他型号都会收到 warning。把检查成功的型号继续写入表，仍然无法证明其他 endpoint、
最大窗口、长期稳定或 coding quality，只会制造更新、过期和误命中的故障面。

另一条路径上，Pony 声明默认 `128K context / 16K output`，但 `read_file` 单次只允许 200 行，超过 4096 token 的结果又会
被替换成最多 512 token digest。完整结果产生了 `raw_result_id`，模型却没有恢复工具。扩大模型窗口不能弥补工具层先丢正文。

TUI 又把 `tool_started` 显示为永久工具行、成功完成保持静默，并在工具运行时清除 `Working…`。用户无法区分正在聊天、
工具仍在执行、读取只完成一页，还是程序已经卡住。

## Decision

### Model 与 Protocol

- exact Target identity 保持 `protocol_family + model + endpoint_hash`。model id 是不透明路由值，只做结构、安全和 secret
  校验；runtime 不区分 known/unknown，不查型号白名单，不发 unknown warning。
- Pony 1.0 只支持四个固定 wire protocol：`anthropic_messages`、`openai_responses`、
  `openai_chat_completions`、`ollama_chat`。
- Responses 与 Chat Completions 保持两个 adapter 和两个 Session protocol。它们只共享现有 transport、JSON、redaction
  和 error helper，不合并 wire envelope 或真实任务 fallback。
- 显式 protocol 直接装配 adapter。`auto/openai` 仅为选择 protocol 执行 bounded synthetic resolution；真实用户任务失败后
  不切换 protocol、endpoint 或 model 重放。
- `doctor --check-api` 只报告 exact Target 当次 `tool_loop_checked` 事实，不写 catalog、cache、support flag 或窗口认证。
- 不预建 `TargetOptions`/model-family registry。真实 wire incompatibility 必须在所属 adapter 内最窄修复并有一手证据和
  contract test；不得影响预算、准入、UI 或 Session 切换。
- 认证继续由四个强制 Provider 静态决定，不进入 endpoint/model option。

### 请求预算

- 所有未显式配置的 Target 统一使用 Pony 当前产品默认：`context_window=128000`、`output_limit=16384`。
- 不采用 `32K/8K portable_default`，不因 model id 陌生而静默降档，也不自动探测或改写最大窗口。
- `256000` 或其他窗口通过现有 CLI/`pony.toml [model]` 显式配置；未显式修改 output 时仍保持 `16384`。
- CLI 与 `[model]` 复用同一个 Pony output safety ceiling 和 `W/O/R/I` 交叉校验；删除 CLI 独有的 32768 cap，
  有效 output 不再与 model-name max 静默取 `min()`。
- 请求预算是 Pony 本地策略，不是 Provider 物理能力。Provider limit rejection 返回 exact Target、stage 和稳定错误，不降档重试。
- output limit 始终用于本地 context planning/reserve；adapter 另投影 `enforced|local_only`，不得把本地 response bound 或
  timeout 描述为远端 generation/计费上限。
- 删除 `BUILTIN_MODEL_CAPABILITIES` 与 `fallback` warning 语义，将现有计算收敛为 Provider-neutral
  `RequestPolicy`/`RequestBudget`；不增加命名 profile 或逐字段 provenance 配置面。

### 工具结果

- 默认 `context.tool_results.inline_tokens` 从 4096 提高到 16384。
- `read_file`、`memory_read` 和 raw-result read 共用一个页面合同：最多 2000 行、50 KiB UTF-8、16384 token 或显式
  inline token budget，任一先到即停止。
- `start` 默认 1，`end` 省略表示 EOF，显式 `end` 才限制请求范围；范围再大也只返回一个安全页。
- 超长单行使用脱敏后当前 start 行内、UTF-8 boundary 上的 `start_byte` continuation；普通页面保留原始换行。
- continuation 携带 `expected_sha256`，分页间 source 变化稳定失败；完成事实区分 `range_complete` 和
  `file_complete`。
- 页面在脱敏后按最终模型可见 envelope 计算 bytes/token，正文不加逐行行号，返回准确范围和下一次工具参数。
- `inline_tokens` 最小值为 256，`digest_tokens` 最小值为 128；更小旧值稳定拒绝。最小 envelope 仍无法容纳时不返回
  空页或同位置 continuation。
- pageable 结果不得进入语义 digest。不可重放大结果先私有保存完整脱敏文本，再返回 head-tail preview。
- Provider-visible `raw_result_id` 必须使用完整 SHA-256，且只在 durable write 成功后出现。
- 同一交付切片提供模型可见只读 `read_tool_result`：只读取当前 running top-level Run，参数不接受 host path/run id，
  使用同一页面合同，并对 id、private-file identity、hardlink/symlink/special file、size 和完整 hash fail closed。
- 单个 raw result 最多 4 MiB；每个 Run 的 tool-result subtree 最多 8 MiB、100 个 distinct full-hash 文件。读写都检查
  实际大小和 subtree 总量，不淘汰旧结果。
- raw write 失败不得重跑原工具；preview 必须投影 `recoverable=false`。历史/terminal Run 的 id 稳定返回
  `tool_result_expired`。preview 显式标记 `scope=current_run expires=end_of_turn`，Session 历史中的 id 不承诺跨 turn 恢复。
- `result_view` 合法组合固定为：inline 不截断且无 continuation；page 无损、不标 truncated，未完成只用 `next_*`；preview
  必须 truncated 且以 required bool 表示是否可恢复到本 turn 结束。畸形 metadata 只产生 generic UI receipt，不影响 Agent。

### TUI

- 保留现有行内 TUI、完整欢迎资产、toolbar、approval、queue 和 Markdown renderer，不引入全屏 transcript、主题或新依赖。
- 借鉴 Pi 稳定的 transcript/status/editor/footer 排列、单一 working indicator 生命周期和 tool pending/success/error 语义，
  但不复制 Pi 的品牌、Extension UI、reasoning block、两类 queue delivery、tool card 或 fullscreen 架构。
- 交互必须区分 Waiting input、Model working、Tool running、Tool result 和 Assistant answer 五态。
- 用户块使用无角色标签的低对比背景和无色侧轨；Assistant answer 只在块首显示一次低对比 `Pony` 标识，input 使用非空
  `› ` prompt。颜色只能增强，不能承担唯一状态语义。
- presentation 只维护一个非持久化活动行；turn worker 在本地 context/RepoMap 准备前立即显示 `Working…`，durable
  `model_requested` 只确认同一状态，`tool_started` 等后续事件继续由 listener 驱动。ADR-0052 定义的 current-request、
  无 durable writer 的 Streaming preview callback 只能把同一行更新为 `Receiving...`/安全文本。
  Final/error/interrupt/approval/close 必须幂等清除；`Working` 不包含 Provider reasoning。
- approval 隐藏 activity 但保留 pending tool summary；批准后由 approval callback 恢复 Tool running，拒绝后等待 rejected 回执，
  不增加第二个 `tool_started` 或新的 runtime event。
- listener/resize 次生渲染错误不得覆盖 primary result；approval UI 任一步异常必须按拒绝/中断 fail closed，工具不得执行，
  并恢复 hook 与终端输入所有权。
- `tool_started` 只显示瞬态活动；`tool_executed` 写一条永久结果回执，完整/分页/截断/失败和恢复状态不得静默。
- success/page/truncated/failure 使用固定前缀和文字，在 `NO_COLOR` 下仍可区分；失败的 stable code 不得被宽度裁掉。
- 忙碌输入只进入现有单一五条 FIFO；prompt 动态区域的 editor header 显示瞬态 `Queued N/5`，可与 activity 同时存在，真正
  出队时才渲染用户块；approval 始终前台。
- 运行中低于 112 列的扩宽提示与 queue 共用单行 editor header；组合态固定为 `Widen terminal · Queued N/5`，安全提示优先且
  两项都不能丢。
- 分页/截断 UI 只消费 executor metadata 中已脱敏的 `result_view` 白名单。它在 durable trace append 后仅进入内存 listener，
  不把工具正文、host path、raw id 或 secret 写入 trace。
- 112 列只作为启动和完整 Logo 门槛。运行中缩到 112 列以下时保留输入并要求扩宽，但 conversation、activity、approval、
  editor 和 footer 继续按当前宽度有界渲染，永久事件不得暂停或丢失；扩宽后恢复 active UI，不重复 Logo，不缩放或裁切
  冻结的欢迎资产。

### Session 与失败边界

- `/model` 只在相同 protocol/endpoint 且 active path 无 opaque Provider state 时切换；切换前重新装配 client、budget、
  counter 和 delegate factory，CAS 失败零写。
- Canonical Messages 仍是唯一 transcript，不进行跨协议 state 转换。
- persistence、result retention 或 UI 失败不得重放已执行工具；secondary failure 不覆盖 primary tool/runtime failure。
- 不新增动态 Provider registry、在线 model catalog、第二配置面、qualification cache、请求级 router 或新 runtime dependency。

## Consequences

- 新型号只要实现用户所选协议基线，就能零 catalog、零 warning 直接尝试运行；偏离协议时返回准确失败，而不是假装支持。
- 默认体验至少维持现有 `128K/16K`，用户可显式扩大到 `256K`；Pony 不虚构远端物理上限。
- 大窗口获得真实可用的文件正文：常见仓库约 97% 文件单页完成，其余结果有准确 continuation。
- 不可重放大输出只有在真实保存成功时才声称当前 turn 可恢复；历史 preview 明确标注过期，不把它描述成永久能力。
- 用户能持续看见当前 turn、工具运行、分页/截断、失败和下一次输入状态，不再把等待误认为 bug。
- 实现已同步修改 model budget、tool schema/validation/runner、memory read、RunStore、Agent result shaping、TUI listener/render
  及其聚焦测试；完整门禁和收费 live 验证仍是独立验收证据。
