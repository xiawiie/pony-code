# Pico 恢复模型

Pico 的 recoverable editing 让 agent 产生的变更可检查、可归因和可选择恢复；它不替代 Git，也不承诺
整机、环境变量或完整对话回滚。

## Records

**Tool Change Record** 在可能修改其绑定 target 的工具运行前写入 pending，执行后根据真实 effect 终结为
finalized、error、partial_success 或 interrupted。记录包含 affected paths、file entries、approval、shell
side effects、verification 和 trace references。

**Checkpoint Record** 表示 turn、restore 或 manual recovery point，关联 tool changes、可恢复 file-state
blobs、Git review context 与 integrity/review 状态。AgentLoop 还在 Session 内保存 task checkpoint，供 resume
freshness 使用；它不是独立 Recovery Record。

ADR-0040 接受的 Sandbox 目标架构固定三个互不兼容的 Recovery domain：

| domain | private store | exact target |
| --- | --- | --- |
| Host Recovery | Project State Root 原 Checkpoint store | Source Root |
| Staging Recovery | `Sandbox State Root/recovery` | Execution Root |
| Source Apply | `Sandbox State Root/sandbox_apply` | Source Root |

每个 record/blob 都绑定 domain、store identity、target identity 和相关 baseline/final digest。三类 store 不得互读
record 或 blob，Staging Recovery 不能冒充 Source Apply rollback，Host Recovery 也不能解释 Sandbox manifest。
v0.2.0 只在 macOS arm64 的 sealed local authorization 和 exact image 验证后使用后两类；distributed Product
Enablement 尚未签发。

## 当前格式

三个可独立读取的 family 只接受当前合同：

| family | record type | format version |
| --- | --- | ---: |
| Session Tree header/entry | `session_header` / `session_entry` | 2 |
| Checkpoint Record | `checkpoint` | 1 |
| Tool Change Record | `tool_change` | 2 |

type/version 必须精确、version 必须是整数，required fields 必须完整。reader 拒绝错误类型、未知版本、
duplicate keys、缺失字段和不安全文件。Session inspection 不做转换；显式首次 resume 旧 `.json` 时，
`SessionStore` 在锁下备份、写 candidate、完整复验并原子发布 JSONL。迁移失败保留旧文件并可幂等重试。
Checkpoint/Tool Change runtime 仍不做读时转换或磁盘改写。run/report/trace 是当前审计 artifact，embedded
task checkpoint、verification evidence 与 restore preview 不单独版本化。

旧 OBS 与 Tool Change 数据只由显式事务迁移读取：

```bash
pico migrate status
pico migrate apply
pico migrate recover
```

迁移在 same-filesystem candidate 中转换并验证完整 Checkpoint → Tool Change → Blob 引用图，再通过
live/rollback 原子 rename cutover。任何 active/ambiguous journal 都会阻止正常 runtime 启动；runtime 本身
不包含 legacy reader。

## Restore 流程

先检查 pending/review evidence：

```bash
pico checkpoints pending
pico checkpoints show <checkpoint-id>
```

restore preview 是非修改操作：它比较 store/target identity、当前文件状态、预期 agent-produced state、
blob integrity、baseline/final digest 和 root 边界，输出可恢复、跳过或冲突项。只有用户明确请求 `--apply`
才修改对应 target：

```bash
pico checkpoints resolve-pending <id>
pico checkpoints resolve-pending <id> --apply
```

restore 使用 snapshot，而不是盲目 reverse patch；当前内容与预期不符时进入 Recovery Review。selective
restore 只应用被选择且完全可恢复的 file entry。restore 后创建新的 Checkpoint Record，保留 provenance，
不重写旧记录。

## Session rewind 与 workspace restore

Session Tree rewind 和 Recovery restore 是两个显式层次：

- `/rewind <entry-id>` 只从目标 entry 创建新 Session branch，文件保持不变；
- `/rewind <checkpoint-id> --workspace` 只接受带 `workspace_checkpoint_id` 的 task checkpoint；
- paired rewind 固定执行 preview、展示 restore/skip/conflict、一次确认、restore、最后追加 rewind；
- restore 失败时旧 Session leaf 不变；mid-turn/tool entry 不能用于 workspace rewind；
- intent journal 绑定 old leaf、target、唯一 operation ID、restore plan digest 与 worktree identity；Recovery
  audit 同时保存 operation ID 与 digest，用于“文件已恢复但 Session append 崩溃”的精确 resume reconciliation。
  owner、parent、operation ID 或 digest 任一不匹配时都不得自动认领较新的 restore。

Host 模式恢复 Source Root；Sandbox 模式只恢复当前 active Execution staging。rewind 不触发 Source Apply，
也不撤销已完成的 Source Apply。Session Header 绑定 exact Git worktree；sibling worktree 必须用
`pico session clone ... --to-worktree` 创建清除旧 Recovery、workspace checkpoint 与文件状态，但保留 active
conversation、summary 和去敏任务目标的新 Session。详见
[Context、Session 与长会话](context-and-sessions.md)。

Sandbox Session 结束时先关闭 mutation并持久化 immutable final manifest、redacted diff与tree digest。后续tree
变化时禁止重新生成可apply的diff，只允许discard。Source Apply使用独立授权、source baseline CAS、durable
journal和rollback；它不复用Staging Recovery的before blob作为source事实。Apply journal分别绑定baseline
`before_identity`、已发布`after_identity`和当前`prepared_identity`。replacement与delete tombstone只存在于
Source Root本地`.pico/checkpoints`的owner-only private quarantine；Source用户目录中的名称不会被check后直接
unlink/rmdir。

`pico sandbox diff <sandbox-id>`只读取既有`0600` immutable artifact：reader拒绝错误mode，但不得通过`chmod`
修正它，成功调用前后inode、mode、size、mtime与ctime必须不变。`status/list/inspect/diff/prune --dry-run`也不执行
任何reconciliation。

Source Apply的持久顺序固定为：external control lock → source mutation lock → exact external authority reservation
→ source-before blobs与journal → source-local guard → Session `applying` → source mutation。authority是第一项durable
事实，绑定lexical/source identity、Sandbox/state-root identity、control-directory dev/inode、journal id与diff
digest，关闭“journal发布前没有外部阻断事实”的crash window。authority清理必须持有已验证control-directory fd，
重读并比较完整expected record后CAS unlink、fsync；字段、目录identity、guard或journal任一不匹配都保留review block。

同一`pending_review` Apply可以收养exact reservation或匹配的unclaimed journal。reservation-only重试若在任何source
write前发现source/staging已变化，只有在journal absent、guard absent且Session仍`not_started`时，才可full-record
CAS清reservation并终结为`apply_conflicted`；其他组合都不得猜测或自动删除。terminal cleanup中authority与guard
都已缺失才可视为安全幂等完成；只缺一个仍是invalid/review状态。

## Backups 与故障处理

历史的一次性格式硬切备份保存在 `~/.pico/backups/<repo-hash>/<timestamp>/`，目录与文件保持私有，
journal 记录 prepared、applying、verified。显式 `pico migrate` 只处理计划定义的 OBS/Tool Change 切片，
不是通用数据转换平台；这些备份不会自动删除，也不应在正常恢复中被手工复制回活动 store。

Sandbox cleanup只可回收已验证的workspace、content blobs和temp；terminal manifest、immutable diff、Source Apply
journal/outcome及摘要证据保留在稳定audit root。identity不明的container、目录或record不得自动认领或删除。
terminal delete完成后同路径出现的新文件属于外部新事实；cleanup只删除private journal-bound tombstone并保留新文件。
temp/tombstone/blob全部清理成功前durable source mutation guard不得释放。

如果发现 pending、interrupted、partial、invalid record 或 applying journal：

1. 停止对应 Source Root/Execution Root 的新 mutation；
2. 运行 `pico doctor` 与 `pico checkpoints pending`；
3. 查看 record、trace 和 workspace diff，不编辑 store JSON；
4. preview 后由用户选择 resolve/apply；
5. Sandbox active call按exact identity reconciliation；Source Apply使用
   `pico --cwd <lexical-source> sandbox reconcile --yes`，由external authority O(1)定位exact state/journal，不依赖
   strict Session inventory/find；source root replacement也只收敛到`review_required/apply_review_required`；
6. 若 private identity、blob integrity 或 target root 不一致，保留证据并用 Git/外部备份人工恢复。

安全文件与 exception-order 不变量见[安全](security.md)，验证命令见[验证](verification.md)。

Memory 写入和召回也可能在 Agent Notes、Tool Change、checkpoint、recovery 或其他私有审计 artifact 中留下原文或
副本。删除当前 note 不会自动遍历或清除历史恢复证据；敏感数据处置应先备份并逐个审查相关 record，而不是直接
编辑 store JSON。
