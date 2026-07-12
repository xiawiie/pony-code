# Pico 验证与证据

本文给出最终 HEAD 可重建的离线门禁。命令默认从仓库根目录执行，临时 benchmark/build 输出不提交。

## 本地全量

```bash
uv lock --check
uv sync --frozen --dev
uv run ruff check .
uv run pytest -q
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
```

复杂度 ratchet 使用：

```bash
uv run ruff check pico --select C901 --output-format json
```

目标是 `ToolExecutor.execute` 与 `AgentLoop.run` 均无 C901 finding，并保持仓库与文件级基线不回退。

## 构建与 clean install

```bash
uv build --clear
uv run python scripts/verify_distribution.py --install-smoke
```

verifier 用 `git ls-files pico` 冻结 package 清单，精确检查 sdist/wheel roots、wheel METADATA、唯一 console
entry 与零 runtime dependency；随后在临时 HOME/cwd/venv 中 `pip --no-deps` 安装 wheel，检查
`command -v pico`、`pico --help` 和 `pico doctor --offline`。

## 平台门禁

GitHub Actions 在 push 到 `main`、`memory` 和所有 pull request 上运行：

- Ubuntu latest / Python 3.11：lint、全量 pytest、offline live assertions；
- Ubuntu latest / Python 3.12：同上，并执行 build/clean-install smoke；
- macOS latest / Python 3.12：project env、文件锁/私有路径/artifact、safe subprocess/shell、recovery
  durability 与 Memory reader/append lock focused tests。

macOS FIFO 探针显式使用 spawn，并以 `-W error::DeprecationWarning` 运行两个真实 FIFO 参数用例；不使用
warning filter 或平台 blanket skip。最终证据必须引用 exact HEAD 的实际 CI run，不能由本地结果推断。

## 离线 benchmarks

Memory quality 使用 Fake Provider，workspace 写到临时目录：

```bash
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format text
```

性能 harness 分项运行，输出只保存在终端或临时目录：

```bash
uv run python -m benchmarks.perf.bench_request_build
uv run python -m benchmarks.perf.bench_retrieval
uv run python -m benchmarks.perf.bench_recall
uv run python -m benchmarks.perf.bench_security_recovery
```

## 真实 Provider 授权边界

真实 DeepSeek 或 Anthropic E2E 会产生网络请求、token 消耗和费用。只有最终本地全量、build、双平台 CI
与独立 review 都通过后，才能取得一次新的明确授权并运行一个 Provider；旧授权不能复用，也不运行
Provider matrix。

Live report format v2 只记录 Provider/model、exact Git SHA、固定 caps、每 turn 的行为标签与计数、
assertion name/gate/boolean、usage totals、墙钟时间和固定错误码。不得记录 prompt、answer、raw error、
assertion raw actual、key、header、request URL 或 response body。fixture 退出并验证恢复后才能写最终报告。

四个 gate 必须独立展示：Behavior、Transport/Cost、Credential/Artifact Security、Persistence/Fixture。
只有四者均为 pass 才能称为“全量通过”。Transport 行应显示 `model attempts N (cap 15)`、HTTP attempts 与
retries；cap 是上限，不是通过分母。retry 或 billing ambiguity 为 degraded；证据/usage 缺失或 cap 超限
为 fail，两者都使 `overall_pass=false`。

Live CLI 使用 `--max-model-attempts`、`--request-timeout-seconds` 和 `--max-wall-seconds`。前者限制逻辑
Model Attempt，request timeout 作用于单个 HTTP 请求，wall cap 只在 turn 边界观测。Ollama 只有 `/api/tags`
可达且配置模型已安装时才进入 live；否则为 `not_configured`，不启动服务、不拉取模型、也不发送生成请求。

## 历史基线证据

硬切前源码基线 `5f359bd18fb3a59968167bfe0196352d41a23a01` 的可重建结果是：本地
`1997 passed, 6 skipped`，offline assertions `60 passed`；wheel/sdist 可构建但 sdist 携带完整 tests，
macOS 全量有两条后台线程 `fork()` warning。此前单次获授权 DeepSeek E2E 为 `43/43` assertions、
`10/15` Provider calls、13,842 input tokens、1,330 output tokens、5,248 cache-read tokens、44.253 秒；
该授权与结果不用于最终 E2E。

上一阶段实现证据 commit `ffc5a60ce91885038264c0cfc4185e13c66a19a3`（不代表当前 Provider v2）：

- 本地 Python 3.12：Ruff 通过，`2021 passed, 6 skipped`，offline assertions `66 passed`；
- macOS warning-as-error focused：显式 FIFO `2 passed`，完整 focused `453 passed`；
- Memory quality Fake benchmark：`8/8`；四组 perf harness 均成功；
- C901：全仓 60 个 finding；`ToolExecutor.execute` 与 `AgentLoop.run` 均无 finding；
- wheel/sdist 精确归档检查、METADATA、零 runtime dependency、隔离 venv 安装、CLI/doctor smoke 全部通过；
- GitHub Actions run [29167571366](https://github.com/xiawiie/pico/actions/runs/29167571366)：Ubuntu
  Python 3.11/3.12 均为 `2021 passed, 6 skipped` 且 offline `66 passed`，Python 3.12 build/clean-install
  成功；macOS Python 3.12 为 FIFO `2 passed` 与 focused `453 passed`。

每次新交付仍必须给出交付 commit 的 exact-SHA CI run 和独立 review 结论。真实 Provider live 证据不得从
历史 run 推断；必须在当前离线门禁、build 和 review 通过后取得新的明确授权，并对目标 exact SHA 单独运行。
