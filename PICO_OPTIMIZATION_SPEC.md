# Pico 聚焦优化与演进 Spec

> 状态：Designed / Living Spec
>
> 产品范围：本地运行、小团队共享
>
> 实施起点：<code>410848859910c080f9bdfc079a04b922ad9c260a</code>
>
> 创建日期：2026-07-12
>
> 最后更新：2026-07-16

## 1. 文档目的与使用方式

本文档是 Pico 后续优化工作的主 Spec，用于统一记录：

- 当前实现事实与验证基线。
- 已确认的产品边界和架构决策。
- 上下文工程、工具治理、记忆系统、沙箱、评测与可观测性六条主线。
- 每条主线的目标、非目标、数据流、接口、失败语义和验收标准。
- 从 Pi、Claude Code 和 Anthropic Sandbox Runtime 中吸收或明确不吸收的能力。

后续新增设计应优先更新本文档对应章节。只有当某个子项准备进入实现，并且需要逐文件任务拆解时，才另建实施计划；实施计划不能覆盖或改变本文档已经锁定的产品与架构决定。

本文档不是功能愿望清单。一个新提案只有满足下列至少一项，才进入活跃路线图：

1. 直接提高上下文质量或 token 使用效率。
2. 直接提高工具调用的安全性、可解释性或可恢复性。
3. 直接提高记忆的准确性、可维护性或团队共享效果。
4. 补齐 Shell 的 OS 级隔离。
5. 让核心行为可以被稳定评测。
6. 让一次运行可以被安全、低成本地复盘。

### 1.1 2026-07-15 模型 CLI 硬切决策

本轮已经把公开模型入口收敛为 DeepSeek-first 单路径：

- 固定模型 `deepseek-v4-flash`、Anthropic Messages、`x-api-key` 认证和关闭 thinking 的工具续接路径。
- 唯一运行时配置是精确 API 根 `PICO_API_URL` 与凭证 `PICO_DEEPSEEK_API_KEY`；默认 API 根为
  `https://api.deepseek.com/anthropic/v1`，客户端只追加 `/messages`。
- `pico init` 只交互询问 URL 和隐藏 Key，不联网；`run/repl` 没有模型选择参数。
- 普通 `pico doctor` 始终离线，只有 `pico doctor --check-api` 执行文本、工具调用和 tool result 续接验证。
- 运行时不再存在 Provider/Profile/Connection resolver、capability registry、协议探测、自动回退或旧环境变量兼容。
- Session format v2 只绑定 `protocol_family`、`model`、`endpoint_hash`；任何不一致均拒绝恢复，旧 binding 不迁移。
- OpenAI Responses、OpenAI Chat Completions 与 Ollama Chat client 仅作为内部协议实现和离线测试对象保留，不接入 CLI。

这是硬切合同，不设置双配置期。需要回滚时回滚完整 Git 变更和本地 `.env` 备份，不在新 runtime 中增加
转换、告警或兼容 shim。只有出现明确的第二个公开 CLI 模型需求时，才重新评估模型选择入口。

## 2. 项目现状与总体判断

### 2.1 当前主链路

Pico 已经形成完整的本地 coding-agent runtime：

~~~text
CLI
  → config / trusted workspace
  → context / working memory / durable memory / repo map
  → provider-neutral request
  → provider adapter
  → action decode
  → tool policy / approval
  → tool execution
  → effect observation / verification
  → session / run / checkpoint
  → recovery preview / apply
~~~

### 2.2 已经做得好的部分

- Canonical Messages 是唯一 transcript，没有多套会话真源。
- 一次 Model Attempt 只执行一个 Action，审批、mutation lock、effect observation 和 recovery 的语义清晰。
- 内建文件工具、Shell、Git/RG、私有文件、secret 和恢复记录均有 fail-closed 边界。
- Tool Change Record 能表达 pending、success、partial success、interrupted 和失败后的实际 workspace effects。
- Context 已有固定注入源、intent profile、注入预算、history soft cap、tool result digest 和 request metadata。
- Memory 已有安全 BlockStore、workspace/user scope、BM25/CJK 检索、frontmatter、link、supersedes 和 recently-recalled 抑制。
- RunStore 已经保存 task state、trace 和 report，Provider transport evidence 也能进入最终报告。
- 离线测试、构建和分发验证充分，Python runtime 保持零第三方依赖。

### 2.3 初始基线的核心问题与当前状态

本节保留 2026-07-12 初始基线的问题定义；现行状态以第 6 节活跃路线图和各专题章节为准。
默认 Host 模式仍不是 OS sandbox，但 ADR-0040/0042 接受的 exact-image 本机 Docker MVP 已补上显式
Sandbox 路径，分布式发布仍为 `NO-GO`。

| 领域 | 当前成熟度判断 | 核心缺口 |
| --- | --- | --- |
| 上下文 | 基础成熟 | 注入预算与整个 request 总预算未形成统一闭环；用户看不到 token 去向 |
| 工具 | 安全链路成熟 | effect、risk、approval 和 rejection metadata 分散，缺少统一决策合同 |
| 记忆 | 存储与检索成熟 | 团队共享边界不清晰；召回选择不可解释；质量评测与运行证据未完全连通 |
| 沙箱 | 初始缺失；本机 MVP 已验证 | 默认 Host Shell 仍拥有宿主权限；显式 exact local Sandbox 使用 Docker + filtered staging |
| 评测 | 用例丰富 | runner、artifact 和 gate 分散，无法用一个结果回答“是否回退” |
| 可观测性 | 数据丰富 | trace/report 字段不够稳定，缺少安全的聚合视图和统一关联字段 |

### 2.4 初始总体策略

后续不重写 Pico，不增加通用插件平台，也不复制 Pi 或 Claude Code 的全部功能。优化方式固定为：

1. 复用现有数据和执行链路。
2. 先统一合同和证据。
3. 以 Docker + filtered staging 补齐显式本机 OS sandbox；分布式发布证据独立收口。
4. 每个能力都必须有离线、可重复的验收场景。

## 3. 初始验证基线

以下结果保留为 2026-07-12 代码基线；当前收口证据以 `docs/verification.md` 为准：

| 项目 | 结果 |
| --- | --- |
| <code>uv lock --check</code> | 通过 |
| <code>uv run ruff check .</code> | 通过 |
| <code>uv run pytest -q</code> | 2050 passed, 6 skipped |
| Offline live assertions | 88 passed |
| Memory quality fake benchmark | 8/8 |
| sdist / wheel 构建 | 通过 |
| distribution verifier / clean-install smoke | 通过 |
| Python runtime 第三方依赖 | 0 |
| C901 findings | 61 |

当前本机性能观测：

| 场景 | median | p95 |
| --- | ---: | ---: |
| 大型 request build | 21.8 ms | 44.3 ms |
| 大型 Memory retrieval | 86.9 ms | 122.0 ms |
| 100 notes recall | 约 21–23 ms | 约 25 ms |
| 100 项 artifact redaction | 48.2 ms | 51.1 ms |
| 100 文件 recovery preview | 79.3 ms | 90.9 ms |

这些数字只描述当前机器，不是跨平台 SLA。后续性能门禁只阻止明显回退，不追求脆弱的微秒级稳定。

## 4. 设计原则与决策过滤器

### 4.1 必须保持的不变量

1. Canonical Messages 继续作为唯一会话 transcript。
2. 一次 Model Attempt 继续只产生和执行一个 Action。
3. 未知工具或未知 effect 继续 fail-safe。
4. approval、sandbox、effect observation 和 recovery 是四层不同能力，不能互相替代。
5. 最新用户输入不得因预算压力被静默截断。
6. Shell runner 无论成功、失败、超时或部分执行，都必须进入 effect observation 和 terminalization。
7. secret redaction、私有文件、路径、trusted executable 和恢复记录的现有安全边界不得削弱。
8. Python core 继续保持标准库实现和零 runtime dependency。

### 4.2 实施纪律

- 优先删除、复用和使用标准库。
- 不为单一实现创建 interface、factory、backend registry 或依赖注入容器。
- 不为未来可能存在的需求提前设计配置项。
- 不为了降低 C901 数字机械拆函数。
- 持久化合同只有在跨进程读取确有需要时才版本化。
- 每项非平凡变更必须留下一个最小、可执行、能阻止回归的检查。
- 真实 Provider 验证必须单独授权，限制请求数、时间和成本。

## 5. 目标架构

~~~mermaid
flowchart LR
    U["用户请求"] --> C["Context Plan"]
    WM["Working Memory"] --> C
    DM["Durable Memory"] --> C
    C --> L["Model / Agent Loop"]
    L --> P["Tool Policy Decision"]
    P --> F["内建文件工具"]
    P --> S["Docker 中的 run_shell"]
    SR["Source Root"] -->|"filtered staging"| ER["Execution Root"]
    F --> ER
    S --> ER
    F --> E["Effect Observation / Recovery"]
    S --> E
    E --> A["Reviewed Source Apply"]
    A --> SR
    C --> O["Trace / Run Summary"]
    P --> O
    DM --> O
    S --> O
    E --> O
    O --> V["Deterministic Evaluation"]
~~~

### 5.1 各层职责

| 层 | 决定什么 | 不决定什么 |
| --- | --- | --- |
| Context Plan | 哪些信息进入本次 Model Request、占用多少预算 | 工具是否允许执行 |
| Tool Policy | 工具、参数、effect、risk、approval 是否允许 | 进程能访问哪些宿主资源 |
| Sandbox | 不可信进程的 OS/资源边界与 filtered Execution Root | 命令是否符合用户意图、变更是否应写回 Source Root |
| Effect / Recovery | 命令实际改变了什么、如何记录和恢复 | 是否应该自动撤销 |
| Observability | 安全记录过程和结果 | 远程上传、告警和 Dashboard |
| Evaluation | 判断核心行为是否回退 | 替代真实用户体验判断 |

## 6. 活跃路线图

状态取值：<code>proposed</code>、<code>designed</code>、<code>in_progress</code>、<code>blocked</code>、<code>done</code>、<code>rejected</code>。

| ID | 主线 | 内容 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| CTX-001 | 上下文 | 全 request 预算闭环 | P0 | done |
| CTX-002 | 上下文 | Context Breakdown | P0 | done |
| TOOL-001 | 工具 | 单一工具定义与统一 Policy Decision | P0 | done |
| MEM-001 | 记忆 | Working、个人与团队记忆边界 | P0 | done |
| MEM-002 | 记忆 | 可解释召回与质量诊断 | P1 | done |
| SBOX-001 | 沙箱 | Docker Engine-backed Sandbox + filtered staging | P0 | local arm64 MVP verified；distributed release NO-GO（D7 blocked） |
| OBS-001 | 可观测 | 安全 Trace 合同与 Run Summary | P0 | done |
| EVAL-001 | 评测 | 统一离线评测入口和回归门禁 | P0 | in_progress / functional gate done；Linux performance baseline blocked |

活跃路线图之外的问题仍可作为普通维护工作修复，但不得自动升级为新的平台能力。

## 7. 上下文工程

### 7.1 当前实现

当前 Context 已经具备：

- system 与 tools 固定区。
- Canonical Messages 到 Provider request 的单一构建入口。
- workspace state、memory index、project structure、recalled memory、checkpoint 五类注入源。
- debug、recall、structural、default 四个确定性 intent profile。
- 每来源 token budget 和总体 injection budget。
- history soft cap 和 message floor。
- 大 tool result digest 与 raw artifact。
- request metadata、drop、truncate、recall error 和 cache telemetry。

这些能力不需要推倒重做。

### 7.2 已解决问题（历史设计基线）

以下问题已由 CTX-001/002 和死路径删除关闭，保留用于解释当前设计来源，不是活跃待办：

1. <code>history_soft_cap</code> 与 <code>injection_budget</code> 分别生效，但没有证明 system、tools、history、当前输入、runtime feedback、injection 与预留输出的总和一定不超过硬上限。
2. 当前 DROP_PRIORITY 把 checkpoint 当普通可丢来源，但非空 recovery/resume checkpoint 可能是恢复任务最关键的信息。
3. 用户只能从内部 trace/report 拼接数据，无法直观看到一次请求的 token 分配。
4. token 估算是 Provider tokenizer 与字符估算的混合结果，但输出没有说明计数方式。
5. 原 <code>MemoryRefresher</code> 未接入生产链路，并与实际 context source renderer 形成第二套渲染逻辑；该死路径现已删除。

### 7.3 非目标

- 不引入语义 intent classifier。
- 不增加新的 LLM compaction 请求。
- 不实现 path-scoped instructions、Skills 或 MCP tool search。
- 不增加 model capability registry。
- 不改变 Canonical Messages 的持久化格式。

### 7.4 统一预算模型

一次 Model Request 的输入上限固定计算为：

~~~text
input_limit =
    total_budget_hard_cap
    - max_new_tokens
    - CONTEXT_SAFETY_MARGIN_TOKENS

CONTEXT_SAFETY_MARGIN_TOKENS = 512
~~~

<code>total_budget_hard_cap</code> 继续由现有配置提供，不根据模型名称猜测 context window。

预算项：

| 项 | 说明 | 是否可丢 |
| --- | --- | --- |
| system | Pico prefix、AGENTS.md 等固定指令 | 否 |
| tools | 当前允许工具的 schema | 否 |
| current_user | 本轮原始用户输入 | 否 |
| runtime_feedback | 协议纠正或运行时反馈 | 否 |
| checkpoint | 非空 resume/recovery 信息 | 否 |
| recent_history | 最近完整 turns | 软保留 |
| recalled_memory | 通过召回阈值的 Memory | 是 |
| workspace_state | branch/status 等实时状态 | 是 |
| project_structure | 顶层目录与语言统计 | 是 |
| memory_index | Memory 文件目录摘要 | 是 |

### 7.5 构建顺序

1. 计算 system、tools 和最新用户输入的 token。
2. 将 <code>max_new_tokens</code> 和 512 token safety margin 从硬上限中预留。
3. 如果 system + tools + current user + required runtime feedback 已超限，返回 <code>context_budget_exceeded</code>，Provider 不得被调用。
4. 使用已经写入 Canonical Messages 的 tool result digest，不再二次摘要。
5. 按现有 intent profile 渲染候选注入源。
6. 非空 checkpoint 标记为 required；普通 turn 没有 checkpoint 时不产生空占位。
7. 先移除超出 history soft cap 的最老完整 turn。
8. 总预算仍超限时按以下顺序丢弃 optional source：

~~~text
memory_index
→ project_structure
→ workspace_state
→ recalled_memory
~~~

9. 仍超限时继续移除最老完整 turn。<code>history_floor_messages</code> 是软偏好，不得为了守住 floor 造成真实 request 超限。
10. 不得拆开 assistant tool_use 与紧随其后的 user tool_result。
11. 最新用户输入、runtime feedback 和 required checkpoint 永不截断或静默丢弃。
12. 仍无法满足预算时返回稳定错误，不发送超限请求。

### 7.6 Token 计数

- Provider 提供 <code>count_tokens</code> 时使用 Provider 计数。
- Provider 计数不可用或失败时继续使用现有字符估算。
- metadata 必须记录 <code>token_count_mode=provider|estimate</code>。
- 同一个 request 内所有预算比较使用同一种计数模式，不能在比较过程中切换。

### 7.7 Context Breakdown 合同

每次 request 在 <code>request_metadata</code> 中保存：

~~~json
{
  "context_breakdown": {
    "schema_version": 1,
    "token_count_mode": "provider",
    "budget": {
      "total": 100000,
      "reserved_output": 4096,
      "safety_margin": 512,
      "input_limit": 95392,
      "used": 18240,
      "within_budget": true
    },
    "sources": [
      {
        "name": "recalled_memory",
        "required": false,
        "budget_tokens": 600,
        "actual_tokens": 312,
        "status": "included",
        "reason": "recall_match"
      }
    ],
    "history": {
      "tokens_before": 18000,
      "tokens_after": 12000,
      "dropped_turns": 3
    },
    "digest": {
      "applied_count": 2
    }
  }
}
~~~

source status 固定为：

- <code>included</code>
- <code>empty</code>
- <code>truncated</code>
- <code>dropped_budget</code>
- <code>failed</code>

reason 使用固定低基数字段，例如：

- <code>required_checkpoint</code>
- <code>intent_budget_zero</code>
- <code>source_empty</code>
- <code>source_error</code>
- <code>aggregate_budget</code>
- <code>recall_match</code>

Breakdown 不保存 prompt、文件正文、Memory 正文、query、tool args 或 tool result。

### 7.8 清理项

删除未进入生产链路的 <code>MemoryRefresher</code> 及仅覆盖该死路径的测试。当前 context sources 继续直接复用安全 BlockStore 和 RepoMap；只有 benchmark 证明扫描成本成为实际瓶颈后，才在真实调用链上增加一个缓存。

### 7.9 验收标准

- system、tools、messages 与 reserved output 的总和不超过硬上限。
- 最新用户输入不被截断。
- tool pair 不被拆开。
- 非空 resume checkpoint 在预算压力下仍保留。
- required context 超限时 Provider 未被调用。
- text/JSON inspection 的字段含义一致。
- Breakdown 不包含内容或 secret。
- 现有 context、digest、history、recall 和 Provider request tests 全部通过。

## 8. 工具治理

### 8.1 当前实现

当前工具链已经具备：

~~~text
validate / assess
→ approval
→ pending Tool Change
→ execute once
→ observe effects
→ verification
→ terminalize
~~~

Shell 还额外经过 syntax scan、risk classification、trusted executable、approval 后复验和 hardened Git。

### 8.2 已解决问题（历史设计基线）

以下问题已由 TOOL-001 的统一 registry/decision metadata 关闭，保留用于解释当前合同来源：

- schema、<code>risky</code> 和 effect class 分散在不同位置。
- Shell 与非 Shell 的拒绝 metadata 形状不完全一致。
- 相同语义有时同时通过 free-text error、<code>tool_error_code</code>、<code>security_event_type</code> 和 command approval 表达。
- 外部消费者难以只靠固定字段回答“为什么没有执行”。

### 8.3 非目标

- 不增加工具插件、MCP 或动态 registry。
- 不增加通用 Policy DSL。
- 不增加工具 capability negotiation。
- 不改变单 Action 不变量。
- 不实现通用并行工具执行。

### 8.4 单一工具定义

现有固定工具 registry 成为 schema、description、risk 和 effect 的唯一真源：

~~~python
{
    "run_shell": {
        "description": "...",
        "schema": {...},
        "risky": True,
        "effect_class": "workspace_write",
        "run": ...
    }
}
~~~

effect class 只保留：

- <code>read_only</code>
- <code>workspace_write</code>
- <code>memory_write</code>

未知工具或缺少 effect 的工具按 <code>workspace_write</code> 处理并拒绝执行，不增加 <code>unknown</code> 的宽松路径。

### 8.5 统一 Policy Decision

每次工具调用，无论成功还是拒绝，都产生：

~~~json
{
  "policy": {
    "schema_version": 1,
    "decision": "allow",
    "reason_code": "allowed",
    "effect_class": "workspace_write",
    "risk_class": "approval_required",
    "approval": {
      "mode": "ask",
      "required": true,
      "outcome": "approved"
    }
  }
}
~~~

允许的 <code>decision</code>：

- <code>allow</code>
- <code>deny</code>

首版 reason code 复用现有错误码并补齐缺失项：

- <code>allowed</code>
- <code>unknown_tool</code>
- <code>invalid_arguments</code>
- <code>disallowed_tool</code>
- <code>read_only_block</code>
- <code>repeated_call</code>
- <code>sensitive_path</code>
- <code>policy_rejected</code>
- <code>approval_required</code>
- <code>approval_denied</code>
- <code>approval_arguments_changed</code>
- <code>executable_unavailable</code>
- <code>recovery_review_required</code>
- <code>sandbox_unavailable</code>
- <code>sandbox_policy_invalid</code>
- <code>sandbox_denied</code>

不为这些值新建独立规则引擎；由一个现有 metadata helper 生成同形字典。

### 8.6 固定执行顺序

~~~text
validate tool and args
→ resolve effect and risk
→ enforce allowed-tools / read-only / repeated-call
→ approval
→ sandbox startup readiness and pre-run policy denial for run_shell
→ create pending Tool Change
→ sandbox call-time identity and policy integrity checks
→ runner exactly once
→ observe actual effects exactly once
→ verification and recovery evidence
→ terminalize exactly once
~~~

approval 仍发生在 mutation lock 前。Session startup readiness 与 pre-run policy denial 必须在 pending
record 前完成；单次调用的 critical identity 与 policy integrity checks 在 pending 后执行，其失败为
`wrapper_failed + wrapper_started=false + target_started=false`，并进入 effect observation 与现有
best-effort terminalization。一旦创建 pending record，后续任何异常都必须终态化。

### 8.7 验收标准

- 每个拒绝路径都有稳定 reason code。
- 被拒绝时 runner 调用次数为 0。
- 被允许时 runner 调用次数恰好为 1。
- approval 后参数变化继续拒绝。
- tool result、trace、Tool Change Record 和 report 中的 effect/status 一致。
- Shell、Memory 和普通文件工具使用同形 policy metadata。
- 不降低现有路径、secret、Git、approval、mutation lock 或 recovery 安全测试。

## 9. 记忆系统

### 9.1 记忆分层

Pico 只维护以下三层，不增加第四种隐式记忆：

| 层 | 内容 | 生命周期 | 是否自动写 |
| --- | --- | --- | --- |
| Canonical Messages | 用户、模型与工具的完整会话事实 | session | 按 Agent Loop 正常写 |
| Working Memory | recent files、file summaries、task summary | session | 是 |
| Durable Memory | User Notes 与 Agent Notes | 跨 session | 仅显式规则 |

Canonical Messages 不称为 Memory，也不通过 durable memory 机制复制。

### 9.2 Durable Memory 边界

| scope | User Notes | Agent Notes | 共享方式 |
| --- | --- | --- | --- |
| workspace | <code>.pico/memory/notes/**/*.md</code> | <code>.pico/memory/agent_notes.md</code> | User Notes 经 Git Review；Agent Notes 本地 |
| user | <code>~/.pico/memory/notes/**/*.md</code> | <code>~/.pico/memory/agent_notes.md</code> | 个人本地 |

规则：

1. User Notes 由人维护，Agent 只能 list/read/search。
2. Agent Notes 继续 append-only。
3. 只有用户明确要求“记住”“保存为记忆”等意图时，模型才允许调用 <code>memory_save</code>。
4. 不在任务结束时自动保存总结。
5. 不自动把 workspace Agent Notes 提升为团队知识。
6. 需要共享的 Agent Note 由人整理、去重并写入 User Notes。

### 9.3 团队共享

当前 <code>.pico/</code> 整体被 Git 忽略。实施 MEM-001 时只放行：

~~~gitignore
!.pico/
.pico/*
!.pico/memory/
.pico/memory/*
!.pico/memory/notes/
!.pico/memory/notes/**/*.md
~~~

实际规则需通过现有 repository structure test 验证：

- User Notes 可被 Git 跟踪。
- <code>agent_notes.md</code>、runs、sessions、checkpoints、locks 和其他私有工件仍被忽略。
- 通用 <code>write_file</code>、<code>patch_file</code> 和 sandboxed Shell 均不能修改 User Notes。

不增加远程 Memory API、同步数据库、冲突解决器或权限服务；Git 已经覆盖小团队的审阅、历史与冲突处理。

### 9.4 Working Memory

保留现有规则：

- <code>read_file</code> 可生成 bounded file summary。
- <code>write_file</code> 和 <code>patch_file</code> 使对应 summary 失效。
- recent files 有固定上限。
- 工作摘要不自动进入 Durable Memory。

新增的可观测字段只记录：

- summary 命中数。
- stale/invalidated 数。
- 本轮使用的 recent file 数。

不记录 summary 正文。

### 9.5 可解释召回

保留当前 BM25、CJK bigram、field boost、link expansion、supersedes、min score、top-k、per-note budget 和 recently-recalled guard。

将当前“搜索、过滤、渲染、记录 recent”聚合函数拆成最小的两步：

1. 选择：得到 candidates、selected 和 filter counts。
2. 渲染：只把 selected notes 的 bounded 首段写入 context。

选择结果：

~~~json
{
  "candidate_count": 6,
  "selected_count": 2,
  "filtered": {
    "below_threshold": 2,
    "recently_recalled": 1,
    "superseded": 1,
    "missing_document": 0
  },
  "selected": [
    {
      "scope": "workspace",
      "type": "decision",
      "normalized_score": 0.91
    }
  ]
}
~~~

安全规则：

- trace/report 不保存 query、snippet 或 Note 正文。
- public summary 不保存用户目录下的完整路径。
- session 仍可在私有、脱敏状态中记录 canonical selected path，用于 recently-recalled。
- recall 失败继续不阻断主请求，但必须记录固定错误类别。

### 9.6 Memory 诊断

复用 <code>pico doctor</code>，不增加新的 Memory 管理命令。增加非阻塞诊断：

- 重复的 frontmatter <code>name</code>。
- 无效 frontmatter。
- 指向不存在 note 的 <code>supersedes</code>。
- workspace/user Agent Notes 超过现有 soft cap。
- workspace User Notes 仍被 Git ignore。
- 扫描因文件数、单文件大小或总字节上限提前停止。

诊断只能报告 path、计数、固定错误码和限制值，不打印 Note 正文。

### 9.7 非目标

- 不引入 embeddings 或 vector database。
- 不增加自动记忆写入。
- 不增加自动合并、自动去重或自动过期。
- 不增加 Memory daemon、远程 API 或团队数据库。
- 不为当前规模增加跨 query 索引缓存。

### 9.8 验收标准

- 中英文检索、field boost、links、supersedes 和 recently-recalled 行为保持。
- 无关 query 不注入高分噪声。
- 未明确要求时不调用 <code>memory_save</code>。
- User Notes 可被 Git 跟踪但 Agent 无法写入。
- Working summary 在文件变化后失效。
- recall telemetry 与真实选择一致。
- trace/report 不出现 query、snippet、Note 正文或 secret。
- 现有 Memory quality benchmark 全部通过并增加共享、显式写入和无噪声场景。

## 10. Docker Engine-backed Sandbox

> 2026-07-13 supersession：唯一活跃设计与实施真源是
> Docker Sandbox 的完整实施记录与 ADR-0040 属于历史设计资料；旧 SRT plan 与 ADR-0039 只保留为历史拒绝证据，
> 不属于当前维护文档面。

ADR-0040接受architecture；ADR-0042将当前目标收束为严格本机MVP，分布式发布状态仍为`NO-GO`：

- Source Root和Pico state永不挂载；所有模型可见工具共享filtered Execution Root，Shell只在exact managed
  Docker image的短生命周期container中执行。
- Tool approval只授权staging mutation；Session结束后持久化immutable trusted diff，Source Apply需要第二次
  显式授权、baseline CAS、external reservation、journal和rollback。顺序固定为external control lock → source
  mutation lock → exact reservation → journal/blobs → source-local guard → Session applying → source mutation；
  authority只以anchored full-record CAS清理。
- D1 standalone fixture的Sandbox Feasibility Approval只允许进入D2-D6实现，不解锁正式入口。
- D6必须用production owners从clean wheel重跑vertical corpus并hard-cut SRT；D7四目标可信聚合后由wheel外
  detached Sandbox Product Enablement解锁分布式发布。
- D1缺失阻断实现/发布流程，但不是runtime credential。本机MVP每次启动生成sealed local authorization，只绑定
  当前安装树与already-present exact packaged image；release controller仍可用24小时nonce-bound Candidate
  Attestation执行四平台public smoke，Product Enablement继续作为分布式发布门。
- local/Candidate/Product任一授权identity不一致时，`--sandbox`在Provider/target前fail closed，zero host fallback。
- `status/list/inspect/diff/prune --dry-run`保持zero mutation，public diff读取不改变ctime；Apply中断只由显式
  `pico --cwd <lexical-source> sandbox reconcile --yes`从external authority O(1)定位，不扫描猜测state root。
- 当前production trust root/KMS为空；image-set v2只有无registry reference的`linux/arm64`本地记录，没有
  `linux/amd64`，也没有92个production和4个candidate-smoke真实artifact，因此分布式发布保持`NO-GO`；这些延期项
  不阻断当前arm64主机上exact local image的MVP。

## 11. 可观测性

### 11.1 当前实现

Pico 已经有：

- <code>trace.jsonl</code>：运行过程。
- <code>report.json</code>：最终结果与聚合指标。
- <code>task_state.json</code>：当前任务状态。
- Canonical session、Checkpoint Record、Tool Change Record 和 verification evidence。
- Provider request/transport attempt、usage、duration、stop reason 和 failure evidence。

因此不需要重新建设 tracing subsystem。

### 11.2 已解决问题（历史设计基线）

以下问题已由 OBS-001 的 Trace Envelope 和 Run Summary 关闭，保留用于解释当前 schema 来源：

- trace event 缺少统一 schema version。
- run/task/attempt/tool-use 关联字段并非所有事件都有。
- 部分事件直接携带 args/result，难以作为稳定的低敏感合同。
- 用户只能使用 <code>runs show</code> 查看原始工件，没有聚合的运行解释。

### 11.3 Trace Envelope

所有新 trace event 使用：

~~~json
{
  "trace_schema_version": 1,
  "event_id": "evt_...",
  "event": "tool_completed",
  "created_at": "2026-07-12T00:00:00Z",
  "run_id": "run_...",
  "task_id": "task_...",
  "attempt": 2,
  "tool_use_id": "toolu_..."
}
~~~

字段规则：

- <code>event_id</code> 每条事件唯一。
- <code>run_id</code>、<code>task_id</code> 总是存在。
- <code>attempt</code> 仅在 Model/Action 相关事件存在。
- <code>tool_use_id</code> 仅在 Tool 相关事件存在。
- 不增加 span tree；当前关联需求不需要 parent/span abstraction。
- 每次 run 恰好一个 terminal event。

### 11.4 安全内容策略

默认 trace 允许：

- 类型、ID、status、reason code。
- token/count/usage。
- duration。
- effect 文件数量和 change kind 统计。
- approval、policy、sandbox metadata。
- Provider 名称和模型标识。

默认 trace 禁止：

- 用户 prompt。
- Model completion。
- Tool args 与 tool result。
- Shell stdout/stderr。
- Memory query、snippet 与正文。
- 文件正文。
- Provider request/response body 和 headers。

完整内容继续存在于已有私有 session、raw tool result 或 recovery artifact 中，并经过现有 redaction；trace 不再复制一份。

### 11.5 Run Summary

只新增一个用户入口：

~~~bash
pico runs summary <run_id|latest>
pico runs summary latest --format json
~~~

输出结构：

~~~json
{
  "run": {
    "run_id": "...",
    "task_id": "...",
    "status": "completed",
    "stop_reason": "final",
    "duration_ms": 1200
  },
  "model": {
    "attempts": 2,
    "transport_attempts": 2,
    "input_tokens": 12000,
    "output_tokens": 600
  },
  "context": {},
  "tools": {
    "calls": 3,
    "allowed": 3,
    "denied": 0,
    "status_counts": {}
  },
  "memory": {
    "recall_candidates": 4,
    "recall_selected": 1,
    "filter_counts": {}
  },
  "sandbox": {
    "active": true,
    "calls": 1,
    "outcome_counts": {}
  },
  "effects": {
    "changed_files": 2,
    "partial_successes": 0,
    "recovery_review_required": false
  }
}
~~~

text 与 JSON 必须来自同一结构化 payload。

<code>runs show</code> 保持原始 artifact inspection；不改变其现有用途。

### 11.6 合同切换与迁移

- runtime writer 和 reader 只支持当前合同，不维护多代分派、读时 converter 或 legacy alias。
- Trace/Report 与 Tool Change 确需升级时，只允许通过显式 <code>pico migrate</code> 运行独立 converter；converter 不进入正常 runtime。
- 迁移仅覆盖 MIG-OBS 与 MIG-TOOL 两个已确认切片，使用 same-filesystem candidate、durable journal、current-reader validation 和可恢复 cutover。
- unsafe、ambiguous、identity mismatch 或包含新合同禁止内容的旧记录使对应切片整体失败；不得猜测、补造现代 policy/sandbox 证据或产生混合权威目录。
- <code>runs summary</code> 只读取 current Report/Trace/TaskState；遇到未迁移或损坏工件时报告 incomplete/migration required，不从自由文本推断状态。

### 11.7 非目标

- 不增加实时 stdout event stream。
- 不增加 stdin RPC。
- 不增加 subscriber/hook API。
- 不增加 OTel SDK、Sentry 或远程 collector。
- 不增加 Dashboard。

### 11.8 验收标准

- 每条新事件有 schema version、event ID、run ID、task ID 和时间。
- Model/Tool 事件有对应 correlation ID。
- 每次 run 恰好一个 terminal event。
- Run Summary 可由 trace/report 重建。
- 安全事件中不出现被禁止的内容。
- redaction 或 summary 失败不改变 Agent 主流程结果。

## 12. 评测体系

### 12.1 当前资产

当前已有：

- 全量 pytest 与 ruff。
- fixed coding benchmark。
- synthetic context/memory/security/recovery experiments。
- memory quality scenarios。
- perf request/retrieval/recall/security/recovery harness。
- Provider benchmark。
- offline live assertions。
- live E2E fixtures 与结果格式。
- build/distribution smoke。

统一入口和功能 gate 已实现；同 machine Linux performance baseline 仍缺失，因此 EVAL-001 保持
`in_progress`，但不需要新的 benchmark framework。

### 12.2 单一入口

新增一个标准库脚本：

~~~bash
uv run python scripts/evaluate.py --suite core-fast
uv run python scripts/evaluate.py --suite core-functional
uv run python scripts/evaluate.py --suite core-full
uv run python scripts/evaluate.py --suite sandbox-contract
uv run python scripts/evaluate.py --suite sandbox-real
uv run python scripts/evaluate.py --suite live --provider <name>
~~~

脚本只编排现有 runner 并聚合结果，不复制各 benchmark 的评分逻辑。

输出：

~~~text
artifacts/eval/<timestamp>-<suite>.json
artifacts/eval/<timestamp>-<suite>.md
~~~

artifact 继续被 Git ignore。提交一个：

~~~text
benchmarks/baselines/core-v1.json
~~~

用于保存场景 ID、格式版本和必要阈值。

不增加 YAML manifest、插件发现或第三方 benchmark dependency。

### 12.3 Suite

#### core

完全离线并作为 PR gate：

- ruff。
- 全量 pytest。
- fixed benchmark。
- context budget scenarios。
- memory quality fake scenarios。
- tool policy/security corpus。
- recovery ablation。
- build/distribution smoke。
- 选定 perf scenarios。

#### sandbox

- 无Docker的strict manifest/path/plan/state/apply contract tests。
- D1只在临时fixture运行本机Docker integration，不接正式`--sandbox`。
- D6从clean wheel调用production owners运行完整vertical corpus。
- D7四平台production mandatory/soak、可信聚合和public enablement smoke。
- policy、mount、network、filesystem、process-tree、resource、cleanup、apply与fail-closed tests。

#### live

- 单 Provider 行为验证。
- transport/cost evidence。
- credential/artifact security。
- persistence/fixture 恢复。

live 不作为普通 PR gate，运行前仍需明确授权。

### 12.4 核心指标

#### Context

- <code>within_budget</code>。
- dropped turns。
- dropped/truncated source counts。
- required source 保留率。
- digest applied count。
- request build duration。

#### Tool

- calls/allowed/denied。
- reason code counts。
- approval required/approved/denied。
- runner executed count。
- partial success。
- recovery review required。

#### Memory

- candidate count。
- selected count。
- expected hit at top-k。
- irrelevant injection count。
- recently-recalled suppression。
- stale summary injection count。
- explicit save compliance。

#### Sandbox

- active calls。
- outcome counts。
- policy denial。
- unavailable/fallback count；fallback 必须始终为 0。
- timeout 与残留进程。

#### Observability

- event schema completeness。
- terminal event count。
- correlation completeness。
- forbidden-content leakage count。

### 12.5 Gate

确定性功能场景：

- 必须 100% 通过。
- 新增场景必须有稳定 ID。
- 失败输出场景 ID、当前值、期望值和 artifact path。

性能：

- 同一场景 warmup 后运行多轮。
- 比较 process medians。
- 只有同时满足“超过 committed baseline 2 倍”和“绝对增加超过 5 ms”才失败。
- p95 只报告，不阻断。
- baseline 更新必须与解释性能变化的代码变更同 PR。

安全与 Sandbox：

- 任一 forbidden access 成功即失败。
- 任一 <code>--sandbox</code> 场景发生宿主回退即失败。
- 任一 secret/content 泄漏即失败。

### 12.6 CI 安排

- Python 3.11/3.12 继续跑 lint 和全量 tests。
- `core-functional` 聚合 gate 只在一个固定 Linux/Python 3.12 job 运行，避免重复耗时。
- `core-full` 额外运行性能比较，只允许在已有同 machine class committed baseline 的 runner 上执行；当前缺
  Linux baseline，不能复制或改标 `darwin-arm64` 数值来解锁。
- 当前CI中的SRT stop-gate只证明superseded入口fail closed，不构成Docker real evidence。
- D1临时fixture证据不进入普通PR gate且只授权实现；D6才切换production suites。
- D7由release controller注入signed expected-input manifest；同一universal wheel绑定canonical image-set v2，
  每个job按架构验证对应OCI record并聚合wheel/image-set/policy/corpus证据。正式Product签发前的enabled smoke只由
  nonce-bound Candidate Attestation触发，必须不写Product cache且artifact仍为`product_enablement=false`。
- live suite 不使用 PR secret 自动运行。

### 12.7 验收标准

- 一个命令能生成完整 core 结论。
- 失败能定位具体 scenario 和 metric。
- 现有 benchmark 评分逻辑没有被复制。
- 普通 CI 不依赖真实 Provider。
- perf gate 在合理 runner 波动下稳定。
- artifact 不包含 prompt、Note 正文、tool result、Shell 输出或 secret。

## 13. 实施顺序

### 阶段 0：Spec 收口

- 完成本文档。
- 将非核心能力移出活跃路线图。
- 锁定显式启用、zero fallback、container外断网、filtered staging、双门和显式 Memory 写入。

完成标准：本文档成为后续设计主真源。

### 阶段 1：可观测与评测骨架

1. OBS-001 Trace Envelope。
2. Context/Tool/Memory/Sandbox 安全 metadata。
3. <code>runs summary</code>。
4. EVAL-001 统一 runner 与 core baseline。

原因：先建立观察和验收能力，后续行为变更才能量化。

### 阶段 2：Context 与 Memory

1. CTX-001 全 request 预算。
2. CTX-002 Context Breakdown。
3. 删除未使用的 MemoryRefresher。
4. MEM-001 团队 User Notes Git 边界。
5. MEM-002 recall selection metadata 与 doctor。

完成标准：预算、召回与团队知识都能被安全解释和离线验证。

### 阶段 3：Tool Policy

1. effect class 合入固定 registry。
2. 统一 Policy Decision。
3. 让 Shell、Memory 和普通工具使用同形 metadata。

完成标准：所有工具拒绝和执行路径都能用固定字段解释，现有安全与恢复测试无回退。

### 阶段 4：Sandbox V1 D0-D6

1. D0以ADR-0040切换真源，只接受architecture；该阶段distributed product保持NO-GO，后续ADR-0042另行接受本机MVP。
2. D1只用standalone fixture验证exact本机Docker/image/policy与mandatory corpus；Feasibility Approval只允许实现。
3. D2-D5实现Sandbox Session/staging、Docker runner、统一Execution Root、trusted diff与Source Apply owners。
4. D6从clean distribution运行production vertical gate，完成CLI/observability/distribution与SRT hard-cut；public
   Product Enablement仍blocked。

完成标准：D6 production artifact全部通过且旧SRT production path不可达；D1 fixture不得替代production evidence。

### 阶段 5：Sandbox V1 D7 与发布复核

- 同一exact universal wheel、canonical image-set v2、policy与corpus在macOS Desktop arm64/x86_64和Linux
  rootless amd64/arm64运行完整门禁；每个平台选择其`linux/arm64`或`linux/amd64` OCI record。
- trusted aggregator拒绝混跑、重复、重放、自报归属和不完整输入；production aggregate后只生成未发布、
  job-scoped candidate attestation，四平台public CLI smoke也进入final matrix，全部通过后controller才签发并
  发布detached Product Enablement。
- ADR-0041已冻结release authority：RSA-PSS-SHA256、3072-bit/e65537/32-byte salt、domain-separated canonical
  ASCII JSON、wheel内immutable public-key map、rotation/revocation/expiry/rollback，以及fixed GitHub Releases
  channel、no proxy、HTTPS allowlist和256 KiB上限。hash、自签JSON、artifact-supplied key或test key不能解锁
  production；真实production public key/KMS仍不存在，因此D7保持blocked。
- 只有D7完成后才允许distributed小团队试运行；ADR-0042的exact local MVP是独立、explicit-on的本机路径，
  不构成该发布结论。默认仍关闭。
- 使用真实 Run Summary 观察 denial、timeout、fallback 和兼容性。
- 是否默认启用需要新的独立决策，不属于本 Spec 当前实施范围。

每个阶段一次只推进一个垂直切片，完成 focused tests、全量 tests、ruff 和对应 eval suite 后再进入下一项。

## 14. 明确冻结或拒绝的方向

| 方向 | 当前决定 | 重新考虑条件 |
| --- | --- | --- |
| 公开模型矩阵扩张 | 冻结 | 出现明确的第二个 CLI 模型需求 |
| 额外公开协议入口 | 非本轮 | 固定 Anthropic Messages 路径尚未出现第二个公开协议需求 |
| Skills | 冻结 | 至少两个无法用 AGENTS.md/Memory 解决的真实复用用例 |
| Plugins / Marketplace | 拒绝 | Pico 产品定位发生变化 |
| MCP | 冻结 | 外部服务集成成为明确产品目标 |
| Session tree / fork | 冻结 | 用户确实需要同 session 多分支，而新 session 复制不足 |
| Targeted compaction | 冻结 | 预算评测证明 history drop 无法满足长任务 |
| Steering / follow-up queue | 冻结 | 交互中断需求有可复现用例 |
| 并行工具 | 拒绝通用实现 | read-only latency 成为已测量瓶颈时只评估 read-only batch |
| JSONL RPC / daemon | 冻结 | 出现真实 IDE/orchestrator 控制端 |
| OTel / Dashboard | 拒绝当前实现 | 有明确 collector 和运维责任人 |
| Vector DB / Embedding | 拒绝当前实现 | BM25/CJK 在固定质量集上持续不达标 |
| 自动长期记忆 | 拒绝 | 用户明确改变产品偏好并接受错误记忆治理成本 |
| Sandbox backend registry / OpenShell 多后端 | 拒绝当前实现 | 单一Docker runner真实不满足必须支持的平台，且新backend有独立威胁模型与门禁 |
| Windows sandbox | 冻结 | Pico 的 Windows 文件安全与 CI 基线先完成 |
| 项目网络白名单 | 拒绝首版 | 默认断网阻塞高频核心工作流且有安全审查方案 |

## 15. Pi、Claude Code 与 Pico 的参考对照

### 15.1 证据边界

主要参考：

- Pi：<https://github.com/earendil-works/pi>
- Claude Code How it works：<https://code.claude.com/docs/en/how-claude-code-works>
- Claude Code Permissions：<https://code.claude.com/docs/en/permissions>
- Claude Code Sandboxing：<https://code.claude.com/docs/en/sandboxing>
- Anthropic Sandbox Runtime：<https://github.com/anthropic-experimental/sandbox-runtime>

Claude Code 完整 runtime 并未公开，因此只参考官方公开行为与合同，不推断内部实现。

### 15.2 定位对照

| 维度 | Pi | Claude Code | Pico 决定 |
| --- | --- | --- | --- |
| 产品 | 开放、可扩展 Agent harness | 完整商业 coding-agent 平台 | 小型、本地、证据与恢复优先 |
| Agent loop | streaming、多 tool、steer/follow-up | gather → act → verify，可中断 | 单 attempt / 单 Action |
| Context | Skills、compaction、session tree | context breakdown、rules、subagent context | 固定 sources、预算闭环、可解释 breakdown |
| Tools | 扩展和自定义工具丰富 | permission、plugins、MCP | 固定 registry、统一 policy |
| Memory | session/context 可扩展 | 分层 instructions/session 能力 | Working + 人工审阅 Durable Memory |
| Sandbox | 默认宿主权限，建议外部隔离 | permission + Bash sandbox | approval + Docker filtered staging + recovery + explicit source apply |
| Observability | typed runtime events | OTel/usage/enterprise analytics | 本地安全 trace/report/summary |

### 15.3 从 Pi 学什么

吸收：

- 核心 loop 与 Provider/tool 边界保持清晰。
- 运行状态通过结构化事件表达。
- 安全边界不足时诚实依赖外部 sandbox，而不是用 approval 冒充隔离。
- 扩展能力必须由真实用例驱动。

暂不吸收：

- 完整 Provider matrix。
- extensions、Skills、RPC。
- session tree。
- 通用并行 tool execution。
- TUI 与 SDK 平台。

Pi 官方说明默认不提供文件、进程、网络或 credential 权限隔离，并建议 OpenShell、Gondolin 或 Docker。Pico
采用同样诚实的边界判断，选择本地Docker隔离任意Shell进程，并用filtered staging避免把真实source/state挂载给guest。

### 15.4 从 Claude Code 学什么

吸收：

- Context Breakdown 对用户解释 token 来源。
- permissions 与 sandbox 是互补层。
- Bash sandbox 覆盖整个子进程树，内建文件工具继续走自身权限系统。
- OS/process/network隔离应复用成熟runtime；Pico只拥有自身staging、policy、evidence和apply合同。
- 默认 observability 应以 metadata 为主，不依赖内容采集。

不吸收：

- 企业 managed settings/MDM。
- Plugin、MCP、Marketplace、agent teams。
- 自动 unsandboxed escape hatch。
- 全量 OTel 与远程 analytics。

Claude Code 文档允许配置 sandbox unavailable 时回退；Pico 的显式 <code>--sandbox</code> 语义更严格：一旦用户要求 sandbox，任何不可用状态都 fail closed。

### 15.5 Pico 应继续保持的差异

- 单 Action 的可证明执行语义。
- 对实际 workspace effects 的观察。
- pending/partial/interrupted Tool Change 证据。
- Shell 修改也进入恢复链路。
- secret、私有文件和 trusted executable 的 fail-closed reader。
- Python core 零 runtime dependency。
- 不通过功能数量竞争。

## 16. 单项设计提案模板

后续新增或修改设计时使用：

~~~markdown
### <ID> 标题

- 主线：Context / Tool / Memory / Sandbox / Evaluation / Observability
- 优先级：P0 / P1 / P2
- 状态：proposed

#### 问题与证据

- 当前可观察问题：
- 相关实现：
- 复现或基线：

#### 目标

描述可验证结果。

#### 非目标

明确本项不建设的能力。

#### 最小方案

优先复用现有实现；说明数据流、接口和失败语义。

#### 安全与兼容性

- trust boundary：
- 持久化：
- CLI/public contract：
- 平台：

#### 验收标准

- [ ] focused tests
- [ ] 全量 tests / lint
- [ ] 对应 eval suite
- [ ] 文档更新

#### 实施记录

- Commit：
- 验证结果：
- 遗留问题：
~~~

一个提案如果不能归入六条主线，应先说明为什么它仍属于 Pico 当前产品目标；否则保留在冻结区，不进入实现。

## 17. 决策记录

| 日期 | 决策 | 状态 |
| --- | --- | --- |
| 2026-07-12 | Pico 面向本地、小团队共享，不建设企业控制平台 | accepted |
| 2026-07-12 | 活跃路线图只保留 Context、Tool、Memory、Sandbox、Evaluation、Observability | accepted |
| 2026-07-12 | Canonical Messages、单 Action、effect observation 和 recovery 不变量保持 | accepted |
| 2026-07-12 | 长期 Memory 仅显式写入 | accepted |
| 2026-07-12 | 团队知识使用仓库内 User Notes + Git Review | accepted |
| 2026-07-12 | Sandbox 首版使用外部 SRT，不自研 OS 隔离 | superseded by ADR-0040 |
| 2026-07-12 | Sandbox 默认关闭，通过 <code>--sandbox</code> 显式启用 | accepted |
| 2026-07-12 | Sandboxed Shell 默认完全断网且禁止本地 IPC | superseded by ADR-0040 |
| 2026-07-12 | 显式启用 Sandbox 后 fail closed，不允许宿主回退 | accepted |
| 2026-07-12 | 不实现 Plugins、MCP、RPC、Session tree、通用并行工具和 OTel | accepted |
| 2026-07-12 | 首次显式 `--sandbox` 自动安装固定 Node + SRT；普通模式不安装、不联网 | superseded by ADR-0040 |
| 2026-07-12 | managed Sandbox executable 使用内部 identity capability，不进入普通 trusted PATH | superseded/redefined by ADR-0040 |
| 2026-07-12 | 持久化升级只通过显式事务迁移处理 MIG-OBS/MIG-TOOL；runtime current-only | accepted |
| 2026-07-12 | F0 feasibility 是生产接入硬门；失败不提供 weaker mode 或 host fallback | superseded by separate D1 development / D7 release gates |
| 2026-07-13 | Sandbox V1使用local Docker Engine/Desktop + filtered staging，不挂载Source/State | accepted by ADR-0040；distributed product NO-GO，exact local后由ADR-0042另行接受 |
| 2026-07-13 | D1 Feasibility Approval只授权D2-D6实现；D7 detached Product Enablement解锁distributed release runtime | superseded for exact local MVP by ADR-0042 |
| 2026-07-15 | 每次重算的sealed local authorization可解锁当前安装与already-present exact image的本机MVP | accepted by ADR-0042；distributed release仍NO-GO |
| 2026-07-13 | Tool approval只修改staging；Source Apply使用immutable diff、独立授权、CAS和journal | accepted |
| 2026-07-15 | Source Apply先发布external reservation，authority使用control-dir anchored full-record CAS；显式reconcile处理source replacement | accepted |
| 2026-07-13 | container外网络全禁，container内loopback/private IPC允许 | accepted by ADR-0040 |
| 2026-07-14 | Docker Sandbox release使用ADR-0041 detached RSA-PSS authority、image-set v2与candidate-only smoke | accepted；production key/evidence仍NO-GO |

## 18. 变更日志

| 日期 | 变更 |
| --- | --- |
| 2026-07-12 | 创建初始项目审计、验证基线与 Pi/Claude Code 对标 |
| 2026-07-12 | 根据产品范围将 Spec 重构为六条聚焦主线 |
| 2026-07-12 | 将 SRT、默认断网、显式启用和 fail-closed 固化为 Sandbox 方案 |
| 2026-07-12 | 固化显式长期记忆与仓库 User Notes 团队共享方案 |
| 2026-07-12 | 将 Plugins、MCP、RPC、Session tree、Steering、通用并行和 OTel 移出活跃路线图 |
| 2026-07-12 | 对齐完整实施路线图：改为 explicit-on 自动安装 managed Node/SRT、内部 identity capability 与 MIG-OBS/MIG-TOOL 显式事务迁移 |
| 2026-07-13 | ADR-0040 supersede SRT路线，主Spec切换为Docker + filtered staging、D1/D7双门和显式Source Apply |
| 2026-07-14 | ADR-0041冻结RSA-PSS release authority、universal wheel image-set与candidate/Product分离；未签发生产key或Product Enablement |
| 2026-07-15 | ADR-0042收束为already-present exact image的本机MVP；prepare零网络/零mutation，distributed release仍NO-GO |
