# Pony Coding Agent 工作流实施方案

> 状态：Historical / Superseded。本文记录已被 Session v5 permission mode/rules 与 Plan artifact 合同取代的旧实施方案，
> 不再描述当前产品行为；当前合同以领域模型、架构、Session、安全与验证文档为准。
>
> 文档性质：历史 P0 实施记录，不声明当前或后续 Gate 能力
> 基线：2026-07-18，`main@88c23a0ff961ff540ad3dbb4d5412b24c7f9daea`
> 实施状态：W0-W6 已在 integration 分支完成；以 W7 exact-HEAD 离线门禁为最终证据

本文对 Mode、Plan/Todo、Resume/Rewind、Model Target、持续交互、只读子任务、Skills、过程可见性与后续 IDE 接口
进行可行性分析，并给出可实际执行的 worktree 与集成方案。当前产品合同仍以[领域模型](domain-model.md)、
[架构](architecture.md)、[Session](context-and-sessions.md)、[恢复](recovery.md)、[安全](security.md)和
[验证](verification.md)为准；本文不能自行修改这些合同来批准尚未决策的能力。

## 1. 执行结论

原方案方向正确，但把四类风险不同的工作绑定成了一个 P0：

1. Workflow Mode 与 Plan 是 Agent Core 的单会话能力，可以近期做。
2. Resume 卡与 TUI 投影是上述状态的消费者，可以在状态合同稳定后做。
3. 多 Model Target 会改写唯一 `.env` 配置面和 Session Model Binding，必须先有产品/安全 ADR，不能阻塞近期闭环。
4. 输入队列、并行 delegate 与 Skills 分别涉及 TUI 并发、client 隔离和 Sandbox 信任边界，必须先做有退出条件的 spike。

因此采用以下路线：

| 层级 | 能力 | 结论 | 原因 |
| --- | --- | --- | --- |
| P0 | `plan/act/review` Workflow Mode | 做 | 可复用现有 Tool policy、approval、Session active path |
| P0 | Active Plan 与 `/plan` | 做 | 自动 checkpoint 不能表达 turn 内的显式多步骤进度 |
| P0 | `/mode` 与显式 `--mode` | 做 | 同时覆盖交互和 one-shot，不形成配置默认值 |
| P0 | Resume 卡、prompt history、状态投影 | 做 | 可从已有 Session/checkpoint 派生，不需新 Store |
| P0 | `/todo` | 不做别名 | `/plan` 已覆盖；第二名称增加命令和文档面，没有独立价值 |
| Gate M | Session-scoped `/model` | 已由 ADR-0047 收窄并完成 | 保留四变量配置，只允许相同 protocol/endpoint 切换 model |
| Gate Q | 忙碌时持久化输入队列 | 先 spike | 当前 TUI/Agent 是同步调用，approval 与退出语义尚未解决 |
| Gate D | 命名 delegate | 串行隔离与 worktree batch 已完成 | child 各有 worktree、branch、client、Session 与 Run |
| Gate S | 仓库 Skills | 已完成只读威胁模型与实现 | 仅 `.claude/skills` 受信读取；不执行脚本 |
| P2+ | JSONL/IDE/MCP adapter | 有真实 consumer 再做 | 无 consumer 时协议只会形成第二维护面 |

近期成功标准不是“拥有完整 AI IDE”，而是：用户能在同一 Provider、同一 Session 中可靠执行
`Plan -> Act -> Review -> Resume/Rewind`，状态只有一个真源，安全能力不会被 Mode 或 approval 绕过。

## 2. 当前实现事实与纠偏

### 2.1 可以直接复用的能力

- Session v3 是 append-only JSONL tree，active path 支持 Mode、Plan、fork、rewind、compaction 与 task checkpoint。
- Canonical Messages 是唯一 transcript；`ContextManager` 从 active Session view 构造请求。
- `ToolExecutor` 已集中处理 schema、allowlist、shell assessment、approval、mutation lock、effect observation 与 recovery。
- `assess_command()` 已能证明一小组 shell 命令只读，并将测试等解释器命令判为需要 approval 的外部 effect。
- REPL 与 TUI 共用 `pony.cli.start._process_repl_input`；斜杠菜单来自 `pony.cli.help.SLASH_COMMANDS`。
- Run trace 在 durable append 成功后才向 TUI listener 发送脱敏副本。
- `task_checkpoint` 已保存 goal、status、blocker、next steps、key files、freshness 与 runtime identity，可生成 Resume 事实。
- `RuntimeOptions` 是冻结的可选设置对象；`Pony` 公共构造合同无需改变。

### 2.2 不能按表面现状假设的能力

- `model_change` 已从 v3 entry contract 删除；含该 entry 的 v2 artifact 以 `unsupported_legacy_entry` 拒绝迁移，
  不能作为 Model Target 切换基础。
- `TaskState` 只描述一次 `ask()`；自动 checkpoint 在 turn 结束时推导，不能替代模型主动维护的 Active Plan。
- `read_only=True` 会拒绝所有 `run_shell`；它不能直接表示允许“已证明只读 shell”的 `plan/review`。
- 当前工具 schema 简写只可靠表达 string/integer，不能直接声明 object/array。P0 可用严格、bounded 的 JSON string
  承载 Plan；batch delegate 若继续推进，仍必须先补 provider-neutral schema 能力。
- 命名 delegate 以独立的 child client、Session root 与 Run root 串行执行；它不是 worker pool，不能并行或写入 worktree。
- 当前 TUI 同步执行 `session.prompt() -> agent.ask()`；Provider 使用阻塞式 `urllib` 且没有 cancel API，输入队列不是小改动。
- `.agents/` 不在 Sandbox 的 agent-control 排除集合中，会进入 filtered staging 与 diff capture。
- TUI 基线合同收束为单行 `PONY CODE · v<version>` 启动头，footer 不含绝对 cwd、Session ID、API Base
  或 checkpoint ID；Workflow UI 只在该安全投影上增加 Mode/approval。

### 2.3 责任模块纠偏

仓库没有 `pony/cli/repl.py`、`pony/tools/approval.py` 或 `pony/tools/shell_assessment.py`。真实 owner 是：

| 责任 | 当前 owner |
| --- | --- |
| Session format、active projection、migration、fork/rewind/clone | `pony/state/session_store.py` |
| Runtime state、reload/reset、delegate 装配 | `pony/runtime/application.py`、`pony/runtime/options.py` |
| REPL handler 与斜杠命令执行 | `pony/cli/start.py` |
| 斜杠命令 catalog | `pony/cli/help.py` |
| TUI prompt、completion、history、runtime hooks | `pony/tui/app.py` |
| TUI 状态与事件渲染 | `pony/tui/render.py` |
| Tool schema/registry | `pony/tools/registry.py`、`pony/tools/validation.py` |
| Mode、approval、shell policy 的最终执行边界 | `pony/tools/executor.py`、`pony/security/command_policy.py` |
| Context source 与 token allocation | `pony/context/sources.py`、`pony/agent/context_manager.py` |
| Trace schema、durable writer/listener 顺序 | `pony/agent/observability.py`、`pony/runtime/application.py` |
| Provider 配置与装配 | `pony/config/model.py`、`pony/config/environment.py`、`pony/cli/assembly.py` |

## 3. 近期目标架构

### 3.1 状态所有权

近期只新增两个 active Session state；它们可由三类 entry 投影，但不产生第二个 Store：

```text
Session active path
├── workflow_mode_change            -> workflow_mode
├── plan_update                     -> active_plan（human/reset/clone control）
└── successful update_plan exchange -> active_plan（从原子 tool pair 投影）
```

- `WorkflowMode` 是内部精确名称，值为 `plan`、`act`、`review`；UI 命令仍可用 `/mode`。
- `Plan` 是一个 bounded value object；TUI/CLI 只投影它，不创建 Todo Store 或 history 文件。
- checkpoint 不复制 Plan，也不新增 Plan writer；Resume renderer 在读取时组合 checkpoint 与 Active Plan 投影。
- Run/Trace 记录事件事实，不成为 Mode/Plan 的恢复真源。
- 通用 `session_info` 不得写 Mode/Plan。reset/clone 若需建立新 branch/session，必须追加上述显式 control entry，不能形成隐藏 writer。
- 新 Session 的格式默认值固定为 `act`；`/mode` 与只适用于 `run/repl` 的显式 `--mode` 追加同一种 control entry。
  Mode 不进入 `RuntimeOptions`、环境变量或 `pony.toml`；未显式指定时只从 active path 恢复。

### 3.2 控制流

```mermaid
flowchart TD
    U["Top-level user input"] --> S["Freeze immutable turn snapshot"]
    S --> C["Project WorkflowMode + Active Plan"]
    C --> R["One model request"]
    R --> A{"Exactly one Action"}
    A -->|"update_plan"| P["Validate and atomically commit tool exchange + Plan"]
    A -->|"other tool"| G["Policy, approval, execute, observe, commit tool pair"]
    A -->|"final"| F["Commit answer, then finalize task checkpoint"]
    P --> R
    G --> R
    F --> E["Project resume/UI state"]
```

仍保持：一个 Model Attempt 至多一次真实 request；成功 response 只产生一个 Tool、Final 或 Retry；同一 top-level turn 的
retry/follow-up 复用 immutable snapshot。Mode 只在 top-level turn 边界由用户命令修改，模型无权切换。

### 3.3 WorkflowMode 与 approval 正交

`WorkflowMode` 决定能力上限；`approval=ask|auto|never` 只决定上限内是否需要人工确认。计算顺序固定为：

```text
schema/path/sensitive checks
-> WorkflowMode ceiling
-> existing tool/shell policy
-> current-request Memory authority
-> approval
-> mutation lock and execution
```

| WorkflowMode | read-only tools | `update_plan` | workspace tools | `memory_save` | `run_shell` |
| --- | --- | --- | --- | --- | --- |
| `plan` | 允许 | 允许 | 拒绝 | 拒绝 | 仅 `risk_class=read_only`，之后仍走现有 approval |
| `act` | 允许 | 允许 | 走现有 policy/approval | 仍须当前请求明确授权 | 走现有 policy/approval |
| `review` | 允许 | 允许 | 拒绝 | 拒绝 | 只允许 `read_only` 或 `external_effect`；后者仅 `ask` 可确认 |

补充不变量：

- WorkflowMode 只能缩小能力，不能把现有 shell policy 的 `ask/reject` 提升为 `allow`。
- 既有 `RuntimeOptions.read_only=True` 是更强的内部上限：继续拒绝全部 `run_shell`、`session_state` 和写工具，任何 Mode 都不能放宽。
- `auto` 不能突破 `plan/review` 的 workspace 或 Durable Memory 禁令，也不能自动执行 `external_effect`。
- `never` 保持当前语义：所有 `run_shell` 都拒绝；不要借 WorkflowMode 放宽。
- `review + ask` 可确认测试等外部 effect，但执行后如 observer 发现 workspace change，必须按现有 Tool Change/Recovery 记录，
  并将该次行为标为 policy violation/review required；Mode 不能成为“不记录副作用”的借口。
- destructive、sensitive path、workspace-write shell 在 `plan/review` 直接拒绝，approval 无权提权。
- delegate 继续使用现有 `read_only=True + approval=never`，不继承父 WorkflowMode 的更高权限。
- Mode ceiling 约束模型发起的 Tool。用户显式输入的 `/remember`、`/checkpoint`、`/rewind`、`/clone` 等本地管理命令继续代表
  human authority 并保持既有确认/恢复语义；模型不能把它们作为文本输出触发，也不能借 Tool 间接调用。

实现上只在共享 `ToolExecutor` 根部增加一张静态 policy table；不增加 DSL、策略插件或第二 executor。
每个 top-level turn 同时冻结 Mode 对应的模型可见 Tool Schema；固定 prefix 不再列举静态工具名。隐藏不是授权边界，
Executor 必须继续对所有调用执行同一 Mode ceiling。

### 3.4 Active Plan 合同

不提供 `/todo` 别名。用户通过 `/plan` 查看或清空，模型通过 `update_plan` 完整替换同一 Active Plan：

```json
{
  "goal": "完成 Workflow Mode",
  "items": [
    {"id": "1", "text": "持久化 Mode", "status": "completed"},
    {"id": "2", "text": "执行 policy 矩阵", "status": "in_progress"}
  ]
}
```

约束在 Session 地基切片中锁定为常量并测试：

- canonical empty Plan 固定为 `{"goal":"","items":[]}`；除此之外，`goal` 必须 trim 后 1–300 chars，且必须有
  1–12 items。空 goal 与非空 items 的混合形态一律拒绝。
- canonical JSON 的 UTF-8 编码最多 12 KiB；大小限制在 decode 前后都检查，避免压缩/转义差异绕过上限。
- canonical serialization 固定字段顺序、UTF-8 与无多余空白；Plan digest 是该字节串的 SHA-256。
- `id`：`[A-Za-z0-9][A-Za-z0-9._-]{0,31}`，Plan 内唯一。
- `text`：trim 后 1–300 chars；`status` 只允许 `pending|in_progress|completed`。
- 同时最多一个 `in_progress`；未知字段、重复 JSON key、C0/DEL 控制字符和非字符串字段均拒绝。
- 更新是完整替换，不做 patch/merge、依赖图、负责人、优先级、截止时间或 blocker 子模型。
- 先做结构/输入上限检查，再走现有 sensitive-content gate；若 artifact redaction 会改变任一 Plan string，整次更新以
  `sensitive_content_block` 拒绝并保持旧 Plan，不为该工具放宽通用 action sanitizer。通过后再 canonicalize/revalidate；tool result
  只返回 bounded completed/total count 与 `sha256:<64hex>`，不回显原始文本或持久化前对象。
- `/plan` 显示；`/plan clear` 追加 canonical empty `plan_update`，不删除历史；模型用 `update_plan` tool 完整替换。
- `/reset` 在新 active branch 清空消息、current checkpoint selection 与 Active Plan，但保留历史 checkpoint entries 和用户选择的
  WorkflowMode；fork/rewind 从目标 active path 恢复二者。
- `clone --to-worktree` 复制 Active Plan 和 WorkflowMode，但清除 workspace-bound freshness/recovery，保持现有 clone 边界。

P0 的 `update_plan` 只接受一个 `plan_json: str`，在工具边界执行 bounded、duplicate-key-aware strict decode；这复用当前
string tool schema，不为一个调用方重写 schema 系统。验证和 sensitive-content gate 成功后，Session v3 从成功的原子
tool call/result 直接投影 Plan，不在 `tool_exchange.data` 再保存一份副本。只有 Gate D 获批、出现第二个真实
object/array tool 后，才评估
把所有工具统一迁移到 provider-neutral JSON Schema；届时删除 `plan_json`，不长期保留两套表示。

`update_plan` runner 只返回 bounded acknowledgement，不先修改 `agent.session`。Agent Loop 将原始 tool call/result 原子提交给
SessionStore，并从该 pair 投影 Plan；随后 reload 并核对 canonical digest，成功后才 adopt 新 projection。这样 append 前失败
不需要补偿写或通用事务框架。
它按普通成功 Tool 消耗一个现有 step，并受 repeated-identical-call 与总 step limit 约束，不建立第二个 Plan loop/budget。

### 3.5 Session v3 与迁移

Mode/Plan 都需要 active-path projection，必须在同一个 worktree 中一次完成 Session v3；不要让两个分支分别修改
`session_store.py`。

迁移合同：

1. 所有只读 surface（`pony sessions list/show`、`pony session inspect/tree`）对 legacy v1 JSON 与 v2 JSONL 都不得写盘；
   `show/inspect/tree` 报告 source version 与 `migration required`，且任何 surface 都不得创建目录、backup、candidate 或改变
   inode/mtime。
2. 只有显式 runtime resume（CLI `--resume` 或公共 `Pony.from_session` 路径）允许迁移。compact/checkpoint/fork/rewind/label/clone
   以及 `tail-repair` 等 Session writer 遇到旧格式返回稳定 `session_migration_required`，要求先 resume；不要扩大隐式
   migration writer 数量。
3. 在 Session lock 下读取并严格验证完整源文件。v1 沿用当前 projection-to-tree 迁移语义并直接产出 v3；v2 走结构保持的
   JSONL rewrite，不先发布中间 v2/v3 文件。
4. backup 写入 owner-only 私有子目录并以 source digest 命名；candidate 与目标文件位于同一 filesystem。任何 symlink、hardlink、
   special file、identity drift 或超限输入都 fail closed。
5. v2 candidate 只改变 header/entry `format_version`，保留所有 entry 的 ID、parent、顺序、timestamp、type 与 data；v3 base
   projection 为旧会话提供 `act` 与 canonical empty Plan，不重排 Session Tree。若旧 artifact 含从未被生产 writer 支持的
   `model_change`，返回稳定 `unsupported_legacy_entry`，不猜测其语义或静默丢弃。
6. `fsync` candidate 后按 v3 完整重读：逐 entry 比较除版本号外的结构，并比较 Canonical Messages、active leaf、checkpoint、
   provider binding、worktree identity、compaction/branch projection 与新 Mode/Plan 默认值。
7. 原子发布并 `fsync` parent；失败保留原文件，重复 resume 可幂等重试，不保留双 writer。迁移成功但后续 runtime 装配失败时，
   v3 仍是唯一 canonical artifact，不回滚成旧格式。遗留 candidate 不是恢复真源：重试必须重新验证 source identity/digest 后
   重建或逐字节复验，不能仅因 candidate 存在就发布。

默认 Mode 采用 `act`，保持旧 Session 行为。Session v3 不改变 Canonical Message、Run、Checkpoint Store、Recovery 或 Sandbox
record format；迁移本身不创建 Provider request、不恢复 workspace、不更改 Model Binding。

### 3.6 Resume 与可见性

Resume 卡完全派生，不增加 Store：

```text
Resuming session
Goal: ...
Plan: 2/5 completed; current: ...
Blocker: ...
Next: ...
Workspace: no-checkpoint | full-valid | partial-stale | workspace-mismatch
Workflow: act
Model: <current provider>/<model>
```

- 交互式 `--resume` 在第一个 prompt 前显示一次：TUI 放在 header 后，纯文本 fallback 放在会话开头；两者使用同一份 renderer data。
  `pony run`、JSON inspection 和其他非交互管理命令不增加装饰性文本。
- Goal 的派生优先级固定为 Active Plan goal -> current task checkpoint goal -> omit；Blocker/Next 只来自 current checkpoint，
  Plan 进度只来自 Active Plan。空字段省略，不从 working-memory cache 反推事实。
- TUI `InMemoryHistory` 从 active Canonical Messages 中最近的纯 top-level user messages 初始化；排除 tool_result、runtime terminal
  与当前队列概念，最多 100 条、总计 64 KiB、单条最多 16 KiB；只收完整条目并保持时间顺序，不写 history 文件。
- `/rewind` 或 `/fork` 后立即 reload active projection，footer、`/mode`、`/plan` 与下个 request 必须一致。
- 自动 checkpoint 继续不进入对话区；卡片不显示 Session ID、checkpoint ID、绝对路径或 endpoint。
- 不新增独立 workflow trace 事件。模型更新复用现有 `tool_started/tool_executed`：先提交含 Plan 的 Session
  `tool_exchange`，再 durable append `tool_executed`，最后通知 UI listener。`/mode` 与 `/plan clear` 只有在 Session control entry
  成功后才渲染成功消息；Run trace 不能反向恢复状态。
- `WorkflowMode` 与 Active Plan 复用现有 3,072-token `task_working_set` source，不新增 source/cap，也不进入永久 system prefix；
  Mode policy 由 runtime 强制，不依赖模型遵守文本。首个 required chunk 是 Mode、Plan goal/current/progress；checkpoint 另有
  required chunk，pending items 与文件详情是 optional，不能把 safety/recovery/checkpoint 事实挤出；Session 中的完整 Plan 不变。
- 每个 top-level turn 冻结开始时的 Plan/Mode context；同一 turn 的 `update_plan` follow-up 通过 tool result 看到 commit
  acknowledgement、completed/total count 与 digest，
  下一个 top-level turn 才由 `task_working_set` 注入它，保持 immutable InjectionSnapshot 不变量。

## 4. 可行性与边界判定

### 4.1 现在能做

| 能力 | 最小实现 | 关键证据 |
| --- | --- | --- |
| WorkflowMode | Session v3 projection + 固定 `act` 初值 + ToolExecutor 静态矩阵 | Mode x approval x shell assessment 测试 |
| Active Plan | strict schema + plan_update + `/plan` + `update_plan` | resume/fork/rewind/reset/clone 一致性 |
| Resume 卡 | 从 checkpoint/Plan/Mode/binding 派生 | 仅交互 resume 显示，one-shot 无噪音 |
| Prompt history | 从 active Canonical Messages 填充 InMemoryHistory | branch 后无 abandoned 输入、无 tool result |
| 过程投影 | 复用既有 tool 事件、命令结果与 footer 字段 | Session commit -> trace append -> listener、无 secret/internal ID |

### 4.2 条件满足后能做

#### Gate M：已收窄为 Session-scoped model switching

[ADR-0047](adr/0047-session-scoped-model-switching.md) 没有批准命名 Target、Provider/endpoint 切换或第二配置面，而是只允许
当前 Session 在相同 `protocol_family` 与 `endpoint_hash` 下替换 model。`/model` 与 `run/repl --model` 共用该合同，
`.env` 保持唯一 Provider 默认配置；resume 以 Session model 为准。专用 Session writer 在锁内比较 expected binding，
含 opaque Provider state 的历史和任何 protocol/endpoint 漂移都以 `model_session_mismatch` 零写拒绝。

#### Gate Q：持续输入队列

先做不落产品格式的 spike，证明：

- 单一 worker 拥有 `agent.ask()`，UI thread 只收 input/render event；
- approval 请求能安全回到 UI，UI 退出/异常仍 fail closed；
- queued message 在何时写 Session，崩溃前后不会出现“已显示但未持久化”或“已持久化却自动收费”的歧义；
- Ctrl+C、EOF、Sandbox finalize、Provider timeout 和 worker join 有界；
- 不修改正在进行的 immutable request，也不声称能取消底层 `urllib` request。

若 spike 需要 event bus、daemon、第二 Session writer 或 Provider cancel abstraction，则停止；保持同步 TUI，等真实 steer 需求。

#### Gate D：命名 delegate

串行隔离已落地；Phase 5 追加了不改变单 Action 合同的 worktree batch：

1. `delegate` 接受受限的 `name`；name 进入 child Session ID、隔离 artifact root 与 parent 的 bounded result label。
2. child 使用独立 Session root、Run root 与装配 factory 新建的 model client；不得复制或共享含可变 transport 计数/state 的对象。
3. child 固定 `read_only=True`、`dontAsk`（approval=never）、无 Durable Memory/Plan/workspace 写权限，也不能再次 delegate。
4. parent 只接收最多 4,000 字符的 final result；child 的 Session/Run 不进入 parent store。
5. `delegate_worktrees` 单个 action 接受最多 8 个 named task，并用最多 4 个 stdlib worker；每项通过 factory 新建 client，
   从 clean exact HEAD 创建独立 branch/worktree/Session/Run。
6. write child 只使用 `acceptEdits` 内建编辑权限，thread approval fail closed；terminal manifest 返回 diff/test status，
   merge 与 cleanup 均为显式 CLI 操作。

#### Gate S：Skills

已选择并落实为 `.claude/skills/<name>/SKILL.md` 的受信仓库 control-plane 输入，见
[ADR-0046](adr/0046-read-only-project-skills.md)。loader 使用 anchored/no-follow/single-link/root-identity 读取，并对
strict frontmatter、大小、数量、UTF-8 和 known secret 全部 fail closed；任一条目有问题时整个 catalog 不加载。现有 Memory
frontmatter 解析是宽松容错语义，不能复用到这个 trust boundary。

第一版只提供 catalog、TUI completion、`/name [prompt]` 与一个 immutable read-only context source；不注册
tool/command/hook/provider，不执行脚本。HOME catalog、plugin、`.agents/skills` compatibility、在线安装、市场、自动更新和
loaded-state 持久化继续不做。

### 4.3 明确不能做或不应做

- 不在当前合同下实现跨协议 opaque provider state 重放；事实不明即 `model_session_mismatch`。
- 不实现 mid-request steer、抢占式 tool cancel 或“线程中断等于 HTTP 已取消”的假保证。
- 不让 `review + auto` 自动运行测试等可能产生副作用的命令。
- 不并行共享当前 model client、SessionStore 或可变 runtime 的 delegate。
- 不创建第二 transcript、Todo Store、command registry、Agent Loop、recovery engine 或配置文件。
- 不让 Mode、Skill、delegate、MCP、IDE 绕过 schema/path/secret/policy/approval/sandbox/recovery。
- 不展示或持久化 Provider reasoning/chain-of-thought。
- 不做共享 parent workspace 的并行写 agent、自动 merge、后台 daemon、distributed authority、remote/multi-tenant sandbox。
- 不因路线图需要而增加 plugin container、service locator、policy DSL 或通用 event bus。

## 5. Worktree 实施记录

依赖按批准方案固定为 `W0 -> (W1, W2) -> (W3, W4, W5) -> W6 -> W7`。每一波从已合入前置切片的
integration exact SHA 创建；feature worktree 不各自追赶 `origin/main`，共享热点按 owner 串行。

| Worktree / 分支 | 实际职责 | 状态 |
| --- | --- | --- |
| W0 `codex/workflow-integration` | 方案、ADR-0043、文档 surface | 已完成 |
| W1 `codex/workflow-session-v3` | Session v3、Plan validator/projection、v1/v2 migration、reset/clone | 已完成 |
| W2 `codex/tui-contract-repair` | 单行启动头与安全 footer 基线 | 已完成并先于 Wave 2 合入 |
| W3 `codex/workflow-policy` | `update_plan`、Mode ceiling、visible schemas、turn snapshot、commit verification | 已完成 |
| W4 `codex/workflow-context-resume` | required workflow context、Resume/metadata/history pure helpers | 已完成 |
| W5 `codex/workflow-inspection` | v1/v2/v3 inspection、`latest`、稳定错误 envelope | 已完成 |
| W6 `codex/workflow-cli-tui` | `--mode`、`/mode`、`/plan`、Resume 卡、history、footer | 已完成 |
| W7 integration | 文档同步、接口接缝、exact-HEAD 完整离线门禁 | 已完成 |

实施中保持了唯一 owner：`session_store.py` 只在 W1 与 integration 安全接缝修改；`application.py` 按 W1 -> W3；
`render.py` 按 W2 -> W6；`start.py` 只在 W6。每个切片提交前运行 changed-file Ruff 与聚焦 pytest，随后才合入 integration。

### 5.1 后续 Gate worktree

这些 worktree 不与 P0 并行创建；只有各自 Gate 获批后，从最新 `origin/main` 重新开始：

| Gate | 建议分支 | 第一切片 | 禁止顺手做 |
| --- | --- | --- | --- |
| M | `codex/pony-cap-model` | ADR-0047 + Session-scoped `/model`、`--model` 与完整合同测试 | target registry、跨 endpoint、fallback |
| Q | `codex/input-queue-spike` | 假 Provider + fake prompt 的线程/approval/exit spike | durable format、daemon、steer |
| D | `codex/delegate-isolation` | 独立 child client/session/run，仍串行 | batch、线程池、可写 child |
| S | `codex/pony-cap-skills` | ADR-0046 + read-only project catalog、`/name` 和 context 注入 | scripts、在线安装、HOME/plugin catalog |

## 6. 集成、回滚与兼容策略

### 6.1 合并顺序

实际顺序为 W0 -> W1/W2 -> W3/W4/W5 -> W6 -> W7。W1-W6 都只是 integration slices，不单独发布或进入可发布
`main`；只允许 W7 exact-HEAD full gate 通过后的完整组合进入发布候选。

### 6.2 回滚单位

- W1 是格式地基：一旦 v3 Session 已由正式版本写出，回滚到只懂 v2 的 binary 会 fail closed；发布前必须提供只读 inspection，
  不承诺 downgrade writer。需要真正降级时走独立显式迁移设计，不能让旧 binary 猜。
- W2-W6 都不得改变 v3 基本 record layout；可以独立 revert，只要 v3 reader 仍理解已写 entry。
- Mode policy 的回滚不能把未知 Mode 当 `act`；旧/未知值必须拒绝启动或要求显式迁移。
- Plan UI 的回滚可以隐藏投影，但不能删除 append-only Plan 历史。

### 6.3 兼容边界

- 不保留 Session v1/v2/v3 多 writer；只保留 v1/v2 readonly reader + explicit migration，正常持久化只写 v3。
  v1/v2 的所有 writer（包括 tail repair）都返回 `session_migration_required`。
- 不新增 feature flag 作为永久逃生舱。若需要开发期保护，只在未发布分支使用，收口时删除。
- 不修改 Provider 四变量、Model Binding 或 Sandbox record format，因此 P0 不需要 G8 live 或真实 Model switch 证据。
- Session format 与 Sandbox/runtime 相交，完整离线门禁必跑；真实 Docker G7 只有用户对当轮明确授权且环境适用时运行。

## 7. 验证矩阵

### 7.1 核心行为

| 场景 | 必须结果 |
| --- | --- |
| 新 Session | 默认 `act`、空 Plan，header/entry v3 |
| v1/v2 inspection | 只读报告 source version + migration required，inode/mtime/content 不变 |
| v1 resume | 直接迁移到 v3，保持 legacy projection，不发布中间 v2 |
| v2 resume | 原子迁移并保持全部 entry structure、messages/branch/checkpoint/binding/worktree identity |
| `/mode plan` | 下一个 turn 生效；当前 request/tool 不被中途改写 |
| `plan/review + never` | 所有 shell 仍拒绝，Mode 不扩大权限 |
| `plan/review + auto` | write/memory/destructive/external-effect shell 仍拒绝；仅既有 `allow` 可执行 |
| `review + ask + pytest` | 逐次确认；真实 effect 被 observer/recovery 记录 |
| `update_plan` | tool pair + Plan 单 entry 提交成功才继续；不创建 Tool Change |
| fork/rewind | Mode/Plan 恢复到目标 active path |
| reset | 清 Plan，保留 WorkflowMode，历史 append-only |
| clone worktree | 复制 Mode/Plan，清 workspace recovery/freshness |
| resume card | TUI/plain interactive resume 各一次；one-shot 无；无 internal ID/absolute path/secret |
| prompt history | 仅 active branch 的纯 top-level user text，100 条/64 KiB/16 KiB 单条，纯内存 |
| trace listener | Session tool exchange 提交后，durable trace append，再收到脱敏副本 |

### 7.2 安全与失败注入

- malformed/unknown/oversized Plan、非 canonical empty、重复 key/ID、多个 in-progress、control chars、secret material。
- append 前失败、append committed 后异常、candidate publish crash、backup/candidate identity swap。
- v2 中出现 unsupported `model_change`、v1/v2 source 与 candidate projection/entry structure 不一致。
- Mode/approval 参数在确认后变化，shell reassessment 变化，observer/report/finalizer 次生失败。
- rewind/clone 与 pending recovery、Sandbox pending-review、worktree identity mismatch。
- non-interactive `approval=ask` 被降为 `never` 后，review 外部 effect 不能运行。
- child delegate 不能写 Plan/Memory/workspace，且父 Mode 不扩大 child authority。

### 7.3 门禁层级

每个 worktree 先跑 changed-file Ruff 与聚焦 pytest；W7 最终 exact HEAD 运行：

```bash
./scripts/check.sh
```

证据说明必须区分：

- unit/focused：单模块合同；
- offline full gate：G0-G6/G9，发布必要；
- Docker real：G7，条件且需当轮授权；
- Provider live：G8，收费且需当轮授权。

本 P0 不改变 wire adapter，通常无需 G8；若未执行，明确写“live 未执行”。未获授权时 G7 也写“Docker 未执行”，不能用
fake/offline 代替。

## 8. 文档与发布影响

P0 实现后按责任更新，而不是提前改愿景：

| 文档 | 更新内容 |
| --- | --- |
| `AGENTS.md` | WorkflowMode/Plan/Session v3 的已实现硬合同；仍禁止动态 Model |
| `README.md` | `/mode`、`/plan`、resume 用户路径与真实 TUI footer |
| `docs/domain-model.md` | WorkflowMode、Active Plan 精确定义和不应混用术语 |
| `docs/architecture.md` | active projection、Tool policy 顺序、UI adapter |
| `docs/context-and-sessions.md` | v3 record、迁移、reset/clone/rewind 语义 |
| `docs/security.md` | Mode ceiling、review shell、Plan redaction/fail-closed |
| `docs/recovery.md` | Session v3 与 workspace recovery 仍分离 |
| `docs/verification.md` | Mode matrix、migration、resume/TUI 门禁 |
| `CHANGELOG.md` | 用户可见变更与 Session migration 提示 |

`pyproject.toml`/`uv.lock` 不应变化；若意外需要新依赖，立即停止并重新论证，不把 dependency 变更夹带进工作流功能。

## 9. 风险、停止条件与决策记录

| 风险 | 控制 | 立即停止条件 |
| --- | --- | --- |
| Session v3 损坏历史 | readonly v1/v2 reader、candidate 全量复验、atomic publish | 不能保持 active leaf/messages/binding |
| Mode 被 approval 绕过 | shared ToolExecutor 根部静态矩阵 | `plan/review + auto` 可写 workspace/Memory |
| review external-effect 命令改变 workspace | assessment + observer + recovery | effect 无法可靠记录或阻断后续 mutation |
| Plan/checkpoint 双真源 | Plan 单一 active projection，checkpoint 不复制 Plan | 两处都可独立修改或持有完整 Plan |
| UI 形成第二状态机 | shared REPL handler + Session projection | TUI 与 plain 需要不同命令语义 |
| worktree 合并冲突吞噬收益 | owner/禁止触碰区、共享热点串行 | 两个分支同时大改 Session/Runtime 热点 |
| 路线图膨胀 | Gate、无新依赖、无 consumer 不做 | 为单一实现引入框架/daemon/registry |

任何停止条件触发时，不用兼容层“先跑起来”；保留最小证据，回到合同或 ADR。

## 10. Definition of Done

P0 只有同时满足以下条件才算完成：

- `Plan -> Act -> Review` 在一个 Session 内可预测，WorkflowMode 与 approval 正交且 fail closed。
- Active Plan 只有一个真源，能在 resume、fork、rewind、reset、clone 后符合明确合同。
- v1/v2 -> v3 只在显式 runtime resume 迁移，inspection 零写入，失败可幂等重试且不损坏原文件。
- Resume 卡和 prompt history 均从 active Canonical state 派生，不建立新持久化文件。
- Canonical Messages、Session Tree、Tool policy、Run/Trace、Recovery 各自仍只有一个 owner。
- UI/trace/artifact 不泄漏 Key、reasoning、绝对路径、Session/checkpoint ID 或 endpoint。
- P0 当时未引入多 Target、输入队列、并行 delegate、Skills、MCP/IDE 或新 runtime dependency；后续 Gate 能力以各自
  已批准 ADR 和当前产品文档为准。
- 聚焦测试和最终 exact HEAD 的 `./scripts/check.sh` 通过；目标 integration worktree clean。
- G7/G8 根据当轮授权明确记录为通过、失败或未执行；不夸大离线证据。

Gate M 后续已由 ADR-0047 收窄为 Session-scoped model switching；Gate S 也已由 ADR-0046 收窄为只读 Project Skills。
其余 Gate 没有 ADR、真实 consumer 或 spike 证据时仍不实现。
