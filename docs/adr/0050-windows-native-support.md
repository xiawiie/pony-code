# ADR-0050：Windows 原生一等平台支持

- 状态：Accepted for implementation；尚未形成发布支持声明
- 日期：2026-08-05

## 背景

Pony 当前 CLI、TUI、Provider 和 Python 打包层大体可跨平台，但安全文件、私有状态、文件锁、可信 executable、进程树和
shell policy 依赖 POSIX 原语。仅修复入口或在调用方散布 `os.name == "nt"` 会让 Windows 表面可启动、真实任务中途失败，
也会削弱现有 fail-closed 合同。

原方案用 Git Bash `sh.exe` 承载 Windows shell。这能复用 POSIX grammar，却要求额外兼容层，且用户从 Windows Terminal、
PowerShell 或 cmd 启动后，Agent 实际执行环境仍不是原生 Windows shell。它不满足“一等平台、无感切换、功能同级”的产品目标。

## 决策

### 产品合同

- 目标平台为 Windows 11 x64、原生 CPython 3.11/3.12；Pony 可从 Windows Terminal、PowerShell 和 cmd 直接启动。
- 不把 WSL、Git Bash、MSYS2 或 Cygwin 作为 runtime、shell 或支持前提。
- CLI/TUI、Provider、`.env`、permission、Tool、Session、Run、Memory、recovery 和稳定错误语义保持同一公共合同；不增加
  Windows 配置面、动态 backend registry 或弱化 fallback。
- Windows 支持以常见本地开发工作区达到功能同级为验收目标。不能提供安全等价原语的文件系统、路径 namespace 或执行环境
  必须在启动/doctor 阶段明确 fail closed，不能运行到 mutation 中途才退化。

### 平台边界

现有公共入口保持不变，只在安全文件、锁、进程和 shell policy 的所属模块做静态平台分派。POSIX 路径继续使用当前实现；
Windows 路径使用少量专用模块，不建立通用 filesystem/process framework。

Phase 0 先验证 `NtCreateFile` 的 root-handle-relative traversal、DACL/File ID、`LockFileEx`、Job Object 和
clean install。优先候选是 Windows 条件依赖 `pywin32` 承载已封装的 Win32 security/process API，并只对未覆盖且
确有必要的原语使用极窄 `ctypes`。依赖选择必须由 Windows 3.11/3.12 wheel、离线安装、错误映射和 fault-injection 证据决定；不能先修改 runtime
requirements 再补证明。

### 文件与状态安全

- 从已验证 root handle 逐层打开目标；Phase 0 必须证明 `OBJECT_ATTRIBUTES.RootDirectory` 的相对打开不会退化为字符串
  canonicalization。该能力失败即停止 Windows backend，不能以 `Path.resolve()` 代替。
- 拒绝 symlink、junction 和其他 reparse point；普通文件必须 single-link。身份使用 volume identity 与稳定 File ID，不伪造
  POSIX device/inode、uid、gid 或 mode。
- 私有目录和文件使用当前用户 SID 所有权与 protected DACL；允许集合必须显式、可复验，ACL 或 owner 漂移即拒绝。
- 原子写使用同目录私有 temp、`FlushFileBuffers`、原子 replace、重新打开和身份/DACL/content 后置复验。Windows 不宣称
  与 POSIX parent-directory `fsync` 完全等价；故障注入必须证明结果为 old-or-new，否则 writer fail closed。
- 上层只能消费命名的 platform-neutral identity/signature 字段。Windows backend 未完成前，不改变现有 durable format 或伪造
  Windows 值以塞入 POSIX 字段。

### 锁与进程

- Session、Run、Memory、trust 和 workspace mutation lock 复用同一锁入口；Windows backend 使用持有文件 handle 的
  `LockFileEx`/`UnlockFileEx`，保持 timeout、非重入、authority identity 和 lock 后复验语义。
- Windows 子进程进入带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object；必须在目标进程执行用户代码前完成关联。
  timeout、输出上限、异常和父进程退出都要终止完整进程树。
- Windows anonymous pipe 不复用 POSIX selector 路径；使用两个 bounded reader，stdout/stderr 共用现有 4 MiB 总预算，
  output-limit 与 timeout 都由同一 Job Object 收口。

### 原生 shell 与 executable trust

- argv 模式继续直接执行原生 `.exe`。需要 shell grammar 时，Windows 使用受信系统 Windows PowerShell，显式
  `-NoLogo -NoProfile -NonInteractive -Command` argv；不调用 `shell=True`，不 fallback 到 `cmd.exe`、`sh.exe` 或
  Git Bash。
- Windows command policy 必须有 PowerShell-aware scanner、敏感路径识别和 wrapper/redirect/expansion 规则；审批仍不能绕过
  deny、secret、path、executable trust、mutation lock 或 effect observation。Provider context 明确当前 OS 和
  canonical shell，避免生成 POSIX 命令后隐式转译。
- Git 使用原生 Git for Windows `git.exe`，不调用其 Bash。`git.exe` 与 `rg.exe` 必须通过文件身份、签名/ACL、父目录写权限和
  创建进程前复验；用户可写 shim 不能仅因位于 `PATH` 就受信。
- Windows 安装路径使用受保护的 machine-scope Python 与 Git for Windows；不接受 WindowsApps alias、用户态安装或用户可写
  shim。`rg` 由 WinGet 的 `BurntSushi.ripgrep.MSVC` 固定版本/manifest 获取，再由
  `scripts/windows/install_host_tools.ps1` 校验实际 executable SHA-256、类型和大小，复制许可文件与 executable 到
  `C:\Program Files\Pony Host Tools`，设置 protected DACL（Administrators/SYSTEM FullControl、Users
  ReadAndExecute）并加入 machine PATH。
- 不直接信任 WinGet stable alias 或 package root：前者实测是 reparse/symbolic link，后者向安装用户授予写权限。安装脚本的
  受控复制必须拒绝 reparse point、意外条目和哈希漂移，替换旧条目前先解除其目录项，避免沿既有 hardlink 覆盖外部文件；
  CI 可继续使用同一“包管理器校验来源后复制到受保护工具根”的 Chocolatey wrapper。

### Session 与跨平台切换

Windows 平台可用性不能靠更改当前 Session 事实来冒充。若产品要求同一 Session 随仓库跨机器/跨 OS 移动，后续 format
升级必须把 logical repository identity 与 machine-local physical binding 分开，并以显式迁移重建本机可信绑定。未完成该
迁移前，Windows 上新建和恢复本机 Session 可以进入支持范围，跨机器复制 active Session 不得静默接受。

## 分阶段门禁

1. **Phase 0：Windows capability spike**：先由 Windows 3.11/3.12 CI probe 固定系统 PowerShell 与所需 Win32/NT API
   surface，再验证 root-relative open、DACL/File ID、锁、Job、Git/rg 和 Windows wheel；symbol probe 不计作安全语义证据。
2. **Phase 1：文件安全核**：private/workspace I/O、atomic write、race/fault injection；上层零散 POSIX tuple/mode 依赖清零。
3. **Phase 2：锁与进程**：`LockFileEx`、Job Object、bounded capture、timeout/output-limit 全树终止。
4. **Phase 3：shell 与 executable trust**：PowerShell policy、native Git/rg、frozen environment、plan editor。
5. **Phase 4：Session portability（如需跨机器）**：logical identity 与 physical binding 的显式格式迁移。
6. **Phase 5：Windows CI/实机**：3.11/3.12 全量测试、build、clean install、Windows Terminal TUI 和攻击回归。

Windows classifier、README“支持 Windows”和 release support matrix 只能在 Phase 0-3 与 Phase 5 全部通过后添加；跨机器
Session 支持声明还必须等待 Phase 4。不得用大面积 `skipif Windows`、WSL 结果或 Linux/macOS 测试替代 Windows 证据。

## 后果

- 用户从 PowerShell、cmd 或 Windows Terminal 启动时获得同一 Pony 产品，不需要学习或安装 Git Bash shell。
- 工作量高于“让 CLI 能启动”，但路径、锁、进程和 shell 的安全边界不会因平台支持而下降。
- 当前分支只是在实施该决策；在 Windows 实机、clean install 和完整离线门禁通过前，现有 macOS/Linux 支持声明保持不变。
