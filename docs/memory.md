# Pico Memory

Pico 把 Durable Memory 与 Session Summary 严格分层：User Notes / Agent Notes 是跨 Session 的长期事实；
compaction、branch summary、task checkpoint 和 recent-recall 去重都属于 Session Tree，不会自动写入 Durable
Memory。

## 文件模型与权限

| scope | User Notes | Agent Notes |
| --- | --- | --- |
| workspace | `.pico/memory/notes/**/*.md` | `.pico/memory/agent_notes.md` |
| user | `~/.pico/memory/notes/**/*.md` | `~/.pico/memory/agent_notes.md` |

User Notes 由用户维护，agent 只能 list/read/search。Agent Notes 是每个 scope 唯一的 append-only 文件；只有
当前 top-level turn 获得明确授权时才能通过 `memory_save` 追加：

- `/remember <text>`；
- 当前用户明确要求“记住/保存到 memory”；
- delegated agent、compaction、后台任务和历史 turn 不能继承写权限；
- 否定表达或对“please remember”这句话的讨论不构成授权。

单次 `memory_save` 最多 1,024 model tokens，同时受 16 KiB storage hard cap 限制。Agent Notes 在 64 KiB
发出 soft warning，128 KiB hard fail；User Notes 也保留 128 KiB 单文件安全上限。追加使用 per-scope lock、
owner-only 目录/文件、no-follow regular-file 检查与原子 replace。

## Query Snapshot 与缓存

一个 top-level turn 只创建一个 `MemoryQuerySnapshot`。Memory index、recall、BM25 scoring、snippet 选择和一跳
link expansion 共享同一份 raw documents 与 index，不会为每个 Context Source 重复打开文件。

`Retrieval` 可以跨 turn 复用已解析 snapshot，但复用前会对两个 scope 做 bounded、no-follow inventory，比较
root identity、path、device/inode、size、mtime、ctime、link count 与 file type。新增、修改、删除或替换文件会
立即使 cache 失效；inventory 不安全或在加载期间变化时不缓存。cache 只减少重复读取与分词，不改变 canonical
Memory，也不放宽 symlink/hardlink/FIFO/越界/大小限制。

自动扫描上限为 512 个文件、128 KiB/文件、2 MiB 总输入。一个不安全文件被跳过，不会把外部内容带入 index
或 prompt。

## Retrieval 与 recall

检索保持 stdlib-only BM25 + CJK bigram，不依赖向量数据库、embedding 服务或后台 daemon。frontmatter 的
`name`、`description`、`tags`、`aliases` 有字段权重；`supersedes` 形成 tombstone，使冲突、过期或删除的旧
fact 在建索引时退出。`[[note-name]]` 只做深度 1、最多 3 条、分数衰减的一跳扩展。

默认 recall 合同：

| 参数 | 默认值 |
| --- | ---: |
| `top_k` | 6 |
| `min_score` | 0.3（按本次最高分归一化） |
| 每条 passage | 1,024 tokens |
| `recalled_memory` source 总 cap | 6,144 tokens |
| recent-turn 去重 | 2 turns |

query 由当前用户输入、active goal 和近期文件路径组成。每个 hit 渲染与 query 最匹配的 paragraph，而不是固定
取文件首段。Agent Notes 按 `- <ISO timestamp>  <note>` 分成独立逻辑 blocks，例如
`workspace/agent_notes.md#entry-4`，避免整份大文件作为一个文档参与排名。

recent recall 列表是可重建 cache，不属于 canonical Session entry。Compaction 只生成 Session Summary，绝不把
模型摘要自动提升为长期事实。

## 工具与 CLI

- `memory_list`：列出两个 scope 的安全 metadata；
- `memory_read`：按 canonical path 与行范围读取；
- `memory_search`：返回 BM25 排序结果和 bounded snippets；
- `memory_save`：在当前显式授权下追加 Agent Note；
- `pico memory list|show|search|review`：不启动模型的只读检查入口。

canonical path 示例：`workspace/notes/auth.md`、`workspace/agent_notes.md#entry-2`、
`user/notes/preferences.md`。绝对路径、`..`、敏感内容和未授权 scope 都会被拒绝。

## 质量与限制

离线 fake benchmark 当前覆盖 33 个场景：中文、paraphrase、冲突事实、stale/superseded、删除 tombstone、
长 notes、prompt injection、无关噪声、跨 scope、多跳和 explicit write。fake 结果验证本地检索、工具 trace 与
权限合同，不代表真实模型能稳定决定“何时搜、如何使用结果”。达到 production-quality 声明前，仍需在当前
exact revision 上用至少两个具备凭据的真实 Provider 运行 live benchmark，并分别报告 false recall、stale fact
和 conflicting fact。

Context 预算和 Session 分层见 [Context、Session 与长会话](context-and-sessions.md)，文件安全不变量见
[安全](security.md)，运行命令见[验证](verification.md)。
