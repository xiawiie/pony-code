# Pico Action Kernel 与 Messages v3 收敛设计

- 日期：2026-07-10
- 状态：已完成交互式设计确认，待用户审阅书面规范
- 当前分支：`memory`
- 范围：优化顺序中的 C 阶段——运行时内核与消息模型收敛
- 后续：C 阶段验收后，另行设计 A 阶段——安全与可信基线

## 1. 摘要

Pico 当前已经具备完整的本地 coding-agent harness 骨架：CLI、Provider、上下文、工具执行、session、run artifacts、memory 和 recovery 都有可运行实现，完整 pytest 基线为 668 passed、1 skipped。

问题不在于“缺少更多功能”，而在于主运行链仍同时维护两套决策与消息语义：

1. Provider 返回的 `Response` 有时在 `FallbackAdapter` 内被提前解析成伪 native tool call，`AgentLoop` 又对 `Response` 做第二次决策。
2. 每次模型尝试先构建并丢弃 legacy string prompt，再构建实际发送的 v2 request；trace 与 checkpoint 可能描述未发送的 prompt。
3. `session["messages"]` 是真实 Provider transcript，但运行时仍双写 `session["history"]`，报告、评测和部分 CLI 继续读取旧结构。
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

这首先证明旧评测不再适配真实 v2 messages；它不单独证明 memory 产品能力已经失效。C 阶段必须先修评测路径，再根据当前 HEAD 的真实结果更新结论。

## 4. 目标架构

### 4.1 组件边界

#### Provider Response

`pico/providers/response.py` 继续定义 Provider-neutral 的 `Response` 与 `StopReason`。

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

不创建只有一个实现的 Codec 类，不建立 factory。

它只回答：

> Pico 应该做什么？

#### Canonical Messages

建立一个小型通用消息模块 `pico/messages.py`，集中已有且至少被三个边界共用的操作：

- 去除 Provider 不应看到的 `_pico_meta`。
- 复制 messages 并替换最近的顶层 user 文本，构造 request view。
- 成对追加 tool_use / tool_result。
- 渲染 transcript 供 Fallback、报告与评测消费。
- 检查 role 序列及 tool_use/tool_result 配对不变式。

该模块不负责 Provider HTTP、上下文选择或工具执行。

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

#### ContextManager

`ContextManager.build_v2()` 成为唯一 Provider request builder。

继续保留 `build_v2` 名称，避免在本阶段扩大 Provider API 重命名范围。`ContextManager.build()` 及其 legacy history 压缩辅助函数全部删除。

#### FallbackAdapter

`FallbackAdapter` 继续承担：

- 将 system、tools、messages 拍平成 prompt string。
- 调用只支持 `complete(prompt, ...)` 的现有 Provider。
- 将原始文本包装成 `Response(content=[text])`。

它不再：

- 调用 `parse_model_output`。
- 生成 UUID。
- 将文本协议伪装成 native tool_use。

所有文本协议统一在 `decode_action()` 中解释。

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
- 空 name 或非 dict input 不抛出到 AgentLoop，而是产生 `RetryAction`。

### 5.2 FinalAction

字段：

- `text: str`
- `origin: Literal["native_text", "text_protocol"]`
- `truncated: bool = False`

语义：

- `END_TURN` 且有文本时结束。
- 文本协议 `<final>...</final>` 去掉 wrapper 后结束。
- `MAX_TOKENS` 且有文本时保留当前兼容行为，返回 FinalAction，同时在 Action 与 trace 标记 `truncated=true`。

### 5.3 RetryAction

字段：

- `reason_code: str`
- `notice: str`
- `origin: Literal["native", "text_protocol"]`

稳定 reason code：

- `empty_response`
- `malformed_tool_protocol`
- `invalid_native_tool`
- `stop_sequence`
- `unsupported_response_shape`

语义：

- 不修改 canonical messages。
- trace 记录 reason code 与脱敏后的有限 excerpt。
- notice 只注入下一次 request，消费后清除。
- 不消耗 tool step，但受现有 attempt 上限约束。

### 5.4 严格文本协议

允许执行：

- 去掉前导空白后，第一个有效 token 是 `<tool>` JSON 形式。
- 去掉前导空白后，第一个有效 token 是 `<tool name="...">` attribute 形式。

不允许执行：

- 普通回答中间出现的 `<tool>`。
- Markdown code fence 或引用示例中的 tool 标签。
- 前面已有自然语言、后面才出现的 tool 标签。

首位 tool 协议存在但畸形时返回 `RetryAction`；非首位标签作为普通最终文本处理。

## 6. Canonical Messages 与 Request View

### 6.1 持久化真源

session v3 只持久化 `messages`。

每条 canonical message 保存：

- plain user 请求。
- assistant tool_use content block。
- user tool_result content block。
- assistant final text。
- Pico 内部使用的顶层 `_pico_meta`。

不保存：

- 注入后的 user 文本副本。
- 一次性 retry feedback。
- Provider wire metadata。
- history 镜像。

### 6.2 Turn injection snapshot

在一个顶层 user turn 开始时：

1. 将 plain user message 追加到 canonical messages。
2. `AgentLoop` 调用现有 `render_current_user_message()` 一次，完成 intent、memory recall、workspace、project structure 与 resume checkpoint 渲染。
3. 得到该 turn 的 injection snapshot 与 telemetry。

同一 turn 内的所有模型尝试复用这份 snapshot。

原因：

- 防止同一 turn 第二次 recall 被 `recently_recalled` 去重后消失。
- 保证多次模型尝试看到同一决策上下文。
- 工具产生的新事实已经通过 tool_result 进入 transcript，无需重复扫描并重写旧注入。
- system cache key 不受 retry 或工具步骤影响。

resume checkpoint 必须获得非零来源预算；否则 renderer 中存在该 source 但实际永远不会注入。

### 6.3 Request view

每次 Provider 请求：

1. 浅拷贝 canonical messages。
2. 从后向前找到最近的顶层 plain user message，忽略 role=user 的 tool_result carrier。
3. 将该条内容替换为 turn injection snapshot + plain user request。
4. 若存在 pending retry feedback，在同一 request view 中追加 `<pico:runtime_feedback>` block。
5. 去掉 `_pico_meta` 后发送。

不修改 session 中保存的原始 messages。

`ContextManager.build_v2()` 接收已经生成的 snapshot、telemetry 和可选 runtime feedback，只负责组装 request 与最终 metadata；它不在 attempt loop 中重复触发 recall。这样 injection 生命周期由 `AgentLoop` 管理，请求形状仍只有 `build_v2()` 一个真源。

### 6.4 Tool message pairing

当前实现先单独保存 assistant tool_use，再执行工具，再保存 user tool_result。进程在工具执行中断时会留下孤立 tool_use。

目标流程：

1. Action 留在内存。
2. 写 `tool_started` trace。
3. ToolExecutor 创建 pending Tool Change 并执行工具。
4. 得到 `ToolExecutionResult`。
5. 在内存中构造 tool_use 与 tool_result。
6. 一次 SessionStore save 追加消息对。

如果进程在工具执行中崩溃，session 不留下 Provider 无法接受的孤立 tool_use；pending Tool Change 保留恢复证据。

## 7. Session v3 迁移

### 7.1 Schema

将 session schema 升级到 v3：

- v1：history-only。
- v2：messages + transitional history。
- v3：messages-only。

### 7.2 加载规则

#### v3

校验必要字段后原样返回，不做写入。

#### v2

- 任何写回前先备份原文件。
- 若 `messages` 是合法 list，完全信任 messages。
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
- 原文件与备份保持不变，不启动新的 run。

### 7.3 路径与原子性

- session id 必须是 basename-safe 的合法标识。
- migration write 复用 `SessionStore.save()`，不直接 `Path.write_text()`。
- migration 是幂等的；同一 v3 文件重复 load 不产生新备份或变更。

敏感 session 备份的权限与 redaction 策略归 A 阶段处理，但 C 不允许继续使用非锁、非原子的迁移写回。

## 8. Consumer 迁移与删除

### 8.1 Runtime report

`build_report()` 改为直接从 messages：

- 渲染 recent transcript。
- 统计 tool events。
- 用 `messages_chars` / `messages_tokens` 记录真实 transcript 规模。
- 删除旧 `history_chars` 字段；这是 session v3 的明确内部报告契约变化。
- 用 canonical messages 加 injection/request metadata 描述实际 Provider 请求，不把 plain transcript 冒充完整 wire payload。

不再调用 `history_text()`。

### 8.2 Evaluation

`pico/evaluation/` 中所有依赖扁平 history 或 legacy prompt 的代码改为：

- 读取 messages content blocks。
- 检查 tool_use/tool_result id。
- 检查 recalled-memory 与 injection metadata。
- 使用 `render_transcript(messages)` 仅做显示或 prompt-string fallback。

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
- `Pico.record()`。
- `Pico.history_text()`。
- `Pico.prompt()`。
- `Pico.parse()`、`retry_notice()`、`parse_xml_tool()` 等静态 parser 委派。
- `FallbackAdapter` 内的 parse 与 fake tool UUID。
- `legacy_string_path` pytest marker。
- 最后一个 legacy skip。
- CLI dual-write drift 文案与分支。

旧 session migrator 允许引用 history；生产运行时、报告、CLI 和评测不允许。

## 9. AgentLoop 生命周期

```text
start turn
  -> append plain user message
  -> build one turn injection snapshot
  -> create TaskState / RunStore
  -> attempt loop
       -> build request view
       -> Provider.complete_v2
       -> decode_action
          -> ToolAction
               -> execute tool
               -> append tool pair once
               -> continue
          -> RetryAction
               -> trace reason
               -> set one-shot feedback
               -> continue
          -> FinalAction
               -> append assistant text
               -> terminal finish
  -> step/retry limit
       -> terminal finish
```

所有真实发送给 Provider 的 metadata 来自 `build_v2()`。legacy prompt 不再参与 trace、checkpoint 或 report 决策。

## 10. 错误处理

| 来源 | 表示 | 行为 | 继续 |
| --- | --- | --- | --- |
| 空响应、畸形 tool、非法 native input | RetryAction | trace + 下一次一次性 feedback | 是，不消耗 tool step |
| 工具参数/审批拒绝 | ToolExecutionResult(rejected) | model-visible tool_result | 是，不消耗 tool step |
| 工具执行失败 | ToolExecutionResult(error) | 记录 effect/status，反馈模型 | 是，消耗 tool step |
| 工具部分成功 | ToolExecutionResult(partial_success) | 保留 recovery metadata，反馈模型 | 是，消耗 tool step |
| Provider 异常 | 原异常 | stop_model_error → terminal finish → 重抛 | 否 |
| Ctrl-C | KeyboardInterrupt | 标记 interrupted，写可安全写出的 terminal artifacts | 否 |
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

SIGKILL 无法被进程内代码收尾，不在本阶段伪装支持。

### 10.3 Tool 状态

- runner 不再通过返回 `"error: ..."` 字符串表达拒绝。
- 可在执行前判定的拒绝必须进入 validation 并返回 `rejected`。
- 意外异常由 ToolExecutor 转成 `error` 或 `partial_success`。
- `read_only` 从 effect class 推导，不再从 `tool["risky"]` 反推。
- `memory_save` 使用 `memory_write` effect class，不再标成 read_only。
- 只有 `workspace_write` 触发 workspace snapshot/recovery；`memory_write` 保留审计 metadata，但不伪造 workspace file entries。
- memory 内容的敏感信息规则进入 A 阶段。

### 10.4 Reset

`/reset` 清除：

- messages
- recently recalled state
- working memory
- transient retry/turn state
- session 内 recovery pointer

它不删除磁盘 checkpoint、run artifacts 或用户 memory files。

## 11. C 与 A 的明确边界

### 11.1 C 阶段完成

- Action 解码错误可见、可重试。
- Provider 异常后 run 不停留在 running。
- tool_use/tool_result 成对持久化。
- 工具拒绝和失败状态不再假成功。
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

不引入 property-test 依赖。

### 12.3 Messages 与 Session 测试

- 同一 turn injection snapshot 跨多个工具步骤保持可见。
- retry feedback 只在下一次 request 可见，随后消失。
- feedback 不改变 system cache key。
- request view 不修改 canonical messages。
- tool_use/tool_result 成对一次保存。
- v1 → v3。
- v2 dual-write → v3。
- 损坏 messages 从合法 history 恢复。
- 未知 role 迁移失败且原文件不变。
- v3 load 幂等。
- reset 后 messages 与 transient state 清空。

### 12.4 Runtime 与 Provider 集成

- native Provider path：Response → Action → tool → final。
- Fallback path：原始文本 Response → 同一个 Action codec。
- native/fallback 行为 parity。
- malformed → feedback → corrected response。
- Provider 非 RuntimeError 异常也产生 terminal report。
- rejected/error/partial_success tool 状态真实。
- resume turn 的 request 确实包含 checkpoint injection。
- no orphan tool message invariant。

### 12.5 Benchmark

#### Memory quality

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
```

要求 8/8。

#### Memory ablation

重写 legacy experiment client，使其检查真实 messages 与 recalled-memory metadata。

最低行为标准：

- `memory_on.repeated_reads < memory_off.repeated_reads`
- `memory_on.memory_hit_rate > memory_off.memory_hit_rate`

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
- `history_text(`
- `legacy_string_path`
- `FallbackAdapter -> parse_model_output`
- runtime 静态 parser 委派

### 12.7 全量本地门禁

```bash
./scripts/check.sh
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json
uv run python -m benchmarks.perf.bench_build_v2
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
```

要求：

- Ruff 0 errors。
- pytest 全绿。
- legacy skip 数为 0；平台条件 skip 单独说明。
- memory-quality 8/8。
- 三个 perf smoke 输出合法 JSON。

### 12.8 一次真实 E2E

使用当前已配置的 Anthropic-compatible/DeepSeek 路径，不扩展到全部 Provider。

继续使用现有 live-e2e 的：

- provider call 上限
- total token 上限
- cost guard
- wall-clock timeout
- fixture 恢复

至少断言：

- native Action 解码。
- tool_use/tool_result 配对。
- turn injection 跨工具步骤存在。
- session schema v3 且无 history。
- system cache key 跨 retry/tool step 稳定。
- task/report/trace 为 terminal。

报告必须记录：

- git HEAD
- Python 版本
- session schema
- Provider/model 名称
- assertion summary

不得记录 API key。

## 13. 实施阶段

本节只定义可独立验证的交付顺序；逐文件、逐测试步骤由后续 implementation plan 给出。

### Phase 0：恢复基线

- 清零当前 Ruff 错误。
- 全量 pytest。

### Phase 1：Action contract

- 建立 action codec 与单元测试。
- 让 FallbackAdapter 返回 raw text Response。

### Phase 2：Canonical messages

- 建立 messages helper。
- turn injection snapshot。
- retry feedback。
- 成对工具消息保存。
- session v3 migration。

### Phase 3：AgentLoop convergence

- AgentLoop 只消费 Action。
- 只调用 build_v2。
- provider error / interrupt 统一收尾。
- tool status 与 reset 语义修正。

### Phase 4：Consumer migration 与删除

- runtime report。
- evaluation / benchmark。
- CLI session inspect。
- tests。
- 删除全部 legacy production surface。

### Phase 5：证据与文档

- 重生当前 HEAD benchmark。
- 更新 review-pack 的真实基线。
- 标记旧 benchmark 归档的历史口径。
- 统一 live-e2e README 与代码使用的 Anthropic-compatible 环境变量名。
- 修正 live-e2e 的 aborted 状态汇总，使失败不能产生 overall-pass。

### Phase 6：最终验证

- 全量本地门禁。
- 结构性检查。
- 一次真实 E2E。

## 14. 完成标准

C 阶段只有在以下条件全部满足时完成：

1. `Response -> decode_action -> Action -> AgentLoop` 是唯一模型决策路径。
2. 运行时只保存 session v3 messages。
3. 旧 session 可备份、迁移、幂等加载；失败不覆盖。
4. retry feedback 对下一次真实 request 可见且只出现一次。
5. turn injection 跨工具步骤保持可见。
6. Provider 异常和 graceful interrupt 产生 terminal artifacts。
7. tool_use/tool_result 不产生可持久化 orphan。
8. 所有 legacy production reference 归零。
9. Ruff、pytest、memory-quality 和 perf smoke 达到本设计门禁。
10. memory ablation 使用真实 v3 数据流并按当前 HEAD 报告结果。
11. 一次真实 Anthropic-compatible/DeepSeek E2E 全部断言通过。
12. 未实现 Provider 配置重做、registry、gateway、并行工具或新依赖。

完成 C 后，才为 A 阶段另写设计与 implementation plan。
