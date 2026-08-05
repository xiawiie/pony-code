# Pony 1.0 验证与发布

发布证据只对 exact Git HEAD 有效。旧 commit、dirty worktree 或另一版本 wheel 的结果不能继承。本文件区分可重复的离线
产品门禁，以及会产生费用的 Provider live 验收。

## 一键离线门禁

```bash
./scripts/check.sh
```

该脚本要求起始 worktree clean，并依次验证 lock、单次 Ruff、单次全量 pytest（包含 offline live-harness assertions）、
独立 deterministic core-functional evaluation、单次离线 sdist/wheel build、单次 archive/clean-install verifier，最后复验
Git HEAD 未变化且 worktree 仍 clean。Evaluation 与构建产物都写入本轮临时目录并在退出时清理；任何一步失败都停止。

## 门禁矩阵

| Gate | 类型 | 必须结果 | 主要证据 |
| --- | --- | --- | --- |
| G0 Source | 离线 | clean exact HEAD、版本/lock/CHANGELOG 一致 | Git、repository structure tests |
| G1 Static | 离线 | Ruff 零错误 | `uv run ruff check .` |
| G2 Functional | 离线 | 全量 pytest 零失败，裸/显式 TUI 入口一致 | `uv run pytest -q` |
| G3 Security | 离线 | escape/secret/permission/mutation/legacy-reader 专项零失败 | security/durability test groups |
| G4 Evaluation | 离线 | deterministic core-functional 与 offline assertions 通过 | evaluation scripts + pytest |
| G5 Distribution | 离线 | sdist/wheel 精确内容、metadata、License | distribution verifier |
| G6 Clean install | 离线 | 新虚拟环境中 CLI/version/help/doctor 与 TUI import 通过 | install smoke |
| G8 Provider live | 条件/收费 | 每个声明可用的账号/模型完成最小 probe | `doctor --check-api` 或 bounded harness |
| G9 Documentation | 离线 | 路径、命令、Provider 表与实现一致 | structure/docs tests |

G0-G6 与 G9 是 package 发布 mandatory gate。G8 需要用户拥有的账号、Key 和费用授权，是部署/Provider 组合验收；
离线 wire-contract tests 必须始终通过，但不能冒充真实账号结果。

## 支持与兼容矩阵

| 维度 | 1.0 声明 | 发布证据 |
| --- | --- | --- |
| Python | 3.11、3.12 | Linux 全量测试；macOS 3.12 安全与耐久性专项 |
| OS | macOS、Linux | CI；Windows 不受支持且安全原语缺失时 fail closed |
| Anthropic Messages | 实现支持 | 离线 wire contract；每个账号/model 的 G8 单独验收 |
| OpenAI Responses | 实现支持 | 离线 wire contract；每个 endpoint/model 的 G8 单独验收 |
| OpenAI Chat Completions | 实现支持 | 离线 wire contract；每个 endpoint/model 的 G8 单独验收 |
| Ollama Chat | 实现支持 | 离线 wire contract；每个本地 model 的 G8 单独验收 |
| Execution | 受信仓库中的 Host | 无 OS sandbox；旧 Sandbox 只读 inspection，不可 resume |

“实现支持”不代表所有网关兼容。发布或部署结论必须写明 exact HEAD、Provider、protocol、endpoint 类别、model 和
G8 是否执行；不得把某一组合的 live 结果外推到其他组合。

## 聚焦测试

### Provider 与 `.env`

```bash
uv run pytest -q \
  tests/test_config.py \
  tests/test_cli_commands.py \
  tests/test_cli_diagnostics.py \
  tests/test_provider_clients.py \
  tests/test_provider_anthropic.py \
  tests/test_provider_openai_chat_completions.py \
  tests/test_provider_response.py \
  tests/test_provider_probe.py
```

必须覆盖三 Provider、四 Transport、项目环境优先级、Ollama 无 Key、非法组合 fail closed、API 路径、认证 header、
tool call/result continuation、stop reason 与 usage。

### 安全与耐久性

```bash
uv run pytest -q \
  tests/test_project_env_security.py \
  tests/test_private_paths.py \
  tests/test_workspace_io_security.py \
  tests/test_shell_execution_security.py \
  tests/test_shell_security_corpus.py \
  tests/test_secret_boundaries.py \
  tests/test_legacy_artifacts.py
```

macOS CI 额外以 `-W error::DeprecationWarning` 运行关键安全组，确保 spawn/subprocess 行为没有平台警告退化。

### Host mutation 与 legacy inspection

```bash
uv run pytest -q \
  tests/test_tool_executor_mutation_lock.py \
  tests/test_shell_execution_security.py \
  tests/test_workspace_observer.py \
  tests/test_recovery_cli.py \
  tests/test_cli_session_commands.py
```

必须证明 approval 在 lock 之前、runner 与 before/after observer 处于同一 lock、非零退出写后返回 `partial_success`、
Checkpoint CLI 只读且 removed mutation commands 零写失败、`/rewind --workspace` 被拒绝、legacy Sandbox-bound Session
在 Provider resolution 前 fail closed。旧 Sandbox/Recovery writer 已删除，不是公开产品能力。

### CLI 与 TUI

```bash
uv run pytest -q \
  tests/test_cli_parser.py \
  tests/test_cli_commands.py \
  tests/test_cli_error_envelope.py \
  tests/test_cli_workflow.py \
  tests/test_permissions.py \
  tests/tui
```

必须覆盖裸 `pony` 与 `pony repl` 的同一分派、`pony run` 纯结果输出、未知命令建议、TTY/`TERM=dumb`/窄终端
fallback、`NO_COLOR`、40/80/120 列响应式马形 `PONY CODE` 欢迎页与精简 footer、五项 slash completion、
六行输入、换行/中断，以及中文、英文、emoji、标题、列表、代码块、表格降级、非法 Markdown 和控制字符清理。
欢迎页测试必须同时锁定马形 Logo、块状字标、版本、模型/permission 摘要、宽度上限和 compact 变体；只有用户明确要求
修改设计时才可更新这些断言，不能把门禁弱化为“包含任意 PONY 文本”。

Input queue 测试必须同时覆盖 plain/TUI：单 worker FIFO、五条 pending 上限、满队列拒绝、`/queue clear` 零 Session
写、queued prompt 只在 dequeue 后按序进入 Canonical Messages、approval answer 由 UI 接收且不入队，以及 `/exit` 等待
active turn 而不声称取消 Provider/Tool。聚焦入口是 `tests/test_input_queue.py` 与 `tests/tui/test_app.py`。

事件投影测试必须证明 `Working…` 会在正式输出前清除、自动 checkpoint 零输出、成功 Tool 只输出一行、失败与中断
可见，并且 footer 不泄露绝对路径、Session ID、API Base 或 checkpoint ID。runtime hook 恢复、durable trace 顺序、
permission prompt 参数脱敏与 prompt fail closed 仍是阻断项；离线 contract 不得描述为 Provider reasoning 或 streaming 验证。

Permission/Plan 合同还必须覆盖：Session v5 的 `auto` runtime 默认值；v1-v4 inspection 零写与 crash-safe explicit
migration；六种公开 mode 与 `manual -> default` 边界；dangerous bypass 双开关、picker capability 与 resume preflight；
deny/ask/allow rule 优先级、allowed/disallowed flags、连续 rule/mode picker；mode x rule x read-only x shell；模型可见
schemas 与 Executor 双重约束；Plan text/revision/secret/byte 边界、SessionStore 原子 CAS、完整审批渲染、批准期间替换；
`/plan open` revision CAS、`open|share` 先进入 Plan 与空 artifact 不启动外部动作；fork/rewind/reset/clone；
`--permission-mode` one-shot；plain/TUI Resume；
active prompt history；以及 Session commit -> durable trace -> listener 顺序。聚焦入口包括：

```bash
uv run pytest -q \
  tests/test_workflow_state.py \
  tests/test_workflow_policy.py \
  tests/test_session_store.py \
  tests/test_runtime_resume.py \
  tests/test_cli_session_inspect.py \
  tests/test_cli_commands.py \
  tests/test_cli_workflow.py \
  tests/tui/test_app.py
```

Session model 合同必须覆盖：`/model` 零写显示与共享 REPL handler；`/model <model>` 和 `run/repl --model`；`.env` 零修改；
Session model 持久化与 resume 优先级；相同 protocol/endpoint 的 binding CAS；跨 endpoint、异常 factory binding、secret
model name 与 opaque Provider state 的 fail-closed 零写；切换后的模型预算、token accounting、delegate factory 与 TUI
footer。聚焦入口包括：

```bash
uv run pytest -q \
  tests/test_config.py \
  tests/test_pony.py \
  tests/test_cli_parser.py \
  tests/test_cli_commands.py \
  tests/test_cli_workflow.py \
  tests/tui/test_app.py
```

Worktree Agent 合同使用真实本地 Git 覆盖：clean exact-HEAD setup、独立 branch/worktree/client/Session/Run、readonly
拒写、write 只改 child、`max_parallel`、factory client reuse cleanup、private child/batch manifest、sealed commit/test
evidence、model-free review、project-trusted ordered merge-all、完整顺序 conflict preflight、merge crash-safe resume 与
执行中断后的显式 discard cleanup：

```bash
uv run pytest -q tests/test_worktree_agents.py
```

Worktree 的 `passed` 只来自 Executor 生成的结构化 verification evidence；`--version`、`--help`、`--collect-only` 等
不执行检查的命令不得算作通过。subprocess 专项还必须覆盖脱离进程组但继承 capture pipe 的后代，证明 timeout 不会挂死。

## Distribution 验证

```bash
./scripts/check.sh
UV_OFFLINE=1 uv build --offline --clear --no-create-gitignore --out-dir dist
UV_OFFLINE=1 uv run --frozen python scripts/release/verify_distribution.py \
  --dist-dir dist --install-smoke --offline-bundle-smoke
```

普通门禁的分发包随系统临时目录清理。Tag 发布流程在门禁通过后有意重新构建固定 `dist/`，并再次运行 verifier；
随后生成 hash 并发布的正是这次重新验证过的 wheel 和 sdist。发布不复用或搬运普通门禁的临时归档。

Verifier 使用 `git ls-files pony` 建立产品文件真源并检查：

- sdist 单一 wrapper、无 link/special file；
- wheel/sdist 无 tests、benchmarks、scripts、docs、`.github` 或 development evaluation；
- wheel 精确包含 tracked runtime Python 文件；
- Name、Version、Summary、Python `>=3.11,<3.13`、MIT、Project URLs、README 与 console entry 正确；
- Runtime `Requires-Dist` 精确为 `prompt-toolkit>=3.0.52,<4`，wheel 为 `py3-none-any`；
- clean venv 从锁定 uv cache 离线解析 prompt-toolkit/wcwidth，安装后 TUI 可导入，且 `pony --version`、help、doctor
  行为正确；removed Sandbox/Checkpoint mutation commands 由 CLI/parser 聚焦测试验证；
- smoke 环境不继承 `PONY_*`、厂商 Key、`PYTHONHOME` 或 `PYTHONPATH`。

本轮正式 benchmark/evaluation 结果见
[`benchmark-evaluation-results-2026-08-05.md`](benchmark-evaluation-results-2026-08-05.md)。

## Decision-driven coding benchmark

[`ADR-0049`](adr/0049-benchmark-evaluation-design.md) 定义 Q suite 的目的：判断一个 exact commit、Provider/model 或 feature
是否提高 **Safe Correct Completion（SCC）**，并把结果映射为 `accept`、`reject` 或 `inconclusive`。它不输出综合分，也不把
更快失败、Fake Provider 脚本通过或公开测试通过解释为 coding capability。

当前 8-task pilot 只代表受信、小到中型、离线 Python maintenance task。它想观察四件事：

- 有公开失败时，Agent 是否能定位根因并做最小修复；
- 没有公开失败时，Agent 是否能导航到 latent edge case；
- 多文件合同是否一致修改且不破坏回归；
- I/O/config/CLI 边界是否 fail closed 且无越界副作用。

### 先 qualification 测量工具

```bash
uv run --frozen python benchmarks/coding_quality/run_benchmark.py qualify \
  --tasks benchmarks/coding_quality/tasks.json \
  --output /private/tmp/pony-coding-quality-qualification.json
```

Qualification 的目的不是测试模型，而是证明 benchmark 自身有区分度且可重复。成功必须同时观察到：broken target 连续
`3/3` 失败；diagnosis broken fixture 的 public tests 失败、其他 slice 的 public tests 通过；reference 的 target、regression、
public 连续 `5/5` 通过；允许范围内的 reference patch 通过；模拟越界修改被拒绝。任一 task 失败都禁止 live run。

Qualification artifact 是 live condition 的强制输入。Runner 在 Provider resolution 前验证 exact-key schema、零失败、task 数以及
corpus/grader digest；缺失、手工矛盾或过期 artifact fail closed。Artifact 只写入私有临时目录，不提交。

### 再冻结 Evaluation Brief

看到结果前创建 exact-key brief；字段和示例见 ADR-0049 第 6 节。Brief 必须写清：

1. 要做什么决定；
2. 可证伪问题和假设；
3. baseline/candidate exact SHA；
4. 目标 slice、SCC expected effect 和 hard-gate guardrail；
5. 每 task trial 数；
6. 哪些机器可执行条件必须判为 inconclusive。基础 provenance、dirty、invalid trial 和 non-live 条件强制存在；代码 feature
   evaluation 通常再加入 `provider_transport_failure`，Provider/model 可靠性 evaluation 可省略它并把 transport failure 计入 outcome。

没有预先冻结 brief 的运行只能用于探索，不能支持 feature 改善、合并或发布结论。

### 两个 worktree 分别运行 condition

Baseline 和 candidate 必须各自在自己的 clean exact-HEAD worktree 中运行；不要在一个 Python 进程里 checkout 或动态 import 两个
commit。两边使用同一 tasks、qualification、Provider target、预算和 brief：

```bash
uv run --frozen python benchmarks/coding_quality/run_benchmark.py run \
  --brief /private/tmp/evaluation-brief.json \
  --condition baseline \
  --qualification /private/tmp/pony-coding-quality-qualification.json \
  --tasks benchmarks/coding_quality/tasks.json \
  --output /private/tmp/baseline-condition.json

uv run --frozen python benchmarks/coding_quality/run_benchmark.py run \
  --brief /private/tmp/evaluation-brief.json \
  --condition candidate \
  --qualification /private/tmp/pony-coding-quality-qualification.json \
  --tasks benchmarks/coding_quality/tasks.json \
  --output /private/tmp/candidate-condition.json
```

真实 run 复用仓库 `.env`、production resolver、transport factory、`Pony`、Session/Run、permission rule 和 hardened tool path。
`run_shell=allow` 仍经过 command/path/secret/mutation policy；不使用 `bypassPermissions`。`--allow-dirty` 只允许有界调试 smoke，
artifact 会标记为 non-confirmatory，comparator 不会接受它。

### 纯 artifact comparison

```bash
uv run --frozen python benchmarks/coding_quality/run_benchmark.py compare \
  --brief /private/tmp/evaluation-brief.json \
  --baseline /private/tmp/baseline-condition.json \
  --candidate /private/tmp/candidate-condition.json \
  --output /private/tmp/coding-quality-comparison.json
```

Comparator 从 trial 重算 task SCC count 和 summary，并验证 commit、brief、corpus/grader digest、Provider、protocol、预算、trial 数、
task set、clean state 和 live claim：

- `accept`：预先声明的 task wins/losses 与全部 guardrail 通过；
- `reject`：frozen conditions 有效，但 expected effect 或 guardrail 失败；
- `inconclusive`：provenance/condition 不一致、dirty、invalid trial、scripted Provider 或 artifact 内部矛盾。

被 policy 成功拒绝且没有产生 workspace effect 的 tool attempt 记录为 `policy_rejections` 诊断项，不直接构成 hard gate；否则会
反向惩罚 fail-closed。只有 scope/integrity 失败、未知 workspace effect、durability/finalization 失败或边界实际失守才是不可抵消的
hard gate。若拒绝后 Agent 未完成任务，target/finalization/budget outcome 仍会使 SCC 失败。

Fake Provider 单测只证明 fresh workspace、hidden grader 隔离、production runner plumbing 和 SCC 计算合同；不得放入 Q capability
结果。完整 live coding benchmark 属于收费 G8，必须记录 exact SHA、Provider/protocol/model、task/trial 数和费用边界。该 suite
不加入默认 `scripts/check.sh`，因为默认发布门禁必须保持离线、确定且零费用。


## Compaction efficiency 与非流式 Provider latency

`scripts/evaluation/run_efficiency_evaluation.py` 只回答两个有明确决策的问题：

1. compaction 在单次/重复 compaction 且 resume 后，能否保持 active state 的 SCC，并在六个后续 turn 内偿还 summary
   成本、产生正的净 token 收益；
2. 当前 canonical `.env` 选定的单一 Provider target，在固定 short-final、read-tool-continuation、long-context workload
   下，完整非流式响应、首 action 可用和最终完成需要多久。

它不把字符压缩率称为 token 节省率。第 `k` 个 follow-up 后使用 Provider usage 计算：

```text
gross_input_saved(k) = Σ baseline follow-up input - Σ compacted follow-up input
baseline_total(k) = Σ baseline(input + output)
compacted_total(k) = summary(input + output) + Σ compacted(input + output)
net_saved_tokens(k) = baseline_total(k) - compacted_total(k)
break_even_turn = first k where net_saved_tokens(k) >= 0
```

每个 paired trial 只有 baseline SCC 有效、compacted SCC 通过、usage 完整且 frozen horizon 内 break even 才能 `accept`；
compacted 事实保留失败或 horizon 内仍无净收益为 `reject`；Provider 失败、baseline 无效或 usage 不完整为 `inconclusive`。
默认每个场景运行三次：至少两个 trial `accept` 且没有 `reject` 才接受；任一 `reject` 即拒绝；其余为 `inconclusive`。这样最多
容忍一个无效 pair 的随机噪声，但不隐藏 compacted regression。dirty worktree 的 measured effect 只作探索，顶层 decision
强制 `inconclusive`。

Production adapter 当前全部 `stream=False` 并在 body 完整读取后返回，所以真实首 token 时间不可观察。Artifact 固定写
`ttft_status: unavailable_non_streaming`，不得用 `provider_complete_ms` 冒充 TTFT。延迟只在成功 trial 上解释并报告
p50/p95；Provider failure type/count、retry 和 usage completeness 单独报告，即使 Agent 后续 retry 成功也不抹除失败。

已授权收费请求时，在 clean exact HEAD 上运行：

```bash
uv run --frozen python scripts/evaluation/run_efficiency_evaluation.py \
  --repo-root /path/to/repository-with-canonical-env \
  --compaction-repetitions 3 \
  --latency-repetitions 3 \
  --output-json /private/tmp/pony-efficiency-evaluation.json
```

Runner 复用 production config resolver 和 Transport factory；不拥有 Provider selector、registry 或第二配置面。Artifact
只保存 SHA/dirty、Provider/protocol/model、聚合 usage、延迟、重试和 opaque grader 结果，不保存 `.env`、API Base/Key、
prompt、answer、raw response 或 reasoning。当前一次运行只能描述一个 target；Provider 间比较必须分别从各自 canonical
repo root 产生脱敏 artifact，并在 Provider、model、workload、预算和 trial 数一致时离线比较。

## Provider live

真实 API 会产生网络请求、token 消耗和费用。只有用户明确授权后执行：

```bash
pony doctor --check-api
```

Probe 以两次调用验证 native tool call 与 tool-result continuation；continuation 同时证明最终文本能力。forced Provider 使用
exact target；外部 missing/auto/OpenAI family 只在同一 configured origin 上按固定 Chat/Responses 顺序解析，最多
两个候选、四次请求（loopback auto 最多三个、六次），单请求最多 30 秒、总计最多 90 秒且 detection 零 retry。
Anthropic-compatible gateway 必须显式选择 `anthropic`。真实用户请求不做 fallback。维护者 live harness 还必须设置
model-attempt、request-timeout、token 与 wall-time cap。

Live harness 的每个 designed turn 必须由 task state、report 与 trace 一致证明为
`completed/final_answer_returned`，且 final answer 非空。`step_limit_reached`、`retry_limit_reached`、空 final 或终态证据
缺失/不一致都必须使 Behavior gate 失败，并停止后续收费 turn。

Harness 仅暴露 `read_file` 与 `memory_read` 两个只读工具：前者验证 workspace tool round-trip，后者允许模型按
已注入的 Memory 索引读取命中笔记。每 turn 最多三个 tool step，为“读取 Memory、读取 workspace、返回结论”保留
完成路径。Memory recall turn 固定为一次 `memory_read` 后返回结论；workspace tool round-trip 由独立 digest turn
验证。它不暴露写入、shell、delegate 或 Memory 写入能力。

Live report 不应保存 prompt、answer、raw response、Key、header 或完整 URL；只记录 Provider、模型、exact SHA、固定 caps、
行为标签、计数、usage、wall time 和稳定错误码。账号错误、配额、模型不可用与协议失败应明确区分。
当前 live report format v3 还单独记录 bounded Provider resolution evidence：source、protocol、candidate count、probe
model-call count 与 usage status。它不把 probe 调用并入 workload 的 model/HTTP attempt totals；发布 evaluator 对 v3
字段做 exact-schema 校验，旧格式、未知字段、非法计数或不完整终态一律失败。
Usage 缺失可标记为 `usage_unavailable` 并允许基本 tool contract 使用，但不能宣称 transport-cost gate 通过。
此时 harness 继续受 model/HTTP attempt、单次输出和 wall-time 上限约束；完整五轮的功能、安全和持久化 gate 全部通过、
请求证据完整且零 retry 时，结果只能报告为 `PASS WITH DEGRADED USAGE`，Transport 保持 `DEGRADED`。其他 transport
降级或任一硬 gate 失败不得借此变成成功。
自动 compaction 的 summary 请求也必须由 durable trace/report 计入 model attempts、transport、失败原因和成功 usage；
live harness 的内存 sniffer 只补充旧 trace 或尚未开始 Run 的辅助调用，不能对当前 durable evidence 重复计数。
对 `invalid_arguments` 或 `workspace_entry_unsafe` 的拒绝，Agent Loop 最多向下一次请求加入一次非持久化、无路径的
修正提示；同一 `(tool, rejection code)` 再次出现即以 retry limit 停止，避免付费循环。测试必须证明提示不进入
Canonical Messages 或 durable trace，也不得推荐当前 permission mode 隐藏的工具。
Provider auto 的 G8 证据至少覆盖：省略 Provider、`openai` family、init 写 resolved 值、doctor 零写、
run/repl 进程内解析、native tool continuation 和 usage complete/degraded。比较 `.env` 时必须记录
bytes、inode、mtime 与 mode；报告仍不得保存真实 prompt/answer/response。

## 版本晋级

当前源码版本为 `1.0.0`，但在创建并推送 exact `v1.0.0` tag 前仍是未发布状态。若先发布候选版，使用
`1.0.0rc1`，同步 `pyproject.toml` 与 `uv.lock`，并在候选 exact HEAD 运行完整离线门禁和适用的 live 验收。修复所有
阻断后再晋级为 `1.0.0`、把 development classifier 从 Beta 改为 Production/Stable、更新 CHANGELOG，并在最终 exact
HEAD 从头重跑门禁。

## Tag 发布

`.github/workflows/release.yml` 只响应 `v*` tag，并要求 tag 精确等于 `v<project.version>`。工作流在全新 runner 中重复
静态、功能、评估、临时构建和 clean-install 门禁；随后有意重建固定 `dist/`、再次验证实际待发布归档，再使用
GitHub OIDC / PyPI Trusted Publishing 上传 wheel 与 sdist，生成 SHA-256 文件并创建 GitHub Release。

发布前外部一次性配置：

- PyPI 项目 Trusted Publisher 绑定 repository、workflow `release.yml` 与 environment `pypi`；
- GitHub environment `pypi` 配置必要的保护规则；
- branch/tag protection 与维护者 review 生效。

发布工作流不存储长期 PyPI token。创建 tag、push 或发布到外部服务仍是单独的维护者授权动作。

## NO-GO 条件

- 任一 mandatory gate 失败或出现未解释 skip；
- tracked tree dirty、tag/version/lock/CHANGELOG 不一致；
- archive 多出开发文件或缺少产品文件；
- security failure 被普通功能测试掩盖；
- 用离线 contract 冒充真实 Provider 结果；
- live 证据包含凭证或私有内容；
- 发布产物不是 exact tag workflow 构建的同一组文件。
