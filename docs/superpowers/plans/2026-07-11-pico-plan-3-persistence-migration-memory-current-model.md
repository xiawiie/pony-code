# Pico Plan 3：持久化格式、一次性迁移与 Memory 当前模型

> 权威设计：`docs/superpowers/specs/2026-07-11-pico-current-surface-hard-cut-design.md`。

## 目标与已验证基线

- 分支：`memory`。
- 基线 HEAD：`a44677ae90fb64ec783a317dfdd65df79754e4ac`。
- Plan 2 本地证据：`1987 passed, 6 skipped`、offline live harness `60 passed`、
  `uv build` 成功、clean wheel 只包含 `pico` console script。
- Plan 2 GitHub 证据：push run `29159785939` 与 PR run `29159786954` 均在同一
  HEAD 上通过 Ubuntu Python 3.11/3.12。
- Plan 1 私有 manifest：
  `/Users/wei/.pico/backups/0ebc76a4a401e8a9/preflight-20260711T143538Z/manifest.json`。
  预期仍为 46 项：8 transform（4 session、2 checkpoint、2 tool-change）与 38
  verify-only；Memory 目标为 0。
- 七个既有 untracked 路径属于用户，始终不得移动、删除、stage 或写入。
- 不运行真实 Provider、Provider benchmark 或 online doctor。

## 计划不变量

1. Production reader 不双栈：格式切换提交完成后只接受 current type/version。
2. 一次性 converter 只存在于隔离的临时 migration module；runtime、Store 与 CLI 不调用它。
3. 真实迁移只触碰 manifest 中 8 个 transform JSON；38 个 verify-only 保持 byte/hash 与
   identity/metadata 不变。
4. 迁移不创建 `.pico` 内新 lock。只使用 manifest 已记录的 checkpoint/session store lock；
   migration mutex 位于 repo 外 backup 根。
5. 持有 store lock 时不得调用会再次加锁的 public Store API。
6. Atomic replace 后 inode 会变化；成功验证使用新安全 identity + transformed hash，rollback
   只承诺原 bytes/hash/mode/mtime，不承诺恢复 inode。
7. Memory 不建立跨 query cache；每次 search 只加载一个临时 document snapshot。
8. 每个提交只 stage 本 Task allowlist；每个 Task 跑 focused pytest、touched Ruff 和
   `git diff --check`。

## Task 0：Preflight 与计划提交

**仓库改动：仅本计划文件。**

- 验证 HEAD、origin、Plan 2 两个 CI run 和七个 protected untracked。
- 使用 private reader 重验 Plan 1 manifest 的 repo hash、summary、path set、identity、mode、
  mtime、size、nlink 和 SHA-256；不打印 manifest 内容、path list 或 hash。
- Plan 2 的一次性 `.env` rename 若留下唯一的空 `project-env.lock`，在确认无 writer 后对该
  existing regular/single-link/private lock 做 non-blocking exclusive acquire，安全 unlink 并 fsync
  `.pico` parent，以恢复 Plan 1 的 46-file manifest；它不是数据、不是 transform/verify-only 目标，
  也不得把它加入或改写 manifest。除此之外的任何 path drift 一律停止。
- 用只输出 PID/计数的方式确认当前 repo 无 Pico writer；不得打印可能包含 secret 的进程 argv，
  不得自动 kill。
- 记录当前 4 session message counts、embedded checkpoint 总数（基线 30）、recovery/tool-change
  cross-reference counts，只输出汇总计数。

提交：`docs: plan current persistence and memory model`。

## Task 1：Strict current persistence 与临时事务面

提交：`feat(persistence): hard cut current record formats`。

**Allowlist：**

- `pico/session_store.py`
- `pico/runtime.py`
- `pico/checkpoint.py`
- `pico/checkpoint_store.py`
- `pico/recovery_models.py`
- `pico/recovery_checkpoint_writer.py`
- `pico/recovery_manager.py`
- `pico/verification.py`
- `pico/file_lock.py`
- `pico/cli_session.py`
- `pico/evaluation/fixed_benchmark.py`
- `pico/evaluation/experiments_recovery.py`
- `benchmarks/live_e2e/run_live_session.py`
- `pico/current_surface_migration.py`（临时）
- 新 `tests/test_current_surface_migration.py`（临时）
- 删除 `tests/test_session_store_migrator.py`（唯一安全/转换断言迁入 transaction tests）
- 删除 `tests/memory/test_migration.py`（旧 session 自动 normalization 合同）
- `tests/test_session_store.py`
- `tests/test_checkpoint.py`
- `tests/test_checkpoint_store.py`
- `tests/test_checkpoint_store_durability.py`
- `tests/test_checkpoint_store_invalid_records.py`
- `tests/test_checkpoint_store_security.py`
- `tests/test_recovery_models.py`
- `tests/test_recovery_checkpoint_writer.py`
- `tests/test_recovery_manager.py`
- `tests/test_recovery_journal.py`
- `tests/test_recovery_e2e.py`
- `tests/test_recovery_durability_e2e.py`
- `tests/test_verification_security.py`
- `tests/test_cli_session_inspect.py`
- `tests/test_runtime_report.py`
- `tests/test_pico.py`
- `tests/test_safety_invariants.py`
- `tests/test_cli_diagnostics.py`
- `tests/test_metrics.py`
- `tests/test_artifact_security.py`
- `tests/test_security_integration.py`
- `tests/test_a1_security_integration.py`
- `tests/test_fixed_benchmark.py`
- `benchmarks/live_e2e/tests/test_assertions.py`

### 1.1 Session current contract

只保留：

```python
SESSION_RECORD_TYPE = "session"
SESSION_FORMAT_VERSION = 1
```

required top-level：

```text
record_type, format_version, id, created_at, workspace_root, messages,
working_memory, memory, recently_recalled, checkpoints, resume_state,
recovery, runtime_identity
```

- `record_type` 精确等于 `session`；`type(format_version) is int` 且等于 1。
- `history`、`schema_version` 必须不存在。
- `messages` 通过 `validate_messages(..., require_meta=True)`；nested duplicate key 也拒绝。
- filename stem 必须与 `id` 精确相同；`id/created_at/workspace_root` 必须是 string。
- `working_memory/memory/checkpoints/resume_state/recovery/runtime_identity` 必须是 dict，
  `recently_recalled` 必须是 list；missing/wrong type 全部拒绝且不得改写。
- `SessionStore.load()` 只安全读取、校验并返回；不得备份、转换或改写。
- `save()` 也只接受 current object；固定 content-free 错误，不回显 JSON、message 或 secret。
- 新 session constructor 一次写全 required fields。
- `_ensure_session_shape` 只维护 constructor/current runtime 的内存不变量，不补旧磁盘对象。
- runtime 删除 `migrate_session_to_v3` 调用。

### 1.2 Embedded checkpoint 与 runtime identity

- 删除 embedded `CHECKPOINT_SCHEMA_VERSION`、`schema_version`、`phase1-v1` 和
  schema-mismatch 状态/case。
- Embedded checkpoint 继承 Session version；constructor 一次写全当前字段。
- 删除 `DEFAULT_FEATURE_FLAGS["prompt_cache"]`。
- Provider 的 `supports_prompt_cache`、telemetry、Anthropic/DeepSeek cache wire path 保留。
- 当前及迁移后的顶层/embedded `runtime_identity.feature_flags` 删除 `prompt_cache`；只持久化
  真正影响行为的 flags。

### 1.3 Recovery current contract

只保留：

```python
CHECKPOINT_RECORD_TYPE = "checkpoint"
CHECKPOINT_FORMAT_VERSION = 1
TOOL_CHANGE_RECORD_TYPE = "tool_change"
TOOL_CHANGE_FORMAT_VERSION = 1
```

- constructors 写 `record_type + format_version`，不写 `schema_version`。
- checkpoint/tool-change required fields 精确沿用当前完整 constructors。
- reader 拒绝 wrong/missing/bool/float/string version、wrong type、duplicate keys、missing required
  fields、unsafe path/id 与不合法 status。
- 删除 `_with_additive_defaults`；public load/write/update 不补字段。
- 旧 file entry 缺 mode/source history 时，transaction 转成明确 review-only current state：
  `snapshot_eligible=False`、reason 精确为 `mode_unknown`、未知 `before_mode/after_mode=None`；
  current validator 只在该三项组合同时成立时接受 `None`，其他 current entry 的 mode 必须是 int。
  不得猜测可恢复状态。
- production 删除 `legacy=True` 分支与调用；current validator 只接受明确 current shape。

### 1.4 Verification 与 restore plan

- verification evidence 删除独立 `schema_version` 和常量，但 required field/type validation 保留。
- restore plan 删除版本常量与字典字段；它只是内存 preview plain dict。

### 1.5 临时 transaction module

`pico.current_surface_migration` 不被 production import，提供：

```text
--check        只读预检，不创建 backup/journal
--apply        单进程完成 prepared → applying → strict verify → verified
--verify       对 verified 事务做幂等只读复核/reconcile
--rollback     只允许 prepared/applying，从私有 backup 整批恢复
--verify-original  用 manifest + 迁移专用旧格式纯 validator 复核 rollback
```

- manifest path 只从 `PICO_PLAN1_MANIFEST` 读取；不搜索 parent、其他 repo/worktree/home。
- `file_lock.locked_file` 最小增加 `require_existing=True`，避免缺失 store lock 被迁移创建；
  同时增加 migration-only `blocking=False`；迁移清理时若无生产调用者一并删除这些参数。
- 外部 mutex：`~/.pico/backups/<repo-hash>/migration.lock`，用 non-blocking exclusive acquire；
  lock busy 立即固定错误退出，不等待、不读取 journal。
- store lock 固定顺序：checkpoint `.checkpoint_store.lock` → session `.session_store.lock`。
- 获取 lock 前后重验 manifest；禁止 `.mutation.lock`（不在 46-file baseline）。
- 持锁阶段只使用纯 duplicate-safe decode/transform、private reader/atomic writer，以及
  `SessionStore`/`CheckpointStore` public wrappers 共用的 exact `_load_unlocked` + strict validator。
  authoritative verification 在同一 mutex/双锁临界区完成；释放 lock 后的 public load 只作补充
  smoke，不授权 journal 进入 verified。

### 1.6 Backup、journal、apply 与恢复

Backup root：

```text
~/.pico/backups/<repo-hash>/migration-<UTC timestamp>/
```

- 先在同一 repo-hash root 建不进入正式扫描集合的 `.migration-staging-<random>`；root/子目录
  `0700`，文件 `0600`，只备份 8 个原始 JSON 到 `original/<relative-path>`。
- backup 与 journal 均用 private atomic writer；fsync 文件及每层新目录。
- journal 不保存 JSON 字段值，只保存 status、repo/manifest/commit identity、lexical relative paths、
  original identity/mode/mtime/size/hash、backup relative path/hash、deterministic transformed hash、
  applied paths。
- status 只允许 `prepared → applying → verified`。
- 全部 backup 安全重读、fsync 后在 staging 内写 prepared journal；再把 staging directory 原子
  rename 为预定 `migration-<UTC timestamp>` 并 fsync repo-hash parent。只有 promotion 完成的目录
  才属于正式 transaction；首次 target replace 前再写 applying。
- 成功取得外部 mutex 后，安全扫描 repo-hash root 下 `migration-*` 目录并 duplicate-safe 读取
  journal，以 repo root/hash、manifest identity 和 migration commit 精确匹配：零个匹配才创建；
  一个匹配则 resume/reverify；多个匹配、损坏 journal 或任何 identity 不一致立即拒绝。不得仅按
  “最新时间戳”猜测，也不增加 repo 内 active pointer。
- 同时扫描 `.migration-staging-*`：valid prepared journal 可原子 promotion 后 resume；无 journal
  或 partial backup 只在 46 live files 仍与 original manifest 完全一致、目录 identity/private mode
  安全且内容只属于本次 owned staging allowlist 时清理并重建，否则拒绝。staging 永远不允许进入
  applying，因此其恢复不覆盖任何 live target。
- “valid staging/formal transaction”不仅要求 journal identity 匹配，还要求 8 个 backup 集合完整，
  每个 backup 的安全 identity、regular/single-link/private mode、size 与 hash 均与 journal 精确匹配。
  prepared 或 applying 的任何 resume 都必须在 promotion、补 metadata 或 replace 之前完成这组全量
  复验；任一项损坏/缺失只拒绝，绝不继续写 live target。
- 每次写前重新验证原 inode/nlink/mode/mtime/size/hash；symlink、hardlink、FIFO、directory、
  escape、manifest 外文件或 drift 均停止。
- private atomic replace 后通过 descriptor 恢复原 mode 与纳秒 mtime，再 fsync file/parent。
- 每项完成后验证新 inode 安全、transformed hash 与 metadata，再原子更新 applied paths。
- crash 恢复按 current hash 三态 reconcile：original=未应用、transformed=已应用、其他=外部修改。
  replace 后 journal 前 crash 也能识别；若 metadata 未恢复先补 metadata。
- `--apply` 在同一进程、同一外部 mutex 和双 store lock 内完成全部 strict reread、业务/hash
  verification并写 verified，完成前绝不返回。并发进程或试图创建新 journal 的第二事务必须
  拒绝；原进程已退出后再次 `--apply` 必须识别同一 manifest/commit 的既有 applying journal，
  reacquire mutex+双锁、三态 reconcile，并继续完成到 verified（或显式 rollback）。verified 后
  `--apply/--verify` 只复验并 no-op。
- 普通异常整批 rollback；SIGKILL/KeyboardInterrupt 保留 applying，下一次 reconcile。
- rollback 前先验证所有 backup hash，以及所有 current hash 只属于 original/transformed 集合；
  unknown hash/corrupt backup 时一个 target 都不覆盖。
- rollback 反向恢复，单项失败继续尝试其余项并保留 journal；最终复验 8+38。
- rollback 只用于 verified 之前的失败；verified 事务拒绝自动 rollback，避免 journal 对 live
  bytes 作虚假声明。若 verified 后确需撤销，必须先 revert Task 3 cleanup 以恢复受审计工具，
  再走单独批准的恢复流程，不属于本次正常路径。

### 1.7 纯变换与业务不变量

Session：

- 只接受 manifest 已验证的 v2/v3；existing Canonical Messages 深度相等，不从 history 重建。
- 保留 id/created_at/workspace_root/messages；删除 history/schema_version。
- 补 current required container 的安全空值。
- 删除 30 个 embedded checkpoint version 与所有 identity `prompt_cache`。

Checkpoint/tool-change：

- 写 current type/version，删除旧 schema。
- 只补安全空值：status/review/integrity、prepared entries、recovery context。
- verification 删除版本。
- 保留 IDs、timestamps、status、paths/list order、approval、effect、trace refs 与 cross refs。
- 缺少不可推测的 ID/timestamp/语义字段时停止。

38 verify-only：bytes/hash、size、mode、mtime、dev/inode/nlink 全部不变。

### 1.8 Fault matrix

临时 transaction tests 构造完整 46-file fixture，覆盖：

- manifest/path/type/link/inode/hash/mode/mtime/size drift 与 nested duplicate JSON；
- missing lock、lock reentry、applying 时并发/新事务拒绝、crash 后同 journal resume-to-verified、
  verified 后 no-op 与禁止创建 `.mutation.lock`；
- external mutex busy non-blocking refusal、零/一/多个 journal、journal identity mismatch/corruption；
- 每个 backup/fsync/prepared-journal/promotion 点 SIGKILL 后的 staging resume 或安全清理重试；
- 第 N 个 backup/fsync/replace/journal/metadata restore 失败；
- replace 后 journal 前 crash；strict reread或业务 invariant失败；verify-only drift；
- backup corruption、partial rollback、rollback 二次恢复、verified journal失败；
- staging prepared 与正式 applying 两种状态下的 backup corruption 后重试均在任何 live write 前拒绝；
- root/parent/target/temp 的 symlink/hardlink/FIFO/directory swap；
- canary 不出现在 exception/stdout/stderr；
- 8 transformed、38 byte-identical、messages parity、30 embedded version removal；
- verified 二次调用 no-op。

Focused：所有 session/checkpoint/recovery/verification/runtime-report/security tests，加
`tests/test_current_surface_migration.py`、`tests/test_file_lock.py`。

## Task 2：执行真实 8-JSON 事务（不产生仓库提交）

前提：Task 1 的工作树实现与 focused/fault tests 已通过。紧接 commit 前重新检查 writer/open
file 与 manifest；从该检查开始到 `--apply` 返回 verified 为止进入持续 maintenance window：
不得启动 Pico、不得运行其他测试/命令、不得释放给其他 writer。commit 后唯一正常命令就是
`--apply`。tracked clean，七个 protected untracked 原样存在。禁止 `set -x`，输出不得包含
path/hash/JSON/value。`--apply` 自身在获取锁后再次完整检查 manifest，因此窗口内任何新 session
或 writer drift 都会在 backup/apply 前失败。

```bash
export PICO_PLAN1_MANIFEST=/Users/wei/.pico/backups/0ebc76a4a401e8a9/preflight-20260711T143538Z/manifest.json
umask 077
uv run python -m pico.current_surface_migration --apply
# optional idempotent post-transaction recheck
uv run python -m pico.current_surface_migration --verify
```

`--check` 可在 Task 1 commit 前用于只读预检，但不属于 commit 后正常序列。验收只输出固定状态和
`transformed=8 verify_only=38` 汇总。`--apply` 必须在返回前：

- 在 migration mutex 与两把 store lock 仍持有时，用 public Store 共用的 exact unlocked
  loader/strict validator 重读 4+2+2；
- 从私有 backup 比较 4 session messages 深度相等、embedded checkpoint count/order、IDs、
  timestamps、workspace roots、cross refs 与 recovery semantics；
- 验证 38 verify-only 全部不变、8 target mode/mtime 不变且 hash 为预计算结果；
- 全部通过后才写 verified。

释放 locks 后再用 public locked Store API 做 supplemental smoke；它不改变 verified 决策。

失败时只在同一 migration commit 上执行：

```bash
uv run python -m pico.current_surface_migration --rollback
uv run python -m pico.current_surface_migration --verify-original
```

`--verify-original` 不调用 strict public Store，只使用 manifest hash/identity、duplicate-safe decode
和 migration-only v2/v3/recovery pure validator。rollback 完成后 strict production reader 与原字节
不兼容：必须在继续使用 Pico 前重新 apply 到 verified，或 revert Task 1 commit。不得自动 kill、
不得临时扩大 manifest、不得手工编辑单个 JSON。

## Task 3：删除一次性迁移面

提交：`refactor(persistence): remove one-time migration surface`。

- 删除 `pico/current_surface_migration.py` 和临时 transaction tests。
- 删除剩余 migration-only helper/tests，并确认 production converter、deprecated aliases、compat
  defaults 已在 Task 1 不存在。
- 若 `require_existing` 只服务迁移，从 `locked_file` 删除。
- 严格格式、安全与 current behavior tests 保留。
- 结构扫描确认 production/tests 不再含旧 Store converter、旧 schema constants、embedded version、
  restore/verification version、`legacy=True` 和 dead prompt_cache feature flag。

私有 verified journal/backup 保留在 repo 外，作为 rollback/evidence；不得提交。

## Task 4：Benchmark family 当前格式

提交：`refactor(benchmark): version current artifact families`。

不建立全局 benchmark version，不给只输出 stdout 的 perf JSON 加版本。每个独立 reader family
使用自己的 `record_type + format_version=1`，且 bool/float/string/missing/unknown 均拒绝：

| Family | record_type | constant |
| --- | --- | --- |
| fixed definition | `fixed_benchmark_definition` | `FIXED_BENCHMARK_DEFINITION_FORMAT_VERSION` |
| fixed result | `fixed_benchmark_result` | `FIXED_BENCHMARK_RESULT_FORMAT_VERSION` |
| context ablation | `context_ablation_result` | `CONTEXT_ABLATION_FORMAT_VERSION` |
| memory ablation | `memory_ablation_result` | `MEMORY_ABLATION_FORMAT_VERSION` |
| recovery ablation | `recovery_ablation_result` | `RECOVERY_ABLATION_FORMAT_VERSION` |
| provider experiments | `provider_experiment_result` | `PROVIDER_EXPERIMENT_FORMAT_VERSION` |
| memory-quality scenario | `memory_quality_scenario` | `MEMORY_QUALITY_SCENARIO_FORMAT_VERSION` |
| memory-quality result | `memory_quality_result` | `MEMORY_QUALITY_RESULT_FORMAT_VERSION` |
| live E2E report | `live_e2e_report` | `LIVE_E2E_REPORT_FORMAT_VERSION` |

**Allowlist：**

- `pico/evaluation/benchmark_schema.py`
- `pico/evaluation/fixed_benchmark.py`
- `pico/evaluation/metrics_common.py`
- `pico/evaluation/experiments_recovery.py`
- `pico/evaluation/metrics_reports.py`
- `pico/evaluation/provider_benchmark.py`
- `scripts/run_provider_experiments.py`
- `scripts/run_large_scale_experiments.py`
- `benchmarks/coding_tasks.json`
- `benchmarks/memory_quality/run_benchmark.py`
- `benchmarks/memory_quality/scenario_1_recall.jsonl`
- `benchmarks/memory_quality/scenario_2_search_cn.jsonl`
- `benchmarks/memory_quality/scenario_3_update.jsonl`
- `benchmarks/memory_quality/scenario_4_multi_note.jsonl`
- `benchmarks/memory_quality/scenario_5_no_noise.jsonl`
- `benchmarks/live_e2e/run_live_session.py`
- `tests/test_fixed_benchmark.py`
- `tests/test_metrics.py`
- `tests/test_memory_quality_benchmark.py`
- `benchmarks/live_e2e/tests/test_assertions.py`

- Reader 先校验 type/version，再解析业务字段；duplicate-sensitive JSON 继续拒绝。
- 历史 `benchmarks/results/**` 与 live results 不转换，Plan 5 整体删除。

## Task 5：Memory 单一写入模型与 facade 删除

提交：`refactor(memory): hard cut single note model`。

最终只有：

```text
<scope>/notes/**/*.md      # User Notes，agent 只读
<scope>/agent_notes.md     # Agent Notes，append-only
```

- `memory_save` schema 精确为 `note`、`scope`；删除并明确拒绝额外 `topic/type`。
- 删除 `write_agent_topic`、topic slug/frontmatter creation、`agent/**/*.md` scan/write、
  `agent/legacy-import.md`/`.legacy` compatibility 和 per-topic tests。
- 顶层 obsolete `agent/` 完全忽略且不读取 canary；`notes/agent/*.md` 仍是合法 User Note。
- User Notes 与 Agent Notes 的 symlink/hardlink/FIFO/directory 均 fail closed；unsafe candidate
  继续计入 file/byte cap，不能绕过资源限制。
- append Agent Notes 的 read-modify-write 继续完整位于 per-scope cross-process lock。

`pico/features/memory.py` 只保留七个生产 helper：

```text
canonicalize_path
file_freshness
normalize_file_summaries_dict
set_file_summary_dict
invalidate_file_summary_dict
invalidate_stale_file_summaries_dict
summarize_read_result
```

允许为它们保留少量 private helper；`resolve_workspace_path` 私有化。删除 LayeredMemory、state
normalizer/mutators、tests-only retrieval/rendering、legacy mirrors。删除 `stat_all`。

**Allowlist：**

- `pico/features/memory.py`
- `pico/memory/block_store.py`
- `pico/memory/tools.py`
- `pico/memory/frontmatter.py`
- `pico/memory/retrieval.py`
- `pico/tools.py`
- `pico/prompt_prefix.py`
- `benchmarks/perf/bench_recall.py`
- `benchmarks/perf/bench_retrieval.py`
- `benchmarks/live_e2e/run_live_session.py`
- `benchmarks/live_e2e/tests/test_assertions.py`
- `tests/test_public_api_contract.py`
- `tests/test_memory.py`
- `tests/memory/test_block_store.py`
- `tests/memory/test_memory_tools.py`
- `tests/memory/test_retrieval.py`
- `tests/test_config_memory.py`
- `tests/test_tool_executor.py`
- `tests/test_artifact_security.py`
- `tests/test_context_recall_integration.py`
- `tests/test_memory_block_store_agent_scope.py`
- `tests/test_memory_frontmatter.py`
- `tests/test_memory_recall.py`
- `tests/test_memory_retrieval_field_boost.py`
- `tests/test_memory_retrieval_link.py`
- `tests/test_memory_retrieval_tombstone.py`
- 删除 `tests/test_memory_save_topic.py`
- 职责化重写 `tests/test_memory_block_store_agent_scope.py`

所有 agent fixture 同步改为 notes fixture。

Focused：Memory tools/block/security、public API、ToolExecutor、安全与 live offline tests。

## Task 6：一次 query 一个 document snapshot

提交：`refactor(memory): load one snapshot per query`。

- BlockStore 增加 private document loader；每个 candidate 一次 bounded descriptor read，同时得到
  path、size/mtime、frontmatter、first line、raw content。
- `list()` 复用该 loader。
- `Retrieval.search()` 每次只取一个临时 snapshot；tombstone、documents/fields、name index、DF、
  length/BM25、snippets、one-hop link 全部复用它。
- 删除内部再次 `store.list()` / `store.read(hit.path)` 路径。
- `recall_for_turn` 必须通过 private per-call retrieval path 消费同一个 snapshot 中的 raw/type 数据；
  不得在 search 后再次 `store.list()`/`store.read()`，也不得保存 last-snapshot cache。
- query 返回即丢弃 snapshot；同一 Retrieval 实例下一次 query 重新读磁盘，不加 watcher、cache、
  invalidation 或 public document model。

验收：

- N 个安全文件时每 query 每文件最多一次 `_read_bounded_regular`；
- 一次 `recall_for_turn` 端到端同样每文件最多一次 bounded read；
- ranking/score/snippet/tombstone/link/field boost parity；
- 修改、删除、新增、tombstone 与 link 变化在下一 query 立即可见；
- symlink/hardlink/FIFO fail closed，aggregate caps 不变；
- `stat_all` 在 production/tests 均不存在。

**Allowlist：**

- `pico/memory/block_store.py`
- `pico/memory/retrieval.py`
- `pico/memory/recall.py`
- `tests/memory/test_reader_bounds.py`
- `tests/memory/test_block_store.py`
- `tests/memory/test_retrieval.py`
- `tests/test_memory_retrieval_field_boost.py`
- `tests/test_memory_retrieval_link.py`
- `tests/test_memory_retrieval_tombstone.py`
- `tests/test_memory_recall.py`

## Task 7：Plan 3 完成门禁

Structural scans 只查批准的 exact symbols/tokens，不泛禁真实 Provider URL version、用户数据或
所有 `legacy/v1/v2` 文本。历史 benchmark results 仍按 Plan 5 删除边界排除。

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
git diff --check
```

额外运行 persistence/recovery/security、Memory 全套、benchmark family parser 与 offline live
harness。重验私有 verified migration journal、8 strict records、38 verify-only hash、`.env`
status/permissions 和七个 protected untracked。

推送 `memory`，等待 Ubuntu 3.11/3.12 CI；不得运行真实 Provider。Plan 3 全绿并基于实际 HEAD
复核后，才写 Plan 4 的 complexity baseline 与协调器收敛计划。

## Handoff 证据

- 各提交 SHA；
- migration backup/journal root 与 status（不含 path list/hash/value）；
- transformed/verify-only counts 与 business invariant counts；
- focused/full/offline/build/CI；
- 七个 protected untracked 原样状态；
- 所有 deviation、rollback 或 fault remediation。
