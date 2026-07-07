# Pico Memory / Context Redesign — Design Spec

Date: 2026-07-07
Status: Draft — awaiting user review before writing implementation plan

## 1 · Motivation

Pico 当前的 memory 与 context 设计有两条主线级别的问题：

1. **抽象错位**：Prompt 被当作"一个大字符串"来拼接（`prefix / history / current_request`
   三段），把身份规则、tools schema、workspace 快照、memory index、历史与用户请求
   全部拼进同一根字符串。这导致：
   - Provider 原生的 tool_use API 无法使用，模型要解析手写 `<tool>...</tool>` XML；
   - Provider 原生的 prompt caching 只能通过"稳定 prefix 字符串 hash"近似利用，
     而不是官方支持的多断点 `cache_control`；
   - 动态信息（`memory_index`、`project_structure`）被硬塞进"稳定 prefix"，与
     "稳定"承诺自相矛盾；
   - 工作区快照、resume checkpoint 被挂到 `history section` 前，命名与语义分裂。

2. **Memory 是"档案柜"而不是"工作台"**：Agent 通过 `memory_index` 只能看到"有哪些
   文件"，无法自动看到相关内容。BM25 检索存在，但 agent 需要主动调用
   `memory_search` / `memory_read` 才能获取。没有 per-topic 结构、没有失效机制、
   没有链接图——记忆只增不减，随着时间推移信号被噪声稀释。

这份设计把 pico 从 **prompt-as-string 范式**迁移到 **prompt-as-message-array 范式**，
参考 Claude Code 的架构思想；同时把 memory 从"追加流水"升级为"结构化知识 +
主动召回"。

## 2 · Non-Goals

- 不引入 embedding-based 语义检索。BM25 + CJK bigram 保留为检索底座。
- 不引入向量数据库、外部依赖。stdlib-only 是硬约束。
- 不做 salience / access-driven ranking。单人本地场景 note 数量小，收益不足。
- 不做 ContextItem 全局优化器 / knapsack 求解。静态分配即可。
- 不重写 checkpoint / recovery / trace / run_store 等子系统——它们与 prompt 结构
  独立。
- 不新增 `.pico/memory/.state/` 目录。所有 memory 状态保留在 note 文件本体。

## 3 · Target Architecture

Pico 每一轮向模型发送的不再是"prompt 字符串"，而是**三个 API-level 字段 + 一个消息
数组 + 一套动态注入规则 + 两个 cache 断点**。

```
Request = {
  system:   <SYSTEM_CORE>              [cache_control: ephemeral, 断点 1]
  tools:    [ {name, input_schema, description}, ... ]
  messages: [
    ...历史 user/assistant/tool 消息 (字节稳定)...
    ─────────────────────── cache_control: ephemeral, 断点 2 (上一轮末) ───
    user {
      <system-reminder><workspace_state>...</workspace_state></system-reminder>
      <system-reminder><memory_index>...</memory_index></system-reminder>
      <system-reminder><recalled_memory>...</recalled_memory></system-reminder>
      <system-reminder><project_structure>...</project_structure></system-reminder>
      <system-reminder><checkpoint>...</checkpoint></system-reminder>  (若有)
      [用户当轮真实输入]
    }
  ]
}
```

### 3.1 Layer 1 · `system` 字段

只装 session 内绝对不变的稳定内容：

- 身份声明
- 输出协议规则
- 通用行为规则（"before writing tests, read impl first" 等）
- `MEMORY_USAGE_GUIDANCE` 与 `MEMORY_READING_GUIDANCE`
- `workspace.stable_text()`：cwd / repo_root / default_branch / project_docs

**明确不进 system**：tools schema、workspace_state、memory_index、project_structure、
recalled_memory、history。

打上 `cache_control: {type: "ephemeral"}`。整段生命周期 = session（除非 workspace
静态事实变化触发 rebuild）。

### 3.2 Layer 2 · `tools` 字段

Provider API 原生字段。每条 tool 定义：

```python
{
  "name": "read_file",
  "description": "Read a file from the workspace.",
  "input_schema": {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"]
  }
}
```

Pico 现有 `pico/tools.py` 里的 tool 定义（schema、risky、description）直接映射到
此结构。原 `prompt_prefix.py` 里手写的 `- name(fields) [risk] description` 文本块
**完全删除**。

**输出协议同步变更**：`model_output_parser` 主路径不再解析 `<tool>...</tool>` /
`<final>...</final>` XML，改为消费 provider 原生的 `tool_use` block 与
`stop_reason=end_turn` 语义。对不支持 native tool_use 的 provider（本地小模型），
XML 协议由 provider adapter 内部转换保留（详见 §7.3），上层 `ContextManager` 与
`agent_loop` 对此无感。

### 3.3 Layer 3 · `messages` 数组

真实对话历史。每条 message 是结构化字典：

```python
{"role": "user",      "content": "..."}
{"role": "assistant", "content": [{"type": "tool_use", "id": "...", "name": "...", "input": {...}}]}
{"role": "tool",      "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}
```

**Message 一旦追加，不再回头修改**——这保证 messages 数组的历史前缀字节稳定，
上一轮末尾的 `cache_control` 断点才能命中。

现有 `session["history"]` 里的 `[{role, name, args, content, created_at}, ...]`
结构在 P1 阶段迁移为 message list（见 §6）。

### 3.4 Layer 4 · 动态注入（当轮 user 消息）

每轮**只**在**当轮 user 消息**内注入 `<system-reminder>` 块。历史消息不重写。

注入源：

- **workspace_state**：branch / status / recent_commits
- **memory_index**：可用 memory 文件列表 + first_line
- **project_structure**：top-level tree + language stats
- **recalled_memory**：本轮 top-2 相关 note 首段 + provenance（path / type / score / why）
- **checkpoint**：若存在 recovery checkpoint

**注入契约**（C1-C5）：

- **C1 · 命名空间**：所有系统结构标签使用 `<system-reminder>` 与 `<pico:*>` 前缀。
  `<pico:workspace_state>`、`<pico:memory_index>` 等。这样即使模型看到用户/tool
  内容里恰好有 `<memory_index>` 字面串，也不会与系统结构混淆。
- **C2 · 转义**：注入内容渲染前扫描 `<pico:` / `</pico:` 序列并转义（改为
  `<pico:` → `<pico\:`，简单字符插入）。
- **C3 · Recall 四重保护**：
  - `min_score`：默认 0.3（BM25 归一化后）
  - `max_tokens_per_note`：默认 400 token
  - Skip superseded / tombstoned notes
  - Skip recently-recalled（当轮 + 前 2 轮已 recall 的 path）
- **C4 · 每条 recall 带 provenance**：`<pico:recalled_memory path="..." type="feedback" score="0.72" why="matched:auth,session">...</pico:recalled_memory>`
- **C5 · Intent-driven token budget**：见 §3.6。

### 3.5 Layer 5 · Cache 分层

两个 `cache_control: ephemeral` 断点：

- **断点 1**：`system` + `tools` 之后。session 内极少失效——只在 workspace 静态
  事实（cwd/repo_root/default_branch/project_docs）或 tools schema 变化时才 rebuild。
- **断点 2**：`messages[0..N-1]` 之后，即上一轮末尾。本轮开头命中，模型只需处理
  当轮 user 消息。

**Cache 契约**：
- 任何进入 Layer 1 的内容必须证明"session 内字节稳定"，否则归 Layer 4 注入；
- Message 一旦落进 messages 数组不再修改（tool result digest 替换在追加时决定，
  而非事后重写，见 §5.3）；
- 若 provider 不支持 `cache_control`（本地小模型），跳过断点参数，其余逻辑不变。

### 3.6 Intent-Driven Injection Budget

基于当轮 user_message 的关键词正则做 5-10 条 intent 判定，映射到一个
**注入预算 profile**（只决定 Layer 4 各注入项的 token 上限，不动 Layer 1/2/3）：

```python
INTENT_PROFILES = {
  "structural": {  # "怎么组织的", "架构", "目录结构"
    "project_structure": 2000,
    "memory_index":       400,
    "recalled_memory":    800,
    "workspace_state":    300,
  },
  "debug": {       # "报错", "traceback", "fail", "not working"
    "workspace_state":   1200,   # 更多 branch/status/commits
    "recalled_memory":    600,
    "project_structure":  200,
    "memory_index":       200,
  },
  "recall": {      # "上次", "之前", "记得", "past"
    "recalled_memory":   1600,
    "memory_index":       800,
    "project_structure":  200,
    "workspace_state":    300,
  },
  "default": {
    "project_structure":  600,
    "memory_index":       400,
    "recalled_memory":    600,
    "workspace_state":    500,
  },
}
```

Intent 识别器：`pico/context/intent.py`，纯正则匹配，命中带 `matched_reason`。
每 profile 返回一个 dict，注入渲染时按 profile 上限截断。**Intent 判定失败 →
default profile**，永不抛错。

## 4 · Memory Storage

### 4.1 Layout

```
.pico/memory/
├── notes/            用户手写，agent 只读（不变）
├── agent/            新增，agent 可读写，per-topic
│   ├── prompt-cache-invariant.md
│   ├── auth-middleware.md
│   └── ...
└── agent_notes.md    legacy，一次性 migrator 拆分到 agent/
```

### 4.2 Frontmatter Schema

每个 `agent/` 或 `notes/` 下的 markdown 文件开头：

```markdown
---
name: prompt-cache-invariant
type: feedback           # user | feedback | project | reference
description: memory_index 必须放 volatile head 才能不拖累 cache
tags: [context, cache]
aliases: []              # 可选，同义词/别名
supersedes: []           # 可选，声明它 obsolete 掉哪些旧 name
---

正文...参见 [[context-tier-model]]。
```

**字段处理**：
- `name`：文件名 stem，唯一 ID；
- `type`：4 值枚举；未识别值退回 `""`；
- `description`：单行；用于 `memory_index` 展示（取代当前的 `first_line`）；
- `tags`：字符串数组；
- `aliases`：字符串数组，同义词/别名；用于 BM25 加权命中；
- `supersedes`：数组，声明本 note obsolete 哪些 `name`。检索时被列出的 `name`
  会被过滤（tombstone 语义）。

**兼容**：无 frontmatter 的文件被视为 body-only，`type=""`，`description=""`。
Migrator 尽力从 body 首行推断 `description`，不做强制迁移。

### 4.3 Retrieval Enhancements

BM25 底座不变。在 `Retrieval.search()` 内加三段小逻辑：

1. **Field boost**：命中 `description` × 3、`name` × 5、`tags` × 4、`aliases` × 4；
   body 命中 × 1。所有加权在 tf 阶段乘以系数，不修改 idf 计算。
2. **Link expansion**：Top-k 命中后，扫命中文档正文里的 `[[name]]`，把这些邻居
   加进结果，得分 × 0.4，每次 query 最多加 3 个邻居，深度上限 1。
3. **Tombstone filter**：加载文档时构建 `superseded_names` 集合，`if entry.name
   in superseded_names: continue`——被 supersede 的 note 从检索池中移除，但磁盘
   文件保留。

### 4.4 memory_save Tool 增强

`memory_save` 接受可选 `topic` 参数：

- 有 `topic`：写 `agent/<topic>.md`。首次创建时生成 frontmatter（type 由参数指定，
  默认 `feedback`）；已存在时 append body（frontmatter 不动）。
- 无 `topic`：走原 `agent_notes.md` 追加行为——直到 migrator 跑过后完全退休。

### 4.5 Migration

一次性 CLI 命令 `pico-cli memory migrate --to-agent-topics`：

- 扫 `agent_notes.md`，按时间戳条目分组；
- 对每条条目用启发式（首个非空词、关键词提取）猜 topic 名；
- 生成 `agent/<topic>.md` 文件，frontmatter type=`feedback`；
- 迁移后将 `agent_notes.md` 重命名为 `agent_notes.md.legacy`，检索层跳过 `.legacy`
  后缀文件；
- `--dry-run` 支持预览；`--rollback` 逆向恢复。

## 5 · Context Assembly

### 5.1 New Entry Point

`ContextManager.build(user_message)` 返回签名变更：

```python
# 旧签名
prompt: str, metadata: dict

# 新签名
request: dict {
    "system":   str,
    "tools":    list[dict],
    "messages": list[dict],
    "cache_control_breakpoints": list[int],  # message index for 断点 2
}, metadata: dict
```

### 5.2 Injection Renderer

`pico/context/renderer.py` 负责生成当轮 user 消息内容：

- 收集 injection sources（workspace_state / memory_index / project_structure /
  recalled_memory / checkpoint）；
- 按 intent profile 分配 token budget；
- 每源渲染成 `<system-reminder><pico:name>...</pico:name></system-reminder>` 块；
- 内部内容通过 `escape_pico_tags(text)` 转义；
- 最后 append 用户真实输入。

**Token estimation**：优先用 `model_client.count_tokens(text)`；缺失时退回
`len(text) // 4`。

### 5.3 Tool Result Digest

`pico/context/digest.py`：

```python
@dataclass(frozen=True)
class ToolResultDigest:
    tool: str
    title: str          # 一行摘要
    bullets: list[str]  # ≤5 条
    source_hash: str    # sha256(result) 前 16 字符
```

**决策时机**：tool 执行完成后，`agent_loop` 追加 `tool` message 时**立即**决定
是否使用 digest 版本：
- 若结果 ≤ 阈值（默认 1200 char）：原样进 messages；
- 若超阈值：调用 per-tool summarizer 生成 digest，message.content 存 digest 渲染
  后的短文本，同时把原始 result 存到 session 侧字典 `session["tool_result_raw"]`
  以便后续需要时回读（read_full_tool_result tool，可选未来添加）。

**这样避免"事后重写 message"**——保证 messages 数组一旦 append 不再修改。

Summarizer dispatch：

```python
_DIGESTERS = {
    "read_file": _digest_read_file,   # 提取 imports / 顶层符号 / 行数
    "run_shell": _digest_run_shell,   # exit code / 首 3 行 stdout / 末 3 行 stderr
    "grep":      _digest_grep,        # 命中数 + 前 5 条
}
# 其余 tool 走 fallback：title=tool_name, bullets=[tail 3 行]
```

Summarizer 抛异常时统一走 `_tail_clip_digest` 兜底，counter `digest.fallback_count`
+1（进 telemetry）。

### 5.4 Budget Enforcement

- **Layer 1 (system) + Layer 2 (tools)**：视为 pinned。若两者合计超过
  `system_tools_hard_cap`（默认 20K token），fail-loud 报错而非静默截断——这
  说明 workspace.stable_text() 或 tools 定义异常，需要人工介入。
- **Layer 3 (messages 历史)**：Message 层面 drop 而非字符串裁剪。从最老的
  `(user, assistant?, tool?)` 三元组开始整块 drop，直到累计 token 落在
  `history_soft_cap` 以下。最近 N 条消息（默认最近 6 条）保留不 drop（floor）。
- **Layer 4 (注入)**：按 intent profile 上限分配。每源渲染时若超本源 hard_cap
  用 `_tail_clip` 截断，`telemetry.injection_truncated[source]` +1。
- **Layer 5 (当轮 user 真实输入)**：永不裁剪。若单条 user_message 超过总预算，
  fail-loud。

### 5.5 Clean-up (随本设计一并做)

- 三个同义 hash 字段（`base_prefix_hash` / `stable_prefix_hash` / `prompt_cache_key`）
  合并为 `system_cache_key` 一个。
- 删除 `pico/working_memory.py`（当前无 producer/consumer 的死代码）。
- 删除 `feature_flags["relevant_memory"]`（悬空 flag，无消费点）。
- `session["history"]` → `session["messages"]`，配套 session_store schema 版本 bump。

## 6 · Data Migration

### 6.1 Session Store

老 session 里 `session["history"] = [{role, name, args, content, created_at}, ...]`，
新格式 `session["messages"] = [{role, content, ...}, ...]`。

Migrator 逻辑（load 时惰性执行，无需 CLI）：

```
for old_entry in session["history"]:
    if old_entry["role"] == "tool":
        # 拆成 assistant tool_use + tool tool_result 两条消息
        messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": <derive>, "name": old_entry["name"], "input": old_entry["args"]}]
        })
        messages.append({
            "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": <derive>, "content": old_entry["content"]}]
        })
    elif old_entry["role"] in ("user", "assistant"):
        messages.append({"role": old_entry["role"], "content": old_entry["content"]})

session["messages"] = messages
session.pop("history", None)
session["schema_version"] = 2
```

老 session 一次性升级；不保留双向兼容。session_store `save()` 只写新格式。

### 6.2 Memory Store

`agent_notes.md` → `agent/*.md` 迁移见 §4.5，走显式 CLI（`pico-cli memory migrate
--to-agent-topics`），不做惰性迁移——避免 agent 首次运行时突然文件变多带来困惑。

## 7 · Provider Adaptation

### 7.1 Provider Interface

```python
class Provider:
    supports_prompt_cache: bool
    supports_native_tools:  bool

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None) -> Response:
        ...

@dataclass
class Response:
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | ...
    content: list     # [{"type": "text", "text": ...}, {"type": "tool_use", ...}]
    usage: dict
```

### 7.2 Anthropic Adapter

直接映射到 `messages.create()`：
- `system` → `system` 参数（带 cache_control）；
- `tools` → `tools` 参数；
- `messages` → `messages` 参数；
- `cache_breakpoints` → 在指定 message 上打 `cache_control: {"type": "ephemeral"}`。

### 7.3 Local / Non-tool-use Adapter (Fallback)

对不支持 `tool_use` 的 provider（本地 llama.cpp 类）：

- Adapter 内部把 system + tools 展平成一个大 prompt string（沿用旧 `prompt_prefix`
  格式），messages 也序列化成 "Transcript:\n[role] content" 文本；
- 输出层解析老式 `<tool>...</tool>` / `<final>...</final>` XML；
- 对上层 `ContextManager` 无感——它照样发 `{system, tools, messages}`，
  provider adapter 负责转换。

这样保留了 pico "本地小模型也能跑"的定位。

## 8 · Observability

Telemetry 字段（写入 trace + report）：

| 字段 | 含义 |
| -- | -- |
| `system_cache_key` | 系统层 hash，用于跨轮 cache 判定 |
| `messages_count` | 当前 messages 数组长度 |
| `messages_tokens` | messages 总 token 估算 |
| `injection_tokens[source]` | 各注入源实际渲染 token |
| `injection_truncated[source]` | 各注入源触发截断次数 |
| `intent` | 命中的 intent 名 + matched_reason |
| `recall.hits` | recall top-k 数量 |
| `recall.expanded` | link expansion 增加数量 |
| `recall.tombstoned` | 被 tombstone 过滤数量 |
| `recall.recently_skipped` | 被 recently-recalled 过滤数量 |
| `digest.applied_count` | 本 turn 生成 digest 的数量 |
| `digest.fallback_count` | fallback tail_clip 次数 |
| `dropped_messages` | 因预算 drop 的 message 三元组数 |
| `cache_breakpoints` | 打了断点的 message index 列表 |

## 9 · Configuration (pico.toml)

```toml
[context]
system_tools_hard_cap  = 20000  # token
history_soft_cap       = 40000
history_floor_messages = 6
# total_budget = min(model_client.context_window * total_budget_ratio, total_budget_hard_cap)
# 若 model_client 不提供 context_window，退回 total_budget_hard_cap
total_budget_ratio     = 0.6
total_budget_hard_cap  = 100000

[context.intent.profiles]
# 覆盖 §3.6 默认值

[memory]
notes_read_only       = true
agent_scope_enabled   = true
recall.min_score      = 0.3
recall.top_k          = 2
recall.max_tokens_per_note = 400
recall.skip_recent_turns   = 2
retrieval.field_boost.description = 3.0
retrieval.field_boost.name        = 5.0
retrieval.field_boost.tags        = 4.0
retrieval.field_boost.aliases     = 4.0
retrieval.link.max_added   = 3
retrieval.link.decay       = 0.4
```

所有键均有默认值，`pico.toml` 缺失或字段缺失时不报错。

## 10 · Phased Rollout

| 阶段 | 内容 | 用户可感收益 |
| -- | -- | -- |
| **P1 · Message 范式迁移** | Provider 接口 / context_manager.build 签名 / model_output_parser / agent_loop.record / session_store migrator；Anthropic + fallback adapter；clean-up (三 hash、WorkingMemory、relevant_memory flag) | Prompt 结构自然、native tool_use、cache 断点利用 |
| **P2 · 动态注入 + intent budget** | `<system-reminder>` 注入器、`<pico:*>` 命名空间与转义、intent regex 分类、injection budget profile、renderer.py | 每轮上下文动态调节、老消息字节稳定 |
| **P3 · Memory 结构化 + Digest** | `agent/` 目录、frontmatter parser、tombstone 过滤、link 扩展、`memory_save(topic=...)`、`memory migrate` CLI、recall.py、digest.py、per-tool summarizer | Agent 主动召回相关记忆、长会话 token 大幅节省 |

每阶段一个 PR，独立可发货、独立可回滚。

## 11 · Testing Strategy

**P1 关键测试**：
- `test_session_migrator`：老 history → 新 messages 转换正确性；
- `test_provider_anthropic`：system/tools/messages/cache_control 参数正确构造；
- `test_provider_fallback`：本地模型 adapter 与旧 XML 协议兼容；
- `test_message_append_stability`：一旦 append 的 message 不被后续修改。

**P2 关键测试**：
- `test_injection_render_escape`：内部内容含 `<pico:` 字面串时被正确转义；
- `test_intent_default_fallback`：无匹配的 user_message 走 default profile；
- `test_injection_budget_enforcement`：超 hard_cap 的注入被截断且 telemetry 计数；
- `test_pinned_overflow_failloud`：system + tools 超预算抛 explicit error。

**P3 关键测试**：
- `test_memory_frontmatter_parse`：合法/非法/缺失 frontmatter 均可处理；
- `test_tombstone_filter`：被 supersede 的 note 不出现在检索结果；
- `test_link_expansion_bounds`：max_added、decay、深度上限被遵守；
- `test_recall_four_guards`：min_score / max_tokens / tombstone / recently-recalled
  四条护栏各自触发的场景；
- `test_digest_fallback_on_exception`：summarizer 抛错走 tail_clip。

## 12 · Open Questions

- **Q1**：`agent/*.md` 目录是否需要区分 workspace scope 与 user scope？当前 §4.1
  只画了 workspace scope。倾向保持与现有 `.pico/memory/` 与 `~/.pico/memory/`
  双 scope 一致，即 `.pico/memory/agent/` 与 `~/.pico/memory/agent/` 都存在。
- **Q2**：`read_full_tool_result` tool 是否 P3 就添加？当前 §5.3 提到了这个可选
  未来 tool，但没纳入本设计范围。倾向 P3 只做 digest 不加 read_full，观察 agent
  是否实际需要"完整回读"能力，若需要再另开 spec。
- **Q3**：`<pico:*>` 命名空间的具体前缀名。候选：`pico:` / `x-pico:` / `sys:`。
  倾向 `pico:` 作为最简单可识别选项。

上述三题不影响架构落地，可在实现阶段结合具体 code review 收敛。
