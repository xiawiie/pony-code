# Pony 与 mem0 公开记忆评测：最终结果与差距分析

## 最终数字

| System | Correct / N | Accuracy | 95% Wilson CI | 差距 |
|---|---:|---:|---:|---:|
| Pony User-Notes/BM25 | **249/589** | **42.28%** | **38.35%–46.30%** | **-22.41 pp** |
| mem0 OSS | **381/589** | **64.69%** | **60.74%–68.44%** | reference |

**Pony 比 mem0 少答对 132 题。**

换一种表达：mem0 的正确题数比 Pony 多 **53.01%**；相对于 Pony 的 340 道错题，mem0
减少了 **38.82%** 的错误。两侧 Wilson 区间完全分离，说明差距不是几个样本造成的小幅波动；但由于
两边回答的是同一批题，正式显著性结论仍应以 589 题逐题配对检验为准，不能用两个独立 Wilson 区间代替。

## 核心结论

这 **22.41 个百分点** 不是单一参数、一次随机波动或回答模型能力造成的。根本原因是两套系统实际比较的
不是同一种记忆管线：

- **Pony** 把原始对话按块写成 User Notes，再用纯词法 BM25 检索；
- **mem0** 先调用 LLM 从对话中提取、整理记忆，再生成 embedding，通过 Qdrant 做语义检索；
- 两侧虽然使用相同的最终回答模型、prompt、temperature 和最大检索预算，但 mem0 在回答前已经投入了额外的
  **记忆抽取模型、embedding 模型和向量数据库**。

因此，这个结果证明的是：**在当前完整配置下，mem0 的自动对话记忆覆盖率明显高于 Pony 的原始文本
User-Notes/BM25 适配方案。** 它不是“BM25 与向量检索在完全相同表示、相同计算成本下”的单变量实验。

最短的因果链是：

```text
Pony：原始长对话 → 粗粒度文本块 → 词面匹配不足 → 证据召回不完整 → 回答模型拒答 → 准确率下降
mem0：对话角色对 → LLM 提取/整理事实 → 向量化 → 语义召回 → 证据覆盖更高 → 回答率与准确率上升
```

## 评测中哪些条件相同，哪些条件不同

### 相同条件

- 相同的 PersonaMem-v1 32k retrieval-augmented adaptation，共 589 题；
- 相同的 prefix boundary，不把问题发生之后的对话提前写入记忆；
- 相同的最终回答模型 `qwen3.7-max`；
- 相同的回答 prompt 和 `temperature=0`；
- 相同的最大检索上下文预算：6144 tokens；
- 相同的选择题 exact-match 评分方法。

这些控制项排除了“mem0 使用了更强的最终答题模型”这一解释。

### 不同条件

| 维度 | Pony User-Notes/BM25 | mem0 OSS | 直接影响 |
|---|---|---|---|
| 写入方式 | 原始 transcript 直接写入 Markdown note | LLM 从 role-pair 中抽取记忆 | mem0 在写入时已完成信息压缩和重述 |
| 记忆单元 | 一段或多段长对话 | 较原子的事实/偏好记忆 | 原子记忆更容易被问题准确命中 |
| 元数据 | opaque name、固定 `type/description` | 抽取结果、用户/run metadata | Pony 的 field boost 在本适配中几乎没有可利用的主题字段 |
| 检索 | BM25，英文按词面匹配 | embedding + Qdrant 语义检索 | mem0 能匹配同义改写和间接表达 |
| 候选深度 | 最多 6 个 note，每个 note 只取一个最佳 passage | 搜索 top 200，再按同一 token budget 截断 | Pony 更容易在候选阶段提前丢失证据 |
| 状态变化 | 原始历史并存，没有自动合并新旧偏好 | LLM 记忆管线可抽取并更新事实 | mem0 更适合偏好演化和原因追踪 |
| 写入计算 | 不调用抽取模型或 embedding 模型 | 3547 次 `add`，并调用抽取与 embedding | mem0 质量更高，但成本、延迟和系统复杂度也更高 |
| 运行依赖 | Python 标准库和本地文件 | mem0ai、LLM、embedding、Qdrant | 这是产品级能力与成本的比较，不是等成本算法比较 |

## 为什么差距会达到 132 题

### 1. Pony 的评测适配没有真正做“记忆抽取”

`PonyMemoryBackend` 的 `transcript-to-notes-v1` 只是把对话原文写进 Markdown：

```text
role: content
```

它没有把内容转换成“用户喜欢什么”“为什么改变”“当前状态是什么”“旧状态被什么取代”等可直接检索的
记忆对象。PersonaMem 恰好大量考查偏好、原因、状态演化、建议和新场景泛化，因此原文保存虽然没有主动丢数据，
却把理解和定位工作全部推迟到了检索与回答阶段。

mem0 则在写入阶段就使用 LLM 抽取事实。它付出了额外成本，但也把长对话压缩成更接近问题语义的记忆单元。
当问题换一种说法时，mem0 更有机会召回已经规范化的事实；Pony 必须依赖问题和原文共享足够多的词。

### 2. BM25 解决的是词面相关性，不是语义等价

Pony 当前 tokenizer 对英文使用单词匹配。问题中的词没有出现在相关对话里，即使两句话语义相同，BM25 也不会
因为“意思相近”而加分。例如：

- “更喜欢小型聚会”与“在人多的活动中感到疲惫”；
- “重新开始写理财博客”与“恢复了对个人财务的兴趣”；
- “不再参加烹饪课”与“兴趣从烹饪转向音乐”。

这些问题需要同义改写、因果联系或状态变化理解。embedding 检索天然更适合寻找语义接近的记忆；纯 BM25
只有在关键词重合足够明显时才稳定。

代码中的 `pony/memory/retrieval.py` 也明确记录了这一限制：它是 keyword-level matching，不提供 semantic
similarity。这不是实现 bug，而是当前算法边界。

### 3. Pony 的文档和返回粒度过粗

本次 PersonaMem 适配把 6425 个 context item 写成 246 个 transcript note；部分首段 note 接近 128 KiB 上限。
检索先给整个 note 排名，然后每个命中的 note 只返回一个 `_best_passage`，最多返回 6 个 note。

这会产生三层信息损失：

1. 相关事实位于长 note 中，但整篇文档的 BM25 分数不够高，note 没进入前 6；
2. note 进入前 6，但同一 note 中有多个必要事实，最终只返回词面最匹配的一个段落；
3. 偏好演化需要“过去状态 + 更新原因 + 当前状态”，三段证据可能被拆开，而回答模型只看到其中一段。

所以“数据已经存进文件”不等于“回答时拿到了完整证据”。本轮短板主要发生在从持久化文本到模型上下文的
转换过程。

### 4. Pony 的 field boost 和 link expansion 在该适配中基本没有发挥作用

Pony BM25 支持对 `name`、`description`、`tags`、`aliases` 加权，也支持 `[[name]]` 一跳链接扩展；但 benchmark
adapter 生成的 note 使用 opaque name、统一的 `type: transcript` 和 `description: conversation transcript`，
没有主题 tags、aliases 或实体链接。

因此，生产检索器已有的结构化能力没有被数据适配层利用。最终几乎只剩正文 BM25，不能把“偏好”“原因”“人物”
和“时间变化”等字段作为强信号。

### 5. 逐题行为说明主要瓶颈是证据覆盖，而不是选项判断

仓库现有的 246 题严格配对子集提供了逐题诊断。它不是新的最终榜单，但能解释差距发生在哪里：

| Diagnostic（246 题配对子集） | Pony | mem0 |
|---|---:|---:|
| Answer coverage | 52.85% | 84.96% |
| Abstention rate | 47.15% | 15.04% |
| 已返回 A/B/C/D 后的 conditional accuracy | 74.62% | 76.08% |
| 平均检索 tokens | 754.5 | 1117.7 |

最关键的是：**一旦两边真正返回选项，conditional accuracy 只差 1.46 个百分点；但 answer coverage 相差
32.11 个百分点。**

在这个子集的 75 道 “mem0 对、Pony 错” 题中：

- 56 题是 Pony 拒答、mem0 答对；
- 19 题是 Pony 返回错误选项、mem0 答对。

这说明多数净损失发生在 Pony 没有获得足以作答的证据时。共同的回答模型遵守了“证据不足就说不知道”的 prompt，
高 abstention 是上游记忆覆盖不足的结果，而不是 reader 不会做选择题。

平均 token 数只能作为相关线索，不能解释成“文本越多越好”。真正重要的是 mem0 返回的内容更可能覆盖问题所需事实；
盲目把 Pony token budget 填满，也可能只增加无关 transcript 噪声。

### 6. PersonaMem 的难题类型正好击中 Pony 当前边界

246 题配对子集中，Pony 相对 mem0 的主要差距为：

| Question type | Pony - mem0 |
|---|---:|
| 新场景泛化 | -37.0 pp |
| 回忆先前更新的原因 | -30.0 pp |
| 回忆用户共享事实 | -25.5 pp |
| 偏好对齐推荐 | -25.0 pp |
| 提出新想法 | -22.2 pp |
| 跟踪完整偏好演化 | -17.2 pp |

这些任务不仅要求找到一句原文，还要求把记忆转换成稳定的用户状态：

- **泛化**要求从旧偏好推断新场景；
- **原因回忆**要求同时关联事件与动机；
- **偏好演化**要求区分旧状态、更新事件和当前状态；
- **推荐与新想法**要求把多条偏好组合后再应用。

mem0 的抽取和语义检索与这些任务更匹配。Pony 的原始文本块加词法检索更适合明确关键词的事实查找，对组合、
改写和时间演化能力不足。

## 这次结果真正说明了什么

### 可以下的结论

1. **当前 Pony User-Notes/BM25 方案在 PersonaMem 上显著落后 mem0 OSS。** 22.41 pp 和 132 题不是可忽略差距。
2. **主要工程瓶颈位于记忆写入表示与检索覆盖。** 逐题子集中的 conditional accuracy 接近，但 Pony 拒答明显更多。
3. **Pony 的通用检索能力没有被 benchmark adapter 充分利用。** adapter 没有生成 tags、aliases、links 或状态字段。
4. **mem0 的优势有真实成本。** 它使用额外 LLM 抽取、embedding 和 Qdrant，不能把提升全部归因于一个向量搜索调用。
5. **Pony 当前产品边界与 PersonaMem 目标不完全一致。** Pony 生产设计强调用户明确授权后保存高精度 notes；
   PersonaMem 测的是自动吸收全部对话并长期维护 persona。若要追平，需要明确扩大产品能力，而不只是调参。

### 不能下的结论

1. 不能说 132 道题全部是 BM25 召回失败；完整 589 题还缺逐题错误归因。
2. 不能说“向量检索一定比 BM25 好 22.41 pp”；两侧的抽取、表示、更新和计算成本都不同。
3. 不能说最终回答模型是主要差异；两侧 reader 相同，子集 conditional accuracy 也接近。
4. 不能把更多 retrieved tokens 直接当成原因；相关证据密度比 token 数更重要。
5. 不能通过取消 abstention、强迫模型猜选项来宣称修复；那会掩盖检索问题并增加错误答案。

## 最小、可验证的改进顺序

如果目标是在不立刻复制 mem0 全套架构的前提下提高 Pony，应该先修最便宜、最接近根因的部分：

1. **把检索单元从 note 降到 passage。** 对每个对话段落独立评分，允许同一 note 返回多个相关 passage，避免
   “整篇排名 + 单段返回”的双重丢失。
2. **让 benchmark adapter 生成结构化字段。** 至少写入 `topic`、实体、偏好、原因、时间和状态类型，使现有
   field boost 真正生效。
3. **增加有限 query expansion。** 针对 preference、reason、evolution 等已知题型扩展同义词和状态词；先验证
   词法方案的上限，再决定是否引入 embedding。
4. **显式表示状态更新。** 保留来源和时间，同时把“当前事实”与“已被取代的旧事实”区分开，减少矛盾 transcript
   同时进入上下文。
5. **最后评估语义检索或 LLM extraction。** 这一步最可能继续缩小差距，但会改变成本、隐私、授权和依赖边界，
   应作为产品决策，而不是悄悄加入的 benchmark 特例。

每一步都应在同一 589 Case ID、相同 reader、prompt、temperature 和 6144-token budget 下单独 A/B；同时报告：

- overall accuracy；
- answer coverage 与 abstention；
- conditional accuracy；
- paired win/tie/loss；
- exact McNemar 和 paired bootstrap CI；
- 写入调用数、延迟、token 与基础设施成本。

这样才能回答“提升来自哪里、值不值得付出成本”，而不只是得到一个更高但无法解释的总分。

## 面试回答模板

### 30 秒版本

> 我用 589 道 PersonaMem 题比较了 Pony 的 User-Notes/BM25 和 mem0 OSS。Pony 是 249/589，42.28%；
> mem0 是 381/589，64.69%，Pony 少答对 132 题，差 22.41 个百分点。根因不是最终回答模型，因为两边 reader、
> prompt、temperature 和检索预算相同。真正差别在记忆管线：Pony 直接保存长 transcript，用 top-6 词法 BM25；
> mem0 先用 LLM 抽取事实，再用 embedding 和 Qdrant 做语义检索。246 题逐题诊断里，两边返回选项后的准确率
> 约 75%，但 Pony 的回答覆盖率只有 52.85%，mem0 是 84.96%，所以主要损失来自证据没有稳定召回，模型因此拒答。

### 2 分钟版本

> 这次比较最重要的不是只报 42.28% 对 64.69%，而是解释 22.41 个百分点从哪里来。两边最终答题条件相同：
> 同一批 589 题、同一 qwen3.7-max reader、同一 prompt、temperature=0、同一 6144-token 上限，所以差距主要来自
> reader 之前的 memory pipeline。
>
> Pony 的 benchmark adapter 没有做知识抽取，只把原始对话写成较大的 Markdown note。检索时使用纯 BM25，最多
> 取 6 个 note，而且每个 note 只返回一个最佳 passage。这样在同义改写、原因关联和偏好演化题中，很容易出现
> “内容存着，但没有作为完整证据送到模型”的情况。生成的 note 又没有主题 tags、aliases 或 links，所以 Pony 已有的
> field boost 和 link expansion 基本没有发挥作用。
>
> mem0 在写入时额外调用 LLM，把对话抽取成较原子的事实，再用 embedding 和 Qdrant 做语义检索。它更能处理
> 问题与原文措辞不同、需要组合多条状态的场景，但代价是更多模型调用、延迟、费用和基础设施。
>
> 最有说服力的证据来自 246 题逐题子集：Pony 和 mem0 在已经返回 A/B/C/D 后的准确率分别是 74.62% 和 76.08%，
> 差得不多；但回答覆盖率是 52.85% 对 84.96%。75 道 mem0-only correct 中，有 56 道是 Pony 直接拒答。因此我把
> 根因定位为 extraction/retrieval coverage，而不是先换更强 reader 或强迫模型猜答案。
>
> 下一步我会按成本从低到高做 passage-level BM25、结构化 note、有限 query expansion 和状态更新建模，再评估是否
> 值得引入 embedding 或自动 LLM extraction。每一步保持同一 Case ID 和 reader 做 paired A/B，才能知道收益到底来自哪里。

### 常见追问

**为什么不直接换成向量数据库？**

因为那会同时改变依赖、成本、隐私和运行边界。先修 passage 粒度和结构化 note，可以用最小改动验证 BM25 还能追回
多少分；只有词法上限被证实后，再引入 embedding，结论才可解释。

**这是不是说明 Pony 的记忆设计失败？**

说明当前方案不适合“自动吸收全部对话并维护长期 persona”这一任务。Pony 原设计偏向用户明确授权后保存高精度、
可审计的 notes，目标不同。但如果产品要宣称通用长期记忆，42.28% 就是必须正视的能力缺口，不能用产品边界回避。

**为什么说不是回答模型的问题？**

两侧最终 reader 完全相同；而且逐题子集中，一旦系统返回选项，conditional accuracy 分别为 74.62% 和 76.08%。
真正拉开总准确率的是 Pony 更高的 abstention，说明上游证据覆盖不足。

**132 道题能否全部归因于检索？**

不能。132 是两边总正确数的净差，不等于 132 个纯 retrieval miss。完整归因需要 589 题逐题配对 artifact，区分
双方都对、双方都错、Pony-only correct 和 mem0-only correct。现有 246 题诊断支持“覆盖率是主要原因”，但不应把
相关证据夸大成对全部 132 题的逐题因果证明。

**怎样证明优化真的有效？**

保持数据、Case ID、reader、prompt、temperature 和 budget 不变，只替换一个 memory stage；报告 paired
win/tie/loss、McNemar、bootstrap CI、coverage、abstention 和成本。如果只报新的 overall accuracy，就无法判断提升
来自更好的记忆、更多 token、额外模型计算还是随机波动。
