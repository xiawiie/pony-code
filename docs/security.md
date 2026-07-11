# Pico 安全边界

Pico 的安全目标是把模型发起的本地操作限制在可检查、可审批、可恢复的仓库边界内。Pico 不是 OS
sandbox；经批准的复杂 shell 仍具有当前用户进程的操作系统权限。

## Trust boundary

- lexical repository root 是 workspace 路径锚点；不跟随父仓库、兄弟 worktree 或外部 `.env`。
- Project Environment 只读取 root 下的 `.env`，拒绝 symlink、hardlink、FIFO、directory 和越界替换。
- `.pico/` 与 `~/.pico/` 私有目录使用 owner-only 权限；文件身份在打开、读取和原子替换时复验。
- Git/RG 只从启动时冻结的 trusted executable 集合调用，不依赖模型提供的可执行路径。

## 路径与文件不变量

所有 workspace-relative path 都要规范化并保持在 trusted root。敏感读取使用 no-follow、regular-file、
link-count、device/inode 与 bounded-size 检查；写入使用同目录临时文件、fsync 和原子 replace。文件锁
保护 session、checkpoint、Project Environment 与 Agent Notes 的 read-modify-write。

外部输入验证必须 fail closed。无法证明安全的路径、Git metadata、shell result 或 artifact 会被拒绝，
而不是回退到普通 `Path.read_text`、不受限 subprocess 或非原子写入。

## Secret 边界

Provider key 只来自当前 Project Environment、当前进程环境或安全的 `config set-secret` 输入。解析器不做
shell expansion，不允许 Project Environment 覆盖 PATH/HOME、loader variables 等 execution environment。

redaction snapshot 在 Pico 构造时冻结。模型输入、tool args/result、session、trace、report、checkpoint 和
error metadata 在持久化前脱敏。诊断可以报告 credential 是否存在及来源变量名，但不能打印值。

## Shell 与 approval

shell 请求先分类为 safe、approval-required 或 rejected。safe argv 仍经过 executable trust、环境 allowlist、
timeout 和输出边界；hardened Git 在执行前验证 repo metadata。complex shell 只有 approval policy 允许且用户
确认未变更的原始参数后执行，approval 后会再次校验。

approval 在 mutation lock 之前发生。runner 只调用一次；effect observer 比较实际 workspace 状态，不能只
相信工具声明路径。成功、失败和 partial success 都生成相应 Tool Change evidence。pending 后的 interrupt
会 best-effort 标记 interrupted；次生 finalization 错误不得掩盖 primary exception。

## 非 sandbox 限制

Pico 的 policy、approval、锁、记录和恢复不会阻止已获授权进程访问当前用户本来可访问的系统资源。
不要在不可信仓库中批准复杂 shell；运行前审查命令与路径，使用最小权限凭证，并保留 Git/外部备份。
恢复语义见[恢复](recovery.md)，可复现安全门禁见[验证](verification.md)。
