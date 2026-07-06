# Pico 系统设计深度剖析

- 日期：2026-07-06
- 目标：把 Pico 的设计逻辑讲清楚，而不是包装成空泛面试稿
- 方法：围绕“为什么这样设计、目的是什么、做了什么、怎么做的、收益与边界”展开
- 范围：基于当前本地仓库代码、README、CONTEXT、架构文档、memory 文档和 review-pack；飞书 wiki 仍无读取权限，本文暂不引用飞书内容

## 1. 先判断项目本质：Pico 到底在解决什么问题

Pico 的表层形态是一个 Python CLI coding agent，但它真正要解决的不是“怎么调用模型”，而是：

> 大模型在真实代码仓库里执行任务时，如何让它的输入、行动、副作用和结果都可控、可审计、可恢复。

如果只看功能，Pico 有 CLI、provider、memory、context、tool、recovery、benchmark。这样看会很散。更正确的看法是：这些模块分别对应 agent 落地时的四个核心风险。

| 风险 | 如果不处理会怎样 | Pico 对应设计 |
| --- | --- | --- |
| 输入不可控 | 模型不知道项目上下文，或者 prompt 太长、重点丢失 | ContextManager / PromptPrefix / MemoryRefresher / RepoMap |
| 行动不可控 | 模型直接跑 shell 或乱改文件，副作用不可预测 | Tool registry / ToolExecutor / recovery_policy |
| 副作用不可恢复 | 改错文件后不知道改了什么，也不知道能否安全回滚 | Tool Change Record / Turn Checkpoint / Blob / RecoveryManager |
| 过程不可复盘 | 任务失败只能看最终输出，无法定位是哪一步坏了 | RunStore / task_state / trace.jsonl / report.json / verification evidence |

所以 Pico 的核心不是“功能多”，而是它围绕 agent 的不确定性建立了几层边界。

可以把 Pico 总结为：

> 一个围绕大模型的本地执行边界系统。

## 2. 总体架构：不是聊天循环，而是受控执行循环

### 2.1 为什么要这样设计

普通 chat loop 是：

```text
user -> model -> answer
```

普通 tool agent 是：

```text
user -> model -> tool -> model -> answer
```

但 coding agent 不够。因为 tool 可能改文件、跑命令、触发外部副作用。因此 Pico 需要一个更完整的执行循环：

```text
user request
  -> build workspace/runtime context
  -> create task_state and run artifacts
  -> build bounded prompt
  -> model response
  -> parse into tool / retry / final
  -> execute tool through policy
  -> record trace and recovery metadata
  -> finish report / checkpoint
```

这个流程在 `docs/architecture/agent-harness-v1-overview.md` 里被写成 8 步 runtime flow。代码上由 `AgentLoop.run()` 承担。

### 2.2 设计目的

AgentLoop 的目的不是“循环调用模型”，而是保证每一次模型行动都进入状态机。

它要回答：

- 这次 run 的 id 是什么？
- 当前是第几次 model attempt？
- 已经执行了几个 tool step？
- 当前 prompt 是怎么构造的？
- 模型返回的是工具、重试还是最终答案？
- 工具是否执行成功？
- 这一步是否产生 recovery checkpoint？
- 最终停止原因是什么？

### 2.3 做了什么

`AgentLoop` 做了这些事：

1. 接收 user message。
2. 写入 session history。
3. 创建 `TaskState`。
4. 创建 run directory。
5. 写 `run_started` trace。
6. 每轮构造 prompt。
7. 写 `prompt_built` 和 `model_requested` trace。
8. 调用 model client。
9. 解析模型输出。
10. 如果是 tool，调用 `ToolExecutor`。
11. 写 `tool_started`、`tool_executed`、`tool_finished` trace。
12. 必要时创建 resume checkpoint 和 recovery checkpoint。
13. 如果是 final，写 report 并结束。
14. 如果达到 step limit / retry limit / model error，也统一走 finish path。

### 2.4 怎么做的

关键实现是 `TaskState` + `RunStore` + trace event。

`TaskState` 记录运行状态：

- `run_id`
- `task_id`
- `status`
- `tool_steps`
- `attempts`
- `last_tool`
- `stop_reason`
- `final_answer`
- `checkpoint_id`
- `recovery_checkpoint_id`

`RunStore` 负责每次 run 的审计工件：

- `.pico/runs/<run_id>/task_state.json`
- `.pico/runs/<run_id>/trace.jsonl`
- `.pico/runs/<run_id>/report.json`

这里有一个关键设计：`task_state.json` 不是事件日志，它是状态机快照；`trace.jsonl` 才是事件序列；`report.json` 是最终总结。

### 2.5 这样设计的收益

- 每次 run 都有独立审计空间。
- 运行中可以观察当前状态。
- 运行后可以复盘每一步。
- 错误停止和正常停止共用 finalization 流程。
- trace 和 report 不混在一起。

### 2.6 边界

AgentLoop 不应该关心每个工具怎么执行，也不应该亲自计算文件恢复逻辑。它只调 `execute_tool()`，然后收集 metadata。否则主循环会变成大杂烩。

所以 Pico 正确的边界是：

- AgentLoop 管任务生命周期。
- ToolExecutor 管行动执行。
- ContextManager 管模型输入。
- RecoveryManager 管恢复决策。
- RunStore 管审计落盘。
- CheckpointStore 管恢复真相。

## 3. Context 设计：控制模型“看见什么”

### 3.1 为什么需要 ContextManager

coding agent 的表现高度依赖 prompt。模型如果看不到项目约定，就会乱猜；如果看到太多历史，就会丢重点；如果每轮 prompt 都变化很大，缓存和复盘都困难。

所以 ContextManager 解决的不是“拼 prompt”，而是：

> 在有限预算下，构造一个可解释、可降级、可复盘的模型输入。

### 3.2 设计目的

ContextManager 的目的有五个：

1. 明确哪些内容进入模型输入。
2. 明确不同内容的优先级。
3. 超预算时按规则降级。
4. 保护当前请求不被裁剪。
5. 把 prompt 构造过程写入 metadata，方便 trace/report 复盘。

### 3.3 做了什么

Pico 把 prompt 分为三个主 section：

1. `prefix`
2. `history`
3. `current_request`

其中 `prefix` 里又包含：

- stable prompt prefix
- memory usage guidance
- memory reading guidance
- project structure
- memory index

`history` 里包含：

- volatile workspace state
- checkpoint text
- session history

`current_request` 是当前用户请求。

### 3.4 怎么做的

`ContextManager.build()` 每轮执行时会：

1. 通过 `MemoryRefresher.refresh_if_stale()` 获取 memory index 和 project structure。
2. 取 runtime 当前 stable prefix。
3. 拼入 memory 使用规则。
4. 拼入 project structure。
5. 拼入 memory index。
6. 把 branch/status/recent commits 放进 volatile workspace state。
7. 渲染 history。
8. 拼入 current request。
9. 如果超过总预算，就按 `DEFAULT_REDUCTION_ORDER = ("history", "prefix")` 压缩。

最关键的实现原则是：

> 当前请求永远不裁剪。

这不是小细节，而是设计取舍。因为当前请求定义了本轮任务目标，裁剪它会直接导致任务偏移。

### 3.5 Memory 在 Context 中的角色

Memory 不等于 Context。

- Memory 是持久知识来源。
- Context 是本轮模型实际看到的输入。

Pico 没有把 memory 全量塞入 prompt，而是放入 `<memory_index>`。这个 index 只告诉模型有哪些记忆文件，以及可以用 `memory_search / memory_read` 访问。

这样做的目的：

- 避免 prompt 爆炸。
- 避免旧记忆污染当前任务。
- 让模型按需读取。
- 保持 stable prefix 尽可能稳定。

### 3.6 Project Structure 在 Context 中的角色

RepoMap 不把完整符号表塞进 prompt，而是提供：

- 顶层项目结构进入 prompt。
- 具体符号通过 `repo_lookup` 按需查询。

这样做的原因：

全量符号表会很大，而且大多数符号与当前任务无关。把它作为工具查询能力更合理。

### 3.7 这样设计的收益

- 模型输入可解释。
- prompt 超预算时行为确定。
- 当前任务目标不丢。
- memory 和 repo map 不污染 prompt。
- prompt metadata 可进入 trace/report。
- stable prefix 有机会被 provider prompt cache 利用。

### 3.8 边界

ContextManager 解决不了模型理解能力问题。它只能控制输入结构，不能保证模型一定读懂。它也不是语义检索系统；memory retrieval 当前是 BM25 + CJK bigram。

面试中要说清楚：

> ContextManager 的价值是输入治理，不是让模型变聪明。

## 4. Tool / Safety 设计：控制模型“能做什么”

### 4.1 为什么需要 ToolExecutor

模型输出是文本。如果直接执行模型输出，系统会有三个问题：

1. 不知道模型动作的语义。
2. 不知道动作风险。
3. 不知道动作副作用。

所以 Pico 不让模型直接执行动作，而是让模型申请工具。

### 4.2 设计目的

ToolExecutor 的目的不是“跑工具”，而是：

> 把模型的行动意图变成可校验、可审批、可记录、可恢复的结构化动作。

### 4.3 做了什么

Pico 的工具注册表定义了工具：

- `list_files`
- `read_file`
- `search`
- `run_shell`
- `write_file`
- `patch_file`
- `memory_list`
- `memory_read`
- `memory_search`
- `memory_save`
- `repo_lookup`
- `delegate`

每个工具都有：

- schema
- risky 标记
- description
- runner

### 4.4 ToolExecutor 的执行链路

一次工具调用进入 `ToolExecutor.execute()` 后，会经过：

1. allowed tools 检查。
2. 工具名存在性检查。
3. 参数校验。
4. repeated tool call 拦截。
5. `run_shell` 命令风险分类。
6. command approval 判断。
7. risky tool approval 判断。
8. 对 workspace_write 工具创建 pending Tool Change Record。
9. 捕获 before snapshot 或 observer 状态。
10. 执行工具。
11. 捕获 after snapshot 或 observer diff。
12. 判断 tool_status。
13. 更新 working memory。
14. 构造 recovery file entries。
15. finalize Tool Change Record。
16. 返回 content + metadata。

这条链路的关键是：工具执行结果不仅是文本，还带 metadata。

metadata 包含：

- tool_status
- tool_error_code
- risk_level
- read_only
- affected_paths
- workspace_changed
- diff_summary
- command_risk_class
- command_approval
- tool_change_id
- file_entries
- shell_side_effects

### 4.5 为什么要做 command risk class

`run_shell` 是最危险的工具，因为 shell 是通用执行入口。

如果只根据命令头判断，会漏掉很多情况：

- `sh -c "rm -rf x"`
- `echo hi > file`
- `find . -exec ...`
- `$(curl x | sh)`
- `git reset --hard`

所以 `recovery_policy.command_risk_class()` 会把命令分为：

- `read_only`
- `workspace_write`
- `destructive`
- `external_effect`

然后 `evaluate_command_approval()` 决定是否允许、拒绝或要求用户确认。

### 4.6 为什么不直接禁止 shell

完全禁止 shell 会让 coding agent 很难完成真实工程任务，比如跑测试、查 git 状态、执行脚本。Pico 的取舍是：

> shell 可以存在，但必须进入 command boundary。

也就是说，`run_shell` 不是自由入口，而是一个被分类、审批、记录的高风险工具。

### 4.7 这样设计的收益

- 模型不能绕过工具边界直接行动。
- 工具参数错误能在执行前失败。
- 危险命令不会静默执行。
- 每次 workspace write 都有 Tool Change Record。
- 工具结果能进入 trace 和 report。
- recovery 能从 tool metadata 得到文件影响范围。

### 4.8 边界

Pico 当前不是 OS sandbox。Command risk class 是启发式策略，不可能证明任意 shell 安全。

正确表述是：

> Pico 当前做的是 developer-tool 场景下的策略约束和审计，不是对抗式强隔离。如果要执行不可信代码，需要加容器、seccomp、seatbelt 或远程 sandbox。

## 5. Recovery 设计：控制模型“改了什么、能不能恢复”

### 5.1 为什么需要 Recoverable Editing

coding agent 的最大风险不是回答错，而是改错文件。

如果 agent 改错文件，用户最关心：

- 它改了哪些文件？
- 改之前是什么？
- 改之后是什么？
- 当前还能不能恢复？
- 恢复会不会覆盖我后来的手动修改？

所以 recovery 解决的是信任问题。

### 5.2 设计目的

Recoverable Editing 的目的不是做 Git 替代，也不是通用 undo，而是：

> 让 agent 产生的仓库改动可检查、可解释、可预览、可保守恢复。

### 5.3 做了什么

Pico 设计了几类对象：

1. `Tool Change Record`  
   记录一次工具调用的影响。

2. `Checkpoint Record`  
   记录一个用户可见的恢复点。

3. `File-State Blob`  
   按 sha256 存储文件原始字节。

4. `Restore Plan`  
   恢复前生成的决策结果。

5. `Restore Checkpoint`  
   恢复动作完成后新写的记录，不改历史。

### 5.4 Tool Change Record 为什么是 pending -> finalized

工具执行可能成功、失败、部分成功，甚至执行中断。

如果只在工具成功后写记录，会漏掉失败但已经产生副作用的情况。因此 Pico 在真正执行工具前先创建 pending record，执行后再 finalize。

这解决的是：

> 即使工具失败，也不能默认它没有改工作区。

### 5.5 为什么 Turn Checkpoint 不是每个 tool 一个

用户关心的恢复入口通常是“一次用户请求”，不是“第 3 个内部工具调用”。所以 Pico 把一次 run 中的 tool changes 汇总成 Turn Checkpoint。

这样做的目的：

- 用户心智更简单。
- 一次任务的多个文件改动可以一起 review。
- 内部工具粒度保留在 tool changes，不暴露成主要入口。

### 5.6 Restore Preview 怎么工作

`RecoveryManager.preview_restore()` 会读取 checkpoint record，然后对每个 file entry 调 `_plan_entry()`。

每条 entry 会被判断为：

- `restore`：可以自动恢复。
- `review`：需要人工看，因为缺 before blob 或不 eligible。
- `conflict`：当前文件状态与 expected hash 不一致。

这一步不改磁盘。

### 5.7 为什么要检查 expected_current_hash

假设 agent 改了 `a.py`，然后用户又手动改了 `a.py`。如果此时 restore 直接覆盖，就会丢用户后续修改。

所以 Pico 记录 agent 修改后的 expected hash。恢复时先计算当前文件 hash：

- 当前 hash == expected hash：说明文件仍是 agent 修改后的状态，可以恢复。
- 当前 hash != expected hash：说明用户或其他工具又改过，进入 conflict。

这是 recovery 的核心不变量。

### 5.8 为什么要用 File-State Blob

只存 diff 不够稳定。diff 依赖上下文，后续文件变化后 patch 可能无法应用，或者应用到错误位置。

Pico 第一阶段选择存文件状态 blob：

- before blob 表示恢复目标。
- after hash 表示 expected current state。
- blob 按 content hash 存储，天然去重。

这样恢复逻辑更简单：

> 当前状态匹配 expected，则把 before blob 写回。

### 5.9 为什么恢复后还要写 Restore Checkpoint

恢复本身也是一次仓库变化。如果恢复后不记录，历史就断了。

Pico 选择写新的 `checkpoint_type="restore"`，并记录：

- source checkpoint id
- restore plan id
- restored paths
- skipped entries
- pre_restore_file_states
- post_restore_file_states

这样恢复动作本身也可审计。

### 5.10 为什么不用 Git 作为恢复引擎

Git HEAD 不等于 agent 执行前状态。

现实中：

- 用户工作区可能本来 dirty。
- agent 可能修改 untracked 文件。
- agent 可能生成新文件。
- Git checkout 会影响不属于 agent 的变化。

所以 Pico 把 Git 作为 review context，而不是 restore engine。

正确表达：

> Pico 恢复的是 agent 造成的状态转移，而不是恢复到某个 Git commit。

### 5.11 这样设计的收益

- 用户能知道 agent 改了什么。
- 恢复前能 preview。
- 当前文件变化时不会盲目覆盖。
- 恢复动作也有 provenance。
- trace 和 recovery 不混用。

### 5.12 边界

当前是文件级恢复，不是 hunk 级恢复。遇到冲突不自动 merge。

这是刻意取舍：先保证恢复语义简单、可验证，再考虑更复杂的 hunk-level restore。

## 6. Memory 设计：不是“长期记忆”，而是可审计知识层

### 6.1 为什么需要 memory

如果没有 memory，每次 agent 都像第一次进项目。它不知道：

- 项目约定。
- 用户偏好。
- 之前踩过的坑。
- 常用命令。
- 特殊环境配置。

但 memory 如果设计不好，会污染 prompt。例如 agent 把临时工具结果、当前 diff、无意义路径都写进长期记忆。

### 6.2 设计目的

Pico memory 的目的不是模拟人类记忆，而是：

> 为 Context 提供可审计、可分层、可检索的项目知识来源。

### 6.3 做了什么

Pico memory v2 分三层：

1. `AGENTS.md`  
   项目约定，每次 session 可读。

2. `.pico/memory/notes/*.md`  
   用户手写 notes，agent 可读。

3. `.pico/memory/agent_notes.md`  
   agent 在用户明确要求时追加的短笔记。

### 6.4 为什么这样分层

因为这三类知识来源不同：

- 项目约定是规则。
- 用户 notes 是用户拥有的知识。
- agent notes 是 agent 追加的经验。

如果混在一起，agent 很容易覆盖用户知识，也很难追溯来源。

### 6.5 memory 怎么进入 prompt

MemoryRefresher 不把完整 memory 塞进 prompt，而是渲染 `<memory_index>`。

index 只包含：

- 哪些 notes 存在。
- 哪些 agent records 存在。
- 大小信息。
- 提示模型用 memory_search / memory_read 访问。

这个设计的目的：

- 控制 prompt 体积。
- 保留可发现性。
- 避免旧知识直接污染当前任务。

### 6.6 为什么用 BM25 + CJK bigram

优点：

- 不需要外部服务。
- 行为可解释。
- 容易测试。
- 中文可以通过 CJK bigram 做基础匹配。

缺点：

- 不理解语义同义词。
- 不会自动归纳。
- 不会自动遗忘。

所以正确表述是：

> Pico memory 是 durable local knowledge，不是 semantic long-term memory。

### 6.7 边界

Memory 不能替代 session。文档也明确说：memory 和 sessions 是独立轴，没有 cross-session memory rewind。

这意味着：

- session 管对话连续性。
- memory 管跨 session 知识。
- checkpoint 管恢复状态。

这三个不能混。

## 7. RunStore 和 CheckpointStore：审计证据与恢复真相分离

### 7.1 为什么要分开

很多系统会把日志和恢复记录混在一起。Pico 没有这么做。

原因是：

- 日志用于解释发生了什么。
- 恢复记录用于决定怎么恢复。

这两个目的不同。

### 7.2 RunStore 做什么

RunStore 负责 `.pico/runs/<run_id>/`：

- `task_state.json`
- `trace.jsonl`
- `report.json`

它回答：

- 这次任务运行到哪？
- 模型调用了几轮？
- 工具调用顺序是什么？
- 最终状态是什么？

### 7.3 CheckpointStore 做什么

CheckpointStore 负责 `.pico/checkpoints/`：

- `records/`
- `tool_changes/`
- `blobs/`

它回答：

- 哪些工具造成了 workspace change？
- 哪些文件有 before/after 状态？
- 哪些 blob 仍被引用？
- 哪个 checkpoint 可以 restore？

### 7.4 这层设计的意义

最重要的一句话：

> 能复盘，不等于能恢复。

`trace.jsonl` 可以告诉你模型调用了 `patch_file`，但它不应该承担恢复依据。恢复必须依赖文件状态、hash 和 blob。

## 8. Provider 设计：支撑层，不是主线

### 8.1 为什么 Provider 不是主线

Provider 支持多个模型后端很重要，但它不是 Pico 的系统核心。

因为 provider 解决的是：

> 模型从哪里来。

而 Pico 的核心解决的是：

> 模型来了以后，怎么被约束、执行和复盘。

### 8.2 做了什么

Pico 支持：

- Ollama
- OpenAI-compatible Responses
- Anthropic-compatible Messages
- DeepSeek Anthropic-compatible

runtime 主要依赖统一的 `complete()` 接口。

### 8.3 设计目的

Provider adapter 的目的：

- 把协议差异限制在 adapter 内。
- 不让 AgentLoop 到处关心 OpenAI/Anthropic 差异。
- 保留 usage/cache metadata。
- 支持 fake model client 做测试。

### 8.4 边界

多 provider 抽象不会抹平所有差异。错误格式、streaming、cache、usage 都有 provider-specific 逻辑。

所以面试里不要把 provider 说成最大亮点。它是必要支撑。

## 9. Verification / Benchmark：证明 harness 行为

### 9.1 为什么需要证据

Agent 系统不能只说“我测试过”。它需要结构化证据：

- 本地测试门禁。
- 运行 trace。
- report。
- benchmark。
- verification evidence。

### 9.2 做了什么

Pico 的本地门禁：

```bash
./scripts/check.sh
```

内部是：

```bash
uv run ruff check .
uv run pytest -q
```

另外有：

- memory quality benchmark
- provider benchmark
- review-pack snapshot

### 9.3 fake benchmark 的意义

fake benchmark 不证明模型智能，但证明 harness 路径：

- scripted model output 能触发工具。
- memory tools 能执行。
- trace 能写入。
- scoring 能读取 trace 并判断行为。

这是 release evidence，不是模型能力评测。

## 10. 最终应该如何讲 Pico

不要讲：

> 我做了 CLI、memory、context、provider、recovery。

要讲：

> 我做的是一个本地 coding-agent harness，核心是给大模型建立工程边界。Context 控制模型看到什么，ToolExecutor 控制模型能做什么，Recovery 控制模型改错后如何恢复，RunStore/CheckpointStore 分离审计证据和恢复真相，Memory 提供可审计的持久知识来源，Provider 只是把不同模型接进这个 runtime。

这才是这个项目的清晰主线。

## 11. 面试中最值得深挖的 5 个点

### 11.1 为什么说 Pico 是 harness，不是 wrapper

Wrapper 只是转发用户输入和模型输出。Harness 会控制：

- 输入上下文。
- 工具能力。
- 执行策略。
- 状态持久化。
- 文件恢复。
- 审计证据。

Pico 明显属于后者。

### 11.2 为什么 Context 是输入治理

ContextManager 不只是拼 prompt，而是定义：

- 哪些信息进 prompt。
- 哪些只作为索引。
- 哪些可以裁剪。
- 哪些不能裁剪。
- 裁剪过程如何记录。

### 11.3 为什么 ToolExecutor 是系统咽喉

模型所有真实动作都从 ToolExecutor 进入本地世界。它是安全、审批、side effect、recovery metadata 的交汇点。

如果 ToolExecutor 设计不好，整个 agent 就不可控。

### 11.4 为什么 Recovery 是信任基础

用户是否敢让 agent 改文件，取决于能否知道它改了什么、能否安全恢复。

Pico 通过 checkpoint/blob/hash/conflict 机制建立这层信任。

### 11.5 为什么 Memory 要克制

Memory 如果过度自动化，会污染上下文。Pico 选择可审计、分层、按需检索，是为了保持长期知识的来源和使用可解释。

## 12. 这个项目的真实短板

### 12.1 Runtime 和 ToolExecutor 偏大

`Pico` 作为 composition root 连接了太多组件，`ToolExecutor` 也承担了执行和 side-effect recording。后续可以拆 facade 和 side-effect recorder。

### 12.2 Safe Execution 不是强 sandbox

策略分类不是安全证明。如果要执行不可信代码，需要 OS-level sandbox。

### 12.3 Recovery 是文件级，不是 hunk 级

保守但简单。复杂 merge 需要后续设计。

### 12.4 Memory 是 lexical，不是 semantic

BM25 可解释，但语义能力有限。

### 12.5 Provider 抽象还可以更强

可以引入显式 Protocol，统一 retry、error taxonomy、streaming metadata。

## 13. 从一次真实请求看完整链路

前面的章节是按模块讲。面试里更有说服力的方式，是按一次真实任务讲，因为这样能证明这些设计不是“为了架构而架构”，而是在一条执行链路里互相咬合。

假设用户说：

> 修复某个测试失败，并确保不要破坏现有行为。

Pico 内部不是直接把这句话扔给模型，而是进入下面这条链路。

### 13.1 第一步：Runtime 先构造工作区事实

Pico 会先知道当前 workspace 是什么状态：

- 当前目录。
- Git branch。
- Git status。
- 最近 commit。
- 项目文档。
- 可用工具。
- memory 是否存在。

为什么要先做这个？

因为 coding agent 不是纯聊天。模型的回答必须绑定到一个具体仓库状态。如果不记录工作区状态，后续复盘时就不知道模型是在什么上下文里做出的判断。

这一步的目的不是给模型无限上下文，而是形成一个 runtime snapshot。

### 13.2 第二步：ContextManager 构造本轮 prompt

接下来 ContextManager 会把输入分层：

```text
stable prefix
  + tool instructions
  + workspace stable text
  + memory guidance
  + memory index
  + project structure
history
  + volatile workspace state
  + checkpoint text
  + session messages
current_request
  + user 本轮请求
```

这一步的关键不是“拼得越多越好”，而是“哪些东西该稳定，哪些东西该按需查，哪些东西绝不能丢”。

为什么 current request 不能裁剪？

因为当前请求定义了这轮任务的目标。如果模型丢了当前请求，前面再多项目上下文也没用。

为什么 memory 不直接全塞进去？

因为旧 memory 可能过时，也可能与当前任务无关。直接塞进去会污染推理。Pico 只塞 memory index，让模型在需要时调用 `memory_search` 或 `memory_read`。

### 13.3 第三步：模型先做只读探索

模型通常会先调用：

- `search`
- `read_file`
- `repo_lookup`
- `memory_search`
- `memory_read`

这些工具大多是 read-only。

ToolExecutor 会做：

- allowed tools 检查。
- schema 校验。
- repeated call 检查。
- 执行工具。
- 写 trace metadata。

这一步为什么也要经过 ToolExecutor？

因为 read-only 工具虽然不改文件，但它仍然影响模型后续判断。trace 里需要记录模型查了什么、读了什么、工具是否成功。

### 13.4 第四步：模型申请修改文件

当模型决定修复时，会调用：

- `patch_file`
- `write_file`
- 或通过 `run_shell` 间接产生写入。

这时 ToolExecutor 的作用会变得更重。

以 `patch_file` 为例：

1. 检查工具是否允许。
2. 校验参数。
3. 判断它是 workspace write。
4. 在执行前创建 pending Tool Change Record。
5. 读取目标文件 before state。
6. 把 before content 写入 content-addressed blob。
7. 执行 patch。
8. 读取 after state。
9. 计算 hash。
10. 生成 file entry。
11. finalize Tool Change Record。
12. 返回工具结果和 recovery metadata。

为什么要在执行前创建 pending record？

因为工具可能失败，但失败不代表没有副作用。例如一个脚本执行到一半报错，前半段可能已经改了文件。先建 pending record，可以避免“失败工具没有记录”的盲区。

### 13.5 第五步：模型运行验证命令

修完后模型一般会调用：

```bash
uv run pytest ...
```

这会走 `run_shell`。

`run_shell` 的风险不取决于“它是不是测试命令”这句话，而取决于命令本身的分类。

Pico 会用 command risk policy 判断：

- 是只读命令？
- 会写 workspace？
- 是否 destructive？
- 是否 external effect？

这一步的核心设计是：shell 不是禁掉，而是纳入边界。

因为真实工程里必须跑测试、构建、脚本。如果完全禁用 shell，coding agent 就只能写代码，不能验证。Pico 的取舍是让 shell 继续存在，但让 shell 每次进入策略和审计流程。

### 13.6 第六步：RunStore 记录过程证据

执行过程中，RunStore 持续写：

- `task_state.json`
- `trace.jsonl`
- `report.json`

其中：

- `task_state.json` 表示当前状态。
- `trace.jsonl` 表示每一步事件。
- `report.json` 表示最终总结。

为什么不是只写一个 log？

因为状态、事件、总结三者用途不同。

- 状态用于恢复当前 run 进度。
- 事件用于审计时间线。
- 总结用于用户和后续系统快速读取结果。

### 13.7 第七步：生成 Turn Checkpoint

一次任务可能调用多个写工具，产生多个 Tool Change Record。Pico 会把这些 change 汇总成用户可理解的 checkpoint。

这就是为什么 recovery 入口不是“第几个工具”，而是“这轮任务造成的变更”。

用户心智是：

> 我想撤销这次 agent 的改动。

而不是：

> 我想撤销第 4 个 patch_file。

内部保留 tool change 粒度，外部提供 turn checkpoint 粒度，这是一个很重要的产品化设计。

### 13.8 第八步：用户需要恢复时先 preview

恢复不是立即覆盖文件，而是先生成 restore plan。

每个文件会进入三种状态之一：

- `restore`：当前状态仍匹配 agent 修改后的 expected hash，可以自动恢复。
- `review`：缺少足够恢复信息，需要人工判断。
- `conflict`：当前文件已经被后续修改，不能直接覆盖。

为什么要这样？

因为恢复最怕覆盖用户后来手动改的内容。Pico 通过 expected current hash 把“agent 改完后的状态”和“当前磁盘状态”对齐。只有两者一致，才自动写回 before blob。

这就是 Pico recovery 的核心安全性来源。

## 14. 最容易混淆的概念：Context、Memory、Session、Checkpoint、Trace

这几个词很容易被讲散。面试里应该直接拆开。

| 概念 | 本质 | 生命周期 | 存在哪里 | 是否直接进入 prompt | 解决的问题 |
| --- | --- | --- | --- | --- | --- |
| Context | 本轮模型实际看到的输入 | 每轮模型调用重新构造 | 不一定长期存储，metadata 可进 trace | 是 | 控制模型看见什么 |
| Memory | 跨 session 的持久知识 | 长期存在 | `AGENTS.md`、`.pico/memory/*` | 通常只放 index，内容按需读 | 让 agent 记住项目经验和用户约定 |
| Session | 对话连续性 | 一段交互内 | session history | 会作为 history 进入 prompt，可能被裁剪 | 保持多轮任务上下文 |
| Checkpoint | agent 造成文件变化后的恢复点 | 任务变更后存在 | `.pico/checkpoints/*` | 不作为主要 prompt 内容 | 支持 preview/restore |
| Trace | 执行事件时间线 | 每次 run 内存在 | `.pico/runs/<run_id>/trace.jsonl` | 否 | 复盘模型和工具行为 |
| Report | run 的最终总结 | run 结束后存在 | `.pico/runs/<run_id>/report.json` | 否 | 给人和系统读结果 |

一句话区分：

> Context 是模型这次看到了什么；Memory 是跨任务记住了什么；Session 是这段对话发生了什么；Checkpoint 是文件能不能恢复；Trace 是过程发生了什么。

### 14.1 为什么这些不能混在一起

如果把 memory 当 context，prompt 会膨胀，并被旧信息污染。

如果把 trace 当 checkpoint，恢复会缺少文件 hash 和 blob，无法安全写回。

如果把 session 当 memory，临时对话会变成长期知识，污染后续任务。

如果把 checkpoint 当 Git commit，恢复会误伤用户已有 dirty changes。

所以 Pico 的设计重点不是“都有存储”，而是每种存储只承担一个清晰责任。

## 15. 代码证据地图

下面是面试准备时建议重点看的文件。回答时不要背所有代码，但要能指出关键设计落在哪里。

| 设计点 | 代码位置 | 说明 |
| --- | --- | --- |
| Agent 主循环 | `pico/agent_loop.py` | 负责 run 生命周期、模型调用、工具调用、最终结束 |
| 任务状态 | `pico/task_state.py` | 定义 run 的状态字段、stop reason、tool steps |
| Prompt 构造 | `pico/context_manager.py` | 分层构造 prefix/history/current_request，控制预算和裁剪 |
| Stable prompt | `pico/prompt_prefix.py` | 放稳定系统规则、工具说明和工作区稳定信息 |
| Workspace snapshot | `pico/workspace.py` | 收集 branch/status/docs/project structure 等工作区信息 |
| 工具注册与 schema | `pico/tools.py` | 定义工具 schema、示例、runner 映射 |
| 工具执行边界 | `pico/tool_executor.py` | 校验、审批、风险分类、执行、side-effect metadata |
| 命令风险策略 | `pico/recovery_policy.py` | 将 shell 命令分类为 read/write/destructive/external |
| 运行审计 | `pico/run_store.py` | 写 task_state、trace、report |
| 恢复记录存储 | `pico/checkpoint_store.py` | 存 checkpoint、tool changes、content-addressed blobs |
| 恢复决策 | `pico/recovery_manager.py` | preview restore、冲突检测、apply restore |
| Recovery schema | `pico/recovery_models.py` | 定义 checkpoint、tool change、file entry 等数据结构 |
| Memory 刷新 | `pico/memory/refresher.py` | 渲染 memory index 和 project structure |
| Memory 文件安全 | `pico/memory/block_store.py` | 约束 memory path、安全读写、追加 agent notes |
| Memory 工具 | `pico/memory/tools.py` | memory_list/read/search/save |
| Repo lookup | `pico/repo_map.py` | 提供符号级项目查询能力 |

面试里可以这样说：

> 我不是把所有功能写在一个 loop 里，而是把模型输入、工具动作、运行审计、恢复真相分开。比如 ContextManager 只负责 prompt 输入治理，ToolExecutor 是行动边界，RunStore 只保留过程证据，CheckpointStore 才保存恢复依据。

## 16. 为什么这些设计不是过度设计

一个常见追问是：

> 这个项目是不是做复杂了？一个 coding agent 需要这么多层吗？

回答要从风险出发，而不是从模块出发。

### 16.1 如果没有 ContextManager

模型输入会变成临时字符串拼接。

问题是：

- prompt 超预算时不知道删哪里。
- memory 和 history 容易混在一起。
- 当前请求可能被截断。
- prompt 构造过程无法复盘。

所以 ContextManager 是为了让输入可治理。

### 16.2 如果没有 ToolExecutor

模型工具调用会直接变成函数调用。

问题是：

- 参数不合法时错误会很散。
- 危险命令很难集中拦截。
- 工具副作用没有统一 metadata。
- recovery 无法知道哪些文件被改了。

所以 ToolExecutor 是为了让行动可治理。

### 16.3 如果没有 CheckpointStore

只能依赖 Git 或 diff。

问题是：

- Git 不知道哪些变化属于 agent。
- diff 不能保证后续仍能安全应用。
- untracked 文件和 dirty workspace 很难处理。
- 用户后续手改内容可能被覆盖。

所以 CheckpointStore 是为了让副作用可恢复。

### 16.4 如果没有 RunStore

只能看最终答案。

问题是：

- 不知道模型为什么这么改。
- 不知道跑了哪些命令。
- 不知道失败在哪里。
- 不能把一次 run 的证据交给 benchmark 或 review-pack。

所以 RunStore 是为了让过程可审计。

### 16.5 如果没有 Memory 分层

长期知识会变成一团文本。

问题是：

- 用户写的规则和 agent 追加的经验混在一起。
- 旧知识会直接污染 prompt。
- 无法区分项目约定、用户 notes 和 agent notes。

所以 memory 分层是为了让长期知识可追溯。

## 17. 面试回答模板

### 17.1 30 秒版本

Pico 是一个本地 coding-agent harness。它的重点不是接入某个模型，而是把模型在代码仓库里的行为变成可控流程：ContextManager 控制模型看到什么，ToolExecutor 控制模型能做什么，RunStore 记录执行过程，CheckpointStore 和 RecoveryManager 记录并恢复文件副作用，Memory 提供可审计的长期项目知识。整体目标是让 agent 不只是能改代码，而是改代码这件事可复盘、可验证、可恢复。

### 17.2 2 分钟版本

我会把 Pico 理解成一个围绕大模型不确定性的工程边界系统。大模型进入真实仓库后，主要风险有四个：输入不可控、行动不可控、副作用不可恢复、过程不可复盘。Pico 分别用 ContextManager、ToolExecutor、Recovery 系统和 RunStore/CheckpointStore 解决。

ContextManager 不是简单拼 prompt，而是把 stable prefix、memory index、project structure、history 和 current request 分层，并且在 token 预算不足时按规则裁剪，保证当前请求不丢。ToolExecutor 是所有工具调用的入口，它会做 schema 校验、allowed tools 检查、shell 风险分类、approval 判断，并为写操作生成 Tool Change Record。Recovery 不是依赖 Git checkout，而是记录 before blob、after hash 和 expected current hash，恢复前先 preview，避免覆盖用户后续修改。RunStore 记录 task_state、trace 和 report，用来审计一次 run；CheckpointStore 保存 tool changes 和 blobs，用来支撑真正恢复。

所以这个项目的核心价值不是“我做了一个 CLI agent”，而是我把 coding agent 的执行过程产品化、工程化了。

### 17.3 深挖版本

如果面试官追问“你最核心的设计是什么”，建议优先讲 ToolExecutor + Recovery。

因为这是最能体现工程深度的地方。

可以这样回答：

> 我认为 coding agent 最难的不是生成代码，而是生成代码之后如何建立信任。Pico 里模型不能直接改文件，所有动作都必须经过 ToolExecutor。对于 workspace write 工具，ToolExecutor 会在执行前创建 pending Tool Change Record，捕获 before state，执行后捕获 after state，并把 file entry 写入 checkpoint system。恢复时 RecoveryManager 不会直接覆盖文件，而是检查当前文件 hash 是否仍等于 agent 修改后的 expected hash。如果一致，才写回 before blob；如果不一致，就标记 conflict。这样可以避免 agent restore 覆盖用户后来手动修改的内容。

这段回答有三个优点：

- 讲清楚了问题：信任和恢复。
- 讲清楚了机制：pending record、blob、hash、preview。
- 讲清楚了边界：不自动 merge，不盲目覆盖。

## 18. 高频追问与回答

### 18.1 为什么不用 Git 做恢复

因为 Git 只能表达仓库版本，不知道哪些变化属于 agent。用户工作区可能本来就是 dirty 的，agent 也可能修改 untracked 文件。如果直接 `git checkout`，会误伤用户已有修改。

Pico 恢复的是 agent 造成的状态转移，不是恢复到某个 commit。

### 18.2 为什么 memory 不直接塞进 prompt

因为 memory 是长期知识，不等于当前任务上下文。旧 memory 可能过时，也可能与当前任务无关。直接塞进去会让 prompt 变长，并把无关信息注入模型推理。

Pico 只放 memory index，让模型按需搜索和读取。

### 18.3 为什么 shell 不完全禁用

因为 coding agent 必须跑测试、构建、脚本和诊断命令。完全禁用 shell 会让 agent 无法闭环验证。

Pico 的设计是保留 shell，但通过 command risk class 和 approval 把 shell 纳入受控边界。

### 18.4 为什么不一开始就做 OS sandbox

OS sandbox 是更强安全边界，但成本更高，也会牵涉平台差异、文件挂载、网络策略、进程隔离和调试体验。Pico 当前更像本地 developer tool，它先做策略约束、审计和恢复。

如果要从本地可信开发工具升级到执行不可信代码的 agent，需要再加 OS-level sandbox。

### 18.5 为什么恢复做文件级，不做 hunk 级

文件级恢复语义简单：当前 hash 匹配 expected，就写回 before blob；不匹配就冲突。hunk 级恢复更灵活，但会引入 merge 复杂度，也更容易在代码结构变化后误应用。

Pico 当前优先保证恢复安全和可验证。

### 18.6 为什么 BM25 memory 也有价值

BM25 不如向量检索“聪明”，但它有几个优点：本地、可解释、稳定、容易测试，不依赖外部 embedding 服务。对项目约定、路径、符号、命令、错误文本这类内容，词法检索已经很实用。

它的边界是语义泛化能力弱，所以不能把它包装成完整长期记忆系统。

## 19. 可以突出但不要夸大的亮点

### 19.1 可以重点突出

- Harness 思维：不是 wrapper，而是执行边界。
- Context 输入治理：分层、预算、当前请求保护。
- ToolExecutor 行动边界：schema、approval、risk、metadata。
- Recovery 信任机制：blob、hash、preview、conflict。
- RunStore/CheckpointStore 分离：复盘和恢复不是一回事。
- Memory 克制：index + on-demand read，不污染 prompt。

### 19.2 不要过度包装

- 不要说 Pico 已经是强安全 sandbox。
- 不要说 memory 是完整人类长期记忆。
- 不要说 provider 抽象是最大创新点。
- 不要说 recovery 能自动解决所有冲突。
- 不要说 benchmark 能证明模型能力，只能证明 harness 路径。

面试中越诚实，越显得你真的理解系统边界。

## 20. 如果要继续演进，应该怎么做

### 20.1 第一优先级：收窄 ToolExecutor 职责

当前 ToolExecutor 是关键咽喉，但也偏重。它同时负责：

- 参数校验。
- approval。
- command risk。
- 执行工具。
- 捕获 side effect。
- 写 recovery metadata。

后续可以拆出：

- `ToolInvocationValidator`
- `CommandApprovalPolicy`
- `SideEffectRecorder`
- `ToolChangeBuilder`

但这个拆分应该在行为稳定后做，不应该一开始就抽象过度。

### 20.2 第二优先级：增强 Recovery 的 review 能力

可以加入：

- hunk-level preview。
- conflict diff 展示。
- selective restore。
- restore dry-run report。

但核心不变量不能变：

> 不确认当前状态，就不自动覆盖。

### 20.3 第三优先级：让 Memory 更语义化但保持可审计

可以加入 embedding 检索，但不要替代现有文件来源。

更合理的演进是：

- notes 仍然是 source of truth。
- embedding index 是派生缓存。
- 检索结果必须能回链到原文路径和行号。

这样既提升召回，又不丢可审计性。

### 20.4 第四优先级：更强 Safe Execution

如果定位从本地可信 developer tool 扩展到运行不可信代码，需要：

- OS sandbox。
- 网络隔离。
- 文件系统 allowlist。
- 进程资源限制。
- 外部凭证隔离。

这属于产品定位升级，不是简单加几个 if 判断。

## 21. 一句话总结

Pico 最重要的不是“让模型能写代码”，而是把模型写代码这件事变成一个受控工程流程：输入可治理，行动可审批，副作用可记录，错误可恢复，过程可复盘。
