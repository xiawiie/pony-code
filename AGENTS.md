# Pico Agent 工作约定

本文件是全仓唯一 Agent 规则真源；不要创建重复规则文件。用户、领域、架构、安全与发布说明分别见 README 及
`docs/domain-model.md`、`docs/architecture.md`、`docs/security.md`、`docs/verification.md`。

## 1. 产品目标与边界

Pico 1.0 是本地 coding agent：从仓库构造上下文，以受约束工具修改和验证代码，并把 Session、Run、Checkpoint、
Memory 与恢复证据保存在 `.pico/`。

维护优先级：

1. 一个可安装的 `pico` CLI/TUI 完成配置、运行、检查与恢复；
2. Provider 路由显式、可观察，不猜测、不 fallback；
3. 文件、secret、approval、recovery 与 Sandbox 边界 fail closed；
4. 代码按领域集中，公共 API 和分发包最小；结论由当前 exact HEAD 的证据支撑。

非目标：动态 Provider registry、旧兼容层、自动探测、distributed/remote/multi-tenant Sandbox 和无需求的抽象。
Host 不是 OS sandbox；本地 Docker Sandbox 也不是 microVM。

## 2. 开始工作与 Git 纪律

开始前先检查：

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git log -1 --oneline
```

- 用户修改、未跟踪文件和其他 worktree 不覆盖、不移动、不重置、不顺手清理。
- 生产收口、大型重构和发布准备从最新 `origin/main` 建独立 worktree 与 `codex/<topic>` 分支；先确保干净再 fetch，
  fetch 失败即停止。
- 若 `origin/main` 已前进，在干净 worktree 中 rebase 后再修改；不得操作主工作区的用户文件。
- 普通任务不隐含 commit、push、tag、PR、Release 或 PyPI 授权。
- 搜索优先使用 `rg` / `rg --files`。先定位责任模块、最窄测试和文档真源，再动代码。

## 3. 公共边界与目录所有权

`pico/` 顶层只能有 `__init__.py` 和 `__main__.py`。`import pico` 只公开 `Pico`；console entry 是
`pico.cli.app:main`；构造合同为 `Pico(model_client, workspace, session_store, *, session=None, options=None)`。

可选设置只进入冻结的 `RuntimeOptions`。内部从所属模块导入；package `__init__.py` 只写说明，不做 re-export/shim。

| 路径 | 唯一责任 |
| --- | --- |
| `pico/agent/` | Action、Agent Loop、Canonical Messages、compaction、预算与观测 |
| `pico/cli/` | app、arguments、assembly、命令、REPL、人类/JSON 输出与 doctor |
| `pico/config/` | `.env`、Provider 规格与 `pico.toml` 校验 |
| `pico/context/` | Context source、chunk、escaping、render 与 digest |
| `pico/memory/` | User/Agent Notes、recall、retrieval、RepoMap 与 memory service |
| `pico/providers/` | 四个 wire adapter、Response、transport helper、factory 与 probe |
| `pico/recovery/` | 恢复模型、policy、migration、writer 与 manager |
| `pico/runtime/` | `Pico` 装配、options、reporting、rewind 与 working memory |
| `pico/sandbox/` | 本地 Docker、identity、filtered staging、session、diff/apply 与资源 |
| `pico/security/` | path、private/workspace file 与 redaction 原语 |
| `pico/state/` | Session/Run/Checkpoint store、TaskState 与 file lock |
| `pico/tui/` | 行内 prompt、slash completion、马形品牌、状态与 approval/activity 渲染 |
| `pico/tools/` | registry、validation、executor、approval、effect recorder 与 subprocess |
| `pico/workspace/` | root discovery、WorkspaceContext、snapshot 与 observer |

开发资产不进入 runtime package；Fake Provider 只在 `benchmarks/support/`，evaluation 不回迁 `pico/`。

CLI/TUI 合同：

- 裸 `pico` 与 `pico repl` 进入同一个交互会话；`pico run <prompt...>` 一次执行后退出。
- `runs`、`sessions`、`session`、`checkpoints` 等显式管理命令保持独立；未知首 token 不得静默变成 prompt。
- TUI 是 presentation adapter，必须与纯文本 fallback 共用 REPL handler、Agent、Session、finalize 和错误语义。
- `/` 菜单只展示真实命令；不得增加绕过 approval 的 `!` shell mode、动态 Provider/Model 或第二命令 registry。
- TUI 只在 stdin/stdout 为 TTY、`TERM` 可用且宽度足够时启用；必须遵守 `NO_COLOR` / `--no-color`。
- 马形、字标、快捷键和输入框使用默认前景或中性灰；error、warning、success 继续使用语义色。
- UI listener 只能在 trace durable append 后收到脱敏副本；approval UI 异常必须 fail closed，退出时恢复 hook。

## 4. Provider 与 `.env` 合同

| Provider | Variant | Transport | 默认认证 |
| --- | --- | --- | --- |
| `anthropic` | `messages` | `anthropic_messages` | `x-api-key` |
| `openai` | `responses` | `openai_responses` | `bearer` |
| `openai` | `chat_completions` | `openai_chat_completions` | `bearer` |
| `ollama` | `chat` | `ollama_chat` | `none` |

唯一用户配置面是仓库根目录 `.env` 中六个变量：

```text
PICO_PROVIDER
PICO_MODEL
PICO_API_URL
PICO_API_KEY
PICO_API_VARIANT
PICO_AUTH_MODE
```

必须保持：

- 运行须显式配置 Provider/model/API URL；云端须有 Key，只有 Ollama `auth=none` 可空。`pico init` 写全六项。
- lexical repository root 的 `.env` 高于同名进程变量；不搜索父目录、不修改全局 `os.environ`。
- 不读取厂商 Key、旧 Provider/Profile/Connection 字段或旧 Pico 变量作为 fallback。
- Variant/Auth 的 `auto` 仅选静态默认值，不联网探测。
- CLI、doctor、probe、live harness 与 benchmark 共用配置解析和 Transport factory；benchmark 只以
  `--cwd` / `--repo-root` 选择 `.env`。
- URL 禁止 userinfo、query、fragment 与内嵌凭证；除 loopback 外必须 HTTPS。Adapter 不补版本前缀、不跟随
  redirect、不在失败后切换 Transport。
- Session binding 的 protocol、model 或 endpoint 变化返回稳定的 `model_session_mismatch`，不跨协议重放状态。

修改该合同须同步检查 config/providers/CLI、benchmarks、`.env.example`、用户文档和 Provider/CLI 测试。

## 5. Agent、Tool、Session 与 Recovery 不变量

- 一个 Model Attempt 最多一次请求；成功响应只产生一个 Tool、Final 或 Retry Action。
- 多 tool calls 整体拒绝；同一 turn 的 retry/follow-up 复用 immutable snapshot。
- Canonical Messages 是唯一 transcript；Provider adapter 不维护第二套可变 history。
- Tool 先做 schema、policy、当前授权与必要 approval，再进入 mutation lock；执行一次并观察真实 effect。
- `memory_save` 只接受当前请求的明确授权；历史授权不继承，delegate 不能写 Durable Memory。
- Session、Run、Checkpoint 与 Tool Change 使用独立 record format 和 reader；release version 不能代替 format version。
- Compaction 不删除 append-only Session 历史，不授予 Memory 写权限，也不恢复 workspace。
- 持久化失败后不继续请求 Provider；cleanup、observer 或 finalizer 的次生错误不能覆盖 primary failure。

## 6. 文件、Secret 与 Sandbox 不变量

- 路径锚定可信 root；拒绝 traversal、symlink、hardlink、special file、root escape 与 identity drift。
- I/O 必须 bounded；写入使用 private temp、fsync、atomic replace，patch 保持 CAS。Git/RG 等内部程序使用冻结的可信路径。
- 已知 secret 持久化/输出前脱敏；测试和文档不出现 Key、header、完整 live response 或私有 prompt。
- 公开 Sandbox 只支持 macOS arm64、Docker Desktop 与已存在的 exact `linux/arm64` image。
- Container 唯一 host bind 是 filtered Execution Root；Source Root、`.pico/`、HOME、Docker socket 和凭证不挂载。
- `status`、`prepare` 与只读 inspection 零网络、零 pull/build/repair、零隐式产品状态写入。
- 最终 diff 使用完整 capture；Source Apply 绑定用户刚审查的 exact digest，并保持独立 lock、journal、CAS 与 rollback。
- identity、readiness、capture、cleanup 或 apply 事实不明时 fail closed，不回退 Host。

不要恢复网络治理、distributed authority、candidate、registry 或 SBOM/provenance runtime 字段。扩大平台或远程
能力须有独立设计、威胁模型、ADR 与实机证据。

## 7. Clean Code 执行清单

- 名称表达意图，避免模糊缩写、误导性集合名和不可搜索的魔法值。
- 函数只做一件事，语句保持同一抽象层级；超过 20 行必须主动审查，但禁止为数字无意义拆分。
- 普通函数优先 0–2 个业务参数。参数过多时先检查职责；只有真实数据簇才引入冻结 value object。
- 查询与命令分离；隐藏副作用、静默 fallback 和失败返回魔法值都不可接受。
- 注释解释安全原因、设计意图或危险后果，不复述代码，不用注释粉饰糟糕命名。
- 域失败使用明确异常；CLI 边界统一映射稳定文本/JSON 错误。`None` 只表示显式 Optional。
- 类按变化原因保持单一责任；不因行数机械拆 Store，不创建 mixin、service container 或通用事务框架。
- 选择满足当前需求的最简单实现，保持局部风格；不重构相邻代码、不全仓格式化、不增加未被证明的兼容层。
- 修改代码目标 100 字符宽、最大 120；字符串、命令和测试载荷可例外。唯一直接 runtime dependency 是
  `prompt-toolkit`；新增其他依赖必须有明确产品必要性和 distribution 证据。

## 8. 测试与验收

先跑最窄证据，再扩大范围：

```bash
uv run --frozen ruff check <changed-python-files>
uv run --frozen pytest -q <relevant-test-files>
```

纯移动先运行 `pytest --collect-only`。各领域变更运行所属专项；安全回归优先补可复现的聚焦测试。
CLI/TUI 变更至少运行 parser、commands、error envelope 与 `tests/tui/`。

结构、Provider、Sandbox、版本、分发或发布变更必须在干净 exact HEAD 运行：

```bash
./scripts/check.sh
```

它包含 lock、Ruff、全量 pytest、core-functional、offline assertions、构建、精确归档和两种 clean-install smoke。
任何失败都不能用局部测试替代，修复后从头重跑。

真实 Docker vertical 是 G7；真实 Provider 请求是收费的 G8。两者仅在环境适用且用户对当轮动作明确授权后执行。
离线 contract 或旧 SHA 不能冒充 G7/G8；未运行就写“Docker 未执行”或“live 未执行”。

## 9. 文档、打包与发布

- README 管快速使用；领域、架构、安全、安装、验证和 CHANGELOG 各维护自己的真源。
- 行为、路径、命令、Provider 表、错误码或格式变化必须同步最接近的真源；不要复制整篇文档。
- 打包只以 `pyproject.toml` 为真源。wheel/sdist 不含开发资产、截图、缓存、`.env`、`.planning` 或 Fake Provider；
  distribution verifier 精确比对内容。
- 修改 `pyproject.toml` 或 `uv.lock` 后，同步 `docker/sandbox/image-inputs.lock.json` 的输入 digest。
- Tag 必须为 `v<project.version>`。commit、push、tag、GitHub Release 与 PyPI 发布分别需要用户明确授权。
- 不提交 secret、`.pico/`、dist、evaluation artifact、cache、egg-info 或任务规划文件；保留用户的 `.venv`。

## 10. 与用户沟通

- 默认中文，先给结果与影响；长任务给简短阶段更新。
- 重要假设、范围、安全、迁移、费用和外部写操作提前说明；小型可逆细节保守推进。
- 区分 unit、offline、clean install、真实 Docker 与收费 live，不夸大证据。
- 最终列出核心改动、SHA、验证、未执行条件测试和 worktree 状态。

## 11. Definition of Done

- 用户请求已实现，没有第二配置面、旧别名、死路径或未说明的兼容层。
- 代码位于正确领域，顶层 API、目录结构与 distribution 内容符合合同。
- 聚焦测试和当前 exact HEAD 的完整离线门禁通过；安全、恢复和 fail-closed 语义未弱化。
- README、领域模型、架构、安全、安装、验证、CHANGELOG 与实现一致，Markdown 本地链接有效。
- 生成物、cache、egg-info、artifact、`__pycache__` 和任务 `.planning` 已清理，目标 worktree 完全干净。
- G7/G8、发布等条件动作明确记录为通过、失败或未执行；最终结论可由下一位维护者复现。
