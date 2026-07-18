# Pony 1.0 验证与发布

发布证据只对 exact Git HEAD 有效。旧 commit、dirty worktree 或另一版本 wheel 的结果不能继承。本文件区分可重复的离线
产品门禁、依赖宿主的 Docker 实机验收，以及会产生费用的 Provider live 验收。

## 一键离线门禁

```bash
./scripts/check.sh
```

该脚本依次验证 lock、Ruff、全量 pytest、core-functional evaluation、offline live-harness assertions、sdist/wheel、
archive 精确内容和两个隔离 clean-install smoke。任何一步失败都停止。

## 门禁矩阵

| Gate | 类型 | 必须结果 | 主要证据 |
| --- | --- | --- | --- |
| G0 Source | 离线 | clean exact HEAD、版本/lock/CHANGELOG 一致 | Git、repository structure tests |
| G1 Static | 离线 | Ruff 零错误 | `uv run ruff check .` |
| G2 Functional | 离线 | 全量 pytest 零失败，裸/显式 TUI 入口一致 | `uv run pytest -q` |
| G3 Security | 离线 | escape/secret/approval/recovery 专项零失败 | security/durability test groups |
| G4 Evaluation | 离线 | core-functional 与 offline assertions 通过 | evaluation scripts |
| G5 Distribution | 离线 | sdist/wheel 精确内容、metadata、License | distribution verifier |
| G6 Clean install | 离线 | 新虚拟环境中 CLI/version/doctor/resource smoke 通过 | install smoke |
| G7 Sandbox real | 条件 | 受支持机器上 exact image vertical 通过 | local runtime verifier |
| G8 Provider live | 条件/收费 | 每个声明可用的账号/模型完成最小 probe | `doctor --check-api` 或 bounded harness |
| G9 Documentation | 离线 | 路径、命令、Provider 表与实现一致 | structure/docs tests |

G0-G6 与 G9 是 package 发布 mandatory gate。G7 只在受支持 Docker 环境存在时执行；环境缺失不能伪报通过。G8 需要
用户拥有的账号、Key 和费用授权，是部署/Provider 组合验收；离线 wire-contract tests 必须始终通过，但不能冒充真实账号结果。

## 聚焦测试

### Provider 与 `.env`

```bash
uv run pytest -q \
  tests/test_config.py \
  tests/test_cli_commands.py \
  tests/test_cli_diagnostics.py \
  tests/test_provider_clients.py \
  tests/test_provider_anthropic.py \
  tests/test_provider_openai_chat_completions.py \
  tests/test_provider_response.py \
  tests/test_provider_probe.py
```

必须覆盖三 Provider、四 Transport、项目环境优先级、Ollama 无 Key、非法组合 fail closed、API 路径、认证 header、
tool call/result continuation、stop reason 与 usage。

### 安全与耐久性

```bash
uv run pytest -q \
  tests/test_project_env_security.py \
  tests/test_private_paths.py \
  tests/test_workspace_io_security.py \
  tests/test_shell_execution_security.py \
  tests/test_shell_security_corpus.py \
  tests/test_secret_boundaries.py \
  tests/test_checkpoint_store_durability.py \
  tests/test_recovery_durability_e2e.py \
  tests/test_recovery_journal.py
```

macOS CI 额外以 `-W error::DeprecationWarning` 运行关键安全组，确保 spawn/subprocess 行为没有平台警告退化。

### Sandbox

```bash
uv run python scripts/evaluation/evaluate.py --suite sandbox-contract
uv run pytest -q tests/test_sandbox_identity.py tests/test_sandbox_apply.py
```

完整本地实机步骤见[本地 Sandbox](local-stable-execution.md)。

### CLI 与 TUI

```bash
uv run pytest -q \
  tests/test_cli_parser.py \
  tests/test_cli_commands.py \
  tests/test_cli_error_envelope.py \
  tests/test_cli_workflow.py \
  tests/tui
```

必须覆盖裸 `pony` 与 `pony repl` 的同一分派、`pony run` 纯结果输出、未知命令建议、TTY/`TERM=dumb`/窄终端
fallback、`NO_COLOR`、40/80/120 列的单行 `PONY CODE · version` 与精简 footer、五项 slash completion、六行输入、
换行/中断，以及中文、英文、emoji、标题、列表、代码块、表格降级、非法 Markdown 和控制字符清理。

事件投影测试必须证明 `Working…` 会在正式输出前清除、自动 checkpoint 零输出、成功 Tool 只输出一行、失败与中断
可见，并且 footer 不泄露绝对路径、Session ID、API Base 或 checkpoint ID。runtime hook 恢复、durable trace 顺序、
approval 参数脱敏与 approval fail closed 仍是阻断项；离线 contract 不得描述为 Provider reasoning 或 streaming 验证。

Workflow P0 还必须覆盖：Session v3 默认值；v1/v2 inspection 零写与 crash-safe explicit migration；Plan 全部
schema/byte/secret 边界；Mode x approval x read-only x shell；模型可见 schemas 与 Executor 双重约束；
fork/rewind/reset/clone；`--mode` one-shot；plain/TUI Resume；active prompt history；以及
Session pair commit -> Plan digest reload -> durable trace -> listener 顺序。聚焦入口包括：

```bash
uv run pytest -q \
  tests/test_workflow_state.py \
  tests/test_workflow_policy.py \
  tests/test_runtime_resume.py \
  tests/test_cli_session_inspect.py \
  tests/test_cli_workflow.py
```

## Distribution 验证

```bash
uv build --clear
uv run python scripts/release/verify_distribution.py \
  --install-smoke \
  --offline-bundle-smoke
```

Verifier 使用 `git ls-files pony` 建立产品文件真源并检查：

- sdist 单一 wrapper、无 link/special file；
- wheel/sdist 无 tests、benchmarks、scripts、docs、`.github` 或 development evaluation；
- wheel 包含全部 `pony/**` 与两个 Sandbox JSON 资源；
- Name、Version、Summary、Python 要求、MIT、Project URLs、README 与 console entry 正确；
- Runtime `Requires-Dist` 精确为 `prompt-toolkit>=3.0.52,<4`，wheel 为 `py3-none-any`；
- clean venv 从锁定 uv cache 离线解析 prompt-toolkit/wcwidth，安装后 TUI 可导入，且 `pony --version`、help、doctor、
  Sandbox status/prepare 和资源 digest 正确；
- smoke 环境不继承 `PONY_*`、厂商 Key、`PYTHONHOME` 或 `PYTHONPATH`。

## Provider live

真实 API 会产生网络请求、token 消耗和费用。只有用户明确授权后执行：

```bash
pony doctor --check-api
```

Probe 验证最小文本响应、native tool call 与 tool result 续接。它使用当前 `.env` 的 exact Provider/model/URL/variant/auth，
不做 fallback。维护者 live harness 还必须设置 model-attempt、request-timeout、token 与 wall-time cap。

Live report 不应保存 prompt、answer、raw response、Key、header 或完整 URL；只记录 Provider、模型、exact SHA、固定 caps、
行为标签、计数、usage、wall time 和稳定错误码。账号错误、配额、模型不可用与协议失败应明确区分。

## 版本晋级

候选版使用 `1.0.0rc1`：

1. 更新 `pyproject.toml` 与 `uv.lock`；
2. 运行完整离线门禁与适用的实机/live 验收；
3. 修复所有发布阻断；
4. 将版本晋级为 `1.0.0`，更新 CHANGELOG；
5. 在最终 exact HEAD 再次运行完整门禁。

## Tag 发布

`.github/workflows/release.yml` 只响应 `v*` tag，并要求 tag 精确等于 `v<project.version>`。工作流在全新 runner 中重复
静态、功能、评估、构建和 clean-install 门禁；随后使用 GitHub OIDC / PyPI Trusted Publishing 上传 wheel 与 sdist，
生成 SHA-256 文件并创建 GitHub Release。

发布前外部一次性配置：

- PyPI 项目 Trusted Publisher 绑定 repository、workflow `release.yml` 与 environment `pypi`；
- GitHub environment `pypi` 配置必要的保护规则；
- branch/tag protection 与维护者 review 生效。

发布工作流不存储长期 PyPI token。创建 tag、push 或发布到外部服务仍是单独的维护者授权动作。

## NO-GO 条件

- 任一 mandatory gate 失败或出现未解释 skip；
- tracked tree dirty、tag/version/lock/CHANGELOG 不一致；
- archive 多出开发文件或缺少产品文件；
- security failure 被普通功能测试掩盖；
- 用离线 contract 冒充真实 Docker/Provider 结果；
- live 证据包含凭证或私有内容；
- 发布产物不是 exact tag workflow 构建的同一组文件。
