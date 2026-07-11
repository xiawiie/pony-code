# Pico 当前形态硬切与仓库收敛设计

- 日期：2026-07-11
- 状态：设计已获用户逐节批准，待书面审阅
- 基线分支：`memory`
- 基线提交：`2161811c416e9ba17cb4aa970ac8a240937bd022`
- 实施性质：未发布项目的有边界直接破坏，不提供弃用期或兼容 shim
- 权威关系：本文取代 `2026-07-11-pico-next-optimization-design.md` 的实施方向；后者不得作为本次 implementation plan 的输入

## 1. 背景与结论

Pico 已具备完整的 Action Kernel、Canonical Messages、安全执行、恢复、memory、benchmark
与 CLI 能力，但多轮迭代留下了四类结构债：历史文档和证据仍位于活动目录；`pico` 与
`pico-cli` 双入口；运行时、持久化与 memory 仍保留旧格式兼容路径；文件、函数、测试和
artifact 中暴露 `v1`、`v2`、`v3`、`phase1` 等实施历史。

本次采用“有边界的直接破坏”：项目尚未发布，也没有外部消费者，因此仓内调用者和本地
Pico 数据可以一次迁移后原子切换。直接破坏的边界是：保留真正有使用价值的顶层 API、
当前 Provider 协议能力、安全不变量和恢复能力；删除只为旧版本、旧导入路径、旧命名或
历史证据服务的结构。

最终仓库只表达当前产品模型，不同时保存“旧实现 + 新实现 + 迁移中间态”。Git 历史是
唯一历史档案。

## 2. 已确认决策

1. 唯一 console command 是 `pico`；删除 `pico-cli`。
2. 保留 `python -m pico` 作为 Python 标准模块执行方式，但不把它作为第二个 console entry。
3. 裸 prompt 兼容入口删除；一次性任务显式使用 `pico run`，交互使用 `pico repl`。
4. 旧 session、run、checkpoint、tool-change、verification 和 memory 数据先迁移一次，验证后删除迁移器。
5. 最终 runtime 只识别当前格式，不提供 deprecated alias、warning 或 compatibility shim。
6. 文件名、模块名、类名、函数名、测试名、benchmark 名和用户文案不得包含版本代号。
7. 持久化文件保留内部 `format_version`，用于完整性校验；版本号不进入领域命名。
8. Git 历史是唯一档案；不建立 `docs/archive/`。
9. 删除 Provider 和 evaluation 聚合重导出层；仓内调用者从真实实现模块导入。
10. `pico.__init__` 只保留 `Pico`、`SessionStore`、`WorkspaceContext` 和明确的 CLI API。
11. Memory 写入真源是单一 append-only `agent_notes.md`；不继续推进 per-topic agent 写入模型。
12. User Notes 的 frontmatter、`[[name]]` link expansion 和 `supersedes` 检索属于当前能力，保留。
13. 不以减少行数为理由放宽安全 guard；核心复杂度只按真实生命周期边界收敛。

## 3. 基线证据与优先问题

基线已验证：Ruff 通过；pytest `1997 passed, 6 skipped`；wheel 与 sdist 可构建；Memory fake
benchmark 8/8；macOS 全量测试出现两条多线程进程使用 `fork()` 的弃用警告。

### 3.1 Memory CLI 文件边界缺口

`pico/cli_memory.py` 的 `memory review` 直接使用 `Path.exists()` 和 `Path.read_text()`；两条
memory migration 还直接使用 `read_text()`、`write_text()`、`copy2()`、`rename()`。临时
工作区已复现：`agent_notes.md` 指向工作区外文件时，`memory review` 会打印外部内容。

目标：复用已有 `BlockStore`、anchored regular-file reader、private atomic writer 和现有
路径身份检查；拒绝 symlink、hardlink、FIFO、目录和 inode swap。不得新建第三套路由或
路径验证器。

### 3.2 复杂度超过“小型 agent”定位

生产代码约 23,624 行，复杂度超过 10 的函数约 71 个。主要风险位于
`ToolExecutor.execute()`、`RecoveryManager`、`RecoveryPolicy`、`safe_subprocess` 和
`AgentLoop.run()`。

目标：冻结新的安全规则和 recovery 状态扩张；优先将 ToolExecutor 现有流程显式化为
“校验与审批 → 执行 → 效果归档 → 异常终结”，再收敛 AgentLoop。Recovery、安全 parser
和 subprocess 边界不做全仓机械重构。

### 3.3 发布与复现工程不完整

`pyproject.toml` 元数据最小；sdist 包含大量 tests；`uv.lock` 被忽略；CI 没有 wheel 安装
smoke。项目当前不进入公开发布，因此不得擅自声明 License；但应补齐不涉及法律选择的
README、authors、project URLs 和构建边界。License 是未来发布前的单独所有者决定，不是
本次硬切的阻塞条件。

### 3.4 macOS 证据不足

CI 只有 Ubuntu 3.11/3.12，而 Pico 依赖 POSIX mode、`fcntl`、inode、hardlink、FIFO、Git
metadata 和 subprocess 行为。目标是增加 Python 3.12 的 macOS focused security job，并
消除当前 fork warning；不复制完整 macOS matrix。

### 3.5 重复 TOML parser

Python 最低版本已是 3.11，`tomllib` 恒可用。手写 `_parse_scalar` / `load_pico_toml` 与
malformed TOML 的宽松 fallback 必须删除。格式错误只警告并使用默认配置，不猜测部分值。

### 3.6 Memory retrieval 线性重复工作

现有查询在 10/100/1000 notes 时中位数约为 2.8/21.6/206ms；每次搜索重复 list、frontmatter
解析、文件读取、DF 和 name index 构建。本次实现最小缓存：以 `stat_all()` 快照失效，缓存
解析后的 documents、tombstones 和 name index。验收使用调用次数与失效行为，不使用易抖动
的 wall-clock 硬阈值。

### 3.7 Memory 新旧模型并存

当前工具既可写单一 `agent_notes.md`，也可写 `agent/<topic>.md`，并保留两套 migration。
本文以现有 glossary 的单一 Agent Notes 为准：新写入只追加 `agent_notes.md`；已有
`agent/*.md` 一次性合并后删除 per-topic 写入、topic/type 参数和第二套迁移。

### 3.8 文档、证据和兼容层堆积

历史 specs/plans/SDD 报告与 benchmark JSON 已超过生产源码规模。本文要求物理删除历史
资产，不改写历史 `DATA_PROVENANCE.md`；其原文只留在 Git 历史。兼容层不设“退休日期”，
而是在本次硬切中直接退休。

## 4. 目标领域语言

`CONTEXT.md` 在代码切换时同步更新，只描述当前概念：

- **Pico CLI**：唯一 console command `pico`。
- **Model Request**：system、tools、messages、token budget 组成的 Provider 请求。
- **Model Response**：Provider-neutral 返回。
- **Action**：解码后的 Tool、Final 或 Retry 决策。
- **Canonical Messages**：Session 中唯一 transcript。
- **Text Protocol Adapter**：为 text-only Provider 转换结构化请求的当前能力适配器。
- **Format Version**：持久化文件内部编码版本。
- **User Notes**：用户维护、agent 只读的 Markdown。
- **Agent Notes**：唯一 append-only `agent_notes.md`。
- **Recovery Record**：checkpoint、tool-change、verification 等当前持久记录。

命名规则：模块和类型用职责名；函数用动词；禁止以 `new`、`old`、`legacy`、`current` 或
`vN` 区分实现代际；注释解释不变量，不记录 Task/Phase 历史。`compatible` 只用于真实协议，
`fallback` 只用于真实失败回退。

## 5. CLI 单入口

`pyproject.toml` 最终只包含：

```toml
[project.scripts]
pico = "pico.cli:main"
```

删除所有 `pico-cli` usage、帮助、README、安装说明、测试与脚本引用。macOS 自带
`/usr/bin/pico` 的冲突通过安装验证处理：安装后 `command -v pico` 必须指向当前环境；不为
此保留第二入口。

删除 bare prompt compatibility。`pico` 无参数显示 help；`pico run` 和 `pico repl` 是唯一
运行入口。保留 `python -m pico`、`main`、`build_agent`、`build_arg_parser`、`build_welcome`。

新增安装 smoke：`command -v pico`、`pico --help`、`pico doctor --offline`。

## 6. Provider 与 Context 当前接口

删除 `pico/providers/clients.py`。将其中唯一真实实现 `FakeModelClient` 移到
`pico/providers/fake.py`。CLI 和 tests 从各 Provider 实际模块导入。

统一结构化接口：

```python
client.complete(
    system=system,
    tools=tools,
    messages=messages,
    max_tokens=max_tokens,
    cache_breakpoints=cache_breakpoints,
)
```

删除 `complete_v2` 和旧的 runtime prompt-string Provider 接口。Runtime 不再使用
`hasattr(..., "complete_v2")` 判断代际。text-only transport 由显式装配的
`TextProtocolAdapter` 承担；它是当前能力转换，不是旧版本兼容。`FallbackAdapter` 相应改名。

Context 重命名：

| 旧名称 | 当前名称 |
| --- | --- |
| `ContextManager.build_v2` | `ContextManager.build_request` |
| `_count_tokens_for_v2` | `count_tokens` |
| `complete_v2` | `complete` |
| `FallbackAdapter` | `TextProtocolAdapter` |
| `bench_build_v2.py` | `bench_request_build.py` |

删除 `test_p1_smoke.py`、`test_p2_smoke.py`、`test_p3_smoke.py` 等阶段门测试；具体合同测试保留。

## 7. 删除聚合层与顶层重导出

删除：

- `pico/providers/clients.py`
- `pico/evaluation/metrics.py`
- `pico/evaluation/metrics_experiments.py`
- `pico/evaluation/evaluator.py`

仓内生产代码、测试、scripts、benchmarks 和当前文档全部改为从真实模块导入：

- fixed benchmark / `BenchmarkEvaluator` → `fixed_benchmark`
- benchmark validation → `benchmark_schema`
- context/memory/recovery ablation → `experiments_recovery`
- synthetic experiments → `experiments_synthetic`
- real experiments → `experiments_real`
- provider experiments → `provider_benchmark`
- aggregation/reporting → `metrics_reports`
- shared math/time → `metrics_common`

不提供 alias、warning 或 shim。删除只验证重导出存在的 public-contract tests。

`pico.__init__` 最终只导出：`Pico`、`SessionStore`、`WorkspaceContext`、`main`、
`build_agent`、`build_arg_parser`、`build_welcome`。

## 8. 持久化格式硬切

最终所有持久化对象使用 `format_version`；名称中不编码版本：

```json
{"record_type": "session", "format_version": 1}
```

`schema_version`、`checkpoint-record-v1`、`tool-change-record-v1`、`phase1-v1` 等全部删除。
常量统一为 `SESSION_FORMAT_VERSION`、`CHECKPOINT_FORMAT_VERSION`、
`TOOL_CHANGE_FORMAT_VERSION`、`VERIFICATION_FORMAT_VERSION`、`RESTORE_PLAN_FORMAT_VERSION`、
`BENCHMARK_FORMAT_VERSION`。

一次性迁移范围：repo `.pico/sessions`、`runs`、`checkpoints`、`memory` 和 Pico 自有
`~/.pico/memory`。迁移器只存在于开发分支中间提交，最终删除。

迁移事务必须：关闭运行进程；获取 store lock；拒绝 symlink/hardlink/FIFO/目录/越界；生成
路径、size、SHA-256 manifest；在仓库外私有目录备份原字节；同目录临时写、fsync、验证、
原子替换；用最终 Store API 全量重读；失败时整批恢复。

迁移保持 ID、messages 顺序、blob hash、trace 顺序、memory 文本和时间戳。最终删除所有
v1/v2/v3 converter、`legacy=True` 分支、additive defaults 和 migration tests。未知
`format_version` 继续 fail closed。

## 9. Memory 安全、模型与性能

### 9.1 CLI 安全修复

`memory review` 和一次性 memory migration 必须复用已有安全 reader/writer。分别覆盖：

- symlink 拒绝
- hardlink 拒绝
- FIFO 不阻塞且拒绝
- directory 拒绝
- read/replace 间 inode swap 拒绝
- 正常 private regular file 成功

错误不得输出外部真实路径或文件内容。修复后重新运行已复现 canary，输出不得包含 canary。

### 9.2 单一写入模型

`memory_save` 只接受 note 与 scope，只追加 `agent_notes.md`。删除 topic/type 参数、
`write_agent_topic`、topic slug、`agent/legacy-import.md` 和两套长期 migration API。已有
per-topic agent 文件在硬切迁移中按稳定路径顺序合并，保留来源标题和原始文本，验证后删除。

User Notes 的 frontmatter、links、supersedes 和 BM25 field boosts 保留。

### 9.3 检索缓存

`Retrieval` 缓存解析后的 docs、tombstones、name index、DF 和长度统计；`stat_all()` 快照变化
时整体失效。memory tool 写入显式失效；用户外部编辑由下一次 snapshot 检测。缓存只存在于
进程内，不增加数据库、索引文件、线程或依赖。

测试证明：未变化的第二次查询不重复读取/解析正文；新增、修改、删除、tombstone 与 link
变化都会失效；结果排序和分数与未缓存实现一致。

## 10. 配置、构建与 CI

删除手写 TOML parser，统一 `tomllib`。malformed 文件警告并返回默认配置；不做宽松 fallback。

提交 `uv.lock`，从 `.gitignore` 移除，CI 使用 `uv sync --frozen --dev`。运行时依赖保持 0。

补充不涉及法律选择的 package metadata：README、authors、project URLs、classifiers；License
不擅自声明，作为未来公开发布前的独立 owner decision。收紧 sdist，禁止打入整个 tests 树。

CI 保留 Ubuntu 3.11/3.12 full jobs，增加 macOS 3.12 focused job，覆盖 file lock、private path、
safe subprocess、artifact security、shell corpus 和 recovery durability。修复多线程 fork warning，
优先使用 spawn context 或避免测试进程在后台线程存活时 fork，不放宽安全断言。

CI 增加 wheel smoke：build → clean venv install → `pico --help` → `pico doctor --offline`。

## 11. 复杂度收敛边界

暂停新增安全规则和 recovery 状态。首先锁定 ToolExecutor 行为矩阵，再按四个真实阶段提取：

```text
validate_and_approve
execute_tool
record_effects
finalize_failure_or_result
```

这些名称在 implementation plan 中可按现有术语微调，但不得引入 registry、plugin framework、
event bus 或新的执行状态机。Pending Tool Change 创建后，每个出口必须 terminalize 或留下明确
interrupted evidence；安全判断只能有一个真源。

随后收敛 AgentLoop 为 preflight → one model attempt → apply Action → finalize once。保持
`Response → decode_action → Action` 唯一路径、one-shot retry feedback、tool pair 原子持久化、
usage 聚合和 primary exception 顺序。

RecoveryManager、RecoveryPolicy、safe_subprocess 和 security 本次不因文件大小拆分；只修复
本 spec 直接触发的回归。复杂度检查采用不增长 ratchet，不把现有安全分支压成不可读技巧。

## 12. 文档与证据资产

物理删除：

- `docs/superpowers/`
- `.superpowers/`
- `benchmarks/results/`
- `docs/review-pack/`
- 旧 `docs/architecture/agent-harness-v1-overview.md`
- 以 first-phase/旧实现为核心且已失效的 ADR

历史 `DATA_PROVENANCE.md` 不改写，随结果目录删除，原文只存在于 Git 历史。

最终保留并重写：`README.md`、`CONTEXT.md`、CLI 安装文档；新增当前
`docs/architecture.md`、`docs/security.md`、`docs/recovery.md`、`docs/verification.md`；
`docs/memory-model.md` 改为 `docs/memory.md`。

只保留两个当前 ADR：唯一 `pico` CLI、单一当前格式且 runtime 无兼容层。ADR 简短记录为何
项目未发布且无外部消费者时选择硬切。其余当前架构事实进入上述权威文档。

Benchmark 大型 JSON 只写临时输出目录。`docs/verification.md` 记录 commit SHA、命令、平台、
Python、pass/fail、测试数和安全摘要，不提交 prompt、answer、key、header 或 request URL。

## 13. 分阶段实施与提交边界

1. **Preflight**：固定删除/rename manifest、基线、当前数据 dry-run；保护未跟踪文件。
2. **Memory CLI safety**：先修可复现的外部读取，再做其他迁移。
3. **One-time migration**：写、测、dry-run、备份、执行、验证；随后删除迁移器。
4. **Provider/evaluation imports**：移动 Fake、删除聚合层、统一 complete/build request。
5. **Persistent formats**：切 `format_version`，删除旧 readers、fixtures、tests。
6. **Memory convergence/cache**：单一 Agent Notes，删除 per-topic，增加进程内缓存。
7. **CLI/naming**：只留 `pico`，删除 bare prompt，完成全仓 rename。
8. **Config/package/CI**：tomllib、lock、metadata、sdist、macOS、wheel smoke、fork warning。
9. **Core coordinators**：先 ToolExecutor，后 AgentLoop；每项独立提交。
10. **Docs/evidence deletion**：写当前文档后删除历史资产和被本文取代的 specs。
11. **Structural and final audit**：运行全部门禁并逐项对照本文完成矩阵。

阶段可以有中间提交，但不得发布或合并中间兼容状态。每项只 stage allowlist 中的文件，不自动
移动、删除或提交当前用户未跟踪资料。

## 14. 结构检查

新增 `tests/test_repository_structure.py`，精确检查：

- 已删除模块不存在。
- `pyproject.toml` 只有 `pico` console script。
- 旧 imports 为零。
- 活动 Python 标识符无 `_v1/_v2/_v3/phase1`。
- benchmark 文件和 artifact type 无版本后缀。
- 当前代码与文档无 `pico-cli`。
- 活动持久化代码无 `schema_version`。
- runtime 无 migration/deprecated alias。
- docs/superpowers、.superpowers、benchmark results 和旧 review pack 不存在。

规则使用 AST 或精确 token/路径清单，不粗暴禁止所有数字、`compatible` 或普通错误 fallback。
结构测试排除自身的 forbidden-string fixture，避免自命中。

## 15. 验证门

每个任务先跑 focused tests、touched-path Ruff、`git diff --check`。每个阶段运行：

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
```

干净环境安装 wheel 后运行 `pico --help` 和 `pico doctor --offline`。运行 harness、context、
memory、recovery、memory-quality 和 perf smokes，结果只写临时目录。

Provider/Context 接口改名属于真实 wire path 变化；全部本地门通过后，另行取得一次明确授权，
执行一个真实 Provider E2E。不得自动沿用旧授权或运行 Provider matrix。

## 16. 实施完成后的合规审计

实现者不能只报告测试绿色，必须逐条填写以下矩阵并给出命令或文件证据：

| 规划项 | 完成证据 |
| --- | --- |
| Memory CLI symlink/hardlink/FIFO/inode-swap 已关闭 | focused security tests + canary reproduction |
| ToolExecutor/AgentLoop 生命周期已收敛且复杂度未增长 | symbol/complexity report + behavior matrix |
| package metadata、sdist、wheel smoke 完成 | wheel METADATA + archive listing + clean install |
| uv.lock/frozen CI 完成 | tracked lock + CI command |
| macOS focused CI 与 fork warning 完成 | CI run + warning-free local test |
| TOML 只使用 tomllib | structural scan + malformed-config tests |
| Retrieval 缓存正确失效且结果不变 | read-count/invalidation/parity tests |
| Memory 只有单一 Agent Notes 写入模型 | tool schema + structural scan + migration evidence |
| 历史文档和证据资产物理删除 | path absence + Git diff |
| Provider/evaluation 聚合层删除 | module absence + import scan |
| 唯一 CLI 为 pico，bare prompt 已删除 | pyproject + CLI smoke + usage scan |
| v1/v2/v3/phase 命名从活动代码消失 | AST/token structure test |
| 旧持久化数据完成一次迁移，runtime 无迁移器 | manifest、backup/restore test、source scan |
| 顶层 API 边界符合批准清单 | import contract test |
| 当前文档链接、命令和术语一致 | link/path/command scan |
| 全量测试、benchmark、build 与真实 E2E 达标 | final verification summary |

任何一项没有证据即视为 spec 未完成；不能以“已在其他测试间接覆盖”代替明确核对。被设计明确
排除的 License 决策和 Provider matrix 标为 `deferred by design`，不得伪报完成。

## 17. 非目标

- 新 Provider、registry、gateway、plugin framework。
- 数据库、向量索引、持久化 retrieval cache。
- 并行工具、多 agent runtime、UI、TUI。
- Windows 支持或 OS sandbox 重设计。
- 为 LOC/复杂度数字删除安全 case。
- 自动读取父目录或兄弟 worktree 配置。
- 未经授权的真实 API 调用。
- 公开发布或擅自选择 License。

## 18. 完成定义

只有以下条件同时成立才完成：唯一 `pico`；唯一结构化 Provider 请求接口；唯一 Canonical
Messages；唯一 Agent Notes 写入模型；所有 Store 只认识当前 `format_version`；聚合层、迁移器、
deprecated alias 和版本化命名消失；历史文档/证据物理删除；当前 Pico 数据完成迁移并验证；
Memory CLI 外部读取漏洞关闭；Ruff、全量 tests、offline live、benchmarks、build、clean install、
macOS focused CI 和经授权的单一 Provider E2E 全部通过；第 16 节合规矩阵逐项有证据。
