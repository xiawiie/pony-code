# Pico v0.2.0 验证与发布证据

本文定义本地稳定版最终 exact HEAD 的验证真源。历史 commit、历史 Provider live、旧 benchmark 或 workflow
定义本身都不是当前证据。所有命令默认从干净仓库根目录运行，临时 build/benchmark artifact 不提交。

## 必跑命令

```bash
uv lock --check
uv run ruff check .
uv run pytest -q
uv build --clear
uv run python scripts/verify_distribution.py --install-smoke
uv run python scripts/evaluate.py --suite core-fast
uv run python scripts/evaluate.py --suite core-functional
uv run python scripts/evaluate.py --suite sandbox-contract
uv run python scripts/evaluate.py --suite sandbox-real
uv run python -m benchmarks.perf.bench_sandbox
uv run python -m benchmarks.perf.bench_sandbox --real
git diff --check
```

发布标准是零失败、零未解释 skip/xfail/xpass。删除 legacy SRT tests 后测试总数下降是预期行为；每次发布在最终
artifact 中冻结 exact HEAD 的实际 count，不能用旧数量作为目标。

## Distribution gate

`uv build --clear` 后 `dist/` 必须且只能存在一个与 `pyproject.toml` 版本匹配的 wheel，以及一个匹配的 sdist。
verifier 从 Git index 和 `pyproject.toml` runtime package 真源计算 archive 清单，检查：

- wheel/sdist roots、METADATA、唯一 `pico` console entry、零 runtime dependency；
- package manifest 和 Docker config resource 的 exact 内容；
- wheel 不包含 `pico.sandbox*` legacy runtime、`pico._sandbox_toolchain` 或 `pico.evaluation`；
- 隔离 venv、隔离 HOME、空 cwd、无源码 `PYTHONPATH` 的 `pip --no-deps` clean-install；
- installed `pico --help`、`doctor --offline`、`sandbox status/prepare` 的只读、零网络/零隐式修复合同。

没有 exact local image、宿主不是 macOS arm64或 Docker 不满足要求时，inspection/smoke 必须以稳定 reason code
fail closed；不能 pull image、修改用户 Docker config 或把 Host fallback 当成通过。

## 统一评测入口

| Suite | 内容与边界 |
| --- | --- |
| `core-fast` | Ruff、Context snapshot/budget、Tool/shell security focused contracts。 |
| `core-functional` | 完整功能、build 和 distribution 编排，不比较 machine-specific baseline。 |
| `core-full` | 在 functional 上增加同 machine committed performance comparison。 |
| `sandbox-contract` | Session/Runner/Runtime/CLI/Apply contracts；不冒充真实 Docker execution。 |
| `sandbox-real` | clean wheel + isolated install + production Docker owner 的真实 vertical。 |
| `live` | exact-HEAD Provider live；显式 Provider、网络和费用授权必需。 |

`core` 仅是 `core-full` alias；`sandbox` 仅按顺序编排 contract 与 real，不是第二套实现。每次 suite 输出到
`artifacts/eval/<timestamp>-<suite>.json/.md`，只保留稳定 scenario ID、status、exit code、duration、低敏感
provenance 和性能统计；不得保存 prompt、completion、tool result、credential 或 Host 绝对路径。

## 必测安全场景

| 领域 | 必须验证 |
| --- | --- |
| Memory | 无授权、否定句、历史授权、delegate、显式中英授权、secret rejection、零副作用 |
| Context | 多段 source、伪 marker、drop priority、retry reuse、Provider payload exact match、闭合标签 |
| Workspace I/O | symlink/hardlink/FIFO/device、父目录/root/target交换、超限、Patch CAS、外部零读写 |
| Provider | official 默认、任意显式第三方、来源优先级、无凭证泄漏、禁止 redirect |
| Staging | 128 MiB、5000 文件、跨 chunk secret、env template 超限、中途变化、mode/目录交换、清理 |
| Capture | no-op、新增/删除/chmod、mtime恢复、inode更换、resume、cache失效、final full capture |
| Watchdog | 长/短命令、快速超限、特殊文件、最终 scan、container cleanup |
| Apply | 展示与使用同一 digest、确认后 source/staging 漂移、rollback/reconcile、`--yes` |
| Packaging | 无 legacy SRT、无 runtime evaluation、CLI/doctor/status/prepare、零 runtime dependency |

Workspace TOCTOU tests 必须在最终 open 前交换 file、parent 和 root，断言 workspace 外 canary 零读取、零写入、
runner 不执行且 reason code 稳定。Incremental capture tests 必须证明无法安全复用时自动回全量，最终完整 capture
仍能发现调用期 cache 未观察到的持久变化。

## 性能协议

- 普通场景：5 次 warmup、20 次 measured，报告 median/p95。
- 5000 文件和 128 MiB：1 次 warmup、5 次 measured。
- 固定同一机器、Python、Docker、exact image 和代表性仓库。
- artifact 记录 exact commit、dirty state、机器、OS、Python、Docker、image digest 和参数。
- 首次超限只可在确认机器空闲后完整重跑一次；第二次仍超限即 `NO-GO`，不能修改阈值掩盖回退。

本机回归阈值：

| 场景 | 门槛 |
| --- | ---: |
| 128 MiB staging Python 额外峰值 | ≤ 32 MiB |
| 5000×1 KiB staging median | ≤ 6.34 s |
| 326 文件 no-op shell observed median | ≤ 1.40 s |
| 326 文件 no-op shell observed p95 | ≤ 1.60 s |
| 5000 文件 watchdog | ≤ 单核 10% |
| watchdog 违规检测 | ≤ 2 s |
| status/prepare/context/baseline/finalize/apply | 各自 median 回退 < 10% |

这些是同机基线，不是跨机器 SLA。`bench_sandbox` 的非 real 模式不能代替真实 container vertical。

## GitHub Actions

Workflow 中所有 third-party actions 必须固定到 immutable commit SHA，并在同一行保留 tag 注释。当前 CI：

- Ubuntu Python 3.11/3.12：lint、全量/aggregate tests、offline live assertions、build/install 和 report-only perf；
- macOS Python 3.12：security/durability/Sandbox contracts，以及隔离 HOME 的 status/prepare 零 mutation gate；
- Ubuntu Docker status：只报告 unsupported/fail-closed capability，不构成 Linux release evidence。

CI 不 pull managed image，也不把 status、contract 或 Linux report-only job 称为 real Sandbox gate。最终交付必须引用
exact HEAD 的实际 CI run；本地结果不能推断远端通过。

## Release Gates

| Gate | 必须满足 |
| --- | --- |
| G0 Authority | exact HEAD、干净 tracked tree、版本、CHANGELOG、文档真源一致 |
| G1 Static | lock、Ruff、diff-check 全绿 |
| G2 Functional | focused security、全量 pytest、offline assertions 全绿且无未解释 skip |
| G3 Distribution | wheel/sdist、archive contract、isolated clean-install 全绿 |
| G4 P0 Security | workspace escape=0、unauthorized memory write=0、snapshot exact、Provider route correct |
| G5 Sandbox Real | Apply 前 source 不变、host fallback=0、cleanup residue=0 |
| G6 Performance | 达到 staging memory、shell latency 和 watchdog 阈值 |
| G7 Provider Live | 默认 DeepSeek exact-HEAD live 全绿；其他 Provider 只按各自 exact-HEAD 证据声明 |
| G8 Documentation | README、安全、恢复、Provider、平台、迁移和 CLI help 与实现一致 |

任一 mandatory Gate 失败时，发布保持 `NO-GO`，不得创建 `v0.2.0` stable tag。

## Provider live 授权

DeepSeek/OpenAI/Anthropic live 会产生网络请求、token 消耗和费用。必须在最终离线门禁、build 和 review 完成后，
针对 exact HEAD 获得新的明确费用授权；旧授权不能复用。缺少 credential 或费用授权本身就是 G7 blocker，不得用
offline assertions 或其他 Provider 结果替代。

Live report 只记录 Provider/model、exact SHA、固定 caps、行为标签与计数、assertion boolean、usage totals、wall
time 和固定 error code。不得记录 prompt、answer、raw error、key、header、request URL 或 response body。Behavior、
Transport/Cost、Credential/Artifact Security、Persistence/Fixture 四组必须分别 pass；retry/billing ambiguity 是
degraded，证据缺失或 cap 超限是 fail。

Ollama 只有 loopback `/api/tags` 可达且目标模型已安装时才进入 live；不启动服务、不拉模型。OpenAI、Anthropic 和
Ollama 只有各自 exact-HEAD live 通过后才进入 stable 支持矩阵。
