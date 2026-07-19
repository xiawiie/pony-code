# ADR-0040：Docker + filtered staging Sandbox

- 状态：Superseded（2026-07-19；公开 Sandbox/Source Apply 已删除）
- 日期：2026-07-15

## 背景

> 历史记录：本 ADR 不再描述当前 runtime。旧实现暂留给 legacy binding/inspection，后续由独立删除波次清理。

Host 模式中的工具和获批 shell 继承当前用户权限，无法提供操作系统级隔离。Pony 需要一个本机执行边界，同时保留
可审查、可恢复、显式写回的工作流，并且不能让模型或容器直接接触 Source Root、凭证和 Pony 私有状态。

## 决策

Sandbox 的唯一 production owner 是 Docker + filtered staging：

1. Source Root、Execution Root、Project State Root、Sandbox State Root 和 Provider 是不同边界。
2. Session 启动时用 anchored、bounded、流式读取构建 filtered staging；Source Root 不挂载进容器。
3. Sandbox 模式下所有模型可见文件工具、RepoMap、Context 和 shell 都只面向 Execution Root。逻辑路径统一显示为
   `/workspace`，不得回退 Host Source Root。
4. 每次 shell 使用 exact managed image 的短生命周期容器。唯一 host bind 是 Execution Root；网络关闭，Docker
   socket、HOME、Source Root 和状态目录均不挂载，并执行进程、CPU、内存、文件和超时上限。
5. 调用期可以用进程内 incremental capture 减少重复 hash；异常、resume 或非 shell mutation 会使 cache 失效。
   Session 最终 diff 无条件执行完整 capture，不复用调用期 cache。
6. 最终变更先形成 immutable redacted diff。Source Apply 是结束后的独立事务，绑定用户看到的 exact diff digest，
   再执行 source baseline CAS、journal、guard、原子发布和恢复收敛。
7. `status`、`prepare` 和只读 inspection 不 pull、build、repair、下载、隐式 reconcile 或修改 Sandbox state。
8. 无法确认路径身份、文件类型、capture、容器清理或恢复状态时 fail closed。

## 结果

- Sandbox 授权只允许修改 staging，不等同于 Source Apply 授权。
- 自动 secret 过滤只能覆盖固定敏感路径、已知 secret 和已冻结规则；未知、变换后或 guest-generated secret 仍需
  immutable diff 和人工 review。
- 普通 Docker container 是受限本机边界，不是 microVM，也不是 hostile multi-tenant 安全边界。
- 旧 SRT runtime、platform adapters、toolchain package data 和 offline bundle 路径从 v0.2.0 runtime 与 wheel 删除，
  不保留兼容 alias。

## 被拒绝的方案

- 直接把 Source Root 挂载进容器：无法保证 Apply 前 source 不变。
- 自动把 staging 合并回 source：丢失独立授权和 CAS 边界。
- 多 backend/插件抽象：本地稳定版没有第二个已验证 owner，只会扩大维护和安全表面。
