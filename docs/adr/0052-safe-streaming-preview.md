# ADR-0052: Streaming 只提供安全瞬态文本 Preview

## Status

Proposed。完整方案见[Model Target、预算、工具结果与 TUI 设计](../model-target-and-budget-design.md)。

本 ADR 修订 `AGENTS.md` 中 Pony 1.0 不实现 streaming 的旧产品边界；Provider reasoning、tool argument preview、持久化
partial transcript、请求取消、非 TTY streaming 和协议 fallback 仍不属于 Pony 1.0。

## Context

Pony 的四个 production adapter 当前都等待完整 response body 后返回 `Response`。TUI 只能显示 `Working...`，用户看不到模型
是否开始生成，真实 TTFT 也不可观察。直接打印 wire delta 并不安全：secret、UTF-8 和协议对象可能跨 chunk，reasoning 与 tool
arguments 也可能混在增量事件中；把 partial text 写入 Session 或 trace 会产生第二份、不完整且可能与 Final 冲突的 transcript。

四种既有协议又有不同流事件：Anthropic Messages SSE、OpenAI Responses SSE、OpenAI Chat Completions SSE 和 Ollama
NDJSON。共享 framing 不等于共享 wire contract，尤其是 tool assembly、usage、stop reason 和终止条件。

## Decision

### 产品范围

- Streaming 首版仅由交互入口的 `--stream` 显式启用，并只在完整 TUI 可用时工作；默认、`pony run`、非 TTY、doctor、probe、
  resolution、compaction、benchmark grading 和 delegate 保持 final-only。
- 该请求选项只进入 transient `RuntimeOptions`，不增加 `.env`/`pony.toml` 字段，也不作为配置或恢复状态持久化到 Session/Run；
  当前 Run 仍可记录不含内容的低敏执行事实。
- 现有 ModelClient `complete()` 不变；production adapter 另提供
  `complete_stream(..., on_stream_committed=..., on_text_delta=...)`。显式启用但 client 不支持时，在请求前返回
  `streaming_unavailable`，不静默 fallback。
- CLI 在 `build_agent` 前拒绝 `run`/管理命令和非完整 TUI 的 `--stream`；assembly 在得到 client 后、构造或 resume `Pony`
  前检查 `complete_stream`。Python API 在 `_initialize()` 的任何 writer 前防御性检查，`run_repl` 在 plain fallback 前再复核。
  因此失败不创建 Session 目录、不改变已有 revision，也不触发 compaction/network。无 flag 的 custom client 合同不变，
  delegate/worktree child 不继承 stream。
- `complete_stream` 的请求参数与 `complete` 相同：keyword-only `system/tools/messages/max_tokens/cache_breakpoints=None`，另加
  required `on_stream_committed()` 和 `on_text_delta(str)`，返回终态 `Response`。Adapter 在 Provider worker thread 串行调用；先
  内部 committed、再恰好一次 committed callback、再按序发送零到多次普通文本 delta。callback 返回值被忽略；异常只禁用
  后续 callback，不影响 assembly，方法结束后无迟到调用。`on_safe_preview(...)->bool` 只是 Agent 内 `SafeTextPreview` 的 TUI sink。

### Provider 与终态

- 标准库 transport 提供有界、严格 UTF-8 的 SSE/NDJSON framing；四个 adapter 各自解析事件、组装 tool/content/usage/state。
  每个 adapter 的非流与流路径共用一个协议私有终态 decoder 并返回等价 `Response`；多 tool/text 冲突继续由
  `decode_action()` 统一处理，流路径不增加更严格语义。
- Chat Streaming 固定发送标准 `stream_options={"include_usage": true}`，接受 `[DONE]` 前 `choices=[]` 的 usage-only chunk；
  endpoint 拒绝该字段时准确失败且不重发，终态 usage 缺失时沿用现有 degraded usage 事实。
- Adapter 只向 callback 发送已经识别为普通 Assistant 文本的 delta。reasoning/thinking、tool arguments、opaque state、
  refusal/error detail、request id 和 usage 不进入 callback。
- Agent 只有在流完整结束、终态 `Response` 校验且 `decode_action()` 成功后才执行 Tool 或提交 Canonical Messages。
- 每个 Model Attempt 只发送一次 HTTP 请求。首个有效 event 前的 retryable failure 只能进入新的 Agent retry attempt；任一有效
  wire event 后请求视为 committed，后续失败全部不可 retry，不切换非流、protocol、endpoint 或 model 重放。

### 安全 Preview

- Agent-side `SafeTextPreview` 有界累计完整逻辑行，再调用现有 secret redaction 和 terminal sanitization；不得逐 delta 直接显示，
  也不得读取终端宽度或裁剪显示文本。构造时若冻结 redaction snapshot 含 CR/LF，或任一已知 secret 短于
  `MIN_SECRET_SUBSTRING_REDACTION_LENGTH`，则本次文本 preview fail closed。若后续完整行包含 private-key BEGIN marker，必须在
  显示该行前永久禁用本次剩余 preview，不能依赖只对单行调用的 PEM block redactor。
- TUI 在单一动态 activity 区原位展示最新一条完整、有界的安全 snapshot，并独占前缀、当前宽度裁剪与 resize 重投影，复用
  现有单行清除原语。未完成单行等待 Final；单行超过 64 KiB 时停止文本 preview，保留 `Receiving...`，但继续完成权威
  Response。
- Preview callback 生命周期仅限当前 `complete_stream()`，没有 durable writer 能力；内容不进入 Session、Run trace、Canonical
  Messages、Checkpoint、Memory、log、scrollback 或 artifact。
- Adapter 在首个合法 wire event 后先冻结 committed 状态，再调用无 payload 的 `on_stream_committed()`；它只切换
  `Working -> Receiving`，不携带事件或计入 preview latency。`on_safe_preview(snapshot) -> bool` 只有在 TUI 同步显示至少一个模型文本
  字符时返回 true；false/异常只禁用本次 preview，不取消 Provider 请求或覆盖主结果。
- success、Provider error、真实 Provider interruption、terminal turn failure 和 close 均幂等清理，Final Markdown 只渲染一次。
  busy Ctrl+C 仍只清 pending queue，不能取消或伪装取消当前流。
- 首个安全 snapshot 同步渲染一次以取得 acknowledgment；后续更新进入单槽 latest-snapshot coalescer，由 prompt-toolkit event
  loop 以不高于 10 Hz 刷新，Provider reader 不等待 UI。Final/error/close 先使 preview generation 失效，迟到刷新为 no-op。
- 现有 durable trace listener 顺序不变。Streaming 不通过 listener 发送伪 trace event；证据只进入既有
  `request_metadata.streaming={requested, committed, preview_emitted, first_preview_ms}`。`model_requested` 写初始 snapshot，成功
  `model_turn`/失败 `model_failed` 写终态 snapshot；布尔只 false→true，preview 蕴含 committed，time 非空当且仅当 preview。
  final-only 省略该 key。为该 nested value 增加 exact-key/type/invariant validator，不新增 trace envelope 字段或 schema version；
  禁止 delta、文本和 wire event。

### 资源与测量

- 流固定受单行 256 KiB、总响应 16 MiB、100000 个 wire event 和现有 socket timeout 约束；超限稳定失败且不返回部分
  `Response`。
- `first_preview_ms` 表示从 Agent 即将调用 `complete_stream()` 到首个实际可见、已脱敏的非空文本 preview；它是
  safe-preview latency，不冒充排除 adapter payload 构造时间的 wire-level TTFT。使用可注入 monotonic clock，只有
  `on_safe_preview()` 首次返回 true 才记录；false、异常、重复 callback、prefix、首 wire event、`Receiving...`、完整 body 和
  首 action 都不是 TTFT。
- Streaming 不改变 128K/16K 或显式 256K/16K 请求预算、output enforcement、计费或 Session binding。

## Consequences

- 用户能确认模型已经开始响应，并看到有界文本反馈；最终 transcript 仍只有一次权威 Markdown Answer。
- 未知 model 只要支持所选 wire protocol 的标准流事件即可使用，不需要型号表或能力 catalog；偏离协议时返回准确错误。
- 首版对没有换行的长段落只能显示 `Receiving...`，直到 Final 到达。这是防止跨 chunk secret 泄露的有意边界；真实可用性证据
  证明需要更细粒度预览后，才可单独设计可证明安全的 overlap redactor。
- 四个 adapter 增加各自的流 parser 和 fixture，但不新增运行时依赖、通用 Provider event model 或第二事件总线。
