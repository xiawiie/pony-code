# Context、Session 与长会话

Pony 的长会话模型由四层组成：模型能力与 token 账户、动态 Context Sources、append-only Session Tree、
以及只改变 active view 的 compaction。Canonical history 永远保留在磁盘；模型只看到当前分支的
summary + recent tail。

## 1. ModelCapabilities 与总预算

所有 Provider 共用同一个能力合同：

```python
ModelCapabilities(
    context_window=...,
    max_output_tokens=...,
    token_counter_mode=...,
    source="cli|config|builtin|fallback",
)
```

能力按以下优先级解析：

1. CLI `--context-window`、`--max-output-tokens`；
2. `pony.toml` 的 `[model]`；
3. Pony 内置默认模型记录；
4. 未知模型回退到 128,000 context / 16,384 output，并输出显著告警。

统一公式是：

```text
W = context_window
O = min(configured output, model max output)
R = max(compaction reserve, O)
I = W - R
```

默认值：

| 项目 | tokens |
| --- | ---: |
| Context Window `W` | 128,000 |
| 输出上限 `O` | 16,384 |
| Compaction reserve `R` | 16,384 |
| 输入上限 `I` | 111,616 |
| System + tools hard cap | 24,576 |
| Context Sources pool | 16,384 |
| Recent tail | 20,000 |
| Compaction summary hard cap | 13,107，即 `floor(0.8 × R)` |
| Split-turn summary hard cap | 8,192，即 `floor(0.5 × R)` |
| Branch summary hard cap | 2,048 |
| Inline tool result | 4,096 |
| Tool digest | 512 |

`W - R` 至少要留下 16,384 个输入 token，否则配置被拒绝。提高输出上限会同步提高 reserve，避免请求
声明大输出却没有为它留出窗口。小模型的 pinned cap 缩放到 `min(24,576, floor(W × 0.20))`，source pool
缩放到 `min(16,384, floor(W × 0.125))`。

模型请求、summary、Memory recall 和 tool digest 全部使用 token；文件和 Session 限制使用 bytes。字符数只
用于 CLI 展示。没有真实 Provider usage 时，估算器按 CJK code point 约 1 token、其他文本约 4 字符/token，
再计入 JSON/message/tool schema 结构成本，并对完整请求增加 5% 余量。真实 usage 成功返回后，后续请求优先
使用 usage anchor 加新增尾部估算。

## 2. Pinned Context 与动态来源

核心 system instructions、当前路径适用的项目指令和 tool schemas 共用 24,576-token hard cap。超过总 cap
直接抛出 `SystemContextTooLarge`；安全指令或工具 schema 不会被静默截断。README、repo map 和普通项目元数据
属于动态来源，不永久占用 system prefix。

每个 top-level turn 先产生不可变的 `ContextChunk` 候选，再由一个 allocator 按 required、P0、P1、P2 顺序
放入共享池。分配只接受完整 chunk，不从字符中间切断 XML、列表或结构化事实。

| Source | hard cap | 典型内容 |
| --- | ---: | --- |
| `workspace_state` | 3,072 | branch、dirty state、近期 Git 事实 |
| `project_structure` | 6,144 | repo map、语言、模块与入口 |
| `task_working_set` | 3,072 | task checkpoint、文件、阻塞、下一步 |
| `recalled_memory` | 6,144 | 当前 query 相关 passages |
| `memory_index` | 1,024 | 可用 Memory 文件及短描述 |

未使用的 source tokens 直接归还 history。实际 history 预算是：

```text
history_budget = I
  - actual(system + tools)
  - actual(current user + runtime feedback)
  - actual(selected sources)
```

Pony 不再使用关键词 intent profiles、静态 100k 总预算、40k history soft cap、固定 drop order 或
`injection_budget_ratio`。历史过长只能通过 compaction 退出 active request，不能从磁盘静默删除。

每次请求 telemetry 都记录 model limits、output/reserve/input limit、pinned/source/history 实际用量、token
count mode、summary/tail、compaction 原因和 compression ratio。`dropped_turns` 在正常合同中恒为 0。

## 3. Append-only JSONL Session Tree

Session 文件位于 `.pony/sessions/<session-id>.jsonl`。第一行是 `session_header`，后续每行是一个
`session_entry`：

```text
SessionHeader
└─ SessionEntry(id, parent_id, timestamp)
   ├─ message
   ├─ tool_exchange
   ├─ permission_mode_change
   ├─ plan_artifact
   ├─ compaction
   ├─ branch_summary
   ├─ task_checkpoint
   ├─ label
   ├─ rewind
   ├─ context_recovery
   └─ session_info
```

`parent_id` 形成树，文件中最后追加的 entry 是当前 leaf。正常 context 只遍历 leaf 到 root 的 active path。
rewind/fork 从目标 entry 追加一个新分支，旧分支和其中的工具证据仍留在 JSONL。

工具调用和对应结果存入同一个 `tool_exchange` entry；崩溃不能留下“已提交 tool call、未提交 result”的合法
Session 状态。运行时 message commit 只验证并追加本轮 message batch 和小型 state delta，不深拷贝或重写完整
transcript。

Session v5 active projection 另外包含 `permission_mode`、`permission_rules`、`plan_text`、`plan_revision` 与
`pre_plan_mode`。runtime 创建的新 Session 默认 `auto`；公开的 `manual` 只在 CLI 边界映射为内部 `default`。
Mode 变更使用显式 `permission_mode_change`，进入 `plan` 时同时冻结此前 mode；Plan 使用最多 12 KiB 的
`plan_artifact` 完整替换文本并递增 revision。`permission_rules` 只保存合法 Tool 名的 allow/ask/deny 集合。
fork/rewind/reset/clone 都从目标 active projection 恢复或复制这些状态，不创建第二份 Plan writer。

持久化约束：

- append 在 Session lock 下执行并 `fsync` 文件和目录；
- 单行 hard cap 8 MiB；
- Session 在 128 MiB 发出 soft warning，512 MiB hard fail；
- 私有目录/文件分别为 owner-only 0700/0600，并拒绝 symlink、hardlink、identity swap；
- v5 尾部不完整 JSONL 返回 `SessionTailRepairRequired`；必须显式执行 `pony session tail-repair <id> --yes`，
  reader 不会静默修证据。v1-v4 的 tail repair 与其他 writer 一样返回 `session_migration_required`。

Session Header 绑定 exact lexical root、Git common-dir、Git-dir 以及 root device/inode。HEAD 和 branch 可以正常
变化，但 sibling worktree 不能直接 resume。`clone --to-worktree` 创建新 Session，复制 active conversation branch、
permission mode/rules、Plan artifact、当前 summaries 和去敏后的任务目标 checkpoint，并绑定目标 worktree 的新 identity。

## 4. Legacy 显式迁移

`sessions list/show` 与 `session inspect/tree` 能只读识别 v1 `.json` 和 v2-v4 `.jsonl`，但不会迁移、chmod 或创建
artifact。只有 CLI `--resume` 与 `Pony.from_session()` 使用显式迁移入口：

1. 锁住 Session root，严格验证旧格式；
2. 把原文件写入私有 `legacy-backups/`；
3. v1 将 message 链转成 v5 entry 链并提升 embedded task state；v2-v4 在保持 ID、parent、顺序、timestamp 与 active
   leaf 的前提下转换当前格式能表达的 entry；
4. v1/v2 迁移为内部 `default`；v3 的 `act` 映射为 `default`，`plan/review` 映射为 `plan`。旧 Active Plan 只保留在
   migration evidence 中，不成为新的 Plan artifact；含 `model_change` 的 v2 artifact 返回 `unsupported_legacy_entry`；
5. 写 `.jsonl.candidate`，`fsync` 后重新完整解析验证；
6. 原子发布 JSONL 并删除活动 legacy 文件。

source/candidate identity 与 exact bytes 在发布前复验；已有 backup 也必须是安全 single-link 文件且逐字节匹配。
任何一步失败都保留原文件；重复 resume 可幂等重试。迁移完成后 runtime 只写 v5 JSONL，不长期保留双 writer。

## 5. Compaction

当 assembled request 超过 `W - R`，或用户显式执行 `/compact`，Pony 从 active path 尾部向前累计并保留约
`keep_recent_tokens`。cut point 只落在 entry/turn 边界，永远不会拆开 `tool_exchange`。

普通压缩生成一个 structured history summary。若单个 turn 本身超过 recent-tail 目标，则该 turn 的前缀使用
独立 split-turn summary，尾部继续逐字保留。summary 的 section 数值是 prompt 的软目标；真正 hard cap 只有
总 tokens：

| Compaction section | 软目标 |
| --- | ---: |
| Goal | 1,024 |
| Constraints & Preferences | 1,024 |
| Progress | 3,072 |
| Key Decisions | 2,048 |
| Next Steps | 1,024 |
| Critical Context | 3,072 |
| Files & Errors | 1,536 |
| 格式开销 | 307 |

Split-turn 的 8,192 tokens 分配给 current goal、actions/tool results、decisions、live workspace、next action、
files/errors；branch summary 的 2,048 tokens 分配给 abandoned approach、discoveries/decisions、file operations
和 carry-forward facts。

成功生成 summary 后才追加 `compaction` entry，记录 `first_kept_entry_id`、tokens before/after、tail、读写文件、
原因和 Provider usage。Summary 调用失败不会追加 entry，也不会删除历史。Context reconstruction 找到 active path
上最新 compaction 后，只发送 summary、可选 split summary、recent tail 和其后的新 entries。

Provider 返回明确 context-length error 时，AgentLoop 允许一次 forced compaction + retry，并追加
`context_recovery` 审计 entry。若压缩后仍超限，最多再压缩一次并返回明确错误，不循环猜测。

## 6. Task Checkpoint 与 Session Rewind

每个 top-level turn 结束时追加一个 `task_checkpoint` entry，包含：goal/status、completed/in-progress/blocker、
next steps、key/read/modified files、worktree digest 和本次 context usage。旧格式中的 `workspace_checkpoint_id` 只作为
legacy inspection 字段读取，active runtime 不写 Workspace Recovery 绑定。Working Set、Working Memory 与 file summaries
从 active branch 最新 checkpoint 派生；它们不是另一份可变 canonical history。

`/rewind <entry-id>` 只从目标 entry 创建新的 Session branch，不改文件。`--summary[=focus]` 可在分支点生成 bounded
summary；summary 调用失败不追加 rewind。`--workspace` 与 `--yes` 已删除，Git/外部备份负责 workspace 恢复。

rewind、fork、label、compact 和 checkpoint 都写入同一 append-only Session Tree，并由 Session lock/CAS 保护。旧
Sandbox-bound Session 在 CLI resume 装配阶段稳定拒绝，不能通过 rewind 或 fork 绕过到 Host。

## 7. CLI 与配置

交互命令：

```text
/permissions
/allowed-tools
/plan [open|share|description]
/compact [focus]
/tree
/checkpoint [label]
/fork <entry-id>
/rewind <entry-id> [--summary[=focus]]
/clone --to-worktree PATH
/remember <text>
```

非交互等价入口使用 `pony session inspect|tree|compact|checkpoint|fork|rewind|label|clone|tail-repair`。
`pony run` 与 `pony repl` 可使用一次性
`--permission-mode manual|auto|acceptEdits|bypassPermissions|dontAsk|plan`；它追加 Session control entry，不进入 `.env`、
`pony.toml` 或 `RuntimeOptions`。`--allowed-tools` 与 `--disallowed-tools` 和 slash picker 共用
锁内原子的 `SessionStore.set_permission_rules()`；一次提交只追加一个 rule state entry。picker 的 mode 操作写
`permission_mode_change`。
`--allow-dangerously-skip-permissions` 仅授予本进程选择或 resume bypass 的 capability，自己不切 mode；
`--dangerously-skip-permissions` 直接选择 bypass。普通 bypass resume 必须重新授权，显式切换到其他 mode 不需要。
只有这个非持久化 capability 进入当前 RuntimeOptions；permission mode 仍只属于 Session。
显式交互 resume 在首个 prompt 前显示一次 permission/checkpoint/resume/model 来源投影；one-shot、JSON inspection
与管理命令不显示。交互 history 每次从 active Canonical Messages 重建，只保留最多 100 条 top-level user 文本
（64 KiB 总量、16 KiB 单条），不会保留 slash 命令或 abandoned branch 输入。

`/plan open|share` 会先进入 Plan；空 artifact 只启用 mode，已有 artifact 才打开 editor 或尝试 share。

推荐配置：

```toml
[model]
context_window = 128000
output_limit = 16384

[context]
system_tools_hard_cap = 24576
source_pool_tokens = 16384

[context.compaction]
enabled = true
reserve_tokens = 16384
keep_recent_tokens = 20000

[context.tool_results]
inline_tokens = 4096
digest_tokens = 512

[memory.recall]
top_k = 6
min_score = 0.3
max_tokens_per_note = 1024
skip_recent_turns = 2
```

`total_budget_hard_cap` 在缺少 `[model]` 时迁移为 `model.context_window`；`history_soft_cap`、
`history_floor_messages` 与 `injection_budget_ratio` 已移除并告警。
