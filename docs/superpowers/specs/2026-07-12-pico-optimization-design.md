# Pico 下一阶段优化与演进设计（修订版）

- 状态：Architecture Direction Approved；Implementation Contracts Approved
- 代码基线：`99f9a8f5788231b5360c63426a3ebe2d3f20cf4c`
- 工作树基线：dirty；审计、评测和迁移必须记录 commit 与 dirty state
- 产品范围：本地运行、小团队共享
- 实施约束：单人主导；Release A 目标 2–3 个月；macOS Sandbox 首发；Linux Sandbox 独立发布
- 兼容策略：运行时只读取当前合同；旧工件通过按合同切片的事务迁移转换，成功后删除旧工件，不保留兼容分支

## 1. 最终结论

Pico 当前已经具备成熟的 Agent Loop、工具安全、effect observation、recovery、Memory 存储检索、运行工件和评测资产。本阶段不重写 runtime，不建设 Plugins、MCP、RPC、daemon、OTel、Dashboard、Vector DB、Embedding、自动长期记忆、通用并行工具或 Sandbox backend registry。

当前真正的缺口不是新的 Agent framework，而是已有能力没有形成闭环：Context 的总 request 硬预算尚未落实；Tool policy 事实分散；Memory 的显式写入和团队边界没有完全由 runtime 强制；审批后的 Shell 尚无 OS 隔离；Trace/Report/Summary 合同不够稳定；已有评测缺少统一 artifact/gate；合同升级没有按发布切片配套迁移。

整体方案采用：

```text
合同与迁移基础
→ 低敏感证据合同
→ Context 全 request budget
→ Tool Policy Decision
→ Memory 边界与可解释召回
→ Approved Shell Execution
→ macOS Sandbox Release A
→ Linux Sandbox Release B
```

六条产品主线保留，新增横向 `MIG-001`，并将 `SBOX-000` feasibility 提前到生产接入前。当前总纲由四份可执行子 Spec 支撑：Migration、Observability、Context Budget、Sandbox Release。每份子 Spec 必须独立完成合同、失败语义、测试和发布切片，不能等到最终阶段统一补迁移。

## 2. 不变量

1. Canonical Messages 是唯一会话 transcript。
2. 一次 Model Attempt 只产生和执行一个 Action。
3. 未知工具或无效工具定义 fail closed。
4. approval、sandbox、effect observation、recovery 是四层不同能力，不能互相替代。
5. 最新用户输入不得被静默截断；required-only 超限时 Provider 不得被调用。
6. Shell 无论成功、失败、超时、被中断或部分执行，都进入 effect observation 和 terminalization。
7. secret redaction、私有文件、路径、trusted executable、Git hardening 和恢复记录的现有边界不得削弱。
8. Python core 保持标准库实现和零 runtime dependency。
9. Sandbox 显式开启后任何 bootstrap、policy 或 runner 错误都不得回退宿主执行。
10. 运行时只读取当前持久化合同；旧格式只由独立迁移器读取。
11. 行为模块生产事实；Trace、Report、Summary 和 Evaluation 只投影或聚合事实，不通过 free-text 重新推断。
12. 同一事实只计算一次，消费者不得各自重新分类。
13. 每次持久化合同变更必须同时交付 writer、current-only reader、converter、cutover/recovery、inspection consumer 和 evaluation scenarios。

## 3. 事实生产者与消费者

| 事实 | 唯一生产者 | 消费者 |
|---|---|---|
| request token allocation | Context request builder | Report、Trace projection、Summary、Evaluation |
| Memory selection | Retrieval selection | Context、Report、Trace projection、Evaluation |
| Tool policy | ToolExecutor policy phase | Tool Result、Tool Change、Trace、Summary |
| Approval outcome | Approval phase | Policy、Trace、Report |
| Sandbox outcome | SRT adapter | Tool Result、Tool Change、Trace、Summary |
| Workspace effects | WorkspaceObserver | Tool Change、Recovery、Summary |
| Verification evidence | verification recorder | Checkpoint、Report、Evaluation |
| Terminal run state | single run finalizer | TaskState、Trace、Report、Summary |

## 4. MIG-001：按合同切片的事务迁移

### 4.1 迁移原则

迁移不是第 12 周一次性处理全部 `.pico`，而是每个合同发布切片的一部分：

```text
新 schema + writer + current-only reader
+ source converter + transaction cutover/recovery
+ inspection/evaluation consumer
```

只有同时满足以下条件的工件才迁移：合同结构或语义改变；新 runtime 必须继续读取；且不能从其他可信工件安全重建。未改变的工件不转换、不重写、不升级版本。

### 4.2 逐工件矩阵

| 工件 | 当前合同 | 目标合同 | 本阶段处理 |
|---|---|---|---|
| Session | `session`, version 1 | 暂不改变 | 不迁移；strict reader 原样保留 |
| TaskState | 当前私有状态 | 暂不改变 | 不迁移；新增持久字段需另开 slice |
| Trace JSONL | 无统一 envelope | `trace_schema_version=1` | MIG-OBS；逐事件安全投影，无法判断则失败 |
| Report | 无明确 record header | `run_report`, version 2 | MIG-OBS；优先从可信事实重建 |
| Run Summary | 派生 inspection | `run_summary`, version 1 | 不落盘、不迁移 |
| Checkpoint | `checkpoint`, version 1 | 暂不改变 | 不迁移 |
| Tool Change | `tool_change`, version 1 | 仅在持久化 Policy/Sandbox 时 version 2 | 条件迁移；缺失事实写 unknown，不伪造 allow/deny |
| Recovery blobs | hash 引用 | 暂不改变 | 不迁移；校验 hash/权限/引用 |
| Working Memory | Session 内字段 | 暂不改变 | 不迁移 |
| Workspace User Notes | Markdown/frontmatter | 不改变 | 永不由私有迁移改写，只读验证 |
| Agent Notes | append-only Markdown | 不改变 | 排除，原样保留 |
| Evaluation artifacts | 各自当前合同 | 可再生 artifact | 不迁移 |
| locks/temp/cache | 临时状态 | 非合同 | 不迁移 |

### 4.3 迁移目录与 Journal

```text
.pico/
├── runs/                         # live
├── .migration/
│   ├── lock
│   ├── journal.json
│   ├── candidate/
│   └── rollback/
```

`.migration` 位于同一文件系统、owner-only、不进入 Git、不被普通 Store 扫描；所有路径是 `.pico` 内相对路径，拒绝 symlink。Journal 合同：

```json
{
  "record_type": "migration_journal",
  "format_version": 1,
  "migration_id": "mig_...",
  "contract": "run_artifacts",
  "source_version": 1,
  "target_version": 2,
  "state": "candidate_ready",
  "created_at": "...",
  "updated_at": "...",
  "workspace_identity": {
    "repo_commit": "99f9a8f...",
    "repo_dirty": true
  },
  "paths": {
    "live": "runs",
    "candidate": ".migration/candidate/runs",
    "rollback": ".migration/rollback/runs"
  },
  "source_identity": {"manifest_hash": "sha256:..."},
  "candidate_identity": {"manifest_hash": "sha256:..."},
  "error_code": ""
}
```

Journal 禁止 prompt、completion、Tool args/result、Memory 正文、secret 和 workspace 绝对路径。

### 4.4 迁移状态机

```text
ABSENT → PREPARING → CANDIDATE_READY
CANDIDATE_READY → OLD_MOVED → NEW_INSTALLED → VALIDATED → COMMITTED → ABSENT
NEW_INSTALLED → ROLLBACK_REQUIRED → ROLLED_BACK → ABSENT
ROLLBACK_REQUIRED → ROLLBACK_FAILED
```

磁盘不变量：

| 状态 | live | candidate | rollback | 普通 runtime |
|---|---|---|---|---|
| PREPARING | 旧 | 构建中 | 无 | 拒绝/等待 lock |
| CANDIDATE_READY | 旧 | 完整新 | 无 | 不越过迁移 |
| OLD_MOVED | 无 | 完整新 | 完整旧 | 拒绝启动 |
| NEW_INSTALLED | 完整新 | 无 | 完整旧 | 只允许验证 reader |
| VALIDATED | 完整新 | 无 | 完整旧 | 新状态权威，继续清理 |
| ROLLBACK_REQUIRED | 失败新 | 无 | 完整旧 | 拒绝启动 |
| ROLLED_BACK | 完整旧 | 可选失败副本 | 无 | 旧状态可读，报告迁移失败 |
| ROLLBACK_FAILED | 不确定 | 可能存在 | 可能存在 | 无条件拒绝 |
| COMMITTED | 完整新 | 无 | 无 | 正常启动 |

每次 rename 后先 fsync 被修改目录，再原子写 journal、fsync journal、fsync `.migration`。状态只描述已 durable 的磁盘事实。`VALIDATED` 状态下 live 已验证，rollback 清理可恢复；`ROLLBACK_FAILED` 或目录组合与 journal 不一致时不自动删除、不猜测权威目录。

### 4.5 启动恢复

访问迁移 Store 前运行 migration preflight：

- 无 journal/残留：正常启动；
- `PREPARING`：验证 live，清理安全 candidate；
- `CANDIDATE_READY`：继续 apply 或显式 abort；
- `OLD_MOVED`：继续 candidate→live；
- `NEW_INSTALLED`：current-only reader 验证，成功提交，失败回滚；
- `VALIDATED`：继续清理 rollback；
- `ROLLBACK_REQUIRED`：执行唯一可证明的反向切换；
- `ROLLED_BACK`：验证旧 live，清理失败 candidate，拒绝新合同 runtime；
- `ROLLBACK_FAILED`/identity mismatch：fail closed，仅允许 status/recover。

入口：

```bash
pico migrate status
pico migrate apply
pico migrate abort
pico migrate recover
```

### 4.6 发布规则与测试

不得先合并新 writer、最后再补 migration。每个合同 slice 必须覆盖：candidate 中断、两次 rename 前后崩溃、reader 验证失败、反向 rename 失败、journal 损坏/重复 key、identity mismatch、symlink、磁盘不足、lock contention、跨文件系统 staging、成功删除旧工件和未改变工件不被重写。

## 5. OBS-001：Trace、Report、Summary 合同

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

保持扁平结构，不建设 span tree。Trace 禁止用户 prompt、model completion、Tool args/result、Shell stdout/stderr、Memory query/snippet/body、文件正文、完整路径和 secret。`run_started` 不保存 user request，tool/verification 事件不保存 args/result/完整 command。

### 5.2 `run_report` v2

固定 header：

```json
{"record_type":"run_report","format_version":2}
```

必填：`run_id`、`task_id`、`status`、`stop_reason`、`duration_ms`、`model`、`context`、`tools`、`memory`、`sandbox`、`effects`、`recovery`、`integrity`、`finalization`。

`status` 终态为 `completed|stopped|failed`；`running` 只允许 TaskState。`stop_reason` 复用现有枚举并增加 `context_budget_exceeded|sandbox_bootstrap_failed|migration_required`。

Report 禁止 final answer、完整 TaskState、Working Memory 正文、prompt、completion、Tool args/result、Shell output、Memory query/body、secret 和完整路径。Report 是终态聚合真源；TaskState/Session 保留私有内容。

最小示例：

```json
{
  "record_type":"run_report",
  "format_version":2,
  "run_id":"run_ok",
  "task_id":"task_ok",
  "status":"completed",
  "stop_reason":"final_answer_returned",
  "duration_ms":1200,
  "model":{"attempts":2,"turns":2,"failures":0,"retries":0,"transport_attempts":2,"transport_retries":0,"transport_evidence_complete":true,"usage":{"input_tokens":12000,"output_tokens":600,"total_tokens":12600,"cached_tokens":0,"cache_hit":false}},
  "context":{"request_count":2,"budget_failure_count":0,"last_breakdown":{"schema_version":1,"count_mode":"estimate","within_budget":true,"final_input_tokens":6000,"headroom_tokens":89384}},
  "tools":{"calls":1,"allowed":1,"denied":0,"runner_executed":1,"reason_code_counts":{"allowed":1},"status_counts":{"ok":1}},
  "memory":{"candidate_count":0,"selected_count":0,"included_count":0,"dropped_budget_count":0,"filter_counts":{}},
  "sandbox":{"requested":false,"active_calls":0,"outcome_counts":{},"host_fallback_count":0},
  "effects":{"changed_file_count":0,"change_kind_counts":{},"partial_success_count":0,"recovery_review_required":false},
  "recovery":{"checkpoint_count":0,"recovery_checkpoint_count":0,"verification_count":0,"pending_count":0},
  "integrity":{"trace_status":"ok","trace_schema_version":1,"terminal_event_count":1,"correlation_complete":true,"summary_complete":true},
  "finalization":{"errors":[],"observability_degraded":false}
}
```

中断场景必须表达 `status=stopped`、`stop_reason=interrupted`、Tool status `interrupted`、Sandbox `timeout`（若适用）、partial effects、`recovery_review_required=true`，且 pending Tool Change 已 terminalize。

Trace 损坏但 Report 可读时：Report 终态仍可用；`integrity.trace_status=corrupt`、`terminal_event_count=0`、`correlation_complete=false`、`summary_complete=false`、固定 `error_code=trace_invalid_json`；Summary 不从坏 Trace 猜测状态、不重写原 Trace。

### 5.3 `run_summary` v1

Summary 是 inspection-time 派生 payload，不落盘：

```json
{
  "record_type":"run_summary",
  "format_version":1,
  "run":{"run_id":"run_ok","task_id":"task_ok","status":"completed","stop_reason":"final_answer_returned","duration_ms":1200},
  "model":{"attempts":2,"failures":0,"input_tokens":12000,"output_tokens":600},
  "context":{"request_count":2,"budget_failure_count":0,"within_budget":true},
  "tools":{"calls":1,"allowed":1,"denied":0,"status_counts":{"ok":1},"reason_code_counts":{"allowed":1}},
  "memory":{"candidate_count":0,"selected_count":0,"included_count":0,"dropped_budget_count":0},
  "sandbox":{"requested":false,"active_calls":0,"outcome_counts":{},"host_fallback_count":0},
  "effects":{"changed_file_count":0,"partial_success_count":0,"recovery_review_required":false},
  "integrity":{"report_status":"ok","trace_status":"ok","terminal_event_count":1,"correlation_complete":true,"summary_complete":true}
}
```

Summary 必须验证 report/run_id/task_id/status/stop_reason 一致；Trace 的 run/task correlation 一致；Report 与 TaskState 冲突时标记 `report_task_state_mismatch`，不选择“看起来更新”的一个作为真源。text 和 JSON 来自同一 payload。

发布切片：`OBS-BASE`（Envelope/projection/validator）、`REPORT-V2`（header/aggregate/rebuild）、`SUMMARY-V1`（Report 主导 + Trace integrity）、`MIG-OBS`（仅 Trace/Report）。

## 6. CTX-001/002：Context Snapshot 与全 request budget

### 6.1 Snapshot

不建设通用 Context framework。当前 renderer 拆为 source selection/rendering 与最终 request budget decision。内部结构：

```python
InjectionSource(name, required, text, token_count, status, reason_code, selected_memory_paths=())
InjectionSnapshot(current_user, runtime_feedback, intent_name, sources)
```

Snapshot 创建后不可修改；同一 Attempt 不重复 recall、workspace scan、checkpoint render 或 intent。`selected_memory_paths` 只保留内存，不进入 telemetry。

### 6.2 Source 分类与固定 drop 顺序

Required：`system`、`tools`、`current_user`、非空 `runtime_feedback`，以及依恢复正确性判定为 required 的 checkpoint。Optional：完整 `recent_history`、`recalled_memory`、`workspace_state`、`project_structure`、`memory_index`、optional checkpoint。

固定删除顺序：

```text
1. history soft cap：最老完整 turn
2. memory_index
3. project_structure
4. workspace_state
5. recalled_memory
6. optional_checkpoint
7. 继续删除最老完整 history turn
8. 仍超限 → context_budget_exceeded
```

Tool-use 与紧随其后的 tool-result 不可拆。`history_floor_messages` 是软偏好，不能制造真实超限。required checkpoint 在生成时 bounded；request builder 不截断，required-only 超限直接失败。

### 6.3 最终预算算法

```text
冻结 snapshot
→ 构建 candidate request
→ sanitize provider payload
→ 锁定 count mode
→ required-only feasibility
→ history soft-cap
→ optional source drop
→ 继续 drop history
→ 最终 sanitize + 同 mode 重计数 + assert
→ 生成 Breakdown
→ 仅在 recalled_memory 最终 included 时提交 recently_recalled
```

`input_limit = total_budget_hard_cap - max_new_tokens - 512`。Mode 为 `provider_request|provider_text|estimate`；任一 Provider component 失败则整次从头 estimate，禁止混用；默认不增加远程 count_tokens 请求。Provider 调用只发生在最终断言成功后。

Breakdown 必须记录 schema、mode、hard cap、reserved output、margin、input limit、final input、headroom、required feasibility、source status/reason、soft/hard dropped turns、digest count，不包含正文/query/args/result/secret/完整路径。

### 6.4 Recall 提交

`recall_for_turn()` 不再直接写 `session["recently_recalled"]`，而返回 `RecallSelection`；ContextManager 只在最终 source included 且 request build 成功后提交 canonical selected paths。被 `dropped_budget` 的 note 不更新 recently-recalled。

### 6.5 Context scenarios

`context.required-current-user-overflow`、`required-checkpoint-preserved`、`optional-source-drop-order`、`history-tool-pair-not-split`、`provider-counter-whole-request-fallback`、`recalled-memory-dropped-not-marked-recent`、`final-request-within-hard-cap`、`provider-not-called-on-budget-error`、`runtime-feedback-preserved`、`empty-checkpoint-not-emitted`。

## 7. TOOL-001 与 Memory 边界

固定 registry 是 schema、description、effect class 唯一真源；`risky` 不等于动态 risk class。Policy 在 validation、allowlist/read-only/repeated-call、approval revalidation、trusted executable/Git preflight、sandbox preflight 全部通过后才冻结 `allow`。Policy、Sandbox、Tool status、Recovery 分层；deny runner=0，allow runner=1；policy 写入 pending Tool Change，但不因 exit/effect 重写。

Durable Memory 只有 User Notes 和 Agent Notes。User Notes 继续 `.pico/memory/notes/**/*.md`，只读、可 Git 跟踪；Agent Notes append-only、私有。内建 write/patch 始终拒绝 User Notes；Sandbox 开启时 Shell 由 OS denyWrite 保护，关闭时不宣称强制保护。

`memory_save` 由当前 top-level 用户输入确定性识别 `memory_write_intent`；模型不能设置，历史不继承，delegate 默认关闭。无明确保存意图：`memory_save → deny → explicit_memory_request_required → runner=0`。

## 8. SBOX-000/001/002/003/004：Sandbox 发布合同

### 8.1 发布策略

**Release A：macOS Sandbox GA**。包含固定 SRT、bootstrap、operator identity、默认断网、敏感路径保护、普通 argv/complex shell/hardened Git、timeout/process group、effect/recovery、fail-closed、doctor 和 macOS real smoke。Release A 不宣称 Linux Sandbox 支持。

**Release B：Linux Sandbox GA**。独立通过 GitHub-hosted runner 的 SRT/Node/bwrap/user namespace/seccomp/socket/filesystem/network/process-tree/Git smoke 和稳定性门禁。Linux 失败不阻塞 Release A；Linux 上 `--sandbox` 必须 unsupported/not_ready 并 fail closed，不得回退宿主。

### 8.2 平台状态

`supported|unsupported|not_ready|incompatible|unavailable|error`。只有 `supported` 允许 `--sandbox`。Doctor 无论是否启用 Sandbox 都报告能力，但不把依赖存在等同于真实隔离成功。

### 8.3 Identity 与启动

SRT 精确锁定 `0.0.65`、Node `>=20.11.0`；不自动安装/更新。冻结 launcher、manifest、package entry、Node identity，每次调用复验；不进入普通 trusted executable registry。Bootstrap 在 Agent/run 创建前失败则不创建 Provider request/run/session mutation，不回退 host。

### 8.4 执行与 Git

普通 argv、complex shell、hardened Git 汇入窄的 Approved Shell Execution；默认 host runner 保留，SRT adapter 不接受 fallback callable。Complex shell 使用显式 `[shell,"-c",exact_command]`，不再使用额外 `shell=True`。

纯文件系统 Git 检查可宿主执行；`rev-parse`、`config --includes`、`ls-files` 和最终 target Git 必须 SRT。首版允许多个 invocation，共享 policy/temp/hash。`.git`、`.pico`、User Notes 默认 denyWrite；不支持需要修改 Git index/metadata 的工作流。

### 8.5 Policy、环境与 timeout

默认无外网、localhost、listener、Unix socket；必须真实分别验证 TCP/DNS/IPv6/socket/继承 fd。默认 denylist 阻断 `.env`、`.pico`、SSH/AWS/GCP/Kube/Docker credentials、netrc/npmrc/pypirc/history；不声称完整 HOME 隔离。每次 call 使用 owner-only temp HOME/TMPDIR/cache/settings 0600；Provider key/Project Environment 仍由 Pico env allowlist 排除。

`Popen(start_new_session=True) → communicate(timeout) → TERM group → grace → KILL group → wait → residue check → effect observation → terminalize`。Bootstrap outcome：`ready|unavailable|version_mismatch|identity_changed|platform_unsupported|platform_not_ready|policy_invalid|bootstrap_failed`。Call outcome：`completed|policy_denied|timeout|wrapper_failed|target_not_started|cleanup_failed`。target 非零 exit 为 sandbox completed、tool error/partial_success。

### 8.6 Sandbox gates

Release A macOS 必须通过 identity、argv/shell、filesystem、sensitive read、`.git/.pico/User Notes` write、network/socket、Git probe/target、timeout、partial effect、zero fallback、doctor 和 real smoke。Release B Linux 独立通过固定 CI 安装、bwrap、namespace、seccomp/socket、filesystem/network/process-tree/Git、zero fallback 和多次稳定运行。

PR 运行 `sandbox-contract`；macOS focused job 是 Release A mandatory real smoke；Release A 阶段 Linux job 可见但不阻塞、不标记 supported、不静默 skip；Release B 前升级为 mandatory。

## 9. EVAL-001

不新建 benchmark framework。`evaluate.py` 只编排现有 runner，读取结构化 artifact，不复制评分逻辑、不解析自由文本作为主要真源、不自动调用真实 Provider。Suite：`core-fast`、`core-full`、`sandbox-contract`、`sandbox-real`、`live`。

Artifact 必须记录 `record_type`、`format_version`、suite、baseline、scenario IDs、status、相对 artifact path、repo commit、dirty、Python、platform、machine class。功能 100% 通过；性能只比较同场景同 machine class 的 median，超过 baseline 2 倍且绝对增加 5ms 才失败，p95 只报告，允许一次确认重跑。Canary 扫描 Trace/Report/Summary/Eval，禁止 prompt/completion/args/result/shell output/memory query/body/secret/绝对路径泄漏。

## 10. 发布顺序与路线图

### Release A 基准 10–12 周

1. MIG primitive、OBS/Report/Summary contracts、artifact provenance。
2. Context Snapshot、全 request budget、Breakdown。
3. Tool Policy、Memory explicit gate、recall commit。
4. SBOX-000 feasibility、Approved Execution、Git probe split。
5. macOS Sandbox GA、macOS real smoke、atomic migration slices。

### Release B 额外 2–4 周

1. Linux CI dependency/install contract。
2. bwrap/user namespace/seccomp/socket。
3. Linux filesystem/network/process-tree/Git smoke。
4. 多次稳定运行和 Linux GA gate。

每个合同 slice 完成 writer、current-only reader、converter、cutover/recovery、inspection/evaluation 后才可发布。若延期，先削减 Summary 文本、非关键 doctor、高级 recall 展示和性能阻断；不能削减 fail-closed、effect observation、recovery、latest input、runner 0/1、迁移失败保留旧状态和 leakage gate。

## 11. Go/No-Go

Migration：状态机、journal、每次 rename fsync、启动恢复、reader 验证、失败反向切换、无混合状态和旧工件清理通过才 Go。

Observability：Report/Summary strict schema、成功/中断/Trace 损坏样例、Trace projection 和 leakage gate 通过才 Go。

Context：required overflow Provider=0、单一 mode、latest input 不截断、tool pair 不拆、source/drop deterministic、recently-recalled 正确提交才 Go。

Tool/Memory：deny/allow runner 0/1、policy/effect/recovery 一致、无明确请求不写 Memory、User Notes 只读/Git boundary 通过才 Go。

Sandbox Release A：macOS mandatory 全部通过；Release B：Linux mandatory 全部通过。Linux 不作为 Release A 的隐含门槛。

## 12. 不做与价值判断

本阶段不做 Plugins、MCP、RPC、daemon、OTel、Dashboard、Vector DB、Embedding、自动长期记忆、通用并行、Sandbox backend registry、多后端、项目网络白名单、Session tree/fork、Sandbox 默认开启、远程 token count、完整 Git 写工作流和高级 score 校准。

P0：全 request budget、Context Breakdown、Policy Decision、Trace 内容收缩、SRT fail-closed、Evaluation gate、按 slice 事务迁移、explicit memory gate。

P1：Recall telemetry、Memory doctor、Summary text、Linux real smoke、性能 gate、User Notes Git 共享。

最终判断：架构可行性高、项目匹配度高、安全收益高；Release A 单人 2–3 个月按基准可行，保守情形不可保证；Linux 应作为独立 Release B。任何平台或工期问题都通过发布范围收缩处理，不通过降低 fail-closed、effect observation、recovery 或内容安全不变量处理。
