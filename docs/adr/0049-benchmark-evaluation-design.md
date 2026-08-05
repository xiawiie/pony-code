# ADR-0049：以产品决策为中心的 Benchmark 与 Evaluation 设计

- 状态：Accepted；8-task pilot 与 comparison runner 已实现，live pilot 证据单独记录
- 日期：2026-08-04
- 分析基线：`1411f62206d60d75d11b304edca083afb6e8b01a`

## 1. 为什么重写这份设计

上一版设计的主要问题不是缺少 task、grader、指标或 runner，而是顺序反了：它先设计了测量设施，之后才尝试解释这些
数字能说明什么。这样得到的结果即使是 8/8、100% 或更快，也不能回答下面任何一个产品问题：

- 这个 Pony 代码变更应不应该合并或发布？
- 这个 Provider/model 是否适合作为 coding target？
- Context、compaction、Memory 等能力是否真的改善了用户结果？
- 失败发生在仓库理解、修改、验证、安全策略、Provider，还是持久化环节？

因此，本 ADR 不再把“建一个 benchmark 框架”当作目标。新的原则是：

> **先声明要做的产品决策和可证伪假设，再选择能减少该决策不确定性的 benchmark。**

若一次运行没有预先声明决策、假设、对照、主要指标、护栏和判定规则，它只能称为一次 benchmark run，不能称为完整的
evaluation，也不能支持能力、回归或发布结论。

## 2. 决策

Pony 保留已有 verification、fixed harness、Memory、performance 和 live Provider 设施，但重新限定它们能够回答的问题。
新增的 coding benchmark 只服务于明确的产品决策，不建设公共排行榜、通用 agent 竞技场或第二套运行时框架。

本设计采用以下顺序：

1. 定义 Pony 希望改善的唯一用户结果；
2. 列出需要证据支持的产品决策；
3. 为每个决策写 Evaluation Brief 和可证伪假设；
4. 从产品承诺和真实维护任务中构造目标任务分布；
5. 先证明 benchmark 自身有效，再用它比较 Pony 或 Provider；
6. 把观察结果映射为明确行动，而不是只输出分数。

## 3. Benchmark 与 Evaluation 的区别

| 概念 | 本设计中的含义 |
| --- | --- |
| Verification | 确定性验证产品合同和发布条件；失败即阻断 |
| Benchmark | 冻结的目标任务分布、执行协议和 grader；负责产生可比较测量 |
| Evaluation | 带着预先声明的产品问题、假设、对照、主要指标、护栏和决策规则运行 benchmark |
| Task | 一个用户目标、一份 fresh fixture、预算和一组模型不可见的 grader |
| Trial | 某个 task 在 fresh workspace、Session、Run 和 Provider client 上的一次执行 |
| Hard gate | 不能被正确率、速度或成本抵消的安全、完整性或耐久性条件 |
| Qualification | 在 benchmark 支持产品结论前，证明 task 和 grader 有效、有区分度且可重复 |

Benchmark 是测量工具，不自动产生结论。Evaluation 是实验和决策过程。例如，同一个 coding benchmark 可以用于：

- 比较 parent commit 与 candidate commit；
- 比较同一 commit 上的两个 Provider/model；
- 做 Context enabled/disabled 的 ablation；
- 复现某类 failure 并定位原因。

这些实验的对照和判定规则不同，不能用一个默认“总分”代替。

## 4. 唯一 North Star：Safe Correct Completion

Pony 的产品承诺不是“调用了多少工具”或“测试输出看起来合理”，而是：

> **在受信代码仓库中完成正确、范围受控、经过验证、可恢复的代码变更，同时不突破 permission、path、secret、
> persistence 和 Provider binding 边界。**

核心 outcome 定义为 **Safe Correct Completion（SCC）**。一个 trial 只有同时满足以下条件才记为 SCC：

1. 模型不可见的 target-behavior grader 通过；
2. regression grader 通过，原有行为没有被破坏；
3. scope/integrity grader 通过，没有越界修改、删除 fixture 基础设施或篡改 grader；
4. Agent 正常结束，Session、Run 和 terminal evidence 完整；
5. 在预先冻结的 step、单次输出和 wall-time budget 内完成；
6. 没有 permission、path、secret、workspace identity、Provider binding 或 durability hard-gate failure。

SCC 是一个用户任务是否真正成功的 outcome event，不是加权总分。安全失败不能因为测试通过而被抵消，错误修改也不能因为
速度更快而得分。

### 4.1 诊断指标

SCC 之外只保留能解释结果或支持行动的指标：

- **3-trial reliability**：每个 task 为 `3/3`、`1–2/3` 或 `0/3`；
- **self-verification rate**：最后一次 mutation 后是否执行并通过了 task-appropriate verification；
- **成功条件下的效率**：仅对 SCC trial 比较 tool steps、tokens、wall time 和可用时的费用；
- **failure category**：discovery、diagnosis/edit、verification、tool policy、context loss、transport、persistence、grader；
- **状态证据**：compaction、resume、Memory、Plan 或 worktree workflow 是否保持预期状态。

效率只能在成功条件下解释。更快失败、少用 token 但未完成任务，都不是改善。

## 5. Evaluation 必须支持的四类产品决策

### 5.1 D1：是否合并或发布某个 Pony 代码变更

**问题**：candidate commit 是否改善了目标能力，同时没有破坏其他能力或安全边界？

**实验**：

- baseline 为 parent 或当前已接受 commit，candidate 为待评估 exact commit；
- 使用相同 corpus、grader digest、Provider/protocol/model、预算和 trial 数；
- baseline/candidate trial 交错运行，降低 Provider 随时间漂移的影响；
- 目标 slice 与非目标 guardrail slice 在运行前冻结。

**想看到的结果**：

- 目标 slice 的 SCC 有预先声明的改善；
- 没有非目标 task 从稳定通过 `3/3` 变成稳定失败 `0/3`；
- deterministic verification 和 harness 全通过；
- safety/durability hard gate 零失败；
- 若完成率相同，成功 trial 的效率没有超出预先声明的容忍范围。

**决策**：只有目标改善和所有 guardrail 同时满足，才能接受“能力改善”的声明。若 deterministic gate 或任一 safety hard
gate 失败，直接拒绝；代码 feature evaluation 应冻结 `provider_transport_failure` 为 inconclusive，避免把 Provider outage 归咎于
candidate。Grader 无效或 provenance 不一致同样只能得到 inconclusive，不得补写成通过。

### 5.2 D2：某个 Provider/model 是否适合作为 coding target

**问题**：在固定 Pony commit 上，该 Provider/protocol/model 是否既能可靠通信，也能完成目标 coding task？

**实验**：

- Pony commit、corpus、预算和 trial protocol 固定；
- 每个 Provider/model 使用 production resolver、Transport adapter 和真实 Session/Run 路径；
- live acceptance 与 coding outcome 分开报告。

**想看到的结果**：

1. live transport、usage、persistence 和 model binding gate 通过；
2. core coding tasks 有足够的 SCC 和 3-trial reliability；
3. failure 不是集中在 timeout、malformed response 或 transport；
4. 在成功条件下，token、时间和费用处于可接受范围。

**决策**：

- “transport supported”只表示协议和最小真实会话可用；
- “recommended for coding”还必须满足预先声明的 SCC/reliability 门槛；
- 结果按质量、可靠性、时间和费用分别报告，以 Pareto 结果支持选择，不合成一个排名分数；
- 一个 Provider/model 的通过不能外推到其他 endpoint、protocol 或 model。

### 5.3 D3：某个 feature 是否真的改善用户结果

**问题**：Context/RepoMap、compaction、Memory、Plan 或 worktree agent 等能力是否带来可观察的用户价值？

单独运行 feature-enabled 版本不能回答这个问题，必须有 A/B 或 ablation：

| Feature | 对照 | 主要假设 | 想看到的信号 | Guardrail |
| --- | --- | --- | --- | --- |
| Context/RepoMap | enabled vs disabled/previous | 更快找到正确责任模块 | navigation slice SCC 上升，无效读取和重复搜索下降 | 非导航 task SCC 不下降 |
| Compaction | fixed vs previous behavior | 长上下文后仍保留完成任务所需事实 | compaction/resume SCC 保持或上升，request size 下降 | 不产生 stale action、重复 mutation 或状态丢失 |
| Memory | recall enabled vs disabled | 后续任务能利用已授权事实 | follow-up SCC 上升，重复探索下降 | false recall、cross-scope 泄露和 prompt injection 不增加 |
| Plan | plan workflow vs direct execution | 高风险多文件任务的范围和验证更完整 | multi-file SCC/self-verification 上升 | 不能绕过 permission 或执行未批准 revision |
| Worktree agents | delegated vs single-agent | 可并行分解的任务更可靠或更快 | batch outcome 改善且 child evidence 完整 | parent 不被隐式修改，merge/cleanup 仍 fail closed |

这里的 disabled/previous 必须是可解释的真实对照，不允许为了制造差异而构造明显残缺版本。

**决策**：若目标用户结果没有改善，即使 feature 的内部指标“工作了”，也不能声称它有产品价值。例如 recall 命中率提高但
follow-up SCC 不变，只能说明检索发生了，不能说明 Memory 帮助完成任务。

### 5.4 D4：失败发生在哪个环节

**问题**：SCC 失败应由哪一层负责，下一步应改什么？

Evaluation 必须保留足够的低敏证据，把失败定位到：

1. repository discovery：没有找到责任模块或相关测试；
2. diagnosis/edit：定位正确但推理或修改错误；
3. self-verification：修改后未验证或忽略失败；
4. tool policy：schema visibility、permission 或 path 决策阻止了必要操作；
5. context/state：compaction、resume、Memory 或 stale action 导致事实丢失；
6. Provider/transport：timeout、refusal、malformed response 或 endpoint 问题；
7. persistence/finalization：Session、Run、trace 或 checkpoint 证据不完整；
8. hidden regression：可见目标通过，但隐藏行为或原有合同被破坏。

Failure taxonomy 不是为了做漂亮图表，而是为了避免用错误的修复手段。例如 discovery failure 增加时应检查 Context/RepoMap，
而不是降低 grader 标准。

## 6. 每次运行前必须填写 Evaluation Brief

Decision-driven evaluation 的最小输入必须是 exact-key JSON record，并在看到结果前冻结。第一版只支持当前需要的字段，
不实现通用表达式或 threshold DSL：

```json
{
  "record_type": "evaluation_brief",
  "format_version": 1,
  "decision": "是否合并 candidate",
  "question": "candidate 是否提高目标 slice 的安全正确完成率？",
  "hypothesis": "candidate 减少目标 failure mode",
  "baseline": {"label": "baseline", "commit_sha": "<exact sha>"},
  "candidate": {"label": "candidate", "commit_sha": "<exact sha>"},
  "target_slices": ["failing-test-diagnosis"],
  "primary_metric": "safe_correct_completion",
  "expected_effect": {"min_task_wins": 1, "max_task_losses": 0},
  "guardrails": {
    "forbid_stable_pass_to_fail": true,
    "max_hard_gate_failures": 0
  },
  "trials_per_task": 3,
  "decision_rule": "expected effect 与全部 guardrail 同时满足才接受",
  "inconclusive_conditions": [
    "dirty_worktree",
    "frozen_condition_or_provenance_mismatch",
    "invalid_trial",
    "non_live_provider",
    "provider_transport_failure"
  ]
}
```

Runner 验证字段、类型和最小边界，并要求前四个 fail-closed 条件始终存在。`provider_transport_failure` 是可选条件：评估代码
feature 时通常应预先加入，避免把临时 Provider outage 归咎于 candidate；评估 Provider/model 本身的端到端可靠性时可以省略，
让 transport failure 作为真实产品 outcome 进入 SCC。Comparator 直接执行这些条件、task wins/losses 与 hard-gate 规则，不接受
事后自由文本。若未来某个真实决策无法由这一合同表达，再以新的 format version 增加最小字段，而不是预建规则语言。

### 6.1 示例：compaction 修复

```text
Decision: 是否合并 compaction request accounting 修复
Question: 修复是否提高长上下文任务的安全完成率？
Hypothesis: candidate 会减少 compaction 后的 context-loss failure
Baseline: parent commit
Candidate: current exact commit
Target task slice: stateful/compaction-resume
Primary metric: SCC count per task
Expected effect: 至少 3 个 paired task wins，且 losses 为 0
Guardrails: core coding 无 stable-pass -> stable-fail；safety/durability 零失败
Trials: 每个 task、每个版本 3 次；A/B 交错
Decision rule: expected effect 与全部 guardrail 同时满足才接受能力声明
Inconclusive conditions: Provider outage、invalid grader、fixture/provenance digest 不一致
```

示例中的阈值只属于该次实验，不是全项目默认值。正式门槛应在 qualification 和 pilot 数据之后确定，并在 confirmatory run 前
冻结。看到结果后修改阈值，必须被记录为新的探索性分析，不能继续使用原来的决策声明。

### 6.2 探索性运行与决策性运行

- **Pilot/qualification**：估计 task 难度、方差、预算和 grader 有效性；结果用于修改 benchmark，不能用于宣称 feature
  提升或阻断发布；
- **Confirmatory evaluation**：corpus、grader、阈值和 brief 已冻结；结果可以支持 accept、reject 或 inconclusive。

两者必须在 artifact 和报告中明确区分，避免用调参阶段的最好结果冒充最终结论。

## 7. 目标任务分布从哪里来

Task 数量不能先拍脑袋决定。目标分布应来自 Pony 的产品承诺和真实维护工作。

### 7.1 产品承诺

README 的主要用户工作流包括：

- inspect failing tests and make the smallest safe fix；
- 理解仓库并完成 bounded code change；
- Plan 后实现和验证；
- 长任务中的 compaction、resume 和 Session recovery；
- Memory 的授权保存与后续使用；
- isolated worktree agents；
- 显式且不可 fallback 的 Provider binding。

因此 task 不能只测 README/sample 文本替换，也不能只验证 Agent 是否按 scripted output 调用了工具。

### 7.2 真实维护历史

对分析基线前最近 80 个 non-merge commits 的审计显示：

- 73/80（91%）触碰测试；
- 41/80（51%）跨至少两个 runtime domain；
- 39/80（49%）触碰 README、docs、CHANGELOG 或规则合同；
- 高频领域包括 CLI、runtime、tools、state、agent、TUI、security、config 和 providers。

近期真实 failure pattern 包括：

- model/terminal contract；
- failed checkpoint 污染 Memory；
- user turn append 与 compaction request accounting；
- stale tool action、并发和 timeout；
- crash-safe worktree merge；
- non-UTF8 search 与 bounded subprocess output；
- Provider response 和 Session snapshot binding。

这些历史数据的用途是提炼任务形态和风险，不是把 Pony 自己的历史 patch 原样放入公开 fixture。Fixture 必须重写名称、布局、
数据和答案，避免直接复制仓库历史解决方案。

### 7.3 明确的代表范围

初始 benchmark 只声明测量：

> **受信、小到中型、离线 Python repository 中的 bounded maintenance task。**

它不代表：

- 所有编程语言；
- 大型 monorepo；
- 需要公网或外部服务的任务；
- 恶意仓库或 OS sandbox 隔离能力；
- 开放式产品设计、美学判断或长期 autonomous development。

报告必须重复这个外推边界，不能把 12 个 Python fixture 的结果写成“通用软件工程能力”。

## 8. Core Coding corpus 的目的与构成

Pilot 使用 4 个用户工作流、每类 2 个 task，共 8 个 task。Qualification 稳定后，每类扩展到 3 个，共 12 个正式 core
tasks。数量来自最小覆盖，而不是为了制造更大的分母。

| Workflow slice | 用户问题 | Benchmark 想观察什么 | 主要 grader |
| --- | --- | --- | --- |
| failing-test diagnosis and fix | 能否从失败证据定位根因并做最小修复 | target SCC、regression preservation、最后 mutation 后验证 | visible failure + hidden edge cases |
| issue-driven navigation + latent edge case | 没有现成失败命令时能否找到责任模块 | discovery path、无效读取、latent behavior | hidden target + trace diagnostics |
| bounded multi-file feature/contract change | 能否一致修改实现、调用方和合同 | multi-file SCC、scope、回归、自验证 | behavior + regression + integrity |
| I/O/config/CLI hardening | 能否在信任边界上 fail closed 且保持兼容行为 | invalid-input behavior、无越界副作用、错误 envelope | adversarial inputs + regression |

每个 task 至少包含：

- 一个独立、可读的用户目标；
- 一份 immutable source fixture 和 fresh working copy；
- 明确的 allowed tool/policy 和预算；
- target、regression、scope/integrity 三类 grader；
- 一份不提供给 Agent 的 reference solution；
- 一个 task 作者预先写明的 failure mode；
- fixture、prompt、grader 和 reference digest。

Task prompt 可以要求“运行合适的测试”或说明用户可见失败，但不能包含 hidden verifier 的完整命令、测试内容或答案。

### 8.1 为什么 stateful workflow 不混入 core 完成率

Plan-to-implementation、compaction/resume、Memory follow-up 和 worktree delegation 的 trial unit 是多 turn、跨状态或多 workspace，
与单次 coding task 的成本和失败面不同。它们应作为独立 stateful slices 报告：

- `stateful/plan-implementation`；
- `stateful/compaction-resume`；
- `stateful/worktree-delegation-review`；
- `stateful/memory-follow-up`。

不能把这些结果简单混入 core SCC 百分比，否则 corpus 权重会替代产品判断。Feature evaluation 根据 brief 选择相关 slice。

## 9. Benchmark 自身必须先通过 Qualification

在 benchmark 被用于合并、发布或 Provider 推荐前，每个 task 必须证明它能测到目标能力，而不是 grader 偶然或 harness 错误。

### 9.1 单 task qualification

1. 原始 broken fixture 必须稳定失败；
2. reference solution 必须在 fresh copy 上连续 5/5 通过；
3. grader 必须 deterministic、bounded、offline，重复执行结果一致；
4. no-op agent 必须失败；
5. 修改 grader、测试基础设施或禁止路径必须被 integrity grader 拒绝；
6. grader 不依赖执行顺序、残留 cache、绝对路径或开发机环境；
7. task prompt 不泄露 hidden grader；
8. failure mode 和 reference solution 经过独立人工审查。

Pilot 还固定检查“公开失败证据是否符合任务形态”：`failing-test-diagnosis` 的 broken fixture 必须运行公开测试失败；其余
issue-driven、contract 和 hardening task 的 broken fixture 公开回归应通过，使 hidden target 真正测量导航、latent contract 或
边界推理，而不是把所有 task 都退化为照着失败测试改代码。Reference overlay 必须使 target、regression 和 public grader 连续
`5/5` 通过。

### 9.2 Offline qualification 与 live qualification 分层

Offline qualification 只证明 schema、trusted path、fresh copy、broken/reference/no-op、grader determinism 和 integrity 合同。它可以
使用 Fake Provider 验证 runner plumbing，但不能形成 Q/SCC coding capability 结论。

Live condition runner 必须先读取 qualification artifact，并验证其 exact-key schema、8 个 task 的 qualification evidence、零失败、
corpus digest 和 grader digest 与当前 runner 加载的 benchmark 完全一致。验证发生在 Provider resolution 之前；缺失或过期 artifact
直接 fail closed，避免对未证明有效的 corpus 发起收费请求。

之后至少使用一个已通过 live acceptance 的真实 Provider/model 运行 live pilot，并检查 task 是否有区分度：

- 若所有合格 Provider/model 都 `3/3`，task 已到 ceiling：保留少量 must-pass canary，其余提高真实难度；
- 若所有合格 Provider/model 都 `0/3`，task 可能歧义、预算不足或 grader 无效：先审查 benchmark，不能直接保留为“难题”；
- 期望 corpus 同时包含稳定通过、混合结果和稳定失败，以区分回归、改善和能力边界；
- task 之间不能共享可被前一 trial 污染的 workspace、Session、client 或 cache。

### 9.3 Invalid evaluation 与产品失败的边界

以下情况属于 **invalid trial/evaluation**，不进入 SCC 分母，也不能形成决策：

- runner 自身崩溃且与被测 Pony 行为无关；
- fixture/grader digest 不匹配；
- hidden grader 无法启动或 qualification 后出现非确定性；
- baseline/candidate 使用了不同 corpus、预算或 Provider target；
- provenance 缺失到无法确认被测对象。

以下情况属于 **产品 task failure**，必须计为 SCC 失败：

- Provider timeout、refusal、malformed response 或真实 transport failure；
- Pony 超出预算、没有 final、工具调用错误或 mutation 未完成；
- Session/Run/trace 持久化失败；
- permission/path/secret/identity 边界实际失守、effect 无法确认，或拒绝后未能完成任务；
- hidden target 或 regression grader 失败。

不能把产品可靠性问题包装成 benchmark infrastructure error 从分母中删除。

安全策略成功拒绝一次不可信 executable、secret/path 访问或未授权动作，本身不是 hard-gate failure：边界没有失守，并且这正是
fail-closed 的预期行为。若 Agent 随后安全正确完成任务，该事件只记录为 `policy_rejections` 诊断指标；若拒绝导致任务无法完成，
则由 target/finalization/budget 等 outcome 产生 SCC failure。`workspace_effect_unknown`、scope integrity failure 和 durability failure
仍是不可抵消的 hard gate。

## 10. Trial 协议

### 10.1 冻结条件

一次 confirmatory evaluation 必须冻结并记录：

- baseline/candidate exact commit；
- dirty state；
- Python/Pony/platform；
- task corpus 和 grader digest；
- Provider、protocol、model、endpoint hash 和 resolution source；
- permission mode、visible tool schemas 和预算；
- trial count、顺序和 Evaluation Brief；
- usage 是否可用，而不是伪造缺失 token 数据。

Provider 解析继续复用唯一 `.env` 配置面和 production resolver。Benchmark 不拥有第二套 Provider detection、认证字段或失败
fallback。Core coding task 使用 production permission rule 显式设置 `run_shell=allow`，仍经过 schema、command policy、path、secret
和 mutation lock；不启用 `bypassPermissions` 或 dangerous bypass capability。

### 10.2 Fresh trial 生命周期

每个 trial：

1. 从受信 source fixture 创建 fresh temporary working copy；
2. 建立 fresh Provider client、Session 和 Run；
3. 冻结 top-level permission mode、rules、tool schemas 和预算；
4. 通过 production `Pony` 路径执行用户目标；
5. 等待 Agent 完整退出并完成 durable finalization；
6. 从 benchmark 外部位置注入或调用 hidden grader；
7. 运行 bounded target、regression 和 integrity checks；
8. 读取 canonical low-sensitivity evidence，生成 trial result；
9. 销毁 working copy，不复用任何 trial 状态。

复制后、Provider client 创建前必须重新计算 source snapshot；若与 task manifest 冻结的 fixture snapshot 不一致，该 trial 标记为
invalid，不允许继续执行或把 source drift 归因于模型。

Hidden grader 的内容和完整命令在第 6 步前不能存在于 Agent 可读 workspace 或 model prompt 中。

### 10.3 Condition runner 与比较顺序

一个 Python 进程不能通过 checkout 或动态 import 同时测量两个 commit。正确协议是：

1. baseline 和 candidate 各自在自己的 clean exact-HEAD worktree 中运行同一个 condition runner；
2. 每个 condition 独立产生冻结、低敏 artifact；
3. 纯 artifact comparator 验证 commit、corpus/grader digest、Provider target、预算和 trial 数；
4. 任一 frozen condition 不同则结论为 `inconclusive`，不得比较；
5. comparator 不执行 Agent、不切换 Git 状态，也不读取两个 worktree 的运行时代码。

Comparator 不信任 artifact 中的汇总计数：它对 condition、benchmark、Provider、protocol、task、trial、outcome 和 summary 做
exact-key 校验，从 trial 重算每个 task 的 SCC count 和 condition summary。Scripted Provider、dirty worktree、invalid trial、非 live
claim 或内部计数不一致都不能产生 `accept`。

为降低 Provider 时间漂移，操作者应按 task 交错启动两个 worktree 的独立 trial，例如：

```text
task-1 baseline-1, candidate-1, candidate-2, baseline-2, baseline-3, candidate-3
```

具体顺序可由 brief 外的冻结执行记录管理，但两组必须共享相同时间窗口和顺序策略。不能先跑完所有 baseline，数小时后再跑
candidate，然后把 Provider 漂移解释成代码效果。

### 10.4 Trial 次数

- runner/unit smoke：1 次，只证明 plumbing；
- qualification：reference/no-op 5 次，真实 Provider 至少 3 次；
- confirmatory comparison：每个 task、每个 condition 默认 3 次；
- 若 pilot 显示方差过高，应先修 task 或提高 trial 数，再冻结决策规则。

单次真实运行只能写为 observation，不能写为稳定能力结论。

## 11. Grading 原则

### 11.1 三层 outcome grader

每个 coding task 至少拆分为：

1. **Target behavior**：用户要求的行为是否实现；
2. **Regression preservation**：原有公共行为和失败合同是否保留；
3. **Scope/integrity**：修改是否在允许范围，测试/grader/fixture 基础设施是否未被篡改。

三层全部通过才可能形成 SCC。不能用“通过了用户指定的一个测试”替代隐藏 edge case 和回归检查。

### 11.2 Trace 只用于诊断可观察行为

Trace 可以测量：

- 是否先读后改；
- 最后 mutation 后是否运行验证；
- 重复 search/read 数量；
- tool denied、retry、compaction 和 transport 次数；
- durable finalization 是否完整。

Trace 不评分最终回答的文风，也不要求唯一的工具调用顺序。只要多个路径都能安全正确地完成任务，就都应通过。

### 11.3 不使用 LLM judge

初始目标都能用 deterministic behavior grader 表达。LLM judge 会引入额外费用、模型漂移、prompt 敏感性和第二个需要被校准的
Provider。只有未来出现无法用行为或结构约束表达、且确实影响产品决策的任务时，才单独设计 judge qualification；当前不做。

## 12. 结果如何汇总

### 12.1 必须报告的结果

每个 task/condition 报告：

- `SCC trials / valid trials`；
- reliability：`3/3`、`1–2/3`、`0/3`；
- target/regression/integrity/finalization/budget/hard-gate 分项；
- self-verification；
- failure category；
- 被安全阻止且最终恢复的 `policy_rejections` 类型与次数；
- SCC trial 的 steps、tokens、wall time、费用或 usage unavailable 状态。

Comparison 额外报告：

- 每个 task 的 baseline SCC count 与 candidate SCC count；
- task wins/losses/ties；
- stable-pass -> stable-fail 和 stable-fail -> stable-pass 列表；
- 目标 slice 与 guardrail slice 分开汇总。

Pilot 规模较小时优先报告原始计数和 task-level 结果，不制造小数点后多位的统计精确度。置信区间可以作为补充，但不能替代
预先声明的 decision rule。

### 12.2 禁止单一综合分数

不把 SCC、安全、速度、token、费用和主观质量加权成一个分数，原因是：

- 权重没有稳定的产品含义；
- 安全失败可能被更快速度抵消；
- 不同决策需要不同 guardrail；
- 综合分掩盖 failure localization。

报告可以展示多维 Pareto 结果，但首页必须先给决策结论，而不是排行榜。

## 13. 结果必须转化为行动

| 观察 | 结论或下一步行动 |
| --- | --- |
| 目标 slice SCC 上升且 guardrail 全通过 | 支持该 brief 中的能力改善声明 |
| SCC 下降且 discovery failure 增加 | 检查 Context/RepoMap/search，不降低 grader 权重 |
| SCC 不变但成功 trial tokens/steps 上升 | efficiency regression；检查上下文膨胀和重复 tool use |
| hidden grader 通过但 self-verification 下降 | Agent 验证行为退化；不能宣传“更安全” |
| compaction 前成功、后失败 | compaction/context preservation 回归 |
| policy rejection 增加但 SCC 不变 | 检查模型工具选择和 schema；不要把 fail-closed 误报为安全失败 |
| policy rejection 导致 SCC 下降 | 检查 schema visibility、permission rule 或 tool policy 是否阻断必要路径 |
| Provider/transport failure 增加 | Provider reliability 问题，不归因于 coding reasoning |
| 任一 path/secret/identity/persistence hard gate failure | 阻断合并/发布；不能被平均完成率抵消 |
| 全部 task 都 `3/3` | benchmark ceiling；保留少量 canary，其余升级难度 |
| 全部 task 都 `0/3` | benchmark、预算或 prompt 可能无效；先 qualification，不归咎模型 |
| feature 内部指标改善但 SCC 不变 | 不能声称用户价值；决定简化、继续研究或移除 feature |
| baseline/candidate provenance 不一致 | inconclusive；修复实验后重跑 |

报告首页固定顺序：

1. 要做的决策；
2. 结论：accept、reject 或 inconclusive；
3. 支撑结论的主要证据；
4. guardrail 和 hard-gate 状态；
5. 外推边界与未执行条件；
6. 之后才是逐 task 指标和诊断。

## 14. 现有设施的目的重新划分

Pony 已经存在多套有价值的设施。问题是结论范围混杂，而不是必须推倒重写。

| Family | 现有入口 | 唯一目的 | 支持的决策 | 明确不能声称 |
| --- | --- | --- | --- | --- |
| V：Release verification | [`scripts/check.sh`](../../scripts/check.sh) | exact HEAD 的静态、测试、构建、归档和 clean-install 合同 | 是否具备离线发布条件 | 真实模型 coding 能力 |
| H：Harness regression | [`fixed_benchmark.py`](../../benchmarks/evaluation/fixed_benchmark.py) | scripted Provider 下 Agent/Tool/Session/verifier 管道是否稳定 | harness 改动是否破坏确定性合同 | 真实 Provider 能否理解和修复代码 |
| Q：Coding outcome | 本 ADR 提议新增 | 真实 Provider 在目标 coding 分布上的 SCC 和 reliability | commit、Provider 或 feature 比较 | 通用软件工程能力或公共排行榜 |
| M：Memory quality | [`run_benchmark.py`](../../benchmarks/memory_quality/run_benchmark.py) | recall/search/update/noise/safety 合同 | Memory 行为是否正确；ablation 是否值得继续 | 单独证明 coding task 更成功 |
| P：Performance | [`harness.py`](../../benchmarks/perf/harness.py) | 无公网噪声的本地热点和预算 | 是否存在本地性能回归 | 更快即更正确 |
| L：Live acceptance | [`run_live_session.py`](../../benchmarks/live_e2e/run_live_session.py) | 指定 Provider target 的最小真实 transport/security/persistence 验收 | 该 target 是否具备 live 支持证据 | 其他 Provider/model 或复杂 coding 能力 |

[`scripts/evaluation/evaluate.py`](../../scripts/evaluation/evaluate.py) 是 orchestration，不应因为能同时启动多个 suite 就被解释为一个
具有统一语义的总分 evaluation。每个 suite 的结论仍由对应 brief 和证据边界决定。

### 14.1 当前 fixed benchmark 的正确解释

当前 [`coding_tasks.json`](../../benchmarks/coding_tasks.json) 有 8 个 scripted tasks：

- documentation 2；
- text-edit 2；
- tool-boundary 3；
- session 1；
- 可见工具只有 `read_file` 和 `patch_file`；
- 默认输出由 Fake Provider 脚本驱动；
- task prompt 会把 verifier command 提供给模型路径。

因此 fixed benchmark 的 8/8 通过有明确但有限的价值：它证明 Agent、Tool、Session 和 verifier plumbing 按冻结脚本工作。
它不能证明真实 Provider 能发现责任模块、诊断根因、生成修复或主动验证。新的 Q suite 不替换 H；二者回答不同问题。

## 15. 最小实现设计

实现只增加 `benchmarks/coding_quality/`、聚焦测试和验证文档，不修改 runtime。它复用 production `Pony`、Session/Run store、
permission rule、Provider resolver、transport factory、hardened subprocess 和 observability 路径；未接入默认 `scripts/check.sh` 的收费
live suite，也未增加第二套 evaluation orchestration。

### 15.1 Phase 0：先修正结论语言

- 在文档和 report 中把 fixed benchmark 明确标为 harness regression；
- 禁止把 Fake Provider pass rate 写成 coding capability；
- 保持原 benchmark 和 baseline 格式不变，不为了改名重写设施。

状态：已完成。现有 V/H/M/P/L 的目的和非结论范围可从本 ADR 与验证文档直接看出。

### 15.2 Phase 1：8-task qualified pilot

新增最薄的开发资产：

```text
benchmarks/coding_quality/tasks.json
benchmarks/coding_quality/run_benchmark.py
benchmarks/coding_quality/fixtures/**
benchmarks/coding_quality/graders/**
tests/test_coding_quality_benchmark.py
```

实现只负责：

- exact-key schema、trusted paths 和 bounded input；
- fresh workspace/Session/Run/client；
- production `Pony` 和共享 Provider resolver；
- post-run hidden grader；
- SCC、failure category、provenance 和低敏 summary；
- runner 单元测试中的 Fake Provider，仅测试 plumbing，不作为 Q 结果。

不修改 `pony/`，不新增 runtime dependency，不建立 Provider registry、数据库、dashboard 或 LLM judge。

状态：离线实现与 qualification 已完成；真实 Provider pilot 发现当前小型 corpus 对所测 live target 接近 ceiling，因此它只保留
为 runner/canary pilot，**不得用于 confirmatory feature/model 比较**。这一结果不是“benchmark 失败后没有价值”，而是 qualification
成功阻止了一个低区分度 corpus 被误用。

### 15.3 Phase 2：支持真正的 comparison evaluation

- runner 接受并在 artifact 中冻结 Evaluation Brief；
- baseline/candidate 各自在 exact-HEAD worktree 产生 condition artifact；
- 纯 comparator 拒绝 provenance、corpus/grader、Provider、预算或 trial count 不一致；
- target slice 与 guardrail slice 分开；
- report 首页输出 accept/reject/inconclusive 和依据；
- 可选接入 `evaluate.py`，但不加入默认 `scripts/check.sh`。

状态：runner、Evaluation Brief、condition artifact 和纯 comparator 已实现。能否回答某个真实 D1/D3 问题仍取决于在结果前冻结
brief，并分别从 baseline/candidate clean exact-HEAD worktree 取得匹配的 live artifacts。

### 15.4 Phase 3：用真实维护历史替换 ceiling tasks，再按决策扩展

- 保留 1–2 个稳定 canary，其余优先替换为从 Pony issue/修复历史抽取的 multi-file、stateful、policy-aware task；
- 每个新 task 记录来源 failure mode，但不把原修复 diff 或隐藏断言暴露给模型；
- 仅在有明确 feature 决策时增加相关 stateful task；
- 重新运行 offline qualification 与至少 `3` 次 live pilot；只有出现可解释的稳定通过、混合结果和稳定失败分布，才冻结 confirmatory
  trial 数和 decision threshold；
- 基线更新必须人工审查 task/grader diff 和 provenance，不自动覆盖。

Task 数量不是长期 KPI。当前行动是提高代表性和区分度，不是为了凑 12 个而加入更多低价值文本题。

## 16. Artifact、隐私与安全边界

Shareable summary 只保留支持决策所需的低敏字段：

- brief、exact commit、corpus/grader digest；
- Provider/protocol/model 和 endpoint hash，不含完整 API base；
- SCC 分项、failure category、预算和 usage status；
- 相对 task id，不含本机绝对路径；
- 不含 prompt、Provider reasoning、完整 stdout/stderr、header、Key 或 live response。

Raw artifact 可以保存 bounded trace、grader stdout/stderr 和 workspace diff，但必须 private、默认不提交，并经过现有 redaction 和
atomic-write 路径。Evaluation artifact、cache、fixture working copy 和 `.pony/` 不进入 distribution。

Host 不是 OS sandbox。Q suite 只运行受信、离线 fixture 和 grader；fresh workspace 与 hidden grader 隔离不构成恶意代码隔离承诺。

真实 Provider 请求属于收费 G8。只有用户对当轮 target 和费用明确授权后才执行。离线 Fake Provider、旧 SHA 或 contract test 不能
冒充 live 结果。

## 17. Definition of Done

本设计落地完成时，应能用证据回答“为什么跑、想看到什么、结果后做什么”：

- 每次 confirmatory run 都有运行前冻结的 Evaluation Brief；
- North star 是 SCC，安全和 durability 是不可抵消的 hard gates；
- core corpus 来源、代表范围和非代表范围明确；
- 每个 task 通过 broken/reference/no-op/determinism/integrity qualification；
- baseline/candidate 或 A/B 使用相同 frozen conditions 和交错 trials；
- 真实 Provider coding 结果与 scripted harness 结果严格分开；
- 报告先给决策和行动，再给指标；
- failure taxonomy 能把结果映射到责任环节；
- 效率只在 SCC 条件下解释；
- 不存在综合分、LLM judge、第二 Provider 配置面或 fallback；
- Q 开发资产不回迁 `pony/`，不进入 wheel/sdist；
- PR/default check 零网络，收费 live 只在明确授权后运行；
- 结果始终记录 exact commit、target、corpus/grader digest、trial 数和未执行条件。

## 18. 被拒绝的替代方案

### 18.1 先建一个通用 benchmark framework，再决定测什么

拒绝。这正是上一版的问题：设施完备不等于结论有价值。先有决策和假设，runner 只实现当前实验所需的最小协议。

### 18.2 直接把 fixed benchmark 的 Fake Provider 换成真实 Provider

拒绝。现有 task 过窄、verifier 可见且 action schema 与脚本耦合。直接替换只会同时失去 H 的确定性，又得不到可信 Q 结论。

### 18.3 把整个 Pony 仓库作为每个 task 的 fixture

拒绝作为 pilot。成本和任务间污染过高，grader 难冻结，也容易复制已知历史答案。小型受信 fixture 更适合 qualification 和因果比较。

### 18.4 先规定一个固定 task 数或统一 pass-rate 门槛

拒绝。Task 数应由目标工作流覆盖和区分度决定，门槛应由具体决策、风险和 pilot 方差在运行前确定。

### 18.5 用单一综合分或公共排行榜体现价值

拒绝。它会掩盖 hard-gate failure，并把 corpus 权重伪装成产品优先级。Pony 需要内部决策证据，不需要模型营销排名。

### 18.6 第一阶段引入 LLM-as-a-judge

拒绝。当前 coding outcome 可用 deterministic grader 表达，引入 judge 只增加漂移、费用和新的校准问题。

### 18.7 新建 benchmark Provider registry、模型 catalog 或配置文件

拒绝。项目已有唯一 `.env` 和 production resolver。Evaluation 必须测真实产品路径，不能建立第二套 target detection。

### 18.8 用 Docker 宣称安全隔离

拒绝。项目明确 Host 不是 OS sandbox。Fresh fixture 解决可重复性和污染问题，不解决恶意代码隔离。

## 19. 当前证据、结论与行动

以下证据采集于 **2026-08-05**，环境为 macOS arm64、Python 3.12.13。当前分支
`codex/benchmark-evaluation-design` 的 Git HEAD 仍是分析基线
`1411f62206d60d75d11b304edca083afb6e8b01a`；benchmark 实现尚未提交，因此 live artifact 明确标记 `dirty: true` 和
`non-confirmatory`，不能冒充 exact candidate commit 的 confirmatory 结果。

### 19.1 Offline qualification：测量工具有效

当前 8-task corpus/grader 的 qualification 结果为 `8/8`：

```text
corpus_digest: sha256:bae5fa80f896ac6e6e37b3e3e623ce8877e5a32713e8649285838548fade1324
grader_digest: sha256:22a1d95711b4799df461b0ec3d9f14575d4930f4e02621fcd0097cc7837e1991
broken target: 每个 task 连续 3/3 失败
reference target/regression/public: 每个 task 连续 5/5 通过
integrity/tamper rejection: 8/8 通过
```

这说明 fixture、reference、hidden grader、公开失败形态和 integrity 合同有效；它不说明真实 Provider 有 coding capability。
旧 qualification artifact 使用 grader 修改前的 digest，review 时已识别为过期并废弃，没有继续作为 live 前置证据。

### 19.2 第一次 live 请求：invalid evaluation

第一次单 task live smoke 的 Provider 请求已经执行，但 runner 在请求结束后才发现临时 manifest 位于 repository 外，无法生成可信
provenance artifact。该次结果属于 **invalid evaluation**，不是模型或产品失败。根因已修复为：manifest 必须在 Provider resolution
前通过 repository-relative、regular-file、single-link 和 bounded 校验；对应聚焦测试已加入。该请求可能已经产生费用，但不进入 SCC
证据。

### 19.3 第二次单 task live smoke：只证明真实路径可工作

修复后，`openai-chat` / `openai_chat_completions` / `qwen3.7-max` 在
`diagnosis-date-boundary` 上得到 `1/1 SCC`：hidden target、regression、integrity、finalization、step/wall budget 全通过，修改后有
成功 shell 验证；5 个 tool steps、6 次 model attempts、22.743 秒、12,239 total tokens，零 dangerous bypass。

该结果只证明 production resolver、Transport、`Pony`、Session/Run、tool policy、hidden grader 和 artifact 路径可以完成一次真实
trial。因为它是 dirty worktree 上的单次 observation，不能证明稳定能力、corpus 区分度或 candidate 改善。

### 19.4 8-task × 3-trial live pilot：拒绝当前 corpus 进入 confirmatory 使用

同一 live target 的 exploratory pilot 共执行 24 个 fresh trials。原始 format-v1 artifact 报告 `15/24 SCC`、10 个 hard-gate
标记；review 逐项检查后发现其中 9 个是 policy **成功拒绝**的事件（8 个 `trusted_executable_missing`、1 个
`sensitive_access_block`），并没有边界失守。把安全拒绝计为 hard gate 会反向惩罚 fail-closed，因此 condition artifact 已升级为
format v2：安全拒绝单独记录为 `policy_rejections`，只有未被安全包含的 security event、scope/integrity、未知 effect 和 durability
失败才是 hard gate。

按修正后的 outcome 语义重算该 exploratory artifact：

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

Task-level corrected SCC 为：

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

format-v2 修复后又执行了 `navigation-nested-merge` 单 task live smoke：hidden target/regression/integrity/finalization 全通过，
`1/1 SCC`，同时独立记录 `policy_rejections: {trusted_executable_missing: 1}`、hard gate 为 0。这验证了“安全拒绝是诊断证据，
不是安全失败”的新合同。

### 19.5 决策

- **Runner/evaluation protocol：接受作为 pilot 基础设施。** 它已经能在请求前 fail closed、区分 invalid/product outcome、冻结 brief、
  产生低敏 artifact，并阻止 dirty/Fake/不匹配条件得到 `accept`。
- **当前 8-task corpus：拒绝用于 confirmatory feature/model comparison。** 7 个 task 为稳定 `3/3`，剩余 task 为 `2/3`，对本次
  live target 接近 ceiling；小型 fixture 也不足以代表 Pony 的真实 stateful、多文件和 policy-aware 维护分布。
- **下一行动：**保留 1–2 个 must-pass canary，其余从 Pony 真实 issue/修复历史构造更难、模型不可见答案的 task；重新做 8/8
  offline qualification 和每 task 至少 3 次 live pilot。只有出现可解释的稳定通过、混合结果和稳定失败分布，才允许冻结首个
  confirmatory Evaluation Brief。

因此本轮没有 baseline/candidate `accept` 结论，也没有必要为凑结果运行虚假的同 SHA comparison。最有价值的执行结果正是：
**评测协议及时发现并拒绝了一个过于容易、且曾错误惩罚安全拒绝的 corpus。**

## 20. Efficiency evaluation：先定义要看到什么

### 20.1 Compaction 的决策目的

决策不是“summary 变短了吗”，而是：**是否在不损失可继续工作状态的前提下，降低后续真实 Provider 总 token 成本。**
因此字符压缩率和 `tokens_before/tokens_after` 只诊断 compaction plumbing；正式结果必须同时看到：

- active goal、当前决策、约束、文件/错误、unfinished work 和 next step 被保留；
- 被 supersede 的决策不重新生效；
- 单次和重复 compaction 后 resume 都能完成同一 follow-up corpus；
- baseline 与 compacted 使用同一 canonical history、Provider target、问题顺序和 output cap；
- summary 请求的 input/output usage 被计入 compacted 总成本；
- usage 完整时才能给 token 与 break-even 结论。

第 `k` 个 follow-up 后：

```text
gross_input_saved(k) = Σ baseline follow-up input - Σ compacted follow-up input
baseline_total(k) = Σ baseline(input + output)
compacted_total(k) = summary(input + output) + Σ compacted(input + output)
net_saved_tokens(k) = baseline_total(k) - compacted_total(k)
break_even_turn = first k where net_saved_tokens(k) >= 0
```

Frozen horizon 为六个 follow-up。每个 paired trial 的 `accept` 要求 baseline SCC、compacted SCC、完整 usage 和 horizon 内
break even 全部成立；compacted SCC 失败或没有净收益为 `reject`；Provider 失败、baseline 无效或 usage 不完整为
`inconclusive`。

默认每个单次/重复 compaction 场景各运行三个 paired trials。场景只在至少两个 trial `accept` 且没有任何 trial `reject` 时
`accept`；任一有效 pair `reject` 就整体 `reject`；其余情况 `inconclusive`。这允许最多一个因 baseline 随机失败、Provider 失败
或 usage 缺失而无效的 pair，但绝不让无效 pair 伪装成收益，也不掩盖 compacted regression。dirty provenance 仍把顶层结果
强制降为 `inconclusive`。先过 SCC guardrail，再解释 token、cache、latency 或费用。

### 20.2 Provider latency 的决策目的

问题是“当前 target 完成可用 action 和任务要等多久”，不是为了制造一个不可观察的 TTFT 数字。生产 adapter 均为非流式并在
完整 body 读取后返回，因此固定声明：

```text
ttft_status = unavailable_non_streaming
```

实际测量固定 short final、read-tool continuation、long context 三类 workload，报告成功 trial 的
`provider_complete_ms`、`time_to_first_action_ms`、`time_to_final_ms` p50/p95，同时报告 success、Provider failure type/count、
retry、transport attempt 和 usage completeness。首 action 指完整非流式响应返回并被 Agent 解码后的第一个 action；不得写成
首 token。成功完成后的内部 Provider failure 也必须保留为失败证据，不能被最终 success rate 吞掉。

一个 repository root 只有一个 canonical `.env` target，因此单次 artifact 不伪造 Provider-to-Provider comparison。不同 target
必须在各自 canonical root 独立执行，再按相同 workload、预算、trial 数和 evidence policy 比较脱敏 artifact。

### 20.3 已实现范围与有意不做

`benchmarks/evaluation/efficiency_evaluation.py` 与
`scripts/evaluation/run_efficiency_evaluation.py` 实现上述 paired compaction 和单 target latency evaluation；离线测试使用
scripted Provider 只验证计费公式、SCC grader、resume/tool plumbing、隐私字段和 decision mapping，不声称模型能力。

本轮不增加 streaming、Provider registry、通用 benchmark framework、LLM judge、dashboard、数据库或综合分。当前 8-task coding
corpus 继续只作 pilot/canary；在它被 Pony 真实 multi-file、stateful、policy-aware 历史任务替换并重新 qualification 前，不运行昂贵
的 baseline/candidate confirmatory comparison。
