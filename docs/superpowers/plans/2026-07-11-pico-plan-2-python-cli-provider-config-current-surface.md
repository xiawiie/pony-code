# Pico Plan 2：Python、CLI、Provider 与配置当前面

> 权威设计：`docs/superpowers/specs/2026-07-11-pico-current-surface-hard-cut-design.md`。

## 目标与基线

- 基线分支：`memory`。
- 基线 HEAD：`5288d59d501d6c853b04ca29bfaa66a54c75a41c`。
- Plan 1 的四个批准提交之后，追加了 `5288d59`：真实 Ubuntu CI 暴露 macOS-only
  `renameatx_np`，该提交以 Linux `renameat2(RENAME_EXCHANGE)` 补齐同一原子交换语义。
- Plan 1 最终证据：本地 `2008 passed, 6 skipped`、offline `60 passed`、build 成功；
  push CI `29156943402` 的 Python 3.11/3.12 均成功。
- 本计划不修改持久化格式、`prompt_cache` feature flag、per-topic Memory、LayeredMemory、
  ToolExecutor 或 AgentLoop 结构；这些分别属于 Plan 3/4。
- 不执行真实 Provider 请求。
- 只保留当前接口，不增加 alias、warning 或 compatibility shim。
- 七个既有 untracked 路径属于用户，不移动、不删除、不 stage。

## 执行不变量

1. 每个提交只 stage 本 Task allowlist。
2. Provider caller、adapter、clients、bench/live wrappers 必须在一个原子提交中切换。
3. 正常 runtime、diagnostics、benchmarks 和 live E2E 只使用一个 resolver，不写全局
   `os.environ`。
4. exact-root `.env` 的旧 generic key 迁移只触碰私有文件，备份在仓库外，不提交 value。
5. TOML 只由 stdlib `tomllib` 解析，每个 Pico 实例一次。
6. 每个 Task 运行 focused pytest、touched Ruff 和 `git diff --check`。

## Task 0：Preflight 与私有 `.env` rename

**不产生代码提交。**

- 验证 HEAD、tracked clean、七个 protected untracked、Plan 1 manifest 无漂移。
- exact-root `.env` 只输出 key set，不输出 value。
- 只在旧 generic key 存在且新 key 不存在，或二者 value 相同时迁移；不同则停止。
- 使用 `.pico/project-env.lock`、private reader、仓库外 `0700/0600` backup 和 atomic writer。
- 完成后只允许 `PICO_API_KEY`；三个 Provider 专属 key可继续存在。

验证：status=`loaded`、旧 key absence、新 key presence、mode `0600`、`nlink=1`、stdout/stderr
无 secret value。

## Task 1：共享 Provider resolver

**Allowlist：**

- `pico/config.py`
- `pico/providers/defaults.py`
- `pico/cli.py`
- `pico/cli_commands.py`
- `pico/cli_diagnostics.py`
- `pico/runtime.py`
- `pico/evaluation/provider_benchmark.py`
- `benchmarks/live_e2e/run_live_session.py`
- 对应 config/diagnostics/benchmark/live tests

实现：

- 在 `pico.config` 增加返回 plain dict 的共享解析函数；不增加 class/registry/Protocol。
- 值优先级精确为 explicit → project env → process env → default。
- 每个来源内部按 Provider names 顺序查找，API key 表精确为：
  - OpenAI：`PICO_OPENAI_API_KEY` → `OPENAI_API_KEY` → `PICO_API_KEY`
  - Anthropic：`PICO_ANTHROPIC_API_KEY` → `ANTHROPIC_API_KEY` → `PICO_API_KEY`
  - DeepSeek：`PICO_DEEPSEEK_API_KEY` → `DEEPSEEK_API_KEY` → `PICO_API_KEY`
  - Ollama：无 key
- 删除旧 generic names 和 OpenAI/Anthropic 互相 fallback。
- 删除生产 `load_project_env` 调用；project env snapshot 和 redaction snapshot 显式下传。
- runtime、config show、doctor、provider benchmark、live E2E 对同一输入返回相同
  value/source/name。

Focused：resolver source matrix、cross-provider rejection、worktree exact-root、redaction、runtime/
doctor/benchmark/live parity。提交：`refactor(config): centralize provider resolution`。

## Task 2：TOML parse-once snapshot

**Allowlist：**

- `pico/config.py`
- `pico/runtime.py`
- `pico/context_manager.py`
- config/context/TOML tests

实现：

- 删除 `_parse_scalar`、手写 parser、`load_pico_toml_full` fallback 和 Python <3.11 分支。
- 一个 `tomllib` loader 读取一次，验证 `policy/context/memory` 当前字段并返回完整 plain dict。
- missing 使用 defaults；syntax/top-level 错误发固定无内容 warning，整文件 defaults；字段错误
  只回退该字段。
- `Pico.__init__` 只加载一次 snapshot，再传给现有消费者；不加 watcher/cache/config class。

Focused：read-count=1、missing、malformed、non-table、field type/range、secret-shaped malformed
line不出现在 warning。提交：`refactor(config): parse project toml once`。

## Task 3：Provider/Context 原子硬切

**单 owner、单原子提交。Allowlist：**

- `pico/agent_loop.py`、`pico/context_manager.py`、`pico/runtime.py`
- `pico/checkpoint.py`、`pico/context/renderer.py`、`pico/context/sources.py`
- `pico/providers/_shared.py`、`anthropic_compatible.py`、`openai_compatible.py`、`ollama.py`
- 新 `pico/providers/fake.py`、`pico/providers/text_protocol_adapter.py`
- 删除 `pico/providers/fallback_adapter.py`
- `pico/cli.py`
- evaluation、memory-quality、live E2E 和 perf benchmark callers
- Provider/Context/AgentLoop/e2e/benchmark tests

实现：

- AgentLoop 唯一调用 structured `client.complete(system, tools, messages, max_tokens,
  cache_breakpoints)`，返回 `Response`。
- Anthropic/DeepSeek 直接 structured `complete`；合并旧 text 路径已有的 header validation、
  retries 和稳定错误封装。
- OpenAI/Ollama 只保留 `complete_text(prompt, max_tokens)`；显式装配
  `TextProtocolAdapter`。删除 Runtime `hasattr` 自动包装。
- Fake structured `complete` 放入 `pico.providers.fake`。
- 删除全部 `complete_v2`、streaming、`supports_native_tools`；OpenAI 删除请求侧 cache 参数，
  保留 response cached-token usage 和非 streaming SSE response parser。
- 重命名 `build_v2→build_request`、`_count_tokens_for_v2→count_tokens`、
  `system_cache_key→system_prefix_hash`、perf benchmark 文件与 case 名。
- Adapter 接受但明确忽略 `cache_breakpoints`，不透传 text transport。

Focused：Anthropic payload/cache/retry、OpenAI/Ollama text-only contract、adapter parity、Fake、
injection、canonical messages、full-turn、live offline 60。提交：
`refactor(provider): hard cut structured completion surface`。

## Task 4：删除 facades 并收窄 Python API

**Allowlist：**

- `pico/__init__.py`、`pico/runtime.py`
- `pico/providers/__init__.py`、`pico/memory/__init__.py`、`pico/evaluation/__init__.py`
- 删除 `pico/providers/clients.py`
- 删除 evaluation `metrics.py`、`metrics_experiments.py`、`evaluator.py`
- `pico/cli.py`、`pico/cli_commands.py`
- 生产、tests、benchmarks、scripts 的真实模块 imports
- 删除 `test_p1_smoke.py`、`test_p2_smoke.py`、`test_p3_smoke.py`

实现：

- 顶层 `__all__` 精确七项：`Pico`、`SessionStore`、`WorkspaceContext`、`main`、
  `build_agent`、`build_arg_parser`、`build_welcome`。
- `SessionStore` 只从 `pico.session_store` 导入；Fake 只从真实模块导入。
- package `__init__.py` 只作 marker。
- 删除 CLI tests-only handler 和 `HELP_DETAILS` reexports。
- 阶段名 tests 把唯一行为断言合并到职责测试后删除/重命名。

Focused：exact `__all__`、deleted module import failure、scripts help、evaluation behavior。
提交：`refactor(api): remove package facades`。

## Task 5：唯一 CLI 与显式运行面

**Allowlist：**

- `pyproject.toml`
- `pico/cli.py`、`pico/cli_parser.py`、`pico/cli_commands.py`
- `pico/cli_diagnostics.py`、`pico/cli_memory.py`、`pico/cli_recovery.py`、`pico/cli_session.py`
- `pico/memory/block_store.py`
- `README.md`、`CONTEXT.md`
- CLI/public API/usage tests

实现：

- `[project.scripts]` 只保留 `pico = "pico.cli:main"`。
- `pico` 显示 root help；`pico run`、`pico repl`、`python -m pico` 为唯一运行方式。
- 删除 bare prompt dispatch 和 compatibility 文案；inspection/recovery namespaces 保留。
- 所有当前 usage/help/hint 从 `pico-cli` 改为 `pico`。
- 激活 venv 后 `command -v pico` 必须指向该环境。

Focused：root help、run、repl、module、inspection commands、unknown/bare prompt、pyproject exact
entry。提交：`refactor(cli): hard cut explicit pico command`。

## Task 6：Plan 2 完成门禁

Structural scans：

```bash
test -z "$(rg -n 'complete_v2|build_v2|_count_tokens_for_v2|FallbackAdapter|stream_complete|supports_native_tools' pico tests benchmarks scripts || true)"
test -z "$(rg -n 'PICO_RIGHT_CODES_API_KEY|RIGHT_CODES_API_KEY' pico tests benchmarks scripts .env.example || true)"
test -z "$(rg -n 'pico-cli' pico tests benchmarks scripts README.md CONTEXT.md pyproject.toml || true)"
test -z "$(rg -n 'providers\.clients|evaluation\.(metrics|metrics_experiments|evaluator)' pico tests benchmarks scripts || true)"
```

Full gate：

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build
git diff --check
```

再验证 wheel 环境的 `command -v pico`、`pico --help` 与 `python -m pico --help`。推送
`memory`，等待真实 Ubuntu 3.11/3.12 CI。不得运行 Provider benchmark 或真实 E2E。

## Handoff

报告各提交 SHA、`.env` backup path（不含 value）、focused/full/offline/build/CI 结果、七个
protected untracked 原样状态和所有 deviation。Plan 2 全绿并复核实际树后，才写 Plan 3。
