# ADR-0048：Pony 1.0 产品与支持边界

- 状态：Accepted（未发布 1.0）
- 日期：2026-07-20

## 背景

预发布历史同时出现过 Docker Sandbox、Host 执行、静态 Provider 路由和自动协议选择。若文档、package metadata 与
当前 runtime 各自保留不同说法，用户无法判断安全边界，测试也无法形成可复现的发布结论。

## 决策

1. Pony 1.0 只在用户信任的 Source Root 上执行 Host 工具。Host 不是 OS sandbox；公开 Sandbox、Source Apply 和
   workspace restore 保持删除，旧 Sandbox artifact 只允许 bounded inspection。
2. 强制 Provider 静态决定 Transport。missing/`auto`/OpenAI family 可在真实任务前进行 bounded synthetic
   resolution；真实用户任务失败后不切换协议重放。
3. 支持 Python 3.11/3.12 的 macOS 与 Linux。安全文件访问和锁依赖 POSIX 原语；Windows 不在 1.0 支持范围，缺少原语时
   fail closed，不提供弱化安全保证的兼容路径。
4. 完整 TUI 的响应式马形 Logo、`PONY CODE` 字标、欢迎页布局和视觉语言是冻结产品资产。除非用户明确要求，维护、
   竞品交互对齐和代码精简不得修改或隐藏它们。
5. 源码版本保持 `1.0.0` 作为未发布目标。创建并推送 exact `v1.0.0` tag 前，不把源码、构建或 metadata 描述为已经发布
   或 Production/Stable。

## 结果

- package metadata 只声明 macOS/Linux，并在未发布阶段使用 Beta development classifier。
- 四个 Transport 的离线合同证明实现存在；每个真实账号、endpoint 与 model 的兼容性仍需独立 G8，不能互相外推。
- 将来增加 Windows 或 OS sandbox 必须有新的威胁模型、ADR、实现和实机证据，不能恢复旧代码作为兼容层。

## 被拒绝的方案

- 保留 `OS Independent`，再让不支持的平台运行时失败：安装 metadata 会误导用户。
- 把 synthetic resolution 称为失败 fallback：它发生在用户任务前，且不携带用户任务内容。
- 为保留历史而继续维护旧 Workflow/Plan 实施方案：ADR-0043 已保存历史，ADR-0045 与当前文档定义现行合同。
