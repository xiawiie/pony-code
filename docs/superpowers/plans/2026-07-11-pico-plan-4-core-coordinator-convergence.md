# Pico Plan 4：核心协调器收敛

状态：待执行

基线分支：`memory`

基线 HEAD：`c9e3aca6bbd9092f79de9e60aa907c84fae1a879`

设计真源：`docs/superpowers/specs/2026-07-11-pico-current-surface-hard-cut-design.md`

## 1. 前提与边界

Plan 3 已完成并推送：

- 本地全量：`1987 passed, 6 skipped`；
- offline live assertions：`66 passed`；
- persistence/recovery/security/Memory/benchmark focused：`604 passed`；
- wheel 与 sdist 构建成功；
- 私有 migration journal 为 `verified`，4 session + 2 checkpoint + 2 tool-change 严格重读成功，
  38 个 verify-only hash 不变；
- GitHub CI run `29164337211`，Ubuntu Python 3.11/3.12 均为 `success`。

本计划只收敛两个协调器：

- `pico/tool_executor.py::ToolExecutor.execute`；
- `pico/agent_loop.py::AgentLoop.run`。

允许在这两个模块增加直接服务于拆分的 private helper；不得修改或重构：

- `tools.validate_tool`；
- `RecoveryManager`、`RecoveryPolicy`；
- `safe_subprocess`、`security`；
- Provider、配置、持久化格式、Memory、CI 或 build surface。

不得新增 registry、event bus、状态机、pipeline dataclass、interface/factory 或通用 executor。
继续使用现有 `Action`、`TaskState`、`ToolExecutionResult`、recorders 与安全 primitives。
`Pico.run_tool` 仍有生产/evaluation 调用者，不删除、不兼容化改名。

真实 Provider 禁止运行。七个 protected untracked 路径不得移动、删除、修改或 stage：

```text
.superpowers/brainstorm/
docs/superpowers/plans/2026-07-09-pico-action-kernel-model-connection.md
docs/superpowers/specs/2026-07-06-pico-full-review-design.md
docs/superpowers/specs/2026-07-08-pico-action-kernel-provider-parity-design.md
findings.md
progress.md
task_plan.md
```

## 2. Plan 4 实际复杂度基线

基线命令：

```bash
uv run ruff check pico --select C901 --output-format json > /tmp/pico-plan4-c901.json || true
```

基于 Plan 3 HEAD 的结果：

| 指标 | `tool_executor.py` | `agent_loop.py` |
| --- | ---: | ---: |
| physical LOC | 1,416 | 977 |
| nonblank/non-comment LOC | 1,320 | 905 |
| functions/methods | 25 | 19 |
| target | `execute`，496 LOC | `run`，404 LOC |
| target C901 | 51 | 24 |
| target branch shape | 38 `if`、5 `try`、9 handlers、15 returns | 15 `if`、1 loop、4 `try`、7 handlers |
| file C901 violations | 4：16、51、17、16 | 2：14、24 |

全仓为 61 个 C901 violations，分布在 28 个文件。设计文档中的 68 只作为历史参考。

最终 ratchet 必须同时满足：

1. `ToolExecutor.execute` 无 C901 finding，即复杂度 ≤10；
2. `AgentLoop.run` 无 C901 finding，即复杂度 ≤10；
3. `tool_executor.py` 最高复杂度 ≤17 且 C901 数 ≤4；
4. `agent_loop.py` 最高复杂度 ≤14 且 C901 数 ≤2；
5. 全仓 C901 数 ≤61；
6. 不以新增高复杂 private helper 平移两个 coordinator 的职责。

不增加永久 complexity framework。每次提交后用同一 Ruff JSON 与下面的 inline checker 重算，
Plan 4 handoff 记录最终数字。

## 3. 冻结的 ToolExecutor 当前合同

唯一生产调用链：

```text
Pico.execute_tool
  → ToolExecutor.execute
  → ToolExecutionResult
```

协调阶段固定为：

```text
validate / approve
  → prepare / execute once
  → observe and record effects once
  → terminalize result or failure
```

必须保持：

- 未知工具采用 fail-safe `workspace_write` effect；allowlist、unknown、read-only、validation、
  repeat、approval rejection 均在 runner 前返回完整且脱敏的 metadata；
- 非 shell 顺序固定为 validate → repeated-call guard → approval → approval 后重新校验；
- approval 在 mutation lock 之前；approval args 与原 snapshot 任一变化都拒绝；
- shell 先做 command assessment；sensitive/reject、safe argv、complex shell approval、trusted
  executable 与 hardened git 顺序不变；
- mutation lock 覆盖 pending-review guard、before-state、pending record、runner、after-state、
  verification 与 terminalization；
- runner 恰好调用一次；shell success/nonzero、普通 exception、workspace changed/not changed 映射到
  `ok/error/partial_success` 的语义不变；
- workspace effect 在 success/error 两条路径使用同一 observer/direct-path 逻辑；
- Tool Change 的 `pending → finalized/error/partial_success/interrupted` 映射、file entries、shell
  side effects、approval 与 verification evidence 不变；
- finalization failure 不伪造 terminal success；保留 pending/review-blocking evidence，并让下一次
  mutation 返回 `recovery_review_required`；
- post-pending `KeyboardInterrupt`、`SystemExit` 和其他 `BaseException` 都 best-effort 标记
  `interrupted` 后原样重抛；若 interrupted finalization 本身失败，则保留 pending review evidence，
  仍不得覆盖 primary exception；
- redaction 先于 result、trace、session、Tool Change 与 recovery artifact。

所有返回 metadata 至少保持当前字段合同：

```text
tool_status
tool_error_code
security_event_type
risk_level
effect_class
affected_paths
workspace_changed
diff_summary
```

shell 继续附加 command risk/approval；验证成功继续附加 verification evidence；记录型写入继续附加
tool change/file entry 信息。

## 4. 冻结的 AgentLoop 当前合同

唯一主调用链：

```text
Pico.ask
  → AgentLoop.run
  → model_client.complete
  → decode_action
  → exactly one Action
```

协调阶段固定为：

```text
preflight
  → one model attempt
  → decode / apply one Action
  → finalize once
```

必须保持：

- 用户消息先脱敏并单次 session commit；失败时不创建 run、不调用 Provider；
- 每个 top-level turn 只构建一次 injection snapshot；
- 每个 attempt 只 build 一次 request、调用一次 `complete`、累计一次 Response usage、decode/sanitize
  一次 Action；
- native 多工具 Response 只执行第一个 action，`ignored_tool_count` 进入 trace；
- Retry 消耗 attempt、不消耗 tool step；runtime feedback 只进入紧接的一次 request，随后清空；
- rejected tool 不消耗 tool step；ok/error/partial-success tool 消耗一步；
- tool-use/tool-result 由 `make_tool_pair` 组成，并在一次 `_commit_session` 中原子保存；pair save
  失败回滚 working memory 与 file summaries，不再调用 Provider；
- usage 只累计已经返回的 Response；
- resume freshness、workspace mismatch、context reduction、tool-executed checkpoint 的触发与 trace
  顺序不变；
- ToolExecutor 拥有 Tool Change terminalization；AgentLoop 只把 workspace tool-change 与
  verification evidence 归入 turn/recovery checkpoint；
- final、step-limit、retry-limit、model error、runtime error、persistence error、KeyboardInterrupt
  均只调用一次 `_finalize_run`，并保持 terminal message、TaskState、checkpoint、report 与 trace；
- finalizer 次生错误不得覆盖 Provider/persistence/interrupt/runtime primary exception。

敏感 action 的拒绝仍通过现有结构化 `ToolExecutionResult` 进入同一 tool-pair path；本计划不改变
该策略，也不增加另一套 dispatcher。

## 5. Task 1：冻结 ToolExecutor 行为矩阵

提交：`test(runtime): freeze tool executor behavior matrix`。

优先复用并强化现有语义测试，不复制 shell/security corpus。把依赖 private helper call-count 的
断言改为最终 result、record、runner、lock 与 next-mutation 语义。

新增或收紧：

- early rejection 表：unknown、disallowed、invalid、sensitive、read-only、repeat、approval denied；
  断言 exact status/code/security/effect、runner=0、pending=0；
- approval args 变更与 approval 后二次校验；
- success/error/partial-success 的 Tool Change terminal record 与 effect evidence；
- post-pending `SystemExit`/custom `BaseException` interrupted + primary identity；
- recorder start 已持久化后抛错、finalize failure、interrupted-finalize failure 的 review evidence；
- mutation lock enter/exit、runner/observer/verification/update-memory fault 的锁释放与 primary ordering；
- malformed shell result 继续 fail closed，runner 产生副作用时仍保留 recovery evidence。

其中现有行为冻结必须在 Task 1 提交前全绿。已知规范缺口对应的 post-pending
`SystemExit`/custom `BaseException`、observer/verification/memory-update `BaseException` 与
mutation-lock exit primary-order 测试，在 Task 1 先确认会精确失败，但不提交失败中间态；它们在
Task 2 与生产修正同一原子提交重新加入并转绿。

Allowlist：

- `tests/test_tool_executor.py`
- `tests/test_tool_executor_mutation_lock.py`
- 仅当已有 malformed-shell matrix 必须补断言时：`tests/test_shell_execution_security.py`

Focused：

```bash
uv run pytest -q \
  tests/test_tool_executor.py \
  tests/test_tool_executor_mutation_lock.py \
  tests/test_shell_execution_security.py \
  tests/test_shell_security_corpus.py \
  tests/test_sensitive_tools.py \
  tests/test_allowed_tools.py \
  tests/test_tool_change_recorder.py \
  tests/test_verification_evidence.py \
  tests/test_verification_security.py \
  tests/test_recovery_e2e.py \
  tests/test_safety_invariants.py
```

## 6. Task 2：收敛 ToolExecutor.execute

提交：`refactor(runtime): converge tool execution coordinator`。

只在 `pico/tool_executor.py` 提取少量 private helper。建议职责边界：

```text
_prepare_tool_request
_begin_tool_change
_invoke_prepared_tool
_observe_tool_effects
_finish_tool_success
_finish_tool_failure
```

名称可按实际代码调整，但必须满足：

- `execute` 只编排四个批准阶段；
- 不把现有 specialized shell、recovery、security helper 重写一遍；
- success/error 复用一个 effect observation 真源；
- mutation lock 的完整临界区不缩短；
- 新 helper 使用普通参数/tuple/dict，不新增 public type 或 pipeline object；
- 新 helper 各自目标 C901 ≤10；不得改其他既有 C901 helper；
- only intentional behavior correction 是 post-pending BaseException 的 interrupted evidence；
- 删除模块中仅描述旧兼容阶段的措辞，但不扩大到其他文件。

Allowlist：

- `pico/tool_executor.py`
- Task 1 测试仅在实现暴露真实行为缺口时作最窄修正。

Task 1 focused 全绿后运行 complexity checkpoint：`execute≤10`、文件最高 `<51`、文件 violation
数不增加、全仓总数不增加。随后独立只读 review ToolExecutor diff，再提交。

## 7. Task 3：冻结 AgentLoop 行为矩阵

提交：`test(runtime): freeze agent loop behavior matrix`。

新增或收紧：

- final、step-limit、retry-limit、model error、preflight/runtime error、persistence error、
  KeyboardInterrupt 的 exact-one finalizer matrix；
- retry-limit 端到端 attempt/tool-step、one-shot feedback、terminal message、usage 与 call cap；
- native multiple-tool Response 只执行第一项，第二项 runner/pending=0，trace 有 ignored count；
- tool runner 普通 error：原子提交 error pair、消耗 tool step、继续下一次 model call、最终 report 与
  recovery checkpoint 正确；
- pair-save failure：无 orphan message、memory 派生状态回滚、Provider 不继续；
- decode/apply/checkpoint/trace/report/finalizer fault：TaskState 与 primary exception 顺序不变；
- injection snapshot 每 turn 一次，runtime retry feedback 精确使用一次。

避免用“第 N 次 save”或只 spy private helper 的方式冻结实现形状；优先按 canonical messages、
TaskState、trace、report、record 与 Provider call count 断言。

Allowlist：

- `tests/test_agent_loop.py`
- `tests/test_agent_loop_injection_sent.py`
- 仅当完整 structured Response fixture 必须复用时：`tests/test_agent_loop_e2e.py`
- 仅当 report primary-order assertion 无法留在主测试时：`tests/test_runtime_report.py`

Focused：

```bash
uv run pytest -q \
  tests/test_agent_loop.py \
  tests/test_agent_loop_e2e.py \
  tests/test_agent_loop_injection_sent.py \
  tests/test_agent_loop_request_shape.py \
  tests/test_action_codec.py \
  tests/test_message_invariants.py \
  tests/test_runtime_report.py \
  tests/test_recovery_e2e.py \
  benchmarks/live_e2e/tests/test_assertions.py
```

## 8. Task 4：收敛 AgentLoop.run

提交：`refactor(runtime): converge agent loop coordinator`。

只在 `pico/agent_loop.py` 提取少量 private helper。建议职责边界：

```text
_start_agent_run
_build_attempt_request
_complete_model_attempt
_apply_tool_action
_finish_budget_stop
_terminalize_run_exception
```

名称可按实际代码调整，但必须满足：

- `run` 只保留 start → attempt loop → action outcome → single terminalization；
- `Response → decode_action → Action` 仍是唯一 decode path；
- 不创建新的 outcome class/state machine；可返回现有 Action、普通 tuple 或直接委托；
- one-shot feedback、fixed injection snapshot、usage、tool-pair commit/rollback 与 checkpoint ownership
  不移动；
- model/persistence/runtime/interrupt 都汇入一个实际的 finalizer call，不再依赖“先改 status，外层
  if 不再 finalize”的隐式去重；
- primary exception 在 finalizer fault 时仍优先；
- 新 helper 各自目标 C901 ≤10；不得重构 `_prepare_tool_result` 或其他非目标复杂函数。

Allowlist：

- `pico/agent_loop.py`
- Task 3 测试仅在实现暴露真实行为缺口时作最窄修正。

Task 3 focused 全绿后运行 complexity checkpoint：`run≤10`、文件最高 `<24`、文件 violation 数
不增加、全仓总数不增加。随后独立只读 review AgentLoop diff，再提交。

## 9. Task 5：组合 complexity ratchet 与完成门禁

不新增永久 complexity framework。用相同 Ruff JSON 运行以下 checker：

```bash
uv run ruff check pico --select C901 --output-format json > /tmp/pico-plan4-final-c901.json || true
uv run python - <<'PY'
import json
from pathlib import Path

rows = json.loads(Path('/tmp/pico-plan4-final-c901.json').read_text())
assert len(rows) <= 61

target_limits = {
    'tool_executor.py': {'count': 4, 'maximum': 17, 'forbidden': '`execute`'},
    'agent_loop.py': {'count': 2, 'maximum': 14, 'forbidden': '`run`'},
}
for filename, limits in target_limits.items():
    matches = [row for row in rows if Path(row['filename']).name == filename]
    assert len(matches) <= limits['count']
    assert all(limits['forbidden'] not in row['message'] for row in matches)
    complexities = [
        int(row['message'].rsplit('(', 1)[1].split(' > ', 1)[0])
        for row in matches
    ]
    assert max(complexities, default=0) <= limits['maximum']
print({'total': len(rows), 'tool_executor': len([
    row for row in rows if Path(row['filename']).name == 'tool_executor.py'
]), 'agent_loop': len([
    row for row in rows if Path(row['filename']).name == 'agent_loop.py'
])})
PY
```

组合 focused：

```bash
uv run pytest -q \
  tests/test_tool_executor.py \
  tests/test_tool_executor_mutation_lock.py \
  tests/test_shell_execution_security.py \
  tests/test_shell_security_corpus.py \
  tests/test_sensitive_tools.py \
  tests/test_tool_change_recorder.py \
  tests/test_verification_evidence.py \
  tests/test_verification_security.py \
  tests/test_agent_loop.py \
  tests/test_agent_loop_e2e.py \
  tests/test_agent_loop_injection_sent.py \
  tests/test_agent_loop_request_shape.py \
  tests/test_action_codec.py \
  tests/test_message_invariants.py \
  tests/test_runtime_report.py \
  tests/test_recovery_e2e.py \
  tests/test_artifact_security.py \
  tests/test_safety_invariants.py
```

全量门禁：

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
git diff --check
```

重验：

- migration journal 仍为 `verified`；
- 4+2+2 strict records、38 verify-only hash 与 business counts 不变；
- `.env` status/mode 与七个 protected untracked 原样；
- tracked worktree clean；
- 未运行真实 Provider。

推送 `memory`，等待与新 HEAD 精确匹配的 Ubuntu Python 3.11/3.12 CI。Plan 4 本地与 CI
全绿后，才基于实际 HEAD 写 Plan 5。

## 10. 失败与回滚

- 行为矩阵在 refactor 前不绿：先修测试/基线判断，不开始 coordinator 提取；
- ToolExecutor matrix 在提取后失败：只回滚 ToolExecutor 提交，不用兼容分支掩盖；
- AgentLoop matrix 在提取后失败：只回滚 AgentLoop 提交；
- coordinator ≤10 但新 helper 复杂度上升或 repo total 增长：视为复杂度平移，继续收敛或回滚；
- pending evidence、primary exception、atomic pair、redaction、approval 或 lock 边界任一变化：P0，停止；
- 七个 protected untracked 或私有 manifest 漂移：停止，不扩大 scope；
- CI failure：只针对实际失败修复并重新跑本地对应门禁；
- 不得以 skip、xfail、降级安全断言或增加 compatibility path 通过门禁。

## 11. Handoff 证据

- 计划文档、两份 matrix、两份 coordinator refactor 的提交 SHA；
- before/after C901：repo total、两个文件 counts/max、两个 target values；
- ToolExecutor/AgentLoop/combined focused 数字；
- full/offline/build 结果；
- private journal/hash/business revalidation；
- GitHub CI run ID、HEAD、Python 3.11/3.12 conclusions；
- 七个 protected untracked status；
- 所有 intentional behavior correction、deviation、rollback 或 fault remediation。
