# ADR-0044: Provider auto resolution before user requests

## Status

Accepted for Pony 1.0 before its first release tag.

## Context

`PONY_PROVIDER=openai` previously selected Responses or Chat Completions from the API hostname and assigned optional wire capabilities to every compatible gateway. A real gateway accepted text but failed the native-tool contract, while the CLI collapsed the reason into a generic runtime failure. Requiring users to understand Provider brand, wire variant, and optional capabilities duplicates knowledge Pony can verify with a bounded synthetic request.

## Decision

- `PONY_PROVIDER` is optional. Missing, empty, or `auto` selects bounded auto resolution; `openai` resolves within the OpenAI family; `openai-chat` and `openai-responses` force one wire protocol.
- `pony init` is the only command that both detects and persists a resolved Provider. `doctor --check-api` is read-only, while run/repl use an unresolved result only in their current process.
- Auto resolution runs before Pony sends a user request. It uses only a fixed `pony_probe` tool call and fixed continuation, never repository, Memory, or user-task content.
- Candidate requests remain on the configured origin, have fixed call/token/time limits, and stop on transient endpoint failures. A real user request is never replayed through another protocol after failure.
- Detection uses at most three candidates and two requests per candidate. Each request is capped at `min(user timeout, 30s)`, total wall time is 90 seconds, and detection performs no retry.
- Timeout, TLS, redirect, rate-limit, and 5xx failures stop detection. Deterministic 4xx and protocol mismatch may advance; global auto may cross authentication families after 401/403, while the OpenAI family may not.
- Exact official endpoints may enable verified optional capabilities. Generic gateways use a conservative profile and must pass native tool-call and tool-result continuation checks.
- Session format does not change. A matching current Session binding can resolve the protocol without network access.
- Ordinary benchmark workloads require a resolved target and fail closed when probing would be required. The paid live harness invokes the shared resolver before its workload, creates a fresh production client after probing, and reports bounded resolution calls separately. Neither path owns a second detector or persists resolution results.

## Consequences

- The former hostname-only OpenAI selection and broad “OpenAI-compatible” support claim are removed.
- Old binaries fail closed on the new `openai-chat` and `openai-responses` values. No compatibility or downgrade writer is provided.
- Configuration inspection and ordinary doctor remain offline. Detection can incur bounded Provider charges only in init, run/repl resolution, or explicit `doctor --check-api`.
- Provider protocol failures retain safe stage and reason metadata instead of exposing raw responses or collapsing to `agent runtime failed`.
- Detection observability is a bounded `provider_resolved` trace containing only source, protocol, candidate count, probe-call count, and usage status.
