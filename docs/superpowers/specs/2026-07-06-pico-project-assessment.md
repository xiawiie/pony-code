# Pico 项目评估报告 · 2026-07-06

- 视角：项目自身目标对齐度
- 方法：CONTEXT.md 术语表 + 39 份 ADR + README + docs/architecture + docs/memory-model 抽取承诺 → 代码锚定 → 测试锁死 → 反例扫描 → 漂移检查
- 评价语言：兑现 / 部分兑现 / 未兑现（漂移），不打分
- 说明：CONTEXT.md 的英文术语作为项目专有语言，报告中保留原名以便追溯；代码符号、文件路径、提交哈希、测试函数名也保留原文。其余全部使用中文。

## 摘要

Pico 目前是一个已经能跑通全部承诺主链的本地 coding-agent harness。它自己写下的五大方向（Harness / Recoverable Editing / Safe Execution / CLI Surface / 记忆系统）在代码里都能找到成建制的实现入口，并且被相当密集的测试锁死（`./scripts/check.sh` 通过：ruff 无警告，452 个测试全绿，耗时 72 秒）。它明确定位在“第一阶段垂直切片”这个刻度上——不做 OS 级 sandbox、不做自动垃圾回收、不做 hunk 级 restore、不做全量 workspace 快照——这些“明确不做”在 ADR 里显式写死，代码里也没有偷偷做。

初评在 2026-07-06 首轮发现两条边界性弱项（User Notes 只读不是路径级、`pico.toml` override 通道未打通），同日进行的修复循环已将两条闭合并加了 3 条测试锁死（见文末“本轮修复记录”）。当前唯一系统性弱项是 Recovery Review：作为词条被承诺成一个决策阶段，但代码里只是一个状态字符串（`"review"`）加一段说明文本，没有独立的组件边界。

## 评估基线

- 源码：66 个 Python 文件（pico/*.py 顶层 40 个 + memory / features / providers / evaluation 子包），顶层约 7908 行
- 测试：55 个测试文件（tests/test_*.py 43 个 + memory 子目录 + fixtures）
- ADR：39 份，编号 0001–0039，全部有内容，包含 Considered Options 或 Consequences
- 文档：`CONTEXT.md`（43 条术语表）、`README.md`（312 行）、`docs/architecture/agent-harness-v1-overview.md`、`docs/memory-model.md`、`docs/cli-installation-and-updates.md`、`docs/review-pack/`、`docs/superpowers/specs/`
- 分支：`memory`，评估当天有 8 个未提交改动（`M docs/review-pack/{README,dashboard}.md pico/config.py pico/providers/{_shared,anthropic_compatible,openai_compatible}.py tests/test_{provider_clients,safety_invariants}.py`）；本评估不纳入未提交改动，只评估已提交的状态
- `./scripts/check.sh` = `uv run ruff check .` + `uv run pytest -q` → **通过**（452 tests / 72.62 秒 / 0 失败）
- 近期重构主题：拆 provider client（0161f07、46c954a、3c8e1ca、fb699fd）；拆 CLI 集群（e55a9d8、185b8eb、d0f6e86、3306506、16f2e40）；拆 test 集群（b0bd30d、eebb671、48714ae）；拆 evaluator（04e33c5、eb3f197、fb728c5、90db0ae）

## 维度 1 — Harness 边界完整性

### 期望清单

- E1.1 有明确的运行时流程：build context → session record → task state → prompt → model → parse → tool exec → 写 task_state / trace / report
- E1.2 三类运行产物各司其职（task_state.json / trace.jsonl / report.json）
- E1.3 三类存储分离：`.pico/runs`、`.pico/sessions`、`.pico/checkpoints`（ADR-0021）
- E1.4 五大 harness boundary 拆分（ADR-0020）
- E1.5 Trace 与 Checkpoint 分离（ADR-0009）
- E1.6 敏感环境变量脱敏

### 逐条判定

- **E1.1 兑现** — `pico/agent_loop.py:16 AgentLoop.run`、`pico/runtime.py:69 Pico`、`pico/context_manager.py`、`pico/tool_executor.py:103 ToolExecutor.execute`、`pico/model_output_parser.py`；`docs/architecture/agent-harness-v1-overview.md` 里的 8 步流程与代码一一对应
- **E1.2 兑现** — `task_state.json` 走 `pico/task_state.py`；`trace.jsonl` 由 `pico/runtime.py:400 emit_trace` 写；`report.json` 由 `pico/runtime.py:559 build_report` 写；三者各司其职由 `test_runtime_report.py`、`test_task_state.py`、`test_run_store.py` 锁死
- **E1.3 兑现** — `pico/run_store.py`、`pico/session_store.py`、`pico/checkpoint_store.py:21 CheckpointStore` 三个独立类；`.pico/checkpoints/{records,tool_changes,blobs}/` 目录结构（`pico/checkpoint_store.py:31-34`）与 ADR-0021 完全对齐
- **E1.4 兑现** — 五大 boundary 都有独立类：`CheckpointStore`、`ToolChangeRecorder`（`pico/tool_change_recorder.py:21`）、`RecoveryManager`（`pico/recovery_manager.py:21`）、TraceTimeline（走 `runtime.emit_trace` + `.pico/runs/*/trace.jsonl`）、CommandPolicy（走 `pico/recovery_policy.py:133 command_risk_class` + `:439 evaluate_command_approval`）
- **E1.5 兑现** — trace 事件走 `trace.jsonl`（append-only），checkpoint 走 `records/*.json`（快照）；`test_runtime_report.py` 锁死 trace 事件序列，`test_checkpoint_store_phase1.py` 锁死 checkpoint 结构
- **E1.6 兑现** — `pico/security.py:23-120` 完整的敏感值脱敏层；`test_safety_invariants.py:272 test_configured_secret_env_names_are_redacted_in_trace_and_report` 是**强证据锁死**（既覆盖 trace 也覆盖 report）

### 维度状态描述

Harness 边界是 pico 目前**最扎实**的一层。五个边界（Checkpoint / ToolChange / Recovery / Trace / CommandPolicy）在代码里都有独立类或独立文件，互相通过 id 引用而非直接持有对象。ADR-0020 里那条“first slice split into five boundaries”的承诺在代码结构上兑现得非常清楚。

## 维度 2 — Recoverable Editing 兑现度

### 期望清单

- E2.1 checkpoint 存储走 `CheckpointStore` 边界（ADR-0001 / 0002）
- E2.2 checkpoint = record + blob 分离，blob 按 sha256 内容寻址（ADR-0003 / 0026 / 0027）
- E2.3 Turn Checkpoint 为用户面 restore 入口（ADR-0005）
- E2.4 Restore 默认预览，需要 `--apply` 才写盘（ADR-0006）
- E2.5 checkpoint 自动创建，restore 必须由用户发起（ADR-0007）
- E2.6 restore 之后写 Restore Checkpoint，不改历史（ADR-0010）
- E2.7 支持文件级 Selective Restore，不支持 hunk 级（ADR-0011）
- E2.8 Git 只作为 review context，不作为 restore engine（ADR-0012）
- E2.9 Delegated Change 归到 parent Turn Checkpoint（ADR-0013）
- E2.10 Pending Tool Change：预创建 → 完成时 finalize（ADR-0016）
- E2.11 Interrupted Tool Change：resume 时必须 review（ADR-0017）
- E2.12 checkpoint prune：由用户发起 + 先预览，无自动 GC（ADR-0018）
- E2.13 checkpoint 第一切片垂直贯通 8 项能力（ADR-0019）
- E2.14 checkpoint / tool_change 各有独立 `schema_version`（ADR-0023 / 0024）
- E2.15 文件条目 = 状态转移，而非 diff（ADR-0025）
- E2.16 路径存 workspace-relative，拒绝绝对路径与穿越（ADR-0028）
- E2.17 Restore Conflict 不自动合并，必须进入 Recovery Review（ADR-0029）
- E2.18 Affected Paths 而非全量 workspace 快照（ADR-0030）
- E2.19 Verification Evidence 挂在 checkpoint 上但不影响 restore 逻辑（ADR-0008 / 0037）
- E2.20 Snapshot Eligibility 保守（ADR-0004 / 0035）

### 逐条判定

- **E2.1 兑现** — `pico/checkpoint_store.py:21 CheckpointStore`；Recovery 通过 `RecoveryManager(store, ...)` 消费；两者是独立类，仅通过 record 交换（ADR-0002 兑现）
- **E2.2 兑现** — blob 按 sha256 前两位分桶：`pico/checkpoint_store.py:38-46`（`_blob_path`、`write_blob`）；`pico/recovery_paths.py:74 hash_bytes` 用 sha256 算哈希；`test_checkpoint_store_phase1.py:10 test_checkpoint_store_round_trips_records_tool_changes_and_blobs` 强证据
- **E2.3 兑现** — `pico/recovery_checkpoint_writer.py:33 create_turn_checkpoint`；CLI 层 `pico/cli_recovery.py:16-38` 只暴露 `preview-restore` / `restore` 接受 checkpoint_id，不暴露 tool_change_id；`test_recovery_e2e.py:36 test_single_user_request_creates_one_turn_checkpoint_for_multiple_tool_changes` 强证据
- **E2.4 兑现** — `pico/cli_recovery.py:30-38` 的 restore 分支：无 `--apply` 时调 `_preview_restore` 返回 plan；有 `--apply` 才调 `_apply_restore` 写盘；`test_recovery_cli.py:110 test_checkpoints_restore_without_apply_uses_preview_text` + `:123 test_checkpoints_restore_apply_changes_disk_state` 双向强证据
- **E2.5 兑现** — `pico/agent_loop.py:_create_resume_checkpoint` + `pico/runtime.py:415 create_checkpoint` 负责自动创建；restore 只能从 `pico-cli checkpoints restore --apply` 或程序化 `manager.apply_restore` 触发，未在 agent tool 里注册；`test_recovery_e2e.py:60 test_real_checkpoint_can_preview_and_apply_restore` 显式走用户面 API
- **E2.6 兑现** — `pico/recovery_manager.py:182 apply_restore` 末尾必调 `writer.create_restore_checkpoint`；`provenance` 携带 source_checkpoint、pre_restore_file_states、post_restore_file_states；`pico/recovery_models.py` 里 `checkpoint_type="restore"` 是独立类型
- **E2.7 兑现（在承诺范围内）** — `pico/recovery_manager.py:107` 内层 `if entry["decision"] != "restore": continue` 支持逐文件选择（可跳过部分条目）；hunk 级不支持（ADR-0011 明确排除）
- **E2.8 兑现** — `pico/workspace_observer.py:20 WorkspaceObserver.capture` 通过 `git status --porcelain`、`git ls-files -o` 等收集 review context，但不通过 `git checkout / reset / stash` 做 restore；写盘全部走 `pico/recovery_manager.py:210 _write_bytes_verified`（原子 temp + 哈希校验 + replace）
- **E2.9 兑现** — 代码中 21 处 delegate 引用；`tool_change` 里带 delegate 边界信息，但用户面 restore 仍从 parent turn 起，符合 ADR-0013
- **E2.10 兑现** — `pico/tool_change_recorder.py:26 start`（预创建）→ `:40 finalize`（成败与中断都要 finalize）；`test_tool_change_recorder.py:6 test_finalize_records_success_and_error_states` 强证据
- **E2.11 兑现** — `pico/tool_change_recorder.py:74 mark_interrupted_pending`；`test_tool_change_recorder.py:37 test_runtime_marks_existing_pending_tool_changes_interrupted_on_startup` 强证据
- **E2.12 兑现** — `pico/checkpoint_store.py:117 prune(dry_run=True, older_than=None, ...)` 默认 dry_run=True；CLI `pico/cli_recovery.py:39-52` 只有 `--apply` 才真的删；`test_checkpoint_store_phase1.py:45/59/73` 三条测试锁死；无任何后台 GC
- **E2.13 兑现** — 5 个 boundary + preview + restore + prune + verification 全部有代码有测试
- **E2.14 兑现** — `pico/recovery_models.py:14-17` 4 个独立 schema_version 常量；`pico/recovery_manager.py:33` load 时校验 `schema_version` 不符抛错
- **E2.15 兑现** — `pico/recovery_manager.py:_plan_entry` 只读 `before_blob_ref` / `after_blob_ref` / `expected_current_hash` / `change_kind`，不涉及 diff / patch；`test_recovery_manager.py:154 test_apply_restore_verifies_temp_write_before_replacing_target` 强证据
- **E2.16 兑现** — `pico/recovery_paths.py:15 normalize_workspace_relative_path` 拒绝绝对路径 / Windows 盘符 / `..` 穿越；`test_safety_invariants.py:30 test_workspace_escape_is_rejected` + `:39 test_symlink_path_traversal_is_rejected` + `test_recovery_paths.py:6 test_normalize_rejects_absolute_and_traversal_paths` 三重强证据
- **E2.17 兑现** — `pico/recovery_manager.py:87/91/93` 三处显式返回 `"conflict"`；`test_recovery_manager.py:8/34` 两条测试锁死；conflict 不会自动写盘
- **E2.18 兑现** — `pico/tool_executor.py:230 before_paths = _direct_tool_candidate_paths(...)` 只按工具输入推出的候选路径做 blob capture，不做全量 workspace 快照；`_capture_path_snapshot` 只对这一小组路径 snapshot
- **E2.19 兑现** — `pico/agent_loop.py:349 _record_pending_verification_evidence` + `pico/runtime.py:473 record_verification_evidence`；`test_recovery_e2e.py:100 test_verification_evidence_can_attach_to_checkpoint` + `:132 test_run_shell_verification_command_attaches_evidence_to_checkpoint` 强证据；restore 逻辑不读 verification 状态
- **E2.20 兑现** — `pico/recovery_policy.py:462 snapshot_eligibility` 保守判定：非常规文件 / 二进制 / 超过 1 MiB / 被忽略 / 越界都不 eligible；`test_recovery_policy.py:4 test_snapshot_eligibility_is_conservative` 强证据

### 维度状态描述

Recovery 是 pico 最“重”的一个子系统，也是文档承诺最密集的地方。20 条期望全部兑现，其中 15 条有测试强锁死。特别值得指出：`_write_bytes_verified`（`pico/recovery_manager.py:210`）在临时文件写完之后先算哈希再 replace，replace 之后再读回校验一次，任何一步不匹配都返回 `status="error"`——这是“半恢复不可能”的物理保证，不只是承诺。ADR-0029（不自动合并）在 `_plan_entry` 里被翻译成 3 个显式 conflict 分支。整体是“承诺与实现”高度对齐的证据链。

## 维度 3 — Safe Execution 边界

### 期望清单

- E3.1 首阶段不做 OS 级 sandbox，走策略式可审计路径（ADR-0014）
- E3.2 Command Risk Class 共 4 类：`read_only` / `workspace_write` / `destructive` / `external_effect`（ADR-0032）
- E3.3 Command Approval 由 risk class 驱动（ADR-0033）
- E3.4 用 risk class 而非纯白名单 / 黑名单作为主策略（ADR-0015）
- E3.5 三种审批模式：`ask` / `auto` / `never`（README）
- E3.6 Shell Side Effect 检测走 git-aware 的执行前后观察（ADR-0031）
- E3.7 内置默认策略 + `pico.toml` 轻量覆盖（ADR-0034）
- E3.8 verification 命令级证据（ADR-0037）
- E3.9 approval 决策必须写入审计记录

### 逐条判定

- **E3.1 兑现（在承诺范围内）** — 代码里没有 OS 级 sandbox（未调用 seatbelt / landlock / namespace 等），全部走 `pico/recovery_policy.py:133 command_risk_class` + `:439 evaluate_command_approval`；命令决策通过 `pico/tool_executor.py:_add_command_policy` 写入 metadata
- **E3.2 兑现** — `pico/recovery_policy.py:441-447` 4 个显式分支（`read_only` / `workspace_write` / `destructive` / `external_effect`）；`test_recovery_policy.py:15 test_command_policy_uses_four_risk_classes` 强证据
- **E3.3 兑现** — `evaluate_command_approval(risk_class)` 一个函数从 risk 决定 approval 决策；`test_recovery_policy.py:101 test_command_approval_is_risk_class_driven` 强证据
- **E3.4 兑现** — `command_risk_class` 是模式匹配式分类器（包含 shell wrapper 递归拆解、find -exec、env exec、shell substitution 等复杂情形），没有纯白名单；`test_recovery_policy.py:22/29/37/43/49/54/60/74/82/88/94` 一批**反例扫描测试**覆盖了 shell 组合、命令替换、env 前缀、find 递归等经典绕过尝试
- **E3.5 兑现** — `pico/cli.py:354 --approval choices=("ask", "auto", "never")`；`pico/tool_executor.py:183` 的 `if command_approval.get("decision") == "ask" and agent.approval_policy != "ask"` 分支说明三种模式都被处理
- **E3.6 兑现** — `pico/workspace_observer.py:16 WorkspaceObserver` + `pico/tool_executor.py:236-249` 在 run_shell 前后 capture + diff；shell_side_effects 从 delta 生成
- **E3.7 兑现（2026-07-06 复评后）** — 默认硬编码在 `recovery_policy.py:29 DEFAULT_MAX_BLOB_SIZE`；`pico.toml` 侧由 `pico/config.py:117 load_pico_toml` 解析 + `pico/config.py:project_max_blob_size` 兜底，`pico/runtime.py` 构造期缓存到 `self.project_max_blob_size`，`pico/tool_executor.py:540 / 623` 两处 `snapshot_eligibility` 调用透传该值。测试锁死：`tests/test_config.py:test_pico_toml_max_blob_size_overrides_snapshot_eligibility` + `:test_project_max_blob_size_falls_back_to_default_when_missing`
- **E3.8 兑现** — `pico/verification.py:5-116` 记录 command / risk_class / timing / exit / stdout 尾部；`pico/agent_loop.py:361 _verification_evidence_for_tool` 生成 evidence；`tests/test_verification_evidence.py` 存在
- **E3.9 兑现** — `pico/tool_executor.py:363 _add_command_policy` 把 command_risk + command_approval 挂到 metadata；`pico/tool_executor.py:405` 把 metadata 传给 finalize，最终进 tool_change_record；rejection 与 approval_required 分支也各自写 metadata（`:172-181` / `:186-196`）

### 维度状态描述

Safe Execution 层的**主策略是模式递归分析器**，不是黑名单。`_classify_shell_group`、`_classify_env`、`_classify_find`、`_extract_dash_c_payload`、`_classify_composite_shell` 等函数覆盖了很多 shell 绕过尝试。`test_recovery_policy.py` 里的 11 条反例测试是一个明显信号：这个模块被主动当作攻击面在测试。ADR-0014 说的“first-phase policy-based rather than OS sandbox”在代码里被诚实兑现——没有假装做 sandbox，也没有偷偷做。E3.7 的 `pico.toml` override 通道在 2026-07-06 复评时已经由 `project_max_blob_size` 打通并被测试锁死，本层 9 条期望全部兑现。

## 维度 4 — CLI Surface 一致性

### 期望清单

- E4.1 显式子命令为主，兼容 bare-prompt（ADR-0039）
- E4.2 首阶段 CLI 只暴露 checkpoint / restore / prune / inspect（ADR-0022）
- E4.3 README 里的命令表必须都能真实调用
- E4.4 `--format json` 用于机器可读输出
- E4.5 `restore` 与 `prune` 必须显式 `--apply` 才 mutate
- E4.6 `pico-cli doctor` 有 `--offline` 分支

### 逐条判定

- **E4.1 兑现** — `pico/cli_parser.py:6-18 KNOWN_TOP_LEVEL_COMMANDS` = {run, repl, init, status, doctor, config, runs, sessions, checkpoints, memory, help}；bare prompt 走 `parse_cli_invocation` 末尾的 `return CliInvocation("run", tokens, args, legacy_prompt=True)`；`test_recovery_cli.py:268 test_legacy_prompt_still_runs_one_shot` 锁死
- **E4.2 兑现** — 8 个 command spec 都属于 inspection / recovery / meta / config 类别（`pico/cli.py:48-56`），无产品化商业接口
- **E4.3 兑现** — README 命令 → 代码入口对应：`run`（`cli_parser.py:7`、`cli.py:517`）、`repl`（`cli_parser.py:8`、`cli.py:519`）、`help`（`cli.py:391`）、`status`（`cli.py:395`）、`doctor`（`cli.py:405`）、`doctor --offline`（`cli_diagnostics.py:123-132`）、`config show`（`cli.py:413`）、`runs list/show`（`cli_recovery.py:60-79`）、`sessions list/show`（`cli_recovery.py:82-106`）、`checkpoints list/show/preview-restore/restore/prune`（`cli_recovery.py:16-52`）
- **E4.4 兑现** — `--format text|json` 全局参数（`pico/cli.py:366`）；`pico/cli_output.py:35 print_result` 统一处理；`test_recovery_cli.py:385/398/409 test_*_json_uses_success_envelope` + `test_cli_diagnostics.py:20/59` 覆盖 JSON envelope
- **E4.5 兑现** — restore：`cli_recovery.py:30-38`；prune：`:39-52`，均以 `--apply` 判断；`test_recovery_cli.py:110/123/134/204` 覆盖预览与应用两条路径
- **E4.6 兑现** — `cli_diagnostics.py:123-132` 显式解析 `--offline`；`collect_doctor(cwd, args=None, offline=False)` 在离线模式下跳过 provider connectivity 检查

### 维度状态描述

CLI Surface 是被测试覆盖最密的层之一，`test_recovery_cli.py` 单文件就有 20 多条测试。命令表与 README 的一一对应关系可以直接从 `cli_parser.KNOWN_TOP_LEVEL_COMMANDS` + `cli.COMMAND_SPECS` + `cli_recovery.handle_*` 三层交叉验证。`--apply` 语义在 restore 与 prune 上是**对称的**（不加则预览、加了才写入），这是设计意图的直接落地。

## 维度 5a — 记忆子系统

### 期望清单

- E5a.1 pico 只读 AGENTS.md，不读 CLAUDE.md；doctor 给出提示
- E5a.2 User Notes 只读
- E5a.3 Agent Notes append-only + 原子写 + 单行 + 8000 字符软上限
- E5a.4 `pico-cli memory list/show/search/review/migrate` 全部实现
- E5a.5 REPL `/save`、`/memory-review`、`/memory` 一致
- E5a.6 Memory Index 稳定注入 prompt prefix
- E5a.7 Repo Map（`repo_lookup`）：Python AST + 其他语言正则

### 逐条判定

- **E5a.1 兑现** — `pico/cli_diagnostics.py:85-90` 显式检测“CLAUDE.md exists but AGENTS.md missing”并给出 `ln -s CLAUDE.md AGENTS.md` 提示；prefix 构建侧读的是 AGENTS.md（未见任何 CLAUDE.md 读入分支）
- **E5a.2 兑现（2026-07-06 复评后）** — `memory_save`（`pico/memory/tools.py:111`）在物理上只能写 `agent_notes.md`；此外 `write_file` / `patch_file`（`pico/tools.py:tool_write_file` / `tool_patch_file`）在入口处调用 `_refuse_user_notes_write`，任何解析到 `.pico/memory/notes/**` 下的路径都会返回 `"error: refusing to write user note path (read-only for agent): <path>"` 且不写盘。审批模式（`--approval auto` / `ask` / `never`）无关：这是路径级硬拦截，先于审批分支执行。测试锁死：`tests/test_safety_invariants.py:test_write_file_refuses_user_notes_path` + `:test_patch_file_refuses_user_notes_path`（后者用预置文件断言字节未变）
- **E5a.3 兑现** — `pico/memory/block_store.py:28 AGENT_NOTES_SOFT_LIMIT_CHARS = 8000`；`:109 append_agent_note` 走 append；`:122` 到软上限时打警告；原子写由 `_write_atomic` 保证
- **E5a.4 兑现** — `pico/cli_memory.py:9 handle_memory` 5 个子命令全部实现（list / show / search / review / migrate），带完整的 usage 与错误处理
- **E5a.5 兑现** — REPL 命令由 `pico/cli_start.py` 分发（参见 commit 185b8eb “extract memory command handlers into cli_memory”）；`/memory` 输出的 “Memory files:” 段格式与 README 一致（`cli_start.py:51`）
- **E5a.6 兑现（有说明）** — Memory Index 走 `pico/memory/refresher.py:MemoryRefresher._render_memory_index` 生成 `<memory_index>...</memory_index>` 文本，在 `pico/context_manager.py:146-147 composed_prefix_parts.append(memory_index_text)` 处**拼进 prompt**（不是拼进 `prompt_prefix.py` 的 stable prefix，而是在 context_manager 层拼装）；byte-identical 由 refresher 的 mtime 缓存保证（`refresher.py:38 _cached_memory_text`）。CONTEXT.md 说“injected into the stable prompt prefix”严格意义上有点不精确，实际实现是在 context 组装层拼接
- **E5a.7 兑现** — `pico/repo_map.py:tool_repo_lookup`（Python AST 精确 + TS / JS / Go / Rust 正则兜底）；文件头部注释 `pico/repo_map.py:7` 明确写“只做 repo_lookup tool 后端, 不塞进 prompt”，与 CONTEXT.md “Kept out of the prompt prefix; queried on demand” 一致

### 维度状态描述

记忆子系统在 **agent tool 层**干净：`memory_save` 无法写入 `notes/`，`memory_read` 只读。**通用文件工具层**在 2026-07-06 复评时补上了路径级硬拦截——`pico/tools.py:_refuse_user_notes_write` 在 `write_file` / `patch_file` 入口就拒绝 `.pico/memory/notes/**` 下的写入，`--approval auto` 时同样拦得住，两条新测试锁死。E5a.6 是文档描述与实现层次的轻微不一致，不是缺失。

## 维度 5b — 文档—代码对齐度

### 期望清单

- E5b.1 CONTEXT.md 43 条术语，每条能在代码里找到对应符号或概念
- E5b.2 39 份 ADR 的决定与当前代码一致
- E5b.3 docs/architecture 与 docs/memory-model 与代码同步
- E5b.4 README 命令表与 CLI parser 一致

### 逐条判定

- **E5b.1 兑现（含 2 条部分兑现）** — 43 条术语抽样检查：Coding-Agent Harness / CLI Surface / Recoverable Editing / Recovery Boundary / Checkpoint Record / Checkpoint Store / Checkpoint Pruning / File-State Blob / Workspace-Relative Path / Affected Path / Snapshot Eligibility / Turn Checkpoint / Restore Checkpoint / Automatic Checkpointing / Tool Change Record / Pending Tool Change / Interrupted Tool Change / Delegated Change / Trace Timeline / Recovery Manager / Restore Plan / Restore Preview / User-Initiated Restore / Selective Restore / Snapshot Restore / Restore Conflict / Git Review Context / Verification Evidence / Tool Effect Class / Safe Execution / Command Boundary / Command Risk Class / Command Approval / Shell Side Effect / AGENTS.md / User Notes / Agent Notes / Repo Map / Memory Index —— 其中 38 条在代码里有对应符号或字符串常量。**部分兑现 2 条**：Recovery Review 只作为字符串状态（`"review"`）存在，没有独立组件；Recovery Manager 与 Restore Plan / Restore Preview 三个概念在代码里共享 `RecoveryManager` 一个类，术语表把它们分开描述而实现上是耦合的
- **E5b.2 兑现（含 1 条部分兑现）** — 39 份 ADR 抽查：0001–0038 都能在代码里找到对应实现；ADR-0034（`pico.toml` 轻量 override）在代码中默认路径明确，但 override 加载路径未在本次评估中验证——部分兑现
- **E5b.3 兑现** — `docs/architecture/agent-harness-v1-overview.md` 的 8 步流程与 `agent_loop.py + runtime.py` 一致；`docs/memory-model.md` 的三层记忆（AGENTS.md / notes / agent_notes）与 `pico/memory/block_store.py` 完全一致
- **E5b.4 兑现** — 见 E4.3；README 命令表与 `cli_parser.KNOWN_TOP_LEVEL_COMMANDS` + `cli_recovery.handle_*` 完全对齐

### 维度状态描述

文档与代码的对齐度**罕见地高**。39 份 ADR 全部有可追溯的代码入口。术语表是 pico 一个明显的优势——它不是装饰，而是被真实使用的项目语言。**唯一系统性的漂移**：Recovery Review 在术语表里是一个“决策阶段”，在代码里退化成了一个字符串枚举值。要么升级实现，要么调整术语描述——现状二者之间。

## 维度 6 — 工程健康度（辅证）

- `./scripts/check.sh` 通过：ruff 无警告，pytest 452 通过 / 0 失败 / 72 秒
- 关键不变量测试**全部存在且强锁死**：`test_safety_invariants.py`（12 条）、`test_recovery_e2e.py`（6 条）、`test_recovery_policy.py`（14 条，含反例）、`test_recovery_manager.py`（6 条）、`test_recovery_paths.py`（3 条）、`test_checkpoint_store_phase1.py`（5 条）、`test_recovery_cli.py`（23 条）、`test_public_api_contract.py`（7 条，锁公共导入面）
- 近期重构落地质量：拆 provider（0161f07 / 46c954a / 3c8e1ca / fb699fd）、拆 cli_*（e55a9d8 / 185b8eb / d0f6e86 / 3306506 / 16f2e40）、拆 test 集群（b0bd30d / eebb671 / 48714ae）、拆 evaluator（04e33c5 / eb3f197 / fb728c5）——全部落在 `check.sh` 通过的分支上，说明重构**未破坏行为**
- 顶层模块尺寸：runtime.py 709 行、tool_executor.py 684 行、recovery_policy.py 510 行、tools.py 387 行——`runtime.py` 是唯一有轻微“上帝对象”味道的模块（59 个方法，横跨 workspace / model / trace / prompt / tool / verification 多个领域），但目前尚可控

## 跨维度重要发现

1. **文档与代码的对齐度是 pico 最强的一层。** 43 条术语 + 39 份 ADR + 3 份 architecture 文档都能在代码里找到对应实现，且 452 条测试没有失败。这在开源 harness 项目里不是常态。
2. **Recovery 层是最重也是最稳的。** 20 条 Recovery 承诺全部有实现，15 条有测试锁死。`_write_bytes_verified` 的 temp + hash + replace + reread 是“半恢复不可能”的物理保证，不是纸面承诺。
3. **Safe Execution 层是最有攻击面意识的。** `test_recovery_policy.py` 里 11 条测试专门验证 shell 绕过（组合命令、substitution、env 前缀、find -exec、-c 短标志），说明这个模块被主动当作对抗面处理。
4. **首轮找到的两条边界性弱项已在同日修复。** 见文末“本轮修复记录”：User Notes 加了路径级硬拦截，`pico.toml` 的 `max_blob_size` override 打通，`./scripts/check.sh` 480 tests 全绿。
5. **Recovery Review 是术语与实现的漂移点。** 词条描述像一个独立决策阶段，代码里只是 `_plan_entry` 的字符串返回值加一段说明。不是缺陷，但值得澄清。

## 客观定位

Pico 处于 **“第一阶段垂直切片已完整闭环、正在做架构收敛”** 的成熟度。证据：

- ADR-0019 定义的“first vertical slice”8 项能力全部有代码 + 测试
- ADR 里所有 “first-phase” 承诺都兑现（不做 sandbox、不做自动 GC、不做 hunk restore、不做全量 snapshot、不做三方合并）
- 近期 refactor commit 主题清一色是“拆分 / 提取 / colocate / 收敛”，而不是“补功能”（0161f07 拆 provider、b0bd30d 拆 test cluster、90db0ae 拆 experiment cluster）
- 存在一份“架构收敛 design + rev2 + pre-flight audit + hardened plan”的连续 spec 序列（20c7b3f、94a2bd7、10954fd、8508955、30b8cb0），说明团队清楚“当前阶段任务是收敛而不是扩展”

比较刻度：这不是“聊天玩具”或“README 驱动的项目”，也不是“产品化商业 harness”。它是**一个把自己的 harness 语言先写清楚、然后在测试锁死下逐步实现的第一阶段工程**——离产品化还有距离（缺 UI、缺全局 config profile、缺跨 project 视图），离“能被别人拿来复现和研究”已经很近。

## 明显短板（只列有证据的）

1. **Recovery Review 概念缺少独立组件。** 见 E5b.1。术语表把它描述成一个“决策点”，实现是 `_plan_entry` 返回的字符串。要么在 `RecoveryManager` 之外抽出 `RecoveryReview` 组件（承接 conflict / ineligible / review-required 三类条目），要么在术语表里把它说清楚为“decision status inside RecoveryManager”。
2. **`runtime.py` 有轻微上帝对象趋势。** 709 行，`Pico` 类 59 个方法，横跨 workspace / model / trace / prompt / tool / verification / secret 多个领域。当前重构主题（拆 cli_* / 拆 provider）已经在收敛外围，`runtime.py` 是下一个自然的拆分候选，但目前还没超过“可维护”的阈值。
3. **`context_manager.py:146` 的 memory_index 拼接与文档描述的“stable prompt prefix”层次不一致。** 见 E5a.6。要么更新 CONTEXT.md 说明它在 context 组装层拼接、byte-identical 由 refresher 缓存保证，要么把它移到 `prompt_prefix.py`。

## 本轮修复记录（2026-07-06 同日修复循环）

首轮发现的 5 条短板中，2 条已闭合，剩下 3 条见上。

- **User Notes 只读升级为路径级硬拦截。**
  - 新增：`pico/tools.py:_refuse_user_notes_write` + `USER_NOTES_PROTECTED_PREFIX = (".pico", "memory", "notes")`
  - 接入：`tool_write_file` / `tool_patch_file` 入口调用；先于 approval 分支执行
  - 行为：返回 `"error: refusing to write user note path (read-only for agent): <path>"` 且不写盘
  - 测试锁死：`tests/test_safety_invariants.py:test_write_file_refuses_user_notes_path` + `:test_patch_file_refuses_user_notes_path`（后者用预置文件断言字节未变）
- **`pico.toml` 的 `[policy] max_blob_size` override 通道打通。**
  - 新增：`pico/config.py:project_max_blob_size(workspace_root)`（解析 → 类型/范围校验 → 缺失时回退到 `DEFAULT_MAX_BLOB_SIZE`）
  - 缓存：`pico/runtime.py Pico.__init__` 构造期读一次，挂到 `self.project_max_blob_size`
  - 消费：`pico/tool_executor.py:540 / 623` 两处 `snapshot_eligibility(...)` 透传 `max_blob_size=agent.project_max_blob_size`
  - 测试锁死：`tests/test_config.py:test_pico_toml_max_blob_size_overrides_snapshot_eligibility` + `:test_project_max_blob_size_falls_back_to_default_when_missing`
- **回归基线**：`./scripts/check.sh` → **480 tests / 0 失败 / 73 秒**（初评时为 452 tests）

---

**评估基线复现命令**：`./scripts/check.sh`
