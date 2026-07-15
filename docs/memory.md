# Pico Memory

Pico Memory 只有 User Notes 与 Agent Notes 两类长期文本，分别存在 workspace scope 和 user scope。

## 文件模型

| scope | User Notes | Agent Notes |
| --- | --- | --- |
| workspace | `.pico/memory/notes/*.md` | `.pico/memory/agent_notes.md` |
| user | `~/.pico/memory/notes/*.md` | `~/.pico/memory/agent_notes.md` |

User Notes 由用户维护，agent 只能 list/read/search；Agent Notes 是每个 scope 唯一的 append-only 文件，
仅在当前 top-level user request 明确要求记住信息时通过 `memory_save` 追加。历史请求中的授权不能继承，delegate
不能执行 `memory_save`；否定句、引用和示例不构成授权。未授权时返回 `memory_write_not_authorized`，且不创建
Agent Notes、Tool Change、checkpoint 或其他 mutation side effect。agent 不创建 topic writer，也不改写 User Notes。

每条 Agent Note 有长度上限，文件有 soft cap。追加使用 per-scope file lock、私有 regular-file 检查和原子
replace，避免并发 read-modify-write 丢失更新。workspace/user 私有目录和 Agent Notes 使用 owner-only 权限。

## Query Snapshot 与 retrieval

一次 `Retrieval.search()` 首先让 `BlockStore` 建立 Query Snapshot：每个安全文件只读取一次，同时得到
path、mtime、size、first line、frontmatter 与 raw content。list、scoring、snippet 与 link expansion 共享该
snapshot；查询结束释放。下一次 query 重新读取，因此外部更新会立即可见，也不会形成跨查询 cache。

自动扫描有文件数、单文件字节数和总字节数上限。读取使用 no-follow/nonblocking regular-file 检查，
拒绝 symlink、hardlink、FIFO、directory、inode swap、越界路径和超限文件。一个不安全文件不会把外部
内容带入 index 或 prompt。

命中的 Memory 会作为结构化 injection source 进入当次模型请求。使用 OpenAI、Anthropic、DeepSeek 或显式远程
relay 时，召回文本会发送到解析后的 Provider endpoint；Ollama loopback 不离开本机。Agent Notes、Tool Change、
checkpoint、recovery 和其他本地私有审计 artifact 也可能保留原文或副本，因此删除当前 note 不等于清除所有历史
副本。不要在 Memory 中保存无需长期存在的 secret。

## Agent tools

- `memory_list`：列出两个 scope 的 User Notes 与 Agent Notes metadata；
- `memory_read`：按 canonical memory path 与行范围读取；
- `memory_search`：返回排序结果与 bounded snippets；
- `memory_save`：向 `workspace/agent_notes.md` 或 `user/agent_notes.md` 追加一条短记忆。

canonical path 示例：`workspace/notes/auth.md`、`workspace/agent_notes.md`、
`user/notes/preferences.md`、`user/agent_notes.md`。绝对路径、`..` 和未批准目标都会被拒绝。

CLI inspection 只提供 `memory list`、`memory show`、`memory search` 与 `memory review`。review 通过同一个
安全 BlockStore reader 读取，不直接使用 `Path.exists`/`read_text`。

Memory 参与 prompt 的方式、budget 和 runtime 边界见[架构](architecture.md)；文件安全不变量见
[安全](security.md)，fake quality benchmark 见[验证](verification.md)。
