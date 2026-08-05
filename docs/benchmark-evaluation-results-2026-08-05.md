# Benchmark 与 Evaluation 结果（2026-08-05）

本文记录 Pony coding-quality benchmark、compaction efficiency evaluation 和单目标 Provider latency
evaluation 的最终结果。设计与决策规则见
[`ADR-0049`](adr/0049-benchmark-evaluation-design.md)，可复现门禁见
[`verification.md`](verification.md)。

## 1. 结论摘要

| Evaluation | 最终决策 | 结论 |
| --- | --- | --- |
| Coding-quality runner/protocol | 接受为 pilot 基础设施 | 能冻结 brief、隔离 hidden grader、区分 invalid/product outcome，并阻止 dirty/Fake/条件不匹配得到 `accept` |
| 当前 8-task coding corpus | 拒绝 confirmatory 使用 | 修正后 live pilot 为 `23/24 SCC`，接近 ceiling，不能比较模型或 feature 优劣 |
| 单次 compaction + resume | `accept` | SCC 全部通过，六轮净 token 节省率 p50 为 `56.438%`，均在第 2 轮回本 |
| 重复 compaction + resume | `accept` | SCC 全部通过，六轮净 token 节省率 p50 为 `47.362%`，在第 2–3 轮回本 |
| 当前单一 Provider target latency | 测量完成 | 9/9 workload 成功；非流式 Transport 无法观测真实 TTFT |

本轮 efficiency confirmatory evaluation 对应实现 commit：

```text
29428ac2fb602db3c681b382b3d3080d1b25b17c
```

目标绑定为：

```text
provider: openai-chat
transport: openai_chat_completions
variant: chat_completions
model: qwen3.7-max
```

本报告不记录 API Base、API Key、prompt、answer、raw response 或 reasoning。

## 2. Evaluation 要回答什么

### 2.1 Coding quality

目的不是给模型生成一个综合分，而是判断一个 exact commit、Provider/model 或 feature change 是否在保持安全、完整性和
可恢复性的前提下提高 Safe Correct Completion（SCC）。

每个 task 同时检查：

- hidden target 是否满足；
- regression 与公开测试是否通过；
- workspace scope/integrity 是否保持；
- tool effect、durability 和 finalization 是否可信；
- 是否在冻结的 step、wall-time 和模型预算内完成。

成功阻止危险动作只记录为 `policy_rejections`，不作为 hard gate。只有边界实际失守、未知 workspace effect、scope/integrity、
durability 或 finalization 失败才是不可抵消的 hard gate。

### 2.2 Compaction

目的不是判断 summary 字符是否变短，而是判断：

> 在 active state 不丢失的前提下，compaction 能否在六个 follow-up 内偿还 summary 成本，并降低真实 Provider 总 token 成本。

SCC 要求保留 active goal、当前 decision、constraint、file/error、unfinished task 和 next step，同时不得恢复 superseded
decision。正式计算包含 summary 请求的 input/output token：

```text
gross_input_saved(k) = Σ baseline follow-up input - Σ compacted follow-up input
baseline_total(k) = Σ baseline(input + output)
compacted_total(k) = summary(input + output) + Σ compacted(input + output)
net_saved_tokens(k) = baseline_total(k) - compacted_total(k)
net_token_saving_rate(k) = net_saved_tokens(k) / baseline_total(k)
```

### 2.3 Provider latency

目的是真实回答“首个可用 action 和最终结果需要等待多久”。当前 production adapter 使用非流式请求，完整 body 返回前无法
观察 token，因此不得把完整响应延迟称为 TTFT：

```text
ttft_status = unavailable_non_streaming
```

## 3. Coding-quality 结果

### 3.1 Offline qualification

当前 8-task corpus/grader qualification 为 `8/8`：

```text
broken target: 每个 task 连续 3/3 失败
reference target/regression/public: 每个 task 连续 5/5 通过
integrity/tamper rejection: 8/8 通过
```

这证明 fixture、reference、hidden grader、公开失败形态和 integrity contract 有区分度；它不证明真实 Provider coding
capability。

### 3.2 收费 live pilot

同一 live target 共执行 24 个 fresh trials。原始 format-v1 artifact 把 9 个 policy 成功拒绝错误计为 hard gate；review
后升级为 format v2，并按修正语义重算：

```text
hidden target pass: 24/24
regression pass: 24/24
self-verification: 24/24
corrected SCC: 23/24
remaining failure: hardening-env-update trial 3 scope_integrity failure
usage: 500,647 total tokens
model attempts: 196
tool steps: 168
```

| Task | Corrected SCC |
| --- | ---: |
| diagnosis-date-boundary | 3/3 |
| diagnosis-cache-key | 3/3 |
| navigation-unicode-config | 3/3 |
| navigation-nested-merge | 3/3 |
| contract-runtime-option | 3/3 |
| contract-error-envelope | 3/3 |
| hardening-path-traversal | 3/3 |
| hardening-env-update | 2/3 |

因此：

- runner/evaluation protocol 可作为 pilot 基础设施；
- 当前 corpus 对该 target 接近 ceiling；
- 当前结果不能支持 Provider、model 或 feature 的 confirmatory comparison；
- 应保留 1–2 个 must-pass canary，其余替换为来自 Pony 真实修复历史的 multi-file、stateful、policy-aware task。

## 4. Compaction efficiency 正式结果

每种场景执行 3 个 baseline/compacted paired trials。所有 baseline 和 compacted SCC 均通过，usage 完整，Provider failure 与
follow-up transport retry 均为 0。

| 场景 | Trial | 回本轮次 | Baseline total | Compacted total | 净节省 token | 六轮净节省率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 单次 compaction + resume | 1 | 2 | 39,679 | 17,085 | 22,594 | 56.942% |
| 单次 compaction + resume | 2 | 2 | 39,778 | 17,328 | 22,450 | 56.438% |
| 单次 compaction + resume | 3 | 2 | 39,719 | 17,719 | 22,000 | 55.389% |
| 重复 compaction + resume | 1 | 3 | 39,831 | 21,689 | 18,142 | 45.547% |
| 重复 compaction + resume | 2 | 3 | 39,741 | 20,919 | 18,822 | 47.362% |
| 重复 compaction + resume | 3 | 2 | 39,801 | 19,887 | 19,914 | 50.034% |

聚合结果：

| 场景 | Accepted | Rejected | Inconclusive | 净节省率 p50 | 范围 | 决策 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 单次 compaction + resume | 3 | 0 | 0 | 56.438% | 55.389%–56.942% | `accept` |
| 重复 compaction + resume | 3 | 0 | 0 | 47.362% | 45.547%–50.034% | `accept` |

该结论只适用于冻结的 history、六轮 follow-up、当前 Provider/model 和本次预算；它不应外推为所有真实 Session 的固定节省率。

## 5. Provider latency 正式结果

固定 workload 为 `short_final`、`tool_continuation` 和 `long_context_final`，每类执行 3 次：

```text
trial success: 9/9
usage complete: true
Provider failures: 0
transport retries: 0
Provider completions: 12
```

`tool_continuation` 每个 trial 包含两次模型调用，因此 9 个 workload trials 共记录 12 次完整 Provider response。

### 5.1 总体

| 指标 | p50 | p95 |
| --- | ---: | ---: |
| 完整 Provider response | 1.715s | 3.964s |
| 首个 action 可用 | 2.090s | 4.045s |
| 最终完成 | 3.120s | 4.703s |

### 5.2 按 workload

| Workload | Provider complete p50/p95 | First action p50/p95 | Final p50/p95 |
| --- | ---: | ---: | ---: |
| Short final | 1.601s / 1.788s | 1.644s / 1.829s | 1.723s / 1.907s |
| Tool continuation | 1.615s / 2.922s | 2.135s / 3.129s | 3.787s / 4.903s |
| Long context final | 3.864s / 4.064s | 3.903s / 4.115s | 3.967s / 4.193s |

`provider_complete_ms` 是完整非流式响应延迟，不是首 token 延迟。真实 TTFT 只有在产品 Transport 支持 streaming 后才能测量；
本轮没有为了 benchmark 增加 streaming、第二 Provider registry 或模型 catalog。

## 6. 离线与收费验证证据

聚焦验证：

```text
Ruff: passed
83 passed
```

Clean exact-HEAD 完整门禁：

```text
2767 passed
core-functional passed
offline assertions passed
sdist/wheel build passed
archive verification passed
two clean-install smoke checks passed
```

收费验证：

```text
bounded Provider probe: 2 calls, passed, usage complete
v2 smoke: compaction accept, latency 100%, Provider failures 0
formal evaluation: all paired compaction trials accepted, latency 9/9
```

请求费用由实际 Provider/gateway 账单决定；没有可信账单或冻结价格表时不在报告中估算金额。

## 7. Artifact 与隐私

正式 artifact 使用 format version 2，记录 evaluated commit、dirty state、Provider/protocol/model、usage、SCC、延迟、失败类型和
重试计数。检查结果：

```text
provenance dirty: false
claim scope: confirmatory single-target efficiency evaluation
artifact file mode: 0600
privacy/integrity audit: passed
```

原始 evaluation artifact 属于本地低敏证据，不进入 Git、wheel 或 sdist。它不包含 `.env`、API Base、API Key、prompt、answer、
messages、raw response 或 reasoning。

## 8. 适用边界与下一步

1. 当前只有一个 canonical target，不能声称完成 Provider-to-Provider comparison。其他 Provider 必须在相同 commit、workload、预算、
   trial 数和 evidence policy 下独立生成脱敏 artifact 后再比较。
2. 当前 8-task corpus 只作为 pilot/canary；在替换为真实 multi-file、stateful、policy-aware 历史任务并重新 qualification 前，
   不进行昂贵的 baseline/candidate confirmatory comparison。
3. 当前 production Transport 非流式，因此不提供 TTFT。只有产品确实需要 streaming 时才增加 streaming observability，不能只为生成
   benchmark 数字扩大运行时复杂度。
4. Compaction 节省率必须与 SCC、break-even horizon 和 Provider usage 一起解释，不能只报告字符压缩率。
