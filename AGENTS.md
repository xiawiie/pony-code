# Pico Agent 工作约定

本文件适用于整个仓库。它描述 coding agent 在 Pico 1.0 中应遵守的产品边界、目录所有权、实现纪律和验收标准。
更深目录若出现新的 `AGENTS.md`，只补充所属领域规则，不得静默放宽本文件的安全、Provider、Sandbox 或发布边界。

## 1. 项目目标

Pico 是一个面向代码仓库的本地 coding agent：从当前仓库构建上下文，通过受约束工具读取、修改和验证代码，并把
Session、Run、Checkpoint、Memory 与恢复证据保存在本地 `.pico/`。

维护时优先保证：

1. 用户能用一个 `pico` CLI 完成配置、运行、检查和恢复；
2. Provider 路由显式、可观察、不会猜测或 fallback；
3. 文件、secret、approval、recovery 和 Sandbox 边界 fail closed；
4. 代码按领域集中，不重新变成顶层扁平模块集合；
5. wheel/sdist 小而准确，开发资产不进入产品包；
6. 变更能被聚焦测试和完整门禁证明。

不要为了“未来可能需要”恢复已删除的兼容层、分布式 Sandbox、Provider registry 或第二套实现。

## 2. 开始工作前

先完成以下只读检查：

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git log -1 --oneline
```

- 保护用户已有修改；未知变更不覆盖、不重置、不顺手清理。
- 大范围重构、生产收口或发布任务应从最新 `main` 创建独立 worktree 和 `codex/<topic>` 分支。是否 fetch/pull、创建
  branch、commit、push、tag 或发布，服从用户授权；外部写操作不从普通代码任务中推断。
- 若当前分支、HEAD 或工作区状态与任务预期不一致，先说明证据和影响。只有会实质改变方案、数据或安全边界的问题才
  阻塞询问；其余情况做保守假设并继续。
- 在动代码前定位最窄责任模块、相关测试和文档真源。优先使用 `rg` / `rg --files`。

## 3. 目录与所有权

`pico/` 顶层只允许保留以下产品 Python 文件：

```text
__init__.py
__main__.py
config.py
runtime.py
security.py
```

新代码必须进入明确的领域包：

| 路径 | 责任 |
| --- | --- |
| `pico/agent/` | Action、Agent Loop、Canonical Messages、compaction、预算与观测 |
| `pico/cli/` | 参数、命令、输出、inspection、doctor、REPL |
| `pico/context/` | Context sources、chunk、escaping、render、digest |
| `pico/memory/` | User/Agent Notes、recall、retrieval、RepoMap、memory service |
| `pico/providers/` | wire adapter、Response、factory、API probe |
| `pico/recovery/` | 恢复模型、policy、migration、writer、manager |
| `pico/sandbox/` | Docker local runtime、identity、staging、network、diff/apply、resources |
| `pico/state/` | Session/Run/Checkpoint store、TaskState、file lock |
| `pico/tools/` | Tool schema、executor、effect recorder、受限 subprocess |
| `pico/workspace/` | root discovery、workspace view、snapshot、observer |

其他仓库目录：

- `tests/`：产品、契约、安全、durability 和回归测试；
- `benchmarks/evaluation/`：开发期评估，不属于 runtime package；
- `benchmarks/live_e2e/`：显式授权的真实 Provider harness 与离线 assertions；
- `scripts/evaluation/`、`scripts/release/`、`scripts/sandbox/`：对应维护入口；
- `docs/`：架构、安全、安装、恢复、验证与 ADR；
- `.github/workflows/`：CI 和 tag-bound release。

不要新建第二套示例实现，不要把 evaluation 搬回 `pico/`，不要通过空 facade 或兼容 shim 隐藏错误的模块所有权。
内部代码从具体模块导入；package `__init__.py` 保持薄。

## 4. Provider 与 `.env` 合同

用户可见 Provider 只有三个：

| Provider | Variant | 内部 Transport | 默认认证 |
| --- | --- | --- | --- |
| `anthropic` | `messages` | `anthropic_messages` | `x-api-key` |
| `openai` | `responses` | `openai_responses` | `bearer` |
| `openai` | `chat_completions` | `openai_chat_completions` | `bearer` |
| `ollama` | `chat` | `ollama_chat` | `none` |

唯一运行时配置变量：

```text
PICO_PROVIDER
PICO_MODEL
PICO_API_URL
PICO_API_KEY
PICO_API_VARIANT
PICO_AUTH_MODE
```

必须保持：

- 配置从 lexical repository root 的 `.env` 或进程环境读取，项目 `.env` 优先；
- `.env` 不搜索父目录、不注入全局 `os.environ`；
- CLI 不增加 `--provider`、`--model`、`--base-url` 等第二配置面；
- 不读取 `PICO_DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等旧变量或厂商变量作为 runtime fallback；
- `auto` 只选择 Provider 的静态默认值，不探测 endpoint、模型或协议；
- Provider factory 位于 `pico.providers.factory`，`_shared.py` 只放共享 transport helper；
- CLI run、`init`、`config show`、`status`、`doctor` 和 API probe 使用同一解析结果；
- Ollama `auth=none` 允许空 Key；其他认证缺 Key 时稳定失败；
- URL 禁止 userinfo、query、fragment 和 URL 内 secret，除 loopback 外必须 HTTPS；adapter 不补版本前缀、不跟随
  redirect、不自动切换协议；
- Session binding 的 protocol/model/endpoint 变化必须返回 `model_session_mismatch`，不跨协议重放 provider state。

修改 Provider 行为时至少同步检查 `pico/config.py`、`pico/providers/`、`pico/cli/`、`.env.example`、README、架构、
安全文档和 Provider/CLI 测试。

## 5. Agent、工具与状态不变量

- 一个 Model Attempt 最多一个真实 Provider HTTP request。
- 一个成功响应只产生一个 Tool、Final 或 Retry Action；多个 tool calls 全部拒绝，不部分执行。
- 同一 top-level turn 的 retry 和 tool follow-up 复用 immutable InjectionSnapshot。
- Canonical Messages 是唯一 transcript；Provider adapter 不维护第二套可变 history。
- Tool 先做 schema、policy、当前授权和必要 approval，再进入 mutation lock；执行一次并观察真实 effect。
- `memory_save` 只接受当前用户请求中的明确授权；历史授权不继承，delegate 不能写 Durable Memory。
- Session、Run、Checkpoint、Tool Change 使用各自格式与 reader；release version 不能替代 format version。
- Compaction 不删除 append-only Session 历史，不授予 Memory 写权限，不恢复 workspace。
- Persistence failure 后不继续请求 Provider；primary error 不被 cleanup/finalizer 次生错误覆盖。

不要为绕过失败测试放宽这些不变量。若设计确需改变，先说明用户可见行为、安全影响、迁移和证据，再实施最小方案。

## 6. Workspace、文件与 secret

- 所有路径锚定可信 root，拒绝 traversal、symlink、hardlink、special file 和身份漂移。
- 文件和目录操作必须 bounded；写入使用 private temp、fsync、atomic replace，patch 保持 CAS。
- Git/RG 等内部 executable 必须走冻结的 trusted-executable 机制；不得直接信任模型提供的 PATH。
- Known secret 在 Session、trace、report、Checkpoint、Tool Change、error 和人类输出持久化前脱敏。
- 不把 Key、header、完整 live response、私有 prompt/answer 或未脱敏路径写进测试输出和文档。
- Host 模式不是 OS sandbox，不能在 UI、文档或代码注释中宣称隔离保证。

安全修复优先补一个能复现问题的聚焦测试。不要通过 catch-all exception、静默 fallback 或删除 reason code 掩盖边界失败。

## 7. Sandbox 产品边界

Pico 1.0 公开 Sandbox 只支持 macOS arm64 + Docker Desktop + already-present exact `linux/arm64` image，并只接受每次
从当前安装树重算的 sealed local authorization。

必须保持：

- Container 唯一 host bind 是 filtered Execution Root；
- Source Root、Project/Sandbox State、HOME、Docker socket 和凭证不挂载；
- Context、RepoMap、文件工具、search 和 shell 只操作 Execution Root；
- `status`、`prepare` 和只读 inspection 零网络、零 pull/build/repair、零隐式 state mutation；
- final diff 使用完整 capture；Source Apply 绑定用户刚审查的 exact digest；
- identity、readiness、capture、cleanup 或 apply 事实不明时 fail closed，不回退 Host。

不要恢复已删除的 distributed authority、candidate、product enablement、远程 cache/download、aggregate 或 release
controller。新增平台、remote Docker、registry 或多租户能力需要新的独立设计、威胁模型、ADR 和实机证据。

## 8. 实现纪律

- 先想清楚成功标准、歧义和风险，再编码。
- 选择满足当前需求的最简单实现；不增加 speculative abstraction、配置或依赖。
- 做外科式改动，保留局部风格；不重排无关代码，不借机清理相邻技术债。
- 行为变更与结构移动分开验证；纯移动后先保证 imports/collection，再改行为。
- Runtime dependencies 维持为零，除非需求无法用 stdlib 安全完成且用户接受新增依赖。
- 延迟 import 只用于真实循环或启动成本边界，不作为掩盖错误依赖方向的默认手段。
- 稳定错误码、JSON shape、CLI command、public Python exports 和 record format 都是合同；改变时更新测试和迁移说明。
- 只清理由当前变更产生的临时文件、unused import 和 artifact；不要删除用户状态。

## 9. 测试策略

先跑最窄测试，再逐级扩大。

### 聚焦测试

```bash
uv run pytest -q <relevant-test-files>
uv run ruff check <changed-python-files>
```

Provider / `.env` 变更优先覆盖：

```bash
uv run pytest -q \
  tests/test_config.py \
  tests/test_cli_commands.py \
  tests/test_cli_diagnostics.py \
  tests/test_provider_clients.py
```

Sandbox / security / recovery 变更应选择对应专项，不用普通 happy-path 代替安全证据。

### 完整离线门禁

合并、版本、结构、Provider、Sandbox 或发布改动必须运行：

```bash
./scripts/check.sh
```

该脚本包含 lock check、Ruff、全量 pytest、core-functional evaluation、offline live assertions、最终 build、archive
精确检查和两个 clean-install smoke。不要在失败后只报告局部通过。

### 条件验收

```bash
uv run python scripts/sandbox/verify_runtime.py --require-ready
```

只在受支持的本地 Docker 环境执行。真实 Provider 测试会联网并产生费用，必须取得当轮明确授权；当前 Key、旧授权或
普通“运行测试”请求都不自动构成费用授权。没有授权时运行离线 wire-contract tests，并明确标注 live 未执行。

## 10. 文档与发布

用户可见行为变化必须同步更新最接近的真源：

- 快速使用与支持矩阵：`README.md`；
- 领域词汇与模块责任：`CONTEXT.md`；
- 结构和数据流：`docs/architecture.md`；
- 安全边界：`docs/security.md`；
- 安装和 `.env`：`docs/cli-installation-and-updates.md`；
- 测试与发布：`docs/verification.md`；
- 用户迁移：`CHANGELOG.md`。

版本修改必须同步：

1. `pyproject.toml`；
2. `uv.lock`；
3. `docker/sandbox/image-inputs.lock.json` 中 pyproject/uv digest；
4. CHANGELOG 与文档中的目标版本；
5. final build 和 clean-install 证据。

产品 archive 只允许 `pico/**`、Sandbox JSON、metadata、README 和 LICENSE。不要把 tests、benchmarks、scripts、docs、
`.github`、`.env`、`.pico`、`.planning`、dist 或 evaluation artifact 加入产品包。

Tag 必须是 `v<project.version>`。Commit、push、tag、GitHub Release 与 PyPI 发布都是外部状态变化，只有用户明确授权后
执行；定义 release workflow 不等于授权当前任务发布。

## 11. 与用户协作

- 用中文优先沟通，先讲结果和影响，再讲实现细节。
- 对长任务提供简短阶段更新；不要让用户长时间无法判断进度。
- 遇到会改变产品范围、安全承诺、Provider 合同、数据迁移或外部发布的问题，及时给出证据、选项和建议。
- 小型可逆实现细节按最保守方案推进，不把所有决定都退回用户。
- 不夸大证据：offline、contract、ready status、real Docker vertical 和 paid Provider live 必须明确区分。
- 最终交付列出核心改动、验证命令/结果、未执行的条件测试和工作区/分支状态。

## 12. Definition of Done

只有同时满足以下条件，任务才算完成：

- 请求的用户行为已实现，没有隐含第二配置面或旧兼容路径；
- 代码位于正确领域目录，公共边界和 package 内容符合约束；
- 聚焦测试证明变更，全量门禁与风险成比例地通过；
- 安全、恢复、Sandbox 和 Provider 失败语义没有被弱化；
- README/架构/安全/安装/验证/CHANGELOG 与实现一致；
- 没有提交 secret、生成 artifact、用户状态或无关修改；
- 对 live、Docker、发布等条件动作明确说明已执行、未执行及原因；
- 最终报告可让下一位维护者从当前 exact HEAD 复现结论。
