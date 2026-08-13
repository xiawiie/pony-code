# ADR-0051: 任意 model id 使用协议基线

## Status

Accepted，2026-08-13。实现说明见[Model Target 与预算设计](../model-target-and-budget-design.md)。

## Context

旧实现用单项 `BUILTIN_MODEL_CAPABILITIES` 按 model name 区分“已知/未知”，并为未命中项打印 conservative fallback
warning。这把 Pony 请求预算误写成型号物理能力，也让正常 custom model path 看起来像降级。另一方面，OpenAI Responses 与
Chat Completions 虽属于同一产品 family，却有不同 endpoint、消息结构、tool continuation 和 opaque state，不能为了消除重复
而合并 wire codec。

目标是在不增加动态 Provider registry、在线 catalog、第二配置面或真实任务 fallback 的前提下，让任意合法 model id 使用
现有四种协议基线，并保持预算和可选 wire behavior 的边界可解释。

## Decision

- exact Target identity 继续由 `protocol_family + model + endpoint_hash` 构成。model id 是不透明路由值，只做现有结构与
  secret 校验，不查型号表，不决定准入或预算。
- 删除 `BUILTIN_MODEL_CAPABILITIES` 和 unknown-model warning。预算优先级按字段为 CLI explicit、`pony.toml` explicit、
  Pony default；默认保持 128,000 context / 16,384 output，不静默迁移到 32K/8K。
- 默认值是 Pony 请求策略，不是远端物理上限。用户可用现有 CLI/`pony.toml` 显式设置 256K/32K 等完整预算；非法配置
  回到默认但不标为显式来源。runtime 不根据 model name 自动换档。
- 任意合法 model id 直接使用所选 Transport 的 Protocol Core。future/unknown model 不 warning、不强制 probe，也不获得
  未证明的 optional wire behavior。
- protocol/endpoint 已证明的字段与 continuation 合同可以按 exact protocol/endpoint scope 应用；strict、parallel 等型号增强
  只能以有证据的 exact Target scope 应用。两者都只影响所属 adapter 的 serialization、parsing 或 opaque state replay，不参与
  Target 准入、Request Budget、probe、UI 推荐或 Session 切换边界。
- OpenAI Responses 与 Chat Completions 可共享 User-Agent、canonical system 文本、function schema normalization 和
  optional-null cleanup；必须分别拥有 endpoint、request/response codec、tool continuation、opaque state 与未来 streaming
  parser。strict schema normalization 要求 object 根，递归覆盖 object、array 与 `anyOf`，移除不支持的 `default` 注解，
  对无法安全转换的根 `anyOf` 与嵌套 `oneOf`/`allOf` fail closed，并只清除由 optional nullable 编码产生的 null。
  Chat 不再反向导入 Responses 私有 helper。
- `openai`/`auto` 对不明确 OpenAI-compatible endpoint 按 Responses、Chat 顺序执行已有 bounded synthetic resolution；
  timeout、TLS、redirect、rate-limit、5xx 与认证失败停止；明确 protocol mismatch 或确定性非认证 4xx 可尝试下一
  candidate。真实用户任务失败后绝不切换协议重放。
- `doctor --check-api` 仍是用户显式、只读的 exact Target 当次证据，不写 catalog、Session 或资格缓存。普通 status/doctor
  零网络。
- Streaming 与 transport cancellation 不属于本次决策。未来实现必须为 Responses typed SSE 与 Chat chunk sequence 保持
  独立 parser，并保证 partial delta 不进入 Canonical Messages、不触发 Tool。

## Consequences

- 新 OpenAI、Anthropic、OpenAI-compatible gateway 或 Ollama model id 只要遵循已选协议 baseline，即可零登记进入 runtime。
- Pony 不再把“未见过型号”描述为降级，也不会把 128K/16K 冒充远端上限；过小 deployment 仍需显式预算或返回稳定错误。
- OpenAI 两套 adapter 删除了错误的私有依赖，但没有制造一个同时处理两种协议的条件分支 codec。
- exact Target 型号增强不是 catalog：未命中仍正常使用 Protocol Core 与已证明的 endpoint 字段合同，不会被拒绝或改变预算。
- Chat generic compatible endpoint 保守发送既有 `max_tokens`；官方 Chat endpoint 按当前协议使用
  `max_completion_tokens`。官方 Responses stateless reasoning continuation 保持 endpoint scope；这些选项都不按 model name
  猜测，也不会赋予未知 compatible endpoint 官方能力；strict 与 parallel 型号能力仍保持 exact Target scope。
