# Pico 安全边界

Pico 的目标是把模型发起的本地操作限制在可检查、可审批、可恢复的仓库边界内。默认 Host 模式不是 OS sandbox。
v0.2.0 的显式 Sandbox 采用 [ADR-0040](adr/0040-docker-filtered-staging.md) 与
[ADR-0042](adr/0042-sealed-local-authorization.md)；任一 identity/readiness 失败都在 Provider、staging 或 target 前
fail closed，不回退 Host runner。

## Root 与文件身份

- lexical repository root 是 workspace 锚点；不跟随父仓库、兄弟 worktree或外部 `.env`。
- Project Environment 和 `pico.toml` 从可信 root dirfd no-follow 读取，拒绝 symlink、hardlink、FIFO、device、
  directory、root/parent replacement 和超限文件。
- `.pico/` 与 `~/.pico/` 私有目录使用 owner-only 权限；敏感文件在 open、read 和 atomic replace 前后复验
  device/inode/mode/nlink。
- Git/RG 只调用启动时冻结的 trusted executable，不依赖模型提供的 PATH 或 executable。

Host 和 Sandbox 文件工具共享 anchored、bounded I/O。路径从可信 root descriptor 逐级打开且禁止跟随 symlink；
只读取普通 single-link 文件。写入使用同目录 private temp、`fsync(file)`、atomic replace、`fsync(parent)`；已有文件
保留 mode，新文件为 `0644`。Patch 读取后用 SHA-256 CAS，外部改变内容或 identity 返回
`workspace_changed_during_write`。不安全 entry 不跟随、不读取、不写出 root。

当前上限包括：单文件 8 MiB、read 最多 200 行、list 最多扫描 10,000 entries/输出 200 个结果；Python search
最大深度 32、10,000 文件、单文件 8 MiB、总读取 64 MiB、200 matches。可信 RG 仍受 subprocess timeout、输出和
文件大小上限。`pico.toml` 最大 1 MiB；malformed/unsafe/oversize 时告警并回退安全默认。

## Sandbox 信任边界

正式 local target 只接受 macOS arm64、Docker Desktop、canonical Unix socket 和 package manifest 中的 exact
`linux/arm64` image。公开 `pico --sandbox run/repl` 在其他宿主返回
`sandbox_local_platform_not_released`。自定义 image/policy、remote Docker endpoint、动态 fallback、隐式 pull/build
和 workspace 注入授权都拒绝。普通 container 是受限本机隔离，不是 Docker Sandboxes、microVM 或 hostile
multi-tenant boundary。

Container 的唯一 host bind 是 filtered Execution Root。Source Root、Project State Root、Sandbox State Root、
host HOME、Docker socket 和 credentials 均不挂载；source `.git` 不复制，synthetic `.git` 不可信。container 网络
关闭，container 内 loopback/private IPC 仍允许。资源、进程、文件、输出和 timeout 均受限；容器结束后强制最终
workspace measure并清理，短命令不能绕过检查。

Sandbox 模式下 Context、RepoMap、Working Memory injection、read/write/patch/list/search 和 shell 都只面向 filtered
staging。Source Root 只用于 host 配置、审计与 Source Apply。调用期 incremental capture 仅在 fingerprint 与 blob
都可信时复用；resume、非 shell mutation、blob 缺失或异常强制全量。最终 diff 永远完整 capture。

## Staging 与 secret

Staging builder 从 source dirfd 打开文件，以固定 chunk 流式复制，同时计算 SHA-256、logical/allocated bytes 和已知
secret 匹配；发布前后复验 path identity、mode 和 digest。失败会删除 temp 与未完成 destination，不留下半成品。
普通文件最大 128 MiB，Session 总 logical/allocated 上限各 1 GiB；`.env.example/.sample/.template` 仅在不超过
1 MiB 时允许进入专用 pattern scan。

模型 API Key 只来自当前 Project Environment、进程环境中的 `PICO_DEEPSEEK_API_KEY`，或 `config set-secret`
安全输入。redaction snapshot 在 Pico
构造时冻结；模型输入、tool args/result、session、trace、report、checkpoint 和 error metadata 在持久化前脱敏。
诊断可以显示 Key 是否存在、变量名、API URL 和配置来源，但不能显示值、认证 header 或 URL secret。

自动 secret 识别只承诺固定敏感路径、host/provider/state 内容和运行前已知 secret bytes/pattern。未知、变换后或
guest-generated secret 可能进入 staging；它仍不能自动写回 source，必须经过 immutable redacted diff、规则检查和
人工 review。不得声称“所有 secret 都不会进入 staging”。

## 模型 API destination

运行时只把 `PICO_DEEPSEEK_API_KEY` 发送到同一解析结果中的显式 `PICO_API_URL`。项目 `.env` 优先于进程环境；
没有 URL 或 Key 时 fail closed，不回退旧 Pico/厂商变量，也不按域名推断 official/third-party。URL 中的 userinfo、
secret query、任意 query/fragment 和 HTTP redirect 均拒绝；除 loopback 外必须使用 HTTPS。

## Injection 与 Memory

InjectionSnapshot 由有序 exact source blocks 直接构建，source 内的空行或伪 `<pico:...>` marker 不会改变边界。
同一 user turn 的 retry/tool-followup 复用 immutable snapshot，防止 Provider payload 与审计 metadata 漂移。

模型 API Key 只从当前 Project Environment 或当前进程环境的 `PICO_DEEPSEEK_API_KEY` 读取，也可通过安全的
`config set-secret` 输入。解析器不做
shell expansion，不允许 Project Environment 覆盖 PATH/HOME、loader variables 等 execution environment。

`memory_save` 的 durable mutation 只读取当前 `TaskState.user_request`。历史授权不能继承，delegate 永远不能写；
无明确中英授权返回 `memory_write_not_authorized`，且不得创建 Agent Notes、Tool Change、checkpoint 或其他 mutation
side effect。否定句、引用和示例不构成授权。

Memory recall 命中会进入模型请求。使用远程 Provider 时，召回文本会发送到该 endpoint；Agent Notes、Tool Change、
checkpoint、recovery 和其他本地私有 artifact 也可能保留原文或副本。删除当前 note 不保证清除历史 artifact，处理
敏感长期记忆前应先评估 Provider 与本地留存范围。

## Shell、approval 与 Source Apply

Shell 先分类为 safe、approval-required 或 rejected，再验证 executable、环境 allowlist、timeout 和输出边界。
approval 后重新校验原始参数。runner 只调用一次；effect observer 比较真实 workspace 状态，不只相信工具声明路径。
pending 后的 interrupt 会终结为 interrupted；次生 finalization 错误不掩盖 primary exception。

Sandbox tool approval 只授权 Execution Root mutation。Source Apply 必须加载 immutable diff，在确认前显示 sandbox id、
exact digest、Source Root、数量/字节、变更分类和高风险摘要，再把刚显示的 digest 传入 apply。`--yes` 只跳过输入。

Apply 在 external control lock 和 source mutation lock 内，先发布 exact authority reservation，再写 journal/blobs、
source-local guard和 Session `applying`，最后才允许 source mutation。source baseline、artifact 或 identity 漂移时整次
零 source writes。create/modify/delete 只通过 source-local private quarantine 和原子 publish；原子原语、same-device、
ACL/flags/metadata 或 recovery 事实无法证明时保留 review block。

## 已知限制与发布边界

能以同一用户改写 Python 环境的攻击者可能在 installed-tree 检查前执行；sealed authorization 不是 secure boot。
需要抵御该威胁时必须使用可信不可变安装环境。Host 模式中经批准的进程仍可访问当前用户可访问的系统资源，因此
不要在不可信仓库批准复杂 shell，并保留 Git/外部备份。

当前安全原语依赖 POSIX/macOS descriptor、no-follow、file lock 和 atomic replace。Git marker、结构元数据、
config 或 index 即使通过初检，仍按可能发生校验后并发修改处理；所需安全原语不可用时 fail closed，
Windows 等价机制留待后续设计。

legacy SRT runtime 与 package data 已从 v0.2.0 删除。distributed authority reader 合同保留，但 production public
key/KMS、registry、amd64 image 与多平台证据不存在，Product Enablement 保持 `NO-GO`。恢复语义见
[恢复](recovery.md)，可重建门禁见[验证](verification.md)。
