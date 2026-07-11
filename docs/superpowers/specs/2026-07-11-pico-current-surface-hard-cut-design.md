# Pico 当前形态硬切与仓库收敛设计

- 日期：2026-07-11
- 状态：两份设计已完成源码核验、冲突决策与整合，待用户书面审阅；尚未进入 implementation planning
- 基线分支：`memory`
- 源码基线：`5f359bd18fb3a59968167bfe0196352d41a23a01`
- 文档整合基线：`3ec211ebee59a3a8beb1d654a09a9ea87de9e5c7`
- 实施性质：未发布项目的有边界直接破坏，不提供弃用期或兼容 shim
- 实施组织：一份权威 master spec，拆成五份顺序执行、独立验证和独立回滚的 implementation plans
- 权威关系：本文取代 `2026-07-11-pico-next-optimization-design.md`；后者只保留短暂的 superseded 指针，不得作为 implementation plan 输入

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

本文不是把两份旧设计机械拼接。它保留硬切设计的当前形态目标，并吸收原优化设计中已经
由源码证实的运行可见性、CI、依赖复现、dead-code 删除和验证要求；冲突项已经逐项决定。

## 2. 已确认决策

1. 唯一 console command 是 `pico`；删除 `pico-cli`。
2. 保留 `python -m pico` 作为 Python 标准模块执行方式，但不把它作为第二个 console entry。
3. 裸 prompt 兼容入口删除；一次性任务显式使用 `pico run`，交互使用 `pico repl`。
4. 旧 session、run、checkpoint、tool-change、verification 和 memory 数据先迁移一次，验证后删除迁移器。
5. 最终 runtime 只识别当前格式，不提供 deprecated alias、warning 或 compatibility shim。
6. 本次批准清单中仅用于区分实施代际的文件、模块、符号、测试、benchmark 和用户文案改为
   当前职责名；不建立对所有版本文本的全局禁令。
7. 需要兼容性判定的 structured record families 保留内部 `format_version`；版本号不进入领域命名。
8. Git 历史是唯一档案；不建立 `docs/archive/`。
9. 删除 Provider 和 evaluation 聚合重导出层；仓内调用者从真实实现模块导入。
10. `pico.__init__` 只保留 `Pico`、`SessionStore`、`WorkspaceContext` 和明确的 CLI API。
11. Memory 写入真源是单一 append-only `agent_notes.md`；不继续推进 per-topic agent 写入模型。
12. User Notes 的 frontmatter、`[[name]]` link expansion 和 `supersedes` 检索属于当前能力，保留。
13. 不以减少行数为理由放宽安全 guard；核心复杂度只按真实生命周期边界收敛。
14. `.env` 继续只从 lexical repo root 精确读取；通过诊断显示路径和状态，不扩大搜索范围。
15. `memory` 与 `main` 的直接 push 都触发 CI；`uv.lock` 被跟踪且 CI frozen sync。
16. 删除没有行为消费者的 `prompt_cache` feature flag 和 tests-only `LayeredMemory` 状态系统；
    保留真正工作的 Provider cache 能力和七个生产 memory helpers。
17. Retrieval 不建立跨查询 cache；一次查询只构建一个一致性 document snapshot，所有计算复用它。
18. 构建与 clean-install 必须可验证；authors、project URLs、classifiers 和 License 延后到真正发布。
19. 结构检查只执行本次批准的精确 hard-cut manifest，不建立泛化的版本字符串 lint。
20. 当前 master spec 只产生五份顺序 implementation plans，不继续扩张子 spec。

## 3. 基线证据与优先问题

源码基线已验证：Ruff 通过；pytest `1997 passed, 6 skipped`；offline live harness `60 passed`；
wheel 与 sdist 可构建；Memory fake benchmark 8/8；macOS 全量测试出现两条多线程进程使用
`fork()` 的弃用警告。

最终真实证据为 DeepSeek `qwen3.7-max`：43/43 assertions、8 个 native actions、10/15 次
Provider calls、13,842 input tokens、1,330 output tokens、5,248 cache-read tokens、44.253 秒。
key、payload、active artifact、private mode、fixture restoration、session、terminal artifact、call cap
和 token cap 检查全部通过；独立 review 为 C0 / I0 / M0。live JSON 仍故意不提交。

规模基线：生产 Python 23,624 行，tests 31,388 行，运行时依赖为 0。本地当前 workspace 的
`.pico` 有 46 个 regular files、约 580 KiB；其中 sessions 5、runs 36、checkpoints 5、memory 0。
当前 `~/.pico/memory` 为空。一次性迁移设计必须以这个显式清单为起点，不能扫描其他仓库。

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

### 3.3 构建与复现工程不完整

`pyproject.toml` 元数据最小；sdist 包含大量 tests；`uv.lock` 被忽略；CI 没有 wheel 安装
smoke。项目当前不进入公开发布，因此只补 `readme`、准确 description、构建边界和安装验证。
authors、project URLs、classifiers 和 License 都属于未来发布决策，本次不填推测值。

### 3.4 macOS 证据不足

CI 只有 Ubuntu 3.11/3.12，而 Pico 依赖 POSIX mode、`fcntl`、inode、hardlink、FIFO、Git
metadata 和 subprocess 行为。目标是增加 Python 3.12 的 macOS focused security job，并
消除当前 fork warning；不复制完整 macOS matrix。

### 3.5 重复 TOML parser

Python 最低版本已是 3.11，`tomllib` 恒可用。手写 `_parse_scalar` / `load_pico_toml` 与
malformed TOML 的宽松 fallback 必须删除。格式错误只警告并使用默认配置，不猜测部分值。

### 3.6 Memory retrieval 单次查询重复扫描

现有查询在 10/100/1000 notes 时中位数约为 2.8/21.6/206ms；一次 `search()` 最多触发四次
`store.list()`，而 `list()` 自身已经读取并解析文件，随后 `_load_docs()` 和 link expansion 又
重复读取。原“以 `stat_all()` 失效的跨查询 cache”设计无效，因为当前 `stat_all()` 仍调用
`list()`，没有消除主要 I/O。

本次改为单次查询一致性 snapshot：BlockStore 通过现有 anchored/bounded reader 每个文件只读
一次，Retrieval 从同一批 metadata、frontmatter 和 raw content 计算 tombstones、name index、
tokens、DF、BM25 与 link expansion。不保留跨查询状态，因此没有 cache invalidation 或陈旧结果。

### 3.7 Memory 新旧模型并存

当前工具既可写单一 `agent_notes.md`，也可写 `agent/<topic>.md`，并保留两套 migration。
本文以现有 glossary 的单一 Agent Notes 为准：新写入只追加 `agent_notes.md`；已有
`agent/*.md` 一次性合并后删除 per-topic 写入、topic/type 参数和第二套迁移。

### 3.8 文档、证据和兼容层堆积

历史 specs/plans/SDD 报告与 benchmark JSON 已超过生产源码规模。本文要求物理删除历史
资产，不改写历史 `DATA_PROVENANCE.md`；其原文只留在 Git 历史。兼容层不设“退休日期”，
而是在本次硬切中直接退休。

### 3.9 配置来源不可见

`.env` 已采用正确的 exact-root 语义，但 `config show`、`doctor`、`init` 和 `config set-secret`
没有一致展示完整路径。主 checkout 与 worktree 使用不同 `.env` 时，这会把路径错误表现成
Provider 401。目标是显示 repo root、精确 `.env` path、`repo_root_exact` scope 和
`loaded | missing | review_required` 状态；绝不读取父目录或兄弟 worktree。

### 3.10 无效开关与 tests-only memory facade

`DEFAULT_FEATURE_FLAGS["prompt_cache"]` 没有任何 `feature_enabled("prompt_cache")` 生产调用方；
真正 cache 行为由 Provider capability 控制。`pico/features/memory.py::LayeredMemory` 及其
state-level episodic/retrieval API 也只被 tests 导入；生产代码只使用路径、freshness、
file-summary normalization/mutation 和 read-summary 七个 helpers。两者都应直接删除。

## 4. 目标领域语言

`CONTEXT.md` 在代码切换时同步更新，只描述当前概念：

- **Pico CLI**：唯一 console command `pico`。
- **Model Request**：system、tools、messages、token budget 组成的 Provider 请求。
- **Model Response**：Provider-neutral 返回。
- **Action**：解码后的 Tool、Final 或 Retry 决策。
- **Canonical Messages**：Session 中唯一 transcript。
- **Text Protocol Adapter**：为 text-only Provider 转换结构化请求的当前能力适配器。
- **Project Environment**：当前 lexical repo root 下唯一允许读取的 `.env`。
- **Format Version**：持久化文件内部编码版本。
- **User Notes**：用户维护、agent 只读的 Markdown。
- **Agent Notes**：唯一 append-only `agent_notes.md`。
- **Query Snapshot**：一次 memory 查询内共享的 metadata、frontmatter 与 raw-content 视图；查询结束即释放。
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

这个切换必须原子完成：Provider method、Context build method、AgentLoop caller、scripted test
clients 和 TextProtocolAdapter 在同一计划中切换，不能留下“新 caller + 旧 adapter”中间状态。
Provider 的 request payload、Response usage、Action decode 和 prompt-cache capability 行为不变。

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

`pico.providers.__init__` 不再重导出 client class，只作为 package marker。所有 Provider client
从实际模块导入。

`pico.__init__` 最终只导出：`Pico`、`SessionStore`、`WorkspaceContext`、`main`、
`build_agent`、`build_arg_parser`、`build_welcome`。

## 8. 持久化格式硬切

最终所有需要兼容性判定的 Pico-owned structured record families 使用 `record_type` 和
`format_version`；Markdown memory、`.env`、TOML 和普通文本不添加无意义版本字段：

```json
{"record_type": "session", "format_version": 1}
```

versioned families 明确包括 session、checkpoint、tool-change、verification、restore plan 和
benchmark artifact。它们当前使用的 `schema_version`、`checkpoint-record-v1`、
`tool-change-record-v1`、`phase1-v1` 等全部删除。常量统一为 `SESSION_FORMAT_VERSION`、`CHECKPOINT_FORMAT_VERSION`、
`TOOL_CHANGE_FORMAT_VERSION`、`VERIFICATION_FORMAT_VERSION`、`RESTORE_PLAN_FORMAT_VERSION`、
`BENCHMARK_FORMAT_VERSION`。

一次性迁移默认范围只包括显式指定的当前 repo `.pico/sessions`、`runs`、`checkpoints` 和
`memory`。不发现、不遍历、不修改其他 repo、worktree 或 checkout。`~/.pico/memory` 只有在
preflight manifest 明确发现数据并单独列入批准范围时才处理；当前基线为空。迁移器只存在于
开发分支中间提交，最终删除。

迁移事务必须：关闭运行进程；获取 store lock；拒绝 symlink/hardlink/FIFO/目录/越界；生成
路径、size、SHA-256 manifest；在仓库外 0700/0600 私有目录备份原字节；同目录临时写、fsync、
验证、原子替换；用最终 Store API 全量重读；失败时整批恢复。备份不自动删除；迁移器删除后
需要回滚时，使用私有备份和仍包含迁移器的中间 Git commit。

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

### 9.3 单次查询一致性 snapshot

不建立进程内跨查询 cache。BlockStore 增加一个复用现有安全扫描与 bounded reader 的内部
document-load 路径；每个文件在一次查询中最多读取一次，并同时返回当前 metadata、frontmatter
和 raw content。`list()` 与 Retrieval 复用同一个底层读取真源，不增加 metadata-only 路径、
第二套路由器或文件观察线程。

`Retrieval.search()` 只获取一次 snapshot，并从中完成：

- supersedes/tombstone 集合；
- 可检索 documents 与 field tokens；
- frontmatter name index；
- DF、长度统计和 BM25；
- link expansion 所需的 primary raw content。

查询返回后 snapshot 即释放；下一次查询重新读取磁盘，因此新增、修改、删除、tombstone 和 link
变化立即可见，不需要 mtime cache invalidation。测试证明每个文件每次查询最多一次 bounded read、
排序/分数/snippets 与基线一致、下一查询可见外部修改，并继续拒绝 symlink/hardlink/FIFO。

### 9.4 删除 tests-only LayeredMemory

删除 `LayeredMemory`、`default_memory_state`、`normalize_memory_state`、state-level task/file/note
mutation、tests-only retrieval/rendering 和 legacy `task/files/notes` mirrors。项目未发布，不提供
deprecation 或 import shim。

保留生产实际使用的 `canonicalize_path`、`file_freshness`、`normalize_file_summaries_dict`、
`set_file_summary_dict`、`invalidate_file_summary_dict`、`invalidate_stale_file_summaries_dict` 和
`summarize_read_result`。若继续放在 `features.memory` 最少改动，就保留该文件名，不为命名新建
模块。删除只验证 facade 存在的 tests；session/current-format tests 继续验证 canonical
`working_memory` 与 `memory.file_summaries`。

## 10. 配置、构建与 CI

### 10.1 Project Environment 可见性

配置真源仍是 `WorkspaceContext.repo_root/.env`。不增加 parent search、sibling-worktree search、
main-checkout fallback 或 shell expansion。`config show`、`doctor`、`init` 和 `config set-secret`
统一显示：

```json
{
  "workspace": {"repo_root": "/absolute/current/worktree"},
  "project_env": {
    "path": "/absolute/current/worktree/.env",
    "scope": "repo_root_exact",
    "status": "loaded"
  }
}
```

status 只允许 `loaded`、`missing`、`review_required`。输出不得列出变量值、API key、header 或
未脱敏 URL。缺失时明确提示每个 worktree 使用自己的 repo-root `.env`，Pico 不读取其他
checkout。测试覆盖 root、子目录、两个 worktree、缺失、symlink/hardlink/directory 和 secret
redaction；JSON 只做 additive schema change。

### 10.2 TOML 与依赖复现

删除 `_parse_scalar`、手写 `load_pico_toml`、Python <3.11 fallback 和 malformed TOML 的宽松部分
解析。所有配置读取统一使用 stdlib `tomllib`；文件不存在返回默认，格式错误给固定 warning 并
整文件返回默认，不猜测部分值。

提交 `uv.lock`，从 `.gitignore` 移除，CI 使用 `uv sync --frozen --dev`。lockfile 使用 CI 已固定
的 uv `0.11.26` 生成并最终检查；设计自审时本机 uv `0.11.19` 的 `uv lock --check` 虽通过，
但不能作为生成版本。运行时依赖保持 0，lock 更新独立 review，业务提交不顺手刷新版本。

### 10.3 CI 触发与平台范围

Ubuntu 3.11/3.12 full jobs 保留 lint、全量 pytest 和 offline live harness。直接 push 到 `main`
或 `memory` 都必须触发 CI；pull request 继续全覆盖。

增加 Python 3.12 `macos-latest` focused job，使用 frozen lock，覆盖 project env security、file
lock、private paths、artifact security、safe subprocess、shell corpus 和 recovery durability。
修复多线程进程使用 `fork()` 的两条弃用 warning，优先使用 spawn context 或避免测试在后台线程
存活时 fork；不能用 blanket skip 或放宽安全断言通过 macOS。

### 10.4 构建边界

`pyproject.toml` 保留准确 description、`readme = "README.md"`、Python 版本、零运行时依赖和唯一
`pico` console entry。收紧 sdist，禁止打入整个 tests 树。CI 增加：

```text
build wheel + sdist
  -> inspect archive contents and wheel METADATA
  -> clean venv install wheel
  -> command -v pico resolves to that environment
  -> pico --help
  -> pico doctor --offline
```

authors、project URLs、classifiers 和 License 都延后到真正公开发布，不填推测值。

### 10.5 删除 dead prompt-cache flag

从 `DEFAULT_FEATURE_FLAGS` 删除没有消费者的 `prompt_cache`。一次性 checkpoint migration 丢弃
旧 identity 中该 key，最终 identity 只保存真实影响行为的 flags；不在 runtime 增加兼容过滤。
Anthropic/OpenAI-compatible 的 `supports_prompt_cache`、payload、cache breakpoint 和 usage tests
保持不变。删除的是假开关，不是 Provider cache 能力。

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

在当前权威文档写完并验证链接后，从 Git 当前树删除以下**已跟踪**资产：

- `docs/superpowers/`
- `.superpowers/sdd/`
- `benchmarks/results/`
- `docs/review-pack/`
- 旧 `docs/architecture/agent-harness-v1-overview.md`
- 以 first-phase/旧实现为核心且已失效的 ADR

历史 `DATA_PROVENANCE.md` 不改写，随结果目录删除，原文只存在于 Git 历史。不建立
`docs/archive/`，也不先修复即将删除的 Review Pack；只提取其中仍为当前真值的安全摘要。

最终保留并重写：`README.md`、`CONTEXT.md`、CLI 安装文档；新增当前
`docs/architecture.md`、`docs/security.md`、`docs/recovery.md`、`docs/verification.md`；
`docs/memory-model.md` 改为 `docs/memory.md`。

只保留两个当前 ADR：唯一 `pico` CLI、单一当前格式且 runtime 无兼容层。ADR 简短记录为何
项目未发布且无外部消费者时选择硬切。其余当前架构事实进入上述权威文档。

Benchmark 大型 JSON 只写临时输出目录。`docs/verification.md` 分开记录“硬切前基线”和“最终
HEAD”：各自的 commit SHA、命令、平台、Python、pass/fail、测试数和安全摘要。基线包括
1997/6、offline 60、真实 43/43、native action、Provider call/token caps 与 C0/I0/M0；最终数值
必须来自 Plan 5 新运行，不能复制基线冒充结果。文档不提交 prompt、answer、key、header、
request URL 或 live JSON；确定性结果通过记录的当前命令重建。

本地未跟踪的 `.superpowers/brainstorm/`、`task_plan.md`、`findings.md`、`progress.md` 和其他
用户资料不移动、不删除、不 stage。最终结构审计检查 `git ls-files` 中的 tracked path 归零，
不要求这些本地目录物理不存在。

## 13. 五份顺序 implementation plans

本文是唯一 master spec。实施不写更多子 spec，而是依次生成五份计划；只先为当前下一阶段
运行 writing-plans，上一计划全绿、提交并复核实际树后，才基于新 HEAD 写下一计划，避免提前
生成很快失真的巨型计划。任何计划都不得发布或合并中间兼容状态。

### Plan 1：安全与运行基线

- 固定 rename/delete manifest、源码基线、当前 `.pico` 数据 manifest 和 staged allowlist。
- 先为 Memory CLI symlink canary 写失败测试，再让 review/migration 复用现有安全 reader/writer。
- 增加 exact-root `.env` path/status/scope 诊断与 worktree tests。
- 提交由 uv 0.11.26 生成的 lock，CI frozen sync，push branches 加 `memory`。
- 运行 focused security tests、全量本地门禁和 offline live assertions。

### Plan 2：当前 Python、Provider、Context 与 CLI surface

- 移动 Fake，删除 Provider/evaluation 聚合重导出层，收窄 `pico.__init__`。
- 原子切换 `complete` / `build_request` / `TextProtocolAdapter`，删除 prompt-string runtime path。
- 只保留 `pico` 与 `python -m pico`，删除 `pico-cli`、bare prompt 和相关文案/tests。
- 删除手写 TOML parser、dead `prompt_cache` flag 和已批准的版本化公共命名。
- 更新生产代码、tests、scripts、benchmarks 和当前文档的实际 imports；历史材料不改写。

### Plan 3：持久化与 Memory 当前格式硬切

- 写并测试一次性迁移器；dry-run、私有 backup、apply、最终 API 重读和故障整批恢复。
- 只迁移 Plan 1 preflight manifest 中当前 workspace 的文件；46 是设计时基线而非硬编码数量，
  未经列入不碰其他路径。
- 统一 `format_version`，删除旧 readers、migration branches、旧 fixtures 和迁移器。
- 合并 per-topic Agent Notes 到单一 `agent_notes.md`，删除 topic/type 写入与长期 migration API。
- 删除 tests-only `LayeredMemory`；实现 Retrieval 单次查询 snapshot，不做跨查询 cache。

### Plan 4：核心协调器收敛

- 先固定 ToolExecutor 的 validation/approval/execution/effect/failure 行为矩阵，再提取真实阶段。
- ToolExecutor 全绿并独立提交后，固定 AgentLoop preflight/attempt/action/finalize 行为矩阵。
- 保持 Action 唯一路径、tool pair、usage、terminalization、redaction 和 primary exception 顺序。
- 只整理这两个改动直接触及的 test setup；安全 corpus 不以压缩为目标。

### Plan 5：构建、平台、当前文档与最终收敛

- 收紧 sdist，增加 wheel clean-install smoke；不补发布型 metadata。
- 增加 macOS focused CI 并消除 fork warnings。
- 写当前 README/CONTEXT/architecture/security/recovery/verification/memory 文档与两个 ADR。
- 删除已跟踪历史 docs、SDD、Review Pack 和 result artifacts，保护所有 untracked 文件。
- 增加精确 repository-structure audit；运行完整验证矩阵、最终独立 code review 和经授权的单一
  Provider E2E。

每份计划内部允许多个小提交，但每项只 stage allowlist 中的文件。计划之间的回滚边界不得合并；
真实 GitHub CI 结果必须在对应 push 后记录，不能用本地推断代替。

## 14. 结构检查

新增 `tests/test_repository_structure.py`，精确检查：

- `providers.clients`、`evaluation.metrics`、`metrics_experiments`、`evaluator` 不存在。
- `pyproject.toml` 只有 `pico` console script。
- 生产、tests、scripts、benchmarks 的旧 imports 为零。
- 已批准删除的 `complete_v2`、`build_v2`、`FallbackAdapter`、旧 migration functions 为零。
- 已批准重命名的 benchmark 文件和 artifact type 不存在。
- 当前代码与文档无 `pico-cli`。
- 活动 Store 只读写 `format_version`，无 `schema_version` reader。
- runtime 无 migration/deprecated alias。
- `prompt_cache` feature flag、`LayeredMemory`、per-topic Agent Notes writer 和 bare-prompt dispatch 不存在。
- `git ls-files` 中 docs/superpowers、.superpowers/sdd、benchmark results 和旧 review pack 归零。

规则只使用批准的精确 symbol/path/token manifest，不泛化禁止 `_v1/_v2/_v3/phase1`、数字、
版本化 Provider URL、`compatible` 或真实错误 fallback。测试自己的 manifest 数据不参与源码扫描，
避免自命中。

## 15. 验证门

每个任务先跑 focused tests、touched-path Ruff、`git diff --check`。每份计划结束运行：

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

Plan 1 额外重跑 Memory CLI canary，必须拒绝且输出不含 canary；验证两个 worktree 的 `.env`
path 不同且不回退。Plan 3 必须对 migration manifest、backup hashes、最终全量重读和 query
snapshot read-count/parity 给出证据。Plan 5 必须记录 Ubuntu、macOS、wheel archive 与 clean-install
结果。

Provider/Context 接口改名属于真实 wire path 变化；全部本地门通过后，另行取得一次明确授权，
执行一个真实 Provider E2E。不得自动沿用旧授权或运行 Provider matrix。

## 16. 实施完成后的合规审计

实现者不能只报告测试绿色，必须逐条填写以下矩阵并给出命令或文件证据：

| 规划项 | 完成证据 |
| --- | --- |
| Memory CLI symlink/hardlink/FIFO/inode-swap 已关闭 | focused security tests + canary reproduction |
| ToolExecutor/AgentLoop 生命周期已收敛且复杂度未增长 | symbol/complexity report + behavior matrix |
| `.env` 精确路径可见且不跨 worktree | config/doctor text+JSON tests + two-worktree test |
| sdist、wheel smoke 完成且发布型 metadata 延后 | wheel METADATA + archive listing + clean install |
| uv.lock/frozen CI 完成 | tracked lock + CI command |
| `main`/`memory` push CI、macOS focused CI 与 fork warning 完成 | workflow triggers + CI run + warning-free test |
| TOML 只使用 tomllib | structural scan + malformed-config tests |
| Retrieval 单次 snapshot 每文件最多读一次且结果不变 | read-count + next-query freshness + parity tests |
| Memory 只有单一 Agent Notes 写入模型 | tool schema + structural scan + migration evidence |
| dead prompt-cache flag 与 LayeredMemory 删除 | symbol/import absence + Provider cache/memory helper tests |
| 历史 tracked 文档和证据资产删除，untracked 未动 | `git ls-files` absence + staged allowlist + status snapshot |
| Provider/evaluation 聚合层删除 | module absence + import scan |
| 唯一 CLI 为 pico，bare prompt 已删除 | pyproject + CLI smoke + usage scan |
| 批准清单中的旧命名消失且合法版本文本未被误伤 | exact manifest structure test |
| 旧持久化数据完成一次迁移，runtime 无迁移器 | manifest、backup/restore test、source scan |
| 顶层 API 边界符合批准清单 | import contract test |
| 当前文档链接、命令和术语一致 | link/path/command scan |
| 最终实现重新完成独立 review | final HEAD review report；不得复用基线 C0/I0/M0 |
| 全量测试、benchmark、build 与真实 E2E 达标 | final verification summary |

任何一项没有证据即视为 spec 未完成；不能以“已在其他测试间接覆盖”代替明确核对。被设计明确
排除的发布型 metadata、License、跨查询 cache 和 Provider matrix 标为 `deferred by design`，
不得伪报完成。

## 17. 非目标

- 新 Provider、registry、gateway、plugin framework。
- 数据库、向量索引、持久化 retrieval cache。
- 进程内跨查询 retrieval cache 或第二套 metadata scanner。
- 并行工具、多 agent runtime、UI、TUI。
- Windows 支持或 OS sandbox 重设计。
- 为 LOC/复杂度数字删除安全 case。
- 自动读取父目录或兄弟 worktree 配置。
- 未经授权的真实 API 调用。
- 公开发布，或提前填写 authors、project URLs、classifiers、License。
- docs archive；Git history 是唯一历史档案。

## 18. 完成定义

只有以下条件同时成立才完成：唯一 `pico`；唯一结构化 Provider 请求接口；唯一 Canonical
Messages；唯一 Agent Notes 写入模型；所有 versioned record families 只认识当前 `format_version`；聚合层、迁移器、
deprecated alias 和批准清单中的旧命名消失；dead flag 与 tests-only memory facade 删除；历史
tracked 文档/证据删除且 untracked 原样保留；当前 Pico 数据完成迁移并验证；Memory CLI 外部读取
漏洞关闭；`.env` 精确来源可见；Retrieval 单次 snapshot 正确；Ruff、全量 tests、offline live、
benchmarks、build、clean install、Ubuntu/macOS CI 和经授权的单一 Provider E2E 全部通过；五份计划
各有独立提交与回滚边界；第 16 节合规矩阵逐项有证据。
