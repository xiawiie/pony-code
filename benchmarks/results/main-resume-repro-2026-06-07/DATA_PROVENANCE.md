# Pico 简历数据复现说明

本目录用于复现这段简历描述里的核心数据。复现对象是 main 分支的本地 agent harness 指标，不是线上业务数据。

## 目录内容

| 文件 | 用途 |
| --- | --- |
| `harness-regression-v2.json` | 固定 harness regression 任务结果 |
| `context-ablation-v2.json` | 长上下文治理对照实验 |
| `memory-ablation-v2.json` | 结构化记忆对照实验 |
| `recovery-ablation-v2.json` | checkpoint / resume 恢复实验 |
| `pico-benchmark-core-report.md` | 自动生成的核心 benchmark 汇总 |

说明：本归档只提交可复核的 JSON/Markdown 结果，不提交临时 workspace 副本；每题的摘要、verifier、状态和运行工件字段已经写入 `harness-regression-v2.json`。

## 复现命令

在仓库根目录执行：

```bash
uv run python - <<'PY'
from pathlib import Path
from pico.evaluation.evaluator import run_harness_regression_v2
from pico.evaluation.metrics import (
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
)

out = Path("benchmarks/results/main-resume-repro-2026-06-07")
run_harness_regression_v2(
    benchmark_path=Path("benchmarks/coding_tasks.json"),
    artifact_path=out / "harness-regression-v2.json",
    workspace_root=Path("/tmp/pico-main-resume-workspaces"),
)
run_context_ablation_v2(out / "context-ablation-v2.json", repetitions=5)
run_memory_ablation_v2(out / "memory-ablation-v2.json", repetitions=5)
run_recovery_ablation_v2(out / "recovery-ablation-v2.json", repetitions=3)
write_benchmark_core_report(
    report_path=out / "pico-benchmark-core-report.md",
    harness_artifact_path=out / "harness-regression-v2.json",
    context_artifact_path=out / "context-ablation-v2.json",
    memory_artifact_path=out / "memory-ablation-v2.json",
    recovery_artifact_path=out / "recovery-ablation-v2.json",
)
PY
```

## 数据逐条解释

### 1. Agent Harness 架构设计

简历写法：

> 支持 2 类模型后端、7 类工具和 3 类运行工件。

口径拆解：

| 数字 | 怎么来的 | 复现/查看方式 |
| --- | --- | --- |
| 2 类模型后端 | 这是早期 `resume-metrics.md` 的 provider 实验口径，不等于当前 main 所有可配置 provider 数。当前 main 已经支持更多 provider 配置路径。 | 早期本地实验快照中的 `Model backends: 2` |
| 7 类工具 | `pico/tools.py` 里 6 个基础工具，加上 `delegate`，总共 7 个可暴露工具。 | `BASE_TOOL_SPECS` 有 6 个：`list_files/read_file/search/run_shell/write_file/patch_file`，`legal_tool_names()` 额外加入 `delegate` |
| 3 类运行工件 | 每次 run 固定落盘 `task_state.json`、`trace.jsonl`、`report.json`。 | `pico/run_store.py` 的 `task_state_path()`、`trace_path()`、`report_path()` |

面试解释：

这些不是线上统计，而是 harness 的能力口径。更严谨地说，当前 main 的 provider 配置已经多于 2 类；如果不想被追问，可以把“2 类模型后端”改成“多 provider 配置”。

### 2. 长上下文治理

简历写法：

> 在 12 组长上下文配置里，将平均 prompt 长度从 7082 压到 5664，平均压缩率 16.19%，最高压缩率 33.28%，同时保证当前请求不被裁坏。

原始简历口径：

| 指标 | 值 | 来源字段 |
| --- | --- | --- |
| 配置数 | 12 | 历史 context ablation 快照：`config_count` |
| 平均压缩前 prompt 长度 | 7082.33 | `summary.avg_raw_prompt_chars` |
| 平均压缩后 prompt 长度 | 5663.67 | `summary.avg_full_prompt_chars` |
| 平均压缩率 | 16.19% | `summary.avg_prompt_compression_ratio` |
| 最高压缩率 | 33.28% | `summary.max_prompt_compression_ratio` |
| 当前请求保留率 | 100% | `summary.current_request_preserved_rate` |

本次复跑结果：

| 指标 | 值 | 来源字段 |
| --- | --- | --- |
| 配置数 | 12 | `context-ablation-v2.json`: `config_count` |
| 平均压缩前 prompt 长度 | 6994.33 | `summary.avg_raw_prompt_chars` |
| 平均压缩后 prompt 长度 | 5575.67 | `summary.avg_full_prompt_chars` |
| 平均压缩率 | 16.36% | `summary.avg_prompt_compression_ratio` |
| 最高压缩率 | 33.59% | `summary.max_prompt_compression_ratio` |
| 当前请求保留率 | 100% | `summary.current_request_preserved_rate` |

为什么本次复跑和简历数字略有差异：

`run_context_ablation_v2()` 会构造 12 组固定矩阵：3 档 history、2 档 note、2 档 request。prompt 文本由当前代码里的 prompt 模板、工具说明、上下文段落共同决定。main 后续改过 prompt/上下文模板后，字符数会轻微变化，所以本次复跑是 `6994 -> 5576`，而简历原始快照是 `7082 -> 5664`。两者证明的是同一个机制：压缩后 prompt 变短，并且当前请求没有被裁掉。

面试解释：

这是 context ablation，不是用户流量统计。实验固定生成 12 组长上下文压力配置，对比开启上下文治理前后的 prompt 字符数，并检查 current request 是否仍保留。

### 3. 结构化记忆系统

简历写法：

> 在 12 个记忆依赖任务里，follow-up 阶段的重复读文件次数从 60 次降到 0 次，且不再需要额外工具调用去重新确认已经拿到的事实。

本次复跑结果：

| 指标 | 值 | 来源字段 |
| --- | --- | --- |
| 任务数 | 12 | `memory-ablation-v2.json`: `task_count` |
| 每个 variant 运行数 | 60 | `runs_per_variant`，12 个任务 x 5 次 repetition |
| memory off 重复读 | 60 | `variants.memory_off.repeated_reads` |
| memory on 重复读 | 0 | `variants.memory_on.repeated_reads` |
| memory off 平均工具步数 | 1.00 | `variants.memory_off.avg_tool_steps` |
| memory on 平均工具步数 | 0.00 | `variants.memory_on.avg_tool_steps` |
| memory on 正确率 | 100% | `variants.memory_on.correct_rate` |
| memory 命中率 | 100% | `variants.memory_on.memory_hit_rate` |

怎么测的：

`run_memory_ablation_v2(repetitions=5)` 内部构造 12 个 memory dependency 任务，分成 `fact_lookup`、`edit_dependency`、`history_reference` 三类。每个任务分别跑 `memory_off`、`memory_irrelevant`、`memory_on` 三种 variant。

判断重复读的方式是看 follow-up 阶段是否仍然需要工具读文件确认事实。`memory_on` 时相关事实已经进入可召回记忆，所以工具步数从 1 降到 0，重复读从 60 降到 0。

### 4. 任务恢复机制

简历写法：

> 覆盖 10 个恢复场景，workspace 漂移识别率 100%，且没有出现误信旧状态继续执行的情况。

本次复跑结果：

| 指标 | 值 | 来源字段 |
| --- | --- | --- |
| 恢复任务数 | 10 | `recovery-ablation-v2.json`: `task_count` |
| 每个 variant 运行数 | 30 | 10 个任务 x 3 次 repetition |
| resume enabled 成功率 | 90% | `variants.resume_enabled.summary.resume_success_rate` |
| stale reanchor 率 | 100% | `variants.resume_enabled.summary.stale_reanchor_rate` |
| workspace 漂移识别率 | 100% | `variants.resume_enabled.summary.workspace_drift_detection_rate` |
| false accept 率 | 0% | `variants.resume_enabled.summary.resume_false_accept_rate` |

怎么测的：

`run_recovery_ablation_v2(repetitions=3)` 构造 10 个恢复相关任务，覆盖 checkpoint resume、partial stale、workspace mismatch、schema mismatch、partial success recovery 等类别。每个任务跑 `resume_enabled` 和 `resume_disabled` 两种 variant。

注意：不是 10 个全是 workspace 漂移。workspace 漂移是其中一个子类。本次复跑里 drift 子场景有 2 个 task，每个重复 3 次，共 6 次；6 次都检测到漂移，所以漂移识别率是 100%。`false_accept_rate=0%` 表示没有出现 checkpoint 已经过期但系统仍误信旧状态继续执行的情况。

更严谨的简历写法：

> 覆盖 10 个 checkpoint / resume 恢复场景，其中 workspace 漂移子场景 6/6 被识别，false accept 为 0。

### 5. 工具安全与运行治理

简历写法：

> 在固定回归任务中保持 100% 通过率、100% 预算内完成率和 100% verifier 通过率。

本次复跑结果：

| 指标 | 值 | 来源字段 |
| --- | --- | --- |
| 固定任务数 | 12 | `harness-regression-v2.json`: `summary.total_tasks` |
| 通过数 | 12 | `summary.passed` |
| 失败数 | 0 | `summary.failed` |
| 通过率 | 100% | `summary.pass_rate` |
| 预算内完成数 | 12 | `summary.within_budget` |
| 预算内完成率 | 100% | `summary.within_budget_rate` |
| verifier 通过数 | 12 | `summary.verifier_passes` |
| verifier 通过率 | 100% | `summary.verifier_pass_rate` |

怎么测的：

`run_harness_regression_v2()` 读取 `benchmarks/coding_tasks.json` 的 12 个固定任务。每个任务复制一份 fixture workspace，用 deterministic scripted model output 跑 agent，再用每题自己的 verifier 命令检查最终工作区和运行工件，不只看模型最终回答。

这组任务覆盖 README patch、无效 patch 恢复、路径逃逸恢复、重复读恢复、context reduction checkpoint、freshness reanchor resume、workspace mismatch resume、durable memory promotion accept/reject 等场景。

### 6. 评测与审计闭环

简历写法：

> 将评测拆成 harness regression、上下文治理、记忆收益和恢复正确性几层。

对应产物：

| 层 | 产物 |
| --- | --- |
| harness regression | `harness-regression-v2.json` |
| 上下文治理 | `context-ablation-v2.json` |
| 记忆收益 | `memory-ablation-v2.json` |
| 恢复正确性 | `recovery-ablation-v2.json` |
| 汇总报告 | `pico-benchmark-core-report.md` |

这层的重点不是一个总分，而是把不同问题分开测：runtime 合同稳定性、上下文模块收益、记忆模块收益、恢复边界正确性分别有独立证据。

## 面试口径建议

如果被问“这些数据怎么来的”，可以这样答：

> 这些不是线上用户数据，是我为了验证 agent harness 的几个关键模块设计的固定 benchmark 和 ablation 实验。固定回归任务用 verifier 检查最终工作区状态；上下文实验用 12 组压力配置对比 prompt 压缩前后字符数；记忆实验对比 memory on/off 下 follow-up 是否重复读文件；恢复实验覆盖 checkpoint、partial stale、workspace mismatch、schema mismatch 等场景，并统计 drift detection 和 false accept。每次运行都会落 `task_state.json`、`trace.jsonl`、`report.json`，所以不是只看模型最后说完成了。
