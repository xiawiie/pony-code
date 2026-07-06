# Pico 面试重点解说稿

- 日期：2026-07-06
- 类型：聚焦版项目解说稿
- 写作目标：把 Pico 讲成一个有主线、有重点、能被追问的 coding-agent harness 项目
- 当前限制：飞书 wiki 文档暂时无读取权限，本稿先基于本地仓库代码、README、架构文档、memory 文档和 review-pack 整理；拿到飞书内容后可以再融合补充

## 0. 这版和上一版的区别

上一版的问题是资料很全，但太散：CLI、memory、context、provider、recovery 都讲了，却没有一条主线，导致听起来像“我做了很多模块”，而不是“我解决了一个关键问题”。

这版只围绕一条主线：

> 大模型已经能写代码，但在真实仓库里工作时最大的问题不是“会不会生成代码”，而是“它的行为是否可控、可追踪、可恢复”。Pico 的价值是构建模型外面的工程控制层，让 agent 的每一步都有上下文边界、工具边界、恢复边界和证据链。

所以讲 Pico 时，不要从“我做了 memory、context、provider”开始。应该从这三个核心问题开始：

1. **模型每一步到底看到了什么？**  
   这对应 Context。

2. **模型想执行动作时，谁来判断能不能执行？**  
   这对应 Tool / Safety。

3. **模型改错文件后，用户怎么知道、怎么恢复？**  
   这对应 Recovery。

Memory、Provider、Benchmark 都是支撑这条主线的模块，不是主线本身。

## 1. 面试中最应该讲的项目定位

### 1.1 一句话版本

Pico 是一个本地 coding-agent harness，它不是简单调用 LLM，而是给模型套上一层可审计的工程运行时：控制它看什么上下文、能调用什么工具、怎么记录执行过程，以及改错文件后怎么安全恢复。

### 1.2 更强的版本

Pico 解决的是 coding agent 落地时的可信执行问题。模型负责决策，runtime 负责约束和执行；模型不能直接碰文件系统和 shell，而是通过显式工具进入一个有参数校验、风险分类、审批策略、trace 记录和 recovery 记录的执行链路。

### 1.3 面试官应该听到的关键词

你要让面试官听到这些词：

- coding-agent harness
- bounded context
- explicit tools
- command risk policy
- recoverable editing
- checkpoint / tool change record / file-state blob
- trace / report / benchmark evidence
- memory as auditable knowledge layer

不要让面试官只听到：

- 我接了几个模型 API
- 我做了一个 CLI
- 我加了 memory
- 我能让模型改代码

后面这些都太浅。

## 2. 30 秒讲稿

Pico 是我做的一个本地 coding-agent harness。它的目标不是单纯调用大模型，而是解决大模型在真实代码仓库里工作时不可控的问题。一次任务里，Pico 会先构造受预算控制的上下文，让模型知道当前仓库状态和可用工具；模型如果想读文件、跑命令或改文件，必须经过显式工具、参数校验、风险分类和审批策略；执行过程会写入 trace 和 report；如果修改了文件，还会进入 recoverable editing 链路，记录 Tool Change、Checkpoint 和文件状态 blob，恢复时先 preview，再用 hash 校验避免覆盖用户后续修改。这个项目最核心的价值是把“模型写代码”变成一个可控、可审计、可恢复的工程流程。

## 3. 2 分钟讲稿

我做 Pico 时关注的不是“LLM 能不能写代码”，而是“LLM 在真实仓库里写代码时怎么被工程化约束”。因为一个 coding agent 真正落地时会遇到几个问题：仓库上下文太大，不能全部塞进 prompt；模型可能重复调用工具或请求危险命令；模型改错文件后，用户需要知道它改了什么，并且能安全恢复；任务失败后也需要 trace 来复盘。

所以 Pico 的架构是一个本地 Python CLI agent harness。入口是 `pico-cli`，runtime 会组装 workspace、session、run store、memory、repo map、tool executor 和 recovery manager。一次用户请求进入后，由 `AgentLoop` 执行主循环：构造 prompt、请求模型、解析输出、执行工具、记录 trace，直到模型给出 final answer 或达到停止条件。

这里面最重要的三层是：

第一层是 **Context**。Pico 不会把全仓库和所有记忆都塞进 prompt，而是把工具说明、项目结构、memory index、workspace 状态、历史和当前请求分区组织。超预算时优先压缩历史，当前请求不裁剪。这样模型每一步“看到什么”是可解释的。

第二层是 **Tool / Safety**。模型不能直接执行 shell 或写文件，只能申请显式注册的工具。`ToolExecutor` 会做工具 allowlist、参数校验、重复调用拦截、command risk classification、approval policy，然后才真正执行。

第三层是 **Recovery**。如果工具会修改文件，Pico 会记录工具执行前后的文件状态，生成 Tool Change Record 和 Turn Checkpoint。恢复时不是盲目回滚，而是先生成 restore plan，再检查当前文件 hash 是否和预期一致；如果用户后续已经手改，就进入 conflict，不自动覆盖。

Memory、Provider 和 Benchmark 是支撑系统。Memory 不是黑盒语义长期记忆，而是 `AGENTS.md`、用户 notes 和 agent notes 组成的可审计知识层；Provider adapter 把 OpenAI-compatible、Anthropic-compatible、DeepSeek、Ollama 统一成 runtime 可调用的接口；Benchmark 和 run artifacts 则提供行为证据。整体上，Pico 体现的是 agent 工程化能力，而不是单纯 API 调用能力。

## 4. 10 分钟完整解说稿

### 4.1 开场：为什么这个项目值得讲

如果面试官问“你做的 Pico 是什么”，不要直接说“它是一个命令行代码助手”。这样太弱。

应该这样开场：

> Pico 是一个本地 coding-agent harness。我做它的出发点是：大模型已经能生成代码，但要让它在真实仓库里工作，真正难的是模型外面的工程控制层。因为模型会读文件、跑命令、修改文件，这些动作如果没有上下文边界、工具边界、恢复边界和证据链，就很难放心使用。

这句话的重点是把项目从“AI 应用”拔高到“agent runtime / harness”。

### 4.2 核心矛盾：LLM + Tools 为什么不够

一个最朴素的 coding agent 可以这样做：

1. 把用户需求发给模型。
2. 让模型输出 shell 命令或代码补丁。
3. 本地执行。
4. 把结果再发回模型。

这个 demo 可以跑，但工程上不够。原因有四个。

第一，模型上下文不可控。  
仓库文件很多，历史也越来越长，如果每轮都拼一堆内容，模型可能看不到重点；如果拼得太少，它又不知道项目背景。

第二，工具执行不可控。  
模型可能要求跑 shell、写文件、patch 文件。任何一个动作都可能造成副作用。不能让模型自由输出 bash 后直接执行。

第三，文件修改不可恢复。  
模型改错文件很常见。只知道“它执行了 patch_file”是不够的，你要知道改前是什么、改后是什么、现在还能不能安全恢复。

第四，失败不可复盘。  
agent 失败时，不能只看最终答案。你要知道 prompt 怎么构造的、模型返回了什么、工具为什么被拒绝、哪个 checkpoint 被创建、测试有没有跑。

Pico 就是围绕这四个问题做工程拆解。

### 4.3 Pico 的总架构

Pico 的执行链路可以用一句话概括：

```text
用户请求
  -> CLI 解析配置和 provider
  -> Runtime 组装工作区、记忆、工具、恢复组件
  -> AgentLoop 控制一次任务
  -> ContextManager 构造受预算约束的 prompt
  -> ModelClient 返回 tool call 或 final answer
  -> ToolExecutor 校验、审批、执行工具
  -> RunStore / CheckpointStore 写审计和恢复证据
```

这里的关键设计是：**模型只是 planner，不是 executor**。

模型不能直接读写本地文件，也不能直接跑命令。它只能提出工具调用。是否执行、怎么执行、执行后怎么记录，由 Pico runtime 负责。

### 4.4 第一条主线：Context，模型每一步到底看到了什么

Context 不是“把东西塞进 prompt”这么简单。对 coding agent 来说，context 决定模型能不能正确理解任务。

Pico 的 ContextManager 解决三个问题：

1. 当前仓库状态怎么表达？
2. 历史和记忆怎么放进 prompt？
3. 超出预算时牺牲什么、不牺牲什么？

Pico 把 prompt 分成几个区域：

- stable prefix：工具说明、约束规则、项目结构等相对稳定的内容。
- memory guidance：告诉模型什么时候可以读 memory，什么时候不能乱写 memory。
- project structure：仓库顶层结构，让模型知道项目大概长什么样。
- memory index：告诉模型有哪些记忆文件，但不把完整内容全塞进去。
- workspace volatile state：分支、状态、最近提交等会变化的信息。
- history：之前的对话和工具结果。
- current request：当前用户请求。

重点是：**当前用户请求不裁剪**。  
超预算时，Pico 优先压缩 history，再压缩 prefix。因为当前请求是这一轮任务本身，裁掉它会导致模型误解目标；历史信息只是辅助材料，可以压缩。

这在面试里要讲成一个取舍：

> 我没有追求上下文越多越好，而是把上下文分优先级。当前请求是硬约束，历史是软上下文，memory 和 repo map 走索引加按需读取。这样模型每一步看到的内容是可解释、可压缩、可复盘的。

#### Context 和 Memory 的区别

这里很容易被问：既然有 memory，为什么还要 context manager？

可以这样答：

> Memory 是可持久化的知识来源，Context 是本轮实际喂给模型的输入。Memory 不应该全部进入 prompt，否则既浪费预算，也污染注意力。Pico 只把 memory index 放进 prompt，让模型知道有哪些知识可以查；真正需要时，再用 `memory_read` 或 `memory_search` 工具读取。

这句话很重要。它把 memory 从“一个模块”变成了 context 策略的一部分。

#### Context 面试重点

你要强调：

- Pico 不是全量塞 prompt。
- 它做 section 化。
- 它区分 stable 和 volatile。
- 它把 memory/repo map 作为索引，不是全文。
- 它有预算和降级顺序。

不要只说：

“我做了上下文压缩。”

要说：

“我把上下文变成可解释的输入预算系统。”

### 4.5 第二条主线：Tool / Safety，模型想做动作时谁来把关

有了 context，模型知道任务和工具。但下一步更危险：模型会要求执行动作。

Pico 的工具层有两个原则：

1. 工具必须显式注册。
2. 模型只能申请工具，不能直接执行。

Pico 的工具包括读文件、搜索、跑 shell、写文件、patch 文件、memory 工具、repo lookup、delegate 等。每个工具都有 schema、description、risky 标记。

ToolExecutor 的流程可以讲得非常具体：

1. 检查这个工具是否在本次 allowed tools 里。
2. 检查工具名是否存在。
3. 校验参数，比如路径、行号、timeout、patch 的 old_text 是否唯一。
4. 拦截重复工具调用，避免模型卡在同一个动作上。
5. 如果是 `run_shell`，先做 command risk classification。
6. 根据 risk class 和 approval policy 判断 reject / ask / allow。
7. 如果工具会改 workspace，先创建 pending Tool Change Record。
8. 捕获执行前文件状态或 workspace observer 状态。
9. 执行工具。
10. 捕获执行后 affected paths。
11. finalize Tool Change Record。
12. 写 trace metadata。

这条链路是 Pico 的核心工程价值之一。

#### 为什么不能直接让模型输出 bash

这是高频追问。

强回答：

> 直接 bash 的问题是动作语义丢失。模型输出一段 shell，harness 很难知道它到底是读、写、删、联网还是修改配置。Pico 把常见动作拆成显式工具，让每个动作都有 schema、risk、approval 和 recovery 记录。`run_shell` 仍然存在，但它只是工具之一，而且要经过 command risk policy。

#### Safe Execution 的边界

这里必须诚实：

Pico 当前不是 OS sandbox。它做的是策略层安全：

- 路径限制。
- 工具参数校验。
- command risk classification。
- approval policy。
- read-only delegate。
- shell env allowlist。
- secret redaction。
- trace 审计。

它不能替代容器、seccomp、macOS sandbox 或远程隔离环境。

面试里可以主动说：

> 如果目标是执行不可信代码，我不会说 Pico 当前安全边界足够。它现在更适合本地开发者工具场景，通过策略、审批和审计降低误操作风险。后续如果要给不可信任务使用，需要加 OS 级隔离。

主动说边界，反而更可信。

### 4.6 第三条主线：Recovery，模型改错文件后怎么办

Recovery 是最该讲深的部分。

因为 coding agent 最现实的问题就是：它会改文件，而且可能改错。  
如果系统没有 recovery，用户很难放心让 agent 自动编辑仓库。

Pico 的 recoverable editing 不是简单 undo，也不是 Git checkout。它是一套围绕工具副作用建立的文件状态记录系统。

核心概念有四个：

1. **Tool Change Record**  
   一次工具调用产生的文件影响记录。

2. **Turn Checkpoint**  
   一次用户请求结束时，把这一轮的 Tool Change Records 打包成恢复入口。

3. **File-State Blob**  
   文件内容按 hash 存储，保存改动前或改动后的真实字节状态。

4. **Restore Plan**  
   恢复前先生成计划，告诉用户哪些能恢复、哪些需要 review、哪些冲突。

#### Recovery 的完整流程

可以这样讲：

> 对于会修改 workspace 的工具，Pico 在执行前创建 pending Tool Change Record，并捕获候选路径的 before 状态。执行后，它通过路径快照或 workspace observer 计算 affected paths，再构造 file entries，里面包含 path、change kind、before hash、after hash、blob ref、snapshot eligibility 等信息。run 结束时，AgentLoop 会把本轮所有 tool changes 打包成 Turn Checkpoint。

恢复时：

1. 用户选择一个 checkpoint。
2. RecoveryManager 读取 checkpoint。
3. 对每个 file entry 生成 restore decision。
4. 如果当前文件 hash 和 expected_current_hash 不一致，标记 conflict。
5. 如果没有 before blob 或不 eligible，标记 review。
6. 只有 decision 是 restore 的条目才应用。
7. 应用时先写临时文件，校验 hash，再 replace，最后读回校验。
8. 恢复完成后生成 Restore Checkpoint，记录 restore provenance。

这个流程的重点是：**不盲目覆盖用户文件**。

#### 为什么不直接用 Git

这是一定会被问的。

强回答：

> Git 很适合做 review context，但不适合作为 Pico 的恢复真相。因为用户工作区可能本来就是 dirty，agent 可能修改 untracked 文件，Git HEAD 不等于工具执行前状态。Pico 要恢复的是 agent 造成的变化，所以它在工具执行前后自己记录文件状态和 hash。这样即使用户后来手动改了文件，restore 时也能发现 hash 不一致，避免覆盖用户后续修改。

#### 为什么不做 hunk 级恢复

强回答：

> 第一阶段选择文件级恢复，是为了保证语义简单、可验证。hunk 级恢复需要处理上下文漂移、patch apply、merge conflict，会把系统复杂度提高很多。Pico 当前宁可在冲突时进入 review，也不自动做不可靠 merge。

#### Recovery 面试重点

你要强调：

- 恢复不是 trace。
- 恢复不是 Git。
- 恢复不是 blind overwrite。
- 恢复依赖 checkpoint + blob + hash。
- 冲突时保守处理。

一句很有力的话：

> Trace 回答“发生过什么”，Checkpoint 回答“现在能不能安全恢复”。

## 5. Memory 要怎么重点讲

上一版里 memory 讲散了。这一版要把 memory 放到主线里讲：它不是炫技模块，而是 context 的持久知识来源。

### 5.1 先讲为什么需要 memory

coding agent 如果完全没有 memory，每次 session 都像第一次见项目：

- 不知道项目约定。
- 不知道用户偏好。
- 不知道之前踩过的坑。
- 不知道某些命令或 provider 的环境配置。

但如果 memory 设计得太随意，也会出问题：

- agent 乱写记忆，污染后续 prompt。
- 用户手写知识被覆盖。
- 所有历史都塞进 prompt，预算爆炸。
- 记忆来源不可追踪。

所以 Pico 的 memory 目标不是“像人一样记忆”，而是：

> 提供一个可审计、可分层、可检索的本地知识层。

### 5.2 Pico 的三层 memory

第一层：`AGENTS.md`  
这是项目级约定，类似“每次进入这个仓库都要遵守的规则”。它适合放团队规范、测试命令、代码风格、注意事项。

第二层：`.pico/memory/notes/*.md`  
这是用户手写 notes。它的定位是用户主动沉淀的项目知识。agent 可以读，但不应该把它当普通 scratchpad 随便改。

第三层：`.pico/memory/agent_notes.md`  
这是 agent 在用户明确要求“记住”时追加的短笔记。它是 append-only 思路，不是随便重写。

这三层的重点不是文件名，而是权限边界：

- 项目约定独立。
- 用户知识独立。
- agent 经验独立。

### 5.3 Memory 和 Context 的关系

这段一定要讲清楚：

> Memory 是持久知识库，Context 是当前这轮模型真正看到的输入。Pico 不会把全部 memory 塞进 prompt，而是把 memory index 放进 prompt，让模型知道有哪些文件可以查。模型需要细节时，通过 `memory_search` 或 `memory_read` 按需读取。

这样讲，面试官会知道你不是简单做“长上下文堆料”。

### 5.4 为什么用 BM25 + CJK bigram

Pico 的 memory retrieval 是关键词检索，不是 embedding。

优点：

- stdlib-only，不需要外部向量数据库。
- 结果可解释，能看到为什么命中。
- 容易测试。
- CJK bigram 能覆盖中文连续文本的一些搜索场景。

缺点：

- 不理解语义同义词。
- “身份认证”和“auth”不一定互相命中。
- 不会做自动总结和聚类。

强回答：

> 我没有把它包装成语义长期记忆。它更像一个可审计的项目知识层。第一阶段我选择 BM25，是因为它可解释、可测试、依赖低。后续如果引入 embedding，需要同时解决隐私、成本、索引更新和评估问题。

### 5.5 Memory 被追问时怎么答

问题：Pico 的 memory 有什么价值？

回答：

> 它解决的是跨 session 的项目知识复用。比如项目约定、用户手写注意事项、agent 被明确要求记住的经验，都可以在后续任务里被索引和读取。但它不会无脑塞进 prompt，而是通过 memory index 和 memory tools 按需访问。

问题：这是不是长期记忆？

回答：

> 如果“长期记忆”指持久化保存和跨 session 读取，那它是 durable memory；如果指语义理解、自动归纳和个性化推理，那它还不是。它当前定位是可审计的本地知识层。

问题：为什么不让 agent 自动保存所有经验？

回答：

> 自动保存会造成记忆污染。Pico 的 guidance 明确要求 `memory_save` 只在用户明确要求记住时使用，避免把临时工具结果、当前 turn 状态、无价值路径都写进 durable memory。

## 6. Evidence：为什么说 Pico 不是 demo

面试里最后要讲证据链。否则听起来像“设计得很好”，但不知道有没有落地。

Pico 的证据链有三层。

### 6.1 Run artifacts

每次用户请求会生成：

- `task_state.json`
- `trace.jsonl`
- `report.json`

它们解决的是运行可观测性：

- 当前任务状态是什么？
- 模型请求了几轮？
- 执行了哪些工具？
- 哪些工具被拒绝？
- 最终为什么停止？
- prompt metadata 和 completion metadata 是什么？

其中 trace 用 JSONL append，适合记录事件流。

### 6.2 Checkpoint store

Checkpoint store 解决的是恢复真相，不是运行日志。

它包含：

- checkpoint records
- tool change records
- file-state blobs

它回答的问题是：

- 哪些文件由 agent 改了？
- 改之前是什么 hash？
- 改之后是什么 hash？
- 有没有 before blob？
- 当前文件还能不能安全恢复？

### 6.3 Tests and benchmarks

本地门禁是：

```bash
./scripts/check.sh
```

它包含：

```bash
uv run ruff check .
uv run pytest -q
```

除此之外，项目还有 memory-quality benchmark 和 provider benchmark。

这里要注意说法：

- fake benchmark 证明 harness 链路稳定，比如 memory tool 调用、trace 写入、scoring path。
- live benchmark 才观察真实 provider 行为。

不要说 fake benchmark 证明模型能力。

## 7. 面试中最该深入的 5 个问题

### 7.1 你这个项目和普通 ChatGPT wrapper 有什么区别？

强回答：

普通 wrapper 的核心是 prompt 和模型回复。Pico 的核心是模型外面的 runtime。它控制模型看什么上下文、能申请哪些工具、工具怎么校验和审批、执行结果怎么记录、文件改动怎么恢复、失败后怎么复盘。所以它不是聊天壳，而是 coding-agent harness。

### 7.2 你觉得 Pico 最难的部分是什么？

强回答：

最难的不是 provider API，而是状态边界。比如 context 是当前输入边界，tool 是执行边界，checkpoint 是恢复边界，trace 是审计边界，memory 是跨 session 知识边界。这些边界如果混在一起，系统很快就不可控。Pico 的设计重点就是把这些边界拆清楚。

### 7.3 Context 为什么重要？

强回答：

agent 的每一步决策都取决于模型看到的内容。上下文太少，模型不知道项目；上下文太多，预算爆炸且注意力分散。Pico 做的是把上下文分区、分优先级、可压缩，并把 memory 和 repo map 作为索引按需访问，而不是全量塞 prompt。

### 7.4 Recovery 为什么重要？

强回答：

coding agent 最让用户不放心的是自动改文件。Recovery 的价值是降低使用风险：用户可以看到 agent 改了什么，恢复前能 preview，当前文件变了就 conflict，不盲目覆盖。这让 agent 从“试试看”更接近“可以托付小任务”。

### 7.5 Memory 为什么不是重点功能，而是支撑系统？

强回答：

Memory 本身不是目的。它服务于 context，让 agent 在后续任务中能找到项目约定和用户经验。但它必须受控，否则会污染 prompt。所以 Pico 的 memory 重点是分层、权限、索引和按需读取，而不是做一个黑盒大脑。

## 8. 如果被要求讲技术架构图

可以用这段口头图：

```text
                  +------------------+
                  |    User / CLI     |
                  +---------+--------+
                            |
                            v
                  +------------------+
                  |   Pico Runtime    |
                  | session/run/mem   |
                  +---------+--------+
                            |
                            v
                  +------------------+
                  |    AgentLoop      |
                  | plan-act-record   |
                  +----+--------+----+
                       |        |
                       v        v
             +-------------+  +----------------+
             | ContextMgr  |  | Model Provider |
             | prompt/budg |  | openai/anth... |
             +-------------+  +----------------+
                       |
                       v
                  +------------------+
                  |  ToolExecutor     |
                  | validate/policy   |
                  +----+--------+----+
                       |        |
                       v        v
             +-------------+  +----------------+
             | RunStore    |  | CheckpointStore|
             | trace/report|  | blobs/restore  |
             +-------------+  +----------------+
```

讲图时按这个顺序：

1. CLI 只是入口。
2. Runtime 是依赖组装。
3. AgentLoop 是控制循环。
4. ContextManager 决定模型看到什么。
5. Provider 返回下一步。
6. ToolExecutor 决定能不能执行。
7. RunStore 负责审计。
8. CheckpointStore 负责恢复。

## 9. 对上一版材料的修正总结

上一版材料把模块都列出来了，但没有回答“为什么这些模块必须存在”。新稿的修正是：

- `context` 不再只是“上下文模块”，而是回答“模型每一步看到什么”。
- `tool` 不再只是“工具模块”，而是回答“模型想行动时谁来把关”。
- `recovery` 不再只是“checkpoint 模块”，而是回答“模型改错后怎么恢复信任”。
- `memory` 不再独立散讲，而是作为 context 的持久知识来源。
- `provider` 不再当亮点展开，而是作为 runtime 的外部模型适配层。
- `benchmark` 不再泛泛说测试多，而是说明它提供 harness 行为证据。

这才是面试里的重点。

## 10. 最终推荐背诵版本

如果只能背一段，就背这一段：

> Pico 是一个本地 coding-agent harness。它解决的不是“怎么调用大模型”，而是“怎么让大模型在真实代码仓库里可控地工作”。我把问题拆成三层：第一，Context，模型每一步到底看到什么，Pico 通过 section 化 prompt、memory index、repo structure 和预算压缩来控制输入；第二，Tool / Safety，模型不能直接执行动作，只能申请显式工具，ToolExecutor 负责参数校验、风险分类、审批和 trace；第三，Recovery，模型改文件后，Pico 会记录 Tool Change、Checkpoint 和文件状态 blob，恢复时先 preview，再用 hash 判断能否安全恢复，冲突不自动覆盖。Memory、Provider、Benchmark 都围绕这条主线服务：Memory 提供可审计的持久知识，Provider 适配不同模型协议，Benchmark 和 run artifacts 证明 harness 行为。这个项目最能体现的是 agent 工程化能力，而不是简单的 API 调用能力。

## 11. 拿到飞书文档后要补哪里

飞书文档如果之后能读取，优先补三类内容：

1. **项目动机**  
   如果飞书里有为什么做 Pico、和其他 agent 的比较，要补进第 4.1 和 4.2。

2. **核心设计取舍**  
   如果飞书里有 memory/context/recovery 的原始思考，要补进第 4.4、4.6、5。

3. **面试表达**  
   如果飞书里有你自己的经历背景或项目目标，要补进第 2、3、10，让讲稿更像你的个人表达，而不是通用项目分析。

当前由于权限不足，本文先不引用飞书内容，避免编造。

