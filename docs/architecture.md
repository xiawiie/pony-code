# Pico 1.0 架构

本文描述 1.0 产品代码的真实结构。领域术语以 [`CONTEXT.md`](../CONTEXT.md) 为准；安全和恢复细节分别见
[安全](security.md)与[恢复](recovery.md)。

## 1. 系统全景

```mermaid
flowchart TB
    U["User / CLI"] --> CLI["pico.cli"]
    CLI --> CFG["pico.config"]
    CLI --> RT["pico.runtime.Pico"]
    CFG --> ENV["Repository-root .env"]
    RT --> CTX["context + memory + repo map"]
    CTX --> LOOP["agent.loop"]
    LOOP --> PF["providers.factory"]
    PF --> API["Anthropic / OpenAI / Ollama"]
    LOOP --> ACT["action codec"]
    ACT --> TOOLS["tools.executor"]
    TOOLS --> WRK["workspace view"]
    LOOP --> STATE["session / run / checkpoint"]
    STATE --> LOCAL[".pico private state"]
    TOOLS --> REC["recovery evidence"]
```

Pico 是一个分层的本地控制循环，不是 Provider SDK 的薄包装。Provider 只负责 wire protocol；Agent Loop 决定一次
响应能否成为 Tool、Final 或 Retry Action；工具层负责 policy、approval、effect 与恢复证据。

## 2. 产品目录

`pico/` 顶层只保留五个稳定入口文件，其余实现按领域归位：

```text
pico/
├── __init__.py        # 小型公共 Python API
├── __main__.py        # python -m pico
├── config.py          # .env、pico.toml、安全配置解析
├── runtime.py         # Pico 运行时装配和跨领域协调
├── security.py        # 共享安全原语和脱敏
├── agent/             # loop、action、message、compaction、预算、观测
├── cli/               # parser、commands、doctor、inspection、REPL
├── context/           # source、chunk、render、digest、escaping
├── memory/            # notes、recall、retrieval、repo map
├── providers/         # 三 Provider、四 Transport、probe、factory
├── recovery/          # checkpoint writer、manager、migration、policy
├── sandbox/           # Docker、identity、session、diff/apply、resources
├── state/             # session/run/checkpoint store、task state、file lock
├── tools/             # tool registry、executor、effect recorder、subprocess
└── workspace/         # root discovery、snapshot、observer
```

仓库级开发资产不进入产品 package：

| 路径 | 责任 |
| --- | --- |
| `tests/` | 产品、契约、安全、durability 与回归测试 |
| `benchmarks/evaluation/` | 离线评估与 Provider benchmark |
| `benchmarks/live_e2e/` | 显式授权的真实 Provider harness 与离线 assertions |
| `scripts/evaluation/` | 评估入口 |
| `scripts/sandbox/` | 本地镜像构建和 runtime 验证 |
| `scripts/release/` | distribution 内容和 clean-install 验证 |
| `.github/workflows/` | CI 与 tag-bound release |

## 3. 启动与配置

`pico.cli:main` 是唯一 console entry。只读命令如 `status`、`config show` 和普通 `doctor` 不构造 Agent，也不发送
网络请求。`run` / `repl` 的装配顺序为：

```mermaid
sequenceDiagram
    participant C as CLI
    participant W as WorkspaceContext
    participant E as Project .env
    participant P as Provider factory
    participant S as SessionStore
    participant A as Pico / AgentLoop

    C->>W: discover lexical repository root
    C->>E: anchored, no-follow read
    E-->>C: six generic PICO_* values
    C->>P: exact protocol/model/url/auth/capabilities
    C->>S: resume or create session binding
    C->>A: construct runtime object graph
    A-->>C: ready
```

配置解析只接受 `PICO_PROVIDER`、`PICO_MODEL`、`PICO_API_URL`、`PICO_API_KEY`、`PICO_API_VARIANT` 和
`PICO_AUTH_MODE`。项目 `.env` 高于进程环境；旧变量和厂商变量不会回退生效。

### Provider 路由

| 用户 Provider | 用户 Variant | 内部协议 | 适配器 |
| --- | --- | --- | --- |
| `anthropic` | `messages` | `anthropic_messages` | `AnthropicCompatibleModelClient` |
| `openai` | `responses` | `openai_responses` | `OpenAICompatibleModelClient` |
| `openai` | `chat_completions` | `openai_chat_completions` | `OpenAIChatCompletionsModelClient` |
| `ollama` | `chat` | `ollama_chat` | `OllamaModelClient` |

`auto` 只表示选择当前 Provider 的静态默认值，不表示运行时探测。Factory 接收已经解析完成的内部协议，不根据域名、
模型名或响应失败更换路径。每种 adapter 返回统一的 `Response`。

## 4. 一个 Turn 的控制流

```mermaid
flowchart LR
    Q["User request"] --> SNAP["Immutable InjectionSnapshot"]
    SNAP --> REQ["Model Request"]
    REQ --> RES["Provider Response"]
    RES --> DEC{"decode_action"}
    DEC -->|Final| DONE["Finalize Run"]
    DEC -->|Tool| POL["Policy + approval"]
    DEC -->|Retry| REQ
    POL --> EXE["Execute once"]
    EXE --> OBS["Observe effect + persist evidence"]
    OBS --> REQ
```

关键不变量：

- 一个 Model Attempt 最多一次 Provider HTTP request；明确可重试失败由 Agent Loop 产生新的 Model Attempt。
- 一个成功响应只允许一个 Tool、Final 或 Retry Action；多工具调用不部分执行。
- 同一 top-level turn 的 retry 与 tool follow-up 复用同一不可变注入快照。
- 工具调用先校验 policy 与当前授权，再进入 mutation lock；实际 effect 由 observer 复核。
- Session 持久化失败时不继续向 Provider 发送后续请求。

## 5. Workspace 与 Sandbox

Host 模式中 Execution Root 等于 Source Root。Sandbox 模式把二者严格分开：

```mermaid
flowchart LR
    SRC["Source Root"] -->|filtered staging| EXE["Execution Root"]
    EXE --> CTX["Context / RepoMap / Tools / Shell"]
    CTX --> DIFF["Immutable redacted diff"]
    DIFF --> REVIEW["User review + exact digest"]
    REVIEW -->|explicit apply| SRC
    PST["Project State Root"] -. sessions / runs .-> CTX
    SST["Sandbox State Root"] -. capture / journal .-> DIFF
```

Source Root、Project State Root、Sandbox State Root、host HOME 与 Docker socket 都不挂载进容器。Sandbox 的 local
authorization 每次从当前安装树与 packaged image manifest 重算；状态不一致即 fail closed。1.0 不包含远程签名、
candidate、product enablement、registry pull 或运行时下载链路。

## 6. Context、Memory 与状态

`ContextManager` 统一管理 system、tools、Canonical Messages、Context Sources、Memory recall 和 token budget。
历史只通过 compaction 从 active request 退出，append-only Session Tree 中的旧 entry 不删除。

```mermaid
flowchart TB
    SM["System + tool schemas"] --> B["Token budget"]
    CM["Canonical Messages"] --> B
    CS["Context sources"] --> B
    MM["Memory snapshot"] --> B
    B --> MR["Model Request"]
    MR --> SS["Session Tree"]
    MR --> RS["Run / trace"]
    MR --> CP["Checkpoint / recovery"]
```

Session 的 Model Binding 固化 `protocol_family`、`model` 和 `endpoint_hash`。恢复时任一字段变化都会返回
`model_session_mismatch`，避免跨 Provider 或跨 endpoint 重放 opaque provider state。

Memory 分为用户维护的 User Notes 和 agent 追加的 Agent Notes。`memory_save` 只接受当前用户请求中的明确授权；
历史授权不继承，delegate 不能写。被召回的 Memory 会进入模型请求，因此远程 Provider 能看到相关文本。

## 7. 安全与失败语义

Pico 的安全设计采用可组合的不变量：anchored/no-follow 文件访问、bounded I/O、原子写入、CAS、可信 executable、
secret snapshot、结构化脱敏、稳定错误码和显式批准。外部输入无法确认时默认拒绝，不通过猜测继续运行。

Host 模式依然可以执行本地命令和修改仓库，不能被描述为隔离执行。Sandbox 是本机 Docker 边界，不是 hostile
multi-tenant 或 microVM 安全边界。

## 8. 打包与发布边界

wheel/sdist 只包含：

- `pico/**` Python 文件；
- `pico/sandbox/resources/**` JSON；
- package metadata、README、MIT License。

运行时依赖为零。distribution verifier 将 Git tracked 产品文件与 archive 精确比对，并在新建虚拟环境中安装 wheel、
检查入口、版本、资源、离线 Sandbox 状态和 doctor。Tag 发布工作流要求 `v<pyproject version>` 精确匹配，通过全部
离线门禁后才调用 PyPI Trusted Publishing 与 GitHub Release。
