# ADR-0042：sealed local authorization 与 macOS arm64 Sandbox

- 状态：Accepted；由 Pico 1.0 重新确认
- 日期：2026-07-15；2026-07-16 更新

## 背景

Pico 需要在不引入 registry、远程签名服务、运行时下载或缓存授权的情况下，严格绑定当前安装代码与本机 already-present
Docker image。授权不能由项目 `.env`、workspace 内容或普通 CLI flag 伪造，也不能被描述为跨平台或多租户能力。

## 决策

1.0 的唯一公开 Sandbox 平台是：

- macOS arm64；
- 本机 Docker Desktop 的受信 endpoint；
- package manifest 中已存在且 identity 完全匹配的 exact `linux/arm64` image。

每次 `pico --sandbox run/repl` 都在 Provider request、Session staging 与 target 创建前验证宿主平台，并从可信 Pico
安装树生成 sealed local authorization。授权绑定 distribution version、installed-tree digest、canonical image set、
policy 和 corpus；不缓存、不联网，不接受 workspace、环境变量或自定义 image 注入。

`pico sandbox status` 是只读诊断；`pico sandbox prepare` 只核验 already-present image。两者都不 pull、build、repair
或下载外部授权。任何 identity/readiness 失败都 fail closed，不回退 Host runner。

## 结果

- Host 模式仍可使用，但不是 OS sandbox。
- Linux、amd64、remote Docker、registry 和 hostile multi-tenant 不在 1.0 Sandbox 支持范围。
- Local authorization 只允许使用 filtered staging；Source Apply 仍需 immutable diff、exact digest 和独立用户授权。
- 1.0 产品中删除未发布的 candidate、distributed release authority、product enablement 与远程 cache/download 代码。
- 将来若增加新平台或远程发布系统，必须以新的 ADR、实现、威胁模型和实机证据独立进入产品，不能恢复已删除代码作为兼容路径。
