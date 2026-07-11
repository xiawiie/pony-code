# Pico 当前形态硬切与仓库收敛设计

- 日期：2026-07-11
- 状态：源码核验、方案比较和冲突决策均已完成；设计已批准，待用户书面复核；尚未进入 implementation planning
- 基线分支：`memory`
- 源码基线：`5f359bd18fb3a59968167bfe0196352d41a23a01`
- 本轮文档复核基线：`dfbb9b8`
- 项目阶段：`0.1.0`，未发布，无已知外部兼容承诺
- 实施性质：有边界的直接破坏；不提供 deprecated alias、warning 期或兼容 shim
- 实施组织：本文是唯一 master spec；后续拆成五份顺序生成、顺序执行、独立验证和独立回滚的 implementation plans
- 权威关系：本文取代 `2026-07-11-pico-next-optimization-design.md` 及此前同主题设计；旧设计和本文最终都在 Plan 5 从当前树删除，只保留于 Git 历史

## 1. 执行结论

Pico 已具备完整的 Action Kernel、Canonical Messages、安全执行、恢复、Memory、benchmark
和 CLI 能力。当前主要问题不是功能缺失，而是多个历史阶段同时留在运行面：

- `pico` 与 `pico-cli` 双 console 入口、裸 prompt 与显式 `run` 并存；
- Runtime 依靠 `hasattr` 识别 Provider 代际，structured 与 text transport 边界不清；
- Provider 配置在 runtime 与 diagnostics 中各自解析，API key 还存在跨 Provider fallback；
- Session、Recovery、embedded checkpoint 和 benchmark 混用多个版本命名；
- Memory 同时保留单文件、per-topic 和两套相反方向的 migration；
- 手写 TOML parser 与 `tomllib` 并存，单次 Pico 构造重复读取同一文件；
- 历史 specs、plans、SDD、review pack 和 benchmark results 长期占据活动仓库；
- 复杂度优化没有明确边界，容易误伤已经验证过的安全与恢复行为。

因此采用“当前形态硬切”，而不是继续兼容：

1. 先关闭已复现的 Memory 文件边界漏洞、丢写风险和基础复现缺口；
2. 再一次性切换 Python import、CLI、Provider、配置和 TOML 当前接口；
3. 然后事务化迁移当前本地 Pico 数据，切换到严格单一格式并删除迁移器；
4. 只收敛两个核心协调器，不进行全仓复杂度重构；
5. 最后完成构建、macOS、八份当前文档、历史资产删除和最终验证。

硬切的安全边界不允许简化：外部输入验证、secret redaction、路径锚定、no-follow、
hardlink/FIFO 拒绝、原子写、锁、恢复 terminalization 和 primary exception 顺序必须保留。
Git 历史是唯一历史档案；最终仓库只表达当前产品。

## 2. 已核验基线

### 2.1 代码与验证

- 生产 Python：23,624 行、78 个文件。
- Tests：31,388 行、117 个文件。
- 运行时第三方依赖：0。
- Ruff：通过。
- Pytest：`1997 passed, 6 skipped`。
- Offline live harness：`60 passed`。
- Wheel 与 sdist：可构建，但当前 sdist 包含完整 tests tree。
- Memory fake benchmark：8/8。
- macOS 全量测试：通过，但 FIFO 参数化测试因后台线程中 `fork()` 产生两条弃用警告。

已授权并完成的基线真实 DeepSeek `qwen3.7-max` E2E：

- 43/43 assertions；
- 8 个 native actions；
- 10/15 Provider calls；
- 13,842 input tokens；
- 1,330 output tokens；
- 5,248 cache-read tokens；
- 44.253 秒；
- key、payload、active artifact、private mode、fixture restoration、session、terminal artifact、
  call cap 和 token cap 均通过；
- 独立 review：C0 / I0 / M0；
- live JSON 未提交。

这些数字是硬切前基线，不得复制为最终结果。最终 HEAD 必须重新运行并单独记录。

### 2.2 复杂度

`uv run ruff check pico --select C901 --output-format json` 当前返回 68 个 violation，不是此前
估算的 71 个。最高风险包括：

- `ToolExecutor.execute`：51；
- `tools.validate_tool`：50；
- `RecoveryManager._mutate_workspace_bytes_if_unchanged`：35；
- `RecoveryManager.preview_restore`：32；
- `RecoveryPolicy._scan_shell_syntax`：27；
- `AgentLoop.run`：24。

本次只优化 `ToolExecutor.execute` 和 `AgentLoop.run`。其余复杂函数包含大量已验证的安全或
恢复分支，不因数字高而机械拆分。

### 2.3 当前本地 Pico 数据

当前 workspace 的 `.pico` 有 46 个 regular files：

| 区域 | 文件数 | 组成 |
| --- | ---: | --- |
| `sessions` | 5 | 4 个 session JSON + 1 个 lock |
| `runs` | 36 | task state、report、trace 等审计 artifact |
| `checkpoints` | 5 | 2 个 checkpoint JSON + 2 个 tool-change JSON + 1 个 lock |
| `memory` | 0 | 空 |

- 45 个文件 mode 为 `0600`；
- `.pico/checkpoints/.checkpoint_store.lock` 为 `0644`；
- 46 个文件的 link count 都是 1；
- `~/.pico/memory` 当前为空。

四个 session 中，三个是 `schema_version = 2` 且含 `history`，一个是
`schema_version = 3`；全部在顶层及 embedded runtime identity 中留下
`feature_flags.prompt_cache`。共有 30 个 embedded task checkpoints，均使用
`phase1-v1` 代际字段。

四个 Recovery JSON 均使用字符串 `schema_version`；两个 checkpoint records 和两个
tool-change records 缺少当前 constructor 已新增的 additive fields。当前 file entries 都为空。

36 个 run artifacts 是运行审计输出，没有读时格式分派需求，保持不版本化且迁移时字节不变。

### 2.4 已确认的真实缺口

1. `memory review` 使用 `Path.exists/read_text`，会跟随 symlink；已用 canary 复现读取工作区外内容。
2. 两套 Memory migration 都直接操作文件，而且方向相反；当前数据又没有需要它们迁移的内容。
3. `append_agent_note` 是无锁 read-modify-write，并发调用可丢失更新。
4. Retrieval 一次 `search()` 最多四次 `store.list()` 并重复读取；`stat_all()` 自身也调用 `list()`。
5. `config.py` 同时有手写 parser 和 `tomllib`；malformed TOML 还回退到手写部分解析。
6. 单次 `Pico` 构造约九次读取/解析同一个 `pico.toml`。
7. Runtime 先读取 `.env` 并写入全局 `os.environ`，再使用一套变量表；diagnostics 使用另一套。
8. OpenAI 与 Anthropic API key 会互相 fallback，可能把配置错误表现为 HTTP 401。
9. 当前项目 `.env` 使用待删除名称 `PICO_RIGHT_CODES_API_KEY`；值从未写入设计或日志。
10. `pico.memory.__init__`、Provider/evaluation 模块和 CLI 模块包含 tests-only 重导出 facade。
11. OpenAI/Ollama streaming 和 `supports_native_tools` 没有生产消费者。
12. `uv.lock` 被忽略；本机 uv 为 0.11.19，CI 固定为 0.11.26。
13. CI 只覆盖 Ubuntu，push 只覆盖 `main`，没有 build/clean-install smoke。
14. `docs/*` 默认被忽略；若不修改 `.gitignore`，新的当前文档不会自然进入版本控制。

## 3. 目标、边界与成功不变量

### 3.1 目标

最终实现必须同时满足：

1. 唯一 console command 是 `pico`，运行方式只有 `pico run`、`pico repl` 和
   `python -m pico`。
2. AgentLoop 只调用一个结构化 `complete(...)` 接口；text-only Provider 只暴露 transport 方法。
3. Runtime、diagnostics、benchmarks 和 live E2E 共用同一 Provider 配置解析真源。
4. 顶层 Python API 只有七个明确导出；所有 package facade 和兼容重导出删除。
5. Session、Recovery checkpoint、tool change 和独立 benchmark family 只识别当前格式。
6. 本地 8 个结构化 JSON 经一次事务迁移后切换，其他 38 个 `.pico` 文件字节不变。
7. Memory 只有 User Notes 和单一 append-only Agent Notes；无 migration 命令、per-topic writer
   或 tests-only state facade。
8. 一次 retrieval query 每个文件最多读取一次，不建立跨查询 cache。
9. TOML 只用 stdlib `tomllib`，每个 Pico 实例只解析一次。
10. `ToolExecutor.execute` 与 `AgentLoop.run` 的 C901 均不超过 10，安全语义不变。
11. 最终面向维护者的 tracked 文档面精确为用户批准的八份文件；测试输入 Markdown 不冒充文档。
12. Build、clean-install、Ubuntu、macOS、offline harness 和经新授权的单一真实 Provider E2E
    形成可重建证据。

### 3.2 非目标

- 公开发布或对外分发；
- 新 Provider、Provider registry、gateway、plugin framework 或抽象 Protocol；
- OpenAI/Ollama native tools；
- 数据库、向量索引、文件 watcher 或跨查询 retrieval cache；
- 通用 migration framework、通用 config framework 或 schema library；
- Windows 支持、OS sandbox 重设计、并行工具、多-agent runtime、UI 或 TUI；
- 全仓复杂度清零；
- 自动读取父目录、兄弟 worktree 或主 checkout 的 `.env`；
- authors、project URLs、classifiers、License 等发布元数据；
- docs archive 或新的 ADR 层；
- 未经新的明确授权执行真实 API 请求。

### 3.3 设计约束

- 删除优先于兼容；复用当前安全 primitives，不能写第三套文件安全 helper。
- 不增加只有一个实现的 interface、factory、registry 或 request dataclass。
- 各计划只改直接相关文件；不顺手重构相邻代码。
- 每次破坏性切换必须在一个计划内保持调用者与实现原子一致。
- 每个中间计划必须可独立验证和回滚；不能把未完成兼容态推给下一计划。
- 未跟踪文件属于用户，任何计划都不得移动、删除、stage 或以“结构不干净”为由处理它们。

## 4. 已批准的关键决策

| 主题 | 最终决策 | 被否决方案 |
| --- | --- | --- |
| Provider | A+：一个 runtime-facing `complete`；OpenAI/Ollama 通过显式 Text Protocol Adapter | `hasattr` 自动包装、Protocol/registry、全部 Provider 假装 native structured |
| API key | `PICO_API_KEY` 是 OpenAI/Anthropic/DeepSeek 的共享 fallback | `PICO_RIGHT_CODES_API_KEY`、跨 Provider key fallback、环境全局注入 |
| Memory migration | 立即删除两套旧 migration；当前 memory 为空，不造不存在的数据转换器 | 先加固再删除、单文件转 per-topic、per-topic 转单文件 |
| Record version | 只给可独立解析和兼容判定的顶层 family 版本 | 每个嵌套对象都带版本、全局 `BENCHMARK_FORMAT_VERSION` |
| 本地数据迁移 | 8 个 JSON 事务转换，38 个文件 verify-only | 扫描其他 repo、就地无备份改写、通用迁移平台 |
| TOML | 单一 `tomllib` loader，Pico 构造时解析一次 | 手写 fallback、watcher、config class/schema dependency |
| Imports | 直接硬切到真实模块，package `__init__` 只作 marker | deprecated alias、facade 保留期 |
| 复杂度 | 只收敛两个协调器，并设置精确 ratchet | 全仓 C901 清零、为拆分引入状态机或 event bus |
| Cache | 保留 Anthropic/DeepSeek 真实 cache；OpenAI text transport 删除无调用请求参数，保留 cached-token usage | 删除所有 cache 或保留 tests-only capability 假象 |
| 文档 | 最终只保留八份当前文档，无 ADR | docs archive、两个新 ADR、保留历史 superpowers 树 |

## 5. 当前领域语言与总体边界

`CONTEXT.md` 在实现切换时同步为以下当前术语：

- **Pico CLI**：唯一 console command `pico`。
- **Model Request**：system、tools、messages、token budget 和 cache breakpoints。
- **Model Response**：Provider-neutral `Response`。
- **Action**：`decode_action` 产生的 Tool、Final 或 Retry 决策。
- **Canonical Messages**：Session 中唯一 transcript。
- **Text Protocol Adapter**：将 structured request 显式转换为 text transport prompt 的当前能力。
- **Project Environment**：当前 lexical repo root 下唯一允许读取的 `.env`。
- **Format Version**：可独立解析记录内部的编码版本。
- **User Notes**：用户维护、agent 只读的 Markdown。
- **Agent Notes**：每个 scope 唯一 append-only `agent_notes.md`。
- **Query Snapshot**：一次查询内共享的 path、metadata、frontmatter 和 raw content；查询结束释放。
- **Recovery Record**：顶层 checkpoint 或 tool-change 持久记录。

生产模块和类型按职责命名。`new`、`old`、`legacy`、`current`、`vN` 或 `phaseN` 不用于区分
当前实现代际；真实协议版本、Provider URL、用户输入内容和 Git 历史不受此命名规则影响。

## 6. CLI 与 Python import surface

### 6.1 CLI

`pyproject.toml` 最终只包含：

```toml
[project.scripts]
pico = "pico.cli:main"
```

删除 `pico-cli` entry、usage、help、README、tests、scripts 和 benchmark 引用。macOS 系统
`/usr/bin/pico` 冲突通过环境安装验证解决：激活环境后 `command -v pico` 必须解析到该环境，
不为此保留第二入口。

删除 bare prompt dispatch。最终行为：

- `pico`：显示 root help；
- `pico run <prompt...>`：一次性执行；
- `pico repl`：交互执行；
- `python -m pico`：Python 标准模块入口，行为与 `pico` 相同；
- inspection/recovery 命令保留现有显式 namespace；
- Memory 只保留 `list`、`show`、`search`、`review`，没有 `migrate`。

### 6.2 顶层 API

`pico.__init__` 最终只导出：

```python
Pico
SessionStore
WorkspaceContext
main
build_agent
build_arg_parser
build_welcome
```

`SessionStore` 从 `pico.session_store` 导入，不继续通过 `pico.runtime` 形成额外路径。
`FakeModelClient` 从 `pico.providers.fake` 导入。Provider、evaluation 和 memory package
`__init__.py` 只作为 package marker，不重导出 class、constant 或 helper。

删除以下 facade：

- `pico/providers/clients.py`；
- `pico/evaluation/metrics.py`；
- `pico/evaluation/metrics_experiments.py`；
- `pico/evaluation/evaluator.py`；
- `pico.memory.__init__` 中的 `VERSION = 2` 和全部重导出；
- `pico.cli_commands` 中只为 tests 暴露的 `# noqa: F401` handler reexports；
- `pico.cli` 中只为 tests 暴露的 `HELP_DETAILS` reexport。

生产代码、tests、benchmarks 和 scripts 统一从真实模块导入。删除只验证 facade 存在的 tests，
保留并改写真实行为合同测试。不提供 alias、warning 或 shim。

Evaluation 导入目标固定为：

| 职责 | 实际模块 |
| --- | --- |
| fixed benchmark 与 `BenchmarkEvaluator` | `pico.evaluation.fixed_benchmark` |
| benchmark validation | `pico.evaluation.benchmark_schema` |
| context/memory/recovery ablation | `pico.evaluation.experiments_recovery` |
| synthetic experiments | `pico.evaluation.experiments_synthetic` |
| real experiments | `pico.evaluation.experiments_real` |
| provider experiments | `pico.evaluation.provider_benchmark` |
| aggregation/reporting | `pico.evaluation.metrics_reports` |
| shared math/time | `pico.evaluation.metrics_common` |

## 7. Provider 当前合同

### 7.1 Runtime-facing 请求

AgentLoop 只调用：

```python
client.complete(
    system=system,
    tools=tools,
    messages=messages,
    max_tokens=max_tokens,
    cache_breakpoints=cache_breakpoints,
)
```

返回值始终是 `pico.providers.response.Response`。该接口不新增 Protocol class、request
dataclass、registry 或 factory。

实现边界：

| Provider | 最终实现 |
| --- | --- |
| Anthropic / DeepSeek | `AnthropicCompatibleModelClient.complete` 直接处理 structured request 和 native tools |
| Fake | `pico.providers.fake.FakeModelClient.complete` 直接返回 structured `Response` |
| OpenAI | `OpenAICompatibleModelClient.complete_text(prompt, max_tokens)`，由 `TextProtocolAdapter` 转换 |
| Ollama | `OllamaModelClient.complete_text(prompt, max_tokens)`，由 `TextProtocolAdapter` 转换 |

`_build_model_client` 按 Provider 显式装配；OpenAI/Ollama 明确包一层 `TextProtocolAdapter`，
Anthropic/DeepSeek/Fake 直接返回。删除 Runtime 的 `hasattr(..., "complete_v2")` 自动包装。

`FallbackAdapter` 改名 `TextProtocolAdapter`。它只做四件事：清理 Pico meta、扁平化
system/tools/messages、调用 `complete_text`、把文本与 usage 包装为 `Response`。它接受
`cache_breakpoints` 以满足 runtime-facing 调用，但明确忽略且不映射到 text transport。

删除：

- `complete_v2`；
- Anthropic 的旧 text `complete(prompt, ...)`；
- 所有 Provider 的 `stream_complete`；
- `supports_native_tools`；
- runtime prompt-string 自动兼容路径；
- scripted tests 中只模拟旧代际接口的分支。

OpenAI/Ollama native tools 延后到真实需求出现且有独立 E2E 时再设计；当前 Adapter 已提供
清楚的替换边界，不需要为未来预建接口。

### 7.2 Cache 语义

Anthropic/DeepSeek 保留真实工作的：

- message cache breakpoints；
- Anthropic `cache_control` payload；
- cache creation/read token usage；
- `supports_prompt_cache` 能力判断；
- Provider payload 和 usage 合同测试。

OpenAI 当前只位于 text transport，`TextProtocolAdapter` 从未传递
`prompt_cache_key/prompt_cache_retention`。因此删除 OpenAI 请求中的这两个未使用参数、
`supports_prompt_cache` 请求分支和对应的假 capability tests；保留响应中
`cached_tokens` 的解析与 usage 传播，因为后端仍可能自动缓存。

Ollama 同样不暴露 prompt cache 请求参数。`system_cache_key` 重命名为
`system_prefix_hash`，明确它只是稳定前缀的观测 hash，不代表 cache key 已发送给 Provider。

### 7.3 原子切换要求

Provider method、AgentLoop caller、Context request builder、TextProtocolAdapter、Fake、
provider benchmarks、live E2E wrapper 和 scripted clients 必须在 Plan 2 同一提交序列完成。
任何提交都不得留下“新 caller + 旧 adapter”或 Runtime 依赖 `hasattr` 的状态。

当前职责重命名固定为：

| 旧名称 | 当前名称 |
| --- | --- |
| `ContextManager.build_v2` | `ContextManager.build_request` |
| `_count_tokens_for_v2` | `count_tokens` |
| `complete_v2` | `complete` |
| `FallbackAdapter` | `TextProtocolAdapter` |
| `bench_build_v2.py` | `bench_request_build.py` |
| `system_cache_key` | `system_prefix_hash` |

`test_p1_smoke.py`、`test_p2_smoke.py`、`test_p3_smoke.py` 直接删除。其他以 `v1/v2/phase1`
命名的 test 先把唯一行为断言合并到现有职责测试，再删除或改为职责名；不能只为保留测试数量
继续保存阶段文件。

## 8. Provider 配置与 TOML

### 8.1 单一 Provider resolver

实现一个简单共享函数集合，不引入配置对象层级。Provider、model、base URL/host 和 API key
均采用：

```text
显式 CLI 值
  → 当前 repo root 的 .env
  → 当前进程 environment
  → 代码默认值
```

Runtime、`config show`、`doctor`、provider benchmarks 和 live E2E 必须调用同一 resolver，
并对同一输入给出相同 value/source/name。正常 CLI 读取 project env 后不再写入全局
`os.environ`；只把解析结果和用于 redaction 的快照显式传入下游。

API key 在每一个来源内部的顺序是：

| Provider | 变量顺序 |
| --- | --- |
| OpenAI | `PICO_OPENAI_API_KEY` → `OPENAI_API_KEY` → `PICO_API_KEY` |
| Anthropic | `PICO_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY` → `PICO_API_KEY` |
| DeepSeek | `PICO_DEEPSEEK_API_KEY` → `DEEPSEEK_API_KEY` → `PICO_API_KEY` |
| Ollama | 无 API key |

删除 `PICO_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY` 及 OpenAI/Anthropic 互相 fallback；
不提供 alias。这样错误 key 不会再被静默送给另一 Provider 并表现为难以定位的 401。

当前私有 `.env` 在 Plan 2 做一次安全 rename：

- 只读取 exact-root `.env`；
- 若只有 `PICO_RIGHT_CODES_API_KEY`，原子改为 `PICO_API_KEY`；
- 不打印、记录或比较显示 secret value；
- 备份放在 repo 外私有目录；
- 若新旧 key 同时存在且值不同，停止并要求人工消解，不覆盖；
- 若相同，保留一个 `PICO_API_KEY` 并删除旧名；
- 使用当前 private reader、lock 和 atomic writer，不写专用迁移框架；
- 完成后 production structural scan 确认旧变量不再参与解析；当前 `.env` 只用一次性、
  不输出 value 的 key-set 检查验证，不能让仓库测试依赖本地 secret 文件。

### 8.2 Project Environment 可见性

`.env` 真源保持 `WorkspaceContext.repo_root/.env`。不搜索 parent、siblings、main checkout
或用户 home。`config show`、`doctor`、`init` 和 `config set-secret` 统一展示：

```json
{
  "workspace": {"repo_root": "/absolute/current/worktree"},
  "project_env": {
    "path": "/absolute/current/worktree/.env",
    "scope": "repo_root_exact",
    "status": "loaded"
  }
}
```

`status` 只允许：

- `loaded`：文件是安全 regular file，且所有被采用的配置行解析成功；
- `missing`：exact path 不存在；
- `review_required`：path 存在但类型/link/权限不安全、无法读取、含被拒绝行，或 key rename
  存在冲突。

输出可以显示变量名和命中来源，不得显示变量值、API key、authorization/header、secret query
或未脱敏 URL。Plan 1 只增加 path/scope/status provenance，不复制 Provider value resolution；
Plan 2 再让所有值解析进入单一 resolver。

测试覆盖 root、子目录、两个 worktree、missing、symlink、hardlink、directory、invalid line、
source parity 和 secret redaction。text 与 JSON 输出都必须可诊断路径，但不能泄密。

### 8.3 TOML

删除 `_parse_scalar`、手写 `load_pico_toml`、`load_pico_toml_full` 的 fallback、Python <3.11
分支和每个字段独立重新读文件的 helper。

最终行为：

1. Pico 构造时用 stdlib `tomllib` 读取一次 `repo_root/pico.toml`；
2. 一个简单函数验证当前 `policy`、`context`、`memory` 字段并返回 plain dict；
3. 结果作为该 Pico 实例生命周期内的不可变 snapshot 传给现有消费者；
4. 文件不存在：使用全部默认值；
5. TOML syntax 错误或 top-level 不是 table：发出固定、无内容 warning，整文件使用默认值；
6. 单个字段类型/范围无效：只让该字段回退默认，其余有效字段保留；
7. 不打印错误行内容，不猜测 malformed TOML 的部分值。

不增加 watcher、reload、config class、schema dependency 或跨实例 cache。

## 9. Memory 当前模型

### 9.1 Plan 1 立即修复与删除

当前 workspace 和 `~/.pico/memory` 都为空，因此不存在需要转换的真实 Memory 数据。
Plan 1 直接：

- 从 CLI 删除 `memory migrate`；
- 删除 `_memory_migrate_cmd`；
- 删除 `cli_memory_migrate`；
- 删除两套相反方向 migration 的 tests、help 和 usage；
- 不先加固随后删除的 migration 代码。

Plan 1 manifest 若意外发现 `agent/*.md`、`topics/*.md`、`agent_notes.md.legacy` 或其他待迁移
Memory 数据，必须停止并修订本设计；不得临时发明 converter。

`memory review` 改为复用同一个 `BlockStore`：

```python
store.read("workspace/agent_notes.md")
```

不存在返回 empty；symlink、hardlink、FIFO、directory、越界和 inode 变化沿用现有安全 reader
拒绝。已复现的外部 canary 必须不再出现在 stdout、stderr 或 JSON。

`append_agent_note` 继续复用当前 read/validate/atomic-write 流程，但在每个 scope 的既有
`locked_file` 下完成整个 read-modify-write，避免并发丢写。锁必须位于对应 private root，
不能用进程内 mutex 代替跨进程文件锁。

### 9.2 最终单一写入模型

最终 Memory 只有：

- `<scope>/notes/**/*.md`：User Notes，用户写、agent 只读；
- `<scope>/agent_notes.md`：Agent Notes，agent append-only。

`memory_save` 只接受 `note` 和 `scope`。删除 topic/type 参数、topic slug、
`write_agent_topic`、`agent/legacy-import.md` 约定和 per-topic indexing。保留 User Notes 的
frontmatter、`[[name]]` link expansion、`supersedes` tombstone、BM25 field boosts 和
workspace/user scope。

### 9.3 一次查询一个 snapshot

不建立跨查询 cache。`BlockStore` 的现有安全扫描和 bounded reader 形成一个内部 document
load 真源；一次调用返回 path、size/mtime、frontmatter、first line 和 raw content。它不是
新的 public model，也不增加 metadata-only scanner。

`list()` 和 `Retrieval.search()` 复用该真源。`search()` 一次拿到 snapshot 后完成：

- supersedes/tombstone 集合；
- 可检索 documents 和 field tokens；
- frontmatter name index；
- DF、长度统计和 BM25；
- snippets 与 link expansion。

每个文件每次 query 最多一次 bounded read。query 返回后丢弃 snapshot；下一次重新读磁盘，
因此修改、删除、tombstone 和 links 立即可见，无 invalidation 逻辑。删除 `stat_all()`；
它没有生产调用者，也没有性能价值。

验收必须同时证明 read count、结果排序/score/snippet parity、下一查询 freshness，以及
symlink/hardlink/FIFO 继续 fail closed。

### 9.4 删除 tests-only facade

删除 `LayeredMemory`、`default_memory_state`、`normalize_memory_state`、state-level
task/file/note mutation、tests-only retrieval/rendering 和 legacy mirrors。

保留七个生产 helper：

- `canonicalize_path`；
- `file_freshness`；
- `normalize_file_summaries_dict`；
- `set_file_summary_dict`；
- `invalidate_file_summary_dict`；
- `invalidate_stale_file_summaries_dict`；
- `summarize_read_result`。

若留在 `pico.features.memory` 是最少改动，就保留文件名；不为七个函数新建抽象层。

## 10. 当前持久化合同

### 10.1 哪些记录需要版本

版本只解决“一个文件可否由当前 reader 独立解释”。最终分类：

| Family | `record_type + format_version` | 原因 |
| --- | --- | --- |
| 顶层 session | 是 | SessionStore 独立加载 |
| 顶层 recovery checkpoint | 是 | CheckpointStore 独立加载 |
| 顶层 tool change | 是 | CheckpointStore 独立加载 |
| 每个被独立读取/验证的 benchmark input/output family | 是，各自常量 | 文件可以跨命令消费 |
| embedded task checkpoint | 否 | 只随 session 解析，继承 session version |
| verification evidence | 否 | 只随 recovery checkpoint 持久化 |
| restore plan | 否 | 内存中临时 preview，不是长期 Store family |
| run/task/report/trace artifacts | 否 | 当前审计输出，无兼容分派 |
| Memory Markdown、`.env`、TOML、普通文本和 lock | 否 | 非结构化版本合同 |

顶层格式统一：

```json
{"record_type": "session", "format_version": 1}
```

`format_version` 必须是整数且精确等于当前值；bool、float、string、missing 和未知值都拒绝。
`record_type` 必须与 reader 期望精确一致。删除 `schema_version`、
`checkpoint-record-v1`、`tool-change-record-v1`、`phase1-v1` 等代际编码。
生产常量只保留 `SESSION_FORMAT_VERSION`、`CHECKPOINT_FORMAT_VERSION`、
`TOOL_CHANGE_FORMAT_VERSION` 和各 benchmark family 自己的 format constant。

Benchmark 不建立单一 `BENCHMARK_FORMAT_VERSION`。固定 benchmark definition、固定 benchmark
result、metrics experiment outputs、memory-quality artifact 和 live E2E report 等每个真正被
独立 parser 消费的 family 使用自己的 type/constant；只打印到 stdout、临时目录且没有 reader
合同的 perf JSON 不加版本。

### 10.2 Session 最终形态

顶层 session 必须显式包含当前 runtime 所需字段：

- `record_type`、`format_version`；
- `id`、`created_at`、`workspace_root`；
- `messages`；
- `working_memory`、`memory`、`recently_recalled`；
- `checkpoints`、`resume_state`、`recovery`、`runtime_identity`。

`messages` 是唯一 transcript；`history` 不存在。Embedded task checkpoints 不再有自己的
`schema_version`。`runtime_identity.feature_flags` 只保留真实影响行为的 flags，顶层与嵌套
identity 中都没有 `prompt_cache`。

新 session constructor 一次写全当前 shape。`SessionStore.load` 只校验和返回，不静默调用
旧 converter、不把 missing fields 补成旧默认、不在读取时改写磁盘。`_ensure_session_shape`
只能用于新建内存对象的当前不变量，不能作为旧 session reader。

### 10.3 Recovery 与 benchmark

Recovery checkpoint 和 tool-change 使用当前 constructors 的完整 required fields。迁移时为
旧记录补入当前缺失字段的安全空值，例如 status/review/integrity、prepared entries 和
recovery context；不得推测未发生的 effect、approval 或 verification。

Verification evidence 删除独立版本字段，但字段验证仍保留。Restore plan 删除版本常量，只是
一次 preview 的临时 plain dict。

最终 reader 对错误 type/version、缺失 required field、非 JSON object、duplicate-sensitive
输入和不安全文件继续 fail closed；错误不得包含文件内容或 secret。

## 11. 一次性本地数据迁移

### 11.1 范围

Plan 1 先用当前安全 reader 和 file identity primitive 生成 manifest。基线是 46 个 `.pico`
regular files，不把数量硬编码为业务规则：

- 8 个 transform targets：4 session JSON + 2 checkpoint JSON + 2 tool-change JSON；
- 38 个 verify-only files：36 run artifacts + 2 lock files；
- Memory 为 0；
- 不遍历其他 repo、worktree、checkout 或 `~/.pico` 非空区域。

当前 `0644` checkpoint lock 在 manifest 前通过现有 lock helper 收紧为 `0600`。如果实际
文件集合、type、link count 或安全身份与基线不符，preflight 停止并更新 manifest，不猜测。

### 11.2 事务

Plan 3 使用一次性的最小迁移函数，不建设 framework。备份目录：

```text
~/.pico/backups/<repo-hash>/<timestamp>/
```

目录 `0700`，文件 `0600`。只备份 8 个将变换的 JSON 原字节；38 个 verify-only 文件以 hash
证明不变，不复制。

迁移 journal 与备份同目录，只有：

```text
prepared → applying → verified
```

流程：

1. 确认没有 Pico 运行进程并获取现有 store locks；
2. 对每个目标记录 lexical path、device/inode、link count、mode、mtime、size 和 SHA-256；
3. 拒绝 symlink、hardlink、FIFO、directory、越界和 manifest 外文件；
4. 备份 8 个原字节，fsync 文件与目录；
5. 写 `prepared` journal；
6. 每次写前重新验证 path/inode/nlink/hash precondition；
7. 用现有 private atomic writer 写同目录临时文件、fsync、replace，并记录已应用 path；
8. 恢复原 mode 与 mtime；
9. 用最终严格 Store API 全量重读 8 个 JSON；
10. 重新 hash 38 个 verify-only files；
11. 验证业务不变量后写 `verified`。

中断后只能根据 journal 继续验证/完成或整批 rollback，不能并行启动第二次迁移。任一步失败都
从私有备份恢复全部已变换文件并重新验证 hash；不能留下半数新格式。

### 11.3 变换

Session：

- v2/v3 统一到当前 `record_type=session`、`format_version=1`；
- 保留 id、created_at、workspace_root 和 messages 顺序；
- 删除 `history`；
- 写全当前 required top-level fields；
- 删除 30 个 embedded checkpoint 的代际版本字段；
- 从所有 runtime identity 删除 `feature_flags.prompt_cache`；
- 不新增、删除或重排 Canonical Messages。

Recovery：

- checkpoint 写 `record_type=checkpoint`；
- tool change 写 `record_type=tool_change`；
- 两者写当前 `format_version`；
- 删除旧 `schema_version`；
- 按当前 constructor 补全缺失字段的安全空值；
- 保留所有 ids、record timestamps、path 顺序、status、approval、effect 和 trace references。

Run/trace/tool result/locks：

- 不解析重写；
- 内容必须 byte-identical；
- 除 preflight 明确收紧的 checkpoint lock mode 外，mode/mtime 不变。

迁移验证通过并切换严格 readers 后，在 Plan 3 最终提交删除迁移器、旧 converters、旧 readers、
`legacy=True`、additive compatibility defaults 和 migration-only tests。备份不自动删除；
需要回滚时使用备份和仍包含迁移器的中间 Git commit。

## 12. Dead flag 与 Runtime identity

`DEFAULT_FEATURE_FLAGS["prompt_cache"]` 没有生产 `feature_enabled` 调用者，不决定真实 Provider
cache 行为。它在 Plan 3 与 session 迁移同一事务删除：

- 从默认 flags、CLI/config surface 和 tests 删除；
- 从顶层及 embedded runtime identity 移除；
- 旧 session 迁移原子清除该 key；
- 严格 runtime 不再兼容过滤旧 key。

Plan 2 不提前删除它，否则仍在磁盘的 session 会产生 identity mismatch。删除的是 dead feature
flag，不是第 7.2 节保留的 Anthropic/DeepSeek cache capability。

## 13. 核心复杂度收敛

Plan 4 开始时，以 Plan 3 完成后的 HEAD 重新生成 C901 JSON baseline；68 只是设计时参考。
只修改：

- `ToolExecutor.execute`；
- `AgentLoop.run`。

先为 `ToolExecutor.execute` 固定行为矩阵：

```text
validate/approve
  → prepare/execute
  → record effects
  → terminalize result or failure
```

矩阵覆盖 invalid tool、approval deny/ask、read-only、safe/complex shell、tool exception、
KeyboardInterrupt、pending tool change、checkpoint、verification、redaction 和 primary exception。
每个 pending change 出口必须 terminalize，或留下明确 interrupted evidence。

再为 `AgentLoop.run` 固定：

```text
preflight
  → one model attempt
  → decode/apply one Action
  → finalize once
```

保留 `Response → decode_action → Action` 唯一路径、one-shot retry feedback、tool pair 原子持久化、
usage 聚合、checkpoint 触发、terminal artifact 和 primary exception 顺序。

验收 ratchet：

1. 两个目标 coordinator 各自 C901 ≤ 10；
2. `tool_executor.py` 与 `agent_loop.py` 的最高复杂度都低于 Plan 4 开始值；
3. 两个文件各自 C901 violation 数不增加；
4. 全项目 C901 总数不高于 Plan 4 开始 baseline；
5. focused behavior matrix、全量测试和 live offline assertions 全绿。

不引入 registry、event bus、新状态机、dataclass pipeline 或通用执行框架。RecoveryManager、
RecoveryPolicy、safe_subprocess、security 和 `tools.validate_tool` 不在本次重构范围。

## 14. 构建、依赖与 CI

### 14.1 Lock 与构建边界

从 `.gitignore` 移除 `uv.lock` 并提交。用 CI 同版 uv `0.11.26` 生成最终 lock；本机
0.11.19 的 `uv lock --check` 只能用于检查，不能作为最终生成证据。CI 使用：

```bash
uv sync --frozen --dev
```

运行时依赖保持为 0。业务修改不顺手升级 dev dependencies。

`pyproject.toml` 只补当前需要的准确 description、`readme = "README.md"`、唯一 console entry
和明确 package/archive selection。发布元数据延后。

sdist 允许的源内容只有 `pyproject.toml`、`README.md`、`pico/**` 以及构建后端必需的生成
metadata；不得包含 tests、benchmarks、docs、`.pico` 或历史资产。Wheel 只包含 `pico/**`
和标准 `.dist-info`。

CI build smoke：

```text
build wheel + sdist
  → inspect exact archive roots and wheel METADATA
  → clean venv install wheel
  → command -v pico resolves to that environment
  → pico --help
  → pico doctor --offline
```

### 14.2 CI 平台

保留 Ubuntu Python 3.11/3.12 的 lint、全量 pytest 和 offline live harness。Push 到 `main`
或 `memory` 都触发 CI；pull request 继续全覆盖。

新增 Python 3.12 `macos-latest` focused job，使用 frozen lock，覆盖：

- project environment security；
- file locks/private paths/artifact security；
- safe subprocess 和 shell corpus；
- recovery durability；
- Memory reader 与 append lock。

FIFO 参数化测试改用 `multiprocessing.get_context("spawn")` 的现有仓内模式，消除后台线程
`fork()` warning；不得 blanket skip、过滤 warning 或放宽安全断言。

## 15. 最终文档与仓库资产

### 15.1 唯一面向维护者的 tracked 文档面

最终面向用户/维护者的 tracked 文档精确为：

1. `README.md`；
2. `CONTEXT.md`；
3. `docs/cli-installation-and-updates.md`；
4. `docs/architecture.md`；
5. `docs/security.md`；
6. `docs/recovery.md`；
7. `docs/verification.md`；
8. `docs/memory.md`。

不保留 ADR 层。CLI、格式硬切和其他决策直接写入 `architecture.md`、`recovery.md` 和
`verification.md`，Git commit/history 记录决策演变。删除已跟踪
`docs/adr/0039-make-explicit-cli-surface-primary.md`，不创建两个新 ADR。

`benchmarks/live_e2e/fixtures/seed_cache_note.md` 和
`tests/fixtures/bench_repo_readme/README.md` 是可执行验证输入，不属于文档面，必须保留。
除此之外，不以 `README.md` 后缀为由保留 benchmark 使用说明；其仍有效的命令统一进入
`docs/verification.md`。

`.gitignore` 必须显式允许上述 docs 文件，并停止忽略 tracked `CONTEXT.md`；其余 `docs/*`
继续默认忽略，防止本地草稿误入版本控制。

### 15.2 各文档职责

| 文件 | 唯一职责 |
| --- | --- |
| `README.md` | 产品定位、最短安装、显式运行示例、能力与限制 |
| `CONTEXT.md` | 当前领域语言、模块边界和 agent 工作上下文 |
| `docs/cli-installation-and-updates.md` | venv/uv 安装、`pico` 路径冲突、更新与本地恢复 |
| `docs/architecture.md` | CLI → config → context → provider → action → tools → persistence 的当前结构 |
| `docs/security.md` | trust boundary、路径/secret/shell/approval 不变量和非 sandbox 限制 |
| `docs/recovery.md` | checkpoint/tool-change、restore、当前格式、备份与故障处理 |
| `docs/verification.md` | 基线与最终 HEAD 的可重建命令、平台、结果和授权边界 |
| `docs/memory.md` | User Notes、Agent Notes、retrieval snapshot、安全边界和工具行为 |

文档之间使用链接而不是复制大段内容。README 不成为架构全集；`CONTEXT.md` 不保存历史任务
日志；`verification.md` 不提交 prompt、answer、key、header、request URL 或 live JSON。

### 15.3 删除范围

Plan 5 在八份当前文档写完并通过链接审计后，从当前 Git tree 删除所有其他 tracked docs，
包括：

- `docs/superpowers/**`，含本文和后续五份 implementation plans；
- `.superpowers/sdd/**`；
- `benchmarks/results/**`；
- `docs/review-pack/**`；
- `docs/architecture/agent-harness-v1-overview.md`；
- `docs/memory-model.md`；
- `docs/adr/0039-make-explicit-cli-surface-primary.md`；
- `benchmarks/live_e2e/README.md`；
- `benchmarks/live_e2e/results/README.md`；
- `benchmarks/memory_quality/README.md`；
- `benchmarks/perf/README.md`。

`DATA_PROVENANCE.md` 随历史 result 目录删除，不改写。没有 `docs/archive`。

本地 ignored/untracked `docs/adr/0001...0038`、`.superpowers/brainstorm/`、
`task_plan.md`、`findings.md`、`progress.md` 和三份已存在的 untracked superpowers 文档均不移动、
不删除、不 stage。结构审计只看 `git ls-files`，不要求这些本地文件物理消失。

## 16. 五份顺序 implementation plans

本文批准后只先使用 `writing-plans` 生成 Plan 1。上一计划完成、全绿、提交并复核实际树后，
才按新的 HEAD 写下一份，避免预生成失真的巨型计划。

### Plan 1：安全与可复现基线

- 固定 rename/delete manifest、source baseline、46-file `.pico` manifest 和 staged allowlist；
- 通过现有 lock helper 收紧 checkpoint lock mode；
- 为已复现 symlink canary 写 focused failing test，再让 `memory review` 使用 `BlockStore.read`；
- 删除 `memory migrate` 和两套 migration 实现/tests；
- 给 `append_agent_note` 加现有 per-scope file lock；
- 增加 exact-root project env path/scope/status 诊断，不复制 Provider resolver；
- 用 uv 0.11.26 生成并跟踪 lock，CI frozen sync，push branches 增加 `memory`；
- focused security、全量本地门禁和 offline harness 全绿后提交。

### Plan 2：Python、CLI、Provider 与配置当前面

- 移动 Fake，删除 Provider/evaluation/memory/CLI facade，收窄顶层七个 exports；
- 原子切换 `complete`、`build_request`、`TextProtocolAdapter` 和所有 callers；
- 删除 streaming、`supports_native_tools` 和 OpenAI text transport 的 dead cache request 分支；
- 实现共享 Provider resolver，删除跨 Provider key fallback；
- 安全迁移 exact-root `.env` 的 `PICO_RIGHT_CODES_API_KEY` → `PICO_API_KEY`；
- 只保留 `pico` 与 `python -m pico`，删除 `pico-cli` 和 bare prompt；
- 用 `tomllib` parse once；
- 重命名 `build_v2`、`bench_build_v2.py`、`FallbackAdapter`、`system_cache_key` 等批准项；
- 不在本计划删除 `prompt_cache` feature flag；
- 运行 Provider payload/parity、CLI、config/TOML、imports、全量测试和 offline harness。

### Plan 3：持久化与 Memory 硬切

- 基于 Plan 1 manifest 写一次性 8-JSON transaction、backup、journal、fault injection 和 rollback；
- 38 个 verify-only files 保持 hash/byte 不变；
- 切换顶层 `record_type + format_version`，删除 embedded/verification/restore 的多余版本；
- 删除 session `history`、embedded checkpoint 版本和全部 identity `prompt_cache`；
- 同步删除 dead `prompt_cache` feature flag；
- 切换严格 current readers 后删除 converter、compat defaults 和迁移器；
- 删除 per-topic writer/schema 和 tests-only `LayeredMemory`；
- 实现 per-query document snapshot，删除 `stat_all`；
- 完成 migration evidence、Memory security/parity、全量测试和 offline harness。

### Plan 4：两个核心协调器收敛

- 在 Plan 3 HEAD 记录 C901 JSON baseline；
- 先冻结并验证 ToolExecutor 行为矩阵，再把 `execute` 收敛到 ≤10；
- ToolExecutor 独立全绿后冻结 AgentLoop 行为矩阵，再把 `run` 收敛到 ≤10；
- 运行 complexity ratchet、focused matrices、security/recovery suites、全量测试和 offline harness；
- 不改造其他高复杂度安全/恢复函数。

### Plan 5：Build、macOS、八份文档与最终收敛

- 收紧 sdist/wheel，增加 archive、METADATA 和 clean-install smoke；
- 增加 macOS focused CI 并用 spawn 消除 fork warnings；
- 写完并互链八份当前文档，修改 `.gitignore`；
- 删除全部其他 tracked docs、SDD、review pack、results 和 ADR，保护 untracked；
- 增加精确 repository structure audit；
- 完成本地全量、offline harness、benchmarks、build、Ubuntu/macOS CI；
- 本地所有门禁通过后另行取得一次明确授权，只执行一个真实 Anthropic 或 DeepSeek E2E；
- 对最终 HEAD 做独立 review并填写第 18 节证据矩阵。

每份计划允许多个小提交，但每个提交只 stage allowlist 文件。计划之间不得合并回滚边界。
GitHub CI 结果只能在实际 push 后记录，不能用本地推断。

## 17. 精确结构合同

新增 `tests/test_repository_structure.py`，只检查本次批准的 manifest：

### 17.1 必须不存在

- 模块：`providers.clients`、`evaluation.metrics`、`metrics_experiments`、`evaluator`；
- symbols：`complete_v2`、`build_v2`、`FallbackAdapter`、`stream_complete`、
  `supports_native_tools`、两套 Memory migration functions、`write_agent_topic`、
  `stat_all`、`LayeredMemory`；
- config names：`PICO_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`；
- CLI：`pico-cli`、bare-prompt dispatch；
- persistence：runtime `schema_version` readers、embedded task checkpoint version、
  restore-plan/verification version constants、runtime migration/deprecated alias；
- dead state：`prompt_cache` feature flag；
- tracked paths：除八份批准文件外的所有面向维护者文档、`.superpowers/sdd`、
  benchmark 使用说明/results、review pack 和 ADR；两份批准的 Markdown fixture 不在删除清单。

### 17.2 必须精确成立

- `pyproject.toml` 只有 `pico` console entry；
- `pico.__all__` 精确为七个批准 exports；
- Provider/evaluation/memory `__init__.py` 不重导出；
- Runtime/benchmarks/tests 从真实模块导入；
- structured Provider 只暴露 runtime-facing `complete`，OpenAI/Ollama 只暴露
  `complete_text` 并由显式 Adapter 装配；
- 顶层 session/checkpoint/tool-change reader 只接受各自当前 type/version；
- `git ls-files` 的面向维护者文档集合精确为八份，另只允许两份精确列名的 Markdown fixture；
- sdist/wheel roots 符合第 14.1 节；
- `.gitignore` 能跟踪八份文档而继续忽略其他本地 docs 草稿。

扫描只使用批准的 exact path/symbol/token 数据，不泛化禁止 `_v1/_v2/_v3/phase1`、数字、
真实 Provider URL version、用户数据、`compatible` 或真实错误 fallback。测试自身的 manifest
常量不参与源码扫描，避免自命中。

## 18. 验证与证据

### 18.1 每个任务

先运行 focused tests、touched-path Ruff 和：

```bash
git diff --check
```

### 18.2 每份计划

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
```

只在相关计划运行额外门：

- Plan 1：Memory canary、append concurrency、two-worktree env provenance、46-file manifest；
- Plan 2：Provider payload/parity、resolver source parity、`.env` rename、TOML read-count、
  CLI/import structural tests；
- Plan 3：transaction fault points、backup hash、rollback、strict full reread、38-file byte parity、
  query read-count/result parity/freshness；
- Plan 4：C901 JSON ratchet与两个行为矩阵；
- Plan 5：archive listing、wheel METADATA、clean venv、macOS CI、tracked-doc exact set、link scan。

Harness、context、memory、recovery、memory-quality 和 perf 输出只写临时目录。

### 18.3 真实 Provider

Provider/Context 接口和配置 resolver 都会改变真实 wire path。只有最终本地全量门禁通过后，
才能另行取得一次明确授权，执行一个真实 Anthropic 或 DeepSeek E2E。不得自动沿用此前授权，
不得运行 Provider matrix。结果只记录 Provider/model、commit、命令、counts/caps、耗时和
redacted summary，不提交 request/response JSON 或 secrets。

### 18.4 完成证据矩阵

| 规划项 | 必需证据 |
| --- | --- |
| Memory review 外部读取已关闭 | focused symlink/hardlink/FIFO/directory/inode tests + canary |
| Agent Notes 并发不丢写 | 跨进程 lock test + final line count |
| 两套 Memory migration 已删除且无待迁移数据 | manifest + CLI/symbol absence |
| Provider A+ 合同原子切换 | payload/parity tests + caller scan |
| Provider 配置来源唯一且无跨 Provider fallback | resolver table tests + runtime/doctor/benchmark parity |
| `PICO_API_KEY` rename 安全完成 | redacted conflict/success tests + old-name absence |
| TOML parse once | read-count + malformed/top-level/field tests |
| 顶层 API/import facades符合批准清单 | exact `__all__` + module/import absence |
| 8 个 JSON 事务迁移，38 个文件不变 | manifest、backup、journal fault tests、hashes、strict reread |
| Runtime 只接受当前格式 | type/version rejection + converter/source absence |
| dead flag、per-topic Memory 和 LayeredMemory 删除 | symbol/schema absence + retained helper tests |
| Retrieval 一次 query 每文件最多读一次 | read count + parity + next-query freshness |
| 两个 coordinator 复杂度和行为达标 | C901 JSON + behavior matrices |
| 唯一 CLI 为 `pico` | pyproject、help/run/repl/module smoke、usage scan |
| uv lock/build/clean install 完成 | tracked lock、archive listing、METADATA、clean venv |
| Ubuntu/main+memory/macOS/fork warning 完成 | workflow config + actual CI run |
| 面向维护者文档精确为八份且 untracked 未动 | `git ls-files` exact set（另含两份批准 fixtures）+ before/after status |
| 当前文档链接、命令和术语一致 | link/path/command scan |
| 最终独立 review 完成 | final HEAD review；不得复用基线 C0/I0/M0 |
| 最终全量与真实 E2E 达标 | final verification summary + fresh authorization |

没有明确证据的项目视为未完成。明确延后的公开发布元数据、License、跨查询 cache、
OpenAI/Ollama native tools 和 Provider matrix 标为 `deferred by design`，不得伪报完成。

## 19. 失败处理与回滚

- Memory 安全修复失败：保留 failing canary，不继续 Provider/格式硬切。
- `.env` 新旧 key 冲突：不改文件、不启动 Provider，请人工选择；不得打印值。
- TOML malformed：固定 warning + 整文件 defaults，不把部分内容带入运行。
- 数据 manifest 漂移：停止 Plan 3，重新核验；不得扩大扫描范围。
- 迁移中断：根据外部 journal 继续验证/完成或整批 rollback；不得开启第二事务。
- 最终 Store 重读失败或 verify-only hash 改变：整批 rollback。
- Plan 4 行为矩阵失败：回滚该 coordinator 提取，不通过增加兼容分支掩盖。
- macOS 与 Ubuntu 行为不同：保留 fail-closed 语义，修复平台实现或测试进程模型，不 skip。
- 文档删除误包含 untracked：以 staged allowlist 和 status snapshot 阻止提交。
- 真实 E2E 未授权或失败：本地实现可保持完成候选，但最终合规矩阵不得标记完成。

## 20. 完成定义

只有以下条件同时成立，本轮优化才完成：

- 唯一 `pico` console、无 bare prompt；
- 唯一 structured Provider 请求接口和显式 text Adapter；
- 唯一 Provider 配置 resolver，`PICO_API_KEY` 合同生效；
- 唯一 Canonical Messages 和严格顶层当前格式；
- 唯一 Agent Notes 写入模型和 per-query Memory snapshot；
- Package facade、迁移器、dead flag、tests-only state 和批准的旧命名全部消失；
- 当前 8 个 JSON 完成事务迁移，38 个 `.pico` 文件验证不变；
- Memory 外部读取与并发丢写风险关闭；
- TOML parse once；
- 两个目标 coordinator C901 ≤10 且行为不变；
- uv lock、build、clean install、Ubuntu/macOS CI 和 warning 门通过；
- 面向维护者的 tracked 文档精确为八份，两个 Markdown fixtures 保留，历史资产删除，
  所有 untracked 原样保留；
- 本地全量、offline harness、benchmarks、最终独立 review 和经新授权的一个真实 Provider E2E
  均有当前 HEAD 证据；
- 五份顺序计划各自有提交、验证与回滚边界；
- 第 18.4 节矩阵逐项有证据。
