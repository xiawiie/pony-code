# Pico 项目面试拆解材料

- 日期：2026-07-06
- 目的：把 Pico 整理成可用于简历、面试自述、资深面试官追问的完整项目材料
- 视角：候选人表达 + 面试官深挖
- 事实边界：只基于当前仓库可见代码、文档和本次只读检查，不编造用户量、线上规模或商业化结果

## 0. 怎么使用这份材料

这份材料分两层。

第一层是你在面试里主动讲的内容：项目定位、简历 bullet、30 秒版本、2 分钟版本、8-10 分钟展开稿。

第二层是面试官可能追问的内容：架构为什么这样拆、安全边界在哪里、恢复机制怎么保证、记忆系统为什么不用 embedding、provider 怎么适配、测试怎么证明可信、有哪些不足。

建议准备顺序：

1. 先背熟 30 秒版本和 2 分钟版本。
2. 再理解模块拆解中的 9 个核心模块。
3. 最后拿追问题库自测，尤其是“为什么不直接用 Git”、“为什么不是 OS sandbox”、“记忆系统是不是长期记忆”、“怎么证明 agent 改动可恢复”。

## 1. 项目一句话定位

### 1.1 最推荐的表达

Pico 是一个面向本地代码仓库的轻量级 coding-agent harness，它把大模型包装在一个可审计的工程运行时里，通过显式工具、上下文管理、执行策略、本地记忆、可恢复编辑、运行 trace 和 benchmark 证据，让模型能够在仓库里完成代码阅读、修改、测试和复盘。

### 1.2 更口语化的表达

这个项目不是单纯调一个 LLM API，而是做了模型外面那层工程控制系统：模型想读文件、改文件、跑命令，都必须经过工具校验、审批策略、trace 记录和 recovery 记录。核心目标是让 agent 写代码这件事可控、可查、可恢复。

### 1.3 面试里不要这样说

不要说：

- “这是一个完整的 IDE。”
- “它可以完全自动开发项目。”
- “它有生产级沙箱。”
- “它能替代 Git。”
- “它实现了语义长期记忆。”
- “它已经是商业级 agent 平台。”

更准确的边界是：

- 它是一个本地 CLI coding-agent harness。
- 安全是策略驱动，不是 OS 级 sandbox。
- 恢复是文件级、保守恢复，不是 Git 替代品。
- 记忆检索是 BM25 + CJK bigram，不是 embedding 语义检索。
- 目前更像一个工程可信度强的本地工具/研究型 harness，而不是商业产品。

## 2. 简历项目描述版

### 2.1 标准版

项目：Pico 本地 Coding Agent Harness

- 设计并实现一个面向本地代码仓库的 Python CLI coding agent，支持在仓库上下文中读文件、搜索代码、执行命令、修改文件、记录运行状态并生成审计工件。
- 构建 agent 控制循环，将一次任务拆为 prompt 构造、模型请求、输出解析、工具执行、trace 记录、checkpoint 生成和 report 汇总等阶段，提升模型执行过程的可观测性。
- 设计显式工具注册与执行策略，对 `read_file`、`search`、`run_shell`、`write_file`、`patch_file`、`memory_*`、`repo_lookup` 等工具做参数校验、风险分类、审批控制和执行结果记录。
- 实现可恢复编辑机制，通过 Tool Change Record、Turn Checkpoint、文件状态 blob、restore preview 和 hash conflict 检测，使 agent 产生的文件改动可以被检查、预览和保守恢复。
- 实现本地记忆 v2，将项目约定、用户笔记和 agent 追加笔记分层管理，并通过 BM25 + CJK bigram 检索和 RepoMap 符号索引辅助上下文构造。
- 接入多类模型后端，包括 OpenAI-compatible Responses、Anthropic-compatible Messages、DeepSeek Anthropic-compatible 和 Ollama，并保留统一 runtime 调用接口。
- 建立本地质量门禁和 benchmark 证据，使用 `ruff`、`pytest`、run artifacts、memory-quality benchmark 和 provider benchmark 支撑行为验证和发布可信度。

### 2.2 偏工程亮点版

- 围绕“LLM 写代码如何可控、可审计、可恢复”设计本地 agent harness，将模型输出限制为显式工具调用，并在工具执行前后记录风险策略、文件影响范围、trace 事件和恢复元数据。
- 设计文件级 recoverable editing 链路：执行前捕获候选文件状态，执行后计算 affected paths，持久化 checkpoint/tool-change/blob，并在恢复时用 sha256 校验避免覆盖用户后续修改。
- 设计 prompt context 管理器，将稳定 prefix、项目结构、memory index、workspace 状态、历史记录和当前请求分区组装，并在超预算时优先压缩历史，保证当前请求不被裁剪。
- 设计本地记忆体系，将 `AGENTS.md`、用户手写 notes 和 agent append-only notes 分开，配合路径安全、原子写入、软上限提醒和关键词检索，避免把记忆系统做成不可控 scratchpad。
- 通过 `task_state.json`、`trace.jsonl`、`report.json` 和 checkpoint store 分离运行审计与恢复真相，使 agent 执行过程既能复盘，又不会把 trace 当恢复依据。

### 2.3 保守版

如果你不想在面试里把所有模块都说成“自己独立完成”，可以这样写：

- 参与设计和完善 Pico 本地 coding-agent harness，重点负责/深入参与 runtime 控制循环、工具执行策略、可恢复编辑、记忆上下文和 benchmark 证据链的实现与梳理。
- 基于 Python stdlib 实现 CLI agent 的核心运行链路和多 provider 适配，降低运行时依赖，提升本地可复现性。
- 通过测试、文档和 review pack 固化核心不变量，包括路径隔离、命令风险分类、restore preview、checkpoint 结构、memory 工具行为和 provider benchmark 选择。

## 3. 30 秒面试版本

Pico 是我做的一个本地 coding-agent harness。它不是简单聊天机器人，而是让模型在代码仓库里通过受控工具工作。模型要读文件、搜索、跑命令、改文件，都必须经过工具注册、参数校验、风险策略和 trace 记录。项目里我重点解决三个问题：第一，怎么把仓库上下文压进有限 prompt；第二，怎么让 agent 的文件改动可审计、可恢复；第三，怎么把记忆、provider 和 benchmark 做成可维护的工程边界。最终它形成了 CLI、runtime、tool executor、recovery、memory、provider、evaluation 几个模块，能作为一个轻量但比较完整的本地代码 agent 运行时。

## 4. 2 分钟面试版本

这个项目的背景是：现在很多 coding agent 看起来只是“模型加工具”，但真正落地时最大的问题不是调用 LLM，而是模型行为不可控。比如模型会误改文件、重复调用工具、跑危险命令、上下文越来越长、任务失败后无法复盘。因此我做了 Pico，一个本地代码仓库里的轻量 coding-agent harness。

架构上，它是一个 Python CLI 项目，入口是 `pico-cli`。一次用户请求进入后，会经过 `Pico` runtime 初始化，然后由 `AgentLoop` 执行主循环。这个循环每一轮都会构造 prompt，请求模型，解析模型输出，如果模型返回工具调用，就交给 `ToolExecutor` 做参数校验、风险分类、审批策略和执行记录；如果返回最终答案，就结束任务并写 report。

这个项目最重要的不是“工具能不能跑”，而是“跑完之后有没有证据”。所以 Pico 每次 run 都会生成 `.pico/runs/<run_id>/task_state.json`、`trace.jsonl` 和 `report.json`。另外文件修改不只写进 trace，而是进入独立的 recoverable editing 系统，存 Tool Change Record、Turn Checkpoint 和文件状态 blob。恢复时不是盲目覆盖，而是先 preview，再检查当前文件 hash 是否还符合预期，只有安全的条目才 restore，冲突则进入 review。

记忆方面，它没有把所有信息都塞进 prompt，而是分成 `AGENTS.md` 项目约定、用户手写 notes、agent append-only notes，并用 BM25 + CJK bigram 做可审计检索。RepoMap 则负责符号索引，Python 用 AST，其他语言做 best-effort 正则。

模型后端方面，它支持 Ollama、OpenAI-compatible、Anthropic-compatible 和 DeepSeek，runtime 只依赖统一的 `complete()` 形状。质量上，项目有 `scripts/check.sh` 作为本地门禁，包含 ruff 和 pytest，也有 memory-quality benchmark 和 provider benchmark 来证明 harness 行为。整体上，我会把它定位成一个“让 LLM 写代码更可控、更可恢复、更可复盘”的本地工程 harness。

## 5. 8-10 分钟展开稿

### 5.1 背景与问题

我做 Pico 的核心动机是：LLM 已经能生成代码，但要让它真的在本地仓库里工作，靠“把仓库内容丢给模型 + 让模型自由输出”是不够的。真实开发里会遇到几个工程问题：

1. 上下文问题：仓库文件很多，prompt 有预算，不可能每轮把全仓库塞进去。
2. 执行安全问题：模型可能要求运行命令、写文件、patch 文件，这些动作必须可控。
3. 可恢复问题：agent 改错文件后，用户要知道它改了什么，能不能恢复到之前状态。
4. 可观测问题：一次 run 里模型看了什么、做了什么、为什么停下，需要 trace 和 report。
5. 记忆问题：跨会话的项目约定和用户经验需要保留，但不能变成无限制、不可审计的黑盒记忆。
6. 多模型问题：不同 provider API 协议不同，但 runtime 不应该到处写 provider-specific 逻辑。

所以 Pico 的目标不是做一个花哨的 AI 产品，而是做模型外面的工程 harness。

### 5.2 总体架构

Pico 是一个 Python package，`pyproject.toml` 里暴露了 `pico` 和 `pico-cli` 两个 console script，推荐用 `pico-cli`，因为 macOS 系统里可能已有 `/usr/bin/pico` 编辑器。

整体路径可以这样讲：

```text
pico-cli
  -> cli.py 解析参数、加载配置、选择 provider、构造 runtime
  -> runtime.Pico 组装 session、run store、memory、repo map、recovery、tool executor
  -> agent_loop.AgentLoop 执行主循环
  -> context_manager.ContextManager 构造 prompt
  -> model_client.complete 调模型
  -> model_output_parser 解析 tool / final / retry
  -> tool_executor.ToolExecutor 执行工具并记录策略和副作用
  -> run_store / checkpoint_store 写 task_state、trace、report、checkpoint
```

面试里可以强调：Pico 把“模型推理”和“工程执行”分开。模型只负责决定下一步，真正的工具调用和文件修改都在 runtime 策略下完成。

### 5.3 控制循环

`AgentLoop.run()` 是主循环。它可以概括成“感知 -> 决策 -> 行动 -> 记录”：

- 感知：刷新 workspace/context，构造 prompt。
- 决策：调用模型，解析输出是 tool、retry 还是 final。
- 行动：如果是 tool，交给 `ToolExecutor` 执行。
- 记录：写 history、task_state、trace、checkpoint、verification evidence。

代码里 `pico/agent_loop.py:49-54` 的注释也正是这个流程。这个点非常适合面试讲，因为它把 agent 系统从“玄学调用大模型”变成了一个有限状态控制循环。

### 5.4 工具执行与安全策略

Pico 的工具不是动态任意调用，而是在 `pico/tools.py` 里显式注册。核心工具包括：

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

每个工具都有 schema、risky 标记和 description。`ToolExecutor.execute()` 会依次做：

1. allowed tools 检查。
2. 工具是否存在。
3. 参数校验。
4. repeated tool call 拦截。
5. `run_shell` 的 command risk classification。
6. approval policy 判断。
7. 对 workspace-write 工具创建 pending Tool Change Record。
8. 执行工具。
9. 捕获 affected paths 和 shell side effects。
10. finalize Tool Change Record。

这个设计的重点是：模型不能直接操作系统或文件，必须经过工具边界；工具边界同时承担校验、审批、记录和恢复元数据采集。

### 5.5 可恢复编辑

Recoverable Editing 是项目里最适合深挖的模块。Pico 不把恢复建立在 trace 上，也不直接依赖 Git rollback，而是有独立 checkpoint store。

核心概念：

- Tool Change Record：一次工具调用对 workspace 的影响。
- Turn Checkpoint：一次用户请求结束后，把这一轮的 Tool Change Record 打包成用户可见恢复入口。
- File-State Blob：文件内容按 hash 存储的原始字节。
- Restore Preview：恢复前先生成 plan，不直接写盘。
- Restore Conflict：当前文件状态和 expected hash 不一致时，不自动覆盖。

恢复流程可以这样讲：

1. 执行写工具之前，Pico 对候选路径捕获 before 状态。
2. 执行后，通过 workspace observer 或 path snapshot 判断 affected paths。
3. 对可 snapshot 的文件记录 before/after hash 和 blob 引用。
4. run 结束时生成 Turn Checkpoint。
5. 用户要恢复时，先 `preview_restore()`，逐条判断 restore、review、conflict。
6. 只有 decision 是 restore 的条目才会应用。
7. 应用时 `_write_bytes_verified()` 先写临时文件，校验 hash，再原子 replace，最后再次读回校验。
8. 恢复完成后写一份 Restore Checkpoint，保留恢复 provenance。

面试重点：这里不是“撤销按钮”，而是一个保守的文件状态恢复系统。它宁可进入 review，也不自动合并冲突。

### 5.6 上下文管理

`ContextManager` 负责 prompt 预算控制。它不是简单拼字符串，而是把 prompt 分成几个 section：

- prefix：工具说明、系统规则、项目结构、memory guidance。
- history：workspace volatile state、checkpoint 状态、历史消息。
- current_request：当前用户请求。

默认总预算是 15000 字符级别，prefix 和 history 有独立预算。超预算时，优先压缩 history，再压缩 prefix，当前用户请求不裁剪。这是一个很重要的取舍：旧上下文可以损失，当前任务不能损失。

另外它会把 memory index 和 project structure 放进上下文，让模型知道“有哪些记忆文件可以查”和“仓库大概长什么样”，但不把完整记忆和完整 repo map 全塞进 prompt。

### 5.7 记忆系统

Pico 的记忆 v2 分三层：

1. `AGENTS.md`：项目约定，每次 session 开始会读，用来承载团队规则。
2. `.pico/memory/notes/*.md`：用户手写笔记，agent 可读。
3. `.pico/memory/agent_notes.md`：agent 在用户明确要求记住时追加的短笔记。

这个设计的价值在于：不同信息有不同权限和生命周期。项目约定、用户笔记、agent 追加经验不混在一个文件里。

检索采用 BM25 + CJK bigram。这个选择很朴素，但可审计、stdlib-only、容易测试。它不解决语义同义词问题，比如“身份认证”不一定命中“auth”，所以面试里不要把它吹成 semantic memory。

RepoMap 则是另一个辅助上下文工具：Python 用 AST 提取 class/function/method，TS/JS/Go/Rust 用正则 best-effort。它不塞进 prompt，而是通过 `repo_lookup` 按需查询。

### 5.8 Provider 适配

Pico 支持多 provider，但 runtime 尽量只关心统一的 model client 形状：

- `complete(prompt, max_new_tokens, **kwargs)`
- 可选 `stream_complete(...)`
- 可选 `supports_prompt_cache`
- `last_completion_metadata`

支持的后端包括：

- Ollama
- OpenAI-compatible Responses API
- Anthropic-compatible Messages API
- DeepSeek Anthropic-compatible API

这个模块的主要难点不是 HTTP 请求本身，而是各 provider 的返回格式、usage/cache metadata、错误路径、streaming 能力都不一样。Pico 的方向是把差异压在 provider adapter 内部，不让 `AgentLoop` 和 `ToolExecutor` 到处关心具体协议。

### 5.9 Run artifacts 与证据链

Pico 每次用户请求会生成 run directory：

```text
.pico/runs/<run_id>/
  task_state.json
  trace.jsonl
  report.json
```

这三个文件分工不同：

- `task_state.json`：当前状态机快照。
- `trace.jsonl`：append-only 事件序列，记录 prompt、model、tool、checkpoint、verification 等。
- `report.json`：最终 summary，适合复盘。

注意：run artifacts 是审计证据，不是恢复真相。真正恢复文件内容依赖 `.pico/checkpoints/` 下的 checkpoint/tool_change/blob。这是一个很重要的系统边界。

### 5.10 质量与 benchmark

项目里有本地门禁：

```bash
./scripts/check.sh
```

它运行：

```bash
uv run ruff check .
uv run pytest -q
```

此外还有：

- memory-quality benchmark：用 fake/live 模式跑记忆工具 trace scoring。
- provider benchmark：支持选择 `gpt`、`claude`、`deepseek` 或 `all`。
- review-pack：记录当前 snapshot、benchmark evidence、smoke command。

面试里可以说：这个项目不是只靠“能跑一次”证明，而是通过 tests、trace、report、benchmark、review-pack 把行为变成可复查证据。

## 6. 模块级拆解

### 6.1 CLI Surface

做什么：

CLI 负责把用户输入变成明确 command，例如 `run`、`repl`、`doctor`、`config`、`checkpoints`、`memory` 等。它还负责加载 `.env`、选择 provider、设置 approval policy、构建 `Pico` runtime。

为什么存在：

coding agent 的入口不能只有“prompt 字符串”。用户需要能检查状态、诊断 provider、查看 run、恢复 checkpoint、管理 memory。CLI Surface 是用户和 harness 能力之间的明确边界。

代码锚点：

- `pyproject.toml:12-14` 注册 `pico` 和 `pico-cli`。
- `pico/cli.py` 负责参数解析、provider 构造和 command dispatch。
- `pico/cli_recovery.py` 负责 runs/sessions/checkpoints。
- `pico/cli_memory.py` 负责 memory 子命令。

设计取舍：

保留 bare prompt 兼容，但把显式子命令作为主入口。这样既兼容旧习惯，又让 CLI 更适合脚本和诊断。

面试官追问：

为什么不直接做成一个 REPL？

强回答：

REPL 只能覆盖交互使用，但 harness 还需要诊断、恢复、配置查看、run artifact 检查、checkpoint preview、memory 管理等非聊天能力。显式 CLI command 能让这些能力脚本化，也能避免把所有行为藏在聊天命令里。

### 6.2 Runtime / Pico

做什么：

`Pico` 是 runtime facade，负责组装 session store、run store、checkpoint store、tool change recorder、recovery manager、workspace observer、memory store、retrieval、repo map、tools、tool executor、context manager。

为什么存在：

agent harness 需要很多协作组件。`Pico` 的作用是把它们 wire 起来，并提供 `ask()`、`execute_tool()`、`emit_trace()`、`record_verification_evidence()` 等统一操作面。

代码锚点：

- `pico/runtime.py:68-160` 是 `Pico.__init__()` 的主要 wiring。
- `pico/runtime.py:103-120` 初始化 run store、checkpoint store、tool change recorder、recovery manager。
- `pico/runtime.py:133-151` 初始化 memory v2、repo map、tools、tool executor、context manager。

设计取舍：

优点是所有 runtime 依赖在一个地方组装，调试容易。缺点是 `runtime.py` 会变大，有 God Object 趋势。面试里可以主动说这是后续可拆分点，例如拆成 `MemoryFacade`、`RecoveryFacade`、`SecurityFacade`、`ToolFacade`。

面试官追问：

`Pico` 类是不是职责太多？

强回答：

是的，这个风险存在。目前它更多是 runtime composition root，很多具体逻辑已经拆到 `AgentLoop`、`ContextManager`、`ToolExecutor`、`RecoveryManager`、`BlockStore` 等模块。后续优化方向不是在 `Pico` 里继续堆逻辑，而是把它收敛成依赖注入和 facade，减少薄委派方法。

### 6.3 AgentLoop

做什么：

`AgentLoop` 是一次用户请求的控制循环。它负责创建 task state、开始 run、构造 prompt、请求模型、解析输出、执行工具、记录 trace、创建 checkpoint、最终写 report。

代码锚点：

- `pico/agent_loop.py:20-38` 创建 run、task_state、发出 `run_started` trace。
- `pico/agent_loop.py:49-54` 明确注释“感知 -> 决策 -> 行动 -> 记录”。
- `pico/agent_loop.py:55-130` 是每轮 prompt/model/parse 的主干。
- `pico/agent_loop.py:257-287` `_finish_run()` 写 checkpoint、trace、report。

设计取舍：

`AgentLoop` 不直接执行工具细节，不直接做 recovery 细节，而是调 `agent.execute_tool()` 和 helper。这让主循环可读，但仍然保留对任务状态和 trace 的掌控。

面试官追问：

为什么要记录 `model_turn`、`tool_started`、`tool_finished` 这些 trace？

强回答：

因为 agent 系统的错误经常不是最终答案错，而是中间过程错。trace 能回答“模型当时看到了什么 metadata”、“请求了哪个工具”、“工具是否被拒绝”、“哪个 checkpoint 被创建”、“verification 是否执行”。没有 trace，很难复现一次 agent 失败。

### 6.4 ContextManager

做什么：

按预算组装 prompt，将 stable prefix、memory guidance、project structure、memory index、workspace volatile state、checkpoint text、history 和 current request 组合起来。

代码锚点：

- `pico/context_manager.py:13-17` 默认预算。
- `pico/context_manager.py:20-32` memory 使用/读取 guidance。
- `pico/context_manager.py:105-163` 构造 prompt section。
- `pico/context_manager.py:184-188` 超预算时优先压缩 history，当前请求不裁剪。

设计取舍：

当前请求永远是最高优先级。历史记录和 prefix 可以裁剪，因为它们是辅助上下文；当前请求是本轮任务本身，裁掉会让模型误解目标。

面试官追问：

为什么不把 repo map 和所有 memory 都塞进 prompt？

强回答：

全塞进去会破坏 prompt 预算，也会降低缓存稳定性。Pico 的做法是把 memory index 和项目结构作为索引放进 prompt，让模型知道有什么可查；真正需要细节时通过 `memory_read`、`memory_search`、`repo_lookup` 工具按需获取。

### 6.5 Tool Registry / ToolExecutor

做什么：

工具注册表定义模型可申请的动作，`ToolExecutor` 负责校验、审批、执行、记录和 recovery metadata。

代码锚点：

- `pico/tools.py:26-82` 显式工具定义。
- `pico/tools.py:110-121` 构造工具 registry。
- `pico/tool_executor.py:107-160` allowlist、unknown tool、参数校验、重复调用拦截。
- `pico/tool_executor.py:165-197` `run_shell` 命令风险分类和 approval requirement。
- `pico/tool_executor.py:215-234` 创建 pending Tool Change Record 并捕获 before snapshot。

设计取舍：

工具是白盒、显式、可测试的，而不是让模型输出任意 shell。`run_shell` 仍然存在，但它经过风险分类和 approval policy。

面试官追问：

为什么不直接让模型输出 bash？

强回答：

直接 bash 最大的问题是边界不可审计。Pico 的工具层能知道模型是在读文件、写文件、patch 文件还是跑命令，并针对不同动作做参数校验、风险分类、恢复记录和 trace metadata。bash 可以作为工具之一，但不能成为唯一执行接口。

### 6.6 Safe Execution

做什么：

通过 command risk class、approval policy、read-only mode、路径校验、secret redaction、shell env allowlist 等方式约束模型发起的动作。

核心设计：

- `run_shell` 先分类：read_only、workspace_write、destructive、external_effect。
- 不同 risk class 进入不同 approval decision。
- shell command 使用过滤后的环境变量，不直接继承完整父进程环境。
- tool result 和 artifact 经过 redactor。

设计边界：

这不是 OS sandbox。它不能替代系统级隔离。它是一层 harness policy 和审计机制。

面试官追问：

没有 OS sandbox，安全性是不是不够？

强回答：

如果目标是执行不可信代码，确实不够，应该引入 OS sandbox、container、seccomp、macOS seatbelt 或远程隔离环境。Pico 当前阶段的目标是本地 developer tool 的可审计执行，因此先做策略分类、approval、路径边界、redaction 和 trace。这个取舍让实现轻量，也便于测试，但我会明确它不是强安全沙箱。

### 6.7 Recoverable Editing

做什么：

记录 agent 对 workspace 的修改，使用户可以预览和保守恢复。

代码锚点：

- `pico/recovery_manager.py:31-61` `preview_restore()` 生成 restore plan。
- `pico/recovery_manager.py:63-96` `_plan_entry()` 判断 restore/review/conflict。
- `pico/recovery_manager.py:98-180` `apply_restore()` 应用可恢复条目并记录 provenance。
- `pico/recovery_manager.py:210+` `_write_bytes_verified()` 写临时文件、校验 hash、replace、读回校验。
- `pico/tool_executor.py:215-234` 工具执行前创建 pending record 和 before snapshot。

设计取舍：

恢复是文件级，不是 hunk 级；遇到当前文件 hash 不匹配，不自动 merge；恢复完成会写新的 Restore Checkpoint，不改历史。

面试官追问：

为什么不用 Git 直接恢复？

强回答：

Git 是很好的 review context，但不是这个 harness 的恢复真相。原因是 agent 可能操作未跟踪文件，用户工作区可能本来就 dirty，Git HEAD 不一定等于工具执行前状态。Pico 记录的是工具调用前后的文件状态和 hash，能更准确表示 agent 自己造成的变化。Git 可以辅助展示差异，但不应该是唯一恢复机制。

### 6.8 Memory v2

做什么：

把跨会话知识分成项目约定、用户笔记和 agent 笔记，并提供 list/read/search/save 工具。

代码锚点：

- `pico/memory/block_store.py:39-52` list workspace/user memory files。
- `pico/memory/block_store.py:103-127` append agent note，限制长度并原子写。
- `pico/memory/block_store.py:138-155` 路径解析和 symlink escape 检查。
- `pico/memory/retrieval.py:29-41` 英文词和 CJK bigram tokenizer。
- `pico/memory/retrieval.py:55-80` BM25 search。

设计取舍：

选择 lexical retrieval，而不是 embedding。优点是 stdlib-only、可审计、可测试、行为可解释；缺点是不理解语义等价。

面试官追问：

这算长期记忆吗？

强回答：

它是 durable local memory，但不是语义长期记忆系统。它能持久保存项目约定、用户 notes 和 agent notes，并通过关键词检索找回；但它没有 embedding、没有语义聚类、没有自动遗忘策略。准确说，它是可审计的本地知识层。

### 6.9 RepoMap

做什么：

为 `repo_lookup` 提供符号索引，让模型不用盲目全文搜索就能定位类、函数和方法。

代码锚点：

- `pico/repo_map.py:1-8` 说明 Python AST、其他语言 best-effort。
- `pico/repo_map.py:24-31` 忽略目录和扫描上限。
- `pico/repo_map.py:87-117` scan / refresh_if_stale。

设计取舍：

Python 精确解析，其他语言正则兜底。RepoMap 不塞进 prompt，只通过工具按需查询。

面试官追问：

为什么不用 LSP？

强回答：

LSP 更强，但依赖重、配置复杂、跨语言环境成本高。Pico 的目标是轻量 stdlib-first harness，所以第一阶段用 AST/regex 构建足够可用的符号索引。后续如果要提升准确性，可以按语言接入 tree-sitter 或 LSP。

### 6.10 Provider Adapters

做什么：

封装不同模型 API，给 runtime 一个统一 `complete()` 接口。

代码锚点：

- `pico/providers/clients.py` re-export provider clients 和 `FakeModelClient`。
- `pico/providers/openai_compatible.py` OpenAI-compatible Responses。
- `pico/providers/anthropic_compatible.py` Anthropic-compatible Messages。
- `pico/providers/ollama.py` Ollama。
- `pico/evaluation/provider_benchmark.py:10-40` provider benchmark selection。

设计取舍：

用 stdlib `urllib`，减少运行时依赖。代价是 HTTP ergonomics、retry、streaming 处理要自己维护。

面试官追问：

多 provider 的抽象难点在哪里？

强回答：

难点不只是 endpoint 不同，而是响应结构、usage metadata、cache metadata、错误码、streaming 协议、tool output 风格都不同。Pico 的策略是 runtime 只关心文本输出和少量 metadata，协议差异尽量留在 adapter 内部。

### 6.11 RunStore / Trace / Report

做什么：

每次 run 生成独立审计工件。

代码锚点：

- `pico/run_store.py:22-41` 定义 run_dir、task_state、trace、report 路径。
- `pico/run_store.py:43-49` 每次 run 创建目录。
- `pico/run_store.py:57-65` trace 使用 JSONL append。
- `pico/run_store.py:79-93` task_state/report 原子写。

设计取舍：

把 session continuity 和 run audit 分开。session 负责继续对话，run artifacts 负责复盘一次请求，checkpoint store 负责恢复真相。

面试官追问：

为什么 trace 用 JSONL？

强回答：

agent 运行过程是事件序列，JSONL 支持逐条 append，运行中也能看到已发生事件。相比最后一次性写完整 JSON，JSONL 更适合调试、恢复中断和后续聚合分析。

### 6.12 Benchmark / Evaluation

做什么：

提供可复查的 release evidence，而不是只靠人工试用。

模块：

- `benchmarks/memory_quality/run_benchmark.py`：memory 工具 trace scoring。
- `pico/evaluation/provider_benchmark.py`：provider benchmark selector 和 provider summary。
- `pico/evaluation/fixed_benchmark.py`：固定任务 benchmark。
- `docs/review-pack/README.md`：记录当前 local snapshot 和 smoke command。

设计取舍：

fake mode 用来证明 harness 行为和 scoring path，live mode 才能观察真实模型表现。两者不能混淆。

面试官追问：

fake benchmark 有什么意义？

强回答：

fake benchmark 不证明 LLM 智能，但能证明 harness 链路：模型输出工具调用后，Pico 能执行 memory tools，写 trace，读取 run artifacts，并按场景评分。这是 release gate 里很重要的一层，因为它稳定、可复现、不依赖外部 quota。

## 7. 核心技术难点与回答模板

### 7.1 难点一：模型输出不可控，如何变成可控执行

问题本质：

LLM 输出是文本，不可靠。直接执行文本会有安全和可恢复问题。

Pico 的方案：

- 约束模型输出为 tool call 或 final answer。
- 工具名来自显式 registry。
- 参数必须 validate。
- repeated identical call 会被拒绝。
- risky 工具必须通过 approval。
- 执行结果进入 trace 和 history。

强回答：

我把模型看成 planner，而不是 executor。模型只能申请动作，真正执行动作的是 harness。这样系统可以在工具层做验证、审批、记录和恢复，而不是相信模型输出天然正确。

### 7.2 难点二：如何让文件改动可恢复

问题本质：

agent 可能误改文件。只记录“它执行了 write_file”不够，必须知道改了哪个文件、改前是什么、改后是什么、当前是否还能安全恢复。

Pico 的方案：

- 写工具前捕获候选路径 before 状态。
- 写工具后比较 affected paths。
- 对 eligible 文件存 before/after blob。
- run 结束生成 Turn Checkpoint。
- restore 前 preview。
- 当前 hash 不匹配则 conflict。
- restore 后写 Restore Checkpoint。

强回答：

我没有把 trace 当恢复依据，因为 trace 只说明发生过什么，不保存可靠文件状态。恢复要依赖独立 checkpoint store 和内容 hash。这样即使用户后续手改了文件，restore 也能检测 hash mismatch，避免盲目覆盖。

### 7.3 难点三：如何管理 prompt budget

问题本质：

仓库上下文、历史消息、工具说明、记忆内容都想进 prompt，但预算有限。

Pico 的方案：

- section 化：prefix、history、current_request。
- stable prefix 和 volatile workspace state 分开。
- memory index 和 project structure 是索引，不是全文。
- 超预算先压缩 history，再压缩 prefix。
- 当前请求不裁剪。

强回答：

我没有追求“上下文越多越好”，而是把上下文分成不同优先级。当前请求是硬约束，历史是软上下文，memory/repo map 尽量走索引加按需工具查询。这样更稳定，也更利于 prompt cache。

### 7.4 难点四：如何设计记忆系统

问题本质：

agent 记忆如果无限制写入，很容易污染上下文；如果完全没有记忆，又无法跨 session 复用项目经验。

Pico 的方案：

- `AGENTS.md` 放项目约定。
- `notes/*.md` 放用户手写知识。
- `agent_notes.md` 只 append 短笔记。
- `memory_save` 只应在用户明确要求时使用。
- 检索用 BM25 + CJK bigram。

强回答：

我更关注记忆的权限和可审计性，而不是一开始就做复杂语义记忆。用户手写 notes 和 agent 追加 notes 分开，能避免 agent 覆盖用户知识；append-only 也更容易复盘来源。

### 7.5 难点五：如何证明 agent harness 可信

问题本质：

agent 系统一次跑通不代表稳定。需要测试核心不变量。

Pico 的方案：

- `scripts/check.sh` 统一 ruff + pytest。
- tests 覆盖 CLI、tool executor、recovery、memory、provider、security invariants。
- run artifacts 记录 task_state/trace/report。
- memory-quality benchmark 用 deterministic fake mode。
- provider benchmark 支持单 provider 选择，降低 smoke 成本。

强回答：

我把“可信”拆成两层：一层是本地不变量测试，比如路径越界、restore conflict、memory tool 行为；另一层是运行证据，比如 trace/report/benchmark。这样问题发生时可以定位是模型输出问题、工具执行问题，还是 harness 状态管理问题。

## 8. 成果与证据怎么讲

### 8.1 可以讲的成果

- 完成了一个可运行的 Python CLI coding-agent harness。
- 形成了清晰的模块边界：CLI、runtime、agent loop、context manager、tool executor、recovery、memory、provider、evaluation。
- 实现了本地 run artifacts：task_state、trace、report。
- 实现了文件级 recoverable editing：checkpoint、tool change、blob、restore preview、conflict detection。
- 实现了 memory v2：项目约定、用户 notes、agent notes、BM25/CJK 检索、RepoMap。
- 支持多 provider，并提供 fake model client 用于测试。
- 提供本地质量门禁和 benchmark evidence。

### 8.2 不要讲的成果

- 不要说它已经有大量真实用户。
- 不要说它已经线上稳定运行。
- 不要说它完全防止危险命令。
- 不要说它能恢复任何文件改动。
- 不要说它有语义记忆。
- 不要说 benchmark 证明模型能力很强。fake benchmark 主要证明 harness 行为。

### 8.3 面试表达模板

可以这样说：

“这个项目当前更像一个工程可信度较强的本地 harness。它的价值不是模型本身，而是把模型执行变成一条可审计链路。每次任务有 trace，每次文件修改有 recovery metadata，每个工具有风险策略，每个记忆入口有权限边界。它还不是商业产品，也没有 OS sandbox，但作为本地 coding-agent 运行时，核心闭环已经比较完整。”

## 9. 项目亮点

### 9.1 亮点一：把 agent 做成控制循环，而不是聊天壳

很多 agent demo 是 prompt + API。Pico 有明确状态机：

- task state
- attempts
- tool steps
- stop reason
- trace events
- checkpoint ids
- report

面试价值：

这说明你理解 agent 工程化的核心不在“接 API”，而在运行时状态管理。

### 9.2 亮点二：工具边界清晰

工具显式注册，schema 清楚，risky 标记明确。模型不能随便调用任意动作。

面试价值：

这体现了你对 LLM tool calling 的工程边界有理解。

### 9.3 亮点三：恢复系统不依赖幻想

恢复不靠“模型再生成补丁”，也不把 Git 当唯一真相，而是记录文件状态、hash 和 checkpoint。

面试价值：

这体现了你对失败恢复、状态一致性和用户工作区保护的理解。

### 9.4 亮点四：记忆系统强调权限和可审计

不是把所有内容都丢进向量库，而是区分项目约定、用户笔记、agent 笔记。

面试价值：

这能表现你对“长期记忆”这个概念的克制，知道什么时候不该过度设计。

### 9.5 亮点五：证据链完整

run artifacts、checkpoint store、tests、benchmark、review-pack 形成闭环。

面试价值：

这说明你不是只做功能，还考虑怎么证明功能、怎么复盘问题。

## 10. 项目短板与诚实回答

### 10.1 `runtime.py` 和 `tool_executor.py` 偏大

怎么说：

目前 `Pico` 是 composition root，同时保留了一些 facade 方法，所以文件会偏大。`ToolExecutor` 也承担工具执行和 recovery metadata finalization，后续可以拆出 side-effect recorder。

不要说：

“没有问题，这样挺好。”

更好说法：

“这是一个可维护性压力点。我目前的判断是先保证行为闭环和测试，再逐步把 runtime facade、tool side-effect recorder、security facade 拆出来。”

### 10.2 Safe Execution 不是 OS sandbox

怎么说：

当前是 command policy + approval + redaction + path validation，不是强隔离。如果要跑不可信代码，需要引入系统级 sandbox 或容器。

### 10.3 Restore 是保守文件级恢复

怎么说：

它不做 hunk-level merge，不自动合并冲突。这样牺牲便利性，但能避免误覆盖用户后续修改。

### 10.4 Memory 是 lexical，不是 semantic

怎么说：

BM25 + CJK bigram 的优势是可解释和 stdlib-only，缺点是不理解语义同义词。后续可以引入 embedding，但需要处理隐私、成本、索引更新和评估。

### 10.5 Provider 抽象还可以更规范

怎么说：

当前 runtime 通过 `complete()` 和 optional attributes 适配多 provider。后续可以引入显式 Protocol/ABC，统一错误分类、retry、stream metadata。

## 11. 面试官追问题库

### 11.1 项目动机

问题：你为什么做这个项目？

面试官想看：

你是不是只是在包装 API，还是理解 coding agent 落地的工程问题。

强回答：

“我做它是因为 coding agent 真正难的是模型外面的执行可信度。模型生成代码只是第一步，后面还有上下文预算、工具调用约束、文件改动追踪、失败恢复、trace 复盘和多 provider 适配。Pico 是围绕这些问题做的本地 harness。”

弱回答：

“因为现在 AI 很火，我就做了一个代码助手。”

### 11.2 架构拆分

问题：Pico 的核心模块怎么拆？

强回答：

“入口是 CLI，runtime 负责依赖组装，AgentLoop 负责控制循环，ContextManager 负责 prompt 预算，ToolExecutor 负责工具策略和执行，RecoveryManager 负责恢复预览和应用，memory/* 负责记忆存储和检索，providers/* 负责协议适配，run_store/checkpoint_store 分别负责审计工件和恢复真相。”

### 11.3 控制循环

问题：一次用户请求的完整流程是什么？

强回答：

“CLI 构造 Pico runtime 后，AgentLoop 创建 TaskState 和 run directory，写 run_started trace；每轮构造 prompt，调用 model client，解析输出。如果是 tool，就由 ToolExecutor 校验、审批、执行、记录 tool trace 和 tool-change；如果是 final，就写 final answer、checkpoint、report。中间任何 model error、step limit、retry limit 都会走 terminal finalization。”

### 11.4 Prompt 设计

问题：你的 prompt 怎么控制长度？

强回答：

“我把 prompt 分 section：stable prefix、memory guidance、project structure、memory index、workspace state、history、current request。预算超了先裁 history，再裁 prefix，当前请求不裁剪。这样保证任务目标不丢，同时尽量保持 stable prefix 可缓存。”

### 11.5 工具调用安全

问题：模型怎么调用工具？如何防止危险行为？

强回答：

“模型只能输出已注册工具。ToolExecutor 会检查工具是否允许、参数是否合法、是否重复调用。run_shell 会先做 command risk classification，再根据 approval policy 决定允许、拒绝或要求人工确认。risky 工具还会走 approve。执行结果会被 trace 和 recovery 记录。”

### 11.6 路径安全

问题：如果模型试图读写 workspace 外的文件怎么办？

强回答：

“路径解析走 workspace-relative 规则，会拒绝绝对路径、`..` 穿越和 symlink escape。memory store 也有自己的 scope root 检查。这样工具层能把文件访问限制在 workspace 或 memory scope 内。”

### 11.7 Recovery

问题：agent 改坏文件后怎么恢复？

强回答：

“写工具执行前捕获 before 状态，执行后计算 affected paths，存 Tool Change Record 和 file entries，run 结束生成 Turn Checkpoint。恢复时先 preview，逐条判断 restore/review/conflict。只有当前 hash 和 expected hash 匹配的条目才自动恢复，冲突不覆盖。”

### 11.8 为什么不用 Git

问题：为什么不直接 Git checkout？

强回答：

“Git HEAD 不一定等于工具执行前状态，尤其用户工作区可能已经 dirty，也可能有 untracked 文件。Pico 要恢复的是 agent 造成的变化，而不是回到某个 commit。所以它记录工具执行前后的文件状态和 hash，Git 只作为 review context。”

### 11.9 Trace 和 Checkpoint 区别

问题：trace 和 checkpoint 有什么区别？

强回答：

“trace 是事件时间线，用来复盘发生了什么；checkpoint 是恢复状态，用来决定能不能恢复文件。trace 不应该承担恢复真相，因为它可能只有摘要，不一定有完整文件字节。”

### 11.10 Memory

问题：Pico 的 memory 设计是什么？

强回答：

“分三层：`AGENTS.md` 是项目约定，`notes/*.md` 是用户手写知识，`agent_notes.md` 是 agent 在用户明确要求下追加的短笔记。检索用 BM25 + CJK bigram，RepoMap 提供符号查询。这样 memory 是可审计本地知识层，不是黑盒向量库。”

### 11.11 为什么不用 embedding

问题：为什么不用向量数据库？

强回答：

“第一阶段我更重视可解释性、依赖成本和测试稳定性。BM25 + CJK bigram 能覆盖很多项目笔记场景，结果可解释，也不需要额外服务。embedding 可以作为后续增强，但要处理隐私、成本、更新和评估问题。”

### 11.12 Provider

问题：怎么支持多个模型后端？

强回答：

“provider adapter 把 OpenAI-compatible、Anthropic-compatible、DeepSeek、Ollama 的协议差异封装起来，runtime 只依赖 `complete()` 和少量 metadata。不同 provider 的 usage/cache/streaming/error 差异留在 adapter 内部处理。”

### 11.13 测试

问题：怎么证明系统可靠？

强回答：

“本地门禁是 `./scripts/check.sh`，包含 ruff 和 pytest。测试覆盖 CLI、tool executor、recovery manager、checkpoint store、memory tools、provider clients、security invariants 等。除此之外，run artifacts 和 benchmark 提供运行证据，memory-quality fake mode 能稳定验证 memory tools 和 trace scoring。”

### 11.14 性能

问题：这个系统会不会很慢？

强回答：

“主要成本来自模型请求和文件扫描。Pico 做了几个控制：RepoMap 有忽略目录和扫描上限，delegate 不重复触发全仓扫描；ContextManager 做预算压缩；run artifacts 是增量 append trace；provider benchmark 支持单 provider，降低 smoke 成本。后续可以继续优化 session 重写、HTTP 连接复用和 large repo indexing。”

### 11.15 如果重做

问题：如果重做，你会怎么改？

强回答：

“我会保留控制循环、显式工具、recovery store 和 memory 分层这些方向。会优先改三点：第一，把 `Pico` facade 进一步拆成更小的 service；第二，把 ToolExecutor 里的 side-effect recording 抽出去；第三，给 provider client 定义显式 Protocol，并统一错误分类和 retry 策略。”

## 12. 面试回答中的高频坑

### 12.1 不要把 AI 能力讲成工程能力

弱说法：

“模型很聪明，所以可以自动改代码。”

强说法：

“模型负责提出下一步动作，harness 负责限制、执行、记录和恢复。”

### 12.2 不要把 trace 和恢复混为一谈

弱说法：

“trace 里有记录，所以可以恢复。”

强说法：

“trace 用于审计，恢复依赖 checkpoint store 和 file-state blob。”

### 12.3 不要过度承诺安全

弱说法：

“我们能防止所有危险命令。”

强说法：

“当前是策略和审批层，不是 OS sandbox。它能降低本地 developer tool 的误操作风险，但不用于执行不可信代码。”

### 12.4 不要把 BM25 说成语义记忆

弱说法：

“它能理解用户长期记忆。”

强说法：

“它是可审计的本地 durable memory，检索是 lexical BM25 + CJK bigram。”

### 12.5 不要说 benchmark 证明模型强

弱说法：

“fake benchmark 说明模型记忆能力很好。”

强说法：

“fake benchmark 证明 harness 能按预期执行 memory tools、写 trace、评分；live benchmark 才观察真实 provider 行为。”

## 13. 自我介绍可嵌入版本

可以在自我介绍中这样嵌入：

“我最近主要做的是一个叫 Pico 的本地 coding-agent harness。它的目标不是再做一个聊天界面，而是解决 LLM 在代码仓库里真实工作时的工程问题：上下文怎么组织、工具怎么受控、命令怎么审批、文件改动怎么记录和恢复、运行过程怎么 trace、跨会话知识怎么保存。这个项目让我对 agent 系统的 runtime、tool calling、安全策略、checkpoint 和 evaluation 有了比较完整的理解。”

## 14. 项目职责表达模板

### 14.1 如果你是主要作者

“这个项目主要由我设计和实现。我从 CLI 入口、runtime 控制循环、工具注册、上下文管理、可恢复编辑、记忆系统到 provider 适配和 benchmark 证据都做了完整闭环。过程中我重点关注的是 agent 行为的可控性和可审计性，而不是单纯堆模型能力。”

### 14.2 如果你是核心参与者

“我深度参与了这个项目的核心模块设计和实现，重点负责 runtime/tool/recovery/memory/evaluation 这几块。我主要做的是把模型调用后的工程边界补齐，例如工具执行策略、checkpoint 记录、restore preview、memory 分层和 benchmark 验证。”

### 14.3 如果你只是拿它作为学习项目

“我系统拆解和完善了这个本地 coding-agent harness，重点学习并整理了 agent runtime 的工程设计，包括控制循环、工具边界、上下文压缩、可恢复编辑、记忆系统和评估证据链。这个项目让我能从工程视角理解 coding agent，而不是停留在 prompt/API 层。”

## 15. 不同岗位的讲法

### 15.1 后端工程师

强调：

- 状态机和运行时。
- 工具执行策略。
- 文件恢复一致性。
- 本地持久化和原子写。
- 测试和质量门禁。

少讲：

- UI 和产品体验。
- 模型效果主观评价。

### 15.2 AI Infra / Agent 工程师

强调：

- LLM harness。
- Tool calling 边界。
- Prompt budget。
- Multi-provider adapter。
- Trace/report/evaluation。
- Recovery 和安全策略。

### 15.3 安全/平台方向

强调：

- Command risk class。
- Approval policy。
- Path boundary。
- Secret redaction。
- Run audit。
- OS sandbox 边界和后续计划。

### 15.4 简历筛选场景

强调：

- Python CLI。
- Agent runtime。
- 本地文件系统和恢复机制。
- Tests/benchmark。
- 多模型 provider。

## 16. 反问面试官的问题

如果面试官对这个项目感兴趣，你可以反问：

1. “如果你们团队做 coding agent，更看重模型能力、工具安全、还是回滚恢复？”
2. “你们现在是否有 agent run 的 trace/replay 机制？失败后怎么定位是模型问题还是工具问题？”
3. “对于本地 agent，你们会选择 OS sandbox、container，还是策略审批？为什么？”
4. “你们怎么管理跨 session 的项目知识，是放 prompt、文档、向量库，还是 memory tools？”
5. “在你们的场景里，recoverable editing 更需要文件级恢复还是 hunk 级恢复？”

这些反问能把话题引回你熟悉的 harness、trace、recovery、memory 边界。

## 17. 最终备忘卡

### 17.1 三个关键词

- Harness：模型外面的工程运行时。
- Recoverable Editing：agent 改动可检查、可预览、可恢复。
- Evidence：trace、report、checkpoint、tests、benchmark。

### 17.2 三条主线

1. 控制模型：显式工具、参数校验、risk class、approval。
2. 保护工作区：path boundary、Tool Change Record、checkpoint、restore conflict。
3. 证明行为：task_state、trace、report、tests、benchmark。

### 17.3 三个亮点

1. 不是聊天壳，而是 agent control loop。
2. 不是盲目改文件，而是 recoverable editing。
3. 不是黑盒记忆，而是可审计 memory surfaces。

### 17.4 三个边界

1. 不是 OS sandbox。
2. 不是 Git 替代。
3. 不是 semantic memory。

### 17.5 最后一段总结

Pico 最值得讲的地方，不是“我调用了某个大模型”，而是“我围绕大模型构建了一个本地工程 harness”。这个 harness 让模型在代码仓库里工作的每一步都有边界：看什么上下文、能调用什么工具、命令是否需要审批、文件改动如何记录、失败后如何复盘、哪些状态能恢复、哪些记忆能持久化。这样的项目能体现的不只是 AI API 使用能力，更是系统设计、状态管理、安全边界和工程验证能力。

