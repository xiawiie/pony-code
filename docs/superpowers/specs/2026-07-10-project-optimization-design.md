# Pico Action Kernel 与 Messages v3 收敛设计

- 日期：2026-07-10
- 状态：已完成交互式设计与源码级全面 review，用户已批准，进入 implementation planning
- 当前分支：`memory`
- 范围：优化顺序中的 C 阶段——运行时内核与消息模型收敛
- 后续：C 阶段验收后，另行设计 A 阶段——安全与可信基线

## 1. 摘要

Pico 当前已经具备完整的本地 coding-agent harness 骨架：CLI、Provider、上下文、工具执行、session、run artifacts、memory 和 recovery 都有可运行实现，完整 pytest 基线为 668 passed、1 skipped。

问题不在于“缺少更多功能”，而在于主运行链仍同时维护两套决策与消息语义：

1. Provider 返回的 `Response` 有时在 `FallbackAdapter` 内被提前解析成伪 native tool call，`AgentLoop` 又对 `Response` 做第二次决策；completion usage 还通过 Provider 对象的可变 side channel 回读。
2. 每次模型尝试先构建并丢弃 legacy string prompt，再构建实际发送的 v2 request；trace 与 checkpoint 可能描述未发送的 prompt。
3. `session["messages"]` 是实际 request 所依据的 canonical transcript，但运行时仍双写 `session["history"]`，报告、评测和部分 CLI 继续读取旧结构。
4. retry feedback 只写入 legacy history，下一次真实 Provider 请求看不到。
5. 动态注入只在当前 user message 位于 messages 尾部时生效；一旦尾部变成 tool_result，下一次模型尝试会丢失该 turn 的注入。
6. 唯一遗留 skip 正好覆盖旧 memory ablation；当前代码复跑后，三个 variant 都得到 `repeated_reads=12`、`memory_hit_rate=0`，历史“60→0 / 100%”证据不能继续沿用。

本设计采用最小纵向收敛方案：

```text
ContextManager.build_v2
  -> Provider.complete_v2
  -> Response
  -> decode_action(response)
  -> ToolAction | FinalAction | RetryAction
  -> AgentLoop applies action
  -> session["messages"] only
```

不重做 Provider 配置，不增加 registry、gateway、并行工具或第三方依赖。

## 2. 已确认的决策

### 2.1 优化顺序

1. 先完成 C：Action Kernel、messages-only 与 legacy 直接移除。
2. C 全部验收后，再进入 A：敏感数据、shell risk、checkpoint/restore 安全等可信基线。

### 2.2 C 阶段范围

包含：

- 建立唯一的 `Response -> Action` 解码边界。
- 让 `AgentLoop` 只消费 Action，不再理解 Provider 内容块细节。
- 让 `session["messages"]` 成为唯一持久化 transcript。
- 删除生产运行时中的 `session["history"]`、legacy prompt build 和 parser 兼容委派。
- 修复 retry feedback、turn injection、tool message pairing、provider 异常收尾和 `/reset` 语义。
- 将报告、评测、CLI session inspection 和 benchmark 迁到 messages。
- 将 session schema 升级为 v3，并支持旧 session 一次性无损迁移。
- 恢复绿色质量门禁与可信 benchmark。
- 完成全部本地验证后，运行一次真实 Anthropic-compatible/DeepSeek E2E。

不包含：

- 删除或重做 `--provider` / `PICO_PROVIDER`。
- 新的 model resolver、Provider registry、gateway 或 capability framework。
- 全 Provider live matrix。
- 并行工具调用。
- streaming 重构。
- 新的测试、类型检查、并发或 benchmark 第三方依赖。
- A 阶段的完整安全与 restore 语义改造。

### 2.3 与现有草案的关系

本设计是 C 阶段的当前权威规范，取代以下未完成草案中与 Action Kernel、legacy 迁移和 model connection 混在一起的范围：

- `docs/superpowers/specs/2026-07-08-pico-action-kernel-provider-parity-design.md`
- `docs/superpowers/plans/2026-07-09-pico-action-kernel-model-connection.md`

旧草案仍可作为调查材料，但不能作为本阶段实施依据。尤其是 model connection、Provider 重命名和删除 provider 轴的任务已经明确排除。

## 3. 当前证据

### 3.1 质量基线

- `uv run pytest -q`：668 passed、1 skipped。
- `./scripts/check.sh`：在 pytest 前被 Ruff 阻断，共 11 个 lint 错误。
- 唯一 skip：`tests/test_metrics.py::test_run_memory_ablation_v2_writes_expected_artifact`。
- 去掉 metrics/evaluator/memory-quality 三类慢 benchmark 测试后：627 passed，约 17 秒。
- 完整 pytest：约 61 秒，其中单个 benchmark report 测试约 22 秒。

### 3.2 行为复现

- `AgentLoop` 每次尝试先调用 legacy `ContextManager.build()`，随后调用真实 `build_v2()`；前者结果被丢弃。
- retry 分支只写 `history`，连续两次 Provider 请求的 messages 完全相同。
- tool_result 成为尾消息后，`build_v2()` 计算了注入文本但没有放入请求。
- `/reset` 只清 history，真实 `messages` 仍保留并继续发送。
- Fallback 文本解析会在普通文本任意位置寻找 `<tool>`，引用示例可能被当作动作。
- Provider 非 `RuntimeError` 异常可能绕过 terminal finalizer，使 TaskState 停在 running。
- tool_use 在工具执行前单独保存；执行中崩溃会留下孤立 tool_use。

### 3.3 Benchmark 真值

当前 HEAD 上以 `repetitions=1` 重跑 memory ablation：

| Variant | repeated_reads | memory_hit_rate | avg_tool_steps | correct_rate |
| --- | ---: | ---: | ---: | ---: |
| memory_on | 12 | 0.0 | 1.0 | 1.0 |
| memory_off | 12 | 0.0 | 1.0 | 1.0 |
| memory_irrelevant | 12 | 0.0 | 1.0 | 1.0 |

这证明了两个同时存在的问题：旧评测 client 不再适配真实 v2 messages；legacy build 删除后，`session.memory.file_summaries` 也没有进入任何 v2 injection。它仍不单独证明 memory 产品能力已经失效。C 阶段必须先恢复 working-summary 的真实 request 路径、再修评测，并根据当前 HEAD 的实际结果更新结论。

### 3.4 源码级 review 结论

本规范在进入 implementation planning 前，按当前源码逐条复核了 Action、request、session、tool/recovery、evaluation 和 live-e2e 链路。没有发现需要推翻总体方向的 P0 问题，但发现以下若不先修订设计就会在实施中造成错误或数据语义不实的 P1 问题：

| 问题 | 当前源码证据 | 本设计的修订 |
| --- | --- | --- |
| 原 Phase 1 会先让 Fallback 返回 raw text，而旧 AgentLoop 会把 leading tool 文本当 final | `pico/providers/fallback_adapter.py` 先解析；`pico/agent_loop.py` 只识别 native content block | Fallback 与 AgentLoop codec 必须在同一个原子切换阶段落地 |
| Action 优先级、multi-text 合并、retry excerpt 和 origin 命名不完整 | `pico/agent_loop.py` 当前自行过滤 blocks 且只取首个 text | 在 §5 明确定义总顺序、字段和 trace 合同 |
| 删除 legacy build 会同时丢掉 prefix refresh、resume refresh、memory guidance 和旧 context benchmark 的触发源 | `Pico._build_prompt_and_metadata()` 承担这些副作用与 metadata | 在 turn preflight 中显式接管；guidance 移入稳定 prefix；benchmark 改测真实 request view |
| 仅重写 memory benchmark client 仍无法产生有效 on/off 差异 | v2 renderer 不读取 `session.memory.file_summaries`，而 canonical 旧 tool_result 会让三个 variant 看到相同事实 | 复用现有 `memory_index` injection 加入 recent working-file summaries，并在 benchmark 中用真实 request-view 压力移除旧 tool turn |
| live 要求 native ToolAction，但 stable prefix 当前强制所有模型输出 XML | `pico/prompt_prefix.py` 同时发送 native schemas 和 “Return exactly one `<tool>`” | stable prefix 改为协议中立；Fallback flatten 时才附加文本协议说明，codec 仍是唯一 decoder |
| v3 迁移若只让 `load()` 调 `save()`，无法保证 read → backup → replace 是同一锁事务 | 当前 `SessionStore.load()` 无锁读取并直接 `write_text()` | 整个迁移持锁，复用一个内部 atomic-write primitive，禁止嵌套锁猜测 |
| “成对保存”不能让外部副作用与 session 落盘成为同一事务 | ToolExecutor 先执行并写 Tool Change，session 随后保存 | save 失败立即以 persistence error 终止，并保留已完成 Tool Change 证据，不再调用 Provider |
| 多个 runner 仍以 `"error: ..."` 或 `"... unavailable"` 返回，ToolExecutor 会标成 `ok` | `pico/memory/tools.py`、protected-note write path | 可预判问题进 validation/rejected；运行期失败抛出并由 ToolExecutor 结构化为 error/partial_success |
| terminal finalizer 自身失败可能遮蔽原 Provider 异常；失败 turn 还可能留下未闭合的 plain user 尾消息 | 当前 `_finish_run()` 串行写多个 artifact，Provider 只捕获 `RuntimeError` | 先在内存终结状态、追加 runtime terminal assistant，再 best-effort 写 artifact；原异常始终为 primary |
| `memory_save` 不是 read-only，但也不是可恢复的 workspace write | 当前 effect table 将其标为 `read_only` | 使用 `memory_write` 审计类；不拍 workspace snapshot、不生成空的可恢复 turn checkpoint |
| live E2E 无法可靠诱发 retry，且当前 trace 没有 Action origin | live 脚本只统计 `model_turn.kind` | retry 稳定性放确定性本地测试；live 断言真实 tool step，并由 `action_decoded` trace 证明 native action |
| request metadata 与完整 session transcript 指标容易混名 | `build_v2.messages_tokens` 只描述裁剪后的 request view | request 继续使用 `messages_*`；report 使用明确的 `session_messages_*` |

这些修订不扩大到 model connection 或 A 阶段安全改造；它们只补齐 C 本身已经承诺的正确性与可验收性。

## 4. 目标架构

### 4.1 组件边界

#### Provider Response

`pico/providers/response.py` 继续定义 Provider-neutral 的 `Response` 与 `StopReason`。

`Response.usage` 是 `complete_v2()` 的唯一 per-call completion telemetry 真源。`AgentLoop` 不再在收到 Response 后回读 Provider 对象上的 `last_completion_metadata`；Fallback 负责把 legacy Provider 的 metadata 填入它返回的 `Response.usage`。每个 `model_turn` 保存该 call 的 usage，AgentLoop 同时累计本 run 的 token/cache 数值 totals；最终 report 不再只代表最后一次 call。`StopReason` 增加明确的 `UNKNOWN`；Provider 为未知 stop reason 做归一化时不得默认为 `END_TURN`。

它只回答：

> Provider 返回了什么？

Provider adapter 不再决定 Pico 下一步做什么。

#### Action Codec

将现有 `pico/model_output_parser.py` 收敛为一个无状态动作解码模块 `pico/action_codec.py`。

该模块同时拥有：

- `ToolAction`
- `FinalAction`
- `RetryAction`
- `Action` union
- 纯函数 `decode_action(response)`

三种 Action 使用简单 dataclass 和一个 union，不建继承层级。不创建只有一个实现的 Codec 类，不建立 factory。

它只回答：

> Pico 应该做什么？

#### Canonical Messages

建立一个小型通用消息模块 `pico/messages.py`，集中已有且至少被三个边界共用的操作：

- 去除 Provider 不应看到的 `_pico_meta`。
- 复制 messages 并替换最近的顶层 user 文本，构造 request view。
- 成对追加 tool_use / tool_result。
- 渲染 transcript 供 Fallback、报告与评测消费。
- 检查 role 序列及 tool_use/tool_result 配对不变式。
- 以纯函数返回追加一条或一对消息后的新 messages list，不就地修改输入。

该模块不负责 Provider HTTP、上下文选择、工具执行或 store I/O。AgentLoop 的单一 session commit helper 负责先保存包含新 messages 的 session 副本，再替换内存 session；message 在进入副本前沿用现有 redaction，磁盘与内存看到同一份已处理数据。

现有 `pico/providers/message_utils.py::strip_pico_meta()` 移入该模块；若迁移后 Provider 子模块不再包含其他职责，则删除该空壳，避免保留两个 message utility 真源。

#### AgentLoop

`AgentLoop` 只负责：

1. 创建 turn 与 task state。
2. 构造 request。
3. 调用 Provider。
4. 调用 `decode_action()`。
5. 按 Action 类型执行分支。
6. 记录 messages、trace、report 和 checkpoint。
7. 通过唯一 terminal finalizer 结束。

它不再：

- 遍历 Provider content blocks 决定 kind。
- 调用 legacy string prompt。
- 双写 history。
- 通过字符串前缀判断工具是否失败。
- 从 Provider 对象的可变属性回读本次 completion usage。

#### ContextManager

`ContextManager.build_v2()` 成为唯一 Provider request builder。

继续保留 `build_v2` 名称，避免在本阶段扩大 Provider API 重命名范围。`ContextManager.build()` 及其 legacy history 压缩辅助函数全部删除。

原来由 `Pico._build_prompt_and_metadata()` 隐式承担的 turn preflight 改为显式步骤，并且每个顶层 turn 只执行一次：

1. `refresh_prefix()`。
2. `evaluate_resume_state()`。
3. 生成 prefix/workspace/resume/redaction 等 turn telemetry。
4. 调用 `render_current_user_message()` 生成 injection snapshot。

`build_v2()` 只接收这份已冻结的 turn snapshot、telemetry 和可选 retry feedback，不重复 refresh、resume evaluation 或 recall。原 legacy build 中仍有价值的 `MEMORY_USAGE_GUIDANCE` / `MEMORY_READING_GUIDANCE` 移入稳定 prefix；不能因为删除 string build 而静默消失。

stable prefix 改为 action-protocol neutral：要求使用 Provider 暴露的 tool interface、不得伪造结果，但不再强制 native Provider 输出 `<tool>/<final>`，也不再嵌入 XML response examples。工具本身的 schema/risk/workflow guidance 继续保留。

#### FallbackAdapter

`FallbackAdapter` 继续承担：

- 将 system、tools、messages 拍平成 prompt string。
- 只在拍平后的 legacy prompt 中附加严格 `<tool>` / `<final>` 文本协议与一个最小 JSON tool example。
- 调用只支持 `complete(prompt, ...)` 的现有 Provider。
- 将原始文本包装成 `Response(content=[text])`。
- 将 legacy Provider 的 completion metadata 写进 `Response.usage`。

它不再：

- 调用 `parse_model_output`。
- 生成 UUID。
- 将文本协议伪装成 native tool_use。

所有文本协议统一在 `decode_action()` 中解释。

这份 fallback-only protocol instruction 是 wire encoding guidance，不是 decoder；它不能调用 parser、判断 kind 或制造 tool id。attribute XML 仍为 codec 接受的兼容输入，但不需要在 prompt 中继续维护第二套复杂示例。

Fallback 对原始文本 Response 使用 `END_TURN` 作为“调用正常返回”的 transport stop reason；codec 的 leading protocol 规则优先于该 stop reason，因此 `<tool>` 仍会变成 ToolAction。Fallback 不伪造自己无法获知的 `MAX_TOKENS` 或 native capability。

### 4.2 依赖方向

```text
providers.response
        |
        v
   action_codec       messages
        |                |
        +-------+--------+
                v
            AgentLoop
          /     |      \
 ContextManager |   ToolExecutor
                |
          Session / Run / Recovery stores
```

`action_codec` 不依赖 runtime、session、tools 或 stores。`messages` 不依赖 Provider 实现。

## 5. Action 合同

### 5.1 ToolAction

字段：

- `name: str`
- `arguments: dict`
- `tool_use_id: str | None`
- `origin: Literal["native_tool_use", "text_protocol"]`
- `ignored_tool_count: int = 0`

语义：

- native `tool_use` 优先。
- 多个 native tool call 暂时只执行首个，并将其余数量写入 trace。
- 本阶段不实现并行或批量工具执行。
- “首个”按 Provider content 顺序定义；首个 native tool 非法时返回 `RetryAction`，不能跳过它去执行后面的 call。
- 空 name、非 dict input 或缺失必要 tool block 字段不抛出到 AgentLoop，而是产生 `RetryAction`。
- text protocol 的 `tool_use_id` 为 `None`，只在构造待保存的 message pair 时生成；native id 原样保留。

### 5.2 FinalAction

字段：

- `text: str`
- `origin: Literal["provider_text", "text_protocol"]`
- `truncated: bool = False`

语义：

- 没有 native tool 时，按 content 顺序以换行合并所有非空 text block；不能只取第一个 block。
- `END_TURN` 且有普通文本时结束，origin 为 `provider_text`。该命名同时适用于 native 与 Fallback Provider，不冒充 capability。
- 文本协议 `<final>...</final>` 去掉 wrapper 后结束。
- `MAX_TOKENS` 且有非 tool 文本时保留当前兼容行为，返回 FinalAction，同时在 Action 与 trace 标记 `truncated=true`。

### 5.3 RetryAction

字段：

- `reason_code: str`
- `notice: str`
- `origin: Literal["response", "text_protocol"]`
- `excerpt: str = ""`

稳定 reason code：

- `empty_response`
- `malformed_tool_protocol`
- `empty_final_protocol`
- `invalid_native_tool`
- `stop_sequence`
- `unsupported_response_shape`

语义：

- 不修改 canonical messages。
- trace 记录 reason code 与脱敏后的有限 excerpt。
- notice 只注入下一次 request，消费后清除。
- 不消耗 tool step，但受现有 attempt 上限约束。
- excerpt 在 codec 内限长，进入 trace 前仍走统一 redaction；notice 不回显原始模型输出。

### 5.4 严格文本协议

允许执行：

- 去掉前导空白后，第一个有效 token 是 `<tool>` JSON 形式。
- 去掉前导空白后，第一个有效 token 是 `<tool name="...">` attribute 形式。

opening token 必须是精确的 `<tool>`，或 `<tool` 后紧跟空白、`>`、`/>`；`<toolbox>` 等相似标签不是协议。

不允许执行：

- 普通回答中间出现的 `<tool>`。
- Markdown code fence 或引用示例中的 tool 标签。
- 前面已有自然语言、后面才出现的 tool 标签。

首位 tool 协议存在但畸形时返回 `RetryAction`；非首位标签作为普通最终文本处理。

leading `<final>` 只有在正文非空时才产生 FinalAction；空正文产生 `empty_final_protocol`。`MAX_TOKENS` 下若 `<final>` closing tag 因截断缺失但已有正文，保留正文并标记 `truncated=true`。普通文本中间出现的 `<final>` 与非首位 `<tool>` 一样，不被当作控制协议。

### 5.5 决策优先级与 trace

`decode_action()` 的总顺序固定为：

1. 若存在任何 native `tool_use` block，检查并处理第一个；它优先于 text block 与 stop reason。
2. 否则检查合并文本是否以严格 `<tool>` 协议开头；合法则 ToolAction，畸形则 RetryAction。
3. 否则检查是否以 `<final>` 协议开头。
4. 否则 `END_TURN + 非空文本` 为 FinalAction。
5. 否则 `MAX_TOKENS + 非空文本` 为 truncated FinalAction。
6. 否则 `STOP_SEQUENCE`、空响应、未知 stop reason 或不支持的 content shape 为 RetryAction。

每次 decode 后写一个 `action_decoded` trace event，只记录：

- `action_type`
- `origin`
- `reason_code`（仅 retry）
- `truncated`（仅 final）
- `ignored_tool_count`（仅 tool）
- 已脱敏限长 excerpt（仅 retry）

不把完整 Provider raw response 复制进 trace。该事件也是 live E2E 判断 native Action 是否真实发生的证据。

## 6. Canonical Messages 与 Request View

### 6.1 持久化真源

session v3 只用 `messages` 表示对话 transcript；working memory、checkpoint pointer、runtime identity 等现有非 transcript session 状态继续保留。这里的 canonical 含义是“Pico 后续请求所依据的规范化运行时 transcript”，不是 Provider 原始响应的逐字归档：被忽略的并行 tool call、thinking block 和 native tool 前置寒暄不会伪装成已完整回放。

每条 canonical message 保存：

- plain user 请求。
- assistant tool_use content block。
- user tool_result content block。
- assistant final text。
- Pico 内部使用的顶层 `_pico_meta`。

v3 不变式保持最小且明确：role 只能是 `user|assistant`；plain content 必须是 string；tool_use 只能位于 assistant block，tool_result 只能位于 user block；每个 canonical tool_use 后面紧跟且只跟一个相同 id 的 tool_result；tool id 非空且在 session 内唯一；顶层 `_pico_meta` 至少是 dict。v2 迁移可为缺失的 `_pico_meta` 补空 dict/可得的 created_at，但 v3 runtime 新写消息必须包含 created_at。

不保存：

- 注入后的 user 文本副本。
- 一次性 retry feedback。
- Provider wire metadata。
- history 镜像。

### 6.2 Turn injection snapshot

在一个顶层 user turn 开始时：

1. 将 plain user message 追加到 canonical messages。
2. 完成 §4.1 的 turn preflight 后，`AgentLoop` 调用现有 `render_current_user_message()` 一次，完成 intent、durable memory index + recent working-file summaries、memory recall、workspace、project structure 与 resume checkpoint 渲染。
3. 得到该 turn 的 injection snapshot 与 telemetry。

同一 turn 内的所有模型尝试复用这份 snapshot。

原因：

- 防止同一 turn 第二次 recall 被 `recently_recalled` 去重后消失。
- 保证多次模型尝试看到同一决策上下文。
- 工具产生的新事实已经通过 tool_result 进入 transcript，无需重复扫描并重写旧注入。
- system cache key 不受 retry 或工具步骤影响。

resume checkpoint 必须获得非零来源预算；否则 renderer 中存在该 source 但实际永远不会注入。

为保留 legacy history compression 曾提供的 working-memory 能力，现有 `memory_index` source 扩展为同一 block 内的两部分：

- durable memory files index（现有行为）。
- `agent.memory.recent_files` 对应的 `session.memory.file_summaries`，仅在现有 `memory` feature 开启时按 recent_files 顺序、在现有 memory_index budget 内裁剪。

working 部分使用稳定、可测试的显示形状：`Recent working file summaries:`，随后每行 `<canonical_path> -> <summary>`。preflight 的 stale invalidation 先于渲染，已失效 summary 不注入。

没有 durable entries 但存在 working summaries 时仍必须渲染该 block。不开新 source、不加新配置；`memory_off` 不生成/保留这些 summaries，`memory_irrelevant` 将一个不相关 summary 放入 recent_files，使噪声真实进入 request，而不是退化成另一个 off。它是 turn 开始时的 snapshot，同一 turn 工具新产生的 summary 不重渲染，当前工具事实仍只通过 tool_result 进入下一次 attempt。

### 6.3 Request view

每次 Provider 请求：

1. 浅拷贝 canonical messages。
2. 从后向前找到最近的顶层 plain user message，忽略 role=user 的 tool_result carrier。
3. 将该条内容替换为 turn injection snapshot + plain user request。
4. 若存在 pending retry feedback，在同一 request view 中追加 `<pico:runtime_feedback>` block。
5. 去掉 `_pico_meta` 后发送。

不修改 session 中保存的原始 messages。

pending feedback 在成功构造出下一份 request view 后立即消费；同一 notice 不进入再下一次 request。request metadata 显式记录 `runtime_feedback_present: bool`，以便 trace/test 验证，而不是保存 notice 正文。

`ContextManager.build_v2()` 接收已经生成的 snapshot、telemetry 和可选 runtime feedback，只负责组装 request 与最终 metadata；它不在 attempt loop 中重复触发 recall。这样 injection 生命周期由 `AgentLoop` 管理，请求形状仍只有 `build_v2()` 一个真源。

request view 的 `messages_count` / `messages_chars` / `messages_tokens` / `dropped_messages` 只描述本次实际发送的裁剪后消息视图。若 `dropped_messages > 0`，沿用现有语义创建 `context_reduction` resume checkpoint；不再依赖 legacy `budget_reductions`。`context_reduction` feature flag 随 legacy build 一并删除，benchmark 通过“有限 history soft cap vs 足够大的 soft cap”比较真实 request view，不保留一个生产运行时不再读取的假开关。

### 6.4 Request metadata 合同

build_v2 每次 attempt 至少输出以下真实 request metadata：

- system/tools：`system_cache_key`、`system_tokens`、`tools_tokens`、`prompt_cache_supported`。
- messages：`messages_count`、`messages_chars`、`messages_tokens`、`dropped_messages`、`cache_control_breakpoints`、`runtime_feedback_present`。
- injection：`intent`、`injection_tokens`、`injection_truncated`、`injection_dropped`、`injection_budget`、recall error counters。
- turn preflight：`prefix_chars`、`workspace_changed`、`prefix_changed`、`workspace_fingerprint`、`tool_signature`、`resume_status`、stale/mismatch fields、request/tool/workspace counts 与现有 secret summary。

删除 legacy string prompt 专属的 `prompt_chars`、`sections`、`section_order`、`section_budgets`、`budget_reductions`、`history_chars`。v2 metadata 只保留 `system_cache_key`，删除 `prompt_cache_key` alias；legacy Provider `complete(prompt, prompt_cache_key=...)` 的签名属于本阶段明确排除的 model connection 范围，不因此改动。

`Response.usage` 作为 completion metadata 单独进入 `model_turn` 和最终 report，不反向伪装成 request-build 结果。

### 6.5 Tool message pairing

当前实现先单独保存 assistant tool_use，再执行工具，再保存 user tool_result。进程在工具执行中断时会留下孤立 tool_use。

目标流程：

1. Action 留在内存。
2. 写 `tool_started` trace。
3. ToolExecutor 创建 pending Tool Change 并执行工具。
4. 得到 `ToolExecutionResult`。
5. 在内存中构造 tool_use 与 tool_result。
6. 先构造包含该 pair 的新 session 副本并一次 `SessionStore.save()`。
7. save 成功后才替换 AgentLoop 的内存 session；失败则保留旧 transcript。

如果进程在工具执行中崩溃，session 不留下 Provider 无法接受的孤立 tool_use；pending Tool Change 保留恢复证据。如果工具副作用与 Tool Change 已完成、但 message pair save 失败，AgentLoop 将该 tool_change_id 加入本 run 的恢复证据，以 `persistence_error` 终止且不再调用 Provider。C 不声称外部副作用与 JSON session 可以跨资源原子提交。

tool_result block 对 `rejected`、`error`、`partial_success` 设置 `is_error=true`，并在顶层 `_pico_meta` 记录 `tool_status`、`effect_class` 与可选 `tool_change_id`；`ok` 不设置或设置为 false。这样 Provider 能看到失败语义，内部审计也不再依赖解析 `content` 的字符串前缀。

## 7. Session v3 迁移

### 7.1 Schema

将 session schema 升级到 v3：

- v1：history-only。
- v2：messages + transitional history。
- v3：messages-only。

### 7.2 加载规则

#### v3

校验必要字段、schema version、message/content block 形状和 tool id pairing 后原样返回，不做写入。`messages-only` 只表示没有 history 镜像，不表示删除 `working_memory`、`memory`、`recently_recalled`、`checkpoints`、`runtime_identity`、`resume_state` 或 `recovery`。

#### v2

- 任何写回前先备份原文件。
- 若 `messages` 通过与 v3 相同的结构和 pairing 校验且非空，以 messages 为 transcript 真源，不尝试与 history 合并。
- 若 `messages` 为空但合法 history 非空，或 messages 损坏但 history 合法，从 history 重建；不能把“空 list 合法”误当成可丢弃唯一 transcript 的依据。
- 删除 transitional history。
- 设置 `schema_version=3`。
- 原子写回。

#### v1

- 任何写回前先保留原始备份。
- user/assistant 转为普通 messages。
- 每个 tool entry 转为配对的 assistant tool_use + user tool_result，并生成唯一 tool_use id。
- 设置 `schema_version=3`。
- 通过现有 SessionStore lock 与 atomic save 写回。

#### 损坏或不明数据

- v2 messages 损坏但 history 合法时，允许从 history 恢复。
- 未知 role、非法 content 或无法配对的结构不静默跳过。
- 迁移失败抛出明确的 `SessionMigrationError`。
- 原 session 文件保持不变，不启动新的 run；若失败发生在备份成功之后，已写好的原始备份可以保留，绝不回写半迁移内容。

### 7.3 路径与原子性

- session id 必须是 basename-safe 的合法标识。
- `load()` 的 read → JSON decode → validate/migrate → backup → atomic replace 全程持有现有 SessionStore lock；普通 `save()` 也使用同一把锁。
- `save()` 与 migration 复用一个“调用方已持锁”的内部 atomic-write primitive；migration 不通过二次调用 public `save()` 形成嵌套锁。
- backup 使用唯一且不覆盖的文件名并保存原始 bytes；它在目标 replace 前完成。
- migration 禁止直接 `Path.write_text()`。
- migration 是幂等的；同一 v3 文件重复 load 不产生新备份或变更。

敏感 session 备份的权限与 redaction 策略归 A 阶段处理，但 C 不允许继续使用非锁、非原子的迁移写回。

## 8. Consumer 迁移与删除

### 8.1 Runtime report

`build_report()` 改为直接从 messages：

- 统计 tool events。
- 用 `session_messages_count` / `session_messages_chars` / `session_messages_tokens` 记录完整 canonical transcript 规模，与 request metadata 中裁剪后的 `messages_*` 区分。
- `session_messages_chars/tokens` 使用与 build_v2 request metadata 相同的 content-only 序列化/估算规则，对完整 canonical messages 计算；不把 `_pico_meta` 大小混进模型上下文指标。
- 删除旧 `history_chars` 字段；这是 session v3 的明确内部报告契约变化。
- 用 canonical messages 加 injection/request metadata 描述实际 Provider 请求，不把 plain transcript 冒充完整 wire payload。
- report 将最后一次 request metadata 与 `completion_usage_totals` 分开；totals 汇总本 run 全部 Response.usage 的 input/output/total/cached/cache-create/cache-read 数值字段，`cache_hit` 表示任一 call 命中。
- 不在 report 中新增一份完整或 recent transcript 副本；需要显示时才调用 `render_transcript()`，避免无价值的重复存储和额外敏感面。

不再调用 `history_text()`。

### 8.2 Evaluation

`pico/evaluation/` 中所有依赖扁平 history 或 legacy prompt 的代码改为：

- 读取 messages content blocks。
- 检查 tool_use/tool_result id。
- 检查 recalled-memory 与 injection metadata。
- 使用 `render_transcript(messages)` 仅做显示或 prompt-string fallback。
- 用不同的 `history_soft_cap` 构造 context ablation 的有界/近似无界 request view；删除只被 legacy build 消费的 `context_reduction` feature flag。
- memory experiment client 直接检查实际 Fallback prompt 或 request messages 中的 working-file summary，不再调用 `Pico.record()` 造 legacy history。
- 三个 memory variant 都先产生相同的 bootstrap read/tool_result，再用合法 filler turns 与相同 history soft cap 让该旧 turn 从 follow-up request view 中整体掉出；否则原 tool_result 会泄漏答案，使 on/off 对照无效。
- fixed benchmark artifact 的 `initial_history_empty` 等存储实现字段改为 messages 语义；`history_reference` 这类任务类别名称是领域描述，可以保留。

### 8.3 CLI session inspect

现有 dual-write drift inspector 改为 v3 invariant inspector：

- schema version。
- message role sequence。
- tool_use/tool_result 配对。
- orphan tool id。
- content block 形状。
- 顶层 `_pico_meta` 完整性。

不再比较 history 与 messages。

### 8.4 删除清单

完成 consumer 迁移后删除：

- `session["history"]` 初始化与四处双写。
- `ContextManager.build()`。
- legacy history rendering、compression 与 metadata helpers。
- `Pico._build_prompt_and_metadata()` 与 `Pico.prompt_metadata()`。
- `Pico.record()`。
- 当前就地 append + save 的 `Pico.record_message()`，由 AgentLoop 单一 copy-on-write commit helper 取代。
- `Pico.history_text()`。
- `Pico.prompt()`。
- `Pico.parse()`、`retry_notice()`、`parse_xml_tool()` 等静态 parser 委派。
- `FallbackAdapter` 内的 parse 与 fake tool UUID。
- `legacy_string_path` pytest marker。
- 最后一个 legacy skip。
- CLI dual-write drift 文案与分支。
- 只服务 legacy build 的 `context_reduction` feature flag、section budget 常量与测试。

旧 session migrator 允许引用 history；生产运行时、报告、CLI 和评测不允许。

`Pico.repeated_tool_call()` 改为扫描 canonical tool_use blocks，而不是作为漏网的 runtime history consumer。测试和 benchmark 如需预置 transcript，直接构造合法 v3 messages fixture；不为测试保留新的 history compatibility API。

## 9. AgentLoop 生命周期

```text
start turn
  -> append plain user message with copy-on-write save
  -> create TaskState / RunStore
  -> enter terminal-error boundary
       -> refresh prefix + evaluate resume state once
       -> build one turn injection snapshot
       -> attempt loop
            -> build request view
            -> Provider.complete_v2
            -> read completion telemetry from Response.usage
            -> decode_action
               -> ToolAction
                    -> execute tool
                    -> append tool pair with one copy-on-write save
                    -> continue
               -> RetryAction
                    -> trace reason
                    -> set one-shot feedback
                    -> continue
               -> FinalAction
                    -> append assistant text with copy-on-write save
                    -> terminal finish
       -> step/retry limit
            -> append runtime final with copy-on-write save
            -> terminal finish
```

所有真实发送给 Provider 的 metadata 来自 `build_v2()`。legacy prompt 不再参与 trace、checkpoint 或 report 决策。

只有最初 plain user message 的 save 发生在 run 创建之前；它失败时不启动 run。run 创建后的 preflight、request build、Provider、tool、message commit 和 finalization 都位于同一个 terminal-error boundary 内，未预期异常会标记 `runtime_error` 后重抛，不能留下仍为 running 的 TaskState。

一次 attempt 的 `prompt_built`、`model_requested`、`action_decoded` 和 `model_turn` 必须引用同一个 request metadata snapshot。turn preflight metadata 在该 turn 内稳定；request-specific 的 message count、drop count、retry-feedback presence 可以随 attempt 改变。

## 10. 错误处理

| 来源 | 表示 | 行为 | 继续 |
| --- | --- | --- | --- |
| 空响应、畸形 tool、非法 native input | RetryAction | trace + 下一次一次性 feedback | 是，不消耗 tool step |
| 工具参数/审批拒绝 | ToolExecutionResult(rejected) | model-visible tool_result | 是，不消耗 tool step |
| 工具执行失败 | ToolExecutionResult(error) | 记录 effect/status，反馈模型 | 是，消耗 tool step |
| 工具部分成功 | ToolExecutionResult(partial_success) | 保留 recovery metadata，反馈模型 | 是，消耗 tool step |
| Provider 异常 | 原异常 | stop_model_error → terminal finish → 重抛 | 否 |
| Ctrl-C | KeyboardInterrupt | 标记 interrupted，写可安全写出的 terminal artifacts | 否 |
| session/message 持久化失败 | 原持久化异常 | 标记 persistence_error，保留已知 Tool Change，禁止再次请求模型 | 否 |
| run 创建后的其他未预期异常 | 原异常 | 标记 runtime_error → terminal finish → 重抛 | 否 |
| finalizer 子步骤失败 | finalization_errors | 尽力写其余工件，不覆盖 primary exception | 否 |
| session 迁移失败 | SessionMigrationError | 保留原文件，不启动 run | 否 |

### 10.1 Codec 总函数

模型返回的任何 content shape 都必须产生一种 Action；JSON/XML/content block 解析错误不允许逃出 codec。

### 10.2 单一 terminal finalizer

以下出口共用 terminal finalizer：

- success
- step limit
- retry limit
- Provider error
- graceful interrupt
- persistence/runtime error

SIGKILL 无法被进程内代码收尾，不在本阶段伪装支持。

finalizer 合同：

1. 先把 TaskState 在内存中设为 terminal，再做任何可能失败的写入。
2. success/limit 使用正常 assistant final；Provider error、graceful interrupt 与 runtime error 追加一条带 `_pico_meta.origin="runtime_terminal"` 的通用、限长且已脱敏 assistant terminal message，以闭合 canonical turn。该消息不冒充模型输出，也不把完整异常回显进 transcript。
3. task state、resume checkpoint、recovery checkpoint、trace、report 分步骤 best-effort 执行；一个子步骤失败不阻止尝试其余步骤。
4. 如果已有 Provider、KeyboardInterrupt、tool 或 persistence primary exception，finalizer error 只记录/附加，最终仍重抛 primary exception；没有 primary exception 时才抛 finalization/persistence error。
5. “产生 terminal artifacts”的完成标准以相应 store 可写为前提；磁盘故障不能被伪报为成功。

TaskState 使用稳定 stop reason：现有 `model_error` / `persistence_error`，新增 `interrupted` / `runtime_error`；interrupt 状态为 stopped，Provider/runtime/persistence error 状态为 failed。

### 10.3 Tool 状态

- runner 不再通过返回 `"error: ..."` 字符串表达拒绝。
- 可在执行前判定的拒绝必须进入 validation 并返回 `rejected`。
- protected user-note write、memory 参数和其他可预判约束都在 validation 中拒绝。
- runner 的 unavailable、not-found、I/O 与 store 错误改为抛出异常；意外异常由 ToolExecutor 转成 `error` 或 `partial_success`。只有 ToolExecutor 可以为 model-visible tool_result 格式化错误文本。
- ToolExecutionResult 的闭集为 `ok | rejected | error | partial_success`；AgentLoop 只读 status，不解析 content。
- 每个 ToolExecutionResult metadata 都包含 `tool_status`、`effect_class`、`read_only`；error code、affected paths 与 tool_change_id 按适用性附加。
- `read_only` 从 effect class 推导，不再从 `tool["risky"]` 反推。
- 最小 effect 表固定为：文件/搜索/memory read/repo lookup/受限 delegate → `read_only`；`memory_save` → `memory_write`；run_shell/write_file/patch_file → `workspace_write`。未知工具沿用 fail-safe 的现有 fallback，不新增 capability registry。
- `agent.read_only=True` 在 runner 前拒绝所有非 `read_only` effect，不依赖 `tool["risky"]`；因此 memory_save 也会得到 `rejected/read_only_block`。delegate 的 child 本身继续以 read_only 构造。
- `memory_save` 使用 `memory_write` effect class，不再标成 read_only。
- 只有 `workspace_write` 触发 workspace snapshot 并进入可恢复 turn checkpoint。
- `memory_write` 创建非 restoreable 的 Tool Change 审计记录与 trace metadata，但不拍 workspace snapshot、不生成 file entries、不加入 `run_tool_change_ids`，因此不会制造空的 recovery checkpoint。
- ToolExecutor 在 runner 周围单独捕获 `KeyboardInterrupt`：best-effort 将当前 pending Tool Change 标成 `interrupted` 后原样重抛；不能把 Ctrl-C 转成普通 `ToolExecutionResult(error)`，也不能留下当前 owner 的永久 pending record。
- memory 内容的敏感信息规则进入 A 阶段。

### 10.4 Reset

`/reset` 清除：

- messages
- recently recalled state
- recall error counters
- working memory
- working file summaries
- transient retry/turn state
- `checkpoints.current_id`、`resume_state` 与 session 内 `recovery.current_checkpoint_id`

它可以保留 session 内旧 checkpoint items 作为审计数据，但 current pointer 必须清空；它不删除磁盘 checkpoint、run artifacts 或用户 memory files，也不改变 session id。

## 11. C 与 A 的明确边界

### 11.1 C 阶段完成

- Action 解码错误可见、可重试。
- Provider、interrupt、persistence 与 run 内未预期异常后，store 可写时 run 不停留在 running。
- tool_use/tool_result 成对持久化。
- 工具拒绝和失败状态不再假成功。
- completion usage、request metadata 与 session transcript metrics 各有单一且不混名的真源。
- reset 与 messages-only 语义一致。
- migration 失败不覆盖用户 session。

### 11.2 A 阶段后续

单独设计和实现：

- raw tool result、checkpoint、approval、verification 的全链 redaction。
- 敏感文件读取策略。
- snapshot eligibility 的 secret basename/extension 排除。
- shell command risk fail-closed。
- workspace 外 redirect、解释器执行、sudo/system command 分类。
- 同一路径 A→B→C 的 checkpoint entry 合并。
- restore apply 前 hash 复验与半恢复记录。
- 跨进程 pending Tool Change Recovery Review。
- `.env` 原子写、0600 权限及 CLI secret 输入方式。

## 12. 测试设计

### 12.1 阶段 0：绿色基线

任何语义改动前：

1. 只修当前 11 个 Ruff 问题。
2. 运行 `./scripts/check.sh`。
3. 记录基线 HEAD、测试数、skip 数和运行时间。

不在红色门禁上继续架构迁移。

### 12.2 Action Codec 单元测试

新增一个小型 `tests/test_action_codec.py`，覆盖：

- 单个 native tool_use。
- 多个 native tool_use 与 ignored count。
- native input 非 dict。
- leading JSON tool protocol。
- leading attribute tool protocol。
- leading final protocol。
- 普通文本。
- 非首位/引用/code fence 中的 tool 标签。
- 畸形 leading tool。
- 空响应。
- STOP_SEQUENCE。
- MAX_TOKENS 文本与 truncated flag。
- native tool 相对 text/stop reason 的优先级。
- 首个 native tool 非法时不跳到第二个。
- 多个 text block 按顺序合并。
- leading / non-leading `<final>` 与截断 closing tag。
- 未知 stop reason 不被误当成 END_TURN。
- RetryAction excerpt 限长且 notice 不回显 raw output。

不引入 property-test 依赖。

### 12.3 Messages 与 Session 测试

- 同一 turn injection snapshot 跨多个工具步骤保持可见。
- `memory_index` 在 durable index 为空时仍能渲染 recent working-file summary，并受既有 budget/escaping 约束。
- 同一 turn 工具刚生成的 working summary 不重渲染，下一顶层 turn 才进入 snapshot。
- retry feedback 只在下一次 request 可见，随后消失。
- feedback 不改变 system cache key。
- request view 不修改 canonical messages。
- request metadata 有 `messages_chars` / `runtime_feedback_present`，且不再有 legacy prompt/section metadata 或 v2 `prompt_cache_key` alias。
- tool_use/tool_result 成对一次保存。
- pair save 失败时 session 文件与内存 transcript 都保持旧值。
- v1 → v3。
- v2 dual-write → v3。
- v2 空 messages + 非空合法 history → v3。
- 损坏 messages 从合法 history 恢复。
- 未知 role 迁移失败且原文件不变。
- v3 load 幂等。
- read/backup/replace 使用同一锁事务，backup 保留原始 bytes 且不覆盖。
- v3 保留所有非 transcript session 状态，但不存在 history key。
- reset 后 messages 与 transient state 清空。

### 12.4 Runtime 与 Provider 集成

- native Provider path：Response → Action → tool → final。
- Fallback path：原始文本 Response → 同一个 Action codec。
- native/fallback 行为 parity。
- native system prefix 不强制 XML；Fallback flattened prompt 独有且只有一份严格文本协议说明。
- malformed → feedback → corrected response。
- 每次 `Response.usage` 进入对应 model_turn，report totals 汇总所有 attempts，AgentLoop 不依赖 Provider side channel。
- Provider 非 RuntimeError 异常也产生 terminal report。
- Provider error / KeyboardInterrupt 追加 runtime terminal assistant，并在可写 store 上留下 terminal task state。
- run 创建后的 request/preflight 异常标记 runtime_error，而不是留下 running。
- finalizer 子步骤失败不遮蔽 primary exception。
- tool 副作用完成但 pair save 失败时停止为 persistence_error，且不发下一次 Provider 请求。
- rejected/error/partial_success tool 状态真实。
- memory runner unavailable/not-found、protected-note write 和 memory_save 都不会被标成 ok。
- read_only agent 拒绝 memory_save/workspace writes，但允许受限 delegate 与读取工具。
- memory_write 有审计记录但没有 workspace snapshot 或空 recovery checkpoint。
- runner 中 Ctrl-C 将当前 pending Tool Change 标为 interrupted 后重抛。
- resume turn 的 request 确实包含 checkpoint injection。
- `dropped_messages > 0` 触发真实 request-view context_reduction checkpoint。
- stable prefix 在删除 legacy build 后仍包含 memory usage/reading guidance。
- no orphan tool message invariant。

### 12.5 Benchmark

#### Memory quality

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
```

要求 8/8。

#### Memory ablation

重写 legacy experiment client，使其检查真实 request messages/Fallback prompt 与 working-summary injection；三个 variant 的 follow-up request 都必须已丢弃 bootstrap tool turn，artifact 记录该前置条件是否成立。

固定重生命令：

```bash
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2(repetitions=5)'
```

最低行为标准：

- `memory_on.repeated_reads < memory_off.repeated_reads`
- `memory_on.repeated_reads < memory_irrelevant.repeated_reads`
- `memory_on.memory_hit_rate > memory_off.memory_hit_rate`
- `memory_on.memory_hit_rate > memory_irrelevant.memory_hit_rate`
- 三个 variant 的 `correct_rate == 1.0`

不锁死旧的 60→0 或 100% 数字。最终文档只能引用当前 HEAD 实际生成的结果。

#### Performance smoke

```bash
uv run python -m benchmarks.perf.bench_build_v2
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
```

要求输出合法 JSON；不把本机 noisy latency 设为硬 CI 阈值。

### 12.6 结构性删除检查

除 migration code 外，生产代码引用必须归零：

- `session["history"]`
- `.get("history")`
- `ContextManager.build(`
- `Pico.record(`
- `Pico.record_message(`
- `history_text(`
- `legacy_string_path`
- `FallbackAdapter -> parse_model_output`
- stable prefix 中强制 `<tool>/<final>` 的 response protocol（只允许出现在 Fallback flatten instruction）
- runtime 静态 parser 委派
- AgentLoop 回读 `last_completion_metadata`
- 生产 runtime 的 `context_reduction` feature flag
- `pico/model_output_parser.py`（逻辑已经收敛到 action codec 后）
- build_v2/report metadata 中的 `prompt_chars`、`sections`、`budget_reductions`、`prompt_cache_key` alias

### 12.7 全量本地门禁

```bash
./scripts/check.sh
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2(repetitions=5)'
uv run python -m benchmarks.perf.bench_build_v2
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
```

要求：

- Ruff 0 errors。
- pytest 全绿。
- legacy skip 数为 0；平台条件 skip 单独说明。
- memory-quality 8/8。
- memory ablation 达到 §12.5 的相对行为标准并生成当前 HEAD artifact。
- 三个 perf smoke 输出合法 JSON。

### 12.8 一次真实 E2E

在本地门禁全部通过后，使用当前已配置的一个 Anthropic-compatible 路径运行一次：`--provider anthropic` 或 `--provider deepseek` 二选一，不要求两者都跑，也不扩展到 Provider matrix。live harness 只增加这个显式选择，先调用现有 `load_project_env()` 再按 `providers.defaults` 的 canonical env 名取值；不新建 model resolver。README 不再声称支持脚本实际不会加载的 `.env` 或别名。

```bash
uv run python -m benchmarks.live_e2e.run_live_session --provider deepseek
# 或：--provider anthropic
```

继续使用现有 live-e2e 的：

- provider call 上限
- total token 上限
- cost guard
- wall-clock timeout
- fixture 恢复

所有 guard 的 assertion 必须读取本次 `RunConfig`，不能继续硬编码 15 calls / 200K tokens 而忽略命令行覆盖。至少一个专门的 tool turn 明确要求使用 API 提供的 native tool；若真实后端只返回 text protocol，该 live gate 失败，不能用 Fallback 或伪 native block 把它改写成通过。

TurnRunner 的 provider-call count 与 token/cost totals 从该 turn 全部 `model_turn` trace events 的 `Response.usage` 聚合，不再读取 Provider 的“最后一次 completion”可变属性；多工具 attempt 不能只计最后一 call。sniff wrapper 只保留验证每次实际 wire user content 的职责。

真实 E2E 的每个 Provider call 都必须给出可解析的 input/output token usage；缺失时 cost guard 状态为 unknown 并使 gate 失败，不能按 0 token 通过。

至少断言：

- native Action 解码。
- tool_use/tool_result 配对。
- `action_decoded.origin=native_tool_use` 至少出现一次。
- turn injection 在同一真实 tool turn 的每个 Provider call 中都存在。
- session schema v3 且无 history。
- system cache key 跨该真实 tool step 的 Provider calls 稳定。
- 每个 call usage 完整，聚合 token/call totals 未超过 RunConfig。
- task/report/trace 为 terminal。

live 不负责概率性诱发 malformed retry；“跨 retry 稳定、feedback one-shot”由 §12.3/12.4 的确定性 scripted Provider 测试完成。live 报告不得因为提前 aborted、少跑 turn 或 assertion list 为空而出现 `overall_pass=true`。

联网前的离线 live-harness 单测必须覆盖：`.env` 加载、anthropic/deepseek canonical env 选择、RunConfig guard 覆盖值、多 `model_turn` usage 求和、aborted/少 turn 强制 overall-fail，以及报告中不出现 key 值。

报告必须记录：

- git HEAD
- Python 版本
- session schema
- Provider/model 名称
- action origin summary
- aborted reason（为空也显式记录）
- assertion summary

不得记录 API key。

## 13. 实施阶段

本节只定义可独立验证的交付顺序；逐文件、逐测试步骤由后续 implementation plan 给出。

### Phase 0：恢复基线

- 清零当前 Ruff 错误。
- 全量 pytest。

### Phase 1：Inert foundations

- 建立 action codec、messages helper、v3 validator/migrator 纯函数与单元测试。
- 这些 helper 先不切换生产调用链；Fallback 和旧 AgentLoop 仍一起工作，因此本阶段结束时全量测试必须保持绿色。

### Phase 2：Consumer-first readiness

- runtime report、除 memory experiment 外的 evaluation/benchmark、CLI session inspect 和对应 tests 先改为读取 messages。
- context ablation 改测真实有限/近似无界 request view。
- 生产运行时暂时仍保留现有 dual write，但本阶段已迁移的 consumer 不再依赖 history；不新增 compatibility layer。

### Phase 3：Atomic Action switch

- 在同一个可回滚、可验证的切换中：FallbackAdapter 开始返回 raw text Response，同时 AgentLoop 改为调用 `decode_action()`。
- 同一切换把 stable prefix 改为 protocol-neutral，并由 Fallback flatten 独占文本协议 instruction；避免 native 与 fallback 中任一方失去可执行格式。
- AgentLoop 从 `Response.usage` 读取 completion metadata，并写 `action_decoded` trace。
- 不能提交“Fallback 已 raw、AgentLoop 尚未 codec”或相反的半状态。

### Phase 4：Request-loop convergence

- 显式 turn preflight。
- turn injection snapshot 与 one-shot retry feedback。
- 复用 `memory_index` source 注入 recent working-file summaries，恢复 legacy build 曾提供的 working-memory request 路径。
- 同阶段改写 memory experiment 的真实 request 检查、移除 bootstrap tool turn 泄漏并解除 legacy skip；不能先启用一个必然无差异的 benchmark。
- 只调用 build_v2；`dropped_messages` 接管 context-reduction checkpoint 触发。
- 在删除 legacy build 的同一阶段将 memory usage/reading guidance 移入 stable prefix，避免中间版本重复注入。
- 删除 `ContextManager.build()`、`Pico._build_prompt_and_metadata()`、legacy section renderer 和生产 `context_reduction` flag。

### Phase 5：Runtime integrity

- copy-on-write message commit 与成对工具消息保存。
- tool status/effect class、read_only enforcement、Provider error、interrupt、persistence error 与 terminal finalizer 按 §10 收敛。
- 本阶段可继续读取现有 v2 session，结束时全量测试绿色；不把 persistence/error 改造和 schema 激活混成一次不可定位的大切换。

### Phase 6：Messages v3 cutover 与删除

- 激活同锁事务的 v1/v2 → v3 migration。
- 新 session 直接创建为 v3，停止所有 history 双写。
- repeated-tool detection 与 reset 改读/改写 canonical messages。
- 删除 `Pico.record()` / `Pico.record_message()`、history helpers、旧 parser 文件/委派和 marker。
- 运行结构性删除检查与全量测试，确认没有隐藏 consumer 后再继续。

### Phase 7：证据与文档

- 重生当前 HEAD benchmark。
- 更新 review-pack 的真实基线，并标记旧 benchmark 归档的历史口径。
- live-e2e 增加 `anthropic|deepseek` 单选、action origin、HEAD/Python/session schema 与 aborted reason。
- live preflight 先加载项目 `.env`，统一 README 与代码使用的 canonical Anthropic/DeepSeek 环境变量名。
- 所有 live guard 使用 RunConfig，aborted/少跑 turn 不能产生 overall-pass。
- provider calls 与 usage/cost 从全部 `model_turn` trace 聚合，不读取最后一次 Provider metadata。

### Phase 8：最终验证

- 全量本地门禁。
- 结构性检查。
- 一次真实 Anthropic-compatible/DeepSeek E2E。

## 14. 完成标准

C 阶段只有在以下条件全部满足时完成：

1. `Response -> decode_action -> Action -> AgentLoop` 是唯一模型决策路径。
2. runtime completion telemetry 只从 `Response.usage` 进入 per-call trace，并在 report 中汇总全部 calls。
3. 运行时只保存 session v3 messages 作为 transcript。
4. 旧 session 可在单锁事务内备份、迁移、幂等加载；失败不覆盖原 session。
5. retry feedback 对下一次真实 request 可见且只出现一次。
6. turn injection 跨 retry 和工具步骤保持可见。
7. Provider 异常和 graceful interrupt 在 store 可写时产生 terminal artifacts，finalizer 不遮蔽 primary exception。
8. tool_use/tool_result 不产生可持久化 orphan；副作用后 pair-save 失败有 persistence_error 与 Tool Change 证据。
9. rejected/error/partial_success 不被记录成 ok，memory_write 不伪装 read-only 或 workspace recovery。
10. 所有 legacy production reference 归零。
11. Ruff、pytest、memory-quality、memory ablation 和 perf smoke 达到本设计门禁。
12. working-file summaries 通过真实 v3 request injection 生效；memory ablation 在旧 tool turn 已被裁掉的前提下按当前 HEAD 报告结果。
13. 一次真实 Anthropic-compatible/DeepSeek E2E 全部断言通过，且至少观察到一个 native ToolAction。
14. 未实现 Provider 配置重做、registry、gateway、并行工具或新依赖。

完成 C 后，才为 A 阶段另写设计与 implementation plan。
