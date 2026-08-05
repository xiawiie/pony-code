# Pony 公开记忆评测：LongMemEval、PersonaMem 与 mem0

> 结果冻结日期：2026-08-05。本文采用用户指定的 PersonaMem **43.5%** 中期快照，
> 并把描述性数字与严格配对统计分开。当前证据不是 589 题 Pony/mem0 全量配对报告，
> 因此不能标记为 `publishable=true`。

## 摘要

我为 Pony Agent 已有的 User Notes/BM25 记忆路径实现了公开基准评测适配层，覆盖
LongMemEval-S cleaned V1 和 PersonaMem-v1 32k，并接入固定版本 mem0 OSS baseline。
评测层记录 exact commit、数据集和 prompt 摘要、模型/transport、reader budget、逐题答案、
失败状态、Wilson 区间、paired bootstrap 与 McNemar 检验。

本轮可对外使用的 Pony 数字是：

> **PersonaMem-v1 32k retrieval-augmented adaptation：189/434，accuracy 43.5%，
> 95% Wilson CI 39.0%–48.2%。**

这个数字必须带上 `434-case interim snapshot` 限定。严格相同 Case ID 的 246 题比较为
Pony **39.4%**、mem0 **64.6%**，Pony 相对 mem0 为 **-25.2 percentage points**；
paired bootstrap 95% CI 为 **[-32.1, -18.3] pp**，exact McNemar
`p=9.33e-12`。结果说明当前简单 BM25 memory baseline 明显落后 mem0；项目的主要价值是
建立了公开、可恢复、可审计的评测方法，并用数据定位了下一步优化方向。

## 1. 最终数字表

### 1.1 用户指定的 headline snapshot

| System | Correct / N | Accuracy | 95% Wilson CI | 状态 |
|---|---:|---:|---:|---|
| Pony User-Notes/BM25 | **189/434** | **43.55%** | **[38.96%, 48.25%]** | 中期、事后冻结 |
| mem0 OSS | 159/246 | 64.63% | [58.48%, 70.34%] | 部分 live run |

两行不是相同 Case 集。直接相减为 `-21.09 pp`，只能作为描述性差异，不能解释为 paired
效果，也不能对它做显著性推断。

### 1.2 严格 246-case paired subset

| Pony | mem0 | Delta | Paired bootstrap 95% CI | Win/Tie/Loss | McNemar p | N |
|---:|---:|---:|---:|---:|---:|---:|
| **39.43%** | **64.63%** | **-25.20 pp** | **[-32.11, -18.29] pp** | **13/158/75** | **9.33e-12** | **246** |

这里两侧来自同一个 Pony commit `67626ead32c97805178e2069417978da8596ea5f`，使用相同的
246 个 Case ID、数据集摘要、回答模型、transport、prompt、temperature、reader budget 和 workers，
且两侧都是零 row failure。subset 覆盖 16 个 shared contexts、85 个 prefix boundaries。

它仍是运行过程中形成的 post-hoc partial subset，不是预注册随机样本，所以可以用于工程诊断和带限定的
项目陈述，但不能冒充 PersonaMem 589 题正式 leaderboard 结果。

### 1.3 结果进展与选择性报告控制

| Evidence | Pony result | 用途 |
|---|---:|---|
| 用户指定冻结点 | **189/434 = 43.55%** | 可作为带 N 和 interim 限定的项目数字 |
| 停止收费进程时的续跑文件 | 214/500 = 42.80% | 审计上下文，不应隐藏 |
| 先前完成的 Pony-only run | 249/589 = 42.28% | 敏感性参考；没有完整 mem0 pair |
| 同 commit 严格 paired subset | 97/246 = 39.43% | 与 mem0 159/246 做配对推断 |

因此，**43.5% 可以使用，但不能写成“PersonaMem 全量最终 accuracy”**。更稳妥的公开格式是：

> Pony User-Notes/BM25 在 PersonaMem-v1 32k adaptation 的 434-case interim evaluation 中达到
> 43.5% accuracy（189/434；95% CI 39.0%–48.2%）。

## 2. 被评测系统

### 2.1 Pony User-Notes/BM25

准确名称为：

> **Pony User-Notes/BM25, transcript-to-notes benchmark adapter**

公开 transcript 经 benchmark-only adapter 转为中性的 User Notes，随后复用生产检索链路：

```text
BlockStore -> Retrieval.snapshot() -> recall_candidates()
```

默认返回 top 6 notes，每条最多 1024 tokens，最终由统一 TokenAccounting 截断到 6144 tokens。
这证明的是现有记忆存储和检索路径在公开数据上的表现，不等于生产 Agent 会自动把全部对话提炼成同样的 notes。

### 2.2 mem0 OSS baseline

本轮 baseline 固定为：

- `mem0/memory-benchmarks` commit `4b61c5d31b9c668a12b4f5e78064248a02c82d2b`；
- `mem0ai==2.0.12`；
- `qdrant/qdrant:v1.18.0`；
- `qwen3.7-max` extraction/answer model，temperature 0；
- `text-embedding-v4`，实测 embedding dimension 1024；
- `/search` 取 top 200，再裁剪到同一个 6144-token reader budget。

比较的是两个端到端 memory pipeline，不是单独比较 BM25 与向量检索器。mem0 同时包含结构化 extraction、
embedding、Qdrant retrieval 和其 memory representation，这些差异都是被测系统的一部分。

## 3. 数据集与防泄漏边界

### 3.1 PersonaMem-v1 32k adaptation

- 原数据包含 589 个 multiple-choice questions；
- memory 按 `shared_context_id` 隔离；
- 问题按 `end_index_in_shared_context` 分组；
- 每个边界只写入 `context[previous_end:end_index]`；
- 在当前边界回答完问题后才允许继续写入后续上下文；
- question、options、correct answer 和 question type 不进入 memory；
- 两侧共用 answer model、prompt、temperature 与 6144-token reader budget。

因此该结果应称为 **PersonaMem-v1 32k retrieval-augmented adaptation**，不能与官方 full-context
leaderboard setting 直接混用。

### 3.2 LongMemEval-S cleaned V1

评测框架也支持 LongMemEval-S cleaned V1 的 500 个问题：每题独立 memory root/user，只写入 transcript
的 `role` 与 `content`，不把答案、answer session IDs、question ID 或 question type 泄漏给 memory，
回答后再走独立 LLM judge。

本轮没有形成 Pony/mem0 的 500 题正式配对结果，因此本文不报告 LongMemEval headline accuracy。
已有 smoke artifacts 只能证明链路可运行，不能作为公开质量数字。

## 4. 协议与可审计性

冻结协议为 `pony-public-memory-v2`：

- answer/judge temperature 为 0；
- reader budget 为 6144 tokens；
- workers 为 4，只并发独立 case/context；
- 单个 context 内增量写入与问题顺序保持串行；
- 单个模型调用不自动 retry；
- live failure 后依赖逐题 artifact 显式 resume；
- 每个完成行原子落盘，artifact 上限 64 MiB；
- benchmark 共用 Pony 的 Provider resolver 和 transport factory；
- 不向 runtime package 引入 mem0、Qdrant 或其他 benchmark dependency。

正式 `publishable=true` paired report 还要求：两侧同 clean commit、同完整 Case ID 集合、达到 benchmark
expected count、全部已评分、零 failure，并且所有可比 run metadata 一致。本轮 434/246 证据没有满足完整
589-case paired gate，所以分析 artifact 显式标记为 post-hoc、non-publishable-as-full-benchmark。

## 5. 统计方法

### 5.1 单侧 accuracy 与 Wilson interval

Accuracy 为 `correct / scored`。Wilson interval 比简单的正态近似更适合有限样本的二项比例；
434 题 Pony 快照为 43.55%，95% interval 为 38.96%–48.25%。

### 5.2 Paired bootstrap

对相同 Case ID 的逐题差值 `Pony_correct - mem0_correct` 做固定 seed 的 10,000 次有放回重采样。
246 题 delta 为 -25.20 pp，95% interval 完全低于 0，说明差距不只是总体百分比的偶然波动。

### 5.3 Exact McNemar

配对结果中 Pony-only correct 为 13，mem0-only correct 为 75，discordant pairs 为 88。
Exact McNemar `p=9.33e-12`，拒绝两侧错误率相同的假设。这个检验只说明该 subset 上存在稳定差异，
不把 post-hoc subset 自动升级成全数据集结论。

## 6. 分问题类型结果

| Question type | N | Pony | mem0 | Delta | W/T/L | McNemar p |
|---|---:|---:|---:|---:|---:|---:|
| `generalizing_to_new_scenarios` | 27 | 37.0% | 74.1% | -37.0 pp | 2/13/12 | 0.01294 |
| `provide_preference_aligned_recommendations` | 28 | 46.4% | 71.4% | -25.0 pp | 1/19/8 | 0.03906 |
| `recall_user_shared_facts` | 51 | 52.9% | 78.4% | -25.5 pp | 2/34/15 | 0.00235 |
| `recalling_facts_mentioned_by_the_user` | 6 | 0.0% | 33.3% | -33.3 pp | 0/4/2 | 0.5 |
| `recalling_the_reasons_behind_previous_updates` | 40 | 45.0% | 75.0% | -30.0 pp | 1/26/13 | 0.001831 |
| `suggest_new_ideas` | 36 | 16.7% | 38.9% | -22.2 pp | 3/22/11 | 0.05737 |
| `track_full_preference_evolution` | 58 | 39.7% | 56.9% | -17.2 pp | 4/40/14 | 0.03088 |

最明显的差距出现在新场景泛化（-37.0 pp）、更新原因回忆（-30.0 pp）和用户事实回忆（-25.5 pp）。
`recalling_facts_mentioned_by_the_user` 只有 6 题，interval 很宽且 McNemar 不显著，不应基于它单独下结论。

## 7. 错误分析

### 7.1 主要差异是 answer coverage，而不是选项判断能力

在严格 246 题 subset 上：

| Diagnostic | Pony | mem0 |
|---|---:|---:|
| 返回 A/B/C/D 的题数 | 130 | 209 |
| Answer coverage | 52.85% | 84.96% |
| 返回选项后的 conditional accuracy | 74.62% | 76.08% |
| `I do not know` | 116 | 37 |
| Abstention rate | 47.15% | 15.04% |

两侧一旦返回选项，conditional accuracy 很接近；总体差距主要伴随 Pony 更高的 abstention。
逐题看，56 个 case 是“Pony abstain、mem0 correct”，19 个是“Pony 返回错误选项、mem0 correct”，
13 个是“Pony correct、mem0 wrong/abstain”。

这不是严格的因果分解，但它把优化重点从“换更强 reader”转向“让 memory extraction/retrieval 更稳定地提供
足够证据”。

### 7.2 检索上下文长度提供了相关性线索

paired subset 中，Pony 平均返回 754.5 tokens，中位数 668.5；mem0 平均返回 1117.7 tokens，
中位数 1123.5。两侧上限预算相同，但实际送给 reader 的有效记忆量不同。

不能据此直接断言“token 越多越好”：更多文本也可能引入噪声，而且两个系统的 memory representation 不同。
但结合 Pony 的高 abstention，当前最值得验证的假设是：简单 transcript-to-notes + BM25 对隐含偏好、原因和
跨阶段演化的覆盖不足。

### 7.3 下一步最小优化顺序

1. **先改 extraction coverage**：把偏好、原因、事实、状态变化分别写成可检索的结构化 note，而不是只保留中性文本块。
2. **再改 retrieval query**：对 preference evolution、reason 和 generalization 做有限的 query expansion；保持同一 reader budget。
3. **增加 evidence-aware fallback**：只在检索确实缺证据时 abstain，不通过 prompt 强迫盲猜。
4. **每次优化先跑固定 paired slice**：保留相同 Case ID，先看 abstention、W/T/L 与 McNemar，再决定是否扩大 live run。
5. **最后才扩展模型或预算**：否则无法区分记忆算法收益与 reader/model 成本增长。

## 8. 工程实现与 Akashic 参考

实现保持在 `benchmarks/memory_public/`：

- `datasets.py`：严格加载 LongMemEval 与 PersonaMem，拒绝重复或畸形记录；
- `backends.py`：Pony、mem0 与 full-context diagnostic backend；
- `run.py`：answer、judge、resume、原子 artifact 与 paired report CLI；
- `scoring.py`：PersonaMem exact match、LongMemEval judge 和配对统计。

结构参考并适配了 Akashic 的 `eval/longmemeval/`：复用了 benchmark-only package、严格数据加载、逐题隔离、
resume 和机器可读 artifact 等思想，但没有逐行复制。Pony 版本增加了 PersonaMem prefix boundary、mem0 backend、
Provider/config 共用、失败可恢复和完整配对门槛。

benchmark 代码不进入 `pony/` runtime package，也没有增加运行时依赖；Fake Provider 仍留在 benchmark/test 边界。

## 9. nanobot 仓库审计的安全表述

截至 2026-08-05，审计的 nanobot commit 为 `5a1ab44baa6d68038ea452586197e5a9354d180e`，
当时记录的 GitHub stars 为 46,641。完整仓库树中发现了面向外部 eval runner 的 LongMemEval transcript
ingestion 示例，但没有发现 checked-in public memory benchmark harness、结果 artifact 或公开 memory-quality 数字。

可以说：

> 我在给自己的 Agent 建公开记忆评测时，也审计了 4.6 万 star 的 nanobot；它提供 LongMemEval ingestion
> 示例，但在所审计 commit 中没有 checked-in 的公开记忆质量评测框架或结果。

不要说“nanobot 没有任何记忆评测”，因为仓库审计无法证明维护者从未做过未公开或仓库外评测。

## 10. 简历与面试叙事

### 10.1 推荐简历版本

> 为自研 coding agent 的 User-Notes/BM25 记忆系统接入 LongMemEval 与 PersonaMem，并实现 mem0 OSS
> baseline、断点续跑、逐题 artifact 和配对统计；PersonaMem 434-case interim evaluation 达到 **43.5%**
> accuracy（189/434），在 246 个严格配对 Case 上为 **39.4% vs mem0 64.6%**，用 bootstrap CI 与
> McNemar 检验量化 **-25.2 pp** 差距并定位 retrieval coverage/abstention 为首要优化方向。

如果简历空间很小：

> Built auditable LongMemEval/PersonaMem evaluation for Pony memory with a pinned mem0 baseline; measured
> 43.5% on a 434-case PersonaMem interim run and diagnosed a -25.2 pp gap on 246 matched cases.

### 10.2 面试展开顺序

1. **问题**：memory demo 只能证明“能存、能搜”，不能证明长期记忆质量。
2. **方法**：引入公开数据集、prefix leakage boundary、统一 reader model/budget 和逐题 artifact。
3. **工程**：live run 可恢复、失败不丢已完成 sibling、报告对 metadata 和 Case ID fail closed。
4. **数字**：先报 43.5% 的 N 与 CI，再明确 246-case paired 结果 39.4% vs 64.6%。
5. **洞察**：Pony 与 mem0 在“已返回选项”时准确率接近，但 Pony abstention 47.2% vs 15.0%。
6. **决策**：不先堆模型或 token，而是优先改结构化 extraction 与 retrieval coverage。
7. **边界**：主动说明 43.5% 是 interim snapshot、paired subset 是 post-hoc，不伪装成 full leaderboard。
8. **行业观察**：用 nanobot 的 bounded repository audit 说明公开、checked-in memory evaluation 仍不普遍。

这套叙事的亮点不是“打赢 mem0”，而是：能设计公平实验、发现自己的系统落后、量化根因线索，并把结果转成
下一轮工程优先级。对面试官而言，这通常比选择性展示一个高分更可信。

## 11. 证据与复现

Git 忽略的本地证据：

```text
benchmarks/memory_public/results/personamem-pony-434-snapshot-20260805.json
benchmarks/memory_public/results/personamem-434-and-paired-246-analysis-20260805.json
benchmarks/memory_public/results/personamem-434-and-paired-246-analysis-20260805.md
benchmarks/memory_public/results/archive-67626ead-20260805/personamem-v2-pony-20260805.json
benchmarks/memory_public/results/archive-67626ead-20260805/personamem-v2-mem0-20260805.json
```

核验时至少检查：

- 434 snapshot 为 189 correct、434 scored、434 unique Case IDs、零 failure；
- paired subset 为 246 个相同 Case IDs、两侧同 commit 和同 protocol metadata；
- Pony paired 为 97/246，mem0 为 159/246；
- paired W/T/L 为 13/158/75；
- bootstrap interval 与 McNemar p 可由 `benchmarks.memory_public.scoring` 独立复算；
- 文档不把 434/246 结果标成 full `publishable=true`。

## 12. 局限

1. 43.5% 是用户指定的 434-case post-hoc interim snapshot，不是 589-case final score。
2. 246-case subset 是先完成的 16 个 shared contexts，不是预注册随机样本。
3. 当前没有完整 589-case mem0 artifact，因此不能给出 PersonaMem full paired comparison。
4. 当前没有 LongMemEval 500-case paired result，smoke run 不能替代正式数字。
5. Temperature 0 不保证云端模型跨请求绝对确定；重复 run 仍可能有轻微变化。
6. PersonaMem adaptation 与官方 full-context setting 不可直接比较。
7. benchmark ingestion adapter 不代表生产 Agent 自动 memory formation 的真实分布。
8. 检索 token 数与 abstention 的关系是诊断相关性，不是已证明的因果关系。
