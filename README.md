# Pony

> 面向受信代码仓库的本地 coding agent：理解上下文、受控地调用工具、保存可恢复的会话证据。

Pony 是一个 Python CLI/TUI coding agent。它在当前 Git 仓库内构造上下文，由模型提出一个动作，再由本地运行时做
schema、permission、路径、secret 与 effect 检查后执行。Session、Run、Memory、Plan 和恢复证据都保存在项目的
`.pony/` 中。

当前 `1.0.0` 是未发布源码目标；在 `v1.0.0` tag 与对应 package 真正发布前，请从源码安装。

![Pony TUI welcome screen](docs/assets/terminal/pony-tui-welcome.png)

## 一眼看懂

```mermaid
flowchart LR
    U["开发者"] --> C["pony CLI / TUI"]
    C --> R["Pony runtime"]
    R --> A["Agent loop"]
    A --> P["Provider adapter"]
    P --> M["模型 API"]
    A --> T["受控 Tools"]
    T --> W["受信 Git 仓库"]
    R --> S[".pony 状态与证据"]
```

```mermaid
mindmap
  root((Pony))
    交互
      行内 TUI
      纯文本 REPL
      one-shot run
    Agent
      Context 与 RepoMap
      Memory recall
      Plan
      Session fork / rewind
    执行
      Permission modes
      Tool validation
      Host mutation lock
      Effect observation
    协作
      Read-only delegate
      Isolated worktree agents
      Explicit merge review
    Provider
      Anthropic Messages
      OpenAI Responses
      OpenAI Chat Completions
      Ollama Chat
```

## 能做什么

| 场景 | Pony 的方式 |
| --- | --- |
| 修复或审查代码 | 从仓库读取上下文，模型提出单个 Tool/Final 动作，本地执行器复核并记录真实效果 |
| 先规划后实现 | `plan` mode 只暴露只读与 Plan 工具；退出前针对精确 Plan revision 确认 |
| 长任务继续执行 | append-only Session Tree、compaction、checkpoint、fork、rewind 与 Session 级模型切换 |
| 使用项目规范 | 显式读取受信 `.claude/skills/<name>/SKILL.md`，仅作为当前 turn 的只读上下文 |
| 并行处理任务 | 在隔离 Git worktree 中创建 child；审查后显式 `merge` 或有序 `merge-all` |
| 切换模型或 Provider | 用单一 `.env` 配置；协议/endpoint 明确绑定，真实任务失败不 fallback |

## 一次任务如何运行

```mermaid
flowchart LR
    Q["用户请求"] --> F["冻结 mode / rules / tool schemas"]
    F --> X["绑定 Session leaf 的模型请求"]
    X --> D{"模型动作"}
    D -->|"Final"| Z["写入 Run / Session 并显示结果"]
    D -->|"Tool"| V["schema + policy + permission"]
    V -->|"需要确认"| H["用户 approval"]
    H --> L["mutation lock"]
    V -->|"允许"| L
    L --> E["执行一次并观察真实 effect"]
    E --> X
    D -->|"Retry"| X
```

一个模型响应最多形成一个 Tool、Final 或 Retry Action；多 tool call 不会部分执行。Session leaf、Provider binding、
permission mode、可见工具和请求上下文都会在 turn 内冻结；并发写入、路径事实不明或持久化失败时 fail closed。

## 五分钟开始

### 1. 从源码安装

Pony 1.0 支持 Python 3.11、3.12 的 macOS 与 Linux。Windows 不在 1.0 支持范围；它缺少当前安全文件与锁模型
依赖的 POSIX 原语。

```bash
git clone https://github.com/xiawiie/pony-code.git
cd pony-code
uv sync --frozen --dev
uv run pony --version
```

若要把源码工作区安装为用户级 CLI：

```bash
uv tool install --editable .
uv tool update-shell
exec zsh
pony --version
```

### 2. 在要操作的仓库配置模型

```bash
cd /path/to/your/repository
pony init
pony config show
pony doctor
```

`pony init` 以私有权限原子写入仓库根目录 `.env`。强制 Provider 只做本地校验；`auto` 或 `openai` family 会在写入前
执行 bounded synthetic probe，因此可能产生 Provider 费用。普通 `pony doctor` 不联网；`pony doctor --check-api`
会执行最小文本、tool call 与 tool-result continuation 验证，但不写 `.env` 或 Session。

### 3. 进入交互或执行一次任务

```bash
pony
pony run "inspect the failing tests and make the smallest safe fix"
pony --permission-mode plan run "inspect the repository and produce a plan"
```

`pony` 与 `pony repl` 是同一个交互会话；`pony run` 一次执行后退出。未知首 token 不会被静默当作 prompt。
非 TTY、缺少/空白 `TERM`、`TERM=dumb` 或窄于 40 列时自动回退为纯文本 REPL；`pony run` 不显示装饰性 banner。

## 配置与 Provider 路由

`.env` 是唯一用户配置入口，只在当前 lexical repository root 读取，且项目值优先于同名进程变量：

```dotenv
PONY_PROVIDER=openai-chat
PONY_API_BASE=https://api.openai.com/v1
PONY_API_KEY=your-api-key
PONY_MODEL=gpt-5.4
```

| 变量 | 是否必需 | 说明 |
| --- | --- | --- |
| `PONY_PROVIDER` | 否 | `auto` / 缺失可解析；也可指定强制 Provider |
| `PONY_API_BASE` | 是 | 已含版本前缀的精确 API root |
| `PONY_API_KEY` | 云端是 | Ollama 可为空 |
| `PONY_MODEL` | 是 | 精确模型名；Session 可用 `/model` 临时切换 |

```mermaid
flowchart LR
    ENV["Repository .env"] --> CFG["配置解析"]
    CFG --> A["anthropic_messages"]
    CFG --> R["openai_responses"]
    CFG --> C["openai_chat_completions"]
    CFG --> O["ollama_chat"]
    A --> N["统一 Response"]
    R --> N
    C --> N
    O --> N
```

| 用户 Provider | 内部 Transport | 认证 |
| --- | --- | --- |
| `anthropic` | Anthropic Messages | `x-api-key` |
| `openai-responses` | OpenAI Responses | bearer |
| `openai-chat` | OpenAI Chat Completions | bearer |
| `ollama` | Ollama Chat | none |
| missing / `auto` / `openai` | known origin、Session binding 或 bounded synthetic resolution | 解析后固定 |

真实任务失败不 fallback：Pony 不会在任务失败后切换 Provider 或协议并重放状态。每个 Provider/模型组合的 live 结果不能证明其他组合可用；
四种 Transport 的实现有离线 wire-contract 测试，真实账号/endpoint/model 仍需分别验收。

## 交互能力

```mermaid
stateDiagram-v2
    [*] --> Auto
    Auto --> Plan: /plan
    Manual --> Plan: /plan
    AcceptEdits --> Plan: /plan
    DontAsk --> Plan: /plan
    Bypass --> Plan: /plan
    Plan --> PreviousMode: exit_plan_mode + exact approval
    Auto --> Manual: /permissions
    Manual --> AcceptEdits: /permissions
    AcceptEdits --> DontAsk: /permissions
    DontAsk --> Bypass: dangerous capability + /permissions
```

| 能力 | 用户入口 | 关键边界 |
| --- | --- | --- |
| 权限模式 | `--permission-mode`、`/permissions` | `manual`、`auto`、`acceptEdits`、`bypassPermissions`、`dontAsk`、`plan` |
| Plan | `/plan [description|open|share]` | 退出前确认精确文本与 revision；`share` 在本地 runtime 明确不可用 |
| Session | `/session`、`/tree`、`/fork`、`/rewind`、`/compact` | fork/rewind 只改变 Session branch，不恢复 workspace 文件 |
| 模型 | `/model [model]`、`--model` | 仅同 protocol 与 endpoint；含 opaque state 时拒绝切换 |
| Memory | `/memory`、`/remember`、`/memory-review` | `memory_save` 必须是当前请求的明确授权 |
| Skills | `/<skill-name> [prompt]` | 仅受信 `.claude/skills`、只读、当前 turn、不会执行脚本 |
| Follow-up | `/queue [clear]` | 最多五条内存队列；不持久化、不取消已经开始的请求 |

完整 TUI 始终保留响应式马形 Logo、`PONY CODE` 字标和欢迎页布局；它们是冻结的产品资产，除非用户明确要求，维护和重构
不得修改。

## 并行 Worktree Agent

```mermaid
flowchart LR
    P["Clean parent HEAD"] --> B["delegate_worktrees batch"]
    B --> A1["Child A\nbranch + worktree + client + Session"]
    B --> A2["Child B\nbranch + worktree + client + Session"]
    A1 --> S1["sealed revision + test evidence"]
    A2 --> S2["sealed revision + test evidence"]
    S1 --> R["review"]
    S2 --> R
    R --> M["explicit merge / merge-all"]
```

并行 child 不会自动合入 parent。`merge` 与 `merge-all` 要求 project trust、clean parent、sealed exact revision；
`merge-all` 在写 parent 前预检完整顺序，任何冲突都不会部分合并。

```bash
pony agents list
pony agents show-batch <batch-id>
pony agents merge-all <batch-id>
pony agents cleanup <agent-id> --discard
```

## 安全与边界

```mermaid
flowchart LR
    I["Tool request"] --> S["schema / path / secret"]
    S --> P["project trust + permission"]
    P --> A["approval when required"]
    A --> L["workspace mutation lock"]
    L --> X["execute once"]
    X --> O["observe real effect"]
    O --> D["durable trace / Session evidence"]
```

- Pony 只在受信 Source Root 执行 Host 工具。Host 不是 OS sandbox，也不隔离恶意命令、依赖、编译器插件或测试进程。
- 路径访问拒绝 traversal、symlink、hardlink、special file、root escape 与 identity drift；I/O 与 subprocess 输出均有上限。
- `bypassPermissions` 只跳过普通 prompt，不绕过 trust、deny rule、schema、路径、secret、可信 executable、mutation lock 或 effect observation。
- 已删除公开 Sandbox、Source Apply、workspace restore 与 `/rewind --workspace`。旧 Sandbox-bound Session 会稳定拒绝 resume，绝不静默切到 Host。
- 旧 Checkpoint/Sandbox artifact 只允许 bounded、只读检查；恢复工作区请使用 Git 或外部备份。

详细威胁模型见[安全边界](docs/security.md)，状态/恢复见[Context 与 Session](docs/context-and-sessions.md)与[恢复](docs/recovery.md)。

## 常用命令

```bash
pony status
pony doctor
pony doctor --check-api
pony sessions list
pony runs summary latest
pony checkpoints pending
pony memory search "release decision"
pony agents batches
```

## 验证与支持范围

```mermaid
flowchart LR
    G0["G0 Source"] --> G1["G1 Static"] --> G2["G2 Functional"]
    G2 --> G3["G3 Security"] --> G4["G4 Evaluation"]
    G4 --> G5["G5 Distribution"] --> G6["G6 Clean install"]
    G6 --> R["release candidate"]
    G8["G8 Provider live\nconditional / charged"] -. per provider target .-> R
```

```bash
./scripts/check.sh
```

该命令在 clean exact HEAD 上运行 lock、Ruff、全量 pytest、offline assertions、deterministic evaluation、sdist/wheel
构建和 clean-install smoke。G8 真实 Provider 验收会产生费用，必须获得明确授权；离线 contract 不会被写成 live 结果。
完整门禁和发布说明见[验证与发布](docs/verification.md)。

## 文档导航

| 想了解什么 | 文档 |
| --- | --- |
| CLI 安装、配置、命令与迁移 | [CLI 安装与更新](docs/cli-installation-and-updates.md) |
| 系统边界、目录与数据流 | [架构](docs/architecture.md) |
| 领域术语、模块所有权与不变量 | [领域模型](docs/domain-model.md) |
| 路径、secret、Host 与 permission 安全模型 | [安全](docs/security.md) |
| Context、Session、compaction、fork 与 rewind | [Context 与 Session](docs/context-and-sessions.md) |
| Memory 行为 | [Memory](docs/memory.md) |
| Legacy artifact 与恢复边界 | [恢复](docs/recovery.md) |
| exact-head 门禁、live 验收与发布 | [验证与发布](docs/verification.md) |
| 产品支持边界 | [ADR-0048](docs/adr/0048-product-and-support-boundary.md) |

Pony 使用 [MIT License](LICENSE)。
