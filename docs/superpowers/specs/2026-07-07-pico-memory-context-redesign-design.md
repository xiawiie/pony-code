# Pico Memory / Context Redesign — Design Spec

Date: 2026-07-07
Status: Draft v2 — awaiting user review before writing implementation plan

---

## 1 · Motivation & Expected Benefits

### 1.1 现状诊断

Pico 当前的 memory 与 context 设计**没有错**——它工程严谨、路径安全、cache-aware、
带完整 telemetry，是一份典型的"小而结构清晰"的 harness 参考实现。但它有两条**主线级
未跟上**的设计取舍：

1. **抽象错位**：Prompt 被当作"一个大字符串"来拼接（`prefix / history /
   current_request` 三段），把身份规则、tools schema、workspace 快照、memory
   index、历史、用户请求全部拼进同一根字符串。后果：
   - Provider 原生的 tool_use API 无法使用，模型要解析手写 `<tool>...</tool>` XML；
   - Provider 原生的 prompt caching 只能靠"稳定 prefix 字符串 hash"近似利用，
     而非 API 支持的多断点 `cache_control`；
   - 动态信息（`memory_index`、`project_structure`）被硬塞进"稳定 prefix"，
     与"稳定"承诺自相矛盾；
   - 工作区快照、resume checkpoint 被挂到 `history section` 前，命名与语义分裂。

2. **Memory 是"档案柜"而不是"工作台"**：Agent 通过 `memory_index` 只能看到"有哪些
   文件"，无法自动看到相关内容。BM25 检索存在，但 agent 需要主动调用
   `memory_search` / `memory_read` 才能获取。没有 per-topic 结构、没有失效机制、
   没有链接图——记忆只增不减，随着时间推移信号被噪声稀释。

### 1.2 本次改造

从 **prompt-as-string 范式**迁移到 **prompt-as-message-array 范式**，参考 Claude
Code 的架构思想；同时把 memory 从"追加流水"升级为"结构化知识 + 主动召回"。

### 1.3 预期可量化收益

- **Cache 命中范围**：从"整个 prefix 字符串"扩展到"system + tools" + "messages
  前缀" 两个 cache_control 断点。稳定性显著提升——workspace 抖动（branch/status/
  new file mtime）不再打断 cache。
- **Tool 调用可靠性**：从"手写 XML + regex 解析"变为 provider native `tool_use`
  block。模型端不再需要学习 pico 的私有 XML 协议，第三方 provider 直接兼容。
- **Memory 触达率**：从"agent 主动 read"变为"每轮相关内容自动送到眼前"——无需
  agent 记得调 `memory_search`。BM25 命中的 top-2 note 首段进入当轮 user 消息。
- **长会话 token 效率**：老 tool result 通过 digest 摘要压缩，原文单独存盘可回读。
  预估长会话（20+ 轮）token 使用减少 40-60%。

---

## 2 · Non-Goals

- **不引入 embedding-based 语义检索**。BM25 + CJK bigram 保留为检索底座。
- **不引入向量数据库、外部依赖**。stdlib-only 是硬约束。
- **不做 salience / access-driven ranking**。单人本地场景 note 数量小，收益不足。
- **不做 ContextItem 全局优化器 / knapsack 求解**。静态分配即可。
- **不重写 checkpoint / recovery / trace / run_store 等子系统**——它们与 prompt
  结构独立。若 checkpoint 里 embed 了老 history 数据，通过 session_store migrator
  一并升级；checkpoint 对外 API 不变。
- **不新增 `.pico/memory/.state/` 目录**。所有 memory 状态保留在 note 文件本体
  或 session 内存里。
- **不新增 model provider**。本次只改 provider **接口**，不做新增（不同时接
  OpenAI、Gemini 等）。
- **不改动 Anthropic API 契约**——一切按 Anthropic messages API 现行规范
  （`system` 是 content-block 列表、`tool_result` 是 user 消息 content block、
   `role` 只有 `user`/`assistant`）。

---

## 3 · Architecture Overview

Pico 每一轮向模型发送的不再是"prompt 字符串"，而是**三个 API-level 字段 + 两个
cache_control 断点**：

```
Request = {
  system:  [ {"type": "text", "text": SYSTEM_CORE, "cache_control": ephemeral} ]  ← 断点 1
  tools:   [ {name, input_schema, description}, ... ]                              ← 与 system 一同缓存
  messages: [
    { role: "user",      content: <turn 1 user 消息 + 注入> },
    { role: "assistant", content: [{"type": "tool_use", ...}] },
    { role: "user",      content: [{"type": "tool_result", ...}] },
    ...
    ─────────────────── cache_control: ephemeral, 断点 2 (上一轮末尾) ───────────
    { role: "user",      content: <当轮 user 消息 + <system-reminder> 注入> }
  ]
}
```

**核心思想**：
- **稳定 vs 易变的分离靠字段/消息边界，不靠字符串顺序**：system + tools 走 API
  原生字段（天然稳定、cache-able）；动态信息通过 `<system-reminder>` 注入到当轮
  user 消息，历史消息**永不重写**。
- **history 即 messages 数组本身**，不再有"history section"的人造概念。
- **两 cache 断点**：Anthropic API 最多 4 个断点，pico 用 2 个足够。

---

## 4 · Layer Details

### 4.1 Layer 1 · system 字段

**内容**：session 内绝对稳定的部分。

- 身份声明
- 输出协议规则（native tool_use 期望语义 + fallback XML 说明）
- 通用行为规则（"before writing tests, read impl first" 等）
- `MEMORY_USAGE_GUIDANCE` 与 `MEMORY_READING_GUIDANCE`
- `workspace.stable_text()`：cwd / repo_root / default_branch / project_docs

**API shape**（Anthropic 语义）：

```python
system = [
    {
        "type": "text",
        "text": SYSTEM_CORE,
        "cache_control": {"type": "ephemeral"}
    }
]
```

`system` 是 content-block 列表（不是 str），才能对指定 block 打 cache_control。
本次实现只用一个 block。

**明确不进 system**：tools schema、workspace_state、memory_index、
project_structure、recalled_memory、history。

**生命周期**：整段 = session。只在 workspace 静态事实（cwd/repo_root/
default_branch/project_docs）变化或 tools schema 变化时才 rebuild。

---

### 4.2 Layer 2 · tools 字段

**Provider API 原生字段**。每条 tool 定义映射到 Anthropic tools schema：

```python
{
  "name": "read_file",
  "description": "Read a file from the workspace. (Safe: no approval required.)",
  "input_schema": {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"]
  }
}
```

**Risky flag 迁移策略**：Pico 现有 `pico/tools.py` 里 `risky=True` 的 tool
（write_file / patch_file / run_shell / delegate）在 native tools API 里**没有
对应字段**。解决：

> **把 approval 语义写进 `description` 末尾**。例如：
> `"Write to a file at PATH. Requires user approval before execution."`
>
> 模型据此在生成 tool_use 前判定是否需要 user confirm；实际 approval 拦截仍在
> pico 端 `tool_executor` 做。

**Tool 例子（`TOOL_EXAMPLES`）**：native tool_use 路径下**不进 system**（Claude
天然知道怎么调 tool）。仅在 fallback adapter（§8.3）拼字符串时才追加。

**Tools 与 system 的 cache 关系**：Anthropic API 中 `tools` 是独立顶层参数，
不能单独打 cache_control——它与最靠近它的 system cache_control **一同缓存**。
断点 1 覆盖 system + tools。

---

### 4.3 Layer 3 · messages 数组

**Pico 内部 message 结构**（发送前剥离 `_pico_meta`）：

```python
{
  "role": "user" | "assistant",             # ← 只有这两种；Anthropic API 无 "tool" role
  "content": str | list[ContentBlock],
  "_pico_meta": {                            # 发 API 前剥离
      "created_at": ISO8601,
      "tool_use_id": Optional[str],
      "digest_applied": bool,
      "source_hash": Optional[str]           # 若 digest 引用了原始 tool result
  }
}
```

**Tool 调用的两条消息**（Anthropic 语义）：

```python
# 模型调用 tool
{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_...", "name": "read_file", "input": {...}}]}

# tool 执行结果——注意 role="user"，不是 "tool"
{"role": "user",      "content": [{"type": "tool_result", "tool_use_id": "toolu_...", "content": "..."}]}
```

**核心不变式**：**Message 一旦追加，不再回头修改**。所有"事后压缩"逻辑改为"追加时
决定" —— 见 §6.3 Tool Result Digest。这保证 messages 数组前缀字节稳定，
`cache_control` 断点 2 才能命中。

---

### 4.4 Layer 4 · Dynamic Injection

每轮**只**在**当轮 user 消息**内注入 `<system-reminder>` 块。历史消息永不重写。

#### 4.4.1 Injection Sources

| Source | 内容 | 来源代码 |
| -- | -- | -- |
| `workspace_state` | branch / status / recent_commits | `workspace.volatile_text()` |
| `memory_index` | 可用 memory 文件列表 + description（P3 前 first_line） | `BlockStore.list()` |
| `project_structure` | top-level tree + language stats | `RepoMap.top_level_tree()` |
| `recalled_memory` | 本轮 top-2 相关 note 首段 + provenance | `pico/memory/recall.py`（P3） |
| `checkpoint` | resume checkpoint text（若有） | `checkpointlib.render_checkpoint_text()` |

**Renderer 输入契约**：每个 source 提供一个 **纯函数**：

```python
def source_render(agent, budget_tokens: int) -> str | None:
    """
    Returns pre-escaped source content string, or None if empty/disabled.
    Must respect budget_tokens (self-truncate if needed).
    """
```

---

#### 4.4.2 Namespace & Escaping Contract

**C1 · Namespace**：所有系统结构标签使用 `<system-reminder>` 与 `<pico:*>` 前缀。
如 `<pico:workspace_state>`、`<pico:memory_index>`。用户/tool 输出里恰好含
`<memory_index>` 字面串时不会与系统结构混淆。

**C2 · Escaping**（明确字节替换规则）：注入内容渲染前对 body 做两条替换：

```python
def escape_pico_tags(text: str) -> str:
    # 把可能被误认为闭合结构标签的字面串"打断"
    text = text.replace("<pico:", "<pico​:")   # zero-width space 插入
    text = text.replace("</pico:", "</pico​:")
    return text
```

理由：
- Zero-width space (U+200B) 打断词边界，LLM 不会把 `<pico​:` 识别为结构标签；
- 用户看不到差异（虽然带 zero-width space 但可见字符不变）；
- 可 100% 逆转（如果需要显示原文）。

**C3 · Recall 四重护栏**（在 recall.py 生效，见 §5.4）：
- `min_score` = 0.3（BM25 归一化后）
- `max_tokens_per_note` = 400 token
- Skip superseded / tombstoned notes（frontmatter `supersedes` 声明）
- Skip recently-recalled（当轮 + 前 2 轮已 recall 的 path）

**C4 · Recall provenance**：每条 recall block 带明确元数据：

```xml
<system-reminder>
<pico:recalled_memory path="agent/prompt-cache.md" type="feedback" score="0.72" why="matched:cache,prefix">
描述... 首段正文
</pico:recalled_memory>
</system-reminder>
```

`why` 是 recall 命中的关键词列表，帮助 agent 判断相关性。

---

#### 4.4.3 Intent-Driven Budget Profile

基于当轮 user_message 的关键词正则做 intent 分类，映射到各注入源的 token 上限：

```python
INTENT_PROFILES = {
  "structural": {
    "keywords": ["架构", "结构", "怎么组织", "目录", "layout", "architecture"],
    "budget":   {"project_structure": 2000, "memory_index": 400, "recalled_memory": 800, "workspace_state": 300},
  },
  "debug": {
    "keywords": ["报错", "error", "traceback", "fail", "not working", "broken", "崩溃"],
    "budget":   {"workspace_state": 1200, "recalled_memory": 600, "project_structure": 200, "memory_index": 200},
  },
  "recall": {
    "keywords": ["上次", "之前", "记得", "past", "previous", "last time"],
    "budget":   {"recalled_memory": 1600, "memory_index": 800, "project_structure": 200, "workspace_state": 300},
  },
  "default": {
    "keywords": [],
    "budget":   {"project_structure": 600, "memory_index": 400, "recalled_memory": 600, "workspace_state": 500},
  },
}
```

**匹配算法**（明确 first-match-wins）：

```
for intent_name in ["debug", "recall", "structural"]:   # 固定优先级
    for kw in INTENT_PROFILES[intent_name]["keywords"]:
        if kw.lower() in user_message.lower():
            record telemetry.intent = intent_name, matched_keyword = kw
            return INTENT_PROFILES[intent_name]["budget"]
return INTENT_PROFILES["default"]["budget"]
```

**分配算法**（避免全局 knapsack）：

1. 每 source 有独立 `hard_cap`（profile 声明值 + `pico.toml` 覆盖）；
2. 每 source 单独渲染，超本 source 的 hard_cap 用 tail_clip 截断，
   `telemetry.injection_truncated[source]` += 1；
3. 若所有 source 合计仍超 `injection_budget`（total_budget × `injection_ratio`），
   按 profile 声明顺序**丢弃末尾 source**（先丢 workspace_state，最后丢
   recalled_memory / project_structure）；
4. 不做全局重分配、不做比例削减。

**Intent 判定失败**：走 `default` profile，永不抛错。

---

### 4.5 Layer 5 · Cache Control

**两个 `cache_control: ephemeral` 断点**：

- **断点 1**：`system` 的 content block 上。session 内极少失效——只在 workspace
  静态事实或 tools schema 变化时才 rebuild。Anthropic API 中该断点会同时覆盖
  `tools` 参数（tools 与最靠近它的 system cache_control 一同缓存）。

- **断点 2**：`messages[-2]` 上（当轮 user 消息**之前**的最后一条消息）。本轮
  开头命中，模型只需处理当轮 user 消息。

**多轮滚动策略**：Anthropic prompt cache 有 **5 min TTL**。每轮把断点 2 移到最新
"上一轮末尾"消息上——**这不会使前一轮的 cache 立即失效**，前一轮的断点仍在 5min
TTL 内可命中。Anthropic 计费按"最长命中前缀"计算，滚动断点不产生额外成本。

**Cache 契约**：
- 任何进入 Layer 1 的内容必须证明"session 内字节稳定"，否则归 Layer 4 注入；
- Message 一旦落进 messages 数组不再修改；
- 若 provider 不支持 `cache_control`（本地小模型），跳过断点参数，其余逻辑不变。

---

## 5 · Memory Storage

### 5.1 Layout

```
.pico/memory/
├── notes/            用户手写，agent 只读（不变）
├── agent/            新增，agent 可读写，per-topic
│   ├── prompt-cache-invariant.md
│   ├── auth-middleware.md
│   └── ...
├── agent_notes.md    legacy；migrator 跑过后重命名 .legacy
└── (无 .state/ 目录)
```

**Scope 双写**（Q1 收敛）：与现有 `.pico/memory/` 与 `~/.pico/memory/` 双 scope
保持一致。`agent/` 目录在两个 scope 下都可存在。

---

### 5.2 Frontmatter Schema

每个 `agent/` 或 `notes/` 下的 markdown 文件开头：

```markdown
---
name: prompt-cache-invariant
type: feedback           # user | feedback | project | reference
description: memory_index 必须放 volatile head 才能不拖累 cache
tags: [context, cache]
aliases: []
supersedes: []
---

正文... 参见 [[context-tier-model]]。
```

**字段处理**：
- `name`：文件名 stem，唯一 ID；
- `type`：4 值枚举；未识别值退回 `""`；
- `description`：单行；用于 `memory_index` 展示（取代当前 `first_line`）；
- `tags`：字符串数组；
- `aliases`：字符串数组，同义词/别名；用于 BM25 加权命中；
- `supersedes`：数组，声明本 note obsolete 哪些 `name`。检索时被列出的 `name`
  从检索池移除（tombstone 语义），磁盘文件保留。

**兼容**：无 frontmatter 的文件被视为 body-only，`type=""`，`description=""`。

**解析器实现**：**stdlib-only 手写解析器**，30-50 行代码：
- 只支持 `---` 分隔的 frontmatter；
- 只支持一层平铺 `key: value`（无嵌套 dict）；
- Values：str（可含 `[list, form]` 用逗号分隔）、无 quoted string 处理；
- 遇未识别字段：忽略；
- 遇非法结构：视为无 frontmatter，body 从文件开头算起。

不引入 PyYAML。

---

### 5.3 Retrieval Enhancements

BM25 底座不变。**Tokenize 需重构为 per-field**：

```python
# 旧：tokens = list[str]（整个文档扁平化）
# 新：
tokens_by_field = {
  "name":        list[str],
  "description": list[str],
  "tags":        list[str],
  "aliases":     list[str],
  "body":        list[str],
}
```

**Field boost**（BM25 tf 计算变化）：

```python
# 每个 term 的加权 tf
tf_weighted = sum(
    boost[field] * doc_counter[field].get(term, 0)
    for field in tokens_by_field
)
# 后续 BM25 公式不变（idf、length norm 与旧版一致）
```

默认 boost：`{name: 5.0, description: 3.0, tags: 4.0, aliases: 4.0, body: 1.0}`。

**Link expansion**：Top-k 命中后，扫命中文档正文里 `[[name]]`，把这些邻居加进
结果：
- 得分 × `decay=0.4`
- 每次 query 最多加 `max_added=3` 个邻居
- 深度上限 1（不递归扩展）
- 若邻居已在原命中集中，跳过（不重复计分）

**Tombstone filter**：加载文档时构建 `superseded_names` 集合，`if entry.name
in superseded_names: continue`。

---

### 5.4 Recall Module (`pico/memory/recall.py`)

**入口**：

```python
def recall_for_turn(agent, user_message: str, budget_tokens: int) -> str | None:
    """
    Returns rendered <pico:recalled_memory> blocks or None if no valid hits.
    Applies four guards from §4.4.2 C3.
    """
```

**流程**：
1. 用 `user_message + agent.memory.task_summary` 作为 query 调 `Retrieval.search`；
2. 过滤：score < min_score → drop；tombstoned → drop；path in
   `agent.session["recently_recalled"]`（当轮 + 前 2 轮，见下）→ drop；
3. 取 top-2；每条抽 "第一段"（`\n\n` 前的所有行，或 max_tokens_per_note tail_clip）；
4. 渲染成 `<system-reminder><pico:recalled_memory ...>...</pico:recalled_memory>
   </system-reminder>` 块；
5. 更新 `agent.session["recently_recalled"]`（deque，保留最近 3 轮 recall 过的 path）。

**Recently-recalled 状态存储**：在 `session["recently_recalled"] = deque(maxlen=3)`
里，每个元素是**当轮**recall 的 path 列表。这个字段与 session_store 一起持久化，
崩溃恢复后不丢；重启后从 session file load。

**API contract**：recall.py 不修改 disk，只读；不写 sidecar 文件；无副作用。

---

### 5.5 `memory_save` Tool 增强

`memory_save` 接受可选 `topic` 参数：

- 有 `topic`：写 `agent/<topic>.md`。首次创建时生成 frontmatter：
  ```
  ---
  name: <topic>
  type: <参数指定，默认 feedback>
  description: <取 note 首行前 80 字符>
  tags: []
  aliases: []
  supersedes: []
  ---
  ```
  已存在时 append body（frontmatter 不动）。
- 无 `topic`：走原 `agent_notes.md` 追加行为——直到 migrator 跑过后完全退休。

---

### 5.6 Migration (`pico-cli memory migrate`)

**推荐算法**（简化，避免自动 topic 猜测）：

```
默认: 把所有老 agent_notes.md 内容整体写入 agent/legacy-import.md，
      frontmatter type=feedback, description="Migrated legacy agent notes"

--split：尝试按时间戳条目切分成多个 agent/legacy-<N>.md 文件
--rollback：把 agent_notes.md.legacy 恢复为 agent_notes.md，删除 agent/legacy-*.md
--dry-run：预览操作，不写盘
```

理由：自动 topic 猜测容易出错，人工事后按 topic 拆分反而更精准；默认策略保证
"迁移不会丢信息"。

**Backup**：migrator 执行前**总是**先 backup 到
`.pico/memory/backup/agent_notes.md.<timestamp>`。

---

## 6 · Context Assembly

### 6.1 New Entry Point

`ContextManager.build(user_message)` 返回签名变更：

```python
# 旧
prompt: str, metadata: dict

# 新
request: dict, metadata: dict

# request 结构
{
    "system":   list[dict],              # [{"type":"text","text":...,"cache_control":...}]
    "tools":    list[dict],              # [{"name":..., "input_schema":..., "description":...}]
    "messages": list[dict],              # 完整历史 + 当轮 user 消息（已注入 <system-reminder>）
    "cache_control_breakpoints": list[int]   # message index，本轮为 [len(messages) - 2]
}
```

`ContextManager.build()` 内部流程：

1. Build `system` block（复用 `build_prompt_prefix` 的稳定部分文本）；
2. Build `tools` list（从 `agent.tools` 转换）；
3. Load session messages；
4. 生成当轮 user 消息内容 = injection blocks + user_message；
5. Append 到 messages；
6. Enforce budget（§6.4）；
7. Compute cache breakpoints；
8. Return `(request, metadata)`。

---

### 6.2 Injection Renderer (`pico/context/renderer.py`)

**入口**：

```python
def render_current_user_message(agent, user_message: str) -> str:
    profile = intent.classify(user_message)                    # §4.4.3
    telemetry_intent = {"name": ..., "matched_keyword": ...}

    blocks = []
    for source_name in ("workspace_state", "project_structure",
                        "memory_index", "recalled_memory", "checkpoint"):
        budget_tokens = profile.get(source_name, 0)
        if budget_tokens <= 0:
            continue
        raw = SOURCES[source_name](agent, budget_tokens)       # per-source function
        if not raw:
            continue
        escaped = escape_pico_tags(raw)
        blocks.append(f"<system-reminder>\n<pico:{source_name}>\n{escaped}\n</pico:{source_name}>\n</system-reminder>")

    return "\n\n".join(blocks + [user_message])
```

**Token estimation**：优先用 `model_client.count_tokens(text)`；缺失时退回
`len(text) // 4`。

---

### 6.3 Tool Result Digest (`pico/context/digest.py`)

```python
@dataclass(frozen=True)
class ToolResultDigest:
    tool: str
    title: str
    bullets: list[str]
    source_hash: str    # sha256(result) 前 16 字符
    raw_path: str       # 磁盘存储路径
```

**决策时机**：tool 执行完成后，`agent_loop` 追加 `tool_result` message 时**立即**
决定：

- **若结果 ≤ 阈值**（默认 1200 char）：原样进 messages；
- **若结果 > 阈值**：
  1. 生成 digest；
  2. 把 **原始 result 写盘**到
     `.pico/runs/<run_id>/tool_results/<source_hash>.txt`
     （复用 `RunStore` 逻辑，非 session 内）；
  3. Message content 存 digest 渲染后的短文本 + `raw_path` provenance；
  4. `_pico_meta.digest_applied = True`。

**为何原始 result 不进 session**：
- 长会话 session 文件会极速膨胀（每次 run_shell 数十 KB × 数百次 = 数 MB）；
- Redact 逻辑与 RunStore 一致；
- 可独立 GC（`pico-cli runs prune`，未来添加）。

**Summarizer dispatch**：

```python
_DIGESTERS = {
    "read_file": _digest_read_file,   # imports / 顶层符号 / 行数
    "run_shell": _digest_run_shell,   # exit code / 首 3 行 stdout / 末 3 行 stderr
    "grep":      _digest_grep,        # 命中数 + 前 5 条
}
# 其余 tool 走 _tail_clip_digest：title=tool_name, bullets=[tail 3 行]
```

Summarizer 抛异常时统一走 `_tail_clip_digest` 兜底，
`telemetry.digest.fallback_count` +1。

**回读能力**：P3 阶段**不添加**回读 tool。原始 result 通过 `raw_path` provenance
在 digest 里可见；agent 若真的需要，可以用现有 `read_file` 读那个 raw 文件——避免
新增 tool 面。若未来观察到实际需求再另开 spec 加 `read_full_tool_result`。

---

### 6.4 Budget Enforcement

| 层 | 策略 |
| -- | -- |
| **Layer 1 + Layer 2** | Pinned。合计 > `system_tools_hard_cap`（默认 20K token）→ fail-loud（抛 `SystemTooBig` 异常，不发请求）。表示 workspace.stable_text() 或 tools 定义异常。 |
| **Layer 3 (历史 messages)** | Message 层面 drop。从最老三元组开始整块 drop，直到累计 ≤ `history_soft_cap`（默认 40K token）。最近 `history_floor_messages` 条（默认 6）保留不 drop。Drop 三元组 = `(user, assistant?, tool_result?)`，保持 Anthropic API 合法性。 |
| **Layer 4 (注入)** | 按 intent profile 上限分配（§4.4.3）。每源渲染时超本源 hard_cap → `_tail_clip` + telemetry 计数。 |
| **Layer 5 (当轮 user 真实输入)** | Never truncated。若 user_message > total_budget → fail-loud（`UserMessageTooBig`）。 |

**Drop 顺序保证 Anthropic API 合规**：
- Assistant 消息含 `tool_use` 时，紧邻的 `tool_result` 消息必须同时保留或同时丢弃；
- Migrator/drop 逻辑通过检测 `_pico_meta.tool_use_id` 配对。

---

### 6.5 Clean-up (随本设计一并做)

- 三个同义 hash 字段（`base_prefix_hash` / `stable_prefix_hash` / `prompt_cache_key`）
  合并为 `system_cache_key`；
- 删除 `pico/working_memory.py`（当前无 producer/consumer 的死代码）；
- 删除 `feature_flags["relevant_memory"]`（悬空 flag，无消费点）；
- `session["history"]` → `session["messages"]`，session_store schema 版本 bump 至 2。

---

## 7 · Provider Adaptation

### 7.1 Provider Interface

```python
class Provider(Protocol):
    supports_prompt_cache: bool
    supports_native_tools: bool

    def complete(
        self, *,
        system: list[dict],
        tools: list[dict],
        messages: list[dict],
        max_tokens: int,
        cache_breakpoints: list[int] | None = None,
    ) -> Response:
        ...

@dataclass
class Response:
    stop_reason: StopReason
    content: list[dict]  # [{"type":"text","text":...}] 或 [{"type":"tool_use","id":...,"name":...,"input":...}]
    usage: dict          # {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, ...}

class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
```

Pico 内部只依赖 `StopReason` 枚举；adapter 层做 provider-specific 字符串映射。

---

### 7.2 Anthropic Adapter

直接映射到 Anthropic `messages.create()`：

- `system` → `system` 参数（原样传 content-block 列表，带 cache_control）；
- `tools` → `tools` 参数（原样传）；
- `messages` → `messages` 参数（**发送前剥离 `_pico_meta`**）；
- `cache_breakpoints` → 在指定 message index 上打
  `cache_control: {"type": "ephemeral"}`。

`stop_reason` 直接对应枚举。

---

### 7.3 Fallback Adapter (Local / Non-tool-use)

对不支持 `tool_use` 的 provider（本地 llama.cpp 类）：

**发送侧**：
```
adapter.complete(system, tools, messages, ...):
    # 1. 展平 system + tools + messages 为一个 prompt string
    prefix = flatten_system(system) + "\n\n" + flatten_tools(tools) + TOOL_EXAMPLES
    transcript = "Transcript:\n" + flatten_messages(messages)
    prompt = prefix + "\n\n" + transcript
    # 2. 调 provider.complete_raw(prompt)
    raw = provider.complete_raw(prompt, max_tokens)
    return parse_response(raw)
```

**接收侧**（复用现有 `model_output_parser`）：
```
parse_response(raw):
    kind, payload = model_output_parser.parse(raw)  # <tool> or <final>
    if kind == "tool":
        return Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type":"tool_use","id":f"toolu_local_{uuid}", "name":payload["name"], "input":payload["args"]}],
            usage={...}
        )
    if kind == "final":
        return Response(stop_reason=StopReason.END_TURN, content=[{"type":"text","text":payload}], usage={...})
```

上层 `ContextManager` 与 `agent_loop` 对此无感——它照样发 `{system, tools,
messages}`，看到的 `Response` 与 Anthropic 一致。

---

### 7.4 Delegation Compatibility

现有 `Pico._delegate()` 机制在新架构下的行为：

- **子 pico 独立**：有自己的 system + tools + messages，不继承父的 messages 数组；
- **父子消息隔离**：父的 messages 数组**不**传给子；子从 `user_message`（父传入的
  任务描述）开始新会话；
- **子完成后返回**：给父一个 `final_answer` 字符串 + `tool_change_ids` 列表；父在
  自己的 messages 数组里追加一条 assistant 消息记录 delegate 结果；
- **子 pico 的 session/run/checkpoint 独立**——现有 `runtime.py` 已经这样做，
  本次不改。

---

## 8 · Data Migration

### 8.1 Session Store

**惰性升级**（session_store `load()` 时执行）：

```python
def _migrate_session_v1_to_v2(session: dict) -> dict:
    if session.get("schema_version", 1) >= 2:
        return session

    old_history = session.pop("history", [])
    messages = []
    for old_entry in old_history:
        created_at = old_entry.get("created_at")
        if old_entry["role"] == "tool":
            tool_use_id = f"toolu_migrated_{uuid.uuid4().hex[:8]}"
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": old_entry["name"],
                    "input": old_entry["args"],
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
            messages.append({
                "role": "user",  # ← 不是 "tool"
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": old_entry["content"],
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
        else:  # user / assistant
            messages.append({
                "role": old_entry["role"],
                "content": old_entry["content"],
                "_pico_meta": {"created_at": created_at},
            })

    session["messages"] = messages
    session["recently_recalled"] = []
    session["schema_version"] = 2
    return session
```

**Backup**：`session_store.save()` 首次执行升级时，把老 session 存到
`.pico/sessions/backup/<session-id>.v1.json`——万一新代码 bug，能回滚。

---

### 8.2 Checkpoint Compatibility

Recovery checkpoint 里若 embed 了老 `history` 快照（`checkpointlib.render_checkpoint_text`
不 embed，只引用），本次改造不受影响。若发现 embed 情况，session_store migrator
同步处理。

**Fail-fast 兜底**：若 checkpoint schema version < 2 且无法自动 migrate，`agent`
启动时 log 一条 warning 并跳过 resume，让 agent 从头开始新 turn——这是保守但安全
的策略。

---

### 8.3 Memory Store

`agent_notes.md` → `agent/*.md` 迁移见 §5.6，走显式 CLI（`pico-cli memory
migrate`），不做惰性迁移——避免 agent 首次运行时突然文件变多带来困惑。

---

## 9 · Observability

Telemetry 字段（写入 trace + report）：

| 字段 | 含义 |
| -- | -- |
| `system_cache_key` | 系统层 hash |
| `system_tokens` | Layer 1 token 数 |
| `tools_tokens` | Layer 2 token 数 |
| `messages_count` | 当前 messages 数组长度 |
| `messages_tokens` | messages 总 token 估算 |
| `intent.name` | 命中的 intent（default/debug/recall/structural） |
| `intent.matched_keyword` | 命中的关键词（可能为空） |
| `injection_tokens[source]` | 各注入源实际渲染 token |
| `injection_truncated[source]` | 各源触发截断次数 |
| `injection_dropped[source]` | 因合计超注入预算被丢弃的源 |
| `recall.hits` | recall top-k 数量 |
| `recall.expanded` | link expansion 增加数量 |
| `recall.tombstoned` | 被 tombstone 过滤数量 |
| `recall.recently_skipped` | 被 recently-recalled 过滤数量 |
| `digest.applied_count` | 本 turn 生成 digest 的数量 |
| `digest.fallback_count` | fallback tail_clip 次数 |
| `dropped_messages` | 因预算 drop 的 message 三元组数 |
| `cache_breakpoints` | 打了断点的 message index 列表 |
| `cache_creation_input_tokens` | Provider 返回的 cache creation token |
| `cache_read_input_tokens` | Provider 返回的 cache read token（越高越好） |

---

## 10 · Configuration (pico.toml)

```toml
[context]
system_tools_hard_cap    = 20000    # token
history_soft_cap         = 40000
history_floor_messages   = 6
injection_budget_ratio   = 0.15     # 注入预算占 total_budget 的比例
# total_budget = min(model_client.context_window * total_budget_ratio, total_budget_hard_cap)
# 若 model_client 不提供 context_window，退回 total_budget_hard_cap
total_budget_ratio       = 0.6
total_budget_hard_cap    = 100000

[context.digest]
size_threshold_chars     = 1200     # 超此阈值走 digest

# 完整 intent profile 默认配置
[context.intent.structural]
keywords = ["架构", "结构", "怎么组织", "目录", "layout", "architecture"]
budget.project_structure = 2000
budget.memory_index      = 400
budget.recalled_memory   = 800
budget.workspace_state   = 300

[context.intent.debug]
keywords = ["报错", "error", "traceback", "fail", "not working", "broken", "崩溃"]
budget.workspace_state   = 1200
budget.recalled_memory   = 600
budget.project_structure = 200
budget.memory_index      = 200

[context.intent.recall]
keywords = ["上次", "之前", "记得", "past", "previous", "last time"]
budget.recalled_memory   = 1600
budget.memory_index      = 800
budget.project_structure = 200
budget.workspace_state   = 300

[context.intent.default]
keywords = []
budget.project_structure = 600
budget.memory_index      = 400
budget.recalled_memory   = 600
budget.workspace_state   = 500

[memory]
notes_read_only              = true
recall.min_score             = 0.3
recall.top_k                 = 2
recall.max_tokens_per_note   = 400
recall.skip_recent_turns     = 2
retrieval.field_boost.name         = 5.0
retrieval.field_boost.description  = 3.0
retrieval.field_boost.tags         = 4.0
retrieval.field_boost.aliases      = 4.0
retrieval.field_boost.body         = 1.0
retrieval.link.max_added   = 3
retrieval.link.decay       = 0.4
```

所有键均有默认值，`pico.toml` 缺失或字段缺失时不报错。

---

## 11 · Phased Rollout

### P1 · Message-based Paradigm Migration

**内容**：
- Provider interface 定义 + Anthropic adapter + Fallback adapter；
- `ContextManager.build()` 签名变更为返回 `{system, tools, messages}`；
- `model_output_parser` 主路径迁移到 native tool_use；
- `agent_loop.record` 追加 message 结构（含 `_pico_meta`）；
- session_store migrator + backup 机制；
- Clean-up：三 hash 合一 / 删 WorkingMemory / 删 relevant_memory flag。

**Definition of Done**：
- 老 session 能被加载并自动 migrate；
- Anthropic 端 native tool_use 端到端可用；
- Fallback adapter 通过与老 XML 协议的回归测试；
- Cache_control 断点 1 命中被 `cache_read_input_tokens` 观测到；
- `pytest` 通过。

**用户可感收益**：Prompt 结构自然、native tool_use、cache 断点 1 命中。

### P2 · Dynamic Injection + Intent Budget

**内容**：
- `<system-reminder>` 注入体系：workspace_state / memory_index / project_structure / checkpoint（**不含 recalled_memory**）；
- `<pico:*>` 命名空间与 escape_pico_tags；
- `pico/context/intent.py`（regex 分类 + first-match-wins）；
- Injection budget profile（4 个默认 profile）；
- `pico/context/renderer.py` 统一渲染入口；
- Cache_control 断点 2（messages 末尾滚动）。

**Definition of Done**：
- `<system-reminder>` 在 Anthropic prompt 中正确出现；
- Intent 分类命中率符合预期（`pytest` 覆盖每个 intent）；
- Escape 转义防御通过（用户 note 里含 `<pico:` 字面串场景）；
- Cache_control 断点 2 观测到复用 tokens；
- 各 injection source 独立 hard_cap 生效。

**用户可感收益**：每轮上下文动态调节、老消息字节稳定、cache 断点 2 命中。

### P3 · Memory Structuring + Recall + Digest

**内容**：
- `.pico/memory/agent/` 目录；
- Frontmatter parser（stdlib 手写）；
- Tombstone filter + link expansion；
- `memory_save(topic=...)` 参数；
- `pico-cli memory migrate` CLI（含 --dry-run / --rollback / --split）；
- `pico/memory/recall.py` + 四重护栏；
- `pico/context/digest.py` + per-tool summarizer（read_file / run_shell / grep + fallback）；
- Recalled_memory 注入接入 renderer。

**Definition of Done**：
- Agent 新写 note 使用 topic 参数落到 `agent/`；
- Migrator 可将 `agent_notes.md` 迁到 `agent/legacy-import.md`（backup + rollback 可用）；
- 四重护栏各触发场景测试通过；
- Digest tail_clip fallback 兜底测试通过；
- Long-session token 使用相比 baseline 下降。

**用户可感收益**：Agent 主动召回相关记忆、长会话 token 大幅节省。

---

## 12 · Testing Strategy

### P1 测试

- `test_session_migrator_v1_to_v2`：老 history → 新 messages 转换正确性；
- `test_session_migrator_backup_generated`：升级前 backup 文件生成；
- `test_provider_response_normalization`：Anthropic / fallback response 归一到统一
  Response 结构；
- `test_cache_control_placement`：断点 1 打在正确的 system content block 上；
- `test_native_tool_use_end_to_end`：一次完整的 tool_use → tool_result → final
  流程；
- `test_fallback_xml_parse_compat`：fallback adapter 与老 XML 协议兼容；
- `test_message_append_stability`：一旦 append 的 message 不被后续修改。

### P2 测试

- `test_injection_render_escape`：内部内容含 `<pico:` 字面串时被正确转义（zero-width
  space 插入）；
- `test_intent_first_match_wins`：一句同时含 debug 和 recall 关键词的 message 命中
  debug（优先级更高）；
- `test_intent_default_fallback`：无关键词命中的 user_message 走 default；
- `test_injection_budget_enforcement`：超 hard_cap 的 source 被截断且 telemetry
  计数；
- `test_injection_source_drop_order`：合计超 injection_budget 时按声明顺序丢末尾；
- `test_pinned_overflow_failloud`：system + tools 超预算抛 `SystemTooBig`；
- `test_cache_breakpoint_2_placement`：断点 2 在 `messages[-2]` 上；
- `test_message_pair_drop_atomicity`：drop 时 tool_use / tool_result 成对同时丢或
  同时留。

### P3 测试

- `test_memory_frontmatter_parse_valid`：合法 frontmatter 正确解析；
- `test_memory_frontmatter_parse_missing`：无 frontmatter 视为 body-only；
- `test_memory_frontmatter_parse_malformed`：非法结构不崩溃；
- `test_tombstone_filter`：被 supersede 的 note 不出现在检索结果；
- `test_link_expansion_bounds`：max_added / decay / 深度上限被遵守；
- `test_recall_four_guards`：min_score / max_tokens / tombstone / recently-recalled
  四条护栏各自触发场景；
- `test_recall_provenance_render`：recall block 包含 path/type/score/why 字段；
- `test_digest_fallback_on_exception`：summarizer 抛错走 tail_clip；
- `test_tool_result_raw_ondisk`：原始 result 存在 runs/tool_results/ 而非 session；
- `test_memory_save_topic_new_file`：`memory_save(topic="foo")` 新建
  `agent/foo.md` + frontmatter；
- `test_memory_save_topic_existing`：已存在时 append body（frontmatter 不动）；
- `test_memory_migrate_dry_run`：--dry-run 不写盘；
- `test_memory_migrate_rollback`：--rollback 恢复原文件。

---

## 13 · Risks & Mitigations

按发生概率 × 影响排序：

| # | Risk | Prob | Impact | Mitigation |
| -- | -- | -- | -- | -- |
| 1 | Anthropic `cache_control` 位置理解错，P1 上线后 cache 不命中 | 中 | 高 | `test_cache_control_placement` + 人工验证 `cache_read_input_tokens` |
| 2 | Migrator 生成的 tool_use_id 与新会话生成的 id 冲突 | 低 | 中 | 用 `toolu_migrated_` 前缀区分 |
| 3 | Fallback adapter 与 native adapter 行为分裂 | 中 | 中 | 每个 test 用双 provider 跑 |
| 4 | `tool_result_raw` 磁盘文件累积无 GC | 高 | 低 | P3 只写盘、明确 GC 由 `pico-cli runs prune` 未来提供 |
| 5 | Recall 命中错时占位 recalled_memory 挤占预算 | 中 | 中 | `min_score=0.3` 门槛（已在 §4.4.2） |
| 6 | XML injection——用户 note 里含 `<pico:` 未转义 | 中 | 中 | Zero-width space 转义（§4.4.2 C2） |
| 7 | Intent regex 命中冲突 | 高 | 低 | first-match-wins 规则（§4.4.3） |
| 8 | Delegate 子 pico 在新架构下行为异常 | 中 | 中 | §7.4 delegation 章节 + 独立 e2e test |
| 9 | Session backup 生成失败导致升级流程失败 | 低 | 中 | Backup 写失败 fail-loud，不覆盖原文件 |
| 10 | Anthropic API 5min TTL 到期后 cache 完全 miss | 中 | 低 | 已知限制，长闲置场景可接受，无缓解 |

---

## 14 · Open Questions

- **Q1（已收敛）**：`agent/` 目录 workspace + user scope 双写（§5.1）。
- **Q2（已收敛）**：`read_full_tool_result` tool P3 不加，raw file 可通过 `read_file`
  访问 raw_path（§6.3）。
- **Q3（已收敛）**：使用 `pico:` 命名空间前缀（§4.4.2）。
- **Q4**：Digest 通用 fallback 是否需要格式感知（JSON / plaintext / diff）？当前
  统一 tail 3 行。倾向 P3 上线后观察，若发现某类 tool 摘要质量差再补 per-type
  fallback。不影响架构落地。
