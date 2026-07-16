# ADR-0042：sealed local authorization 与 macOS arm64 本地稳定版

- 状态：Accepted
- 日期：2026-07-15

## 背景

ADR-0041 的 distributed authority 尚未就绪，但经过验证的本机 Docker path 可以在更窄范围内交付。该例外必须
是可重算、不可缓存、不能被项目配置伪造的本地能力，并且不能被描述成跨平台 GA。

## 决策

v0.2.1 的唯一正式 Sandbox 平台是：

- macOS arm64；
- 本机 Docker Desktop；
- package manifest 中已存在且 identity 完全匹配的 exact `linux/arm64` image。

每次公开 `pico --sandbox run/repl` 都先验证宿主平台，再从可信 Pico 安装树生成 sealed local authorization。授权绑定
distribution version、installed tree digest、canonical image set、policy 和 corpus，不缓存、不联网，也不接受
workspace、环境变量或自定义 image 注入。其他平台返回稳定错误码
`sandbox_local_platform_not_released`，不得继续构造 Provider、Session staging 或 target。

`pico sandbox status` 保持只读诊断；`pico sandbox prepare` 只核验 already-present image。二者都不 pull、build、
repair、下载 Product Enablement 或写 release cache。任何 identity/readiness 失败都不回退 Host runner。

## 结果

- Host 模式仍可在所有现有 Python 支持平台使用，但它不是 OS sandbox。
- Linux、amd64、rootless Docker、registry 和 distributed Product Enablement 继续 `NO-GO`。
- local authorization 只允许使用 staging；Source Apply 仍需展示 immutable diff digest 并独立授权。
- v0.2.1 是 pre-1.0 的本地稳定版，不代表 hostile multi-tenant 或 distributed production readiness。
