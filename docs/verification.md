# Pico v0.2.0 验证与发布证据

本文定义本地稳定版最终 exact HEAD 的验证真源。历史 commit、历史 Provider live、旧 benchmark 或 workflow
定义本身都不是当前证据。所有命令默认从干净仓库根目录运行，临时 build/benchmark artifact 不提交。

## 必跑命令

```bash
uv lock --check
uv run ruff check .
uv run pytest -q
uv build --clear
uv run python scripts/verify_distribution.py --install-smoke
uv run python scripts/evaluate.py --suite core-fast
uv run python scripts/evaluate.py --suite core-functional
uv run python scripts/evaluate.py --suite sandbox-contract
uv run python scripts/evaluate.py --suite sandbox-real
uv run python -m benchmarks.perf.bench_sandbox
uv run python -m benchmarks.perf.bench_sandbox --real
git diff --check
```

verifier 用 `git ls-files pico` 冻结 package 清单，并拒绝任何未跟踪的 `pico/**/*.py` 或内置 package data，
精确检查 sdist/wheel roots、wheel METADATA、唯一 console entry 与零 runtime dependency；随后在临时
HOME/cwd/venv 中 `pip --no-deps` 安装 wheel，检查 `command -v pico`、Docker package resources、
`pico sandbox status/prepare` 零网络/零 mutation、resource identity 不变、`pico --help` 和离线 `pico doctor`。
没有 exact local image 时 `prepare` 必须 fail closed 且仍为零写入。

在 Darwin 上运行 `sandbox-real` 前，必须显式设置：

```bash
PICO_SANDBOX_MOUNT_FIXTURE=/path/to/source-with-mounted-child \
PICO_SANDBOX_DEVICE_FIXTURE=/dev \
uv run python scripts/evaluate.py --suite sandbox-real
```

mount fixture 的 Source tree 必须包含 `st_dev` 不同的已挂载子目录；device fixture 必须包含 character 或 block
device。评估入口不会隐式创建这类宿主资源，也不会把缺失 fixture 记为通过。

发布标准是零失败、零未解释 skip/xfail/xpass。删除 legacy SRT tests 后测试总数下降是预期行为；每次发布在最终
artifact 中冻结 exact HEAD 的实际 count，不能用旧数量作为目标。

## Distribution gate

`uv build --clear` 后 `dist/` 必须且只能存在一个与 `pyproject.toml` 版本匹配的 wheel，以及一个匹配的 sdist。
verifier 从 Git index 和 `pyproject.toml` runtime package 真源计算 archive 清单，检查：

- wheel/sdist roots、METADATA、唯一 `pico` console entry、零 runtime dependency；
- package manifest 和 Docker config resource 的 exact 内容；
- wheel 不包含 `pico.sandbox*` legacy runtime、`pico._sandbox_toolchain` 或 `pico.evaluation`；
- 隔离 venv、隔离 HOME、空 cwd、无源码 `PYTHONPATH` 的 `pip --no-deps` clean-install；
- installed `pico --help`、`doctor`、`sandbox status/prepare` 的只读、零网络/零隐式修复合同。

没有 exact local image、宿主不是 macOS arm64或 Docker 不满足要求时，inspection/smoke 必须以稳定 reason code
fail closed；不能 pull image、修改用户 Docker config 或把 Host fallback 当成通过。

### DeepSeek-first CLI 硬切证据

- 2026-07-16 Anthropic 主路径 exact-worktree 离线全量为 `3099 passed, 2 skipped`；`uv lock --check`、全仓 Ruff、
  `git diff --check`、build、clean-install 与 offline-bundle verifier 全部通过。
- `core-fast`、`sandbox-contract` 与 report-only sandbox performance runner 通过；CI workflow YAML 可独立解析。
- 原生双轮工具 E2E 覆盖 Anthropic Messages、OpenAI Responses、OpenAI Chat Completions 与 Ollama Chat，场景包括
  read success、write denial 与 tool error；Anthropic thinking state 保存/原样回放也在 AgentLoop 闭环中验证。
- DeepSeek 官方 `/anthropic/v1/messages` 与 Lumina `/v1/messages` 具有离线 wire contract。目标 worktree 当前没有
  `.env`，且用户在对话中提供的 Key 已视为暴露凭据，因此未执行 `pico doctor --check-api`，不宣称新的官方或
  Lumina live 证据。live gate 需要轮换后由用户通过隐藏输入写入本地的新 Key。
- 旧 Provider/Profile/Connection 变量和 CLI 参数在生产代码、示例与活跃用户文档中无匹配；仅保留明确的拒绝/
  不激活回归测试。

## 统一评测入口

| Suite | 内容与边界 |
| --- | --- |
| `core-fast` | Ruff、Context snapshot/budget、Tool/shell security focused contracts。 |
| `core-functional` | 完整功能、build 和 distribution 编排，不比较 machine-specific baseline。 |
| `core-full` | 在 functional 上增加同 machine committed performance comparison。 |
| `sandbox-contract` | Session/Runner/Runtime/CLI/Apply contracts；不冒充真实 Docker execution。 |
| `sandbox-real` | clean wheel + isolated install + production Docker owner 的真实 vertical。 |
| `live` | exact-HEAD Provider live；显式 Provider、网络和费用授权必需。 |

`core` 仅是 `core-full` alias；`sandbox` 仅按顺序编排 contract 与 real，不是第二套实现。每次 suite 输出到
`artifacts/eval/<timestamp>-<suite>.json/.md`，只保留稳定 scenario ID、status、exit code、duration、低敏感
provenance 和性能统计；不得保存 prompt、completion、tool result、credential 或 Host 绝对路径。

这里的 `deepseek` 仅是内部 benchmark target 名称；它使用与公开 CLI 相同的固定
`deepseek-v4-flash` + Anthropic Messages 路径和 `PICO_API_URL` / `PICO_DEEPSEEK_API_KEY`。其他内部 client
target 不会进入公开 CLI 配置。

## 必测安全场景

| 领域 | 必须验证 |
| --- | --- |
| Memory | 无授权、否定句、历史授权、delegate、显式中英授权、secret rejection、零副作用 |
| Context | 多段 source、伪 marker、drop priority、retry reuse、Provider payload exact match、闭合标签 |
| Workspace I/O | symlink/hardlink/FIFO/device、父目录/root/target交换、超限、Patch CAS、外部零读写 |
| Provider | official 默认、任意显式第三方、来源优先级、无凭证泄漏、禁止 redirect |
| Staging | 128 MiB、5000 文件、跨 chunk secret、env template 超限、中途变化、mode/目录交换、清理 |
| Capture | no-op、新增/删除/chmod、mtime恢复、inode更换、resume、cache失效、final full capture |
| Watchdog | 长/短命令、快速超限、特殊文件、最终 scan、container cleanup |
| Apply | 展示与使用同一 digest、确认后 source/staging 漂移、rollback/reconcile、`--yes` |
| Packaging | 无 legacy SRT、无 runtime evaluation、CLI/doctor/status/prepare、零 runtime dependency |

Workspace TOCTOU tests 必须在最终 open 前交换 file、parent 和 root，断言 workspace 外 canary 零读取、零写入、
runner 不执行且 reason code 稳定。Incremental capture tests 必须证明无法安全复用时自动回全量，最终完整 capture
仍能发现调用期 cache 未观察到的持久变化。

## 性能协议

- 普通场景：5 次 warmup、20 次 measured，报告 median/p95。
- 5000 文件和 128 MiB：1 次 warmup、5 次 measured。
- 固定同一机器、Python、Docker、exact image 和代表性仓库。
- artifact 记录 exact commit、dirty state、机器、OS、Python、Docker、image digest 和参数。
- 首次超限只可在确认机器空闲后完整重跑一次；第二次仍超限即 `NO-GO`，不能修改阈值掩盖回退。

本机回归阈值：

| 场景 | 门槛 |
| --- | ---: |
| 128 MiB staging Python 额外峰值 | ≤ 32 MiB |
| 5000×1 KiB staging median | ≤ 6.34 s |
| 326 文件 no-op shell observed median | ≤ 1.40 s |
| 326 文件 no-op shell observed p95 | ≤ 1.60 s |
| 5000 文件 watchdog | ≤ 单核 10% |
| watchdog 违规检测 | ≤ 2 s |
| status/prepare/context/baseline/finalize/apply | 各自 median 回退 < 10% |

这些是同机基线，不是跨机器 SLA。`bench_sandbox` 的非 real 模式不能代替真实 container vertical。

```bash
uv run python -m benchmarks.perf.bench_request_build
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
uv run python -m benchmarks.perf.bench_session_context
uv run python -m benchmarks.perf.bench_security_recovery
uv run python -m benchmarks.perf.bench_sandbox
```

Memory fake suite 当前包含 33 个场景，并按 category 单独报告中文、paraphrase、conflicting/stale fact、
deletion、long notes、prompt injection、false recall、cross-scope、multi-hop 与 explicit write。它验证本地
retrieval/tool contracts，不替代真实模型质量。2026-07-16 已在当前 dirty worktree 上取得两个 Provider 的
全绿 live 结果；但报告只绑定 base commit、不能重建未提交 diff，因此仍不是 clean exact-SHA release evidence。

当前 Memory/Context/Session 重构的本机最终证据为：全量 `3062 passed, 2 skipped`；Ruff、compileall 与
`git diff --check` 通过；Memory fake quality `33/33`；200-turn、50-tool-exchange、两次 compaction、branch、
rewind 和 resume 的长会话合同通过。该证据验证实现与确定性管线，不替代真实 Provider 语义质量。

`bench_session_context` 是 10,000-entry report-only 门禁，目标为：冷加载 median ≤250 ms、warm append p95
≤20 ms、compaction 本地规划 p95 ≤100 ms、compacted warm request build p95 ≤50 ms。`bench_recall` 额外报告
512-note 单 snapshot 的 p95 ≤103 ms、相对 double-scan reference median 至少降低 20%，并记录每 turn scan
count=1。绝对墙钟值应在同一机器、同一 corpus 下比较。

本轮同机同 corpus 实测：10,000-entry cold load median `226.56 ms`、append p95 `2.93 ms`、compaction 本地
规划 p95 `36.64 ms`、compacted warm request build p95 `14.05 ms`；512-note shared snapshot median/p95
`27.78/28.25 ms`，相对 double-scan median 改善 `35.58%`。这些是本机 report-only 数值，不外推到其他机器。

## 真实 Provider 矩阵（2026-07-16）

用户明确授权使用外部私有 env 文件中的 API 配置。测试只通过 Pico 的 bounded/no-follow env reader读取该
`0600` regular file，并把选定值注入 live 子进程内存；没有复制或输出 key。当前 uv Python 的
默认 CA store 不能验证目标链，而系统 curl/OpenSSL 可以；live 子进程使用 `certifi.where()` 作为
`SSL_CERT_FILE`，仍保持完整 TLS 验证，没有使用 unverified context。

| Provider / model | Assertions | Gates | HTTP calls / retry | Input | Output | Cache read | Wall |
|---|---:|---|---:|---:|---:|---:|---:|
| DeepSeek / `qwen3.7-max` | 46/46 | B/T/S/P pass | 10 / 0 | 71,805 | 2,619 | 8,947 | 73.4s |
| Anthropic / `claude-sonnet-4-6` | 41/46 | Behavior fail；T/S/P pass | 9 / 0 | 6,292 | 2,117 | 52,914 | 90.0s |
| OpenAI / `gpt-5.4` | 46/46 | B/T/S/P pass | 10 / 0 | 99,095 | 6,817 | 0 | 165.5s |

DeepSeek 与 OpenAI 的报告均通过 `load_live_report()` strict schema reader 和统一 release evaluator：

- `benchmarks/live_e2e/results/live-e2e-1784167896030218000.json`
- `benchmarks/live_e2e/results/live-e2e-1784168594698921000.json`

Anthropic 的五项失败全部来自 turn 2：模型收到指定 fixture prompt，却改为分页读取 `pico/runtime.py`，没有走
大结果 digest 路径。其 Memory recall、Context allocator、compaction、transport、artifact security、fixture
restore 均通过。这个结果保留为模型/网关工具路径服从性差异，不由 Pico 偷换 tool input 来制造通过。

三份真实 JSONL Session 的 canonical/active 分离如下：

| Provider | Entries | Canonical messages | Active messages | Compaction before | Summary | Tail | Ratio | Checkpoints |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DeepSeek | 108 | 104 | 21 | 27,261 | 1,618 | 3,771 | 0.199 | 5 |
| Anthropic | 107 | 102 | 21 | 30,146 | 1,081 | 3,771 | 0.162 | 5 |
| OpenAI | 107 | 102 | 21 | 29,717 | 2,863 | 3,771 | 0.224 | 5 |

三个 Session 都只有一次 `budget_exceeded` compaction，并保留 `first_kept_entry_id`；磁盘原始消息没有删除。
OpenAI Session 的隔离副本已通过 `Pico.from_session()`：exact worktree identity、2,863-token summary、21 条
active messages和5个 checkpoints 均恢复，canonical message count不变，只追加一个 resume control entry。

真实运行还暴露并关闭了三个共享链路缺陷：Trace projector 丢失 transport evidence；Report v3 terminal reader
读取旧层级；ToolExecutor 的 legacy 4,000-character clip 使 4,096-token digest 在生产链路不可达。另修复了
live harness 的当前 raw reference parser 与 dangling-symlink fixture cleanup。相关 focused tests 为
`205 passed`。

证据边界：每家只有 5 个真实用户 turn；80 条历史是 deterministic fixture，并只触发一次真实 summary调用。
200-turn/50-tool/two-compaction/branch/rewind 仍是离线合同测试。报告记录 `git_head=500bc9e...`，但当前工作树
存在未提交重构差异且 report v2 没有 source-tree digest；所以这是 current dirty-worktree evidence，不是可由
base SHA 单独重建的 release evidence。Provider usage/cache 字段的计费语义也不完全一致，不能直接横向推导价格。

`bench_sandbox`只测Docker production image manifest加载、Session inspect，以及1/4/16路并行inventory，输出绑定
image/policy的未建立baseline结构化provenance。它不启动container，也不冒充D7真实运行；四平台mandatory/soak
evidence仍由release runner单独产生。

## GitHub Actions

Workflow 中所有 third-party actions 必须固定到 immutable commit SHA，并在同一行保留 tag 注释。当前 CI：

- Ubuntu Python 3.11/3.12：lint、全量/aggregate tests、offline live assertions、build/install 和 report-only perf；
- macOS Python 3.12：security/durability/Sandbox contracts，以及隔离 HOME 的 status/prepare 零 mutation gate；
- Ubuntu Docker status：只报告 unsupported/fail-closed capability，不构成 Linux release evidence。

CI 不 pull managed image，也不把 status、contract 或 Linux report-only job 称为 real Sandbox gate。最终交付必须引用
exact HEAD 的实际 CI run；本地结果不能推断远端通过。

## 真实 API 授权边界

真实 API E2E 会产生网络请求、token 消耗和费用。公开入口只有用户显式执行的
`pico doctor --check-api`；它验证最小文本、工具调用和 tool result 续接。维护者运行内部 live benchmark 前仍需
取得明确授权；普通 pytest 和 distribution smoke 不得联网。

真实 E2E 会产生网络请求、token 消耗和费用，必须取得本轮明确授权并设置请求、attempt、token 与 wall-time
上限。2026-07-16 的授权覆盖上述三 Provider 串行矩阵；完成两个独立全绿结果后已停止继续产生模型费用。

Live report format v2 只记录 Provider/model、base Git HEAD、固定 caps、每 turn 的行为标签与计数、
assertion name/gate/boolean、usage totals、墙钟时间和固定错误码。不得记录 prompt、answer、raw error、
assertion raw actual、key、header、request URL 或 response body。fixture 退出并验证恢复后才能写最终报告。
当前格式不绑定 dirty diff；作为 release evidence 时必须在 clean commit 上重跑，或另行设计并评审受控的
source-tree digest，不能把 `git_head` 误称为 dirty worktree 的 exact code identity。

## Release Gates

| Gate | 必须满足 |
| --- | --- |
| G0 Authority | exact HEAD、干净 tracked tree、版本、CHANGELOG、文档真源一致 |
| G1 Static | lock、Ruff、diff-check 全绿 |
| G2 Functional | focused security、全量 pytest、offline assertions 全绿且无未解释 skip |
| G3 Distribution | wheel/sdist、archive contract、isolated clean-install 全绿 |
| G4 P0 Security | workspace escape=0、unauthorized memory write=0、snapshot exact、Provider route correct |
| G5 Sandbox Real | Apply 前 source 不变、host fallback=0、cleanup residue=0 |
| G6 Performance | 达到 staging memory、shell latency 和 watchdog 阈值 |
| G7 Provider Live | 默认 DeepSeek exact-HEAD live 全绿；其他 Provider 只按各自 exact-HEAD 证据声明 |
| G8 Documentation | README、安全、恢复、Provider、平台、迁移和 CLI help 与实现一致 |

内部 Live harness 使用 `--max-model-attempts`、`--request-timeout-seconds` 和 `--max-wall-seconds`。前者限制逻辑
Model Attempt，request timeout 作用于单个 HTTP 请求，wall cap 只在 turn 边界观测。它不是公开 Pico CLI 的
多模型入口。

任一 mandatory Gate 失败时，发布保持 `NO-GO`，不得创建 `v0.2.0` stable tag。

## Provider live 授权

DeepSeek/OpenAI/Anthropic live 会产生网络请求、token 消耗和费用。必须在最终离线门禁、build 和 review 完成后，
针对 exact HEAD 获得新的明确费用授权；旧授权不能复用。缺少 credential 或费用授权本身就是 G7 blocker，不得用
offline assertions 或其他 Provider 结果替代。

Live report 只记录 Provider/model、exact SHA、固定 caps、行为标签与计数、assertion boolean、usage totals、wall
time 和固定 error code。不得记录 prompt、answer、raw error、key、header、request URL 或 response body。Behavior、
Transport/Cost、Credential/Artifact Security、Persistence/Fixture 四组必须分别 pass；retry/billing ambiguity 是
degraded，证据缺失或 cap 超限是 fail。

Ollama 只有 loopback `/api/tags` 可达且目标模型已安装时才进入 live；不启动服务、不拉模型。OpenAI、Anthropic 和
Ollama 只有各自 exact-HEAD live 通过后才进入 stable 支持矩阵。
