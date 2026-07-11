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
uv run python benchmarks/perf/bench_request_build.py
uv run python benchmarks/perf/bench_retrieval.py
uv run python benchmarks/perf/bench_recall.py
uv run python benchmarks/perf/bench_security_recovery.py
```

## 真实 Provider 授权边界

真实 DeepSeek 或 Anthropic E2E 会产生网络请求、token 消耗和费用。只有最终本地全量、build、双平台 CI
与独立 review 都通过后，才能取得一次新的明确授权并运行一个 Provider；旧授权不能复用，也不运行
Provider matrix。

证据只记录 Provider/model、commit、命令、assertion/call/token caps、耗时和 redacted summary。不得提交
prompt、answer、key、header、request URL、response body 或 live JSON。执行后还必须验证 fixture 恢复、
private mode、active artifact、session terminalization 与 secret absence。

最终本地计数、CI run ID 和独立 review 结论在完成时更新本文；没有 exact HEAD 证据的项目保持未完成。
