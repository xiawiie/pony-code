# Pico 下一阶段工程收敛与可维护性优化设计（已取代）

- 日期：2026-07-11
- 状态：Superseded
- 原始提交：`2161811c416e9ba17cb4aa970ac8a240937bd022`
- 当前权威规范：`2026-07-11-pico-current-surface-hard-cut-design.md`

本设计中经源码验证且仍有效的内容——exact-root `.env` 可见性、`memory` push CI、
`uv.lock`/frozen sync、macOS focused CI、dead `prompt_cache` flag、tests-only `LayeredMemory`、
ToolExecutor/AgentLoop 生命周期和验证基线——已经合并到当前权威硬切规范。

两份设计的冲突已经由用户逐项决定：一份 master spec 对应五份顺序 implementation plans；
历史 tracked 文档与生成证据从当前树删除但保护所有 untracked 资料；Retrieval 使用单次查询
snapshot；发布型 metadata 延后；持久化执行限定 workspace 的一次性硬迁移；结构测试使用精确
hard-cut manifest。

本文不得作为 implementation plan 输入。完整历史文本保留在 Git 提交 `2161811` 中。
