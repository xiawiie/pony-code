# Pico 下一阶段优化与演进设计

- 状态：Reviewed / Approved for implementation planning
- 代码基线：`99f9a8f5788231b5360c63426a3ebe2d3f20cf4c`
- 工作树基线：dirty；审计结论必须记录 commit 与 dirty state
- 产品范围：本地运行、小团队共享
- 实施约束：单人主导，目标 2–3 个月；macOS 本机 + GitHub Actions Linux
- 兼容策略：运行时只读当前合同；旧私有工件通过一次性事务迁移，成功后删除旧工件，不保留兼容分支

## 1. 设计结论

Pico 当前已经具备成熟的 Agent Loop、工具安全、effect observation、recovery、Memory 存储检索、运行工件和评测资产。本阶段不重写 runtime，不建设插件/MCP/RPC/daemon/vector database，不复制 Pi 或 Claude Code 的完整功能。

本阶段的核心问题不是缺少 Agent framework，而是已有能力尚未形成完整反馈闭环：Context 预算尚未覆盖整个 request，Tool policy 事实分散，Memory 的团队边界和显式写入尚未由 runtime 强制，批准后的 Shell 尚未具备 OS 隔离，Trace/Report/Summary 合同不够稳定，已有评测无法由一个 gate 统一回答是否回退。

优化后的主线为：

```text
私有状态迁移与评测基础
        ↓
低敏感证据合同（Trace / Report / Summary）
        ↓
Context 全 request budget
        ↓
Tool Policy Decision
        ↓
Memory 边界与可解释召回
        ↓
SRT Shell Sandbox（先 feasibility，再 macOS，再 Linux CI）
```

六条产品主线保留，但工程上增加横向工作包 `MIG-001`，并提前增加 `SBOX-000` feasibility spike。

## 2. 不变量

1. Canonical Messages 仍是唯一会话 transcript。
2. 一次 Model Attempt 只产生和执行一个 Action。
3. 未知工具或无效工具定义 fail closed。
4. approval、sandbox、effect observation、recovery 是四层不同能力，不能互相替代。
5. 最新用户输入不得被静默截断；required-only 超限时 Provider 不得被调用。
6. Shell 无论成功、失败、超时、被中断或部分执行，都进入 effect observation 和 terminalization。
7. secret redaction、私有文件、路径、trusted executable、Git hardening、恢复记录的现有安全边界不得削弱。
8. Python core 保持标准库实现和零 runtime dependency。
9. Sandbox 显式开启后任何 bootstrap、policy 或 runner 错误都不得回退宿主执行。
10. 运行时只读取当前持久化合同；旧格式只由独立迁移器读取。
11. 行为模块生产事实；Trace、Report、Summary 和 Evaluation 只投影或聚合事实，不通过 free-text 重新推断。
12. 同一事实只计算一次，消费者不得各自重新分类。

## 3. 事实生产者与证据消费者

| 事实 | 唯一生产者 | 消费者 |
|---|---|---|
| 最终 request token allocation | Context request builder | Report、Trace projection、Summary、Evaluation |
| Memory selection | Retrieval selection | Context、Report、Trace projection、Evaluation |
| Tool policy | ToolExecutor policy phase | Tool Result、Tool Change、Trace、Summary |
| Approval outcome | Approval phase | Policy、Trace、Report |
| Sandbox outcome | SRT adapter | Tool Result、Tool Change、Trace、Summary |
| Workspace effects | WorkspaceObserver | Tool Change、Recovery、Summary |
| Verification evidence | verification recorder | Checkpoint、Report、Evaluation |
| Terminal run state | single run finalizer | TaskState、Trace、Report、Summary |

## 4. MIG-001：私有状态事务迁移

### 4.1 范围

迁移对象限定为 Pico 私有状态：sessions、runs、checkpoints、Tool Change Records、私有 Agent Notes、相关 blobs 和新 migration metadata。User Notes (`.pico/memory/notes/**/*.md`) 是人维护的团队知识，不随私有 runtime 状态隐式改写；只做只读结构验证。

locks、temp、cache 和可再生 evaluation artifacts 不迁移。

### 4.2 事务流程

```text
获取 workspace migration lock
→ 记录 inventory（commit、dirty、runtime、platform、hash）
→ 检查权限、空间、source identity
→ 在目标私有根同一文件系统的 staging 目录转换
→ 校验所有新记录和跨工件引用
→ fsync 文件与目录
→ 旧私有根原子改名为 rollback staging
→ 新私有根原子切换
→ 用新 reader 做只读启动验证
→ 验证成功后删除 rollback staging
→ fsync 父目录
```

禁止跨文件系统 copy-and-delete fallback。任一转换、校验、切换或新 reader 验证失败：保留完整旧状态，不产生新旧混合，runtime 拒绝继续使用该 workspace。

正常 reader 不保留旧版本分支；旧解析器只能存在于 migration command。若本轮没有改变某一工件合同，不为 telemetry 变化强制升级其 format version。

## 5. OBS-001：低敏感证据合同

### 5.1 Trace Envelope

所有新事件由 `emit_trace()` 自动加入：

```json
{
  "trace_schema_version": 1,
  "event_id": "evt_...",
  "event": "tool_completed",
  "created_at": "...",
  "run_id": "run_...",
  "task_id": "task_...",
  "attempt": 2,
  "tool_use_id": "toolu_..."
}
```

Envelope 保持扁平结构，避免无必要的 payload nesting。attempt 和 tool-use correlation 通过显式可选参数传入，不建设 span tree。

### 5.2 Trace 禁止内容

Trace、Report、Summary 和 Evaluation artifact 默认不得包含：用户 prompt、模型 completion、tool args/result、Shell stdout/stderr、Memory query/snippet/body、文件正文、完整路径和 secret。Trace 只记录类型、ID、status、reason、token/count/usage、duration、effect 统计、approval/policy/sandbox metadata。

`run_started` 不再保存用户请求；tool 事件不保存 args/result；verification 事件不保存完整 command。私有 Session、TaskState、raw tool result、recovery artifact 继续承担内容存储，并经过既有 redaction。

### 5.3 Report 与 Summary

Report 是低敏感终态聚合，不再复制 final answer、完整 TaskState 或 Working Memory 正文。TaskState 和 Canonical Session 保留私有内容状态。

Run Summary 以 Report 为主，Trace 只做完整性校验：schema、event count、terminal event count、correlation、malformed/lifecycle incomplete。Trace 损坏时仍展示安全可用的 Report 字段，但标记 `summary_complete=false`。

Summary 必须来自同一结构化 payload 生成 text 和 JSON，不保存 prompt、completion、query、snippet、args、result 或 secret。

## 6. CTX-001/002：Context 全 request budget

### 6.1 预算

```text
input_limit = total_budget_hard_cap
             - max_new_tokens
             - context_safety_margin_tokens
```

`system_tools_hard_cap` 保留为固定前缀健康边界，不作为总预算真源。Injection source budgets、history soft cap 是选择偏好，不替代最终 hard cap。

### 6.2 构建顺序

```text
冻结 injection snapshot
→ 构建 Canonical Messages request
→ sanitize provider payload
→ 锁定本 request token_count_mode
→ required-only feasibility check
→ history soft-cap 删除最老完整 turn
→ 按固定顺序删除 optional sources
→ 必要时继续删除最老完整 turn
→ 最终 payload 重计数并断言 within_budget
→ 生成 Context Breakdown
→ 返回 request
```

Provider token count 必须按 request 锁定模式：`provider_request`、`provider_text` 或 `estimate`。任一 component 的 Provider 计数失败时，整次 request 从头使用 estimate 重算，禁止逐 component 混用。默认不增加远程 count_tokens HTTP 请求。

Source snapshot 只选择和裁剪，不重新 recall/render。required checkpoint 必须在生成时 bounded；request builder 不截断它，required-only 超限则返回 `context_budget_exceeded`，Provider 调用次数为 0。

### 6.3 Breakdown

Breakdown 至少包括：schema version、count mode、hard cap、reserved output、safety margin、input limit、final input、headroom、required feasibility、各 source 的 included/empty/truncated/dropped_budget/failed 状态、soft-cap 与 hard-cap dropped turns、digest count。不得包含正文、query、tool args、tool result、secret 或完整用户路径。

## 7. TOOL-001：统一 Tool Policy

### 7.1 Registry

固定 tool registry 成为 schema、description、effect class 的唯一真源。effect 只使用 `read_only`、`workspace_write`、`memory_write`。注册工具缺失 effect 是 invalid tool definition 并拒绝；未知工具保守分类为 workspace_write 但 `decision=deny`。

`risky` 不等于动态 `risk_class`。Shell 风险继续由 `assess_command()` 根据参数动态产生；effect、approval kind、risk class、decision 分层表达。

### 7.2 Policy Decision

```json
{
  "schema_version": 1,
  "decision": "allow",
  "reason_code": "allowed",
  "effect_class": "workspace_write",
  "risk_class": "complex",
  "approval": {
    "mode": "ask",
    "required": true,
    "outcome": "approved"
  }
}
```

`allow` 只在 validation、allowed-tools/read-only/repeated-call、approval revalidation、trusted executable/Git preflight、sandbox preflight 全部通过后冻结。Policy 不因 target exit code、effect 或 recovery outcome 重写。

Policy、Sandbox outcome、Tool status、Recovery status 分层。`sandbox_denied` 不作为最初 policy reason；target 在 OS sandbox 中被拒绝时 policy 仍可为 allow，sandbox outcome 表达拒绝。

被拒绝调用 runner count 必须为 0；被允许调用 runner count 必须为 1。允许且产生状态变化的调用把冻结 policy 写入 pending Tool Change；执行前拒绝不创建假的 Tool Change。

## 8. MEM-001/002：Memory 边界与召回

### 8.1 三层记忆

Canonical Messages 是会话事实；Working Memory 是 bounded session 状态；Durable Memory 只有 User Notes 和 Agent Notes。Agent 不自动保存长期总结，不把 Agent Notes 自动提升为 User Notes。

User Notes 继续位于 `.pico/memory/notes/**/*.md`，Agent Notes 继续 append-only。`.gitignore` 改动必须配合真实 `git check-ignore` 和 repository structure tests：User Notes 可跟踪，Agent Notes、sessions、runs、checkpoints、locks 和其他私有工件仍被忽略。

内建 write/patch 始终拒绝 User Notes。Sandbox 开启时 Shell 通过 OS denyWrite 保护；Sandbox 关闭时 Shell 仍是明确的宿主授权能力，不宣称 OS 强制保护。

### 8.2 显式写入

由当前 top-level 用户输入确定性识别 `memory_write_intent`。模型不能自行设置；历史消息不继承；delegate 默认关闭。无明确保存意图时：

```text
memory_save → deny → explicit_memory_request_required → runner=0
```

不引入语义 intent classifier。

### 8.3 召回

Retrieval 拆为 selection/rendering 两步但复用同一 BlockStore snapshot，不重新扫描。分别统计 candidate、selected、included、dropped_budget 和 filter counts。`recently_recalled` 只在 note 最终进入 Model Request 后更新。`normalized_score` 仅表示本次 query primary score 内的相对排序，不表示概率。

Trace/Report 不保存 query、snippet 或正文；工具输出可在 bounded 范围内展示 snippet。Doctor 只报告固定错误码、path/计数/限制值和 Git tracking 状态，不打印 Note 正文。

## 9. SBOX-000/001：SRT Sandbox

### 9.1 Feasibility first

在生产接入前验证固定 SRT 版本的真实 settings schema、allow/deny 优先级、macOS Seatbelt、Linux bubblewrap/user namespace/seccomp、网络/localhost/Unix socket、process tree timeout、npm package identity 和 Node 版本。Linux hosted runner mandatory capability 不可用时收缩支持范围，不降低隔离、不回退宿主。

### 9.2 执行边界

普通 argv、complex shell 和 hardened Git 必须汇入一个窄的 Approved Shell Execution 入口。保留 host runner 作为默认关闭模式，但 SRT adapter 不接受 host fallback callable；SRT 失败即失败。

Complex shell 使用显式 `[approved_shell, "-c", exact_approved_command]`，SRT adapter 不再使用额外 `shell=True`。SRT 不是普通 trusted executable，不进入通用 registry；使用独立 operator trust model：不在 workspace，launcher/manifest/package entry/Node identity 冻结并在每次调用前复验，child 无权修改其安装。

### 9.3 Git

纯文件系统的 `.git`、gitfile、linked worktree、submodule 和 argv 检查可在宿主 preflight；会启动 Git 的 `rev-parse`、`config --includes`、`ls-files` probe 和最终 target Git 必须进入 SRT。首版允许多个 SRT invocation，共享同一 call context/policy；不生成 shell helper。

`.git`、`.pico`、User Notes 默认 denyWrite。必须明确 Sandbox 模式不支持需要修改 Git index/metadata 的 Git 工作流。

### 9.4 生命周期与失败

Sandbox bootstrap 在 Agent/run 创建前完成；每次 Shell call 复验 identity、生成 owner-only temp root/settings（0600）、覆盖 HOME/TMPDIR/cache，并删除临时状态。timeout 使用独立 process group、TERM、bounded grace、KILL、wrapper wait 和残留进程 smoke。wrapper 启动后任何异常都进入 effect observation 和 terminalization。

Bootstrap outcome 与 call outcome 分层。target 普通非零 exit 属于 sandbox `completed`，由 tool status 表达命令失败。任何 unavailable、version mismatch、policy invalid、bootstrap failure 都不得宿主回退。

## 10. EVAL-001：统一评测与门禁

不新建 benchmark framework。`evaluate.py` 只编排现有 pytest、ruff、fixed benchmark、ablation、security corpus、build/distribution 和 sandbox runner，读取结构化 artifact，不复制评分逻辑、不解析自由文本作为主要真源、不自动调用真实 Provider。

Suite 拆分为：

- `core-fast`：PR 快速门禁；
- `core-full`：固定 Linux/Python 3.12 完整 gate；
- `sandbox-contract`：fake SRT 与 fail-closed contract；
- `sandbox-real`：macOS/Linux 真实 smoke；
- `live`：显式授权、限制请求数/时间/成本。

统一 artifact 记录 `record_type`、`format_version`、suite、baseline、scenario IDs、status、artifact 相对路径、repo commit、dirty state、Python、platform 和 machine class。功能场景 100% 通过；性能只比较同场景、同 machine class 的 median，默认仅在超过 baseline 2 倍且绝对增加超过 5ms 时失败；p95 只报告。性能失败允许一次确认重跑。

增加 canary corpus 扫描 Trace、Report、Summary、Eval artifact，验证 prompt、completion、tool args/result、Shell output、Memory query/body、secret 和绝对路径不泄漏。

## 11. 12 周基准路线图

| 周期 | 交付 |
|---|---|
| 第 1 周 | Spec 收口、MIG/SRT feasibility、真实基线和 go/no-go |
| 第 2 周 | migration inventory/dry-run、core-fast、scenario/provenance、leakage canary |
| 第 3–4 周 | Trace Envelope、低敏感 projection、Report、Run Summary、完整性测试 |
| 第 5–6 周 | Context total budget、count mode、required feasibility、Breakdown、zero-call gate |
| 第 7 周 | Tool registry effect、Policy Decision、reason/status 分层、runner 0/1 gate |
| 第 8 周 | explicit memory gate、User Notes Git boundary、selection/render、doctor |
| 第 9 周 | 统一 approved Shell execution、Git preflight/probe 拆分，host 行为保持 |
| 第 10 周 | SRT bootstrap/doctor、private env、普通 argv/complex shell、macOS contract |
| 第 11 周 | SRT Git、timeout/process group、partial effects、macOS real smoke |
| 第 12 周 | Linux CI、atomic migration cutover、core-full、distribution、文档收口 |

基准情形约 12 周；保守情形约 17 周。若延期，先删除非关键 doctor、Summary 文本、性能阻断、Linux支持承诺或高级 recall 展示；绝不削弱 fail-closed、effect observation、recovery、最新输入保留、runner 0/1、迁移失败保留旧状态和内容泄漏 gate。

## 12. 阶段 Go/No-Go

SRT：macOS 固定版本真实启动、普通隔离、deny/read/write、timeout 和 Linux mandatory smoke 通过才 Go；否则收缩平台，不提供 best-effort fallback。

Context：required-only overflow 时 Provider=0、单一 count mode、current user 不截断、tool pair 不拆、Breakdown 与真实 request 一致才 Go。

Tool：deny runner=0、allow runner=1、policy snapshot/effect/recovery 一致才 Go。

Migration：inventory 完整、引用图验证、atomic cutover、新 reader 验证、成功删除旧工件、失败无混合状态才 Go。

## 13. 明确不做

本阶段不做 Plugins、Marketplace、MCP、RPC、daemon、OTel、Dashboard、Vector DB、Embedding、自动长期记忆、通用并行工具、Sandbox backend registry、Docker/OpenShell 多后端、项目网络白名单、Session tree/fork、默认开启 Sandbox、远程 token-count 请求和完整 Git 写工作流支持。

## 14. 验收矩阵

- Context：hard cap、reserved output、required overflow、latest input、tool pair、single count mode。
- Tool：unknown/invalid/denied/approved/repeated、runner count、policy snapshot、effect一致。
- Memory：explicit save、User Notes read-only、Git tracking、selection/included、no noisy recall。
- Sandbox：普通读写、敏感读取拒绝、`.git/.pico`写拒绝、network/socket拒绝、child/grandchild、timeout、no fallback。
- Recovery：pending、partial、interrupted、effect observation、verification、引用完整。
- Observability：Envelope、correlation、terminal count、forbidden-content scan。
- Migration：inventory、disk full、interruption、cutover failure、cleanup、无混合格式。
- Evaluation：scenario IDs、baseline provenance、functional 100%、perf tolerance、相对 artifact path。
- Distribution：sdist/wheel、clean install、zero runtime dependency。

## 15. 最终价值判断

保留并列为 P0：全 request budget、Context Breakdown、Policy Decision、Trace 内容收缩、SRT fail-closed、统一 Evaluation gate、事务迁移、explicit memory gate。

保留但列为 P1：recall telemetry、Memory doctor、Summary 丰富文本、Linux real smoke、性能 gate、User Notes Git 共享。

延后：高级 normalized score、远程 token count、read allowlist、SRT probe 性能优化、Memory index cache、Sandbox 默认开启。

该方案的架构可行性和项目匹配度高，安全收益高；单人 2–3 个月按基准情形可行，按保守情形不可保证。必须通过阶段 Go/No-Go 和范围削减保持安全不变量，而不能通过降低 fail-closed 或 recovery 语义维持表面进度。
