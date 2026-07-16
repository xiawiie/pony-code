# Pico 安全边界

Pico 的安全目标是把模型发起的本地操作限制在可检查、可审批、可恢复的仓库边界内。默认运行不是 OS
sandbox。ADR-0040接受Docker + filtered staging架构，ADR-0042接受sealed local authorization驱动的本机MVP；
当前显式Sandbox任一identity/readiness失败仍在Provider/target前fail closed，任何失败都不回退Host模式。

## Trust boundary

- lexical repository root 是 workspace 路径锚点；不跟随父仓库、兄弟 worktree 或外部 `.env`。
- Project Environment 只读取 root 下的 `.env`，拒绝 symlink、hardlink、FIFO、directory 和越界替换。
- `.pico/` 与 `~/.pico/` 私有目录使用 owner-only 权限；文件身份在打开、读取和原子替换时复验。
- Git/RG 只从启动时冻结的 trusted executable 集合调用，不依赖模型提供的可执行路径。

## Sandbox 信任边界

accepted target 仅支持本地 macOS Docker Desktop 和本地 Linux rootless Docker。resolved Docker CLI、
canonical Unix socket、host-selected OCI record、canonical image-set v2、frozen policy、corpus与Sandbox
Session/root identity必须逐次验证；remote/rootful Linux daemon、自定义image/policy和动态fallback均拒绝。
D1 Feasibility Approval只授权实现，从不是runtime credential；本机runtime要求exact local authorization，
distributed release runtime要求Candidate或Product授权。
普通container是受限本地隔离，不是Docker Sandboxes、microVM或hostile multi-tenant boundary。

Container 的唯一 host bind 是 filtered Execution Root。Source Root、Project State Root、Sandbox State Root
parent、host HOME、Docker socket 和 credentials 均不得挂载；source `.git` 不复制，synthetic `.git` 不可信。
container 外 DNS/网络/host localhost 全禁，container 内 loopback/private IPC 允许。运行时不隐式 pull；
`status/list/inspect/diff/prune --dry-run` 不创建 root/lock、不 reconcile、不启动 container且不写 record；严格
read-only artifact reader拒绝错误mode但不得用`chmod`修复，因此成功读取也不能改变mtime/ctime。

release authority固定使用RSA-PSS-SHA256（3072-bit、e=65537、32-byte salt）、canonical ASCII JSON与domain
separation；Product Enablement只从禁用proxy的stable GitHub Releases HTTPS channel读取，redirect限制在allowlist，
总量上限256 KiB。正式cache是owner-only、no-follow且防rollback的
`~/.pico/releases/docker-sandbox/product-enablement.json`。runtime本地重算installed-distribution、image-set、
核对内置policy constant，并把image-set内的packaged corpus claim与签名provenance对齐；corpus本身不能在
runtime从mandatory check IDs重算。wheel/sdist/commit/expected manifest/aggregate与corpus只能作为controller
签名前核验并由签名认证的provenance claims，不能从普通安装目录反推原wheel SHA或mandatory corpus。

installed-distribution digest是可信Pico进程内的post-import一致性检查，不是secure boot。生成的`__pycache__`
bytecode、installer生成的console wrapper、`.pth`处理和可选安装provenance文件不在该digest内；能以同一用户改写
Python环境的攻击者可在检查前执行，已属于ADR-0040定义的host/Pico TCB。需要抵御该威胁时必须使用可信不可变
安装环境，不能依赖Product Enablement补足。

本机local authorization每次绑定当前package tree和packaged manifest，不缓存、不从环境或workspace注入。
四平台release smoke的candidate例外只能同时注入`PICO_SANDBOX_CANDIDATE_ATTESTATION`与
`PICO_SANDBOX_CANDIDATE_NONCE`。candidate不能下载、不能写正式cache、不能成为Product Enablement，其artifact
必须保持`product_enablement=false`。wheel内不可变production key map当前为空且没有production KMS，因此两条
distributed release路径都默认fail closed。

SRT mirror、managed Node、offline bundle 与原 adapters 已由 ADR-0040 supersede，生产入口不可达，不再构成
产品安全面。legacy代码/package data只保留到Docker registry production vertical通过后的删除门禁。

## 路径与文件不变量

所有 path 都要通过当前 Workspace View 规范化并保持在对应 trusted root。Host 模式的 Execution Root 等于
Source Root；Sandbox 模式的 Execution Root 是 filtered staging。敏感读取使用 no-follow、regular-file、
link-count、device/inode、mount-boundary 与 bounded-size 检查；写入使用同目录临时文件、fsync 和原子
replace。文件锁保护 session、checkpoint、Project Environment 与 Agent Notes 的 read-modify-write。

外部输入验证必须 fail closed。无法证明安全的路径、Git metadata、shell result 或 artifact 会被拒绝，
而不是回退到普通 `Path.read_text`、不受限 subprocess 或非原子写入。

## Secret 边界

模型 API Key 只从当前 Project Environment 或当前进程环境的 `PICO_DEEPSEEK_API_KEY` 读取，也可通过安全的
`config set-secret` 输入。解析器不做
shell expansion，不允许 Project Environment 覆盖 PATH/HOME、loader variables 等 execution environment。

redaction snapshot 在 Pico 构造时冻结。模型输入、tool args/result、session、trace、report、checkpoint 和
error metadata 在持久化前脱敏。诊断可以报告 credential 是否存在及来源变量名，但不能打印值。

Sandbox 的自动 secret 识别只承诺固定敏感路径、host/provider/state 内容和运行前已知 secret bytes/pattern。
未知、变换后或 guest-generated secret 无法保证被识别，可能进入 staging；它不能据此自动写回 source，仍需
immutable redacted diff、已知规则和人工 review。文档不得声称“所有 secret 都不会进入 staging”。

## Shell 与 approval

shell 请求先分类为 safe、approval-required 或 rejected。safe argv 仍经过 executable trust、环境 allowlist、
timeout 和输出边界；hardened Git 在执行前验证 repo metadata。complex shell 只有 approval policy 允许且用户
确认未变更的原始参数后执行，approval 后会再次校验。

approval 在 mutation lock 之前发生。runner 只调用一次；effect observer 比较实际 workspace 状态，不能只
相信工具声明路径。成功、失败和 partial success 都生成相应 Tool Change evidence。pending 后的 interrupt
会 best-effort 标记 interrupted；次生 finalization 错误不得掩盖 primary exception。

Sandbox tool approval 只授权 Execution Root mutation，不授权 Source Root。Source Apply Transaction 必须再次
展示同一 immutable diff digest并取得独立显式授权；CAS、policy或事实不明时整次0 source writes。Source Apply
以journal before inode执行双重CAS；create/modify/delete只通过source-local private quarantine与原子
no-replace/exchange发布，绝不在用户可写source parent中check后unlink或rmdir名称。原子原语、same-device、
private directory identity、ACL/flags或metadata枚举不可证明时fail closed并保留durable review guard。

Source Apply在external control lock和source mutation lock内，必须先发布exact external authority reservation，再写
journal/blobs、source-local guard与Session `applying`，最后才允许source mutation。authority完整绑定source、
Sandbox/state root、control-directory dev/inode、journal和diff；清理只能在已验证control-directory fd内重读完整
expected record并CAS unlink。reservation-only冲突仅在journal/guard不存在且Session仍`not_started`时可清除并记录
`apply_conflicted`；其余不一致全部进入review。source root被替换时只允许显式`pico --cwd <lexical-source>
sandbox reconcile --yes`按authority O(1)定位证据，不扫描或猜测state root。

## Host 模式限制

Pico 的 policy、approval、锁、记录和恢复不会阻止已获授权进程访问当前用户本来可访问的系统资源。
不要在不可信仓库中批准复杂 shell；运行前审查命令与路径，使用最小权限凭证，并保留 Git/外部备份。
恢复语义见[恢复](recovery.md)，可复现安全门禁见[验证](verification.md)。

当前实现依赖 POSIX/macOS 的 descriptor、no-follow、file lock 与原子 replace。Git marker、结构元数据、
config 或 index 即使通过初检，也仍按可能发生校验后并发修改处理；Host 模式不是 OS sandbox。accepted
Sandbox target 的active Execution Root可变；host持久化的Staging Baseline、final manifest和reviewed diff
必须immutable，任一identity变化只能discard。所需安全原语不可用时 fail closed，Windows 等价机制留待后续设计。
当前image-set只有无registry reference的`linux/arm64`记录，没有`linux/amd64`记录、production key/KMS或四平台
release evidence；只有能精确验证该record与本机already-present image的主机属于当前MVP范围。
