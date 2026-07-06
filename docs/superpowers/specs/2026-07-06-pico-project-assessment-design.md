# Pico 项目评估 · 设计文档

- 日期：2026-07-06
- 类型：项目自评估的设计文档（不是新功能设计）
- 视角：**项目自身目标对齐度**（以 CONTEXT.md 术语表 + ADR + README 里 pico 自己写下的承诺为基准）
- 深度：结构 + 质量证据链（读关键源码 + 全量 ADR + CONTEXT + 跑 lint / tests）
- 产物：`docs/superpowers/specs/2026-07-06-pico-project-assessment.md` + 对话摘要
- 边界：不自动 commit、不动源码、不动分支上未提交的 8 个改动
- 语言说明：CONTEXT.md 里的英文术语作为项目专有语言，本文保留原名以便与代码和 ADR 对齐；代码符号、文件路径、提交哈希、测试函数名也保留原文。其余全部使用中文。

## 1. 评估目标

回答一个问题：**pico 在它自己声明的目标范围内做得怎么样？**

pico 自己声明的目标（取自 CONTEXT.md 术语表和 README）：

- 是一个 **Coding-Agent Harness** —— 在仓库上下文中控制工具访问、执行策略、任务状态、checkpoint、trace、验证产物
- 提供 **Recoverable Editing** —— agent 产生的仓库改动可被检查、解释、恢复、回滚
- 提供 **Safe Execution** —— 通过工具策略、审批流、sandbox 边界、可审计执行记录约束模型发起的动作
- 通过 **CLI Surface** 暴露上述能力
- 有一套明确的**记忆模型**（AGENTS.md + `.pico/memory/notes/*.md` 用户笔记 + `.pico/memory/agent_notes.md` agent 追加笔记）

评估**不做**这些事：

- 不评估模型后端本身（DeepSeek / OpenAI / Anthropic / Ollama）
- 不与其他 harness（aider / claude-code / codex）做横向对标
- 不改代码、不动文档、不整理分支上未提交的 8 个改动
- 不主动跑 benchmark（`benchmarks/` 会读，但不主动跑）
- 不评估 `.superpowers/sdd/*` 和 `.planning/*`（过程性资料）
- 不打分、不给星级

## 2. 评估维度（6 个）

| # | 维度 | 涉及的 pico 承诺 |
| - | ---- | ---------------- |
| 1 | Harness 边界完整性 | Coding-Agent Harness、Trace Timeline、Verification Evidence、Tool Effect Class |
| 2 | Recoverable Editing 兑现度 | Recovery Boundary / Checkpoint Record / Checkpoint Store / Checkpoint Pruning / Turn Checkpoint / Restore Checkpoint / Automatic Checkpointing / Tool Change Record / Pending Tool Change / Interrupted Tool Change / Delegated Change / Recovery Review / Recovery Manager / Restore Plan / Restore Preview / User-Initiated Restore / Selective Restore / Snapshot Restore / Restore Conflict / Snapshot Eligibility / File-State Blob / Affected Path / Workspace-Relative Path / Git Review Context |
| 3 | Safe Execution 边界 | Safe Execution / Command Boundary / Command Risk Class / Command Approval / Shell Side Effect |
| 4 | CLI Surface 一致性 | CLI Surface + README 命令表 + `pico-cli` 子命令 + `--format json` + `--apply` 语义 |
| 5 | 记忆与文档—代码对齐度 | 5a. 记忆子系统：AGENTS.md 加载规则、User Notes 只读、Agent Notes 追加约束、Memory Index、Repo Map；5b. 文档—代码对齐：CONTEXT 词汇表 vs 代码符号；39 份 ADR vs 当前实现；docs/architecture、docs/memory-model 是否同步。**报告里分为 5a / 5b 两个子小节呈现。** |
| 6 | 工程健康度（辅证） | 测试是否覆盖各维度关键不变量、`./scripts/check.sh` 是否通过、近期重构（拆 provider / 拆 cli_* / 拆 test 集群）是否稳定落地 |

维度 6 **不独立给结论**，只作为前 5 个维度的证据强度加权。

## 3. 证据收集方法（每个维度都跑一遍）

1. **承诺提取**
   - 从 CONTEXT.md 术语表 + 相关 ADR + README 里抽出该维度下“必须做到 / 明确不做”的条款
   - 产出：期望清单

2. **实现锚定**
   - 对期望清单每一条，用 `grep` / Read 找实现入口
   - 记录格式：`pico/xxx.py:行号 → 该期望`
   - 找不到的直接记为“未兑现”

3. **不变量测试映射**
   - 扫 tests/ 下的 55 个测试文件
   - 重点：`test_safety_invariants.py`、`test_recovery_e2e.py`、`test_public_api_contract.py`、`test_allowed_tools.py`、`test_recovery_policy.py`、`test_recovery_manager.py`、`test_recovery_paths.py`
   - 有测试锁死记为“强证据”，只有代码没测试记为“弱证据”

4. **反例扫描**
   - 主动搜索该维度典型的反模式，例如：
     - Safe Execution：有没有绕过 approval 的 shell 调用路径
     - Recoverable Editing：有没有绕过 `--apply` 就直接改仓库的路径
     - CLI Surface：README 命令表列出的命令与 `cli_*.py` 里注册的命令是否一一对应

5. **文档—实现漂移检查**
   - 对比 CONTEXT 术语表词条 ↔ 代码符号
   - 对比每份 ADR 的决定 ↔ 当前代码
   - 记录：词条存在但代码没有 / 代码存在但文档没提 / 命名或语义漂移

## 4. 评价语言（不打分）

对每一条期望，只有三档：

- **兑现（met）** —— 有代码入口 + 有测试锁死或反例扫描无缺口
- **部分兑现（partial）** —— 有代码入口，但测试或边界不完整；必须写出“什么没兜住”
- **未兑现 / 漂移（missing / drift）** —— 词条 / ADR 里承诺了但代码里找不到，或代码里存在但已经和文档说法不一致

**禁止使用**：分数、星级、“5/10”、“良好 / 一般 / 差”这类无据的定性词。

## 5. 执行步骤（分步执行，每步在对话里给简短进度）

1. **承诺提取**（约 5 分钟）
   - 通读 CONTEXT.md 术语表
   - 扫 39 份 ADR，按 6 个维度归类
   - 通读 `docs/architecture/agent-harness-v1-overview.md`、`docs/memory-model.md`
   - 产出：6 份维度期望清单

2. **代码锚定**（约 10 分钟）
   - 对期望清单每一条找 `pico/xxx.py:行号`
   - 必读模块：`runtime.py`（709 行）、`tool_executor.py`（684 行）、`recovery_policy.py`（510 行）、`tools.py`（387 行）、`recovery_manager.py`（250 行）、`agent_loop.py`、`security.py`、`recovery_models.py`、`recovery_paths.py`、`recovery_checkpoint_writer.py`、`checkpoint_store.py`、`checkpoint.py`、`tool_change_recorder.py`、`workspace_snapshot.py`、`workspace_observer.py`、`workspace.py`、`verification.py`、`prompt_prefix.py`、`working_memory.py`、`cli_*.py` 全套
   - 产出：期望条款 → 实现入口的映射表

3. **测试与不变量映射 + 跑 check.sh**（约 5 分钟）
   - 扫 55 个测试文件
   - 跑一次 `./scripts/check.sh`（ruff check + pytest -q）
   - `check.sh` 结果作为评估基线记入报告；如果不通过必须记下来
   - 产出：期望 → 测试锁死状态；lint / test 基线结果

4. **反例扫描 + 文档—代码漂移检查**（约 5 分钟）
   - 反例扫描：approval bypass、restore 越过 `--apply`、CLI 命令表 vs 注册命令
   - 漂移检查：CONTEXT 术语 ↔ 代码符号；ADR 决定 ↔ 当前实现
   - 产出：缺口清单

5. **写报告 + 对话摘要**（约 5 分钟）
   - 报告落到：`docs/superpowers/specs/2026-07-06-pico-project-assessment.md`
   - 对话里给一段直白结论 + 关键发现

## 6. 报告结构（最终产物模板）

```
# Pico 项目评估报告 · 2026-07-06

## 摘要
（3-5 句客观定位，不含情绪词）

## 评估基线
- `./scripts/check.sh` 是否通过（记具体结果）
- 源码规模、测试规模、ADR 数量
- 评估当天分支状态

## 维度 1：Harness 边界完整性
### 期望清单
- [期望 1] ...
### 逐条判定
- [期望 1] 兑现 / 部分兑现 / 未兑现 —— 证据：pico/xxx.py:行号 / tests/test_xxx.py:行号
### 维度状态描述
（一段话）

## 维度 2：Recoverable Editing 兑现度
（同上）

## 维度 3：Safe Execution 边界
（同上）

## 维度 4：CLI Surface 一致性
（同上）

## 维度 5：记忆与文档—代码对齐度
（分 5a / 5b 两个子小节）

## 维度 6（辅证）：工程健康度
（一段话，喂回前 5 维度）

## 跨维度重要发现
（有证据支撑的才写）

## 客观定位
- pico 处于什么阶段（早期原型 / 内测就绪 / 已产品化）
- 附证据

## 明显短板
（只列有证据的；不写“可以考虑”这类建议）
```

## 7. 边界与不做的事

- 不评估外部依赖质量（模型后端、uv、ruff、pytest 本身）
- 不做横向对标
- 不改代码 / 不改文档 / 不动分支上未提交的 8 个改动
- 不主动跑 benchmark
- 不评估过程性资料（`.superpowers/sdd/*`、`.planning/*`）
- 不打分、不给星级、不用无据的定性词
- 报告只做诊断，不主动产出改进建议清单（后续用户可另开话题）

## 8. 明确会回答的问题

- pico 是不是一个已经“能用”的 harness？
- 它承诺的 Recoverable Editing 是不是真的兜住了？
- Safe Execution 有没有绕过口？
- 词汇表 / ADR / 代码是否三边对齐？
- 近期这波重构（拆 provider / 拆 cli_* / 拆 test 集群）落地质量如何？（作为维度 6 辅证给出观察，不独立成一节结论）
- pico 现在处于什么成熟度阶段？

## 9. 交付形式

- 主报告：`docs/superpowers/specs/2026-07-06-pico-project-assessment.md`
- 对话摘要：直接输出直白结论 + 关键发现
- **不自动 commit**
