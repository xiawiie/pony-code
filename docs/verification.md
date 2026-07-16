# Pico 验证与证据

本文给出最终 HEAD 可重建的离线门禁。命令默认从仓库根目录执行，临时 benchmark/build 输出不提交。

## 本地全量

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
```

复杂度 ratchet 使用：

```bash
uv run ruff check pico --select C901 --output-format json
```

目标是 `ToolExecutor.execute` 与 `AgentLoop.run` 均无 C901 finding，并保持仓库与文件级基线不回退。

## 构建与 clean install

```bash
uv build --clear
uv run python scripts/verify_distribution.py --install-smoke --offline-bundle-smoke
```

verifier 用 `git ls-files pico` 冻结 package 清单，并拒绝任何未跟踪的 `pico/**/*.py` 或内置 package data，
精确检查 sdist/wheel roots、wheel METADATA、唯一 console entry 与零 runtime dependency；随后在临时
HOME/cwd/venv 中 `pip --no-deps` 安装 wheel，检查
`command -v pico`、Docker package resources、`pico sandbox status/prepare`零网络/零mutation、resource identity
不变、`pico --help`和`pico doctor --offline`。没有exact local image时`prepare`必须fail closed且仍为零写入。

`--offline-bundle-smoke`与当前SRT package data只在registry production vertical通过前保留为遗留检查，不是
产品接口。vertical通过后必须从Git index、wheel/sdist、CI和verifier一并删除；当前exact verifier仍会拒绝任何
未跟踪package data，不能用built wheel存在资源替代Git authority。

## 平台门禁

GitHub Actions 在 push 到 `main`、`memory` 和所有 pull request 上运行：

- Ubuntu latest / Python 3.11：lint、全量 pytest、offline live assertions；
- Ubuntu latest / Python 3.12 / pull request：独立全量 pytest、`core-fast`、`sandbox-contract`、offline live
  assertions、build/clean-install/offline-bundle smoke，并生成和上传 report-only sandbox performance artifact；
- Ubuntu latest / Python 3.12 / push：`core-functional`、offline live assertions，并生成和上传 report-only
  sandbox performance artifact；build/clean-install/offline-bundle smoke 由 `core-functional` 编排；
- macOS latest / Python 3.12：project env、文件锁/私有路径/artifact、safe subprocess/shell、recovery
  durability、D2-D5 Docker Session/Runner/Runtime/CLI/Apply contracts与Memory focused tests；随后以临时HOME
  验证Docker status/prepare零mutation和local authorization fail-closed边界；
- Ubuntu latest 独立上传report-only Docker status，并运行相同local identity gate。

当前workflow不pull镜像、不启动Sandbox container，也不把status/blocked-path contracts命名为real gate；这些
jobs证明D6 CLI与local fail-closed边界，不构成D7四目标evidence。

ADR-0040接受的Docker证据分三层：

1. D1只在standalone临时fixture上跑本机exact Docker/image/policy、staging、mount/network/process/resource/
   cleanup、diff/discard和fixture apply corpus，不接正式`--sandbox`。完整strict artifact才能产生Sandbox
   Feasibility Approval，且只允许D2-D6实现。
2. D2-D5实现production owners；D6从clean wheel经独立release harness调用这些owner运行D7 Corpus V2
   mandatory vertical corpus并hard-cut SRT。D1-v1的34个check ID只保留历史意义；V2必须把完整case matrix、
   正控、external fixture、fault、真实行为probe和全部host/guest判词绑定新identity，不能为兼容旧ID折叠证据；
   fixture不能充当production证据；缺少Candidate/Product授权的distributed入口仍fail closed，ADR-0042的
   exact local authorization可独立解锁当前本机public `--sandbox`。
3. D7在macOS Desktop arm64/x86_64与Linux rootless amd64/arm64各跑3次clean mandatory和20次soak。
   同一universal wheel绑定同时包含`linux/arm64`与`linux/amd64`记录的canonical image-set v2，各host只选择对应
   OCI record。release controller注入signed expected-input manifest；可信聚合器strict schema、
   duplicate-key/unknown-field reject、bounded regular-file/no-follow读取，并拒绝missing/unexpected/duplicate、
   混跑、重放和artifact自报归属。production worker把expected digest/release nonce/job/commit/sdist/run index写入
   exact `release_binding`；D6 unbound artifact不能进入D7。mandatory pytest出现skip、xfail或xpass均拒绝。
   92-job aggregate始终`product_enablement=false`，只作为candidate签发输入。

production aggregate后controller先签发未发布、job-nonce scoped的candidate attestation，绑定exact wheel SHA、
installed-distribution digest、image、policy、corpus和aggregate，只注入四平台public CLI release smoke且不供
`prepare`下载。smoke只通过`PICO_SANDBOX_CANDIDATE_ATTESTATION`与`PICO_SANDBOX_CANDIDATE_NONCE`注入；四个
artifact都必须是`product_cache_written=false`与`product_enablement=false`。smoke进入expected matrix并全部通过
后，controller才在wheel/sdist之外签发正式detached Sandbox Product Enablement。wheel不得内嵌对自身SHA的
批准。ADR-0041冻结未来distributed attestation channel与cache路径，但ADR-0042下当前`prepare`不下载、pull或
缓存任何release artifact；恢复该distributed流程需要新的实现与release复核。workflow定义本身不是证据。

release authority已经冻结RSA-PSS-SHA256（3072-bit、e=65537、32-byte salt）、canonical ASCII JSON/domain、
immutable in-wheel public key map、rotation/revocation/expiry/rollback，以及禁用proxy、HTTPS redirect allowlist、
256 KiB上限的stable GitHub Releases channel。runtime只重算installed-distribution与canonical image-set、
核对内置policy constant，并把image-set内的packaged corpus claim与签名provenance对齐；corpus不能从普通wheel
runtime的mandatory check IDs重算。
wheel/sdist/commit/expected manifest/aggregate与corpus是controller签名前核验、再由签名认证的provenance
claims，普通安装目录不能反推出原wheel SHA或mandatory corpus。

macOS FIFO 探针显式使用 spawn，并以 `-W error::DeprecationWarning` 运行两个真实 FIFO 参数用例；不使用
warning filter 或平台 blanket skip。最终证据必须引用 exact HEAD 的实际 CI run，不能由本地结果推断。

### 当前本机 MVP 与发布 NO-GO

- 2026-07-15阶段14/15最终本机收口：Sandbox focused `362 passed`、closure focused `42 passed`、全量
  `3034 passed, 2 skipped, 0 failed`；Ruff、`uv lock --check`、build和`git diff --check`通过。真实Docker
  `status/prepare`均为`ready`、`runtime_authorization.kind=local`、network/mutation=false；最小真实容器执行
  `host_fallback_count=0`、residue=0，Source identity不变，guest写入只存在于staging并在discard后删除。
  全量与最终Docker回归均隔离HOME；真实`~/.pico`最终快照前后metadata/content digest、Sandbox control-dir
  count `2595`和Sandbox root mtime `1784098288`不变。
- Docker historical D1-v1本机34项mandatory corpus已通过并签发只允许D2-D6实现的Feasibility Approval；它
  已完成该用途，不证明当前D7 Corpus V2。本机MVP另由ADR-0042的exact local authorization解锁。
  D2-D5主线程实现、focused独立Review与回归已完成，但跨平台real gate待补。D6 clean-wheel production harness已接入
  统一evaluate入口，但exact Git distribution gate也未完成。
- 当前image-set v2只有`linux/arm64`本地记录且`registry_reference`为空，`linux/amd64`记录不存在；本机arm64
  already-present exact image可进入MVP，clean-wheel registry vertical仍在启动target前精确fail closed。
- wheel内production trust-root map为空，production public key/KMS signing authority不存在；已冻结的reader合同
  只能证明fail-closed，不能签发、缓存或接受正式Product Enablement。
- 四平台92个production artifacts与4个candidate-smoke artifacts均未生成，trusted aggregates和detached Product
  Enablement不存在。Linux performance baseline与跨平台D7 real gate也尚未完成。
- SRT `0.0.65` 已被Linux future-name `.env*`和macOS DNS事实拒绝，ADR-0040已supersede该路线。这是历史
  rejection evidence，不可替代Docker D1/D7，也不应继续等待“新SRT candidate”。该rejection未执行target，
  `host_fallback_count=null`。
- committed core baseline 当前只有 `darwin-arm64` machine class；没有同 scenario、同 machine class 的 Linux
  baseline。Ubuntu push 使用不含性能比较的 `core-functional`，继续运行完整功能/build/distribution编排；
  `core-full` 仍会在启动任何 runner 前拒绝 machine-class mismatch，只能在有同 machine baseline 的环境运行。
- production owners已有本机fixture/contract与部分真实Docker smoke，但不能把arm64本地结果或contract test
  拆分成任何平台的GA结论。
- D7 strict expected-matrix aggregator与worker binding合同已实现；真实92-job artifacts尚未生成，不能把contract
  tests或空aggregate当成平台证据。
- 最终build artifacts为sdist `ad46e9e3c4fa955d37d14c001bd87776a2d87054689ddc15a51e23b17ca415e0`、
  wheel `16909bceb879e87377eb5dd7a233fd31c5e1aed7cb40a3c9fb51f1a97f559f88`。真实Git index因5个未跟踪
  package-data文件按设计fail closed；不改变真实staging area的临时intended index已通过exact sdist/wheel、
  clean-install、status/prepare、doctor和offline-bundle smoke。只有把完整intended source纳入Git authority后，
  real-index distribution才可转为GO。

## 统一评测入口

```bash
uv run python scripts/evaluate.py --suite core-fast
uv run python scripts/evaluate.py --suite core-functional
uv run python scripts/evaluate.py --suite core-full
uv run python scripts/evaluate.py --suite sandbox-contract
uv run python scripts/evaluate.py --suite sandbox-real
uv run python scripts/evaluate.py --suite live --provider deepseek
```

逻辑 suite 固定为六个：

| Suite | 编排内容与使用边界 |
|---|---|
| `core-fast` | Ruff、Context budget/snapshot 和 Tool/shell security focused contracts；用于 PR 快速门禁。 |
| `core-functional` | 与 `core-full` 相同的完整功能、build和distribution编排，但不运行性能比较；用于缺少同 machine baseline 的 CI。 |
| `core-full` | 在 `core-functional` 上增加选定 perf runner；只用于具备同 machine committed baseline 的 merge/release 性能门禁。 |
| `sandbox-contract` | 编排D2-D5 production Session/Runner/Runtime/CLI/Apply与public API contracts，并拒绝skip/xfail/xpass；用于PR门禁，但不冒充真实Docker运行证据。 |
| `sandbox-real` | 先clean build wheel，再在隔离venv中以`--no-index --no-deps`安装该wheel，由production owners执行已冻结的15-case D7 Corpus V2合同。test-only chain已同步当前合同，但packaged digest、image-set、四平台校准与真实artifacts仍未发布，因此不构成D7 evidence。 |
| `live` | 只接受显式 Provider，验证 exact-HEAD live report v2；会产生网络请求和费用，不属于普通 PR gate。 |

`core` 保留为 `core-full` 的兼容别名；`sandbox` 保留为先运行 `sandbox-contract`，再build wheel并运行
`sandbox-real` 的兼容别名。它们不是额外的逻辑 suite。当前 CI 不调用 `sandbox-real`；D1仍只使用独立fixture，
D6统一入口已hard-cut到Docker production owners，D7只聚合该production path证据。需要nested-mount正向fixture
的平台必须通过`PICO_SANDBOX_MOUNT_FIXTURE`显式提供；character/block device则通过
`PICO_SANDBOX_DEVICE_FIXTURE`提供。两类fixture都必须携带release job预先绑定的creator/facts identity，缺失、
形状不精确或只触发同名error均不能skip或通过。

每次运行只写 `artifacts/eval/<timestamp>-<suite>.json/.md`。聚合产物只保留稳定 scenario ID、状态、
exit code、耗时、低敏感 provenance、相对 artifact path，以及选定 perf 的 median/p95 和 committed median；
不保存命令 stdout/stderr、prompt、completion、tool result 或绝对路径。`core-full` 在启动 runner 前要求
baseline 的 machine class 与本次运行完全一致。性能首次出现 process median 同时超过 baseline 2 倍且绝对
增加超过 5 ms 时，只对该 perf process 做一次确认重跑；最终以确认结果判定，并记录
`confirmation_run=true`。p95 只报告。Baseline 只能在同一 PR 有代码变更并解释性能变化时更新。

## 离线 benchmarks

Memory quality 使用 Fake Provider，workspace 写到临时目录：

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format text
```

性能 harness 分项运行，输出只保存在终端或临时目录：

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

## 真实 Provider 授权边界

真实 E2E 会产生网络请求、token 消耗和费用，必须取得本轮明确授权并设置请求、attempt、token 与 wall-time
上限。2026-07-16 的授权覆盖上述三 Provider 串行矩阵；完成两个独立全绿结果后已停止继续产生模型费用。

Live report format v2 只记录 Provider/model、base Git HEAD、固定 caps、每 turn 的行为标签与计数、
assertion name/gate/boolean、usage totals、墙钟时间和固定错误码。不得记录 prompt、answer、raw error、
assertion raw actual、key、header、request URL 或 response body。fixture 退出并验证恢复后才能写最终报告。
当前格式不绑定 dirty diff；作为 release evidence 时必须在 clean commit 上重跑，或另行设计并评审受控的
source-tree digest，不能把 `git_head` 误称为 dirty worktree 的 exact code identity。

四个 gate 必须独立展示：Behavior、Transport/Cost、Credential/Artifact Security、Persistence/Fixture。
只有四者均为 pass 才能称为“全量通过”。Transport 行应显示 `model attempts N (cap 15)`、HTTP attempts 与
retries；cap 是上限，不是通过分母。retry 或 billing ambiguity 为 degraded；证据/usage 缺失或 cap 超限
为 fail，两者都使 `overall_pass=false`。

Live CLI 使用 `--max-model-attempts`、`--request-timeout-seconds` 和 `--max-wall-seconds`。前者限制逻辑
Model Attempt，request timeout 作用于单个 HTTP 请求，wall cap 只在 turn 边界观测。Ollama 只有 `/api/tags`
可达且配置模型已安装时才进入 live；否则为 `not_configured`，不启动服务、不拉取模型、也不发送生成请求。

## 历史基线证据

硬切前源码基线 `5f359bd18fb3a59968167bfe0196352d41a23a01` 的可重建结果是：本地
`1997 passed, 6 skipped`，offline assertions `60 passed`；wheel/sdist 可构建但 sdist 携带完整 tests，
macOS 全量有两条后台线程 `fork()` warning。此前单次获授权 DeepSeek E2E 为 `43/43` assertions、
`10/15` Provider calls、13,842 input tokens、1,330 output tokens、5,248 cache-read tokens、44.253 秒；
该授权与结果不用于最终 E2E。

上一阶段实现证据 commit `ffc5a60ce91885038264c0cfc4185e13c66a19a3`（不代表当前 Provider v2）：

- 本地 Python 3.12：Ruff 通过，`2021 passed, 6 skipped`，offline assertions `66 passed`；
- macOS warning-as-error focused：显式 FIFO `2 passed`，完整 focused `453 passed`；
- Memory quality Fake benchmark：`8/8`；四组 perf harness 均成功；
- C901：全仓 60 个 finding；`ToolExecutor.execute` 与 `AgentLoop.run` 均无 finding；
- wheel/sdist 精确归档检查、METADATA、零 runtime dependency、隔离 venv 安装、CLI/doctor smoke 全部通过；
- GitHub Actions run [29167571366](https://github.com/xiawiie/pico/actions/runs/29167571366)：Ubuntu
  Python 3.11/3.12 均为 `2021 passed, 6 skipped` 且 offline `66 passed`，Python 3.12 build/clean-install
  成功；macOS Python 3.12 为 FIFO `2 passed` 与 focused `453 passed`。

每次新交付仍必须给出交付 commit 的 exact-SHA CI run 和独立 review 结论。真实 Provider live 证据不得从
历史 run 推断；必须在当前离线门禁、build 和 review 通过后取得新的明确授权，并对目标 exact SHA 单独运行。
