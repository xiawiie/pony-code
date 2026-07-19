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
fallback、`NO_COLOR`、40/80/120 列的 5/7/11 行响应式马形 `PONY CODE` 欢迎页与精简 footer、五项 slash
completion、六行输入、换行/中断，以及中文、英文、emoji、标题、列表、代码块、表格降级、非法 Markdown 和控制字符
清理。

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
- Name、Version、Summary、Python 要求、MIT、Project URLs、README 与 console entry 正确；
- Runtime `Requires-Dist` 精确为 `prompt-toolkit>=3.0.52,<4`，wheel 为 `py3-none-any`；
- clean venv 从锁定 uv cache 离线解析 prompt-toolkit/wcwidth，安装后 TUI 可导入，且 `pony --version`、help、doctor
  行为正确；removed Sandbox/Checkpoint mutation commands 由 CLI/parser 聚焦测试验证；
- smoke 环境不继承 `PONY_*`、厂商 Key、`PYTHONHOME` 或 `PYTHONPATH`。

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
对 `invalid_arguments` 或 `workspace_entry_unsafe` 的拒绝，Agent Loop 最多向下一次请求加入一次非持久化、无路径的
修正提示；同一 `(tool, rejection code)` 再次出现即以 retry limit 停止，避免付费循环。测试必须证明提示不进入
Canonical Messages 或 durable trace，也不得推荐当前 permission mode 隐藏的工具。
Provider auto 的 G8 证据至少覆盖：省略 Provider、`openai` family、init 写 resolved 值、doctor 零写、
run/repl 进程内解析、native tool continuation 和 usage complete/degraded。比较 `.env` 时必须记录
bytes、inode、mtime 与 mode；报告仍不得保存真实 prompt/answer/response。

## 版本晋级

当前源码版本为 `1.0.0`，但在创建并推送 exact `v1.0.0` tag 前仍是未发布状态。若先发布候选版，使用
`1.0.0rc1`，同步 `pyproject.toml` 与 `uv.lock`，并在候选 exact HEAD 运行完整离线门禁和适用的 live 验收。修复所有
阻断后再晋级为 `1.0.0`、更新 CHANGELOG，并在最终 exact HEAD 从头重跑门禁。

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
