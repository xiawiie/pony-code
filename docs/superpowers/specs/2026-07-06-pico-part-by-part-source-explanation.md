# Pico 逐模块源码级面试解说稿

- 日期：2026-07-06
- 目标：逐个 part 展开 Pico 的设计原因、目标、实现链路、关键细节、边界和面试讲法
- 依据：当前本地仓库源码、`README.md`、`CONTEXT.md`、`docs/architecture/agent-harness-v1-overview.md`、`docs/memory-model.md`、`docs/review-pack/README.md`
- 注意：飞书 wiki 读取时无权限，本文不引用飞书内容；如后续拿到权限，可以把其中与 Pico 相关的信息再融合进来

## 0. 先给整份稿子定调

这份稿子不是“Pico 有哪些模块”的说明书，而是面试时可以展开讲的系统设计剖析。

Pico 这个项目最容易讲散，因为它表面上有很多名词：

- CLI
- provider
- runtime
- agent loop
- context
- memory
- tools
- recovery
- checkpoint
- trace
- benchmark

如果按这个列表平铺，面试官听到的是“我做了很多功能”。但真正有含金量的讲法应该是：

> Pico 是一个本地 coding-agent harness。它不是简单封装模型，而是在模型和真实代码仓库之间建立一套工程边界：控制模型看到什么、能做什么、做完怎么记录、改错怎么恢复、结果怎么验证。

所以每个 part 都要围绕这个问题展开：

> 模型本身是不确定的，Pico 通过哪些边界把这种不确定性转成可控工程流程？

整体可以拆成 12 个 part：

1. CLI Surface：用户怎么进入 Pico，哪些命令不需要启动模型。
2. Runtime Composition：Pico 对象如何把所有子系统装配到一起。
3. AgentLoop：一次用户请求如何进入状态机。
4. ContextManager：模型每轮到底看到什么。
5. Memory：跨轮和跨 session 的知识怎么进入上下文。
6. RepoMap：为什么符号索引按需查询，不全塞 prompt。
7. Tool Registry：模型拥有哪些能力。
8. ToolExecutor：模型行动如何被校验、审批、记录。
9. Safe Execution：shell 风险如何分类和控制。
10. Recoverable Editing：文件改动如何变成可恢复的状态转移。
11. RunStore / CheckpointStore / SessionStore：不同持久化边界怎么分工。
12. Provider / Verification / Benchmark：模型后端和证据链如何支撑 harness。

下面逐个 part 讲。

## 1. CLI Surface：把用户入口和 agent runtime 分开

### 1.1 这个 part 解决什么问题

CLI 是用户看到 Pico 的第一层，但它不应该只是 `argparse`。

它解决的是：

> 用户输入的命令，哪些应该启动模型？哪些只是本地检查？哪些应该进入 recovery inspection？哪些只是 prompt？

这很重要。因为如果所有命令都启动 agent，就会有两个问题：

- 查看状态、查看 checkpoint、查看 memory 这种本地操作会浪费模型调用。
- `pico checkpoints preview-restore ...` 这种明确的恢复检查命令不应该被当成自然语言 prompt 发给模型。

所以 Pico 把 CLI surface 分成两类：

- pre-agent command：不启动模型，直接读本地状态或执行配置/恢复检查。
- agent command：构建 Pico runtime，进入 one-shot 或 REPL。

### 1.2 做了什么

代码入口在 `pico/cli.py`。

主要职责：

- 解析 CLI invocation。
- 判断是不是显式命令。
- 分派 `status / doctor / config / sessions / memory / checkpoints / runs`。
- 构建模型 client。
- 构建 workspace snapshot。
- 构建 `Pico` runtime。
- 根据 `run` / `repl` 进入执行。

核心命令包括：

- `pico-cli run <prompt>`：一次性任务。
- `pico-cli repl`：交互式会话。
- `pico-cli status`：查看 workspace/storage/provider/latest。
- `pico-cli doctor`：检查配置、凭证、provider 连接、存储目录。
- `pico-cli memory ...`：查看/搜索/迁移 memory。
- `pico-cli checkpoints ...`：查看 checkpoint、preview restore、apply restore、prune。
- `pico-cli runs ...`：查看 run artifacts。
- `pico-cli sessions ...`：查看 session。

### 1.3 怎么做的

关键流程是：

```text
main(argv)
  -> build_arg_parser()
  -> parse_cli_invocation()
  -> _dispatch_pre_agent_command()
      -> status / doctor / memory / checkpoints / runs / sessions ...
  -> build_agent(args)
      -> WorkspaceContext.build()
      -> load_project_env()
      -> _build_model_client()
      -> SessionStore(...)
      -> Pico(...)
  -> run_agent_once() or run_repl()
```

其中 `_dispatch_pre_agent_command()` 是一个很重要的边界。它让 inspection/recovery 命令不进入模型调用链。

比如：

```text
pico-cli checkpoints preview-restore ckpt_xxx
```

不会变成用户 prompt，而是：

```text
handle_checkpoints()
  -> CheckpointStore(root)
  -> RecoveryManager.preview_restore()
  -> render restore plan
```

### 1.4 为什么这样设计

因为 Pico 的定位不是“只有聊天入口的 agent”，而是一个可检查、可恢复、可运维的本地 harness。

一个成熟的 harness 必须允许用户不经过模型就查看底层状态：

- session 是否存在？
- run artifacts 在哪里？
- checkpoint 是否可恢复？
- memory 文件有哪些？
- provider 配置是否有效？

这些都是确定性本地操作，不需要模型参与。

### 1.5 设计细节

`COMMAND_SPECS` 给命令分了 category：

- `meta`
- `config`
- `inspection`
- `recovery`

`_RECOVERY_TOP_LEVEL_COMMANDS` 和 `_RECOVERY_SUBCOMMANDS` 用来判断类似 `checkpoints` / `runs` 是否应该走 recovery command。

这里有一个细节：如果用户输入的是普通 prompt，比如：

```text
pico-cli "checkpoints 这个设计怎么样"
```

不能误判成 recovery command。Pico 只有在 command/subcommand 结构匹配时才分派到 recovery inspection。

### 1.6 面试怎么讲

可以这样讲：

> CLI 层不是简单把字符串传给模型。我把它分成 pre-agent inspection 命令和真正进入 agent loop 的命令。像 status、doctor、memory、runs、checkpoints 这些只读或恢复检查命令，不需要模型参与，直接读 `.pico/` 下的本地状态。这样既减少无意义模型调用，也保证用户可以独立审计 agent 的运行证据和恢复点。

### 1.7 边界和不足

CLI surface 当前是命令行工具，不是完整 TUI/IDE。

它能做：

- 本地检查。
- 配置展示。
- recovery preview/apply。
- run/session artifacts 查看。

它不做：

- 富交互 UI。
- 可视化 diff。
- 多任务 dashboard。
- 云端任务管理。

这不是能力缺失，而是产品定位：Pico 先把 harness 的本地边界做清楚。

## 2. Runtime Composition：Pico 对象是整个系统的装配根

### 2.1 这个 part 解决什么问题

Pico 需要把很多子系统组合起来：

- model client
- workspace snapshot
- session store
- run store
- checkpoint store
- tool registry
- tool executor
- context manager
- memory store
- retrieval
- repo map
- recovery manager
- workspace observer

如果每个模块自己去创建依赖，会导致：

- 生命周期混乱。
- 测试很难替换。
- 状态散落。
- 子系统边界不清。

所以 `Pico` runtime 作为 composition root：负责一次性装配对象图。

### 2.2 做了什么

`pico/runtime.py` 里的 `Pico.__init__()` 完成装配：

- 保存 model client、workspace、approval policy、feature flags。
- 创建 `RunStore`。
- 创建 `CheckpointStore`。
- 创建 `ToolChangeRecorder`。
- 创建 `RecoveryCheckpointWriter`。
- 创建 `RecoveryManager`。
- 创建 `WorkspaceObserver`。
- 加载 `pico.toml` 中的 `[policy] max_blob_size`。
- 规范化 session shape。
- 创建 `WorkingMemory`。
- 创建 v2 memory 的 `BlockStore` 和 `Retrieval`。
- 创建 `RepoMap` 并在顶层 runtime 后台扫描。
- 创建工具注册表。
- 创建 `ToolExecutor`。
- 创建 prompt prefix。
- 创建 `ContextManager`。
- 评估 resume state。
- 保存 session。

### 2.3 为什么 runtime 要这么集中

因为 Pico 不是无状态函数调用。它有很多跨模块共享状态：

- 当前 session id。
- 当前 run id。
- 当前 task state。
- 当前 recovery checkpoint id。
- 当前 workspace root。
- 当前 secret redaction policy。
- 当前 approval policy。
- 当前 feature flags。
- 当前 memory store。

这些状态如果散落在模块内部，就很难保证一致性。

`Pico` 作为装配根可以保证：

- 所有 store 都用同一个 workspace root。
- trace/report/session 都使用同一套 redactor。
- ToolExecutor 和 RecoveryManager 使用同一个 CheckpointStore。
- ContextManager 使用同一个 memory store 和 repo map。
- delegate 子 agent 继承关键配置，但被设置成 read-only。

### 2.4 关键设计：resume-summary checkpoint 和 recovery checkpoint 是两条通路

代码里有两套 checkpoint：

1. `pico/checkpoint.py`
   - 用于 session resume summary。
   - 存在 session JSON 里。
   - 关注当前目标、blocker、next step、key files、runtime identity。
   - 主要回答“下次继续时，从哪里接上？”

2. `.pico/checkpoints/*`
   - 由 `CheckpointStore` 管理。
   - 存 checkpoint records、tool changes、blobs。
   - 关注文件 before/after 状态。
   - 主要回答“文件改动能不能恢复？”

这两者不能混。

面试时必须强调：

> resume checkpoint 解决继续任务，recovery checkpoint 解决恢复文件。它们名字相近，但数据结构、存储位置和语义都不同。

### 2.5 关键设计：delegate 是只读子 agent

`Pico.spawn_delegate()` 会创建一个 child `Pico`：

- `approval_policy="never"`
- `read_only=True`
- `max_steps` 更小
- `depth + 1`
- 共享 model client、workspace、session_store、run_store

这说明 delegate 不是“放权给另一个 agent 改仓库”，而是：

> 受限调查能力。

为什么？

因为如果 delegate 也能写文件，就会出现多 agent 并发修改、归因困难、恢复边界复杂的问题。Pico 第一阶段选择把 delegate 限制为 read-only，这非常合理。

### 2.6 面试怎么讲

可以这样讲：

> Pico runtime 是一个 composition root。它不是直接做所有事情，而是负责把模型、工作区、session、run artifacts、checkpoint store、memory、repo map、tool executor 和 recovery manager 连接起来。这样设计的好处是所有子系统共享同一个 workspace root 和 redaction policy，工具执行产生的 metadata 能直接进入 checkpoint system，context 构造也能访问 memory index 和 repo map。

### 2.7 边界和不足

当前 `Pico.__init__()` 确实偏大，因为它承担了太多装配职责。后续可以拆：

- `RuntimeConfig`
- `StoreBundle`
- `MemoryBundle`
- `RecoveryBundle`
- `ToolingBundle`

但现在不一定要立刻拆，因为它作为 composition root 仍然是合理的。需要注意的是：不要把业务逻辑继续塞进 `Pico`，否则 runtime facade 会越来越重。

## 3. AgentLoop：一次请求的状态机，而不是简单 while loop

### 3.1 这个 part 解决什么问题

普通 LLM 调用是：

```text
prompt -> model -> answer
```

普通 tool agent 是：

```text
prompt -> model -> tool -> model -> answer
```

但 coding agent 需要更多状态：

- 现在是第几轮 model attempt？
- 工具调用了几次？
- 有没有达到 step limit？
- 模型输出格式错了怎么办？
- 工具被拒绝算不算 step？
- 工具改了文件，checkpoint 什么时候写？
- 测试命令的验证证据怎么挂到 checkpoint？
- 最终 report 什么时候写？

`AgentLoop` 解决的就是：把一次用户请求变成可持久化、可复盘、可终止的状态机。

### 3.2 做了什么

`pico/agent_loop.py` 的 `AgentLoop.run()` 承担主循环。

它做了这些事：

1. 把用户请求写入 working memory 的 task summary。
2. 把用户消息写入 session history。
3. 创建 `TaskState`。
4. 创建 `.pico/runs/<run_id>/`。
5. 写 `run_started` trace。
6. 进入 model/tool 循环。
7. 每轮先写 attempts。
8. 构造 prompt 和 prompt metadata。
9. 写 `prompt_built` trace。
10. 根据 prompt metadata 判断是否要创建 resume checkpoint。
11. 写 `model_requested` trace。
12. 调 model client。
13. 解析模型输出。
14. 写 `model_parsed` 和聚合的 `model_turn` trace。
15. 如果是工具调用，写 `tool_started`，执行工具，写 `tool_executed` 和 `tool_finished`。
16. 收集 `tool_change_id`。
17. 如果 shell 命令是验证命令，收集 verification evidence。
18. 如果是 final，完成 task state。
19. 结束时写 resume checkpoint。
20. 如果有 tool changes，写 recovery turn checkpoint。
21. 把 pending verification evidence 挂到 recovery checkpoint。
22. 写 `run_finished` trace。
23. 写 report。

### 3.3 TaskState 的作用

`TaskState` 是运行状态快照，不是完整日志。

字段包括：

- `run_id`
- `task_id`
- `user_request`
- `status`
- `tool_steps`
- `attempts`
- `last_tool`
- `stop_reason`
- `final_answer`
- `checkpoint_id`
- `resume_status`
- `recovery_checkpoint_id`

重要区别：

- `attempts` 是模型调用轮数。
- `tool_steps` 是真正进入执行阶段的工具调用次数。
- rejected tool 不会增加 `tool_steps`。
- `checkpoint_id` 是 session resume summary checkpoint。
- `recovery_checkpoint_id` 是 file recovery checkpoint。

这正是 Pico 有工程感的地方：它没有把“运行状态”和“恢复状态”混成一个字段。

### 3.4 为什么要有 max_attempts

`AgentLoop` 有两个限制：

- `tool_steps < max_steps`
- `attempts < max_attempts`

`max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)`

这是为了防止模型反复输出 malformed response 或 retry，但没有真正执行工具。只限制 tool_steps 不够，因为模型可能一直输出无法解析的内容，导致无限循环。

所以：

- `max_steps` 控制实际工具行动次数。
- `max_attempts` 控制模型回合总数。

### 3.5 为什么要有 model_turn trace

代码里不仅写：

- `prompt_built`
- `model_requested`
- `model_parsed`

还额外写了聚合事件：

- `model_turn`

原因是 trace 是给后续复盘、benchmark 和 replay 看的。单独事件粒度细，但下游每次都要把三条事件拼起来很麻烦。`model_turn` 把一轮 model call 的核心信息压成一条逻辑事件：

- attempt number
- kind
- duration
- prompt cache key
- prompt metadata
- completion metadata

这就是审计系统里的“原子事件”和“逻辑事件”并存。

### 3.6 为什么 run 结束才创建 turn recovery checkpoint

工具执行时会生成 Tool Change Record。一次 run 可能有多个 tool changes。

`AgentLoop` 不会每执行一次工具就生成用户可见 checkpoint，而是在 `_finish_run()` 里调用 `_finalize_recovery_checkpoint()`，把本次 run 的 tool_change_ids 汇总成一个 turn checkpoint。

原因：

- 用户关心“一次请求的改动”，不是“第几个工具的改动”。
- 内部保留 tool 粒度，外部暴露 turn 粒度。
- 多文件修改可以作为一次整体变更被 preview/restore。

### 3.7 Verification evidence 怎么接进来

`AgentLoop` 会识别 `run_shell` 是否是验证命令：

- `pytest`
- `ruff`
- `mypy`
- `pyright`
- `npm test`
- `pnpm test`
- `cargo test`
- `go test`

如果是，就解析 `run_shell` 的结果：

- command
- risk_class
- exit_code
- stdout
- stderr

这些不会立刻挂到 checkpoint，因为当时 recovery checkpoint 可能还没创建。它会先放在 `run_verification_evidence`，等 `_finish_run()` 创建 recovery checkpoint 后，再调用 `record_verification_evidence()` 写进去。

这解决了一个顺序问题：

> 验证命令发生在 run 中间，但它要挂到 run 结束时生成的 turn checkpoint 上。

### 3.8 面试怎么讲

可以这样讲：

> AgentLoop 是 Pico 的状态机。每次用户请求都会创建 TaskState 和 run directory；每一轮模型调用都会构造 bounded prompt、写 trace、调用 provider、解析结果；如果模型申请工具，必须走 ToolExecutor；如果工具产生 tool_change_id，就在 run 结束时汇总成 recovery checkpoint。这样一次 agent 工作不是一串临时输出，而是有 task state、trace、report、checkpoint 和 verification evidence 的完整执行记录。

### 3.9 边界和不足

AgentLoop 当前是同步循环，不是并发调度器。

它能处理：

- 单用户请求。
- 多轮 tool/model loop。
- delegate 只读调查。
- stop reason。
- run artifacts。

它不处理：

- 多任务并行调度。
- 长任务队列。
- 分布式执行。
- 中途用户实时打断后的复杂恢复。

这符合 Pico 的本地 CLI agent 定位。

## 4. ContextManager：输入治理，而不是 prompt 拼接

### 4.1 这个 part 解决什么问题

大模型最核心的问题是：它只能基于 prompt 行动。

coding agent 的 prompt 如果设计不好，会出现：

- 看不到项目约定。
- 看不到最近工具结果。
- 历史太长导致当前请求被冲掉。
- memory 全塞导致旧信息污染。
- prompt 超预算时随机截断。
- stable prefix 经常变化，prompt cache 没意义。

ContextManager 解决的是：

> 每轮模型调用时，如何在有限预算内构造一份优先级明确、可压缩、可审计的输入。

### 4.2 做了什么

`pico/context_manager.py` 将 prompt 拆成三段：

```text
prefix
history
current_request
```

其中：

`prefix` 包含：

- base prompt prefix
- memory usage guidance
- memory reading guidance
- project_structure
- memory_index

`history` 包含：

- volatile workspace state
- checkpoint text
- transcript

`current_request` 包含：

- 当前用户请求

### 4.3 为什么 prefix / history / current_request 这样分

这三个部分的变化频率和重要性不同。

`prefix` 是稳定规则：

- agent 身份。
- 工具调用格式。
- 工具列表。
- stable workspace facts。
- memory index。
- project structure。

`history` 是运行过程状态：

- 当前 branch/status/recent commits。
- resume checkpoint text。
- session history。

`current_request` 是本轮目标：

- 不允许裁剪。
- 放在最后，让模型最靠近当前任务。

这个分层的本质是：

> 把稳定内容、易变内容和本轮目标分开治理。

### 4.4 预算策略

默认预算：

- total budget：15000 chars
- prefix：7000 chars
- history：8000 chars
- prefix floor：1200 chars
- history floor：1500 chars
- reduction order：先 history，再 prefix

当前请求没有预算字段，因为它不被裁剪。

如果 prompt 超预算：

1. 计算 overflow。
2. 先尝试压缩 history。
3. history 到 floor 后再压 prefix。
4. 仍超预算时保留 current_request，metadata 标出 over budget。

为什么先压 history？

因为 history 包含很多可压缩内容，例如旧工具结果、旧聊天、重复 read_file。prefix 里有系统规则和工具说明，过度裁剪 prefix 会影响模型遵守协议。

### 4.5 stable prefix 和 volatile workspace state 的区别

`WorkspaceContext.stable_text()` 进入 prefix：

- cwd
- repo_root
- default_branch
- project_docs

`WorkspaceContext.volatile_text()` 进入 history：

- current branch
- git status
- recent commits

为什么这样拆？

因为 branch/status/recent commits 经常变化。如果它们进入 stable prefix，prompt cache key 会频繁变化，稳定前缀就不稳定。

Pico 的设计是：

> stable prefix 尽可能稳定，volatile state 放到 history head。

### 4.6 history 怎么压缩

ContextManager 不是简单从头砍掉历史。它做了几件事：

1. 最近 6 条 history 作为 recent window，优先保留。
2. 旧的重复 `read_file` 会折叠。
3. 旧的 read_file 如果有 file summary，就用 summary 替代原始工具输出。
4. 旧的 tool 输出会压缩成一行摘要。
5. `run_shell` 会总结命令和前三行非空输出。
6. 最近记录如果超预算，会进行 tail clip。

这说明 Pico 的上下文压缩是有语义偏好的：

- 最近的比旧的更重要。
- 文件摘要比旧全文更划算。
- 工具结果可压缩，但用户当前请求不可压缩。

### 4.7 memory_index 为什么在 prefix

`MemoryRefresher` 会生成：

```xml
<memory_index>
Notes (user-written, read-only for agent):
- workspace/notes/auth.md (123 chars)
Agent records:
- workspace/agent_notes.md (456 chars)
Use memory_search / memory_read to access.
</memory_index>
```

注意，它不是 memory 内容本身，而是索引。

为什么？

- 控制 prompt 体积。
- 避免旧 memory 直接影响模型。
- 让模型按需读。
- 保持 memory 使用可审计，因为具体读取会留下 tool trace。

### 4.8 project_structure 为什么在 prefix

`project_structure` 来自 RepoMap 的 top-level tree 和 language stats。

它不是完整符号索引，而是项目的低成本导航：

```xml
<project_structure languages="python=..., markdown=...">
pico/  (... files)
tests/ (... files)
</project_structure>
```

具体符号通过 `repo_lookup` 查。

这是典型的“索引进 prompt，详情走工具”。

### 4.9 prompt metadata 的价值

`ContextManager._metadata()` 会记录：

- prompt chars
- total budget
- over budget
- section order
- section budgets
- raw/rendered chars
- budget reductions
- history compression stats
- current request chars
- stable prefix hash
- prompt cache key

这些 metadata 后续会进入 trace/report。

这意味着如果一次 agent 表现不好，可以回头看：

- prompt 是否超预算？
- history 被压了多少？
- prefix 是否变化？
- current request 是否完整？
- memory index 是否进入 prompt？
- prompt cache key 是否变化？

这就是输入可审计。

### 4.10 面试怎么讲

可以这样讲：

> ContextManager 不是把字符串拼起来，而是做输入治理。Pico 把 prompt 分成 prefix、history、current_request。prefix 放稳定规则、工具说明、memory index 和 project structure；history 放 volatile workspace state、checkpoint text 和会话记录；current request 永远不裁剪。预算不足时先压 history，再压 prefix。旧 history 会折叠重复 read_file、复用 file summary、压缩旧 tool 输出。这样模型每轮看到的内容既有项目导航，又不会被旧上下文淹没。

### 4.11 边界和不足

ContextManager 控制输入结构，但不保证模型理解正确。

当前没有：

- 真正语义级 context condenser。
- 基于任务 intent 的动态文件选择。
- embedding-based code retrieval。
- 多文件依赖图自动注入。

它当前的强项是：

- 规则清晰。
- 行为可解释。
- budget 可追踪。
- 不依赖外部服务。

## 5. Memory：可审计知识层，而不是神秘长期记忆

### 5.1 这个 part 解决什么问题

如果没有 memory，agent 每个 session 都像第一次进入项目：

- 不知道项目规则。
- 不知道用户偏好。
- 不知道之前踩过的环境坑。
- 不知道哪些设计决策已经确定。

但如果 memory 做得太激进，又会有问题：

- 旧信息污染 prompt。
- agent 随便写长期记忆。
- 用户手写 notes 被模型改坏。
- 临时工具结果变成长期知识。
- memory 来源无法审计。

Pico 的 memory 设计是克制的：

> memory 是可审计的项目知识来源，不是自动脑补的长期记忆。

### 5.2 当前实现有两层 memory

第一层：session 内 working memory。

位置：

- session JSON 的 `working_memory`
- session JSON 的 `memory.file_summaries`

用途：

- 当前任务摘要。
- 最近接触文件。
- 文件短摘要。
- 让下一轮 prompt 不必带完整旧工具输出。

第二层：v2 durable memory。

位置：

- `<repo>/.pico/memory/notes/**/*.md`
- `<repo>/.pico/memory/agent_notes.md`
- `~/.pico/memory/notes/**/*.md`
- `~/.pico/memory/agent_notes.md`
- `<repo>/AGENTS.md`
- `~/.pico/AGENTS.md`

用途：

- 跨 session 的项目约定、用户 notes、agent 记录。

### 5.3 WorkingMemory 怎么工作

`WorkingMemory` 只有两个核心字段：

- `task_summary`
- `recent_files`

限制：

- task summary 300 chars。
- recent files 最多 8 个。

工具执行后，runtime 会调用 `update_memory_after_tool()`：

- `read_file`：记住文件，并生成文件摘要。
- `write_file` / `patch_file`：记住文件，并让旧文件摘要失效。

为什么写文件后要失效摘要？

因为旧 summary 对应的是旧文件内容。文件被改后，如果继续使用旧 summary，会误导模型。

### 5.4 file_summaries 的价值

旧的 read_file 工具结果可能很长。ContextManager 在压缩 history 时，如果发现旧 read_file 有 summary，就可以用：

```text
sample.txt -> alpha | beta
```

替代完整输出。

这是一种轻量 context condensation。

它不是自动理解整个文件，而是：

- 从已读内容中提取短摘要。
- 带 freshness hash。
- 文件变更后失效。

### 5.5 Durable memory 三层来源

`docs/memory-model.md` 明确分三类：

1. `AGENTS.md`
   - 项目约定。
   - session start 时读。
   - Pico 读 AGENTS.md，不读 CLAUDE.md。

2. User notes
   - `.pico/memory/notes/**/*.md`
   - 用户手写。
   - agent 可以读，但不能写。

3. Agent notes
   - `.pico/memory/agent_notes.md`
   - 只有用户明确要求 remember/save 时才追加。
   - append-only。
   - 单条 <= 500 chars。
   - soft limit 8000 chars。

### 5.6 为什么 user notes 只读

因为 user notes 是用户拥有的知识。

如果 agent 可以随便 patch `.pico/memory/notes/**`，就会有风险：

- agent 覆盖用户手写规则。
- agent 把错误理解写进权威 notes。
- approval auto 时无法阻止误写。

所以 `write_file` / `patch_file` 在路径层拒绝 `.pico/memory/notes/**`。

这不是靠 approval，而是工具本身拒绝。这点很重要，因为 approval auto 可能绕过用户确认。

### 5.7 memory_save 为什么不是 risky

`memory_save` 在工具表里是 read_only effect class，但它确实写 `agent_notes.md`。

为什么这样可以接受？

因为它不写 workspace source code，不参与 recoverable editing 的文件恢复体系；它是 memory subsystem 的受限 append 操作：

- 只能追加 agent_notes。
- 单条长度有限。
- scope 只能 workspace/user。
- 不能写 user notes。
- prompt guidance 要求只有用户明确要求才使用。

所以它属于“受限 memory append”，不是通用 workspace write。

但面试里要诚实说：

> 从严格副作用角度看，memory_save 也会改磁盘，只是它不属于代码工作区恢复边界，而属于 memory append 边界。

### 5.8 Retrieval 怎么工作

`Retrieval` 使用 BM25 + CJK bigram。

分词：

- 英文/数字/下划线：正则词。
- 中文：按 CJK 字符生成 bigram。

搜索：

1. 读取所有 memory files。
2. tokenization。
3. 计算 DF。
4. 用 BM25 计算得分。
5. 返回 path、score、snippets。

优点：

- stdlib-only。
- 本地运行。
- 可解释。
- 对路径、错误信息、符号名、命令很有效。

缺点：

- 不理解语义同义词。
- `身份认证` 不会自动匹配 `auth`。
- 不会自动归纳和遗忘。

### 5.9 MemoryRefresher 为什么存在

ContextManager 每轮 prompt 都需要 memory index 和 project structure，但不应该每次都无脑重建。

`MemoryRefresher.refresh_if_stale()` 做两件事：

- 比较 memory files 的 mtime snapshot，变了才重渲染 memory index。
- 调用 repo_map.refresh_if_stale()，project structure 变化才重渲染。

它保证底层没变化时输出 byte-identical。

这个细节服务于 prompt cache：如果文本没变，stable prefix hash 才稳定。

### 5.10 CLI memory surface

`pico-cli memory` 暴露给用户：

- `list`
- `show <path>`
- `search <query>`
- `review`
- `migrate [--apply]`

这说明 memory 不是黑盒。用户可以：

- 看有哪些 memory。
- 读 memory。
- 搜 memory。
- review agent_notes。
- 从 legacy topics 迁移到 notes。

### 5.11 面试怎么讲

可以这样讲：

> Pico 的 memory 分两层：session 内 working memory 和跨 session durable memory。working memory 只保存当前任务摘要、最近文件和文件摘要，用于压缩 history；durable memory 分 AGENTS.md、user notes 和 agent_notes。user notes 只读，agent_notes 只能在用户明确要求时 append。Context 里只放 memory index，不直接塞全文；需要时模型通过 memory_search/memory_read 访问。检索用 BM25 + CJK bigram，优点是本地可解释，边界是语义能力有限。

### 5.12 边界和不足

当前 memory 不是完整长期记忆系统。

缺少：

- 语义检索。
- 自动 consolidation。
- memory provenance 的行级引用。
- stale memory 自动检测。
- secret leakage 专项评估。
- 用户可视化编辑界面。

但当前设计已经有一个正确方向：

> durable source 是文件，检索/index 是派生能力。

这比把所有东西塞进一个不可审计的向量库更适合本地 coding agent 第一阶段。

## 6. RepoMap：让符号查询按需发生

### 6.1 这个 part 解决什么问题

模型在代码仓库里经常需要回答：

- 某个 class 在哪？
- 某个 function 在哪？
- 这个 symbol 有几个定义？

如果只用 grep：

- 结果噪声大。
- 可能搜到注释或字符串。
- Python class/function 可以更精确。

如果把完整符号表放进 prompt：

- prompt 变大。
- 大部分符号与任务无关。
- repo 变化会频繁污染 stable prefix。

所以 Pico 的设计是：

> 顶层结构进入 prompt，具体符号通过 repo_lookup 工具按需查。

### 6.2 做了什么

`pico/repo_map.py` 提供：

- Python AST 符号提取。
- TS/JS/Go/Rust 正则 best-effort。
- top-level tree。
- language stats。
- `repo_lookup` tool runner。

### 6.3 怎么做的

扫描逻辑：

```text
RepoMap.scan()
  -> _walk()
      -> 跳过 .git/.pico/.venv/node_modules/dist/build 等目录
      -> 跳过大于 500KB 的文件
      -> 最多 10000 个文件
  -> _index_file()
      -> 根据扩展名判断语言
      -> Python 用 ast
      -> TS/JS/Go/Rust 用 regex
      -> 记录 Symbol(name, file, line, kind)
```

刷新逻辑：

```text
refresh_if_stale()
  -> 遍历当前文件 mtime
  -> 找 stale / dead
  -> 删除旧索引
  -> 只重建变化文件
  -> 最后 recount top-level/language stats
```

### 6.4 为什么 Python 用 AST，其他用 regex

Python stdlib 自带 AST，解析准确，成本低。

其他语言如果引入专门 parser，会引入依赖和复杂度。Pico 当前 `pyproject.toml` 没有 runtime dependencies，所以 regex 是一个轻量取舍。

这符合项目当前定位：

- Python 精确。
- 多语言 best-effort。
- 不承诺 LSP 级能力。

### 6.5 面试怎么讲

可以这样讲：

> RepoMap 是 Pico 的按需代码导航层。它不会把完整符号表塞进 prompt，而是只把 top-level project structure 放进 Context，具体 symbol 用 repo_lookup 查。Python 用 AST 提取 class/function/method，TS/JS/Go/Rust 用正则 best-effort。这样既降低 prompt 负担，也让符号查询比普通 grep 更精确。

### 6.6 边界和不足

RepoMap 不是 LSP。

它不做：

- type resolution。
- references。
- import graph。
- rename safety。
- semantic dependency graph。

它做的是：

- 快速定位定义。
- 给模型低成本导航。
- 避免大符号表污染 prompt。

## 7. Tool Registry：模型能力白名单

### 7.1 这个 part 解决什么问题

模型输出本质是文本。如果让模型自由描述“我要改文件、跑命令”，runtime 就无法可靠执行和审计。

Pico 把模型行动限制为显式工具：

- 工具名固定。
- 参数 schema 固定。
- risky 属性固定。
- runner 固定。

这样模型的行动从自然语言变成结构化请求。

### 7.2 工具有哪些

基础工具：

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

可选 delegate：

- `delegate`

`delegate` 只有在 `context.depth < context.max_depth` 时才暴露。

### 7.3 工具 schema 的作用

每个工具有 schema，例如：

- `read_file(path, start=1, end=200)`
- `run_shell(command, timeout=60)`
- `patch_file(path, old_text, new_text)`
- `memory_search(query, limit=5)`
- `repo_lookup(symbol, kind="")`

schema 不是完整 JSON Schema，而是给 prompt 和 validate_tool 提供约束。

模型看到工具说明后，必须输出：

```xml
<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>
```

或者 XML-style file edit：

```xml
<tool name="patch_file" path="file.py">
  <old_text>...</old_text>
  <new_text>...</new_text>
</tool>
```

### 7.4 validate_tool 的关键约束

`validate_tool()` 会做工具级校验：

- `list_files`：path 必须是目录。
- `read_file`：path 必须是文件，line range 合法。
- `search`：pattern 非空。
- `run_shell`：command 非空，timeout 在 1 到 120。
- `write_file`：目标不能是目录，必须有 content。
- `patch_file`：目标必须是文件，old_text 非空，new_text 存在，old_text 必须精确出现一次。
- `delegate`：task 非空，depth 未超限。
- `memory_read`：路径格式合法，不允许 `..` 或绝对路径。
- `memory_search`：query 非空，长度 <= 512，limit 1-20。
- `memory_save`：note 非空，长度 <= 500，scope 是 workspace/user。
- `repo_lookup`：symbol 是合法 identifier，kind 限定为 class/function/method。

### 7.5 patch_file 为什么要求 old_text 精确出现一次

这是一个很重要的设计点。

如果 old_text 出现 0 次：

- 说明模型基于旧上下文或猜错内容。

如果 old_text 出现多次：

- 不知道该改哪一处。

Pico 选择直接失败，而不是猜。这让修改行为确定，也让错误原因更清楚。

这是 coding agent 的关键原则：

> 宁可让模型再读文件，也不要让它模糊修改。

### 7.6 run_shell 为什么有 timeout

`run_shell` 默认 timeout 60 秒，上限 120 秒。

原因：

- 防止模型跑长时间命令卡死。
- 让 agent loop 有明确进度边界。
- 避免本地 CLI 被无界命令占住。

### 7.7 shell_env 为什么过滤环境变量

`tool_run_shell()` 不直接继承父进程完整环境，而是调用 `context.shell_env()`。

默认 allowlist：

- HOME
- LANG
- LC_ALL
- LOGNAME
- PATH
- PWD
- SHELL
- TERM
- TMPDIR
- USER

这样可以减少 API key、token、secret 被命令意外读到或输出到 trace 的风险。

### 7.8 面试怎么讲

可以这样讲：

> Tool Registry 是模型行动的能力白名单。Pico 没有让模型直接执行任意函数，而是把工具名、参数 schema、risky 标记和 runner 显式注册。每个工具在执行前都要 validate，比如 patch_file 要求 old_text 精确出现一次，run_shell 有 timeout 限制，memory_read 有路径安全限制。这样模型的行动可以被校验、拒绝、记录和恢复。

## 8. ToolExecutor：系统咽喉，真正的行动边界

### 8.1 这个 part 解决什么问题

Tool registry 定义能力，但真正危险的是执行。

ToolExecutor 要解决：

- 模型申请的工具是否允许？
- 工具名是否存在？
- 参数是否合法？
- 是否重复调用？
- shell 命令风险如何？
- 是否需要 approval？
- 工具是否会改 workspace？
- 改了哪些文件？
- 是否需要写 Tool Change Record？
- 工具失败但已经改文件怎么办？
- metadata 怎么传给 trace/report/recovery？

所以 ToolExecutor 是 Pico 的系统咽喉。

### 8.2 执行链路

`ToolExecutor.execute(name, args)` 的流程：

```text
1. allowed_tools 检查
2. tools registry 查工具
3. validate_tool
4. repeated_tool_call 检查
5. run_shell command_risk_class
6. evaluate_command_approval
7. risky tool approval
8. 判断 effect_class
9. workspace_write 工具创建 pending Tool Change Record
10. 捕获 before snapshot / before blob
11. 对 run_shell 捕获 observer_before
12. 执行 tool runner
13. 捕获 after snapshot / observer_after
14. 计算 affected_paths / diff_summary
15. 判断 tool_status
16. 更新 working memory
17. 构造 file_entries
18. finalize Tool Change Record
19. 返回 ToolExecutionResult(content, metadata)
```

### 8.3 metadata 里有什么

ToolExecutor 返回的不只是文本结果，还有 metadata：

- `tool_status`
- `tool_error_code`
- `security_event_type`
- `risk_level`
- `read_only`
- `affected_paths`
- `workspace_changed`
- `workspace_fingerprint`
- `diff_summary`
- `command_risk_class`
- `command_approval`
- `tool_change_id`
- `file_entries`
- `shell_side_effects`

这让工具执行结果进入三条链路：

- 模型下一轮通过 content 继续推理。
- trace/report 通过 metadata 审计。
- recovery 通过 tool_change_id/file_entries 恢复。

### 8.4 allowed_tools 的意义

`allowed_tools` 可以限制当前 run 暴露哪些工具。

如果 name 不在 allowed_tools：

- 直接 rejected。
- `tool_error_code="tool_not_allowed"`。
- 不进入执行。

这对于只读分析、测试、受限 agent 很重要。

### 8.5 repeated_tool_call 为什么要拦

LLM 常见问题是工具循环：

```text
read_file A
read_file A
read_file A
```

或者短窗口拉锯：

```text
A -> B -> A -> B -> A
```

Pico 在最近 6 个 tool events 里统计相同 name+args，如果重复 >= 2，就拒绝。

这不是安全问题，而是 agent 控制问题：

> 如果同一个工具调用没有带来新信息，就应该换策略或结束。

### 8.6 effect class 怎么决定

ToolExecutor 有 `_EFFECT_CLASS_BY_TOOL`：

- read-only：`read_file`、`list_files`、`search`、memory read/search/list、repo_lookup。
- workspace_write：`run_shell`、`write_file`、`patch_file`、`delegate`。

effect class 决定是否记录 recovery。

如果不是 read_only，就创建 Tool Change Record。

### 8.7 pending Tool Change Record 为什么重要

ToolExecutor 在真正执行工具之前创建 pending record。

原因是工具可能：

- 成功。
- 失败。
- 部分成功。
- 抛异常。
- 被中断。

如果只在成功后记录，就会漏掉“失败但已经改文件”的情况。

pending record 的不变量是：

> 只要工具进入执行阶段，最终必须 finalized/error/partial_success/interrupted。

这让 recovery 系统不会丢掉异常路径。

### 8.8 直接文件工具怎么捕获 before/after

对 `write_file` / `patch_file`，Pico 能从参数里知道目标路径。

执行前：

- `_direct_tool_candidate_paths()` 提取 path。
- `_capture_path_snapshot()` 记录 before hash。
- `_capture_before_file_states_for_paths()` 对 eligible 文件存 before blob。

执行后：

- 再 capture path snapshot。
- diff before/after。
- 得到 affected_paths。
- `_build_file_entries()` 写 after blob/hash/expected_current_hash。

这种方式很窄，只看工具声明的 path，不扫全仓。

### 8.9 run_shell 为什么特殊

`run_shell` 可以改任何文件，参数里没有明确 affected path。

所以 ToolExecutor 对 run_shell 做：

- 执行前 `WorkspaceObserver.capture()`。
- 执行后再 capture。
- `WorkspaceObserver.diff()` 得到 changed_paths。
- 对 clean tracked file 尝试 `git show HEAD:path` 取 before blob。
- 对 dirty-before 文件标记为不可自动恢复。
- 生成 shell_side_effects。

这样 run_shell 的副作用也可以进入 recovery 体系。

### 8.10 partial_success 的意义

如果 `run_shell` exit_code 非 0，但 workspace_changed=True：

- tool_status = `partial_success`
- tool_error_code = `tool_partial_success`

这比简单标记 error 更准确。

比如脚本跑失败，但已经生成或修改了文件。模型下一轮看到的是失败，recovery 看到的是副作用。这两个事实都要保留。

### 8.11 面试怎么讲

可以这样讲：

> ToolExecutor 是所有模型行动的总闸口。它不仅执行工具，还负责 allowed tools、参数校验、重复调用拦截、command risk、approval、workspace side-effect 捕获和 Tool Change Record 生命周期。对于写文件工具，它在执行前捕获 before state，执行后捕获 after state；对于 run_shell，它用 WorkspaceObserver 前后比较实际变更。工具失败但产生副作用会变成 partial_success，仍然进入 recovery metadata。

### 8.12 边界和不足

ToolExecutor 当前职责偏重：

- validation。
- approval。
- execution。
- side-effect detection。
- recovery record。
- metadata assembly。

后续可以拆：

- `ToolInvocationValidator`
- `ApprovalPolicy`
- `SideEffectRecorder`
- `ToolChangeBuilder`

但这些拆分必须在行为稳定后做，否则容易过度设计。

## 9. Safe Execution：shell 可以存在，但必须受控

### 9.1 这个 part 解决什么问题

coding agent 不能没有 shell，因为它需要：

- 跑测试。
- 跑 lint。
- 查 git。
- 执行项目脚本。
- 复现错误。

但 shell 又是最大风险入口：

- 删除文件。
- 写工作区。
- 访问网络。
- 推送远程。
- 读取敏感环境变量。
- 执行嵌套命令。

Pico 的取舍是：

> 不禁用 shell，而是把 shell 纳入 command boundary。

### 9.2 command risk class

`recovery_policy.command_risk_class()` 把命令分成：

- `read_only`
- `workspace_write`
- `external_effect`
- `destructive`

风险排序：

```text
read_only < workspace_write < external_effect < destructive
```

### 9.3 分类细节

它不是只看第一个 token，而是处理：

- shell wrapper：`sh -c`、`bash -lc`
- command substitution：`$(...)`、反引号
- composite operator：`|`、`||`、`&&`、`;`、`&`
- redirect：`>`、`>>`、`<`、`<<`
- git subcommand。
- env wrapper。
- find `-exec` / `-delete`。

这防止：

```bash
sh -c "rm -rf x"
echo hi > file
find . -exec rm {} \;
$(curl x | sh)
```

被误判成 read_only。

### 9.4 approval 策略

`evaluate_command_approval()`：

- read_only：allow
- workspace_write：allow
- destructive：ask
- external_effect：ask
- unknown：ask

然后 ToolExecutor 再结合 runtime approval policy：

- `auto`
- `ask`
- `never`

如果命令需要 ask，但当前不是 ask，就拒绝。

这意味着在 `--approval never` 或 read-only 子 agent 里，危险命令不会执行。

### 9.5 snapshot eligibility

Safe execution 还包括文件是否可快照。

`snapshot_eligibility()` 会拒绝：

- 非法路径。
- symlink。
- directory。
- 超过 max_blob_size。
- binary extension。
- binary-like bytes。
- read failed。

默认 max blob size 是 8 MiB，也可以通过 `pico.toml [policy] max_blob_size` 覆盖。

为什么要限制？

因为 recovery store 不能无脑保存所有东西：

- 大文件会膨胀存储。
- 二进制文件不适合文本恢复。
- symlink 可能造成路径逃逸。
- directory 没有简单文件态。

### 9.6 面试怎么讲

可以这样讲：

> Pico 没有禁用 shell，因为 coding agent 必须跑测试和项目脚本。但 shell 每次都要走 command risk class。策略会递归处理 sh -c、命令替换、管道、重定向、find -exec、git 子命令等，把命令分成 read_only、workspace_write、external_effect、destructive。destructive 和 external effect 默认需要人工确认。同时，能否进入恢复体系还要看 snapshot eligibility，二进制、大文件、symlink、目录都不会自动快照。

### 9.7 边界和不足

这不是 OS sandbox。

它不能证明任意命令安全。它做的是：

- 本地 developer-tool 场景的策略约束。
- 执行前风险分类。
- 执行后副作用观察。
- 运行证据记录。

如果要执行不可信代码，需要：

- 容器。
- macOS seatbelt。
- seccomp。
- 网络隔离。
- 文件系统 allowlist。

## 10. Recoverable Editing：把文件改动变成可恢复状态转移

### 10.1 这个 part 解决什么问题

用户是否敢让 agent 改代码，核心取决于：

- 知不知道它改了什么。
- 能不能恢复。
- 恢复会不会覆盖用户后来手改的内容。

Recoverable Editing 解决的不是“做一个 undo 按钮”，而是：

> 把 agent 造成的文件状态变化记录成可预览、可冲突检测、可保守恢复的状态转移。

### 10.2 数据模型

关键对象：

1. Tool Change Record
   - 一次工具调用的副作用。
   - pending -> finalized/error/partial_success/interrupted。

2. Checkpoint Record
   - 用户可见恢复点。
   - 类型：turn / restore。

3. File-State Blob
   - 原始文件字节。
   - sha256 content-addressed。

4. File Entry
   - 一个文件在一次工具/turn 中的变化。
   - 记录 path、change_kind、before/after blob/hash、expected_current_hash。

5. Restore Plan
   - preview_restore 生成的恢复决策。

6. Restore Checkpoint
   - apply_restore 之后写的新 checkpoint。

### 10.3 Tool Change Record 记录什么

字段包括：

- `tool_change_id`
- `checkpoint_id`
- `turn_id`
- `owner_id`
- `tool_name`
- `effect_class`
- `status`
- `started_at`
- `ended_at`
- `input_summary`
- `affected_paths`
- `file_entries`
- `shell_side_effects`
- `approval`
- `error`
- `trace_event_ids`

它是内部粒度，不一定直接面向用户。

### 10.4 Turn Checkpoint 记录什么

`RecoveryCheckpointWriter.create_turn_checkpoint()`：

- 创建 `checkpoint_type="turn"`。
- 加载所有 tool_change_ids。
- 汇总 file_entries。
- 记录 missing_tool_change_ids。
- 反写 tool_change 的 checkpoint_id。

这说明：

- Tool Change 是工具粒度。
- Turn Checkpoint 是用户请求粒度。

### 10.5 File Entry 细节

`_build_file_entries()` 里每条 entry 记录：

- `path`
- `change_kind`: created / modified / deleted
- `snapshot_eligible`
- `ineligible_reason`
- `content_kind`
- `before_blob_ref`
- `before_hash`
- `after_blob_ref`
- `after_hash`
- `expected_current_hash`

`expected_current_hash` 是恢复安全的核心。

它表示：

> agent 改完之后，文件应该是什么 hash。

恢复时只有当前文件 hash 仍等于 expected_current_hash，才说明用户没有在之后手动改过，可以自动恢复。

### 10.6 为什么存 blob，不只存 diff

diff 有问题：

- 依赖上下文。
- 文件后续变化后 patch 可能失败。
- patch 可能误应用到相似位置。
- 删除/创建场景复杂。

Pico 第一阶段用文件状态 blob：

- before blob 是恢复目标。
- after hash 是冲突判断目标。
- blob 按 sha256 去重。

恢复逻辑变简单：

```text
if current_hash == expected_current_hash:
    write before_blob
else:
    conflict
```

### 10.7 preview_restore 怎么决策

`RecoveryManager.preview_restore()` 对 checkpoint 的每个 file_entry 调 `_plan_entry()`。

结果有三类：

1. `restore`
   - snapshot eligible。
   - 当前 hash 和 expected hash 匹配。
   - 或创建文件场景中当前状态符合预期。

2. `review`
   - snapshot 不 eligible。
   - 缺 before blob。
   - path 无法解析。
   - 缺恢复所需信息。

3. `conflict`
   - 当前文件 hash 不等于 expected hash。
   - 当前文件缺失但 expected 表示应该存在。
   - unexpected file present。

preview 不改磁盘。

### 10.8 apply_restore 怎么做

`apply_restore()`：

1. 先调用 preview_restore。
2. 只处理 decision == restore 的 entry。
3. 检查 before blob 是否存在。
4. 记录 pre_restore_file_states。
5. 如果 before_blob_ref 存在，读取 blob。
6. 写入 sibling temp file。
7. 校验 temp hash。
8. atomic replace 目标文件。
9. 读回目标文件再次校验 hash。
10. 记录 post_restore_file_states。
11. 写 restore checkpoint。

这有两个安全点：

- 写目标前先验证 temp 文件 hash。
- replace 后再读回验证。

### 10.9 为什么恢复后要写 restore checkpoint

恢复本身也是一次文件变化。

如果恢复后不记录，历史就断了：

- 不知道什么时候恢复。
- 不知道恢复了哪些文件。
- 不知道跳过了哪些 entry。
- 不知道恢复前后 hash。

所以 Pico 写 `checkpoint_type="restore"`，并记录 restore provenance：

- source checkpoint id
- plan id
- applied_at
- restored_paths
- skipped_entries
- pre_restore_file_states
- post_restore_file_states

### 10.10 为什么不用 Git 作为恢复引擎

Git 不知道“哪些变化属于 agent”。

现实情况：

- 用户工作区可能本来 dirty。
- agent 可能改 untracked 文件。
- agent 可能创建新文件。
- `git checkout` 会影响不属于 agent 的修改。

Pico 恢复的是：

> agent 造成的状态转移。

不是：

> 回到某个 Git commit。

Git 在 Pico 里更多是 review context 和 shell side-effect before-state fallback，不是 restore engine。

### 10.11 CheckpointStore 为什么用 content-addressed blobs

`CheckpointStore.write_blob()`：

- 计算 sha256。
- blob_ref = content_hash。
- 存在 `.pico/checkpoints/blobs/<前两位>/<hash>`。
- 已存在就不重复写。
- 写入走 lock + temp + replace。

优点：

- 去重。
- hash 同时是内容身份。
- blob 引用容易校验。
- prune 时可以扫描引用。

### 10.12 prune 怎么保证不误删

`CheckpointStore.prune()` 会扫描引用：

- checkpoint record 的 file_entries。
- checkpoint record 的 restore_provenance pre/post states。
- tool change record 的 file_entries。

并且只删除不被保留 checkpoint/tool change 引用的 blob。

这说明 prune 也尊重 recovery graph，不是简单按时间删文件。

### 10.13 面试怎么讲

可以这样讲：

> Pico 的 recoverable editing 核心是记录 agent 造成的文件状态转移。每个 workspace write 工具先创建 pending Tool Change Record，执行前捕获 before state，执行后捕获 after state，生成 file_entries。一次用户请求结束时，多个 tool changes 汇总成 turn checkpoint。恢复前先 preview，每个文件判断 restore/review/conflict；只有当前 hash 仍等于 expected_current_hash 时才自动写回 before blob。恢复后还会写 restore checkpoint，保证恢复动作本身也可审计。

### 10.14 边界和不足

当前恢复是文件级，不是 hunk 级。

它不会：

- 自动 merge 冲突。
- 恢复 binary/large/symlink。
- 处理复杂 directory tree snapshot。
- 替代 Git。

这是保守设计。第一阶段优先保证：

- 不盲目覆盖。
- 可解释。
- 可验证。

## 11. RunStore / SessionStore / CheckpointStore：三个持久化边界

### 11.1 为什么要分三个 store

Pico 有三类状态：

1. session continuity
2. run audit artifacts
3. recovery truth

如果混在一起，会导致：

- session 文件过大。
- trace 被误用为恢复依据。
- checkpoint 变成聊天摘要。
- 用户无法独立查看 run 或 checkpoint。

所以 Pico 分三个 store：

- `SessionStore`
- `RunStore`
- `CheckpointStore`

### 11.2 SessionStore

路径：

```text
.pico/sessions/<session_id>.json
```

用途：

- conversation history。
- working memory。
- file summaries。
- resume summary checkpoints。
- runtime identity。
- current recovery checkpoint pointer。

写入：

- file lock。
- temp file。
- replace。

### 11.3 RunStore

路径：

```text
.pico/runs/<run_id>/
  task_state.json
  trace.jsonl
  report.json
```

用途：

- `task_state.json`：当前/最终状态快照。
- `trace.jsonl`：事件时间线。
- `report.json`：最终摘要。

写入细节：

- task_state/report 使用 atomic JSON write。
- trace 使用 append JSONL，因为事件是流式追加。
- 所有 artifact 走 redactor。

### 11.4 CheckpointStore

路径：

```text
.pico/checkpoints/
  records/
  tool_changes/
  blobs/
```

用途：

- 恢复真相。
- Tool Change Records。
- Checkpoint Records。
- File-state blobs。

### 11.5 一句话区分

```text
SessionStore 解决“对话怎么继续”
RunStore 解决“一次运行怎么复盘”
CheckpointStore 解决“文件改动怎么恢复”
```

### 11.6 为什么 trace 不能当 recovery truth

trace 可以说：

```text
tool patch_file 执行了
result: patched a.py
```

但恢复需要：

- before blob。
- after hash。
- expected current hash。
- snapshot eligibility。
- change kind。

这些不是 trace 的职责。

所以：

> 能复盘，不等于能恢复。

### 11.7 面试怎么讲

可以这样讲：

> Pico 把持久化分成三层。SessionStore 负责对话连续性，RunStore 负责单次 run 的审计工件，CheckpointStore 负责恢复真相。RunStore 的 trace 可以解释发生了什么，但不能作为恢复依据；真正恢复依赖 CheckpointStore 中的 tool changes、file_entries 和 blobs。这个分离避免了把日志、会话和恢复状态混在一起。

## 12. Provider Adapter：模型从哪里来，但不是系统核心

### 12.1 这个 part 解决什么问题

Pico 支持多个模型后端：

- Ollama
- OpenAI-compatible Responses
- Anthropic-compatible Messages
- DeepSeek Anthropic-compatible

不同 provider 差异很大：

- URL 不同。
- auth header 不同。
- payload 不同。
- response text 提取方式不同。
- streaming 不同。
- usage/cache metadata 不同。

但 runtime 不应该关心这些。

runtime 只需要：

```python
text = model_client.complete(prompt, max_new_tokens, ...)
```

### 12.2 做了什么

provider adapter 统一暴露：

- `complete(prompt, max_new_tokens, **kwargs)`
- `stream_complete(...)`
- `supports_prompt_cache`
- `last_completion_metadata`

### 12.3 OpenAI-compatible 细节

OpenAI-compatible：

- endpoint：`/responses`
- payload：`input` with `input_text`
- 支持 non-stream JSON。
- 兼容 SSE。
- 提取 `output_text`、`output[].content[].text`、`choices[].message.content`。
- 支持 prompt cache key/retention，但只在 base_url 包含 `openai.com` 或 `right.codes` 时打开。
- 从 response 中提取 usage/cache details。

### 12.4 Anthropic-compatible 细节

Anthropic-compatible：

- endpoint：`/messages`
- header：`x-api-key`、`anthropic-version`
- payload：`messages[].content[].text`
- 支持 cache_control。
- 提取 content blocks 中的 text。
- 如果只有 thinking blocks 且 max_tokens 耗尽，会提示增加 max_new_tokens。

DeepSeek 复用 Anthropic-compatible client，只是 base URL 和 env var 不同。

### 12.5 Ollama 细节

Ollama：

- endpoint：`/api/generate`
- payload：prompt、model、options。
- `think=False`
- 不支持 Pico 这里的 prompt cache 语义。

### 12.6 为什么 provider 不是最大亮点

Provider adapter 很重要，但它解决的是：

> 模型接口差异。

Pico 的核心解决的是：

> 模型接入之后，如何受控地在仓库里行动。

所以面试里不要把多 provider 当作最大创新点。

### 12.7 面试怎么讲

可以这样讲：

> Provider 层把不同模型协议统一成 complete 接口。OpenAI-compatible 走 /responses，Anthropic-compatible 走 /messages，Ollama 走 /api/generate。runtime 只拿到文本和 completion metadata，不关心 HTTP 协议差异。这个抽象的价值是让 AgentLoop、ContextManager、ToolExecutor 都不绑定某个 provider。

## 13. Verification / Benchmark / Review Pack：证明 harness 行为

### 13.1 这个 part 解决什么问题

Agent 系统不能只说“能跑”。它需要证据：

- 本地测试是否过。
- 工具链是否健康。
- run artifacts 是否存在。
- checkpoint 是否可恢复。
- memory benchmark 是否能跑。
- provider smoke 是否能连。

### 13.2 验证命令如何进入 checkpoint

`verification.py` 识别测试/检查类命令：

- pytest
- ruff
- mypy
- pyright
- npm/pnpm/yarn test
- cargo test
- go test

`new_verification_record()` 记录：

- verification_id
- command
- risk_class
- exit_code
- status passed/failed
- stdout_tail
- stderr_tail
- affected_checkpoint_id
- trace_event_id

这让 checkpoint 不只是“改了哪些文件”，还可以挂上“这些改动有没有被验证”。

### 13.3 scripts/check.sh

当前 canonical local gate：

```bash
uv run ruff check .
uv run pytest -q
```

这比只跑 pytest 更严格，因为 lint 也是 release readiness 的一部分。

### 13.4 review-pack 的定位

`docs/review-pack/README.md` 记录：

- 当前 branch。
- 当前 local baseline。
- targeted tests。
- memory-quality gate。
- provider benchmark help。
- provider smoke。
- one-shot smoke。
- architecture map。
- sample run artifact list。

这说明 Pico 不只是有代码，还在尝试建立 release evidence。

### 13.5 面试怎么讲

可以这样讲：

> Pico 的 verification 不是简单在最终回答里写“测试通过”。run_shell 如果被识别成测试或 lint 命令，会生成 Verification Record，并挂到 recovery checkpoint 上。这样一次 turn 的文件改动可以和验证证据关联起来。除此之外，项目有 scripts/check.sh 作为本地门禁，review-pack 记录当前测试、benchmark、provider smoke 和 run artifact 结构。

### 13.6 边界和不足

Verification evidence 不等于 CI。

它记录本地执行证据，但不能保证：

- 所有平台都过。
- provider 行为稳定。
- 外部服务可用。
- 未运行的测试也没问题。

它的价值是让本地 agent 变更有可追溯证据。

## 14. Security / Redaction：避免把敏感信息写进工件

### 14.1 这个 part 解决什么问题

Pico 会写很多本地 artifacts：

- session JSON
- trace JSONL
- report JSON
- checkpoint records
- tool results

这些内容可能包含：

- API key。
- token。
- env var。
- shell 输出。
- provider error body。

所以必须有 redaction。

### 14.2 做了什么

`security.py` 提供：

- sensitive env name detection。
- secret-shaped text detection。
- configured secret env items。
- detected secret env summary。
- redact_text。
- redact_artifact。
- filtered shell_env。

Runtime 会把 redactor 注入：

- `RunStore`
- `SessionStore`

trace/report/session 写入前都会经过 redaction。

### 14.3 shell 环境过滤

`shell_env()` 不直接继承所有 env，而是按 allowlist 过滤，并设置 PWD。

这降低命令执行时泄漏 key 的概率。

### 14.4 面试怎么讲

可以这样讲：

> Pico 会持久化大量运行证据，所以必须先考虑脱敏。Runtime 把 redactor 注入 RunStore 和 SessionStore，写 trace、report、session 时会递归处理敏感 key 和环境变量值。run_shell 也不会继承完整父环境，而是只传 allowlist 环境变量，减少 secret 被命令读取或输出的风险。

### 14.5 边界

这不是机密计算，也不能保证所有 secret 都被识别。

它能做：

- env-name based redaction。
- secret value substring redaction。
- 常见 secret pattern detection。
- shell env allowlist。

它不能替代：

- secret scanner。
- sandbox。
- 凭证隔离。

## 15. CLI Inspection / Recovery Surface：用户如何看到内部状态

### 15.1 为什么需要 inspection surface

如果系统内部有 trace/checkpoint/report，但用户看不到，那只是隐藏实现。

Pico 提供命令：

- `pico-cli runs list`
- `pico-cli runs show <run_id>`
- `pico-cli checkpoints list`
- `pico-cli checkpoints show <id>`
- `pico-cli checkpoints preview-restore <id>`
- `pico-cli checkpoints restore <id> [--apply]`
- `pico-cli checkpoints prune ...`
- `pico-cli sessions list/show`
- `pico-cli memory list/show/search/review/migrate`

这些命令让 harness 的内部状态变成用户可检查的 surface。

### 15.2 preview-restore 和 restore 的交互

`checkpoints restore <id>` 默认不 apply，而是 preview。

只有加 `--apply` 才真正恢复。

这符合 recoverable editing 的原则：

> restore 必须 user initiated，且最好先 preview。

### 15.3 checkpoints id prefix

CLI 支持 checkpoint id prefix，但要求：

- prefix 长度至少 6。
- 只能唯一匹配。
- 多个匹配时报 ambiguous。

这是小细节，但体现 CLI 安全性：恢复命令不能因为短 id 模糊匹配误操作。

### 15.4 面试怎么讲

可以这样讲：

> Pico 不只是内部记录状态，还提供显式 CLI surface。用户可以查看 runs、sessions、checkpoints、memory，也可以 preview restore。restore 默认只是预览，只有 --apply 才落盘，而且 checkpoint id prefix 必须唯一。这让 recovery 不只是内部机制，而是用户可操作、可检查的产品能力。

## 16. 一次完整请求如何穿过所有 part

假设用户输入：

```text
修复测试失败，并验证通过。
```

完整链路是：

```text
CLI
  -> build_agent
  -> Pico runtime
  -> AgentLoop.run
  -> ContextManager.build
  -> model_client.complete
  -> parse_model_output
  -> ToolExecutor.execute
  -> tools runner
  -> Tool Change Record
  -> trace/task_state/session
  -> verification evidence
  -> recovery turn checkpoint
  -> report
```

### 16.1 入口

CLI 判断这是 `run` prompt，不是 pre-agent command。

它会：

- 构建 workspace。
- 加载 `.env`。
- 选择 provider。
- 创建 session store。
- 创建 Pico runtime。

### 16.2 Prompt

ContextManager 生成 prompt：

- prefix：规则、工具、project docs、memory index、project structure。
- history：workspace state、checkpoint text、transcript。
- current request：用户请求。

### 16.3 探索

模型先调用 read-only 工具：

- `search`
- `read_file`
- `repo_lookup`
- `memory_search`

ToolExecutor 仍然记录 metadata，但不创建 recovery Tool Change Record。

### 16.4 修改

模型调用 `patch_file`。

ToolExecutor：

- 校验 old_text 精确出现一次。
- approval。
- 创建 pending Tool Change Record。
- 捕获 before blob/hash。
- 执行 patch。
- 捕获 after blob/hash。
- 生成 file_entries。
- finalize Tool Change Record。

### 16.5 验证

模型调用：

```bash
uv run pytest -q
```

ToolExecutor：

- command risk class。
- run_shell。
- observer diff。
- metadata。

AgentLoop：

- 判断这是 verification command。
- 收集 verification evidence。

### 16.6 结束

模型返回 final。

AgentLoop：

- task_state completed。
- 创建 resume checkpoint。
- 汇总 tool_change_ids 成 recovery turn checkpoint。
- 把 verification evidence 挂上去。
- 写 run_finished trace。
- 写 report。

### 16.7 如果用户要恢复

用户执行：

```bash
pico-cli checkpoints preview-restore <checkpoint-id>
```

RecoveryManager：

- 加载 checkpoint。
- 每个 file entry 做 plan。
- 输出 restore/review/conflict。

用户执行：

```bash
pico-cli checkpoints restore <checkpoint-id> --apply
```

RecoveryManager：

- 只处理 restore entry。
- 校验当前 hash。
- 写回 before blob。
- 写 restore checkpoint。

这就是闭环。

## 17. 面试官追问清单

### 17.1 你这个项目和普通 ChatGPT wrapper 区别是什么

普通 wrapper 只做：

```text
user -> model -> answer
```

Pico 做：

```text
user -> bounded context -> model -> structured tool -> policy -> side effect record -> checkpoint -> trace/report
```

核心区别是：Pico 控制模型进入本地世界的边界。

### 17.2 你最核心的工程难点是什么

不是 provider，不是 CLI，而是：

- ToolExecutor。
- Recoverable Editing。
- Context 输入治理。

其中最能体现工程深度的是 ToolExecutor + Recovery：

> 模型改文件前后如何捕获状态，失败但有副作用怎么办，恢复时如何避免覆盖用户后续修改。

### 17.3 为什么不直接用 Git

因为 Git 不知道哪些 dirty changes 属于 agent。Pico 要恢复的是 agent 状态转移，不是回到 commit。

### 17.4 为什么 memory 要克制

因为 memory 一旦直接全塞 prompt，会污染模型。Pico 只放 index，读取要走工具，保留审计痕迹。

### 17.5 为什么不做强 sandbox

当前定位是本地可信 developer tool。强 sandbox 成本高，需要 OS 级隔离。Pico 当前先做策略、审批、审计、恢复。

### 17.6 为什么 prompt cache 和 stable prefix 相关

因为 stable prefix 如果经常变，cache key 就没意义。Pico 把 branch/status/recent commits 放 history，而不是 stable prefix，就是为了减少 prefix churn。

### 17.7 为什么 run_shell workspace_write 默认 allow

因为很多测试/构建命令会写缓存或生成文件；完全 ask 会让本地开发体验很差。Pico 选择允许 workspace_write，但记录 side effects；destructive/external_effect 才 ask。

这个回答要补一句：

> 这不是强安全边界。如果要执行不可信命令，需要 sandbox。

### 17.8 你如何证明这套系统真的可测

测试覆盖包括：

- context manager。
- tool executor。
- recovery e2e。
- recovery manager。
- checkpoint store。
- command policy。
- workspace observer。
- memory。
- provider clients。
- CLI recovery。
- run store。
- security。

项目还有 `scripts/check.sh`：

```bash
uv run ruff check .
uv run pytest -q
```

## 18. 每个 part 的一句话总结

CLI Surface：

> 把用户入口分成 pre-agent inspection 和 agent execution，避免所有操作都启动模型。

Runtime：

> 装配所有子系统，保证 workspace、redaction、stores、tools、memory、recovery 使用同一套状态边界。

AgentLoop：

> 把一次请求变成可持久化、可复盘、可终止的 model/tool 状态机。

ContextManager：

> 控制模型每轮看到什么，按 prefix/history/current_request 分层，并有明确预算和压缩策略。

Memory：

> 提供可审计、分层、按需读取的持久知识，而不是把旧信息全部塞进 prompt。

RepoMap：

> 把项目结构放进 prompt，把具体符号查询留给工具，降低 prompt 噪声。

Tool Registry：

> 定义模型能申请哪些动作，以及每个动作的参数和风险。

ToolExecutor：

> 把模型行动变成可校验、可审批、可记录、可恢复的结构化事务。

Safe Execution：

> shell 不禁用，但必须经过命令风险分类、approval 和 side-effect 观察。

Recoverable Editing：

> 记录 agent 文件状态转移，恢复前先 preview，当前 hash 不匹配就 conflict。

Stores：

> Session 负责继续对话，Run 负责复盘过程，Checkpoint 负责恢复真相。

Provider：

> 抹平模型协议差异，但不侵入 agent runtime。

Verification：

> 把测试/lint 命令结果作为 evidence 挂到 checkpoint 上。

Security：

> 对 session/run artifacts 脱敏，并过滤 shell 环境变量。

CLI Recovery Surface：

> 让用户能查看、预览、应用和清理恢复点，而不是只能相信最终回答。

## 19. 最终面试讲法

如果只能讲 2 分钟，可以这样说：

> Pico 是一个本地 coding-agent harness，它的核心不是调用模型，而是让模型在真实代码仓库里的行为可控。它分几层：CLI 把 inspection 命令和 agent execution 分开；Runtime 装配 model、workspace、session、memory、tools、recovery；AgentLoop 把一次用户请求变成有 TaskState、trace 和 report 的状态机；ContextManager 控制模型每轮看到什么，stable prefix、history、current request 分层，超预算先压 history，当前请求不裁剪；Memory 只把 index 放进 prompt，具体内容用 memory tools 读；ToolExecutor 是行动边界，做参数校验、重复调用拦截、shell risk、approval、side effect 捕获，并为写操作生成 Tool Change Record；Recovery 用 before blob、after hash、expected_current_hash 做 preview/restore，当前文件状态不匹配就 conflict，避免覆盖用户后续修改。RunStore 负责复盘，CheckpointStore 负责恢复真相，二者不混。整体目标是把“模型写代码”变成输入可治理、行动可控制、副作用可恢复、过程可复盘的工程流程。

如果面试官继续追问，优先展开：

1. ToolExecutor + Recovery。
2. ContextManager + Memory。
3. RunStore / CheckpointStore 分离。
4. shell risk + WorkspaceObserver。
5. CLI recovery surface。

## 20. 这个项目真实的强项和短板

### 20.1 强项

- 系统边界意识强，不是薄 wrapper。
- Context 输入治理有预算、分层、metadata。
- ToolExecutor 是统一行动入口。
- Recovery 有 hash/blob/conflict，不盲目覆盖。
- Run artifacts 和 recovery truth 分离。
- Memory 分层克制，可审计。
- Provider 不污染 runtime。
- CLI 有 inspection/recovery surface。
- 测试面覆盖多个关键子系统。

### 20.2 短板

- Runtime 和 ToolExecutor 偏大。
- Safe Execution 不是 OS sandbox。
- Recovery 是文件级，不是 hunk 级。
- Memory 是 lexical retrieval，不是 semantic memory。
- RepoMap 不是 LSP，跨语言只是 best-effort。
- Provider streaming 不完全对称。
- CLI recovery 缺少更友好的 diff/preview UI。
- durable memory 缺少自动 consolidation 和 provenance 行级引用。

### 20.3 后续优化优先级

第一优先级：

- 拆 ToolExecutor 的 side-effect recording。
- 让 recovery preview 展示更清楚的 diff。
- 加 selective restore。

第二优先级：

- memory provenance。
- memory consolidation。
- memory eval：precision/recall/secret leakage/stale recall。

第三优先级：

- hunk-level restore。
- OS-level sandbox。
- stronger provider protocol。
- RepoMap dependency graph。

## 21. 最后一段总结

Pico 最值得讲的不是“它能调用模型写代码”，而是它把模型写代码这件事放进了一套工程控制系统里。

这套系统的核心是不变量：

- 当前请求不能被裁剪。
- 模型不能绕过工具边界。
- workspace write 必须产生可审计 metadata。
- pending tool change 必须闭环。
- trace 能复盘，但不作为恢复真相。
- 恢复前必须 preview。
- 当前 hash 不匹配不能自动覆盖。
- 用户 notes 只读，agent notes 只能 append。
- provider 差异不能泄漏进 AgentLoop。

只要围绕这些不变量讲，Pico 就不是一堆模块，而是一个有明确工程边界的 coding-agent harness。
